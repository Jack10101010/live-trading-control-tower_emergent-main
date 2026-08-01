"""M-CAPACITY-1 — offline, instrumented recompute benchmark.

Runs the REAL `LiveRunner.golden_pipeline` against copied read-only fixtures, with
every driver/core stage wrapped in a timer. The pipeline itself is NOT
re-implemented here: the stages are monkey-patched with pass-through timing
wrappers before `golden_pipeline` is called, so what is measured is exactly what
production executes. Re-implementing the stage sequence would have measured a
copy that could drift from the original.

Isolation: no MT5, no Control Tower, no Task Scheduler, no order path, no
production state. `LUX_ROOT` points at the capacity worktree and `LIVE_STATE_DIR`
at a scratch directory; nothing here writes outside `bench_state/` and `bench/out/`.

Usage:
    python -m bench.capacity_bench --label cold
    python -m bench.capacity_bench --label warm --repeat 2
    python -m bench.capacity_bench --stages-only     # skip the walk (fast iteration)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out"
OUT.mkdir(parents=True, exist_ok=True)


# ── memory high-water (Windows PROCESS_MEMORY_COUNTERS) ──────────────────────
class _PMC(ctypes.Structure):
    _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]


def peak_rss_mb() -> float:
    """Peak working set. psapi!GetProcessMemoryInfo silently returned 0 here, so
    prefer the kernel32 K32* export and fall back rather than report a fake 0."""
    pmc = _PMC()
    pmc.cb = ctypes.sizeof(_PMC)
    h = ctypes.windll.kernel32.GetCurrentProcess()
    for libname, fn in (("kernel32", "K32GetProcessMemoryInfo"), ("psapi", "GetProcessMemoryInfo")):
        try:
            lib = ctypes.WinDLL(libname + ".dll")
            func = getattr(lib, fn)
            func.restype = ctypes.c_bool
            if func(ctypes.c_void_p(h), ctypes.byref(pmc), ctypes.c_uint(pmc.cb)) and pmc.PeakWorkingSetSize:
                return pmc.PeakWorkingSetSize / (1024 * 1024)
        except (AttributeError, OSError):
            continue
    return float("nan")


# ── stage timing ─────────────────────────────────────────────────────────────
TIMINGS: list[dict] = []


def _wrap(owner, name: str, label: str | None = None):
    """Replace owner.name with a timing pass-through. Behaviour is unchanged."""
    original = getattr(owner, name)
    tag = label or name

    def timed(*args, **kwargs):
        t0, c0 = time.perf_counter(), time.process_time()
        try:
            return original(*args, **kwargs)
        finally:
            TIMINGS.append({"stage": tag,
                            "wall_s": round(time.perf_counter() - t0, 4),
                            "cpu_s": round(time.process_time() - c0, 4)})
    setattr(owner, name, timed)
    return original


def df_fingerprint(df) -> dict:
    """Content identity of the pipeline output — the behavioural baseline."""
    body = df.astype(str).to_csv(index=False).encode()
    return {"rows": int(len(df)), "cols": list(map(str, df.columns)),
            "sha256": hashlib.sha256(body).hexdigest()}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--stages-only", action="store_true",
                    help="skip execute_scenario_job (the walk) for fast stage iteration")
    ap.add_argument("--ingest", choices=["legacy", "optimised"], default="legacy",
                    help="legacy = read full history then to_datetime().max(); "
                         "optimised = settle the frontier first, materialise only on work")
    ap.add_argument("--allow-unpinned-engine", action="store_true",
                    help="OFFLINE BENCH ONLY: measure a Lux tree whose files no longer "
                         "match the approved manifest. Any engine edit changes "
                         "engine_manifest_id (and engine_version, since "
                         "scripts/run_backtest.py is one of its three files), so a "
                         "candidate optimisation cannot be measured without this. It "
                         "NEVER relaxes the production gate - live.runner still refuses "
                         "to trade on an unverified engine.")
    args = ap.parse_args()

    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    import sys
    sys.path.insert(0, str(REPO))

    from live.config import LiveConfig
    from live.runner import LiveRunner, LuxSession, assemble_candles, latest_closed_boundary
    import pandas as pd

    cfg = LiveConfig()
    frozen_csv = lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
    live_csv = cfg.live_segment_csv
    print(f"lux_root   : {lux_root}")
    print(f"state_dir  : {cfg.state_dir}")
    print(f"frozen csv : {frozen_csv.stat().st_size/1024/1024:.1f} MB")
    print(f"live csv   : {live_csv.stat().st_size/1024/1024:.2f} MB" if live_csv.exists() else "live csv   : absent")

    t_sess = time.perf_counter()
    session = LuxSession(lux_root)
    print(f"LuxSession ctor (import driver+core): {time.perf_counter()-t_sess:.2f}s")
    if args.allow_unpinned_engine:
        print("ENGINE PIN NOT VERIFIED (--allow-unpinned-engine): measuring a modified tree")
    else:
        session.verify_engine()  # proves the worktree is a faithful copy of the pin
    print(f"engine_version     : {session.engine_version}")
    print(f"engine_manifest_id : {session.engine_manifest_id}")

    runner = LiveRunner(cfg, session=session)

    results = []
    frozen_cache: dict = {}     # mirrors LiveRunner._frozen_cache across polls
    for i in range(args.repeat):
        TIMINGS.clear()
        rb, core = session.rb, session.core

        # ---- ingestion (outside golden_pipeline, but on the critical path) ----
        if args.ingest == "legacy":
            t0 = time.perf_counter()
            candles_raw = assemble_candles(frozen_csv, live_csv)
            ingest = time.perf_counter() - t0
            TIMINGS.append({"stage": "assemble_candles(read_csv)", "wall_s": round(ingest, 4), "cpu_s": None})

            t0 = time.perf_counter()
            last_m1 = pd.to_datetime(candles_raw["time"]).max()
            boundary = latest_closed_boundary(last_m1)
            TIMINGS.append({"stage": "boundary_detect(to_datetime max)",
                            "wall_s": round(time.perf_counter() - t0, 4), "cpu_s": None})
        else:
            # Optimised order: settle the frontier from the cached frozen max +
            # the small live segment, and only then materialise history. The
            # cache dict persists across iterations exactly as it does across
            # polls in a live process, so iteration 1 shows the cold cost and
            # iteration 2+ the steady state.
            from live.runner import frontier_time
            t0 = time.perf_counter()
            last_m1 = frontier_time(frozen_csv, live_csv, frozen_cache)
            boundary = latest_closed_boundary(last_m1)
            TIMINGS.append({"stage": "frontier_time(cached frozen max)",
                            "wall_s": round(time.perf_counter() - t0, 4), "cpu_s": None})

            t0 = time.perf_counter()
            candles_raw = assemble_candles(frozen_csv, live_csv)
            ingest = time.perf_counter() - t0
            TIMINGS.append({"stage": "assemble_candles(read_csv)", "wall_s": round(ingest, 4), "cpu_s": None})
            ingest += TIMINGS[-2]["wall_s"]

        print(f"\n--- iteration {i+1}/{args.repeat} ---")
        print(f"raw candle rows : {len(candles_raw):,}")
        print(f"last m1         : {last_m1}")
        print(f"boundary        : {boundary}")

        # ---- wrap every pipeline stage ----
        originals = []
        for owner, name in [(rb, "filter_date_range"), (core, "prepare_candles_for_simulation"),
                            (rb, "load_news_calendar_events"), (rb, "load_news_events"),
                            (session, "prepare_news_cache"), (rb, "resample_candles"),
                            (core, "detect_order_blocks"), (rb, "tag_order_blocks_with_news"),
                            (rb, "filter_order_blocks_by_structure"),
                            (rb, "filter_order_blocks_by_structure_direction"),
                            (core, "prepare_order_blocks_for_simulation")]:
            originals.append((owner, name, _wrap(owner, name)))
        if args.stages_only:
            def _skip(job, config, candles, sim_obs, obs, news_cache):
                return {"results": [{"trades": pd.DataFrame(), "summary": {}}]}
            originals.append((rb, "execute_scenario_job", getattr(rb, "execute_scenario_job")))
            rb.execute_scenario_job = _skip
        else:
            originals.append((rb, "execute_scenario_job", _wrap(rb, "execute_scenario_job", "execute_scenario_job(WALK)")))

        t0, c0 = time.perf_counter(), time.process_time()
        trades = runner.golden_pipeline(candles_raw, str(boundary.date()))
        total = time.perf_counter() - t0
        cpu = time.process_time() - c0

        for owner, name, orig in originals:      # restore
            setattr(owner, name, orig)

        fp = df_fingerprint(trades)
        rec = {"label": args.label, "iteration": i + 1,
               "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "ingest_s": round(ingest, 3),
               "pipeline_wall_s": round(total, 3), "pipeline_cpu_s": round(cpu, 3),
               "cycle_total_s": round(ingest + total, 3),
               "raw_rows": int(len(candles_raw)), "boundary": str(boundary),
               "peak_rss_mb": round(peak_rss_mb(), 1),
               "output": fp, "stages": list(TIMINGS)}
        results.append(rec)

        print(f"\ncycle total  : {ingest + total:.1f}s  (ingest {ingest:.1f}s + pipeline {total:.1f}s)")
        print(f"pipeline cpu : {cpu:.1f}s   peak RSS {rec['peak_rss_mb']:.0f} MB")
        print(f"trades rows  : {fp['rows']}  sha256 {fp['sha256'][:16]}...")
        print("\nstage breakdown (wall s, desc):")
        for s in sorted(TIMINGS, key=lambda x: -x["wall_s"]):
            pct = 100 * s["wall_s"] / (ingest + total)
            print(f"  {s['wall_s']:9.2f}s {pct:5.1f}%  {s['stage']}")

    path = OUT / f"bench-{args.label}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    path.write_text(json.dumps(results, indent=1))
    print(f"\nwritten: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
