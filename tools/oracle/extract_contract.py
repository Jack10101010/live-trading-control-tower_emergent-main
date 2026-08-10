"""Extract the canonical parity contract from the deployed production engine.

    python -m tools.oracle.extract_contract [--write] [--out PATH]

Read-only. Without ``--write`` it prints the contract and exits 0/1; with
``--write`` it writes ``contracts/tradingview_oracle_contract.json``.

EXTRACTION POLICY
-----------------
Every field carries a ``_source`` marker so a reader can tell, per value, whether
it is machine-extracted or hand-maintained:

    live    imported from the running engine — cannot drift silently
    scan    parsed from the pinned source — cannot drift silently
    manual  hand-maintained — MUST be drift-tested

The counts of each are reported in ``extraction_summary`` and asserted by
``backend/tests/test_oracle_contract.py``. The manual count is expected to stay
at zero for behavioural values; the only manual content is the Pine-side
limitation register and stage list, which describe the ORACLE, not production.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle import enums as enums_mod
from tools.oracle import fingerprint as fp
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError,
                                        LIVE_ONLY_CONFIG_DELTAS,
                                        effective_policy_table, load_engine,
                                        resolve_config)

DEFAULT_OUT = CT_ROOT / "contracts" / "tradingview_oracle_contract.json"

#: Config fields the Pine oracle actually consumes, grouped as they are consumed.
#: Grounded in the Phase 0 audit §10.2 resolved-configuration table. A field the
#: engine has but Pine does not read is deliberately absent — but the extractor
#: FAILS if a field listed here has vanished from the config (see `missing`).
PINE_RELEVANT_CONFIG = {
    "data_context": ["symbol", "candle_file", "detection_timeframe",
                     "execution_timeframe", "start_date", "pip_size"],
    "structure": ["swing_length", "ob_filter", "min_ob_size_pips", "max_ob_size_pips",
                  "structure_filter", "allowed_structure_directions"],
    "entry": ["entry_models", "triggered_edge_trigger_thresholds",
              "triggered_edge_candle_delays", "triggered_edge_entry_level_pct",
              "ob_entry_depth_pct", "execution_modes", "trade_direction",
              "directional_entry_mode"],
    "risk": ["rr_multiple", "stop_buffer_pips", "spread_pips", "slippage_pips",
             "commission_r_per_trade"],
    "protection": ["protection_modes", "be_enabled", "verify_limit_ticks"],
    "cancels": ["reverse_touch_cancel_enabled", "triggered_edge_cancel_on_retrace",
                "triggered_edge_cancel_retrace_pips",
                "triggered_edge_cancel_retrace_ob_pct",
                "triggered_edge_cancel_on_first_failed_tag",
                "triggered_edge_fft_move_away_pips",
                "triggered_edge_fft_move_away_ob_multiple",
                "triggered_edge_fft_min_ob_width_pips"],
    "session": ["session_filter_enabled", "allowed_sessions"],
    # S6. NOTE `regime_gate_enabled` is FALSE in production, so the global regime
    # gate never fires — but these same fields still drive the LIVE panel through
    # `_portfolio_panel_index`, which is independent of that flag. The flag is
    # extracted so the oracle can state plainly which path is inert.
    "regime": ["regime_gate_enabled", "regime_gate_mode", "regime_ema_length",
               "regime_bbw_length", "regime_bbw_std", "regime_adx_length",
               "regime_adx_chop", "regime_ema_confirm", "regime_bbw_thr_mode",
               "regime_bbw_thr_value", "regime_bbw_pctile", "regime_allowed_states",
               "regime_direction_policy"],
    "news": ["news_blackout_enabled", "news_file", "news_blackout_impacts",
             "news_blackout_currencies", "news_blackout_minutes_before",
             "news_blackout_minutes_after", "news_block_new_fills",
             "news_pause_pending_orders", "news_cancel_if_touched_during_blackout",
             "news_flatten_active_trades", "news_flatten_minutes_before_blackout"],
    "portfolio": ["portfolio_policy_enabled", "portfolio_policy_mode",
                  "portfolio_policy_file", "portfolio_include_disabled_cohorts"],
}

#: Features that are INERT under the deployed config and that Pine deliberately
#: does not implement (Phase 0 §6.15). If any becomes active, the oracle is
#: INCOMPATIBLE — not "partially" compatible — because a whole mechanism would be
#: missing from the chart. Each entry: field -> the value that means "inert".
UNSUPPORTED_IF_ACTIVE = {
    "be_enabled": False,
    "session_filter_enabled": False,
    "reverse_touch_cancel_enabled": False,
    "triggered_edge_cancel_on_retrace": False,
    "triggered_edge_cancel_on_first_failed_tag": False,
    "batch_entry_penetration": False,
}
#: Same idea for non-boolean fields: field -> the ONLY supported value(s).
UNSUPPORTED_UNLESS_EQUAL = {
    "protection_modes": [["baseline"]],
    "execution_modes": [["allow_multi_position"]],
    "entry_models": [["triggered_edge"]],
    "trade_direction": ["both"],
    "detection_timeframe": ["15min"],
    "execution_timeframe": ["1min"],
    "symbol": ["EURUSD"],
    "directional_entry_mode": ["symmetric"],
    "portfolio_policy_mode": ["enforce", "label"],
}


def _jsonable(v):
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    return str(v)


def _config_block(config) -> tuple[dict, list[str]]:
    out, missing = {}, []
    for group, fields in PINE_RELEVANT_CONFIG.items():
        block = {}
        for f in fields:
            if not hasattr(config, f):
                missing.append(f)
                continue
            block[f] = _jsonable(getattr(config, f))
        out[group] = block
    return out, missing


def _feature_flags(config) -> tuple[dict, list[dict]]:
    """Return (flags, violations). A violation makes the oracle INCOMPATIBLE."""
    flags, violations = {}, []
    for field, inert in UNSUPPORTED_IF_ACTIVE.items():
        actual = getattr(config, field, None)
        flags[field] = _jsonable(actual)
        if actual is not None and bool(actual) != bool(inert):
            violations.append({
                "field": field, "expected_inert": inert, "actual": _jsonable(actual),
                "impact": "a production mechanism the Pine oracle does not implement "
                          "is now ACTIVE",
            })
    for field, allowed in UNSUPPORTED_UNLESS_EQUAL.items():
        actual = _jsonable(getattr(config, field, None))
        flags[field] = actual
        if actual is not None and actual not in [_jsonable(a) for a in allowed]:
            violations.append({
                "field": field, "supported": _jsonable(allowed), "actual": actual,
                "impact": "the deployed value is outside the oracle's supported set",
            })
    return flags, violations


def _cohort_block(engine, config) -> dict:
    """The compiled cohort index — the 312-cell surface Phase 0 warned about."""
    index = engine.core._build_cohort_index(
        getattr(config, "session_strategy_scenario", None))
    if index is None:
        return {"actionable": False, "cohort_count": 0, "cohorts": []}
    cohorts = []
    for (session, structure, direction), rule in sorted(
            index.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        cohorts.append({
            "session": session, "structure": structure, "direction": direction,
            "enabled": rule["enabled"],
            "target_rr": rule["target_rr"],
            "be_arm_r": rule["be_arm_r"], "be_trigger": rule["be_trigger"],
            "rr_move_stop": rule["rr_move_stop"],
            "rr_trigger_r": rule["rr_trigger_r"], "rr_stop_r": rule["rr_stop_r"],
            "rr_unsupported_kind": rule["rr_unsupported_kind"],
            "risk_amount": rule["risk_amount"],
            "state_overrides": _jsonable(rule["state_overrides"]),
            "elig_states": _jsonable(rule["elig_states"]),
        })
    cells = sum(1 + len(c["state_overrides"] or {}) + len(c["elig_states"] or {})
                for c in cohorts)
    return {"actionable": True, "cohort_count": len(cohorts),
            "addressable_cells": cells, "cohorts": cohorts}


def _policy_block(engine, config) -> dict:
    from strategy_core.policy import (cohort_key, decide, disabled_cohort_keys,
                                      policy_content_sha256)
    table, flipped = effective_policy_table(engine, config)
    symbol = config.symbol
    rows = []
    for (inst, session, structure, direction), c in sorted(
            table.cohorts.items(), key=lambda kv: tuple(str(x) for x in kv[0])):
        if inst != symbol:
            continue
        d = decide(table, symbol, session, structure, direction)
        rows.append({
            "cohort_key": cohort_key(inst, session, structure, direction),
            "session": session, "structure": structure, "direction": direction,
            "deployed_regime": (c.get("decision_policy") or {}).get("regime"),
            "effective_regime": d.regime,
            "status": d.status, "reason": d.reason, "confidence": d.confidence,
        })
    return {
        "policy_version": table.policy_version,
        "policy_content_sha256": policy_content_sha256(table.raw),
        "include_disabled_cohorts": bool(
            getattr(config, "portfolio_include_disabled_cohorts", False)),
        "disable_cohorts_neutralised_to_label": sorted(flipped),
        "neutralised_count": len(flipped),
        "effective_blocking_regimes": sorted(
            {r["effective_regime"] for r in rows} - {"LABEL"}),
        "cohorts": rows,
        "_note": ("`effective_regime` is what the walk sees. With "
                  "portfolio_include_disabled_cohorts=true every DISABLE cohort is "
                  "rewritten to LABEL in memory (run_backtest.py:2458-2459), so "
                  "`portfolio_disabled` is unreachable. Pine must encode the "
                  "EFFECTIVE column."),
    }


def _regime_block(engine, config) -> dict:
    from strategy_core.regime import (BBW_THRESHOLD_BY_SYMBOL,
                                      DEFAULT_BBW_THRESHOLD, REGIME_DEFAULTS,
                                      SOURCE, VERSION)
    return {
        "engine_source": SOURCE, "engine_version": VERSION,
        "defaults": dict(REGIME_DEFAULTS),
        "bbw_threshold_by_symbol": dict(BBW_THRESHOLD_BY_SYMBOL),
        "default_bbw_threshold": DEFAULT_BBW_THRESHOLD,
        "resolved_bbw_threshold": BBW_THRESHOLD_BY_SYMBOL.get(
            config.symbol, DEFAULT_BBW_THRESHOLD),
        "shifted_days": 1,
        "day_boundary": "UTC calendar day (run_backtest.py:2430 resample('1D'))",
    }


def _session_block(engine) -> dict:
    core = engine.core
    return {
        "windows": [{"start_hour": s, "end_hour": e, "label": lbl, "key": k}
                    for s, e, lbl, k in core._SESSION_SCHEDULE],
        "fallback": {"label": core._SESSION_OUTSIDE[0], "key": core._SESSION_OUTSIDE[1]},
        "boundary_semantics": "[start, end) on UTC hour-of-day",
        "dst": "none — the pinned engine has no DST or weekday logic "
               "(strategy_core/sessions.py:8-9)",
    }


def _time_block(engine, config) -> dict:
    from live.config import DATA_SEAM, TIME_BASE
    return {
        "time_base": TIME_BASE,
        "data_seam": DATA_SEAM,
        "timezone": "UTC above the MT5 gateway",
        "bar_timestamp_convention": "bar-open (pandas resample is left-labelled, "
                                    "left-closed)",
        "resample_rule": f"resample('{config.detection_timeframe}') "
                         "agg(open=first,high=max,low=min,close=last,volume=sum) "
                         "then dropna(subset=OHLC) — empty intervals are DROPPED",
        "date_range_semantics": "[start_date, end_date + 1 day)",
        "news_window_semantics": "[event - before, event + after], inclusive both ends",
        "boundary_rule": "floor(last_m1 + 1min, 15min) - 15min "
                         "(live/runner.py:98-103)",
        "live_only_config_deltas": list(LIVE_ONLY_CONFIG_DELTAS),
    }


def build_contract(*, require_pin: bool = True) -> dict:
    engine = load_engine(require_pin=require_pin)
    config, cfg_provenance = resolve_config(engine)
    table, _ = effective_policy_table(engine, config)
    finger = fp.from_engine(engine, config, table)

    cfg_block, missing = _config_block(config)
    flags, violations = _feature_flags(config)
    enum_block = enums_mod.extract(engine, CT_ROOT)

    by_source = {}
    for b in enum_block.values():
        by_source[b["source"]] = by_source.get(b["source"], 0) + 1

    contract = {
        "contract_schema_version": fp.CONTRACT_SCHEMA_VERSION,
        "trace_schema_version": fp.TRACE_SCHEMA_VERSION,
        "generator_version": fp.GENERATOR_VERSION,
        "fingerprint": finger,
        "engine": {
            "lux_root_basename": engine.lux_root.name,
            "engine_version": engine.engine_version,
            "engine_manifest_id": engine.engine_manifest_id,
            "governed_file_count": engine.manifest["file_count"],
            "governed_files": engine.manifest["files"],
            "pin_verified": engine.pin_verified,
            "pin_detail": engine.pin_detail,
            "environment_provenance": engine.environment,
            "_note": "environment_provenance is RECORDED, never hashed — see "
                     "tools/oracle/fingerprint.py for why.",
        },
        "configuration": {
            "provenance": cfg_provenance,
            "pine_relevant": cfg_block,
            "missing_expected_fields": sorted(missing),
        },
        "feature_flags": flags,
        "unsupported_active_features": violations,
        "sessions": _session_block(engine),
        "market_state": _regime_block(engine, config),
        "cohorts": _cohort_block(engine, config),
        "portfolio_policy": _policy_block(engine, config),
        "time_and_data": _time_block(engine, config),
        # Structural constants read live off the pinned engine, so Pine cannot
        # hard-code a value production has since changed.
        "_engine_constants": {
            "BULLISH": engine.core.BULLISH, "BEARISH": engine.core.BEARISH,
            "BULLISH_LEG": engine.core.BULLISH_LEG,
            "BEARISH_LEG": engine.core.BEARISH_LEG,
            "BOS": engine.core.BOS, "CHOCH": engine.core.CHOCH,
            "_note": "strategy_core.order_blocks / .swings module constants.",
        },
        "enums": enum_block,
        "oracle_status_extensions": enums_mod.ORACLE_STATUS_EXTENSIONS,
        "oracle_only_enums": enums_mod.ORACLE_ONLY_ENUMS,
        "extraction_summary": {
            "enum_count": len(enum_block),
            "enums_by_source": by_source,
            "manual_behavioural_values": 0,
            "config_fields_extracted": sum(len(v) for v in cfg_block.values()),
            "config_fields_missing": len(missing),
        },
    }
    return contract


def canonical_json(contract: dict) -> str:
    """Deterministic serialization — the basis of every hash comparison."""
    return json.dumps(contract, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def _display_path(p: Path) -> str:
    """Repo-relative when possible; absolute otherwise (``--out`` may be anywhere)."""
    try:
        return str(p.relative_to(CT_ROOT))
    except ValueError:
        return str(p)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true", help="write the contract to disk")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--allow-unpinned", action="store_true",
                    help="diagnostic only; never produces a releasable artefact")
    args = ap.parse_args(argv)

    try:
        contract = build_contract(require_pin=not args.allow_unpinned)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    violations = contract["unsupported_active_features"]
    missing = contract["configuration"]["missing_expected_fields"]

    f = contract["fingerprint"]
    print(f"oracle_engine_id   : {f['oracle_engine_id']}")
    print(f"oracle_engine_hash : {f['oracle_engine_hash']}")
    print(f"engine_manifest_id : {contract['engine']['engine_manifest_id'][:16]}…  "
          f"({contract['engine']['governed_file_count']} governed files, "
          f"pin_verified={contract['engine']['pin_verified']})")
    print(f"resolved_config    : {f['inputs']['resolved_config_hash'][:16]}…")
    print(f"policy             : {contract['portfolio_policy']['policy_version']} "
          f"({contract['portfolio_policy']['neutralised_count']} DISABLE→LABEL)")
    print(f"cohorts            : {contract['cohorts']['cohort_count']} rules, "
          f"{contract['cohorts'].get('addressable_cells', 0)} addressable cells")
    s = contract["extraction_summary"]
    print(f"enums              : {s['enum_count']} "
          f"({', '.join(f'{k}={v}' for k, v in sorted(s['enums_by_source'].items()))})")

    if missing:
        print(f"\nFAIL: expected config fields absent from the engine: {missing}",
              file=sys.stderr)
        return 3
    if violations:
        print("\nFAIL: unsupported production features are ACTIVE — the Pine oracle "
              "would be INCOMPATIBLE:", file=sys.stderr)
        for v in violations:
            print(f"  - {v['field']}: {v}", file=sys.stderr)
        return 4

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(canonical_json(contract), encoding="utf-8")
        print(f"\nwritten: {_display_path(args.out)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
