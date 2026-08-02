"""M-LIVE-STATE-4 — execute a real one-boundary Model B advance and compare.

Procedure:
  1. Full-history oracle walk ONCE, snapshotting SimState at the last M1 candle
     of the previous boundary and collecting the oracle trade frame.
  2. Build the two-year window context ending at the SAME next boundary.
  3. Rebase the snapshot into the window frame via the approved registry.
  4. Drive the REAL apply_bar over only the new bars.
  5. finalize() and compare emitted trades field-by-field with the oracle.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bench.model_b import (Captured, build_session, frame_origin_for,  # noqa: E402
                           ordinal_map, restore_state, run_pipeline,
                           state_to_absolute, summarise)

OUT = REPO / "bench" / "out" / "modelb"
OUT.mkdir(parents=True, exist_ok=True)
WINDOW_DAYS = 730
SEMANTIC = ["trade_id", "direction", "detection_time", "fill_time", "exit_time",
            "outcome", "entry", "stop", "tp", "net_r", "rr_multiple",
            "bars_to_fill", "bars_to_exit", "structure_tag", "fill_session"]


def main() -> int:
    import pandas as pd
    from live.config import LiveConfig
    from live.runner import assemble_candles, latest_closed_boundary

    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    session, execution = build_session(lux_root)
    cfg = LiveConfig()
    frozen = lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
    raw = assemble_candles(frozen, cfg.live_segment_csv)

    next_boundary = latest_closed_boundary(pd.to_datetime(raw["time"]).max())
    prev_boundary = next_boundary - pd.Timedelta(minutes=15)
    # SimState must be snapshotted at the LAST M1 candle strictly before the new
    # boundary's bars, i.e. the candle at prev_boundary + 14 minutes.
    snap_time = str(prev_boundary + pd.Timedelta(minutes=14))
    print(f"next boundary : {next_boundary}")
    print(f"prev boundary : {prev_boundary}")
    print(f"snapshot at   : {snap_time}")

    # ---- 1. oracle (cached: the walk costs ~950s and is deterministic) ----
    import pickle
    cache = OUT / f"oracle-{next_boundary.date()}-{snap_time.replace(':', '')}.pkl"
    t0 = time.perf_counter()
    cap_o = Captured()
    if cache.exists() and os.environ.get("MODELB_NO_CACHE") != "1":
        with cache.open("rb") as fh:
            payload = pickle.load(fh)
        cap_o.snapshots = payload["snapshots"]
        cap_o.trades = payload["trades"]
        cap_o.walk_seconds = payload["walk_seconds"]
        cap_o.ctx = {"candle_records": payload["candle_records"]}
        oracle_s = time.perf_counter() - t0
        print(f"(oracle restored from cache in {oracle_s:.1f}s)")
    else:
        run_pipeline(session, cfg, raw, end_date=str(next_boundary.date()), start_date=None,
                     cap=cap_o, execution=execution, snapshot_times={snap_time}, walk=True)
        oracle_s = time.perf_counter() - t0
        with cache.open("wb") as fh:
            pickle.dump({"snapshots": cap_o.snapshots, "trades": cap_o.trades,
                         "walk_seconds": cap_o.walk_seconds,
                         "candle_records": cap_o.ctx["candle_records"]}, fh)
    if snap_time not in cap_o.snapshots:
        print(f"FAIL: snapshot time {snap_time} not present in the oracle frame")
        return 1
    absolute_of = ordinal_map(cap_o.ctx)
    full_origin = frame_origin_for(cap_o.ctx, absolute_of)
    snap = cap_o.snapshots[snap_time]
    print(f"\noracle walk {oracle_s:.1f}s (walk {cap_o.walk_seconds:.1f}s) "
          f"trades={len(cap_o.trades):,}")
    print(f"snapshot state: {summarise(snap, full_origin, execution)}")

    t0 = time.perf_counter()
    absolute_state = state_to_absolute(snap, full_origin, execution)
    to_abs_s = time.perf_counter() - t0

    # ---- 2. two-year window context -------------------------------------
    t0 = time.perf_counter()
    cap_w = Captured()
    start_date = str((next_boundary - pd.Timedelta(days=WINDOW_DAYS)).date())
    run_pipeline(session, cfg, raw, end_date=str(next_boundary.date()), start_date=start_date,
                 cap=cap_w, execution=execution, walk=False)
    window_s = time.perf_counter() - t0
    wctx = cap_w.ctx
    origin = frame_origin_for(wctx, absolute_of)
    print(f"\nwindow ctx {window_s:.1f}s  bars={len(wctx['candle_records']):,} "
          f"obs={wctx['ob_count']:,} origin_ordinal={origin.origin_ordinal:,}")

    # ---- 3. restore + advance exactly the new bars -----------------------
    t0 = time.perf_counter()
    state = restore_state(absolute_state, origin, wctx, execution)
    restore_s = time.perf_counter() - t0

    snap_abs = absolute_of[snap_time]
    start_local = origin.to_local(snap_abs) + 1
    recs = wctx["candle_records"]
    t0 = time.perf_counter()
    n = 0
    for i in range(start_local, len(recs)):
        execution.apply_bar(state, recs[i], i, wctx)
        n += 1
    advance_s = time.perf_counter() - t0
    model_trades = execution.finalize(state, wctx)
    print(f"restore {restore_s*1000:.1f}ms  advanced {n} bars in {advance_s*1000:.1f}ms")
    print(f"post-advance state: {summarise(state, origin, execution)}")

    # ---- 4. compare emitted trades in the new boundary --------------------
    lo = pd.Timestamp(prev_boundary).tz_localize(None) if pd.Timestamp(prev_boundary).tzinfo \
        else pd.Timestamp(prev_boundary)
    o = cap_o.trades.copy()
    m = model_trades.copy()
    for d in (o, m):
        d["fill_dt"] = pd.to_datetime(d["fill_time"], errors="coerce", utc=True)
    win_lo = pd.Timestamp(next_boundary)
    o_new = o[o["fill_dt"] >= win_lo]
    m_new = m[m["fill_dt"] >= win_lo]
    print(f"\ntrades filled at/after {win_lo}: oracle={len(o_new)} modelB={len(m_new)}")

    cols = [c for c in SEMANTIC if c in o.columns and c in m.columns]
    key = ["trade_id"]
    o_i = o_new.set_index("trade_id")[cols[1:]].astype(str).sort_index()
    m_i = m_new.set_index("trade_id")[cols[1:]].astype(str).sort_index()
    same_ids = list(o_i.index) == list(m_i.index)
    print(f"trade_id sets identical: {same_ids}")
    verdict = "PASS"
    if not same_ids:
        verdict = "FAIL"
        print(f"  oracle ids: {list(o_i.index)[:10]}")
        print(f"  modelB ids: {list(m_i.index)[:10]}")
    else:
        for tid in o_i.index:
            for c in cols[1:]:
                if o_i.at[tid, c] != m_i.at[tid, c]:
                    verdict = "FAIL"
                    print(f"  FIRST DIVERGENCE  trade={tid} field={c}")
                    print(f"    oracle : {o_i.at[tid, c]!r}")
                    print(f"    modelB : {m_i.at[tid, c]!r}")
                    break
            if verdict == "FAIL":
                break

    print(f"\nONE-BOUNDARY GATE: {verdict}")
    rec = {"next_boundary": str(next_boundary), "prev_boundary": str(prev_boundary),
           "oracle_total_s": round(oracle_s, 1), "oracle_walk_s": round(cap_o.walk_seconds, 1),
           "window_ctx_s": round(window_s, 1), "to_absolute_ms": round(to_abs_s * 1000, 2),
           "restore_ms": round(restore_s * 1000, 2), "advance_ms": round(advance_s * 1000, 2),
           "bars_advanced": n, "window_bars": len(recs), "window_obs": int(wctx["ob_count"]),
           "oracle_new_trades": int(len(o_new)), "modelb_new_trades": int(len(m_new)),
           "state": summarise(state, origin, execution), "verdict": verdict}
    (OUT / "one_boundary.json").write_text(json.dumps(rec, indent=1))
    print(f"written: {OUT / 'one_boundary.json'}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
