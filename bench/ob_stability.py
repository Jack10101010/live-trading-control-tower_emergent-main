"""M-INCREMENTAL-1 Phase 2 — is order-block detection append-only?

THE GATE FOR THE WHOLE MILESTONE. The bar loop consumes order blocks through a
monotonically advancing `ob_cursor`:

    while ob_cursor < ob_count and ob_detection_times[ob_cursor] <= candle["time"]:

A checkpoint stores that cursor. Resuming is only sound if, when later bars
arrive, detection never inserts an order block BEFORE the stored cursor -- i.e.
if the OB sequence for a longer history is a strict prefix-extension of the OB
sequence for a shorter one. If detection can revise or insert history behind the
frontier, a stored cursor silently skips those blocks and the incremental engine
diverges from the oracle.

Order blocks come from swings, and a swing needs `swing_length` bars on each
side, so blocks are expected to appear only within a bounded window behind the
frontier. This measures that window instead of assuming it.

Method: detect over M15 history truncated at successively earlier cut points,
then check each shorter sequence is a prefix of the longest one, and report how
far behind the frontier the newest block sits.

Read-only. No MT5, no production state.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out"
OUT.mkdir(parents=True, exist_ok=True)

KEY_COLS = ["detection_time", "origin_time", "direction", "top", "bottom"]


def ob_key(row) -> str:
    return "|".join(str(row[c]) for c in KEY_COLS)


def main() -> int:
    sys.path.insert(0, str(REPO))
    from live.config import LiveConfig
    from live.runner import LuxSession, assemble_candles, latest_closed_boundary
    import pandas as pd

    cfg = LiveConfig()
    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    session = LuxSession(lux_root)
    rb, core = session.rb, session.core

    frozen = lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
    raw = assemble_candles(frozen, cfg.live_segment_csv)
    boundary = latest_closed_boundary(pd.to_datetime(raw["time"]).max())
    config = session.golden_config(cfg.golden_config_path, end_date=str(boundary.date()))
    print(f"swing_length      : {config.swing_length}")
    print(f"detection_timeframe: {config.detection_timeframe}")

    candles = core.prepare_candles_for_simulation(rb.filter_date_range(raw, config))
    detection_full = rb.resample_candles(candles, config.detection_timeframe)
    print(f"detection bars    : {len(detection_full):,}")

    # Truncate the M15 detection frame by N bars and re-detect.
    results = []
    for cut in (0, 1, 2, 4, 8, 16):
        det = detection_full.iloc[: len(detection_full) - cut] if cut else detection_full
        t0 = time.perf_counter()
        obs = core.detect_order_blocks(
            det, swing_length=config.swing_length, ob_filter=config.ob_filter,
            pip_size=config.pip_size, min_ob_size_pips=config.min_ob_size_pips,
            max_ob_size_pips=config.max_ob_size_pips)
        wall = time.perf_counter() - t0
        keys = [ob_key(r) for _, r in obs.iterrows()]
        last_bar = det["time"].iloc[-1]
        newest_det = obs["detection_time"].iloc[-1] if len(obs) else None
        results.append({"cut_bars": cut, "det_bars": int(len(det)), "obs": len(keys),
                        "keys": keys, "wall_s": round(wall, 1),
                        "last_detection_bar": str(last_bar), "newest_ob_detection": str(newest_det)})
        print(f"cut={cut:2d} bars -> det_bars={len(det):,} obs={len(keys):,} "
              f"({wall:.1f}s) last_bar={last_bar} newest_ob={newest_det}")

    base = results[0]["keys"]                      # longest history
    print("\n=== PREFIX TEST (shorter history must be a PREFIX of longer) ===")
    verdict_ok = True
    for r in results[1:]:
        k = r["keys"]
        is_prefix = base[: len(k)] == k
        # where does it first disagree?
        first_diff = next((i for i in range(min(len(base), len(k))) if base[i] != k[i]), None)
        lag = len(base) - len(k)
        print(f"cut={r['cut_bars']:2d}: obs={len(k):,} prefix_of_full={is_prefix} "
              f"new_obs_added={lag} first_divergence_index={first_diff}")
        verdict_ok = verdict_ok and is_prefix
        r["is_prefix"] = is_prefix
        r["first_divergence_index"] = first_diff
        r["new_obs_added"] = lag

    print("\n=== VERDICT ===")
    print("APPEND-ONLY (cursor-safe):", verdict_ok)
    if verdict_ok:
        maxlag = max(r["new_obs_added"] for r in results[1:])
        print(f"max order blocks appearing within 16 new M15 bars: {maxlag}")
        print("=> a checkpoint cursor stays valid; new blocks only ever APPEND")
    else:
        print("=> detection REVISES history: a stored ob_cursor is NOT safe as-is")

    payload = [{k: v for k, v in r.items() if k != "keys"} for r in results]
    (OUT / "ob_stability.json").write_text(json.dumps(
        {"swing_length": config.swing_length, "append_only": verdict_ok, "runs": payload}, indent=1))
    print(f"\nwritten: {OUT / 'ob_stability.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
