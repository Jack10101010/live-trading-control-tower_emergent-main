"""Deterministic Pine generation and single-file assembly (Stage S1).

    python -m tools.oracle.generate_pine [--write]

Reads ``contracts/tradingview_oracle_contract.json``, generates the two GENERATED
fragments (identity header + contract constants), assembles them with the
hand-written fragments in filename order, and writes the single ``.pine`` file
TradingView needs plus the parity manifest.

DETERMINISM IS THE POINT
------------------------
Identical inputs must produce a byte-identical artefact, or the source hash in
the manifest cannot detect hand-editing. Two consequences:

  * ``ORACLE_GENERATED_AT`` is derived from the CONTRACT, not the wall clock. A
    wall-clock stamp would make every regeneration a different file and destroy
    the tamper check. The volatile "when did I run this" metadata lives in the
    manifest instead, where it is not content-addressed.
  * no absolute paths are ever emitted — they would embed the build machine.

WHAT WILL MAKE THIS REFUSE TO RUN
---------------------------------
  * the contract fingerprint does not match the deployed engine
  * an unsupported production feature is active
  * a production enum value has no Pine mapping
  * a hand-written fragment is missing
  * a generated fragment was edited by hand
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle import fingerprint as fp
from tools.oracle import parity_status as ps
from tools.oracle import session_codes as codes_mod
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError,
                                        effective_policy_table, load_engine,
                                        resolve_config)
from tools.oracle.export_trace import TIMEFRAME_MINUTES
from tools.oracle.extract_contract import DEFAULT_OUT as CONTRACT_PATH

SRC_DIR = CT_ROOT / "pine" / "src"
GEN_DIR = CT_ROOT / "pine" / "generated"
ARTIFACT_DIR = CT_ROOT / "artifacts" / "tradingview_oracle"

# ── BUILD TARGETS ────────────────────────────────────────────────────────────
#
# WHY TWO BUILDS
# Production has two time domains and they are not interchangeable.
# `run_backtest` resamples to 15m to DETECT order blocks, then hands
# `simulate_trades` the raw 1-minute candle file. Arming, the 3-candle delay and
# the containment touch are therefore minute-by-minute, and a 15-minute bar —
# four numbers — cannot order events inside itself. Measured over the chart
# window: 33 of 58 resolved setups (56.9%) arm and finish inside ONE 15m bar.
#
# So S1-S6 (structure, detection, daily state) live on a 15-minute chart, and
# S7+ (the execution walk) live on a 1-minute chart. Merging them would mean one
# script silently deciding which domain each value belongs to, which is exactly
# the ambiguity this apparatus exists to remove.
#
# Both targets derive from the SAME contract and the SAME governed engine. What
# is per target: the timeframe, the owned stages, the fragments, the generated
# file, the manifest, the plot budget, the export schema and the evidence.
#
#: target id -> build definition. `stages` is what the target OWNS; a stage
#: appears in exactly one target, so an evidence record can never be filed
#: against a build that does not contain it.
BUILD_TARGETS = {
    "detection_15m": {
        "target_id": "detection_15m",
        "label": "Detection oracle (15m)",
        "timeframe": "15min",
        "chart_timeframe_seconds": [900],
        "stages": ("S1", "S2", "S3", "S4", "S5", "S6"),
        "indicator_title": "TV Visual Oracle S1 — {symbol}",
        "indicator_short": "TVO-S1",
        "pine": GEN_DIR / "tradingview_visual_oracle_detection_15m.pine",
        "manifest": ARTIFACT_DIR / "parity_manifest_detection_15m.json",
        "fragments": ("10_inputs", "20_data_context", "30_time", "40_sessions",
                      "45_volatility", "50_visuals", "55_swings", "58_structure",
                      "59_structure_visuals", "65_order_blocks", "66_regime",
                      "67_replay_visuals", "68_live_setups",
                      "60_parity_output", "70_legend", "90_debug", "99_hud"),
        "plot_reserve": 2,
        "declaration": None,
        "embed": ("replay", "targets", "news"),
    },
    "execution_1m": {
        "target_id": "execution_1m",
        "label": "Execution oracle (1m)",
        "timeframe": "1min",
        "chart_timeframe_seconds": [60],
        "stages": ("S7",),
        "indicator_title": "TV Visual Oracle S7 — {symbol} (1m execution)",
        "indicator_short": "TVO-X1",
        "pine": GEN_DIR / "tradingview_visual_oracle_execution_1m.pine",
        "manifest": ARTIFACT_DIR / "parity_manifest_execution_1m.json",
        "fragments": ("x10_inputs", "x20_data_context", "x30_detection_frame",
                      "x60_parity_output", "x99_hud"),
        # Deliberately generous. The execution oracle starts from zero and S8+
        # will need room; spending the budget on S7 debug plots now is how the
        # detection build reached 83 and hit RE10140 on a live chart.
        "plot_reserve": 8,
        "declaration": None,
        "embed": (),
    },
    # ── the Strategy Tester companion ────────────────────────────────────────
    #
    # A `strategy()`, not an `indicator()`, and the ONLY build that is one. It
    # exists because the Strategy Tester will not run an indicator, and the
    # Visual Oracle must stay an indicator: converting it would put orders
    # inside the artefact whose whole purpose is to show what production did.
    #
    # It OWNS NO STAGES. It reuses the detection build's compute fragments
    # verbatim — same swings, same structure, same order blocks, same sessions,
    # same market state — so there is one implementation, not two. Parity
    # evidence is recorded against `detection_15m`, where those stages live; a
    # second claim here would be the same measurement filed twice under two
    # names, and the first question would be which copy is authoritative.
    #
    # What it adds is the order lifecycle, and that is NOT parity-claimable at
    # all: production executes on 1-minute candles and this chart is 15-minute.
    # The script says so on its own panel, unsuppressibly.
    "strategy_companion": {
        "target_id": "strategy_companion",
        "label": "Strategy Tester companion (15m)",
        "timeframe": "15min",
        "chart_timeframe_seconds": [900],
        "stages": (),
        "indicator_title": "TVO Strategy Companion — {symbol}",
        "indicator_short": "TVO-SC",
        "pine": GEN_DIR / "tradingview_strategy_companion_15m.pine",
        "manifest": ARTIFACT_DIR / "parity_manifest_strategy_companion.json",
        "fragments": ("s10_inputs", "20_data_context", "30_time", "40_sessions",
                      "45_volatility", "55_swings", "58_structure",
                      "65_order_blocks", "66_regime", "s90_strategy"),
        "plot_reserve": 8,
        # THE COST MODEL, and the reason `slippage` is zero.
        #
        # Production books cost as a FRACTION OF R: 0.6 pip round trip against
        # the trade's own stop distance. TradingView offers two places to put
        # that — `slippage`, in ticks, and `commission`. Slippage in ticks is a
        # fixed price offset, so on an 8.6-pip stop it is 7% of R and on a
        # 30-pip stop it is 2%: the same setting, two different costs. That is
        # not what production does.
        #
        # Cash-per-contract with risk-derived sizing IS what production does.
        # Quantity = risk / stop distance, so a fixed cash charge per unit is a
        # fixed fraction of R at every stop distance. Verified against S_2094
        # (8.6-pip risk): 2 * 0.00003 / 0.00086 = 0.0698, which is the
        # `total_cost_r` production recorded for that trade.
        #
        # Slippage is therefore ZERO — not because production is frictionless,
        # but because its friction is already fully represented here and
        # charging it twice would be the easy way to make the headline look
        # conservative while being wrong.
        "declaration": (
            'strategy("{title}", "{short}",\n'
            "     overlay = true,\n"
            "     // Production's rail is `max_open_positions = 6`\n"
            "     // (live/config.py). The historical maximum actually observed\n"
            "     // is 2 — that is evidence about a quiet sample, not the\n"
            "     // strategy's authority, and sizing the cap to it would\n"
            "     // silently forbid a seventh trade production permits.\n"
            "     pyramiding = 6,\n"
            "     initial_capital = 100000,\n"
            "     default_qty_type = strategy.fixed,\n"
            "     default_qty_value = 1,\n"
            "     // See the cost model note in generate_pine.py's build target.\n"
            "     slippage = 0,\n"
            "     commission_type = strategy.commission.cash_per_contract,\n"
            "     commission_value = 0.00003,\n"
            "     // FALSE, deliberately. An order submitted at a bar's close is\n"
            "     // then active from the NEXT bar, so the 3-minute delay is\n"
            "     // satisfied with 12 minutes to spare and can never be\n"
            "     // violated. True would fill at the close of the bar the arm\n"
            "     // was detected on — the arm==fill defect, rebuilt.\n"
            "     process_orders_on_close = false,\n"
            "     calc_on_order_fills = false,\n"
            "     max_labels_count = 500, max_boxes_count = 500,\n"
            "     max_lines_count = 500)"),
        # It needs the 144-cell target table and the news schedule (both are
        # fill-time authority) and has nothing to do with the replay recording.
        "embed": ("targets", "news"),
        "authority": (
            "// Python is the sole source of truth. Where this strategy and "
            "the production\n// engine disagree, THIS STRATEGY IS WRONG — and "
            "on the execution lifecycle it\n// cannot be right, because "
            "production executes on 1-minute candles and this\n// chart is "
            "15-minute. Nothing here is a parity claim."),
    },
}

DEFAULT_TARGET = "detection_15m"

#: Backwards-compatible aliases for the detection build. Every existing reader
#: (verify_s1, record_parity, the test suite) predates targets and means "the
#: 15-minute oracle" when it says "the oracle".
OUT_PINE = BUILD_TARGETS["detection_15m"]["pine"]
MANIFEST_PATH = BUILD_TARGETS["detection_15m"]["manifest"]

#: The pre-targets manifest. It meant "the 15-minute oracle", so it migrates into
#: `detection_15m` exactly once — see `carry_forward_evidence`.
LEGACY_MANIFEST_PATH = ARTIFACT_DIR / "parity_manifest.json"


class UnknownTarget(RuntimeError):
    pass


def resolve_target(target=None) -> dict:
    """Look a target up, refusing an unknown name rather than defaulting.

    Defaulting on a WRITE would let `--target executon_1m` (typo) silently
    regenerate and overwrite the detection oracle, which is the one build with
    recorded evidence against it.
    """
    name = target or DEFAULT_TARGET
    if name not in BUILD_TARGETS:
        raise UnknownTarget(
            f"unknown build target {name!r}; known targets: "
            f"{sorted(BUILD_TARGETS)}")
    return BUILD_TARGETS[name]


def target_for_stage(stage: str):
    """Which build owns a stage. One owner, always — a stage in two builds would
    have two source hashes and two evidence trails for one claim."""
    for name, spec in BUILD_TARGETS.items():
        if stage in spec["stages"]:
            return name
    return None


ORACLE_VERSION = "0.1.0"
#: Stage vocabulary:
#: IMPLEMENTATION status only — "does this build contain the stage at all". It is
#: a property of the generated artefact, so it is derived here and NEVER carried
#: across a regeneration.
#:
#:   UNIMPLEMENTED — no Pine code exists for it
#:   IMPLEMENTED   — Pine code exists and the Python side passes
#:
#: Whether the stage is CORRECT is a different question with three different
#: answers (logic / feed / production-replay), recorded per stage by
#: `verify_s1 --record` and defined in `tools/oracle/parity_status.py`. Nothing
#: here is ever hand-promoted: compiling and rendering are not evidence.
STAGE_STATUS = {
    "S1": ps.IMPL_IMPLEMENTED,
    "S2": ps.IMPL_IMPLEMENTED,
    "S3": ps.IMPL_IMPLEMENTED,
    "S4": ps.IMPL_IMPLEMENTED,
    "S5": ps.IMPL_IMPLEMENTED,
    "S6": ps.IMPL_IMPLEMENTED,
    "S7": ps.IMPL_UNIMPLEMENTED,
    "S8": ps.IMPL_UNIMPLEMENTED, "S9": ps.IMPL_UNIMPLEMENTED,
    "S10": ps.IMPL_UNIMPLEMENTED, "S11": ps.IMPL_UNIMPLEMENTED,
    "S12": ps.IMPL_UNIMPLEMENTED, "S13": ps.IMPL_UNIMPLEMENTED,
    "S14": ps.IMPL_UNIMPLEMENTED,
}

#: Stage dependency. A failure invalidates everything downstream — S1 feeds every
#: bar S2/S3/S4 are computed on, S3's swings are what S4 breaks, and S5's boxes
#: are anchored to S2's parsed prices at a bar chosen between S3's pivot and S4's
#: break, so it depends on all four.
#:
#: S6 is declared even though it is unimplemented. An UNDECLARED stage makes
#: `evaluate_gate` return OPEN on an empty dependency list — i.e. "you may
#: begin S6" — which is the one answer a gate must never give by accident.
STAGE_DEPENDS_ON = {"S1": [], "S2": ["S1"], "S3": ["S1"], "S4": ["S1", "S3"],
                    "S5": ["S1", "S2", "S3", "S4"],
                    # S6 reads ONLY the bar series — it aggregates UTC days and
                    # runs three indicators over them. It never touches a swing,
                    # a break or an order block, so S2-S5 are not prerequisites.
                    "S6": ["S1"],
                    # S7 admits candidates from order blocks under a market
                    # state, so it needs both branches.
                    "S7": ["S1", "S2", "S3", "S4", "S5", "S6"],
                    # S8 evaluates policy on a candidate S7 delivered to the
                    # gate. Declared even though it is unimplemented: an
                    # UNDECLARED stage makes `evaluate_gate` return OPEN on an
                    # empty dependency list — "you may begin S8" — which is the
                    # one answer a gate must never give by accident.
                    "S8": ["S1", "S2", "S3", "S4", "S5", "S6", "S7"]}

#: Fragments assembled in this exact order. Generated ones are produced here;
#: the rest are read from disk.
GENERATED_FRAGMENTS = ("00_generated_header", "05_generated_contract",
                       "06_generated_replay", "07_generated_targets",
                       "08_generated_news")
#: Assembly order is load-bearing: Pine resolves top-to-bottom, so a fragment may
#: only reference what an EARLIER fragment declared. 45 (volatility) precedes the
#: visuals; 55 (swings) precedes 58 (structure) because breaks consume swings;
#: 59 draws them; 60 exports; 90/99 read everything.
#: The DETECTION target's fragment list under its historical name, so readers
#: that predate build targets still resolve. ALIASED rather than duplicated:
#: keeping a second copy in step was a manual step, and adding
#: `67_replay_visuals` to one and not the other is exactly what it failed at.
HAND_FRAGMENTS = BUILD_TARGETS["detection_15m"]["fragments"]

#: Limitations ACTIVE for an S1 build (ids from audit §12, plus S1 additions).
S1_LIMITATIONS = [
    {"id": "L-02", "approximation_label": "FEED-DEPENDENT",
     "expected_divergence": "TradingView builds its own bars from its own feed; "
                            "bar counts may differ from a pandas resample of the "
                            "frozen+live M1 series",
     "measured_divergence": None},
    {"id": "L-06", "approximation_label": "UTC-DAY RECONSTRUCTED",
     "expected_divergence": "none, provided the UTC day is computed from the bar "
                            "timestamp rather than a daily security() call",
     "measured_divergence": None},
    {"id": "L-09", "approximation_label": "INDEX-LOCAL",
     "expected_divergence": "bar_index diverges wherever the feeds disagree about "
                            "missing bars; compare index DELTAS only",
     "measured_divergence": None},
    {"id": "L-12", "approximation_label": "EXECUTION-STATE UNKNOWN",
     "expected_divergence": "unbounded — Pine has no broker, ledger or account state",
     "measured_divergence": None},
    {"id": "L-15", "approximation_label": "SHADOW MODE",
     "expected_divergence": "n/a — informational banner",
     "measured_divergence": None},
    {"id": "L-19", "approximation_label": "BUILD-TIME IDENTITY",
     "expected_divergence": "Pine cannot query the repository, so the HUD reports "
                            "what the build MIRRORS, never whether the deployed "
                            "engine still matches; freshness is a repo-side answer",
     "measured_divergence": None},
    {"id": "L-21", "approximation_label": "INTRABAR-EXECUTION-PATH",
     "expected_divergence": "unbounded for S7 and later. `simulate_trades` runs "
                            "on the 1-MINUTE candle file; only order-block "
                            "detection uses the 15-minute resample. The arm "
                            "test, the 3-candle delay and the containment touch "
                            "are minute-by-minute, and a 15-minute bar carries "
                            "four numbers — it cannot order events inside "
                            "itself",
     "measured_divergence": "109/109 setups reproduced against production over "
                            "the chart window; 33 of 58 resolved setups (56.9%) "
                            "arm and finish inside ONE 15m bar — see "
                            "artifacts/tradingview_oracle/"
                            "s7_selfcheck_chart_window.json"},
    {"id": "L-22", "approximation_label": "NEWS-CALENDAR BOUNDED",
     "expected_divergence": "The detection build now CARRIES production's own "
                            "blackout windows (fragment 08), generated through "
                            "production's own loader and deployed filter, and "
                            "applies production's inclusive per-minute match "
                            "over the fifteen minutes of each bar. Two edges "
                            "remain. BEYOND the last embedded window the chart "
                            "reports NEWS UNKNOWN, never ALLOWED — an exhausted "
                            "calendar is indistinguishable from a quiet one from "
                            "inside Pine. And the execution_1m build carries no "
                            "schedule at all, so every bar there is "
                            "INPUT_UNAVAILABLE for news",
     "measured_divergence": "360 windows embedded, covering 2025-06-02 to "
                            "2026-05-20; the Pine minute-grid decision agrees "
                            "with strategy_core.news._news_blackout_match on "
                            "1,824/1,824 sampled 15m bars — see backend/tests/"
                            "test_oracle_news_and_levels.py"},
    {"id": "L-20", "approximation_label": "MANUAL-COMPARISON",
     "expected_divergence": "TradingView cannot be driven headlessly from this VPS, "
                            "so S1 parity requires an operator to read Data Window "
                            "values; until then S1 stays UNVERIFIED",
     "measured_divergence": None},
]


class GenerateError(RuntimeError):
    pass


def _norm_bytes(text: str) -> bytes:
    return text.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(_norm_bytes(text)).hexdigest()


# `array.from(...)` — NOT `array.new()` followed by `array.push()`.
#
# A bare `array.push(X, v)` at global scope is a STATEMENT: it re-executes on
# every bar, so a `var` array declared once would grow without bound and every
# index lookup would drift. `array.from` builds the array inside the `var`
# initialiser, which Pine evaluates exactly once.
#: One colour per session, positional with the schedule. Chosen to stay
#: distinguishable at 90% transparency (the shading) AND as an opaque legend chip.
#: The fallback (Outside) is deliberately the neutral grey at the end.
_SESSION_PALETTE = ("#26547C", "#EF476F", "#FFD166", "#06D6A0", "#7B2CBF", "#8D99AE")


def _pine_color_array(name: str, hexes, transparency: int) -> str:
    items = ", ".join(f"color.new({h}, {transparency})" for h in hexes)
    return f"var array<color> {name} = array.from({items})"


def _pine_str_array(name: str, values) -> str:
    items = ", ".join(f'"{v}"' for v in values)
    return f"var array<string> {name} = array.from({items})"


def _pine_int_array(name: str, values) -> str:
    items = ", ".join(str(int(v)) for v in values)
    return f"var array<int> {name} = array.from({items})"


def _symbol_aliases(symbol: str) -> list[str]:
    """Normalised tickers that mean the production instrument.

    Feeds decorate the same instrument in predictable ways (broker suffixes,
    exchange prefixes, separators). The oracle normalises the chart ticker the
    same way (fragment 20) and compares against this GENERATED list, so the
    accepted set is documented and testable rather than a silent regex.
    """
    base = symbol.upper()
    return sorted({base, base + "OTC", base + "SPOT", base + "CASH"})


#: What each build does and does NOT contain, rendered into its header. Written
#: out per target rather than derived, because "this build contains no order
#: block logic" is a claim a reader will rely on and a generated sentence would
#: be true only by accident.
TARGET_SCOPE_NOTE = {
    "detection_15m": (
        "// DETECTION ORACLE — 15-MINUTE DOMAIN. Owns S1-S6: data context, time,\n"
        "// sessions, volatility and parsed prices, swings, BOS/CHoCH, order-block\n"
        "// creation and the daily market-state panel.\n"
        "//\n"
        "// It contains NO entry, pending-order, fill, stop, target or trade\n"
        "// lifecycle logic. Production runs those on ONE-MINUTE candles\n"
        "// (`simulate_trades` is handed the raw candle file, not this resample),\n"
        "// and a 15-minute bar cannot order events inside itself — so they live in\n"
        "// the execution oracle, not here. Global parity is PARTIAL by\n"
        "// construction."),
    "execution_1m": (
        "// EXECUTION ORACLE — 1-MINUTE DOMAIN. Owns S7 onward: the pending-order\n"
        "// walk that production runs bar-by-bar on the raw 1-minute candle file.\n"
        "//\n"
        "// It rebuilds the 15-minute detection frame FROM the 1-minute bars using\n"
        "// production's own resampling rule, rather than asking TradingView for\n"
        "// 15-minute bars — production derives its detection frame the same way,\n"
        "// and a second aggregation would be a second unproven authority.\n"
        "//\n"
        "// S7 is NOT YET IMPLEMENTED in this build. It claims no parity."),
    "strategy_companion": (
        "// WHAT THIS BUILD IS. A `strategy()` so TradingView's Strategy Tester\n"
        "// has something to run. The Visual Oracle stays an `indicator()` and is not\n"
        "// modified by this build's existence.\n"
        "//\n"
        "// It CLAIMS NO PARITY, and the claim is not merely unrecorded — it is\n"
        "// unavailable. Production detects order blocks on 15-minute bars and EXECUTES\n"
        "// on 1-minute ones: the arm, the 3-minute delay and the fill all happen at a\n"
        "// resolution this chart does not have. A 15-minute bar that both touches the\n"
        "// entry edge and breaches the far edge contains production's fill and\n"
        "// production's invalidation at once, and OHLC cannot order the minutes\n"
        "// between them.\n"
        "//\n"
        "// THE TWO MODES DIFFER IN EXACTLY ONE DECISION, and it is not the one a\n"
        "// reader would guess. Neither can decline the bar above: TradingView matches\n"
        "// a resting order against a bar BEFORE the script is evaluated at that bar's\n"
        "// close, so if such a bar delivers the entry the fill has already happened.\n"
        "// Both modes therefore only refuse to START, and both count the fills whose\n"
        "// own bar broke the block.\n"
        "//\n"
        "// What they do differ on is the ARM BAR. If the bar that armed a setup had\n"
        "// already reached the entry edge, production — on 1-minute candles — may well\n"
        "// have filled inside it; 28 of 46 recorded fills (61%) did. This build cannot\n"
        "// trade a bar that has closed, so its order waits for a later bar to revisit\n"
        "// the edge: same price, different hour, therefore a different session, market\n"
        "// state and possibly matrix cell. STRICT will not call that production's\n"
        "// trade and declines. PRACTICAL takes it, flags it emulator-dependent, and\n"
        "// labels every headline APPROXIMATE. Neither is called parity."),
}


#: The declaration every ORACLE build gets. A target may override it; exactly
#: one does, because exactly one of them is a `strategy()`.
#:
#: max_lines_count WAS OMITTED here once, and Pine's default is FIFTY. The
#: replay layer alone draws up to four lines per setup and the live layer four
#: per block, so entry / stop / target lines were being evicted by the engine
#: within a few setups — which is what "the risk-reward tools are not showing"
#: was. Boxes and labels were already raised; lines were not, and the omission
#: is invisible because Pine evicts silently rather than erroring.
DEFAULT_DECLARATION = (
    'indicator("{title}", "{short}",\n'
    "     overlay = true, max_labels_count = 500, max_boxes_count = 500,\n"
    "     max_lines_count = 500)")

#: The one-way synchronisation rule, stated on every generated file. Per target
#: because the noun differs and the sentence is quoted verbatim by
#: `test_generated_pine_carries_the_do_not_edit_banner` — it is a commitment,
#: not boilerplate, and rewording it for all builds to suit one of them is how a
#: commitment quietly becomes a slogan.
DEFAULT_AUTHORITY = (
    "// Python is the sole source of truth. Where this indicator and the "
    "production\n// engine disagree, THIS INDICATOR IS WRONG.")


def build_generated_header(contract: dict, source_hash_placeholder: str,
                           spec=None) -> str:
    spec = spec or resolve_target()
    f = contract["fingerprint"]
    cfgctx = contract["configuration"]["pine_relevant"]["data_context"]
    title = spec["indicator_title"].format(symbol=cfgctx["symbol"])
    # The declaration is per target because ONE of them is not an indicator.
    # Hardcoding `indicator(...)` here is what made the strategy companion
    # impossible to generate at all, and hand-editing the emitted file would
    # have been detected as tampering — correctly.
    declaration = (spec.get("declaration") or DEFAULT_DECLARATION).format(
        title=title, short=spec["indicator_short"])
    authority = spec.get("authority") or DEFAULT_AUTHORITY
    return f'''// =============================================================================
// TRADINGVIEW VISUAL ORACLE — GENERATED FILE. DO NOT EDIT.
// =============================================================================
//
// Generated by      : tools/oracle/generate_pine.py v{fp.GENERATOR_VERSION}
// Source contract   : contracts/tradingview_oracle_contract.json
// Contract schema   : {contract["contract_schema_version"]}
// Trace schema      : {contract["trace_schema_version"]}
// Engine fingerprint: {f["oracle_engine_id"]}
// Engine hash       : {f["oracle_engine_hash"]}
// Engine manifest   : {contract["engine"]["engine_manifest_id"]}
// Resolved config   : {f["inputs"]["resolved_config_hash"]}
// Policy version    : {f["inputs"]["policy_version"]}
// Source hash       : {source_hash_placeholder}
//
// Build target      : {spec["target_id"]}  ({spec["label"]})
// Chart timeframe   : {spec["timeframe"]}  — this build is REFUSED on any other
// Owned stages      : {", ".join(spec["stages"]) or "none — reuses the detection build's stages"}
//
// Any hand edit to this file is DETECTED by
//     python -m tools.oracle.check_freshness --target {spec["target_id"]}
// which compares the file's hash against this target's parity manifest and
// reports INCOMPATIBLE. Regenerate instead:
//     python -m tools.oracle.generate_pine --target {spec["target_id"]} --write
//
{TARGET_SCOPE_NOTE[spec["target_id"]]}
//
{authority}
// =============================================================================

//@version=6
{declaration}

// The assembled-source identifier, as a CONSTANT (the debug panel renders it).
// It lives in this fragment rather than 05 because it is the one value that
// depends on the assembled body, and the two-pass assembly resolves it here.
ORACLE_SOURCE_HASH_SHORT     = "{source_hash_placeholder}"
'''


def recorded_stages(spec=None) -> dict:
    """The stage records THIS generation will write, for THIS target.

    Implementation status comes from `STAGE_STATUS` (a property of the artefact
    being generated now); the parity dimensions are carried forward from the
    manifest on disk. Both halves are resolved here so the embedded Pine
    constants and the written manifest agree WITHIN ONE RUN — reading the prior
    manifest directly made generation converge only on a second invocation,
    which meant `--write` could leave the chart one step behind the evidence.

    Only the target's OWNED stages appear. A manifest that also carried the other
    build's stages would give one claim two homes, and the next question would be
    which copy is authoritative.
    """
    spec = spec or resolve_target()
    fresh = {st: ps.new_stage_record(STAGE_STATUS[st]) for st in spec["stages"]}
    return carry_forward_evidence({"stages": fresh}, spec)["stages"]


def _logic(stages, stage):
    rec = stages.get(stage)
    if not rec:
        return ps.SHORT_LOGIC[ps.LOGIC_UNIMPLEMENTED
                              if STAGE_STATUS.get(stage) == ps.IMPL_UNIMPLEMENTED
                              else ps.LOGIC_UNVERIFIED]
    return ps.SHORT_LOGIC[rec["logic_parity"]["status"]]


def _max_bootstrap(stages):
    """The largest declared bootstrap across stages — what the chart must warn
    about, since the reader cannot trust ANY stage before the longest one."""
    return max((r["logic_parity"].get("bootstrap_bars") or 0
                for r in stages.values()), default=0)


def _worst(stages, dim, order, default):
    """The least reassuring value across stages. A HUD that showed the best one
    would be a lie of omission."""
    seen = {r[dim]["status"] for r in stages.values()} or {default}
    for status in order:
        if status in seen:
            return status
    return default


def _feed(stages):
    return ps.SHORT_FEED[_worst(
        stages, "feed_parity",
        (ps.FEED_UNAVAILABLE, ps.FEED_DIFFERENT, ps.FEED_PARTIAL,
         ps.FEED_UNVERIFIED, ps.FEED_MATCHED), ps.FEED_UNVERIFIED)]


def _replay(stages):
    return ps.SHORT_REPLAY[_worst(
        stages, "production_replay_parity",
        (ps.REPLAY_DIVERGENT, ps.REPLAY_UNATTAINABLE, ps.REPLAY_UNVERIFIED,
         ps.REPLAY_MATCHED), ps.REPLAY_UNVERIFIED)]


def _quote_basis(stages, side):
    """bid / mid / ask, from the recorded feed measurement. The single most
    useful thing the HUD can say about why the numbers differ."""
    if not MANIFEST_PATH.is_file():
        return "UNKNOWN"
    try:
        m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    except ValueError:
        return "UNKNOWN"
    fm = m.get("feed_measurement") or {}
    return str(fm.get(f"{side}_quote_basis") or "UNKNOWN").upper()


def _global(stages):
    return ps.global_status(stages) if stages else "UNVERIFIED"


def build_generated_contract(contract: dict, spec=None) -> str:
    from tools.oracle import replay_structure as rs_mod
    spec = spec or resolve_target()
    rec = recorded_stages(spec)
    mstatec = codes_mod.market_state_codes(contract)
    trendc = codes_mod.trend_state_codes()
    volc = codes_mod.volatility_state_codes()
    chopc = codes_mod.chop_state_codes()
    rvalc = codes_mod.regime_validity_codes()
    # Mirrors `_portfolio_panel_index`'s cfg construction (run_backtest.py:2417),
    # NOT `build_regime_runtime_config` — the latter returns None here because
    # `regime_gate_enabled` is False, and mirroring it would emit nothing at all.
    _r = contract["configuration"]["pine_relevant"]["regime"]
    reg = {
        "emaLength": _r["regime_ema_length"],
        "bbwLength": _r["regime_bbw_length"],
        "bbwStdDev": _r["regime_bbw_std"],
        "adxLength": _r["regime_adx_length"],
        "adxChop": _r["regime_adx_chop"],
        "bbwThreshold": _r["regime_bbw_thr_value"],
        "gateEnabled": bool(_r["regime_gate_enabled"]),
    }
    if str(_r["regime_bbw_thr_mode"]) != "fixed":
        raise GenerateError(
            f"regime_bbw_thr_mode is {_r['regime_bbw_thr_mode']!r}; only 'fixed' "
            "is mirrored. A median/percentile threshold is computed over the "
            "whole run and is therefore in-sample — the oracle would have to "
            "know the future to reproduce it.")
    sess = contract["sessions"]
    codes = codes_mod.session_codes(contract)
    tcodes = codes_mod.transition_codes()
    dcodes = codes_mod.data_context_codes()
    scodes = codes_mod.structure_codes()
    struct = contract["configuration"]["pine_relevant"]["structure"]
    f = contract["fingerprint"]
    cfgctx = contract["configuration"]["pine_relevant"]["data_context"]

    windows = sess["windows"]
    fb = sess["fallback"]

    # THE TARGET'S timeframe, not both of production's. Accepting either would
    # let the detection oracle run on a 1-minute chart, where its swing window
    # and ATR period mean something entirely different, and report parity
    # statuses that were measured at 15 minutes.
    tf_secs = list(spec["chart_timeframe_seconds"])
    tf_label = spec["timeframe"]
    det_secs = TIMEFRAME_MINUTES[cfgctx["detection_timeframe"]] * 60
    exec_secs = TIMEFRAME_MINUTES[cfgctx["execution_timeframe"]] * 60

    lines = [
        "// ─────────────────────────────────────────────────────────────────────────────",
        "// 05 — GENERATED CONTRACT CONSTANTS. DO NOT EDIT.",
        "//",
        "// Every value below is extracted from the deployed production engine by",
        "// tools/oracle/extract_contract.py. Hand-editing them would make Pine a second",
        "// authority, which the one-way synchronisation model forbids.",
        "// ─────────────────────────────────────────────────────────────────────────────",
        "",
        "// ── build identity ──────────────────────────────────────────────────────────",
        f'ORACLE_BUILD_VERSION         = "v{ORACLE_VERSION}"',
        f'ORACLE_ENGINE_ID_SHORT       = "{f["oracle_engine_id"]}"',
        "// Display form. The full id is 56 chars and forced the HUD table to a",
        "// width that covered the price action; the full value stays available in",
        "// the debug panel and in ORACLE_ENGINE_ID_SHORT above.",
        f'ORACLE_ENGINE_ID_COMPACT     = "{"_".join(f["oracle_engine_id"].split("_")[:2])}"',
        f'ORACLE_ENGINE_HASH           = "{f["oracle_engine_hash"]}"',
        f'ORACLE_ENGINE_MANIFEST_SHORT = "{contract["engine"]["engine_manifest_id"][:16]}"',
        f'ORACLE_CONFIG_HASH_SHORT     = "{f["inputs"]["resolved_config_hash"][:16]}"',
        f'ORACLE_POLICY_VERSION        = "{f["inputs"]["policy_version"]}"',
        f'ORACLE_CONTRACT_SCHEMA       = "{contract["contract_schema_version"]}"',
        f'ORACLE_TRACE_SCHEMA          = "{contract["trace_schema_version"]}"',
        f'ORACLE_GENERATOR_VERSION     = "{fp.GENERATOR_VERSION}"',
        "// Derived from the contract, NOT the wall clock — a wall-clock stamp would",
        "// make every regeneration a different file and destroy the tamper check.",
        f'ORACLE_GENERATED_AT          = "contract {f["oracle_engine_hash"][:12]}"',
        f'ORACLE_BUILD_CODE            = {int(f["oracle_engine_hash"][:8], 16) % 1000000}',
        "",
        "// ── build target ───────────────────────────────────────────────────────────",
        "// Production has TWO time domains and this build owns one of them. The",
        "// data-context check refuses any other chart timeframe rather than",
        "// rendering values whose parameters were measured somewhere else.",
        f'ORACLE_BUILD_TARGET          = "{spec["target_id"]}"',
        f'ORACLE_BUILD_LABEL           = "{spec["label"]}"',
        f'ORACLE_OWNED_STAGES          = "{", ".join(spec["stages"])}"',
        f'ORACLE_DETECTION_TF_SECONDS  = {det_secs}',
        f'ORACLE_EXECUTION_TF_SECONDS  = {exec_secs}',
        "",
        "// ── parity status (generated; never hand-set) ───────────────────────────────",
        "// IMPLEMENTATION — does this build contain the stage. Says nothing about",
        "// correctness; the three parity dimensions below answer that separately.",
    ] + [
        f'ORACLE_{st}_STATUS             = "{STAGE_STATUS[st]}"'
        for st in spec["stages"]
    ] + [
        "// LOGIC PARITY — does Pine reproduce the Python algorithm on IDENTICAL",
        "// bars. The only dimension a Pine defect can move. One line per OWNED",
        "// stage: a build cannot report a status for a stage it does not contain.",
    ] + [
        f'ORACLE_{st}_LOGIC              = "{_logic(rec, st)}"'
        for st in spec["stages"]
    ] + [
        f'ORACLE_LOGIC_BOOTSTRAP       = {_max_bootstrap(rec)}',
        "// FEED PARITY — does the CHART's data equal production's. A property of",
        "// the broker feed; no Pine change can move it.",
        f'ORACLE_FEED_STATUS           = "{_feed(rec)}"',
        f'ORACLE_FEED_PROD_BASIS       = "{_quote_basis(rec, "production")}"',
        f'ORACLE_FEED_CHART_BASIS      = "{_quote_basis(rec, "tradingview")}"',
        "// PRODUCTION REPLAY PARITY — does the chart reproduce the exact historical",
        "// production trace. Presupposes BOTH of the above.",
        f'ORACLE_REPLAY_STATUS         = "{_replay(rec)}"',
        "",
        "// ── packed-code transport ──────────────────────────────────────────────────",
        "// TradingView caps a script at 64 plots (RE10140, raised at RUNTIME on a",
        "// live chart). S1-S6 needed 83, so each stage's SMALL-INTEGER fields travel",
        "// in ONE plot (S5 needs two — see PACKED_GROUPS). Every field keeps its",
        "// exact value; only the transport changes.",
        "//",
        "// Three constants per field, ALL generated from",
        "// tools/oracle/session_codes.py::PACKED_SPEC — the same spec the comparator",
        "// decodes with. Pine therefore contains no hand-written radix arithmetic at",
        "// all, which is the only way the encode and the decode cannot drift:",
        "//   PACK_<G>_<i>   positional multiplier, LSB-first",
        "//   PACK_<G>_W<i>  field width (a value COUNT: width 8 accepts 0..7)",
        "//   PACK_<G>_O<i>  offset added before packing, to lift a signed field",
        "//                  (bias -1..+1, or -1 meaning 'absent') into range",
        "//",
        "// Scoped to THIS TARGET's exported surfaces. The execution build has no",
        "// swings and no order blocks, so shipping their multipliers would imply",
        "// it exports them.",
    ] + [
        line
        for g in _target_packed_groups(spec)
        for i, ((field, width, offset), mult) in enumerate(
            zip(codes_mod.PACKED_SPEC[g], codes_mod.packed_multipliers(g)))
        for line in (f'PACK_{g}_{i:<21} = {mult}',
                     f'PACK_{g}_W{i:<20} = {width}',
                     f'PACK_{g}_O{i:<20} = {offset}    // {field}')
    ] + [
        "",
        "// ── S6 daily regime panel (from the resolved production config) ────────────",
        "// THE LIVE PATH IS NOT THE GLOBAL REGIME GATE. `regime_gate_enabled` is",
        "// FALSE in production, so `regime_emit_for_run` returns None and the in-loop",
        "// regime gate never blocks. The panel is nevertheless computed, by",
        "// `_portfolio_panel_index`, which is deliberately independent of that gate —",
        "// its consumers are the portfolio policy (mode `enforce`) and the",
        "// state-target policy. Mirroring the disabled gate instead would reproduce",
        "// a code path production never executes.",
        f'REGIME_EMA_LENGTH            = {int(reg["emaLength"])}',
        f'REGIME_BBW_LENGTH            = {int(reg["bbwLength"])}',
        f'REGIME_BBW_STD               = {float(reg["bbwStdDev"])}',
        f'REGIME_ADX_LENGTH            = {int(reg["adxLength"])}',
        f'REGIME_ADX_CHOP              = {float(reg["adxChop"])}',
        f'REGIME_BBW_THRESHOLD         = {float(reg["bbwThreshold"])}',
        f'REGIME_EMA_ALPHA             = {2.0 / (int(reg["emaLength"]) + 1)!r}',
        f'REGIME_ADX_ALPHA             = {1.0 / int(reg["adxLength"])!r}',
        "// Six states, positional from strategy_core.regime.MARKET_STATES.",
        _pine_str_array("REGIME_STATE_NAME", list(mstatec)),
        _pine_int_array("REGIME_STATE_CODE", [mstatec[k] for k in mstatec]),
        f'CODE_TREND_BULL              = {trendc["Bull"]}',
        f'CODE_TREND_BEAR              = {trendc["Bear"]}',
        f'CODE_VOL_EXPAND              = {volc["Expand"]}',
        f'CODE_VOL_COMPRESS            = {volc["Compress"]}',
        f'CODE_CHOP_TREND              = {chopc["Trend"]}',
        f'CODE_CHOP_CHOP               = {chopc["Chop"]}',
        f'CODE_REGIME_INVALID          = {rvalc["invalid"]}',
        f'CODE_REGIME_WARMUP           = {rvalc["warmup"]}',
        f'CODE_REGIME_VALID            = {rvalc["valid"]}',
        "",
        "// ── S5 order-block gate (from the resolved production config) ──────────────",
        "// BOTH comparisons are STRICT in production, so the gate is INCLUSIVE at",
        "// both ends: a width of exactly OB_MAX_PIPS PASSES. min is 0.0 and can",
        "// therefore never reject — mirrored anyway, because it is config, not a law.",
        f'OB_PIP_SIZE                  = {float(cfgctx["pip_size"])}',
        f'OB_MIN_PIPS                  = {float(struct["min_ob_size_pips"])}',
        f'OB_MAX_PIPS                  = {float(struct["max_ob_size_pips"])}',
        f'ORACLE_GLOBAL_STATUS         = "{_global(rec)}"',
        "",
        "// ── supported data context ──────────────────────────────────────────────────",
        f'ORACLE_SYMBOL                = "{cfgctx["symbol"]}"',
        f'ORACLE_TF_LABEL              = "{tf_label}"',
        _pine_str_array("ORACLE_SYMBOL_ALIASES", _symbol_aliases(cfgctx["symbol"])),
        _pine_int_array("ORACLE_TF_SECONDS", tf_secs),
        "",
        "// ── session schedule (strategy_core/sessions.py::_SESSION_SCHEDULE) ─────────",
        "// Windows are [start_hour, end_hour) on the UTC hour. First match wins.",
        _pine_int_array("SESSION_START_HOUR", [w["start_hour"] for w in windows]),
        _pine_int_array("SESSION_END_HOUR", [w["end_hour"] for w in windows]),
        _pine_str_array("SESSION_KEY", [w["key"] for w in windows]),
        _pine_str_array("SESSION_LABEL", [w["label"] for w in windows]),
        _pine_int_array("SESSION_CODE", [codes[w["key"]] for w in windows]),
        "// Legend geometry: header + one row per scheduled session + fallback,",
        "// plus one row of HEADROOM. Generated so the table cannot be short by",
        "// one the day a session is added — a truncated legend renders, which is",
        "// worse than failing. The headroom matches the HUD's convention: a table",
        "// overflow is RE10040 at RUNTIME and blanks the whole indicator.",
        f'LEGEND_ROWS                  = {len(windows) + 3}',
        f'SESSION_FALLBACK_KEY         = "{fb["key"]}"',
        f'SESSION_FALLBACK_LABEL       = "{fb["label"]}"',
        f'SESSION_FALLBACK_CODE        = {codes[fb["key"]]}',
        "",
        "// ── session palette (generated, so shading and legend cannot diverge) ───────",
        "// One entry per schedule row, fallback last — the SAME positional order as",
        "// SESSION_KEY/CODE above. Two variants of each colour: a transparent SHADE for",
        "// the background band and an opaque SWATCH for the legend chip. Deriving both",
        "// from one generated list is what stops the legend from claiming a colour the",
        "// chart does not actually paint.",
        _pine_color_array("SESSION_SHADE",
                          [_SESSION_PALETTE[i % len(_SESSION_PALETTE)]
                           for i in range(len(windows) + 1)], 90),
        _pine_color_array("SESSION_SWATCH",
                          [_SESSION_PALETTE[i % len(_SESSION_PALETTE)]
                           for i in range(len(windows) + 1)], 0),
        "",
        "// ── transition reason codes ─────────────────────────────────────────────────",
        f'CODE_NONE                    = {codes_mod.CODE_NONE}',
        f'CODE_UNKNOWN                 = {codes_mod.CODE_UNKNOWN}',
        f'CODE_TR_SESSION_OPEN         = {tcodes["session_open"]}',
        f'CODE_TR_CLOSE_TO_FALLBACK    = {tcodes["session_close_to_fallback"]}',
        f'CODE_TR_FALLBACK_TO_SESSION  = {tcodes["fallback_to_session"]}',
        f'CODE_TR_FIRST_BAR            = {tcodes["first_bar"]}',
        f'CODE_TR_GAP_RESYNC           = {tcodes["gap_resync"]}',
        "",
        "// ── data-context codes ──────────────────────────────────────────────────────",
        f'CODE_CTX_SUPPORTED           = {dcodes["SUPPORTED"]}',
        f'CODE_CTX_UNSUPPORTED         = {dcodes["UNSUPPORTED_DATA_CONTEXT"]}',
        "",
        "// ── S2/S3/S4 structure constants (strategy_core/order_blocks.py) ───────────",
        "// The swing loop and the break test read RAW high/low/close. parsed_high /",
        "// parsed_low are consumed ONLY by _make_order_block, i.e. they set the",
        "// order-block BOX and belong to Stage S5 — they do NOT feed swings or breaks.",
        f'SWING_LENGTH                 = {int(struct["swing_length"])}',
        f'OB_FILTER                    = "{struct["ob_filter"]}"',
        f'ATR_PERIOD                   = {rs_mod.ATR_PERIOD}',
        f'VOL_MULTIPLE                 = {rs_mod.VOLATILITY_MULTIPLE}',
        f'BIAS_BULLISH                 = {contract["_engine_constants"]["BULLISH"]}',
        f'BIAS_BEARISH                 = {contract["_engine_constants"]["BEARISH"]}',
        f'BIAS_NEUTRAL                 = 0',
        f'LEG_BULLISH                  = {contract["_engine_constants"]["BULLISH_LEG"]}',
        f'LEG_BEARISH                  = {contract["_engine_constants"]["BEARISH_LEG"]}',
        f'TAG_BOS                      = "{contract["_engine_constants"]["BOS"]}"',
        f'TAG_CHOCH                    = "{contract["_engine_constants"]["CHOCH"]}"',
        "",
        "// ── structure event codes ───────────────────────────────────────────────────",
        f'CODE_EV_NONE                 = 0',
        f'CODE_EV_BOS_BULL             = {scodes["BOS_BULL"]}',
        f'CODE_EV_CHOCH_BULL           = {scodes["CHOCH_BULL"]}',
        f'CODE_EV_BOS_BEAR             = {scodes["BOS_BEAR"]}',
        f'CODE_EV_CHOCH_BEAR           = {scodes["CHOCH_BEAR"]}',
        "",
    ]
    return "\n".join(lines)


def _pine_float_array(name: str, values) -> str:
    items = ", ".join(repr(float(v)) for v in values) or ""
    return (f"var array<float> {name} = array.from({items})" if items
            else f"var array<float> {name} = array.new<float>()")


def _pine_int_array_safe(name: str, values) -> str:
    items = ", ".join(str(int(v)) for v in values) or ""
    return (f"var array<int> {name} = array.from({items})" if items
            else f"var array<int> {name} = array.new<int>()")


def build_generated_replay(spec=None) -> str:
    """Production's own setup decisions, as Pine arrays.

    The chart draws entry, stop, target, risk-reward, status, blocking reason and
    outcome from THIS — not from a Pine reimplementation of the fill gate, the
    policy chain and the exit precedence. It is exact because it is production's
    output.

    Produced by `python -m tools.oracle.export_replay --write`, which is slow
    (it walks the 1-minute series), so it is a separate step and generation just
    embeds the result. A missing file yields an EMPTY set and a build that draws
    no setups — never invented ones.
    """
    from tools.oracle.export_replay import DEFAULT_OUT as REPLAY_PATH
    from tools.oracle.export_replay import OUTCOME, REASON, STATUS

    data = {"setups": [], "engine_hash": None, "dropped_oldest": 0}
    if REPLAY_PATH.is_file():
        try:
            data = json.loads(REPLAY_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = {"setups": [], "engine_hash": None, "dropped_oldest": 0}
    setups = data.get("setups") or []

    # Evidence hygiene: a replay recorded against a DIFFERENT engine describes
    # decisions this build's production no longer makes. Drop it rather than draw
    # it — a stale trade on the chart is worse than no trade.
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    live_hash = contract["fingerprint"]["oracle_engine_hash"]
    stale = bool(setups) and data.get("engine_hash") not in (None, live_hash)
    if stale:
        setups = []

    idx = {name: i for i, name in enumerate(STATUS)}
    oidx = {name: i for i, name in enumerate(OUTCOME)}
    ridx = {name: i for i, name in enumerate(REASON)}

    lines = [
        "// ─────────────────────────────────────────────────────────────────────────────",
        "// 06 — GENERATED REPLAY DATA. DO NOT EDIT.",
        "//",
        "// Production's OWN decisions for each setup, recorded by",
        "// tools/oracle/export_replay.py and drawn by fragment 67. Entry, stop,",
        "// target, status, blocking reason and outcome are not recomputed here —",
        "// they are what `simulate_trades` decided.",
        "//",
        f"// setups embedded : {len(setups)}",
        f"// older dropped   : {data.get('dropped_oldest', 0)}",
        f"// recorded window : {(data.get('window') or {}).get('first_bar')}"
        f" .. {(data.get('window') or {}).get('last_bar')}",
        f"// engine          : {str(data.get('engine_hash'))[:16]}",
        ("// STALE: recorded against a different engine — DROPPED, so this build "
         "draws no setups." if stale else
         "// Keyed by UTC epoch ms, never by bar index (L-09)."),
        "// ─────────────────────────────────────────────────────────────────────────────",
        "",
        f"RP_COUNT           = {len(setups)}",
        f"RP_DROPPED         = {data.get('dropped_oldest', 0)}",
        f'RP_STALE           = {"true" if stale else "false"}',
        "// A setup that never resolved still needs a right-hand edge; four hours",
        "// is long enough to read and short enough not to imply a duration.",
        "RP_IDLE_MS         = 14400000",
        "// One detection bar, in ms — the unit the lifecycle tail is measured in.",
        "RP_BAR_MS          = 900000",
        "// Pine hard-limits boxes, lines and labels to 500 EACH and evicts beyond",
        "// that unpredictably. Staying under it keeps eviction deterministic.",
        "// RETENTION, SIZED AGAINST THE WHOLE SCRIPT'S BUDGET.",
        "// Pine's ceiling is 500 objects PER TYPE for the ENTIRE indicator, not",
        "// per layer. These were 480, which alone consumed the budget and left",
        "// the live layer's entry/stop/target lines to be evicted by the engine.",
        "// Worst case now: replay 150 + live 4*60 + structure 300 + swings 2",
        "// = 692 lines... see OBJ_* below, which the generator computes so the",
        "// arithmetic is checked rather than asserted in a comment.",
        # THE FEED SEAM, declared. Recorded setups after this instant were
        # simulated on the LIVE MT5 1-minute segment rather than the frozen
        # historical file. The chart says so rather than implying one feed.
        f'RP_SEAM_MS         = {int((data.get("seam") or {}).get("frozen_end_ms", 0))}',
        f'RP_SEAM_TXT        = "{_ms_date((data.get("seam") or {}).get("frozen_end_ms", 0))}"',
        "RP_MAX_LINES       = 150",
        "RP_MAX_BOXES       = 150",
        "RP_MAX_LABELS      = 150",
        "// The newest setup in the recording. The HUD compares the visible bar",
        "// against it, so a chart scrolled PAST the recorded data says so instead",
        "// of just looking empty.",
        f"RP_LAST_MS         = "
        f"{max((s['detected_ms'] for s in setups), default=0)}",
        "",
    ] + [
        f"RP_ST_{name:<12} = {i}" for i, name in enumerate(STATUS)
    ] + [""] + [
        f"RP_OUT_{name:<11} = {i}" for i, name in enumerate(OUTCOME)
    ] + [
        "",
        _pine_int_array_safe("RP_DETECTED", [s["detected_ms"] for s in setups]),
        _pine_int_array_safe("RP_OBID", [s["ob_id"] for s in setups]),
        _pine_int_array_safe("RP_DIRECTION", [s["direction"] for s in setups]),
        _pine_int_array_safe("RP_STATUS",
                             [idx.get(s["status"], 0) for s in setups]),
        _pine_int_array_safe("RP_OUTCOME",
                             [oidx.get(s["outcome"], 0) for s in setups]),
        _pine_int_array_safe("RP_FILL", [s["fill_ms"] for s in setups]),
        _pine_int_array_safe("RP_EXIT", [s["exit_ms"] for s in setups]),
        _pine_int_array_safe("RP_ARMED", [s.get("armed_ms", 0) for s in setups]),
        _pine_float_array("RP_ENTRY", [s["entry"] for s in setups]),
        _pine_float_array("RP_STOP", [s["stop"] for s in setups]),
        _pine_float_array("RP_TARGET", [s["target"] for s in setups]),
        _pine_float_array("RP_NETR", [s["net_r"] for s in setups]),
        _pine_float_array("RP_GROSSR", [s.get("gross_r", 0.0) for s in setups]),
        _pine_float_array("RP_RR", [s.get("rr", 0.0) for s in setups]),
        # Production's OWN order-block bounds. The chart's own boxes cannot be
        # joined to these by id — Pine counts from the first loaded bar and
        # production counts from 2015 (L-05) — so the lifecycle box is drawn
        # from production's bounds directly rather than inferred.
        _pine_float_array("RP_OBTOP", [s.get("ob_top", 0.0) for s in setups]),
        _pine_float_array("RP_OBBOT", [s.get("ob_bottom", 0.0) for s in setups]),
        _pine_str_array("RP_REASON", [s["reason"] for s in setups]),
        _pine_str_array("RP_STRUCT",
                        ["CHoCH" if s["structure"] == 2 else "BOS"
                         for s in setups]),
        # The market state that applied WHEN THE SETUP HAPPENED. The S6 band
        # shows the state NOW; a historical setup must carry the state it was
        # actually judged under.
        _pine_str_array("RP_STATE",
                        [s.get("market_state") or "—" for s in setups]),
        _pine_str_array("RP_SESSION",
                        [s.get("session") or "—" for s in setups]),
        _pine_str_array("RP_COHORT",
                        [s.get("cohort") or "—" for s in setups]),
        "",
    ]
    unmapped = sorted({s["reason"] for s in setups
                       if s["reason"] and s["reason"] not in ridx})
    if unmapped:
        lines.insert(6, f"// UNMAPPED reasons rendered as raw text: {unmapped}")
    return "\n".join(lines)


def _contract() -> dict:
    """The committed contract, for generated blocks that need the regime
    vocabulary. `build_generated_targets` is called without one because the
    target table is loaded from the engine, but the state-column map has to see
    BOTH orderings to relate them."""
    return json.loads(
        (CT_ROOT / "contracts" / "tradingview_oracle_contract.json")
        .read_text(encoding="utf-8"))


def state_column_map(regime_states, target_states) -> list:
    """REGIME state index -> TGT state column, by EXACT NAME.

    `s6_stateCode - 1` indexes `regime_states`; the target arrays are ordered by
    `target_states`. Those two lists come from different modules — the contract's
    `market_state` enum and `target_table.py` — and nothing has ever required
    them to agree. They do not: they are exact reversals, which silently sent
    every confirmed-state lookup to the mirrored column.

    Raising here rather than returning a best effort is deliberate. A generation
    that cannot establish the correspondence has no correct column to emit, and
    a build that falls back to the base target renders a plausible wrong RR with
    no indication that anything went wrong.
    """
    if len(regime_states) != len(set(regime_states)):
        raise GenerateError(
            f"duplicate market-state names in the regime vocabulary: "
            f"{regime_states}")
    if len(target_states) != len(set(target_states)):
        raise GenerateError(
            f"duplicate market-state names in the target vocabulary: "
            f"{target_states}")
    if set(regime_states) != set(target_states):
        missing = sorted(set(regime_states) - set(target_states))
        extra = sorted(set(target_states) - set(regime_states))
        raise GenerateError(
            f"market-state vocabularies do not correspond — regime states with "
            f"no target column: {missing}; target columns with no regime state: "
            f"{extra}. The state override cannot be resolved by name.")
    mapping = [target_states.index(name) for name in regime_states]
    if sorted(mapping) != list(range(len(target_states))):
        raise GenerateError(
            f"state column mapping is not bijective: {mapping}")
    return mapping


def build_generated_targets(spec=None) -> str:
    """Production's trade-target table, as Pine arrays.

    The chart resolves the SAME cell production would — session x structure x
    direction, then a sparse market-state override — and draws the target from
    it. Hand-transcribing 144 cells was never an option; this is generated from
    `tools/oracle/target_table.py`, which reads the deployed scenario.

    A MISSING table yields a build that refuses to draw a live target rather than
    one that falls back to a generic RR: a plausible-looking wrong target is the
    failure this whole apparatus exists to prevent.
    """
    from tools.oracle.target_table import DEFAULT_OUT as TABLE_PATH

    table = None
    if TABLE_PATH.is_file():
        try:
            table = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            table = None
    if not table:
        return "\n".join([
            "// ─────────────────────────────────────────────────────────────",
            "// 07 — GENERATED TARGET TABLE. DO NOT EDIT.",
            "// ABSENT. Run `python -m tools.oracle.update_detection_visual",
            "// --write`. Live setups will report NO TABLE rather than guess a",
            "// target — a plausible wrong RR is worse than a visible refusal.",
            "// ─────────────────────────────────────────────────────────────",
            'TGT_CONFIG_HASH    = "(absent)"',
            "TGT_PRESENT        = false",
            "TGT_SESSION_N      = 0",
            "TGT_STATE_N        = 0",
            "TGT_GLOBAL_RR      = 0.0",
            "TGT_STOP_BUFFER    = 0.0",
            "TGT_ENTRY_PCT      = 0.0",
            "TGT_DELAY_CANDLES  = 0",
            _pine_str_array("TGT_SESSION_NAME", []),
            _pine_str_array("TGT_STATE_NAME", []),
            # Declared even when the table is absent: Pine resolves identifiers
            # at COMPILE time, so a fragment referencing it must find it whether
            # or not there is a table to index.
            _pine_int_array_safe("REGIME_TO_TGT_STATE_IDX", []),
            _pine_float_array("TGT_BASE_RR", []),
            _pine_int_array_safe("TGT_BASE_ON", []),
            _pine_float_array("TGT_STATE_RR", []),
            _pine_int_array_safe("TGT_STATE_ELIG", []),
            "",
        ])

    states = table["market_states"]
    base = table["base"]
    # Flattened EXACTLY as `target_table.cell_index` computes it — one ordering,
    # so the Pine decode cannot disagree with the Python one.
    flat_state_rr = [(-1.0 if v is None else float(v))
                     for row in table["state_rr"] for v in row]
    elig_code = {"none": 0, "allow": 1, "block": 2}
    flat_elig = [elig_code[v] for row in table["state_elig"] for v in row]
    c = table["counts"]

    return "\n".join([
        "// ─────────────────────────────────────────────────────────────────────────────",
        "// 07 — GENERATED TARGET TABLE. DO NOT EDIT.",
        "//",
        "// Production's own trade-target configuration, extracted from the",
        "// deployed `session_strategy_scenario` by tools/oracle/target_table.py.",
        "//",
        "// THE LOOKUP IS session x structure x direction -> a BASE cell, then a",
        "// SPARSE market-state override on top of it. The state is not a fourth",
        "// axis of the key: an UNKNOWN or UNCONFIRMED state keeps the base",
        "// target and never blocks, which is what production does.",
        "//",
        f"// base cells        : {c['base_cells']} "
        f"({c['base_cells_present']} from the scenario, "
        f"{c['base_cells_enabled']} enabled)",
        f"// addressable cells : {c['addressable_cells']}",
        f"// state target rules: {c['state_target_overrides']}",
        f"// state elig rules  : {c['state_eligibility_rules']}",
        "// ─────────────────────────────────────────────────────────────────────────────",
        "",
        f'TGT_CONFIG_HASH    = "{table["config_hash"][:16]}"',
        "TGT_PRESENT        = true",
        f'TGT_SESSION_N      = {len(table["sessions"])}',
        f'TGT_STATE_N        = {len(states)}',
        f'TGT_GLOBAL_RR      = {float(table["global_rr"])!r}',
        "// stop = far edge -/+ buffer, in PRICE. entry% is the depth into the",
        "// block production enters at; both come from the resolved config.",
        f'TGT_STOP_BUFFER    = '
        f'{float(table["stop_buffer_pips"]) * float(table["pip_size"])!r}',
        f'TGT_ENTRY_PCT      = {float(table["entry_level_pct"])!r}',
        "// The ARM threshold. Penetration this far into the block ARMS the",
        "// order; the FILL is still at the edge. Two different parameters.",
        f'TGT_TRIGGER_PCT    = {float(table.get("trigger_pct", 0.0))!r}',
        "// Candles (MINUTES — production executes on 1m) between the ARM and",
        "// the earliest legal fill. It is why a 15m bar that both arms and",
        "// straddles the entry cannot prove whether the fill was legal in it.",
        f'TGT_DELAY_CANDLES  = {int(table.get("delay_candles", 0))}',
        f'TGT_CELLS          = {c["addressable_cells"]}',
        f'TGT_BASE_N         = {c["base_cells"]}',
        "",
        "// Cohort session vocabulary — `_cohort_session_key`, NOT the S1 keys.",
        _pine_str_array("TGT_SESSION_NAME", table["sessions"]),
        _pine_str_array("TGT_STATE_NAME", states),
        "",
        "// ── THE STATE COLUMN MAP ───────────────────────────────────────────",
        "// `s6_stateCode - 1` indexes REGIME_STATE_NAME; TGT_STATE_RR and",
        "// TGT_STATE_ELIG are ordered by TGT_STATE_NAME. The two vocabularies",
        "// come from different modules and are NOT in the same order, so using",
        "// one index against the other's arrays reads the wrong column — which",
        "// it did, for every confirmed state, until this array existed.",
        "//",
        "// Generated by exact name match. Read it as: a setup whose S6 state is",
        "// REGIME_STATE_NAME[i] must use target column",
        "// REGIME_TO_TGT_STATE_IDX[i].",
        _pine_int_array_safe(
            "REGIME_TO_TGT_STATE_IDX",
            state_column_map(list(codes_mod.market_state_codes(_contract())),
                             list(states))),
        "",
        "// index = ((session * 2 + structure) * 2 + direction)",
        _pine_float_array("TGT_BASE_RR", [c2["target_rr"] for c2 in base]),
        _pine_int_array_safe("TGT_BASE_ON",
                             [1 if c2["enabled"] else 0 for c2 in base]),
        _pine_int_array_safe("TGT_BASE_PRESENT",
                             [1 if c2["present"] else 0 for c2 in base]),
        "// index = baseIndex * TGT_STATE_N + stateIndex.  -1 = no override.",
        _pine_float_array("TGT_STATE_RR", flat_state_rr),
        "// 0 none · 1 allow · 2 block",
        _pine_int_array_safe("TGT_STATE_ELIG", flat_elig),
        "",
    ])


def _ms_date(ms) -> str:
    """The coverage edge as a plain date, for the HUD warning."""
    import datetime as dt
    if not ms:
        return "-"
    return dt.datetime.fromtimestamp(
        int(ms) / 1000, dt.timezone.utc).strftime("%Y-%m-%d")


def build_generated_news(spec=None) -> str:
    """Production's blackout windows, so the chart applies the SAME news state.

    Two failure modes this must not have. It must not infer news from price —
    nothing here looks at a candle. And it must not treat the end of the
    schedule as "no news": beyond `NEWS_LAST_MS` the chart reports NEWS UNKNOWN,
    because an exhausted calendar and a quiet calendar are indistinguishable
    from inside Pine and only one of them is permission to trade.
    """
    from tools.oracle.export_news import DEFAULT_OUT as NEWS_PATH

    data = None
    if NEWS_PATH.is_file():
        try:
            data = json.loads(NEWS_PATH.read_text(encoding="utf-8"))
        except ValueError:
            data = None
    if not data:
        return "\n".join([
            "// 08 — GENERATED NEWS WINDOWS. DO NOT EDIT.",
            "// ABSENT. Run `python -m tools.oracle.export_news --write`.",
            "// Every live setup will report NEWS UNKNOWN — production blocks",
            "// fills inside a blackout and the chart cannot tell without this.",
            'NEWS_PRESENT   = false',
            "NEWS_ENABLED   = false",
            "NEWS_COUNT     = 0",
            "NEWS_FIRST_MS  = 0",
            "NEWS_LAST_MS   = 0",
            'NEWS_LAST_TXT  = "no schedule embedded"',
            _pine_int_array_safe("NEWS_START", []),
            _pine_int_array_safe("NEWS_END", []),
            "",
        ])

    windows = data.get("windows") or []
    return "\n".join([
        "// ─────────────────────────────────────────────────────────────────────────────",
        "// 08 — GENERATED NEWS WINDOWS. DO NOT EDIT.",
        "//",
        "// Production's OWN blackout windows, via its own loader and its own",
        "// deployed filter. Nothing here re-implements the filter and nothing",
        "// infers news from price.",
        "//",
        f"// source   : {data.get('source_file')}",
        f"// filter   : impact={data.get('impacts')} "
        f"currencies={data.get('currencies') or 'ALL'}",
        f"// window   : -{data.get('minutes_before')}m / "
        f"+{data.get('minutes_after')}m",
        f"// windows  : {len(windows)}",
        "//",
        "// COVERAGE IS A HARD EDGE. Past NEWS_LAST_MS the chart says NEWS",
        "// UNKNOWN — an exhausted calendar and a quiet one look identical from",
        "// inside Pine, and only one of them is permission.",
        "// ─────────────────────────────────────────────────────────────────────────────",
        "",
        "NEWS_PRESENT   = true",
        f'NEWS_ENABLED   = {"true" if data.get("enabled") else "false"}',
        f"NEWS_COUNT     = {len(windows)}",
        f'NEWS_FIRST_MS  = {int(data.get("first_ms") or 0)}',
        f'NEWS_LAST_MS   = {int(data.get("last_ms") or 0)}',
        # The coverage edge in words, so the HUD can say WHEN the chart stops
        # knowing rather than only that it does.
        f'NEWS_LAST_TXT  = "{_ms_date(data.get("last_ms"))}"',
        _pine_int_array_safe("NEWS_START", [w["start_ms"] for w in windows]),
        _pine_int_array_safe("NEWS_END", [w["end_ms"] for w in windows]),
        "",
    ])


def _read_fragment(name: str) -> str:
    p = SRC_DIR / f"{name}.pinefrag"
    if not p.is_file():
        raise GenerateError(f"missing hand-written fragment: pine/src/{name}.pinefrag")
    return p.read_text(encoding="utf-8")


def assemble(contract: dict, spec=None) -> tuple[str, list[dict]]:
    """Return (pine_source, fragment_records). Two-pass so the header can carry
    the hash of the body it heads."""
    spec = spec or resolve_target()
    gen_contract = build_generated_contract(contract, spec)
    # WHAT EACH BUILD CARRIES, declared per target rather than inferred from
    # "is it the default". The strategy companion needs the 144-cell target
    # table and the news schedule — both are fill-time authority it must read —
    # and has no use for the replay recording. Keying this off DEFAULT_TARGET
    # gave it neither, and a build that silently loses the target table does not
    # fail: `TGT_PRESENT = false` makes every cell fall through to the global
    # RR, which is a plausible number and a wrong one.
    embed = spec.get("embed") or ()
    gen_replay = (build_generated_replay(spec) if "replay" in embed
                  else "// 06 - no replay data for this target.\nRP_COUNT = 0")
    gen_news = (build_generated_news(spec) if "news" in embed
                else "// 08 - no news schedule for this target.\n"
                     "NEWS_PRESENT   = false\n"
                     'NEWS_LAST_TXT  = "no schedule embedded"')
    gen_targets = (build_generated_targets(spec) if "targets" in embed
                   else "// 07 - no target table for this target.\n"
                        "TGT_PRESENT = false")

    def compose(source_hash: str) -> tuple[str, list[dict]]:
        parts, records = [], []
        header = build_generated_header(contract, source_hash, spec)
        for name, text in [(GENERATED_FRAGMENTS[0], header),
                           (GENERATED_FRAGMENTS[1], gen_contract),
                           (GENERATED_FRAGMENTS[2], gen_replay),
                           (GENERATED_FRAGMENTS[3], gen_targets),
                           (GENERATED_FRAGMENTS[4], gen_news)]:
            parts.append(text)
            records.append({"path": f"<generated>/{name}.pinefrag",
                            "sha256": _sha256_text(text), "generated": True})
        for name in spec["fragments"]:
            text = _read_fragment(name)
            parts.append(text)
            records.append({"path": f"pine/src/{name}.pinefrag",
                            "sha256": _sha256_text(text), "generated": False})
        return "\n".join(parts), records

    # Pass 1 with a placeholder to learn the body hash, pass 2 to embed it.
    provisional, _ = compose("<pending>")
    body_hash = _sha256_text(provisional.split("\n", 1)[1])
    final, records = compose(body_hash[:16])
    return final, records


def _target_packed_groups(spec) -> list:
    """The packed groups THIS target exports, in a stable order."""
    from tools.oracle.compare_stages import TARGET_SURFACES
    return sorted({g for s in TARGET_SURFACES[spec["target_id"]]
                   for g in codes_mod.groups_for(s)})


def _export_schema(spec=None) -> str:
    """Lazy: `compare_stages` imports this module, so the import must not be at
    module level. Scoped to the TARGET, so an execution-only change cannot stale
    the detection build's evidence."""
    from tools.oracle.compare_stages import export_schema_hash
    return export_schema_hash((spec or resolve_target())["target_id"])


def _git(repo: Path, *args) -> str:
    import subprocess
    r = subprocess.run(["git", "-C", str(repo), *args],
                       capture_output=True, text=True, check=False)
    return r.stdout.strip() if r.returncode == 0 else ""


#: Dimension values that mean "nothing is known" — the only ones a regeneration
#: is allowed to overwrite.
_IGNORANT = {
    "logic_parity": {ps.LOGIC_UNVERIFIED, ps.LOGIC_UNIMPLEMENTED},
    "feed_parity": {ps.FEED_UNVERIFIED},
    "production_replay_parity": {ps.REPLAY_UNVERIFIED},
    # INPUT_AVAILABLE is the default AND a true claim for every price-and-time
    # stage, so it is the value a regeneration may overwrite; a measured gap is
    # evidence and survives.
    "input_availability": {ps.INPUT_AVAILABLE},
}


def carry_forward_evidence(fresh: dict, spec=None) -> dict:
    """Preserve recorded parity evidence across a regeneration.

    THIS EXISTS BECAUSE REGENERATION USED TO DESTROY EVIDENCE. `build_manifest`
    constructs the manifest from scratch, so `--write` reset `stages` to the
    hard-coded map and wiped `validation` and `tested_windows` — while
    `verify_s1 --record` printed, on success, "now regenerate Pine so the
    embedded status matches". Following that instruction silently deleted the
    record that had just been made. A round trip through the documented workflow
    must not lose evidence.

    Implementation status is NOT carried: it describes the artefact being
    generated right now. Everything else is evidence about a past comparison and
    survives until something invalidates it.
    """
    spec = spec or resolve_target()
    path = spec["manifest"]
    if not path.is_file() and spec["target_id"] == DEFAULT_TARGET \
            and LEGACY_MANIFEST_PATH.is_file():
        # ONE-TIME MIGRATION. Before build targets existed there was a single
        # `parity_manifest.json`, and it meant "the 15-minute oracle" — which is
        # now `detection_15m`. Reading it here is what stops the split from
        # silently discarding the feed measurement, the replay reasons and the
        # supersession trail that Wave 2b-i recorded.
        path = LEGACY_MANIFEST_PATH
    if not path.is_file():
        return fresh
    try:
        prior = ps.migrate_manifest(json.loads(path.read_text(encoding="utf-8")))
        # Evidence recorded against ANOTHER build proves nothing about this one:
        # different timeframe, different stages, different export schema. Reading
        # it would be worse than having none.
        if prior.get("build_target") not in (None, spec["target_id"]):
            return fresh
    except (ValueError, ps.ParityStatusError):
        # Unreadable prior manifest: keep the fresh, ignorant one rather than
        # guess. Losing a claim is safe; inventing one is not.
        return fresh

    # The CHART's export schema, for THIS target. A claim measured over a
    # different set of columns — or over the same columns packed differently —
    # has not been measured against this build, and carrying it forward would let
    # a transport refactor inherit a verdict nobody re-ran.
    schema = _export_schema(spec)

    for stage, rec in fresh["stages"].items():
        old = (prior.get("stages") or {}).get(stage)
        if not old:
            continue
        for dim, ignorant in _IGNORANT.items():
            if old[dim]["status"] in ignorant:
                # A record that was already DEMOTED still carries information —
                # the supersession trail, and the declared bootstrap the HUD
                # warns about. Dropping it back to a bare ignorant record made
                # generation non-idempotent: the first run embedded
                # ORACLE_LOGIC_BOOTSTRAP = 1632 and the second embedded 0, so the
                # file's own hash changed with no input change and the
                # tamper-detection test failed.
                if "superseded" in old[dim]:
                    rec[dim] = old[dim]
                continue
            if (dim == "logic_parity"
                    and old[dim].get("export_schema") != schema):
                rec[dim] = {
                    **old[dim],
                    "status": ps.LOGIC_UNVERIFIED,
                    "superseded": {
                        "status": old[dim]["status"],
                        "export_schema": old[dim].get("export_schema"),
                        "report_sha256": old[dim].get("report_sha256"),
                        "reason": "export schema",
                        "note": "the chart's exported columns or their packed "
                                "encoding changed; re-export and re-compare to "
                                "restore the claim",
                    },
                }
                continue
            rec[dim] = old[dim]
        if "migrated_from" in old:
            rec["migrated_from"] = old["migrated_from"]
    for key in ("validation", "tested_windows", "feed_measurement"):
        if prior.get(key) and key in fresh:
            fresh[key] = prior[key]
    if "global_status" in fresh:
        fresh["global_status"] = ps.global_status(fresh["stages"])
    return fresh


def build_manifest(contract: dict, pine_source: str, fragments: list[dict],
                   engine, spec=None) -> dict:
    spec = spec or resolve_target()
    fixtures_dir = CT_ROOT / "golden" / "tradingview_oracle" / "s1"
    fixtures = []
    idx = fixtures_dir / "INDEX.json"
    if idx.is_file():
        for entry in json.loads(idx.read_text(encoding="utf-8"))["fixtures"]:
            if entry.get("sha256"):
                fixtures.append({
                    "fixture_id": entry["fixture_id"], "path": entry["path"],
                    "sha256": entry["sha256"],
                    "trace_schema_version": fp.TRACE_SCHEMA_VERSION,
                    "rows": entry["rows"],
                })

    cfgctx = contract["configuration"]["pine_relevant"]["data_context"]
    enum_block = contract["enums"]

    return {
        "schema": ps.MANIFEST_SCHEMA_V3,
        "oracle_version": ORACLE_VERSION,
        # WHICH BUILD this manifest describes. Recorded first because everything
        # below is scoped by it: the stages, the export schema, the plot budget
        # and every evidence record. A manifest without it cannot be told apart
        # from the other target's.
        "build_target": spec["target_id"],
        "build": {
            "target_id": spec["target_id"],
            "label": spec["label"],
            "chart_timeframe": spec["timeframe"],
            "chart_timeframe_seconds": list(spec["chart_timeframe_seconds"]),
            "owned_stages": list(spec["stages"]),
            "plot_reserve": spec["plot_reserve"],
            "stage_ownership": {st: target_for_stage(st)
                                for st in sorted(STAGE_STATUS,
                                                 key=lambda s: int(s[1:]))},
        },
        "generated_at": None,   # see `validation.validated_at`; kept null so the
                                # manifest stays deterministic for identical input
        "fingerprint": {
            "oracle_engine_id": contract["fingerprint"]["oracle_engine_id"],
            "oracle_engine_hash": contract["fingerprint"]["oracle_engine_hash"],
            "inputs": contract["fingerprint"]["inputs"],
        },
        "production": {
            "control_tower_commit": _git(CT_ROOT, "rev-parse", "HEAD"),
            "control_tower_branch": _git(CT_ROOT, "rev-parse", "--abbrev-ref", "HEAD"),
            "engine_commit": _git(engine.lux_root, "rev-parse", "HEAD"),
            "engine_branch": _git(engine.lux_root, "rev-parse", "--abbrev-ref", "HEAD"),
            "golden_config_relpath": contract["configuration"]["provenance"][
                "golden_config_relpath"],
            "environment_provenance": engine.environment,
        },
        "pine_source": {
            "path": str(spec["pine"].relative_to(CT_ROOT)).replace("\\", "/"),
            "sha256": _sha256_text(pine_source),
            "line_count": pine_source.count("\n") + 1,
            "module_sources": fragments,
        },
        "export_schema": _export_schema(spec),
        "supported_context": {
            "symbols": [cfgctx["symbol"]],
            # THE TARGET'S timeframe. Production has two; this build owns one,
            # and running it on the other would apply parameters measured in a
            # different domain.
            "chart_timeframe": spec["timeframe"],
            "detection_timeframes": [cfgctx["detection_timeframe"]],
            "execution_timeframes": [cfgctx["execution_timeframe"]],
            "chart_timezone": "UTC",
            "minimum_history_bars": {"detection": 0, "daily": 0},
            "_note": "S1 has no history-seeded values, so no warm-up is required. "
                     "S2 (ATR seed) and S6 (regime panel) will raise these.",
        },
        "stages": recorded_stages(spec),
        "global_status": "PARTIAL",
        "feed_measurement": None,
        "tested_windows": [],
        "golden_fixtures": fixtures,
        "known_limitations": S1_LIMITATIONS,
        "unsupported_active_features": contract["unsupported_active_features"],
        "enum_coverage": {
            "checked_enums": len(enum_block),
            "unmapped": {},
            "oracle_extensions_used": [],
            "_note": "S1 renders only session/transition/data-context vocabularies; "
                     "the remaining enums are carried by the contract but not yet "
                     "referenced by any Pine fragment.",
        },
        "validation": {
            "result": "NOT_RUN",
            "validated_at": None,
            "command": "python -m tools.oracle.verify_s1",
            "stages_run": [],
            "exact_match_failures": 0,
            "tolerance_failures": 0,
            "feed_attributed_divergences": 0,
            "news_excluded_rows": 0,
            "warmup_excluded_rows": 0,
            "report_path": None,
        },
    }


def generate(write: bool = False, target: str = None) -> dict:
    spec = resolve_target(target)
    if not CONTRACT_PATH.is_file():
        raise GenerateError(
            "no parity contract — run `python -m tools.oracle.extract_contract --write`")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))

    engine = load_engine(require_pin=True)
    config, _ = resolve_config(engine)
    table, _ = effective_policy_table(engine, config)
    live = fp.from_engine(engine, config, table)
    if live["oracle_engine_hash"] != contract["fingerprint"]["oracle_engine_hash"]:
        raise GenerateError(
            "contract fingerprint does not match the deployed engine — regenerate "
            "the contract before generating Pine")

    if contract["unsupported_active_features"]:
        raise GenerateError(
            "unsupported production features are ACTIVE; the oracle would be "
            f"INCOMPATIBLE: {contract['unsupported_active_features']}")

    # Every session value the Pine fragments will render must have a code.
    codes = codes_mod.session_codes(contract)
    produced = set(contract["enums"]["session_key"]["values"])
    unmapped = sorted(produced - set(codes))
    if unmapped:
        raise GenerateError(
            f"production session keys with no generated Pine code: {unmapped}")

    pine, fragments = assemble(contract, spec)
    if "C:\\" in pine or "/Users/" in pine or "/home/" in pine:
        raise GenerateError("generated Pine contains an absolute path")

    manifest = carry_forward_evidence(
        build_manifest(contract, pine, fragments, engine, spec), spec)

    if write:
        # ONE target per call, into ITS OWN paths. `generate --write` used to
        # have a single destination; with two builds a shared one would mean the
        # last command run decides which oracle is on disk.
        for path, text in ((spec["pine"], pine),
                           (spec["manifest"], ps.serialize_manifest(manifest))):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8",
                            newline="\n" if path.suffix == ".pine" else None)

    return {"pine": pine, "manifest": manifest, "fragments": fragments,
            "target": spec["target_id"], "spec": spec}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--target", choices=sorted(BUILD_TARGETS), default=None,
                    help="which build to generate. REQUIRED with --write: "
                         "defaulting a destructive operation would let a typo "
                         "regenerate the other oracle, and only one of them has "
                         "recorded evidence against it.")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    if args.write and not args.target:
        print("FAIL: --write requires an explicit --target "
              f"({', '.join(sorted(BUILD_TARGETS))})", file=sys.stderr)
        return 2

    try:
        out = generate(write=args.write, target=args.target)
    except (GenerateError, EngineAccessError, UnknownTarget) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    m = out["manifest"]
    b = m["build"]
    print(f"build target   : {b['target_id']}  ({b['label']})")
    print(f"chart timeframe: {b['chart_timeframe']}   "
          f"owns {', '.join(b['owned_stages'])}")
    print(f"oracle version : v{m['oracle_version']}")
    print(f"fingerprint    : {m['fingerprint']['oracle_engine_id']}")
    print(f"pine source    : {m['pine_source']['path']}  "
          f"({m['pine_source']['line_count']} lines)")
    print(f"source sha256  : {m['pine_source']['sha256']}")
    print(f"fragments      : {len(out['fragments'])} "
          f"({sum(1 for f in out['fragments'] if f['generated'])} generated)")
    print(f"fixtures       : {len(m['golden_fixtures'])}")
    for st in sorted(m["stages"], key=lambda s: int(s[1:])):
        r = m["stages"][st]
        if r["implementation_status"] == ps.IMPL_UNIMPLEMENTED:
            continue
        boot = r["logic_parity"].get("bootstrap_bars") or 0
        print(f"  {st:<3} impl={r['implementation_status']:<12} "
              f"logic={r['logic_parity']['status']:<38}"
              + (f" boot={boot}" if boot else ""))
    print(f"feed           : {_feed(m['stages'])}   "
          f"replay: {_replay(m['stages'])}")
    print(f"global status  : {m['global_status']}")
    if args.write:
        print(f"\nwritten: {m['pine_source']['path']}")
        print(f"         "
              f"{out['spec']['manifest'].relative_to(CT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
