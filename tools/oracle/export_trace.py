"""Offline canonical trace exporter — Stage S1 (data / time / session).

    python -m tools.oracle.export_trace --stage S1 --symbol EURUSD \
        --timeframe 15min --input <candle-csv> --output <trace.json>

A PURE OFFLINE ADAPTER. It calls the SAME production functions the live pipeline
calls — ``prepare_candles_for_simulation``, ``resample_candles``, ``_utc_session``,
``_cohort_session_key``, ``utc_date_key`` — over a stored candle file. It never
runs the walk, never touches ``live_state/``, never writes into ``LUX_ROOT``, and
never acquires the live node's lock. Phase 0 §17 explains why derivation is
preferred over instrumenting the walk: any edit inside ``strategy_core`` moves
``engine_manifest_id`` and makes the deployment refuse to trade.

WHAT S1 COVERS, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
Implemented sections: ``time``, ``session``, ``utc_day``, ``data_context``.
Everything later (volatility, structure, order blocks, market state, candidates,
cohorts, lifecycle) is ABSENT from every row — not null, not zero. The header's
``sections_implemented`` is the authority, and a comparator must ignore anything
not listed there. Emitting placeholder values would be indistinguishable from
implemented parity, which is exactly the failure mode this design exists to
prevent.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle import fingerprint as fp
from tools.oracle import replay_regime
from tools.oracle import replay_structure
from tools.oracle import session_codes as codes_mod
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, load_engine,
                                        resolve_config)
from tools.oracle.extract_contract import DEFAULT_OUT as CONTRACT_PATH

STAGE = "S6"
#: Sections carried at the DETECTION timeframe. S2/S3/S4 are computed by the
#: production replay in `replay_structure`, which is self-checked against
#: `detect_order_blocks` — see that module.
SECTIONS_IMPLEMENTED = ["time", "session", "utc_day", "data_context",
                        "volatility", "swings", "structure", "order_blocks",
                        "regime"]
#: `market_state` stays UNIMPLEMENTED even though S6 computes the daily
#: panel: that section name belongs to the PER-TRADE market-state attachment
#: (S8), which reads the panel but is not the panel.
SECTIONS_UNIMPLEMENTED = ["market_state", "candidates", "cohorts",
                          "lifecycle"]
#: At the EXECUTION timeframe only S1 applies: production computes structure on
#: the resampled detection frame, never on 1m. Emitting s2/s3/s4 for a 1m trace
#: would be inventing values production never had.
SECTIONS_S1_ONLY = ["time", "session", "utc_day", "data_context"]
STAGES = ("S1", "S2", "S3", "S4")

#: A session window is [start_hour, end_hour) on the UTC hour, so a bar may only
#: be assigned one session if it cannot span an hour boundary. Measured: M1/M5/
#: M15/H1 never straddle; H4 straddles 4 bars a day. Timeframes are therefore
#: supported only when they divide 60 evenly.
TIMEFRAME_MINUTES = {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "60min": 60,
                     "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60}


class TraceError(RuntimeError):
    pass


def _tf_minutes(tf: str) -> int:
    if tf not in TIMEFRAME_MINUTES:
        raise TraceError(
            f"unsupported timeframe {tf!r} (known: {sorted(TIMEFRAME_MINUTES)})")
    m = TIMEFRAME_MINUTES[tf]
    if 60 % m != 0:
        raise TraceError(
            f"timeframe {tf!r} ({m}m) does not divide 60, so a bar can span a "
            "session boundary and session assignment would be ambiguous")
    return m


def _canonical_tf(tf: str) -> str:
    """Normalise a chart-style timeframe to the production pandas rule."""
    return {"M1": "1min", "M5": "5min", "M15": "15min", "M30": "30min",
            "H1": "60min"}.get(tf, tf)


def _validate_context(contract: dict, symbol: str, timeframe: str) -> tuple[str, list[str]]:
    reasons: list[str] = []
    cfg = contract["configuration"]["pine_relevant"]["data_context"]
    if symbol != cfg["symbol"]:
        reasons.append(f"symbol {symbol!r} != production {cfg['symbol']!r}")
    canon = _canonical_tf(timeframe)
    supported = {cfg["detection_timeframe"], cfg["execution_timeframe"]}
    if canon not in supported:
        reasons.append(
            f"timeframe {timeframe!r} ({canon}) is neither the detection "
            f"({cfg['detection_timeframe']}) nor the execution "
            f"({cfg['execution_timeframe']}) timeframe")
    return ("SUPPORTED" if not reasons else "UNSUPPORTED_DATA_CONTEXT"), reasons


def _load_candles(pd, path: Path):
    if not path.is_file():
        raise TraceError(f"input candle file not found: {path}")
    df = pd.read_csv(path)
    required = {"time", "open", "high", "low", "close"}
    missing = required - set(df.columns)
    if missing:
        raise TraceError(f"{path.name} missing required columns: {sorted(missing)}")
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df


def _integrity(pd, raw_df) -> dict:
    """Report what production WOULD silently accept, before it does.

    `prepare_candles_for_simulation` sorts but does NOT de-duplicate (verified),
    so duplicates survive into the walk. Out-of-order input is silently reordered.
    Both are reported here so a fixture can assert the behaviour rather than
    discover it.
    """
    t = pd.to_datetime(raw_df["time"])
    dup_mask = t.duplicated(keep=False)
    return {
        "input_rows": int(len(raw_df)),
        "duplicate_timestamps": int(dup_mask.sum()),
        "duplicate_examples": [str(x) for x in t[dup_mask].unique()[:5]],
        "out_of_order_rows": int((t.diff().dt.total_seconds() < 0).sum()),
        "reordered_by_production": bool((t.diff().dt.total_seconds() < 0).any()),
        "note": ("production sorts (stable) but does NOT de-duplicate — "
                 "strategy_core.execution.prepare_candles_for_simulation"),
    }


def _s6_for_bar(regime_rows, stamp):
    """The S6 section for one intraday bar.

    Every bar of a UTC day carries the SAME panel row — the state was fixed when
    the day opened and does not move intraday. That is what makes a per-bar
    export comparable against a per-day Python panel without either side having
    to know about the other's granularity.
    """
    day = str(stamp)[:10]
    r = regime_rows.get(day)
    if r is None:
        return {"warmup": True, "day_key": day, "replay_unattainable": True}
    return {
        "warmup": bool(r["warmup"]) or r.get("market_state") is None,
        "day_key": day,
        "source_day": r.get("source_day"),
        "day_index": r["day_index"],
        # The SOURCE day's aggregate — the completed day the state was
        # classified from, which is the same bar Pine's `s6_dOpen` et al. hold.
        "daily_open": r.get("daily_open"), "daily_high": r.get("daily_high"),
        "daily_low": r.get("daily_low"), "daily_close": r.get("daily_close"),
        "daily_bars": r.get("daily_bars"),
        "ema": r.get("ema"), "px_vs_ema": r.get("px_vs_ema"),
        "bb_basis": r.get("bb_basis"), "bb_sd": r.get("bb_sd"),
        "bb_upper": r.get("bb_upper"), "bb_lower": r.get("bb_lower"),
        "bbw": r.get("bbw"), "bbw_threshold": r.get("bbw_threshold"),
        "bbw_above": r.get("bbw_above"),
        "tr": r.get("tr"), "plus_dm": r.get("plus_dm"),
        "minus_dm": r.get("minus_dm"), "atr": r.get("atr"),
        "plus_dm_smooth": r.get("plus_dm_smooth"),
        "minus_dm_smooth": r.get("minus_dm_smooth"),
        "plus_di": r.get("plus_di"), "minus_di": r.get("minus_di"),
        "dx": r.get("dx"), "adx": r.get("adx"), "adx_chop": r.get("adx_chop"),
        "adx_is_chop": r.get("adx_is_chop"),
        "market_state": r.get("market_state"),
        "prev_market_state": r.get("prev_market_state"),
        "trend_state": r.get("trend_state"),
        "volatility_state": r.get("volatility_state"),
        "chop_state": r.get("chop_state"),
        "transition": r.get("transition"),
        "transition_reason": r.get("transition_reason"),
        "validity": ("valid" if r.get("market_state") else
                     "warmup" if r.get("warmup") or r.get("market_state") is None
                     else "invalid"),
        "confirmed": r.get("confirmed"),
        # The chart can never seed EMA(200) the way production does — measured:
        # 226 chart days vs 3,586, and the EMA does not converge.
        "replay_unattainable": True,
    }


def build_trace(*, symbol: str, timeframe: str, input_path: Path,
                contract_path: Path = CONTRACT_PATH,
                fixture_id: str | None = None,
                require_pin: bool = True,
                allow_unsupported: bool = False) -> dict:
    if not contract_path.is_file():
        raise TraceError(
            f"no parity contract at {contract_path} — run "
            "`python -m tools.oracle.extract_contract --write` first")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))

    engine = load_engine(require_pin=require_pin)
    config, _ = resolve_config(engine)

    # The contract must describe THIS engine, or the trace would be labelled with
    # a fingerprint it does not actually represent.
    live_fp = fp.from_engine(engine, config, _policy_table(engine, config))
    if live_fp["oracle_engine_hash"] != contract["fingerprint"]["oracle_engine_hash"]:
        raise TraceError(
            "contract fingerprint does not match the deployed engine "
            f"(contract {contract['fingerprint']['oracle_engine_hash'][:16]}…, "
            f"live {live_fp['oracle_engine_hash'][:16]}…) — regenerate the contract")

    status, reasons = _validate_context(contract, symbol, timeframe)
    if status != "SUPPORTED" and not allow_unsupported:
        raise TraceError("unsupported data context: " + "; ".join(reasons))

    import pandas as pd
    minutes = _tf_minutes(timeframe)
    canon_tf = _canonical_tf(timeframe)

    raw = _load_candles(pd, input_path)
    integrity = _integrity(pd, raw)

    core, rb = engine.core, engine.rb
    from strategy_core.regime import utc_date_key
    # Production's OWN Europe/London converter. Imported by name rather than
    # reached through `core`, which does not re-export it.
    from strategy_core.sessions import _london_hour

    # EXACT production order: normalise (sort + stamp _session) THEN resample.
    prepared = core.prepare_candles_for_simulation(raw)
    if canon_tf == contract["configuration"]["pine_relevant"]["data_context"]["execution_timeframe"]:
        bars = prepared                      # already at the execution timeframe
        resampled = False
    else:
        bars = rb.resample_candles(prepared, canon_tf)
        resampled = True

    sess_codes = codes_mod.session_codes(contract)
    trans_codes = codes_mod.transition_codes()
    win_by_key = {w["key"]: w for w in contract["sessions"]["windows"]}
    fallback_key = contract["sessions"]["fallback"]["key"]

    # ── S2/S3/S4, only at the detection timeframe ────────────────────────────
    structure_cfg = contract["configuration"]["pine_relevant"]["structure"]
    is_detection = canon_tf == contract["configuration"]["pine_relevant"][
        "data_context"]["detection_timeframe"]
    struct_rows = None
    order_blocks = []
    if is_detection:
        # S5's size gate needs the pip size and the configured bounds; they come
        # from the SAME resolved config the live runner passes to
        # `detect_order_blocks`, never from a default here.
        # The size gate's bounds live under `structure`; the pip size under
        # `data_context`. Both are read from the contract, never defaulted here —
        # a default would silently diverge the moment production retuned one.
        dctx = contract["configuration"]["pine_relevant"]["data_context"]
        struct_rows, order_blocks = replay_structure.replay(
            core,
            bars["high"].tolist(), bars["low"].tolist(), bars["close"].tolist(),
            swing_length=int(structure_cfg["swing_length"]),
            ob_filter=str(structure_cfg["ob_filter"]),
            pip_size=float(dctx["pip_size"]),
            min_ob_size_pips=structure_cfg["min_ob_size_pips"],
            max_ob_size_pips=structure_cfg["max_ob_size_pips"])
        assert len(struct_rows) == len(bars), "replay/bar length mismatch"

    # ── S6, at the detection timeframe only ──────────────────────────────────
    # Production builds the panel from the 1m frame; the daily aggregate of the
    # 15m frame is identical (max of maxes, min of mins, last of lasts, and the
    # first 15m open IS the first 1m open of that day), which is asserted by the
    # S6 tests rather than assumed here.
    regime_rows = None
    if is_detection:
        reg = contract["configuration"]["pine_relevant"]["regime"]
        reg_cfg = {
            "emaLength": reg["regime_ema_length"],
            "bbwLength": reg["regime_bbw_length"],
            "bbwStdDev": reg["regime_bbw_std"],
            "adxLength": reg["regime_adx_length"],
            "adxChop": reg["regime_adx_chop"],
            "emaConfirmDays": reg["regime_ema_confirm"],
            "bbwThresholdMode": reg["regime_bbw_thr_mode"],
            "bbwThresholdValue": reg["regime_bbw_thr_value"],
            "bbwPercentile": reg["regime_bbw_pctile"],
        }
        stamps = [str(t) for t in bars["time"]]
        _daily, panel = replay_regime.replay(
            core, stamps, bars["open"].tolist(), bars["high"].tolist(),
            bars["low"].tolist(), bars["close"].tolist(), reg_cfg,
            str(contract["configuration"]["pine_relevant"]["data_context"]["symbol"]))
        regime_rows = {r["date"]: r for r in panel}

    rows = []
    prev_key = None
    prev_day = None
    prev_ts = None
    since_transition = 0
    step = pd.Timedelta(minutes=minutes)

    for i, rec in enumerate(bars.to_dict("records")):
        ts = pd.Timestamp(rec["time"])
        # LONDON wall-clock hour, through production's own converter. This read
        # `core._session_for_hour(ts.hour)` — the UTC hour — which silently
        # bypassed `_london_hour` after M-SESSION-DST-1 and would have scored a
        # UTC-classifying chart as CORRECT for the seven months a year the two
        # answers differ. The reference must go through the deployed function,
        # not re-derive the same shape from a different input.
        label, key = core._session_for_hour(_london_hour(ts))
        day_key = utc_date_key(ts)

        gap = prev_ts is not None and (ts - prev_ts) > step
        if prev_key is None:
            transition, reason = True, "first_bar"
        elif key != prev_key:
            transition = True
            if gap:
                reason = "gap_resync"
            elif key == fallback_key:
                reason = "session_close_to_fallback"
            elif prev_key == fallback_key:
                reason = "fallback_to_session"
            else:
                reason = "session_open"
        else:
            transition, reason = False, None

        since_transition = 0 if transition else since_transition + 1
        win = win_by_key.get(key)

        row_extra = {}
        if struct_rows is not None:
            sr = struct_rows[i]
            row_extra = {"s2": sr["s2"], "s3": sr["s3"], "s4": sr["s4"],
                         "s5": sr["s5"]}
            # `obs_created` is the schema's long-standing S5 slot; it carries the
            # APPENDED blocks only, in production append order, while `s5`
            # carries every candidate including the rejected ones.
            created = [c for c in (sr["s5"].get("candidates") or [])
                       if c["appended"]]
            if regime_rows is not None:
                row_extra["s6"] = _s6_for_bar(regime_rows, str(rec["time"]))
            if created:
                row_extra["obs_created"] = [{
                    "ob_id": c["ob_id"], "direction": c["side"],
                    "structure_tag": c["tag"],
                    "top": c["proposed_top"], "bottom": c["proposed_bottom"],
                    "break_level": c["break_level"],
                    "width_pips": c["proposed_width_pips"],
                    "origin_index": c["origin_index"],
                    "pivot_index": c["pivot_index"],
                    "detection_index": c["break_index"],
                } for c in created]

        rows.append({
            **row_extra,
            "bar_timestamp": ts.strftime("%Y-%m-%d %H:%M:%S+00:00"),
            "bar_epoch_ms": int(ts.value // 1_000_000),
            "bar_index": i,
            "bar_completeness": "closed",
            "ohlcv": {
                "open": float(rec["open"]), "high": float(rec["high"]),
                "low": float(rec["low"]), "close": float(rec["close"]),
                "volume": float(rec.get("volume", 0.0) or 0.0),
            },
            "utc_day": {
                "day_key": day_key,
                "prev_day_key": prev_day,
                "day_transition": prev_day is None or day_key != prev_day,
                "utc_year": int(ts.year), "utc_month": int(ts.month),
                "utc_dom": int(ts.day),
            },
            "data_context": {"status": status, "reasons": reasons},
            "session": {
                "key": key,
                "label": label,
                "code": codes_mod.code_for(key, sess_codes),
                "utc_hour": int(ts.hour),
                "window_start_hour": win["start_hour"] if win else None,
                "window_end_hour": win["end_hour"] if win else None,
                "is_fallback": key == fallback_key,
                "prev_key": prev_key,
                "transition": transition,
                "transition_reason": reason,
                "transition_code": codes_mod.code_for(reason, trans_codes),
                "bars_since_transition": since_transition,
            },
        })
        prev_key, prev_day, prev_ts = key, day_key, ts

    implemented = list(SECTIONS_IMPLEMENTED if is_detection else SECTIONS_S1_ONLY)
    unimplemented = sorted(set(SECTIONS_IMPLEMENTED + SECTIONS_UNIMPLEMENTED)
                           - set(implemented))
    header = {
        "trace_schema_version": fp.TRACE_SCHEMA_VERSION,
        "stage": STAGE if is_detection else "S1",
        "sections_implemented": implemented,
        "sections_unimplemented": unimplemented,
        "structure_config": {
            "swing_length": int(structure_cfg["swing_length"]),
            "ob_filter": str(structure_cfg["ob_filter"]),
            "atr_period": replay_structure.ATR_PERIOD,
            "volatility_multiple": replay_structure.VOLATILITY_MULTIPLE,
            "_note": ("S2/S3/S4 are emitted only at the detection timeframe. "
                      "Production computes structure on the resampled detection "
                      "frame, never on 1m, so a 1m trace carries S1 only."),
        },
        "oracle_engine_hash": live_fp["oracle_engine_hash"],
        "oracle_engine_id": live_fp["oracle_engine_id"],
        "engine_manifest_id": engine.engine_manifest_id,
        "resolved_config_hash": live_fp["inputs"]["resolved_config_hash"],
        "policy_version": live_fp["inputs"]["policy_version"],
        "symbol": symbol,
        "detection_timeframe": canon_tf,
        "execution_timeframe": contract["configuration"]["pine_relevant"][
            "data_context"]["execution_timeframe"],
        "window": {
            "start": rows[0]["bar_timestamp"] if rows else None,
            "end": rows[-1]["bar_timestamp"] if rows else None,
            "fixture_id": fixture_id or input_path.stem,
        },
        "environment_provenance": engine.environment,
        "warmup_excluded_bars": {"swings": 0, "atr": 0, "regime": 0},
        "source": {
            "input_file": input_path.name,
            "input_sha256": hashlib.sha256(
                input_path.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
            "resampled": resampled,
            "timeframe_minutes": minutes,
            "integrity": integrity,
            "normalisation_applied": [
                "pd.to_datetime(time)",
                "sort_values('time') — STABLE, duplicates preserved",
                "_session stamped via strategy_core.sessions._utc_session",
            ] + (["resample_candles(...) — empty intervals DROPPED"] if resampled else []),
        },
        "codes": {
            "session": dict(sorted(sess_codes.items())),
            "transition_reason": dict(sorted(trans_codes.items())),
            "data_context": dict(sorted(codes_mod.data_context_codes().items())),
            "_note": "CODE_UNKNOWN = -1 is emitted for any value with no mapping; "
                     "it is a visible failure, never a default.",
        },
    }
    return {"header": header, "bars": rows}


def _policy_table(engine, config):
    from tools.oracle.engine_access import effective_policy_table
    table, _ = effective_policy_table(engine, config)
    return table


def canonical_json(trace: dict) -> str:
    return json.dumps(trace, sort_keys=True, indent=1, ensure_ascii=True) + "\n"


def content_hash(trace: dict) -> str:
    return hashlib.sha256(canonical_json(trace).encode("utf-8")).hexdigest()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stage", default=STAGE, choices=list(STAGES),
                    help="highest stage to label the trace with (informational; "
                         "the sections actually emitted depend on the timeframe)")
    ap.add_argument("--symbol", default="EURUSD")
    ap.add_argument("--timeframe", default="15min")
    ap.add_argument("--input", type=Path, required=True)
    ap.add_argument("--output", type=Path)
    ap.add_argument("--fixture-id")
    ap.add_argument("--allow-unsupported", action="store_true",
                    help="emit the trace anyway, flagged UNSUPPORTED_DATA_CONTEXT "
                         "(for the unsupported-context fixtures)")
    args = ap.parse_args(argv)

    try:
        trace = build_trace(symbol=args.symbol, timeframe=args.timeframe,
                            input_path=args.input, fixture_id=args.fixture_id,
                            allow_unsupported=args.allow_unsupported)
    except (TraceError, EngineAccessError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    h = trace["header"]
    digest = content_hash(trace)
    print(f"stage        : {h['stage']}  (sections: {', '.join(h['sections_implemented'])})")
    print(f"fingerprint  : {h['oracle_engine_id']}")
    print(f"symbol/tf    : {h['symbol']} / {h['detection_timeframe']}")
    print(f"bars         : {len(trace['bars'])}  "
          f"[{h['window']['start']} .. {h['window']['end']}]")
    print(f"transitions  : {sum(1 for b in trace['bars'] if b['session']['transition'])}")
    print(f"day rollovers: {sum(1 for b in trace['bars'] if b['utc_day']['day_transition'])}")
    integ = h["source"]["integrity"]
    if integ["duplicate_timestamps"] or integ["out_of_order_rows"]:
        print(f"integrity    : {integ['duplicate_timestamps']} duplicate ts, "
              f"{integ['out_of_order_rows']} out-of-order rows "
              "(production sorts, does NOT de-duplicate)")
    print(f"content_hash : {digest}")

    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(canonical_json(trace), encoding="utf-8")
        try:
            shown = args.output.relative_to(CT_ROOT)
        except ValueError:
            shown = args.output
        print(f"\nwritten: {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
