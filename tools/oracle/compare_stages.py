"""Stage-aware S1–S4 comparison of a TradingView export against the Python trace.

    python -m tools.oracle.compare_stages --fixture F-S4-STRUCTURE \
        --export <tv.csv> --feed "OANDA:EURUSD" --write

Successor to `compare_tv_export`, which handled S1 only. Same contract — join on
the UTC bar timestamp, diff mechanically, never by eye — extended with:

  * per-stage field groups and per-stage verdicts;
  * DEPENDENCY INVALIDATION. S1 defines the bar series S2/S3/S4 are computed on,
    and S3's swings are what S4 breaks. A failure upstream does not merely
    coexist with downstream results, it makes them meaningless, so downstream
    stages are reported INVALIDATED rather than PASS;
  * earliest-divergence reporting. A structure trace is a state machine: once the
    bias or a retained swing diverges, every later bar is wrong as a consequence.
    Only the FIRST divergence is diagnostic; the rest is noise.

TOLERANCES
Codes, flags and counts are EXACT — they are decisions. Prices are compared under
a tolerance because TradingView's CSV rounds and the feeds differ; the tolerance
is per stage, not global, because an S2 ATR is a derived accumulation while an S3
swing level is a raw bar extreme.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import tempfile
from pathlib import Path

from tools.oracle import session_codes as codes_mod
from tools.oracle.compare_tv_export import _norm, _parse_tv_time
from tools.oracle.engine_access import CT_ROOT
from tools.oracle.export_trace import TraceError, build_trace
from tools.oracle.generate_pine import STAGE_DEPENDS_ON

FIXTURE_DIR = CT_ROOT / "golden" / "tradingview_oracle" / "s1"
DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "s1_s5_compare.json"

EXACT = "exact"
PRICE = "price"
#: A running total that carries state from BEFORE the scored window. Compared as
#: an OFFSET from the first scored bar, never as an absolute — see `_baseline`.
CUMULATIVE = "cumulative"


def _s1(b):
    return {
        "oracleSessionCode": (b["session"]["code"], EXACT),
        "oracleUtcHour": (b["session"]["utc_hour"], EXACT),
        "oracleTransCode": (b["session"]["transition_code"], EXACT),
        "oracleDayKey": (codes_mod.day_key_to_int(b["utc_day"]["day_key"]), EXACT),
        "oracleSessIsFallback": (1 if b["session"]["is_fallback"] else 0, EXACT),
        "oracleCtxCode": (0 if b["data_context"]["status"] == "SUPPORTED" else 1, EXACT),
        "oracleBarsSinceTrans": (b["session"]["bars_since_transition"], EXACT),
    }


def _s2(b):
    s = b.get("s2")
    if not s:
        return {}
    return {
        "oracleTrueRange": (s["true_range"], PRICE),
        "oracleAtr": (s["atr"], PRICE),
        "oracleVolMeasure": (s["volatility_measure"], PRICE),
        "oracleParsedHigh": (s["parsed_high"], PRICE),
        "oracleParsedLow": (s["parsed_low"], PRICE),
        "oracleVolFlip": (1 if s["high_volatility"] else 0, EXACT),
        "oracleAtrWarm": (1 if s["atr_warm"] else 0, EXACT),
    }


def _s3(b):
    s = b.get("s3")
    if not s or s.get("warmup"):
        return {}
    return {
        "oracleS3Ready": (1, EXACT),
        "oracleNewLegHigh": (1 if s["new_leg_high"] else 0, EXACT),
        "oracleNewLegLow": (1 if s["new_leg_low"] else 0, EXACT),
        "oracleCurrentLeg": (s["current_leg"], EXACT),
        "oracleLegChange": (s["leg_change"], EXACT),
        "oracleSwingCreated": ({None: 0, "swing_high": 1, "swing_low": 2}[
            s["swing_created"]], EXACT),
        "oracleHasSwingHigh": (0 if s["swing_high_level"] is None else 1, EXACT),
        "oracleSwingHigh": (s["swing_high_level"], PRICE),
        "oracleSwingHighCrossed": (1 if s["swing_high_crossed"] else 0, EXACT),
        "oracleHasSwingLow": (0 if s["swing_low_level"] is None else 1, EXACT),
        "oracleSwingLow": (s["swing_low_level"], PRICE),
        "oracleSwingLowCrossed": (1 if s["swing_low_crossed"] else 0, EXACT),
    }


def _s4(b):
    s = b.get("s4")
    if not s or s.get("warmup"):
        return {}
    evs = s.get("events") or []
    # The plotted code reports the LAST event of the bar; when both a bull and a
    # bear break fire, that is the bear one. `event_count` marks the double.
    code = 0
    broken = None
    if evs:
        last = evs[-1]
        code = codes_mod.structure_codes()[
            codes_mod.structure_event_name(last["side"], last["tag"])]
        broken = last["swing_level"]
    # `oracleTrend` used to be plotted alongside `oracleBias`. It was a PURE
    # DUPLICATE: Python read `swing_trend_bias` for both, and Pine's
    # `s4_trend = bias == BIAS_BULLISH ? 1 : bias == BIAS_BEARISH ? -1 : 0`
    # is the identity map when BULLISH == 1 and BEARISH == -1, which the contract
    # pins. It cost a plot and proved nothing the other did not.
    return {
        "oracleBias": (s["swing_trend_bias"], EXACT),
        "oracleStructEvent": (code, EXACT),
        "oracleStructEventCount": (len(evs), EXACT),
        "oracleBrokenLevel": (broken, PRICE),
    }


def latch_s5(bars):
    """Carry each bar's most recent candidate forward, in place.

    Pine holds the last candidate's values in `var` state until the next one; a
    bar with no candidate keeps showing the previous box. The Python trace
    records candidates PER BAR, so it reports nothing on those bars. Neither is
    more faithful to production — production keeps no per-bar order-block state
    at all, it just appends to a list — so this is a REPORTING convention, and
    the two sides simply have to agree on it.

    Latching loses no detection power. The per-bar DECISION fields
    (`candidates`, `created`, `reject`, `side`, `tag`) are reset every bar and
    still compared, so a box that appears where it should not, or fails to
    appear, is caught on that bar. And a wrong bound on a candidate bar now
    diverges from that bar ONWARD rather than for one bar, which is easier to
    see, not harder.
    """
    last = None
    for b in bars:
        s = b.get("s5")
        if not s or s.get("warmup"):
            continue
        cands = s.get("candidates") or []
        if cands:
            last = cands[-1]
        s["latched"] = last


def _s5(b):
    s = b.get("s5")
    if not s or s.get("warmup"):
        return {}
    cands = s.get("candidates") or []
    # THIS BAR's decisions — reset every bar, on both sides.
    bar_last = cands[-1] if cands else None
    reject = 0
    if bar_last and bar_last.get("rejected_reason") == "size_filter":
        reject = 1
    elif bar_last and bar_last.get("rejected_reason") == "empty_search_window":
        reject = 2
    side = {None: 0, "bullish": 1, "bearish": 2}[
        bar_last["side"] if bar_last else None]
    tag = {None: 0, "BOS": 1, "CHoCH": 2}[bar_last["tag"] if bar_last else None]
    # …and the LATCHED description of the most recent candidate, which is what
    # the Pine plots carry between candidates.
    last = s.get("latched")
    brk = last.get("break_index") if last else None
    return {
        "oracleObCandidates": (len(cands), EXACT),
        "oracleObCreated": (s.get("created_count", 0), EXACT),
        "oracleObReject": (reject, EXACT),
        "oracleObSide": (side, EXACT),
        "oracleObTag": (tag, EXACT),
        "oracleObTop": (last.get("proposed_top") if last else None, PRICE),
        "oracleObBottom": (last.get("proposed_bottom") if last else None, PRICE),
        "oracleObWidthPips": (last.get("proposed_width_pips") if last else None,
                              PRICE),
        "oracleObBreakLevel": (last.get("break_level") if last else None, PRICE),
        # Identity is compared as a DISTANCE, not an index: `bar_index` is
        # feed-local (L-09), so only the gap between two bars survives a feed
        # that disagrees about which bars exist. -1 (not None) means "no
        # candidate has been latched yet" — these two now travel PACKED, and a
        # packed field has to be an integer, so the absence gets its own code
        # rather than a blank cell whose meaning depends on the reader.
        "oracleObOriginBack": (
            (brk - last["origin_index"])
            if last and last.get("origin_index") is not None else -1, EXACT),
        "oracleObPivotBack": (
            (brk - last["pivot_index"])
            if last and last.get("pivot_index") is not None else -1, EXACT),
        # `ob_id` is DELIBERATELY absent: production's counter starts in 2015 and
        # a chart's starts at the first loaded bar, so the values cannot agree
        # (limitation L-05). `oracleObActive` checks the SEQUENCE instead —
        # the count only advances on an append, which is the property that
        # actually encodes "a rejected block consumes no id".
        "oracleObActive": (s.get("active_ob_count", 0), CUMULATIVE),
        # …and the checksum makes a wrong box anywhere in the window fail here,
        # rather than only on the bar that drew it.
        "oracleObChecksum": (s.get("inventory_checksum", 0), CUMULATIVE),
    }


def _s6(b):
    s = b.get("s6")
    if not s or s.get("warmup"):
        return {}
    mstate = codes_mod.market_state_codes(_contract())
    trend = codes_mod.trend_state_codes()
    vol = codes_mod.volatility_state_codes()
    chop = codes_mod.chop_state_codes()
    valid = codes_mod.regime_validity_codes()

    def code(table, key):
        return 0 if key is None else table[key]

    return {
        # Daily identity. `source_day` is the day the state was classified FROM,
        # so comparing it proves the one-day shift landed on the same day rather
        # than merely producing a plausible state.
        "oracleRgSrcDay": (codes_mod.day_key_to_int(s["source_day"])
                           if s.get("source_day") else 0, EXACT),
        "oracleRgDayOpen": (s.get("daily_open"), PRICE),
        "oracleRgDayHigh": (s.get("daily_high"), PRICE),
        "oracleRgDayLow": (s.get("daily_low"), PRICE),
        "oracleRgDayClose": (s.get("daily_close"), PRICE),
        "oracleRgEma": (s.get("ema"), PRICE),
        "oracleRgPxVsEma": (s.get("px_vs_ema"), PRICE),
        "oracleRgBasis": (s.get("bb_basis"), PRICE),
        "oracleRgSd": (s.get("bb_sd"), PRICE),
        "oracleRgUpper": (s.get("bb_upper"), PRICE),
        "oracleRgLower": (s.get("bb_lower"), PRICE),
        "oracleRgBbw": (s.get("bbw"), PRICE),
        "oracleRgAdx": (s.get("adx"), PRICE),
        # DECISIONS — exact. A float inside tolerance never excuses one of these.
        "oracleRgTrend": (code(trend, s.get("trend_state")), EXACT),
        "oracleRgVol": (code(vol, s.get("volatility_state")), EXACT),
        "oracleRgChop": (code(chop, s.get("chop_state")), EXACT),
        "oracleRgState": (code(mstate, s.get("market_state")), EXACT),
        "oracleRgPrevState": (code(mstate, s.get("prev_market_state")), EXACT),
        "oracleRgValidity": (valid.get(s.get("validity"), 0), EXACT),
        # `daily_bars` and `day_index` are FEED-LOCAL (L-02/L-09): a feed that
        # disagrees about which intraday bars exist changes both without any
        # algorithm being wrong. They are exported for diagnosis, not scored.
    }


_CONTRACT_CACHE = {}


def _contract():
    """The committed contract, read once — the market-state codes are positional
    over `MARKET_STATES` and must come from the same place the Pine got them."""
    if "c" not in _CONTRACT_CACHE:
        from tools.oracle.extract_contract import DEFAULT_OUT as _CP
        _CONTRACT_CACHE["c"] = json.loads(_CP.read_text(encoding="utf-8"))
    return _CONTRACT_CACHE["c"]


_RAW_STAGE_FIELDS = {"S1": _s1, "S2": _s2, "S3": _s3, "S4": _s4, "S5": _s5,
                     "S6": _s6}


def _packed(fn, stage):
    """Wrap a stage's field function so its small-integer fields travel PACKED.

    TradingView caps a script at 64 plots and raises RE10140 at RUNTIME on a
    live chart — the compiler does not catch it and neither did static analysis
    until this was hit for real at 83 plots. Packing keeps every field's exact
    value and only changes the transport, so nothing is dropped from the
    comparison to fit a platform limit.

    A stage may pack into MORE THAN ONE plot (S5 does): its per-bar decisions and
    its latched identity distances differ in magnitude by three orders, and
    forcing them into one radix would waste width on every field.
    """
    groups = codes_mod.groups_for(stage)
    consumed = {f for g in groups for f, _w, _o in codes_mod.PACKED_SPEC[g]}

    def wrapped(b):
        fields = fn(b)
        if not fields:
            return fields
        out = {k: v for k, v in fields.items() if k not in consumed}
        for g in groups:
            names = [f for f, _w, _o in codes_mod.PACKED_SPEC[g]]
            out[codes_mod.PACKED_PLOT[g]] = (
                codes_mod.pack(g, {k: fields[k][0] for k in names}), EXACT)
        return out

    return wrapped


STAGE_FIELDS = {st: (_packed(fn, st) if codes_mod.groups_for(st) else fn)
                for st, fn in _RAW_STAGE_FIELDS.items()}

#: The oracle columns each stage exports — the EXPORT SCHEMA.
#:
#: Declared rather than derived, because the schema has to be fingerprintable
#: without an engine, a fixture or a trace in hand (`export_schema_hash` is read
#: by the generator, which runs before any comparison exists). Drift between this
#: declaration and what `STAGE_FIELDS` actually produces is caught by
#: `test_export_schema_matches_stage_fields`, so the duplication is checked, not
#: trusted.
STAGE_COLUMNS = {
    "S1": ("oracleDayKey", "oracleBarsSinceTrans", "oracleS1Codes"),
    "S2": ("oracleTrueRange", "oracleAtr", "oracleVolMeasure",
           "oracleParsedHigh", "oracleParsedLow", "oracleS2Codes"),
    "S3": ("oracleSwingHigh", "oracleSwingLow", "oracleS3Codes"),
    "S4": ("oracleBrokenLevel", "oracleS4Codes"),
    "S5": ("oracleObTop", "oracleObBottom", "oracleObWidthPips",
           "oracleObBreakLevel", "oracleObActive", "oracleObChecksum",
           "oracleS5Codes", "oracleS5Ids"),
    "S6": ("oracleRgSrcDay", "oracleRgDayOpen", "oracleRgDayHigh",
           "oracleRgDayLow", "oracleRgDayClose", "oracleRgEma",
           "oracleRgPxVsEma", "oracleRgBasis", "oracleRgSd", "oracleRgUpper",
           "oracleRgLower", "oracleRgBbw", "oracleRgAdx", "oracleRgCodes"),
    # EXECUTION ORACLE. `XF` is the derived 15-minute detection frame, not a
    # production stage — but it is the whole cross-timeframe claim, so it is
    # exported and scored like one.
    "XF": ("oracleXfBucket", "oracleXfOpen", "oracleXfHigh", "oracleXfLow",
           "oracleXfClose", "oracleXfCodes", "oracleXfBarCount",
           "oracleXfGapCount"),
}

#: Which exported surfaces each build owns. The schema fingerprint is computed
#: over THIS, not over everything, so a change confined to the 1-minute build
#: cannot stale the 15-minute build's recorded evidence — and vice versa. That
#: independence is the point of having two targets at all.
TARGET_SURFACES = {
    "detection_15m": ("S1", "S2", "S3", "S4", "S5", "S6"),
    "execution_1m": ("XF",),
    # The strategy companion EXPORTS NOTHING. It reuses the detection
    # build's compute fragments, so its parity question is already answered
    # there; a second export surface would be the same measurement filed
    # twice, and the first question would be which copy is authoritative.
    "strategy_companion": (),
}


def export_schema_hash(target: str = "detection_15m") -> str:
    """Fingerprint of WHAT the chart exports and HOW it is encoded.

    Recorded evidence is a measurement of one artefact. The engine hash already
    demotes a claim when production moves, but it does not move when only the
    PINE changes — and the packing refactor changed every small-integer field's
    transport without touching a single algorithm. A packing defect would have
    left "LOGIC_MATCHED" standing on a build whose exported numbers had never
    been compared.

    Hashing the column list and the packed encoding closes that: any change to
    which columns exist, what they are called, which stage owns them, or how a
    packed field is laid out produces a different schema and demotes the claims
    measured under the old one. It deliberately does NOT hash the whole Pine
    source — the status strings are themselves embedded in the Pine, so that
    would never converge.
    """
    if target not in TARGET_SURFACES:
        raise CompareError(f"unknown build target {target!r}")
    surfaces = TARGET_SURFACES[target]
    groups = [g for s in surfaces for g in codes_mod.groups_for(s)]
    payload = json.dumps(
        {"target": target,
         "columns": {k: list(STAGE_COLUMNS[k]) for k in sorted(surfaces)},
         "packed_spec": {g: [list(f) for f in codes_mod.PACKED_SPEC[g]]
                         for g in sorted(groups)},
         "packed_plot": {g: codes_mod.PACKED_PLOT[g] for g in sorted(groups)},
         "packed_groups": {k: list(codes_mod.groups_for(k))
                           for k in sorted(surfaces)}},
        sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

#: Absolute price tolerance per stage, in price units.
#: S1 has no price fields. S2's ATR is a 200-period accumulation whose seed
#: depends on where the loaded history starts, so it gets the loosest band and a
#: divergence there is expected to be reported, not hidden. S3/S4 levels are RAW
#: bar extremes and closes — a feed either has the same bar or it does not.
STAGE_PRICE_TOL = {"S1": 0.0, "S2": 5e-5, "S3": 1e-6, "S4": 1e-6,
                   # S5 bounds are S2 parsed prices read off one bar,
                   # so they inherit S2's band, not S3's raw-extreme one.
                   "S5": 5e-5,
                   # S6's daily values are accumulations over 20 daily closes;
                   # the binding constraint is TradingView's CSV rounding, not
                   # the arithmetic, so this matches S2's band for the same
                   # reason. Every STATE CODE is compared exactly.
                   "S6": 5e-5}


class CompareError(RuntimeError):
    pass


def _load_export(path: Path):
    if not path.is_file():
        raise CompareError(f"TradingView export not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise CompareError(f"{path.name} has no data rows")
    lookup = {_norm(h): h for h in rows[0].keys()}
    time_col = next((lookup[k] for k in ("time", "date", "datetime", "timestamp")
                     if k in lookup), None)
    if time_col is None:
        raise CompareError(f"no time column in {path.name}")
    return rows, lookup, time_col, hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


#: Measured convergence of a truncated-history run against a full-history run
#: (2025-09-30 start, to 2026-01-01). Used to justify a warm-up exclusion when the
#: chart's first bar does not exactly match the fixture's.
MEASURED_CONVERGENCE = {
    "swings_converge_bar": 84,
    "bias_converges_bar": 122,
    "atr_within_1e9_bar": 2000,
    "flip_decisions_differing": 3,
    "_note": "With a MATCHED start these are all zero — both sides seed "
             "identically. They quantify what a mismatched start costs.",
}


#: The two things a comparison can measure. They answer DIFFERENT questions and
#: conflating them is how a feed artefact gets recorded as a Pine defect.
FIXTURE_BARS = "fixture_bars"   # "would TradingView show what production computed?"
CHART_BARS = "chart_bars"       # "is the Pine a faithful port of the Python?"

INPUT_BASIS_NOTE = {
    FIXTURE_BARS:
        "Python ran on the PRODUCTION dataset; Pine ran on the broker's feed. A "
        "divergence here may be Pine logic OR a feed difference — the two are "
        "not separable in this mode.",
    CHART_BARS:
        "Python was re-run over the EXPORT'S OWN OHLC, so both sides saw "
        "byte-identical bars. The feed is eliminated as a variable: any "
        "divergence is Pine logic or a seed/warm-up boundary. This does NOT "
        "show that a chart on this feed reproduces production's values.",
}


def _rebase_input_to_chart_bars(rows, lookup, time_col, window, out_path: Path):
    """Write the export's OWN OHLC out as an engine input CSV.

    Comparing Pine-on-broker-feed against Python-on-production-data measures the
    FEED at least as much as the Pine. Re-running the Python trace over the exact
    bars the chart drew removes that variable entirely.

    The bars are written at the detection timeframe, so the engine's resample is
    the identity (left-labelled, left-closed: 15m -> 15m changes nothing).
    """
    need = ("open", "high", "low", "close")
    gone = [c for c in need if c not in lookup]
    if gone:
        raise CompareError(
            f"--input-basis chart needs the raw OHLC columns; missing {gone}. "
            "TradingView includes them by default — re-export without "
            "deselecting them.")
    lo, hi = window
    kept = 0
    with out_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["time", "open", "high", "low", "close", "volume"])
        for r in sorted(rows, key=lambda x: x[time_col]):
            dt = _parse_tv_time(r[time_col])
            if dt is None:
                continue
            stamp = dt.strftime("%Y-%m-%d %H:%M")
            if not (lo <= stamp <= hi):
                continue
            w.writerow([dt.strftime("%Y-%m-%d %H:%M:%S+00:00")]
                       + [r[lookup[c]] for c in need] + [0])
            kept += 1
    if not kept:
        raise CompareError(
            f"no exported bar falls inside the fixture window {lo} .. {hi}")
    return kept


def compare(fixture: str, export_path: Path, timeframe: str = "15min",
            feed: str = "", stages: tuple[str, ...] = ("S1", "S2", "S3", "S4", "S5", "S6"),
            max_examples: int = 5, warmup_bars: int = 0,
            input_basis: str = FIXTURE_BARS) -> dict:
    """`warmup_bars` excludes the first N shared bars from scoring.

    Default 0 — STRICT. It exists because Pine's structure state is seeded by the
    first bar the chart loaded, and if that is not the fixture's first bar the
    early window diverges legitimately (limitation L-04). Excluding bars is an
    explicit, recorded decision, never a silent one: the count and the reason are
    written into the report, and a stage that only passes because of an exclusion
    can be seen to have done so.

    `input_basis` selects WHAT is being measured — see FIXTURE_BARS / CHART_BARS.
    """
    if input_basis not in (FIXTURE_BARS, CHART_BARS):
        raise CompareError(f"unknown input_basis {input_basis!r}")
    csv_path = FIXTURE_DIR / f"{fixture}.csv"
    if not csv_path.is_file():
        raise CompareError(f"unknown fixture {fixture}")

    rows, lookup, time_col, export_sha = _load_export(export_path)

    if input_basis == FIXTURE_BARS:
        trace = build_trace(symbol="EURUSD", timeframe=timeframe,
                            input_path=csv_path, fixture_id=fixture)
        rebased_bars = None
    else:
        # The fixture still fixes the WINDOW, so both modes stay comparable; only
        # the price source changes.
        ref = build_trace(symbol="EURUSD", timeframe=timeframe,
                          input_path=csv_path, fixture_id=fixture)
        stamps = sorted(b["bar_timestamp"][:16] for b in ref["bars"])
        with tempfile.TemporaryDirectory() as td:
            rebased = Path(td) / "chart_bars.csv"
            rebased_bars = _rebase_input_to_chart_bars(
                rows, lookup, time_col, (stamps[0], stamps[-1]), rebased)
            trace = build_trace(symbol="EURUSD", timeframe=timeframe,
                                input_path=rebased,
                                fixture_id=f"{fixture}@chart_bars")

    # Which oracle columns does the export actually carry?
    wanted: dict[str, str] = {}
    missing: dict[str, list[str]] = {}
    probe = trace["bars"][-1]
    for st in stages:
        need = list(STAGE_FIELDS[st](probe).keys())
        gone = [c for c in need if _norm(c) not in lookup]
        if gone:
            missing[st] = gone
        for c in need:
            if _norm(c) in lookup:
                wanted[c] = lookup[_norm(c)]
    if "S1" in missing:
        raise CompareError(
            f"the export is missing required S1 columns: {missing['S1']}. "
            "Re-export with the indicator ON the chart.")
    # FAIL CLOSED on a requested stage whose columns are absent. Reporting
    # NO_EVIDENCE would let an export that predates the stage's plots read as
    # "nothing to see", which is indistinguishable from "verified" at a glance —
    # and S5's whole point is that a box drawn on screen is not evidence.
    for st in stages:
        if st != "S1" and st in missing:
            raise CompareError(
                f"the export is missing required {st} columns: {missing[st]}. "
                f"This export predates the {st} plots — regenerate the Pine, "
                "re-paste it, and export again.")

    # Reporting convention shared with the Pine side; see `latch_s5`.
    latch_s5(trace["bars"])

    tv = {}
    for r in rows:
        dt = _parse_tv_time(r[time_col])
        if dt is not None:
            tv[dt.strftime("%Y-%m-%d %H:%M")] = r
    py = {b["bar_timestamp"][:16]: b for b in trace["bars"]}
    both_all = sorted(set(py) & set(tv))
    both = both_all[warmup_bars:] if warmup_bars else both_all
    excluded = both_all[:warmup_bars] if warmup_bars else []

    results = {}
    first_div = {}
    for st in stages:
        if st in missing:
            results[st] = {"result": "NO_EVIDENCE", "reason":
                           f"export lacks columns: {missing[st]}",
                           "fields": {}, "compared": 0}
            continue
        tol = STAGE_PRICE_TOL[st]
        per_field: dict[str, dict] = {}
        examples: list[dict] = []
        compared = 0
        # A running total carries state from BEFORE the scored window, so after a
        # DECLARED bootstrap it can never agree even when every scored event
        # matches — one extra block created inside the excluded interval offsets
        # it forever. Comparing the offset from the first scored bar keeps the
        # cross-bar integrity check while honouring the exclusion. Forgiving what
        # accumulated inside a declared bootstrap is exactly what declaring it
        # means; anything that diverges INSIDE the window still fails.
        baseline: dict[str, tuple] = {}
        for ts in both:
            expected = STAGE_FIELDS[st](py[ts])
            if not expected:
                continue           # warm-up bar for this stage — not a mismatch
            compared += 1
            for field, (exp, kind) in expected.items():
                col = wanted.get(field)
                if col is None:
                    continue
                raw = (tv[ts].get(col) or "").strip()
                slot = per_field.setdefault(field, {"pass": 0, "fail": 0, "kind": kind})
                if kind == CUMULATIVE:
                    try:
                        got = float(raw)
                    except (TypeError, ValueError):
                        slot["fail"] += 1
                        continue
                    if field not in baseline:
                        baseline[field] = (float(exp), got)
                    b_py, b_tv = baseline[field]
                    okay = abs((float(exp) - b_py) - (got - b_tv)) <= tol
                    if okay:
                        slot["pass"] += 1
                    else:
                        slot["fail"] += 1
                        if st not in first_div:
                            first_div[st] = {
                                "bar_utc": ts, "field": field,
                                "expected_python": float(exp) - b_py,
                                "observed_pine": got - b_tv,
                                "note": "offset from the first scored bar"}
                    continue
                if exp is None:
                    # Python has no value; Pine should be blank or NaN.
                    okay = raw == "" or raw.lower() in ("nan", "na")
                else:
                    try:
                        got = float(raw)
                    except (TypeError, ValueError):
                        okay = False
                        got = None
                    else:
                        okay = (abs(got - float(exp)) <= tol if kind == PRICE
                                else int(round(got)) == int(exp))
                if okay:
                    slot["pass"] += 1
                else:
                    slot["fail"] += 1
                    if st not in first_div:
                        first_div[st] = {"bar_utc": ts, "field": field,
                                         "expected_python": exp, "observed_pine": raw}
                    if len(examples) < max_examples:
                        examples.append({"bar_utc": ts, "field": field,
                                         "expected_python": exp,
                                         "observed_pine": raw, "kind": kind})
        failed = sum(v["fail"] for v in per_field.values())
        results[st] = {
            "result": "PASS" if (compared and not failed) else
                      ("FAIL" if compared else "NO_EVIDENCE"),
            "compared": compared,
            "price_tolerance": tol,
            "fields": {k: {**v, "result": "PASS" if v["fail"] == 0 else "FAIL"}
                       for k, v in per_field.items()},
            "first_divergence": first_div.get(st),
            "examples": examples,
        }

    # ── dependency invalidation ──────────────────────────────────────────────
    # A downstream PASS over a broken upstream is not evidence of anything.
    for st in stages:
        bad = [d for d in STAGE_DEPENDS_ON.get(st, [])
               if results.get(d, {}).get("result") not in ("PASS", None)]
        if bad and results[st]["result"] == "PASS":
            results[st]["result"] = "INVALIDATED"
            results[st]["invalidated_by"] = bad
            results[st]["reason"] = (
                f"{st} compared clean, but {', '.join(bad)} did not pass. "
                f"{st} is computed on what {', '.join(bad)} produces, so the "
                "result carries no information.")

    overall = ("PASS" if all(results[s]["result"] == "PASS" for s in stages)
               else "FAIL")
    return {
        "schema": "tradingview-oracle-stage-comparison-v1",
        "stages": list(stages),
        "fixture": fixture, "timeframe": timeframe, "feed": feed,
        "input_basis": {
            "mode": input_basis,
            "means": INPUT_BASIS_NOTE[input_basis],
            "rebased_bars": rebased_bars,
        },
        "trace": {
            "oracle_engine_hash": trace["header"]["oracle_engine_hash"],
            "oracle_engine_id": trace["header"]["oracle_engine_id"],
            "trace_schema_version": trace["header"]["trace_schema_version"],
            "sections_implemented": trace["header"]["sections_implemented"],
        },
        "export": {"file": export_path.name, "sha256": export_sha,
                   "rows": len(rows), "missing_columns": missing},
        "coverage": {
            "bars_in_both": len(both),
            "bars_shared_before_warmup": len(both_all),
            "warmup_bars_excluded": len(excluded),
            "warmup_window": [excluded[0], excluded[-1]] if excluded else None,
            "python_only": len(set(py) - set(tv)),
            "tradingview_only": len(set(tv) - set(py)),
            "first_shared_bar": both[0] if both else None,
            "last_shared_bar": both[-1] if both else None,
            "measured_convergence": MEASURED_CONVERGENCE,
            "_note": "bars on only one side are FEED_DIFFERENCE (L-02), not a "
                     "Pine defect; they are excluded, not failed. "
                     "warmup_bars_excluded is a DECLARED exclusion — a stage that "
                     "only passes because of it can be seen to have done so.",
        },
        "results": results,
        "result": overall,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--timeframe", default="15min")
    ap.add_argument("--feed", default="")
    ap.add_argument("--stages", nargs="*",
                    default=["S1", "S2", "S3", "S4", "S5", "S6"])
    ap.add_argument("--warmup-bars", type=int, default=0,
                    help="exclude the first N shared bars from scoring. Use ONLY "
                         "when the chart's first bar differs from the fixture's; "
                         "the exclusion is recorded in the report.")
    ap.add_argument("--input-basis", choices=[FIXTURE_BARS, CHART_BARS],
                    default=FIXTURE_BARS,
                    help=f"{FIXTURE_BARS}: Python on the production dataset — "
                         "answers 'would this chart show production's numbers?'. "
                         f"{CHART_BARS}: re-run Python over the export's own OHLC "
                         "— answers 'is the Pine a faithful port?' by removing "
                         "the feed as a variable.")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        rep = compare(args.fixture, args.export, args.timeframe, args.feed,
                      tuple(args.stages), warmup_bars=args.warmup_bars,
                      input_basis=args.input_basis)
    except (CompareError, TraceError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"fixture : {rep['fixture']} @ {rep['timeframe']}   feed: "
          f"{rep['feed'] or '(not recorded)'}")
    ib = rep["input_basis"]
    print(f"basis   : {ib['mode']}"
          + (f"  ({ib['rebased_bars']} chart bars re-run through the engine)"
             if ib["rebased_bars"] else ""))
    c = rep["coverage"]
    print(f"coverage: {c['bars_in_both']} bars compared "
          f"(python-only {c['python_only']}, tv-only {c['tradingview_only']})")
    if c["warmup_bars_excluded"]:
        print(f"          {c['warmup_bars_excluded']} warm-up bars EXCLUDED "
              f"({c['warmup_window'][0]} .. {c['warmup_window'][1]}) — declared, "
              "not silent")
    if c["first_shared_bar"]:
        print(f"          window {c['first_shared_bar']} .. {c['last_shared_bar']}")
    print()
    for st in rep["stages"]:
        r = rep["results"][st]
        print(f"  [{r['result']:<11}] {st}  {r['compared']} bars")
        if r.get("reason"):
            print(f"                 {r['reason']}")
        fd = r.get("first_divergence")
        if fd:
            print(f"                 first divergence: {fd['bar_utc']} "
                  f"{fd['field']} python={fd['expected_python']} "
                  f"pine={fd['observed_pine']}")
    print(f"\nRESULT: {rep['result']}")

    if args.write:
        out = args.out if args.out.is_absolute() else (Path.cwd() / args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n",
                       encoding="utf-8")
        # `relative_to` RAISES when --out points outside CT_ROOT (or is a bare
        # relative path resolved from another cwd). Reporting where the file went
        # must never fail after the file has already been written.
        try:
            shown = out.relative_to(CT_ROOT)
        except ValueError:
            shown = out
        print(f"written: {shown}")
    return 0 if rep["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
