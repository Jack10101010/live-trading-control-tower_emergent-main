"""M-CAPACITY-1 Phase 4 — targeted profile of the news-tagging stage.

`tag_order_blocks_with_news` measured 110.4 s (31.9% of the pre-walk pipeline),
which is second only to `detect_order_blocks`. Reaching it costs ~236 s of
upstream work, so this script captures the stage's real inputs ONCE to a pickle
and then profiles the stage alone as many times as needed.

Read-only w.r.t. production. Writes only under bench/out/.
"""
from __future__ import annotations

import argparse
import cProfile
import io
import os
import pickle
import pstats
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out"
OUT.mkdir(parents=True, exist_ok=True)
FIXTURE = OUT / "tag_news_inputs.pkl"


def capture() -> None:
    """Run the pipeline up to the tagging stage and pickle its exact inputs."""
    sys.path.insert(0, str(REPO))
    from live.config import LiveConfig
    from live.runner import LuxSession, assemble_candles, latest_closed_boundary
    import pandas as pd

    cfg = LiveConfig()
    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    session = LuxSession(lux_root)
    rb, core = session.rb, session.core

    frozen = lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
    t0 = time.perf_counter()
    candles_raw = assemble_candles(frozen, cfg.live_segment_csv)
    boundary = latest_closed_boundary(pd.to_datetime(candles_raw["time"]).max())
    config = session.golden_config(cfg.golden_config_path, end_date=str(boundary.date()))
    candles = rb.filter_date_range(candles_raw, config)
    candles = core.prepare_candles_for_simulation(candles)
    calendar_events = rb.load_news_calendar_events(config)
    detection = rb.resample_candles(candles, config.detection_timeframe)
    order_blocks = core.detect_order_blocks(
        detection, swing_length=config.swing_length, ob_filter=config.ob_filter,
        pip_size=config.pip_size, min_ob_size_pips=config.min_ob_size_pips,
        max_ob_size_pips=config.max_ob_size_pips)
    print(f"upstream took {time.perf_counter()-t0:.1f}s")
    print(f"order_blocks    : {len(order_blocks):,} rows x {len(order_blocks.columns)} cols")
    print(f"calendar_events : {len(calendar_events):,}")
    print(f"detection bars  : {len(detection):,}")
    print(f"candles rows    : {len(candles):,}")
    with FIXTURE.open("wb") as fh:
        pickle.dump({"order_blocks": order_blocks, "calendar_events": calendar_events}, fh)
    print(f"fixture written : {FIXTURE} ({FIXTURE.stat().st_size/1024/1024:.1f} MB)")


def profile(top: int) -> None:
    sys.path.insert(0, str(REPO))
    from live.runner import LuxSession
    session = LuxSession(Path(os.environ["LUX_ROOT"]).resolve())
    rb = session.rb

    with FIXTURE.open("rb") as fh:
        data = pickle.load(fh)
    obs, events = data["order_blocks"], data["calendar_events"]
    print(f"order_blocks {len(obs):,}   calendar_events {len(events):,}")
    print(f"=> per-OB event scans: 2 x {len(events):,} = {2*len(events):,}")
    print(f"=> total scans: {len(obs):,} x {2*len(events):,} = {len(obs)*2*len(events):,}")

    t0 = time.perf_counter()
    result = rb.tag_order_blocks_with_news(obs, events)
    wall = time.perf_counter() - t0
    print(f"\nplain wall: {wall:.2f}s for {len(obs):,} order blocks "
          f"({1000*wall/max(1,len(obs)):.2f} ms/OB)")

    pr = cProfile.Profile()
    pr.enable()
    rb.tag_order_blocks_with_news(obs, events)
    pr.disable()
    for sort in ("cumulative", "tottime"):
        s = io.StringIO()
        pstats.Stats(pr, stream=s).sort_stats(sort).print_stats(top)
        print(f"\n===== by {sort} =====")
        print(s.getvalue()[:4000])

    import hashlib
    body = result.astype(str).to_csv(index=False).encode()
    print("OUTPUT SHA256:", hashlib.sha256(body).hexdigest())
    (OUT / "tag_news_baseline.sha256").write_text(hashlib.sha256(body).hexdigest())


def equiv(tag: str) -> None:
    """Run the tagging stage against the captured fixture and dump output + hash.

    Run once with LUX_ROOT pointing at the PRISTINE pinned tree and once at the
    patched worktree, then diff the two CSVs. Two copies of `scripts.run_backtest`
    cannot be imported into one process (they both pull `src.*`/`strategy_core.*`
    relative to their own repo), so the A/B is done as two processes over one
    identical pickled input rather than in-process monkey-patching.
    """
    import hashlib
    sys.path.insert(0, str(REPO))
    from live.runner import LuxSession

    lux_root = Path(os.environ["LUX_ROOT"]).resolve()
    session = LuxSession(lux_root)
    with FIXTURE.open("rb") as fh:
        data = pickle.load(fh)
    obs, events = data["order_blocks"], data["calendar_events"]

    t0 = time.perf_counter()
    out = session.rb.tag_order_blocks_with_news(obs, events)
    wall = time.perf_counter() - t0

    csv_path = OUT / f"tag_news_{tag}.csv"
    body = out.astype(str).to_csv(index=False).encode()
    csv_path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    print(f"tag        : {tag}")
    print(f"lux_root   : {lux_root}")
    print(f"engine_ver : {session.engine_version}")
    print(f"rows x cols: {len(out):,} x {len(out.columns)}")
    print(f"wall       : {wall:.2f}s")
    print(f"sha256     : {digest}")
    print(f"csv        : {csv_path}")
    (OUT / f"tag_news_{tag}.sha256").write_text(f"{digest}  wall={wall:.3f}s  rows={len(out)}\n")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["capture", "profile", "equiv"])
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--tag", default="run")
    a = ap.parse_args()
    if a.mode == "capture":
        capture()
    elif a.mode == "profile":
        profile(a.top)
    else:
        equiv(a.tag)
