"""Bounded live computation path — same algorithm, far less history.

M-LIVE-BOUNDED-WORKING-SET-1, Phase 1.

This module deliberately does NOT rewrite the detector, the regime model, the
session classifier or the matrix. It feeds the *production* implementations a
bounded working set and reports what they produce, plus the timings.

Naming is load-bearing: this is `live_bounded`. The eleven-year path keeps its
own name (`full_replay`, see live.full_replay) and neither is a flag on the
other — a future reader cannot accidentally get archival behaviour out of a
function that says "bounded".

Phase 1 has NO execution authority. There is no gateway, executor, arm or
ledger reference anywhere in this module, by construction.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from live.daily_state import DailyStateCache, DailyStore, daily_from_m1
from live.m15_accumulator import completed_m15
from live.working_set import (LIVE_M15_MAX_ROWS, M15_DETECTOR_BARS,
                              M15_WARMUP_BARS, M1_WINDOW_DAYS,
                              WorkingSetError, assert_within_ceiling,
                              load_bounded_m1, trusted_detection_floor)

MODE_FULL = "full_replay"
MODE_SHADOW = "bounded_shadow"
MODE_LIVE = "bounded_live"

OB_IDENTITY_COLUMNS = ["detection_time", "origin_time", "direction", "structure_tag",
                       "top", "bottom", "break_level", "swing_length"]


@dataclass
class BoundedResult:
    boundary: str
    obs: pd.DataFrame
    trusted_floor: pd.Timestamp
    m1_rows: int
    m15_rows: int
    detector_rows: int
    daily_rows: int
    state_built_through: str | None
    durations_ms: dict = field(default_factory=dict)

    @property
    def total_ms(self) -> float:
        return round(sum(self.durations_ms.values()), 3)

    def digest(self) -> str:
        return ob_set_digest(self.obs)


def ob_set_digest(obs: pd.DataFrame) -> str:
    """Order-independent digest of an OB set's identity columns."""
    if not len(obs):
        return hashlib.sha256(b"empty").hexdigest()[:16]
    rows = sorted(json.dumps(r, sort_keys=True, default=str)
                  for r in obs[OB_IDENTITY_COLUMNS].to_dict("records"))
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:16]


class BoundedComputation:
    """Produce the strategy's computational inputs from a bounded working set."""

    def __init__(self, config, lux_session, daily_store_path: Path | None = None,
                 window_days: int = M1_WINDOW_DAYS,
                 detector_bars: int = M15_DETECTOR_BARS,
                 warmup_bars: int = M15_WARMUP_BARS):
        self.config = config
        self.session = lux_session
        self.window_days = window_days
        self.detector_bars = detector_bars
        self.warmup_bars = warmup_bars
        self.daily_store_path = Path(daily_store_path) if daily_store_path else (
            Path(config.state_dir) / "market_data" / "daily_ohlc.csv")
        self._cache: DailyStateCache | None = None

    # ── the archive is a SEED for the daily store, never a live input ────────
    def seed_daily_store(self, archive_csv: Path, live_segment_csv: Path) -> DailyStore:
        """One-off/occasional: derive full daily OHLC from the archive.

        This is the only place the bounded subsystem touches archival history,
        it is explicitly named, and it is off the latency-sensitive path — a
        daily store, once built, is maintained incrementally from live M1.
        """
        from live.runner import assemble_candles
        store = DailyStore(self.daily_store_path)
        store.merge(daily_from_m1(assemble_candles(Path(archive_csv), Path(live_segment_csv))))
        store.save()
        return store

    def compute(self, end_date: str | None = None,
                now: pd.Timestamp | None = None) -> BoundedResult:
        cfg, s = self.config, self.session
        d: dict[str, float] = {}
        archive = cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"

        t0 = time.perf_counter()
        m1 = load_bounded_m1(archive, cfg.live_segment_csv, self.window_days, now)
        d["load_ms"] = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        m15 = completed_m15(m1)
        d["m15_ms"] = (time.perf_counter() - t0) * 1000
        if not len(m15):
            raise WorkingSetError("bounded M15 is empty")

        detector_input = m15.tail(self.detector_bars).reset_index(drop=True)
        assert_within_ceiling(detector_input, LIVE_M15_MAX_ROWS, "bounded detector input")
        floor = trusted_detection_floor(detector_input, self.warmup_bars)

        gc = s.golden_config(cfg.golden_config_path,
                             end_date=end_date or str(pd.to_datetime(
                                 m1["time"], utc=True).max().date()))
        t0 = time.perf_counter()
        obs = s.core.detect_order_blocks(
            detector_input, swing_length=gc.swing_length, ob_filter=gc.ob_filter,
            pip_size=gc.pip_size, min_ob_size_pips=gc.min_ob_size_pips,
            max_ob_size_pips=gc.max_ob_size_pips)
        d["detector_ms"] = (time.perf_counter() - t0) * 1000

        if len(obs):
            trusted = pd.to_datetime(obs["detection_time"], utc=True) >= floor
            obs = obs[trusted].reset_index(drop=True)

        t0 = time.perf_counter()
        store = DailyStore(self.daily_store_path)
        # bounded window => its first UTC day is truncated; see daily_from_m1
        store.merge(daily_from_m1(m1, drop_leading_partial=True))
        store.save()
        recs = store.records()
        self._cache = DailyStateCache(_regime_module(s)).build(recs)
        d["state_ms"] = (time.perf_counter() - t0) * 1000

        return BoundedResult(
            boundary=str(pd.to_datetime(m1["time"], utc=True).max()),
            obs=obs, trusted_floor=floor, m1_rows=len(m1), m15_rows=len(m15),
            detector_rows=len(detector_input), daily_rows=len(recs),
            state_built_through=self._cache.built_through, durations_ms=d)

    @property
    def state_cache(self) -> DailyStateCache:
        if self._cache is None:
            raise WorkingSetError("state cache not built — call compute() first")
        return self._cache


def _regime_module(lux_session):
    from strategy_core import regime
    return regime
