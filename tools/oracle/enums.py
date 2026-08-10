"""Extraction of every production enum / reason code the Pine oracle must handle.

TWO SOURCE CLASSES, AND THE DIFFERENCE MATTERS
----------------------------------------------
LIVE  — read from the imported engine at runtime (``strategy_core.MARKET_STATES``,
        ``_SESSION_SCHEDULE``, ``_status_label``'s own mapping, …). These cannot
        drift silently: if production changes them, extraction changes with it.

SCAN  — recovered by parsing the pinned source, because the values exist only as
        string literals at assignment sites (``row["outcome"] = "INVALID"``).
        There is no production constant to import. These are the values most
        likely to grow, so they are scanned rather than hand-listed — a new
        ``row["outcome"] = ...`` is picked up automatically.

MANUAL — values with no machine-readable source at all. Each one is declared
        here with an explicit justification and is drift-tested (§ tests).
        Keep this set as close to empty as possible.

WHY A SCAN AND NOT A HAND-LIST
------------------------------
Phase 0 established that the dominant production block path
(``state_target_block``, 62 configured state-blocks) is a plain string literal.
A hand-maintained Pine mapping would silently miss a 63rd. The scan makes
"Python grew a new outcome" a generation-time failure instead of a chart that
quietly renders the wrong label.
"""

from __future__ import annotations

from tools.oracle import parity_status as _ps

import ast
import re
from pathlib import Path

SOURCE_LIVE = "live"
SOURCE_SCAN = "scan"
SOURCE_MANUAL = "manual"


# ── scanned assignment targets ────────────────────────────────────────────────
# (relative path under LUX_ROOT, subscript key) -> enum name
_SCAN_TARGETS = {
    ("strategy_core/execution.py", "outcome"): "trade_outcome",
    ("strategy_core/execution.py", "cancel_reason"): "cancel_reason",
    ("strategy_core/execution.py", "missed_reason"): "missed_reason",
    ("strategy_core/execution.py", "regime_block_reason"): "regime_block_reason",
    ("strategy_core/execution.py", "be_exit_reason"): "be_exit_reason",
    ("strategy_core/execution.py", "risk_reduction_source"): "risk_reduction_source",
    ("strategy_core/execution.py", "state_eligibility"): "state_eligibility",
}


def _string_subscript_assignments(path: Path) -> dict[str, set[str]]:
    """Collect ``<something>["KEY"] = "LITERAL"`` string literals, keyed by KEY.

    Also follows ``_apply_missed_metrics(row, "reason")``-style helpers via
    ``_HELPER_ARG_TARGETS`` below, because several reason codes are only ever
    passed as an argument, never assigned directly.
    """
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if (isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and isinstance(target.slice.value, str)):
                out.setdefault(target.slice.value, set()).add(node.value.value)
    return out


#: helper(positional_index) -> enum name. These carry reason codes that never
#: appear as a direct subscript assignment.
_HELPER_ARG_TARGETS = {
    ("_apply_missed_metrics", 1): "missed_reason",
    ("_apply_triggered_edge_metadata", 2): "triggered_edge_cancel_reason",
    ("add_candidate", 1): "ghost_cancel_reason",
}


def _helper_arg_literals(path: Path) -> dict[str, set[str]]:
    tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        fname = getattr(node.func, "attr", getattr(node.func, "id", ""))
        for (target_name, idx), enum_name in _HELPER_ARG_TARGETS.items():
            if fname != target_name or len(node.args) <= idx:
                continue
            arg = node.args[idx]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and arg.value:
                out.setdefault(enum_name, set()).add(arg.value)
    return out


def _status_label_map(rb) -> dict[str, str]:
    """Recover ``run_backtest._status_label``'s dict by parsing its source.

    Calling it cannot enumerate the domain (it is a ``.get(status, "UNKNOWN")``
    over a dict literal), so the literal itself is read. Parsed, not hand-copied,
    so a new status appears here automatically.
    """
    import inspect
    import textwrap
    src = textwrap.dedent(inspect.getsource(rb._status_label))
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and node.keys:
            pairs = {}
            for k, v in zip(node.keys, node.values):
                if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                        and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                    pairs[k.value] = v.value
            if pairs:
                return pairs
    raise RuntimeError("could not recover the _status_label mapping")


def _rail_names(ct_root: Path) -> set[str]:
    """``RailVerdict(False, "<rail>", ...)`` names from live/safety.py."""
    tree = ast.parse((ct_root / "live" / "safety.py").read_text(encoding="utf-8"))
    out = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "id", "") == "RailVerdict"
                and len(node.args) >= 2
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value is False
                and isinstance(node.args[1], ast.Constant)):
            out.add(node.args[1].value)
    return out


def extract(engine, ct_root: Path) -> dict:
    """Build the full enum block for the parity contract."""
    lux = engine.lux_root
    core, rb = engine.core, engine.rb

    exec_path = lux / "strategy_core" / "execution.py"
    subs = _string_subscript_assignments(exec_path)
    helper = _helper_arg_literals(exec_path)

    enums: dict[str, dict] = {}

    def add(name, values, source, origin, note=""):
        enums[name] = {
            "values": sorted(values),
            "count": len(set(values)),
            "source": source,
            "origin": origin,
            "note": note,
        }

    # ── LIVE ──────────────────────────────────────────────────────────────────
    add("market_state", core.MARKET_STATES, SOURCE_LIVE,
        "strategy_core.regime.MARKET_STATES")
    add("trend_state", {s.split("/")[0] for s in core.MARKET_STATES}, SOURCE_LIVE,
        "derived from strategy_core.regime.MARKET_STATES")
    add("volatility_state", {"Expand", "Compress"}, SOURCE_LIVE,
        "strategy_core.regime.classify_state",
        "literal in classify_state; the only two volatility labels")
    add("chop_state", {"Chop", "Trend"}, SOURCE_LIVE,
        "strategy_core.regime.classify_state")
    add("session_key", [k for *_, k in core._SESSION_SCHEDULE] + [core._SESSION_OUTSIDE[1]],
        SOURCE_LIVE, "strategy_core.sessions._SESSION_SCHEDULE + _SESSION_OUTSIDE")
    add("session_label", [lbl for *_, lbl, _ in
                          [(s, e, l, k) for s, e, l, k in core._SESSION_SCHEDULE]]
        + [core._SESSION_OUTSIDE[0]],
        SOURCE_LIVE, "strategy_core.sessions._SESSION_SCHEDULE + _SESSION_OUTSIDE")
    add("policy_regime", core.policy.REGIME_POLICIES if hasattr(core, "policy")
        else __import__("strategy_core.policy", fromlist=["x"]).REGIME_POLICIES,
        SOURCE_LIVE, "strategy_core.policy.REGIME_POLICIES")
    add("policy_confidence",
        __import__("strategy_core.policy", fromlist=["x"]).CONFIDENCES,
        SOURCE_LIVE, "strategy_core.policy.CONFIDENCES")
    add("policy_structure",
        __import__("strategy_core.policy", fromlist=["x"]).STRUCTURES,
        SOURCE_LIVE, "strategy_core.policy.STRUCTURES")
    add("policy_direction",
        __import__("strategy_core.policy", fromlist=["x"]).DIRECTIONS,
        SOURCE_LIVE, "strategy_core.policy.DIRECTIONS")
    add("structure_tag", {core.BOS, core.CHOCH}, SOURCE_LIVE,
        "strategy_core.order_blocks.BOS / CHOCH")
    add("ob_direction", {"bullish", "bearish"}, SOURCE_LIVE,
        "strategy_core.order_blocks._make_order_block",
        "the only two values _make_order_block is ever called with")

    status_map = _status_label_map(rb)
    add("ob_final_status", status_map.keys(), SOURCE_LIVE,
        "scripts.run_backtest._status_label (dict literal, parsed)")
    add("ob_final_status_label", status_map.values(), SOURCE_LIVE,
        "scripts.run_backtest._status_label (dict literal, parsed)")
    enums["ob_final_status"]["label_map"] = dict(sorted(status_map.items()))
    add("ob_lifecycle_column", rb.OB_LIFECYCLE_COLUMNS, SOURCE_LIVE,
        "scripts.run_backtest.OB_LIFECYCLE_COLUMNS")

    # ── SCAN ──────────────────────────────────────────────────────────────────
    for (relpath, key), name in _SCAN_TARGETS.items():
        if relpath != "strategy_core/execution.py":
            continue
        vals = {v for v in subs.get(key, set()) if v}
        if name == "missed_reason":
            vals |= helper.get("missed_reason", set())
        if vals:
            add(name, vals, SOURCE_SCAN,
                f"{relpath} :: assignments to row[{key!r}]")
    for name in ("triggered_edge_cancel_reason", "ghost_cancel_reason"):
        if helper.get(name):
            add(name, helper[name], SOURCE_SCAN,
                "strategy_core/execution.py :: helper call literals")

    # ── live wrapper ──────────────────────────────────────────────────────────
    import live.intents as intents
    add("intent_action",
        {intents.OPEN_POSITION, intents.CLOSE_POSITION, intents.MODIFY_STOP,
         intents.SKIP_INTRA_WINDOW},
        SOURCE_LIVE, "live.intents module constants")
    add("engine_exited_outcome", intents._EXITED, SOURCE_LIVE,
        "live.intents._EXITED",
        "the outcomes the live layer treats as an exit for mirroring")
    add("safety_rail", _rail_names(ct_root), SOURCE_SCAN,
        "live/safety.py :: RailVerdict(False, <rail>, ...)")

    return enums


# ── MANUAL mappings — keep this as close to empty as possible ────────────────
#
# ob_final_status does NOT cover the production-live block outcomes. Measured on
# the committed golden run (golden/run-001/parity-oracle/rehearsal/order_blocks.csv,
# 2080 rows): 809 order blocks — 38.9%, the LARGEST single group — carry
# ob_final_status="unknown" / label "UNKNOWN", and every one of them has
# lifecycle_reason="state_target_block".
#
# Cause: _derive_ob_lifecycle_from_trade (run_backtest.py:3072-3167) has no branch
# for COHORT_DISABLED / STATE_BLOCKED / REGIME_BLOCKED, so all three fall through
# to `else: status = "unknown"` (L3147-3150).
#
# Consequence for Pine: mirroring _status_label verbatim would render the dominant
# production path as UNKNOWN. The oracle therefore extends the vocabulary with the
# three statuses below and MUST label them as an oracle-side extension, not as
# production values. lifecycle_reason carries the truth in Python and is the field
# the HUD should show.
#
# This is a DIVERGENCE FROM THE DRIVER VOCABULARY, declared on purpose. It is not
# a licence to invent labels for anything else.
ORACLE_STATUS_EXTENSIONS = {
    "state_blocked": {
        "label": "STATE BLOCKED",
        "python_outcome": "STATE_BLOCKED",
        "python_lifecycle_reason": "state_target_block",
        "driver_status_would_be": "unknown",
        "justification": "run_backtest._derive_ob_lifecycle_from_trade has no branch "
                         "for STATE_BLOCKED; 809/2080 golden OBs land here",
    },
    "cohort_disabled": {
        "label": "COHORT DISABLED",
        "python_outcome": "COHORT_DISABLED",
        "python_lifecycle_reason": "cohort_disabled",
        "driver_status_would_be": "unknown",
        "justification": "same missing branch; unreachable under the current Golden "
                         "scenario (all 24 cohorts have eligibility.base='allow') "
                         "but reachable if a base is ever disabled",
    },
    "regime_blocked": {
        "label": "REGIME BLOCKED",
        "python_outcome": "REGIME_BLOCKED",
        "python_lifecycle_reason": "regime_blocked",
        "driver_status_would_be": "unknown",
        "justification": "same missing branch; reachable via the 4 DIRECTION_AWARE "
                         "and 1 STATE_ONLY effective EURUSD cohorts",
    },
}

#: Enums Pine must render but which have NO production source at all, because they
#: describe the oracle's own state. Drift-tested only against this file.
ORACLE_ONLY_ENUMS = {
    "parity_status": ["MATCHED", "PARTIAL", "UNVERIFIED", "STALE_ENGINE",
                      "STALE_CONFIG", "STALE_CONTRACT", "STALE_TRACE_SCHEMA",
                      "ENGINE_MISMATCH", "CONFIG_MISMATCH", "UNSUPPORTED_DATA_CONTEXT",
                      "INCOMPATIBLE"],
    # DEPRECATED as a per-stage value. Kept so a v1 manifest stays readable and
    # so the contract does not silently drop a published vocabulary; the live
    # model is the four below. See tools/oracle/parity_status.py.
    "stage_status": ["MATCHED", "UNIMPLEMENTED", "FAILING", "NOT_APPLICABLE"],
    # The four-dimension model. Sourced from `parity_status` so the contract and
    # the code cannot drift — a hand-copied list here is exactly the drift the
    # contract exists to catch.
    "stage_implementation_status": list(_ps.IMPLEMENTATION_STATUSES),
    "stage_logic_parity_status": list(_ps.LOGIC_STATUSES),
    "stage_feed_parity_status": list(_ps.FEED_STATUSES),
    "stage_production_replay_parity_status": list(_ps.REPLAY_STATUSES),
    "comparison_input_basis": list(_ps.INPUT_BASES),
}


def unmapped_against(enums: dict, pine_mapping: dict) -> dict[str, list[str]]:
    """Values present in production but absent from a Pine mapping.

    This is the function that makes STEP 6's rule enforceable: generation FAILS
    when production grows a value the Pine mapping does not name. Nothing is
    allowed to fall back to a generic bucket.
    """
    missing: dict[str, list[str]] = {}
    for name, block in enums.items():
        mapped = set(pine_mapping.get(name, []))
        if name in ("ob_final_status", "ob_final_status_label"):
            mapped |= set(ORACLE_STATUS_EXTENSIONS)
        gap = sorted(set(block["values"]) - mapped)
        if gap:
            missing[name] = gap
    return missing
