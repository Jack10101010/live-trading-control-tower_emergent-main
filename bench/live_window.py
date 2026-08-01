"""M-LIVE-WINDOW-1 — does a bounded live window reproduce the 11-year oracle?

The 11-year dataset exists to evaluate the strategy across a large historical
sample. Replaying all of it every fifteen minutes is an inherited backtester
habit, not a stated live requirement. This harness measures what the live
decision actually depends on.

Mechanism: the pipeline already bounds history through `config.start_date`
(`filter_date_range`), so a candidate window is a config override and needs NO
change to governed strategy code. `end_date` stays at the frontier exactly as
production sets it.

Each run dumps the trades frame plus stage timings so equivalence and cost can be
compared per window. Read-only: no MT5, no Control Tower, no production state.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out" / "windows"
OUT.mkdir(parents=True, exist_ok=True)

WINDOWS = {"3mo": 91, "6mo": 182, "1y": 365, "2y": 730, "3y": 1095, "5y": 1826, "full": None}
TIMINGS: list[dict] = []


def _wrap(owner, name, tag=None):
    original = getattr(owner, name)
    label = tag or name

    def timed(*a, **k):
        t0, c0 = time.perf_counter(), time.process_time()
        try:
            return original(*a, **k)
        finally:
            TIMINGS.append({"stage": label, "wall_s": round(time.perf_counter() - t0, 3),
                            "cpu_s": round(time.process_time() - c0, 3)})
    setattr(owner, name, timed)
    return original


def peak_rss_mb() -> float:
    import ctypes
    import ctypes.wintypes as wt

    class PMC(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
    pmc = PMC()
    pmc.cb = ctypes.sizeof(PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    try:
        lib = ctypes.WinDLL("kernel32.dll")
        lib.K32GetProcessMemoryInfo.restype = ctypes.c_bool
        if lib.K32GetProcessMemoryInfo(ctypes.c_void_p(h), ctypes.byref(pmc), ctypes.c_uint(pmc.cb)):
            return round(pmc.PeakWorkingSetSize / 1024 / 1024, 1)
    except OSError:
        pass
    return float("nan")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--window", required=True, choices=sorted(WINDOWS))
    ap.add_argument("--boundary", default=None,
                    help="ISO date to treat as the frontier; default = newest closed boundary")
    args = ap.parse_args()

    sys.path.insert(0, str(REPO))
    from live.config import LiveConfig
    from live.runner import LiveRunner, LuxSession, assemble_candles, latest_closed_boundary
    import pandas as pd

    cfg = LiveConfig()
    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    session = LuxSession(lux_root)
    rb, core = session.rb, session.core
    frozen = lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"

    raw = assemble_candles(frozen, cfg.live_segment_csv)
    boundary = (pd.Timestamp(args.boundary) if args.boundary
                else latest_closed_boundary(pd.to_datetime(raw["time"]).max()))
    frontier_date = str(boundary.date())

    base = session.golden_config(cfg.golden_config_path, end_date=frontier_date)
    days = WINDOWS[args.window]
    if days is None:
        config, start_date = base, base.start_date
    else:
        start_date = str((boundary - pd.Timedelta(days=days)).date())
        config = replace(base, start_date=start_date)

    print(f"window     : {args.window} ({'full oracle' if days is None else str(days) + 'd'})")
    print(f"start_date : {start_date}   end_date: {config.end_date}")
    print(f"boundary   : {boundary}")

    # Re-run the pipeline stages exactly as golden_pipeline does, with the
    # windowed config. Stages are wrapped for timing only.
    for owner, name in [(rb, "filter_date_range"), (core, "prepare_candles_for_simulation"),
                        (rb, "load_news_calendar_events"), (rb, "load_news_events"),
                        (session, "prepare_news_cache"), (rb, "resample_candles"),
                        (core, "detect_order_blocks"), (rb, "tag_order_blocks_with_news"),
                        (rb, "filter_order_blocks_by_structure"),
                        (rb, "filter_order_blocks_by_structure_direction"),
                        (core, "prepare_order_blocks_for_simulation"),
                        (rb, "execute_scenario_job")]:
        _wrap(owner, name, "execute_scenario_job(WALK)" if name == "execute_scenario_job" else None)

    t0 = time.perf_counter()
    candles = rb.filter_date_range(raw, config)
    candles = core.prepare_candles_for_simulation(candles)
    calendar_events = rb.load_news_calendar_events(config)
    news_events = rb.load_news_events(config)
    news_cache = session.prepare_news_cache(news_events, candles,
                                            config.news_flatten_minutes_before_blackout)
    detection = rb.resample_candles(candles, config.detection_timeframe)
    obs = core.detect_order_blocks(detection, swing_length=config.swing_length,
                                   ob_filter=config.ob_filter, pip_size=config.pip_size,
                                   min_ob_size_pips=config.min_ob_size_pips,
                                   max_ob_size_pips=config.max_ob_size_pips)
    obs = rb.tag_order_blocks_with_news(obs, calendar_events)
    obs = rb.filter_order_blocks_by_structure(obs, config.structure_filter)
    obs, _ = rb.filter_order_blocks_by_structure_direction(obs, config.allowed_structure_directions)
    sim_obs = core.prepare_order_blocks_for_simulation(obs)

    entry = [x for x in rb.entry_scenarios(config) if x["mode"] == "triggered_edge"]
    mode = config.execution_modes[0]
    job = {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
           "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}
    out = rb.execute_scenario_job(job, config, candles, sim_obs, obs, news_cache)
    trades = out["results"][0]["trades"]
    total = time.perf_counter() - t0

    tag = args.window
    trades_csv = OUT / f"trades-{tag}.csv"
    body = trades.astype(str).to_csv(index=False).encode()
    trades_csv.write_bytes(body)
    obs_csv = OUT / f"obs-{tag}.csv"
    obs.astype(str).to_csv(obs_csv, index=False)

    rec = {"window": tag, "days": days, "start_date": start_date, "end_date": config.end_date,
           "boundary": str(boundary), "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "total_s": round(total, 2), "peak_rss_mb": peak_rss_mb(),
           "m1_rows": int(len(candles)), "detection_bars": int(len(detection)),
           "order_blocks": int(len(obs)), "trades": int(len(trades)),
           "trades_sha256": hashlib.sha256(body).hexdigest(),
           "stages": sorted(TIMINGS, key=lambda x: -x["wall_s"])}
    (OUT / f"summary-{tag}.json").write_text(json.dumps(rec, indent=1))

    print(f"\nm1 rows {rec['m1_rows']:,}  detection {rec['detection_bars']:,} "
          f"obs {rec['order_blocks']:,}  trades {rec['trades']:,}")
    print(f"total {rec['total_s']:.1f}s   peak RSS {rec['peak_rss_mb']} MB")
    print(f"trades sha256 {rec['trades_sha256'][:16]}...")
    for s in rec["stages"][:6]:
        print(f"  {s['wall_s']:8.2f}s  {s['stage']}")
    print(f"written: {trades_csv.name}, {obs_csv.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
