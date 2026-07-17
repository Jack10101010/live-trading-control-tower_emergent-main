"""Live runner — recompute-on-close over the frozen Golden engine.

Golden fidelity strategy: the pipeline calls the SAME Lux driver helpers the
research runs use (`scripts.run_backtest`: config load, news load, date filter,
resample, OB news-tagging, `execute_scenario_job`) plus the frozen
`strategy_core` boundary — zero re-implemented strategy or preparation logic.
The ONLY config delta from the Golden JSON is `end_date`, extended to the
frontier (you cannot trade the past); everything else, including
`portfolio_include_disabled_cohorts=true`, is byte-identical Golden config.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from live.config import ENGINE_VERSION_EXPECTED, PORTFOLIO_INCLUDE_DISABLED_COHORTS
from live.intents import diff_frontier
from live.state import RunnerState

FIFTEEN_MIN = pd.Timedelta(minutes=15)


class LuxSession:
    """Loads the Lux driver + core once, verifies engine identity.

    The Lux driver's contract is CWD = Lux repo root (e.g. the deployed
    portfolio policy is read via the relative path
    `configs/policy/deployed_policy.v1.json`). LuxSession enforces that
    contract with os.chdir at construction — which is why LiveConfig resolves
    all of ITS paths to absolute before any session exists."""

    def __init__(self, lux_root: Path):
        import os
        self.lux_root = Path(lux_root).resolve()
        if str(self.lux_root) not in sys.path:
            sys.path.insert(0, str(self.lux_root))
        os.chdir(self.lux_root)   # driver contract: relative config/policy paths
        import scripts.run_backtest as rb           # driver helpers (impure layer)
        from src.execution import prepare_news_cache  # deliberate src-resident seam
        from src.run_outputs import engine_version
        import strategy_core as core
        self.rb, self.core = rb, core
        self.prepare_news_cache = prepare_news_cache
        self.engine_version = engine_version()

    def verify_engine(self) -> None:
        if self.engine_version != ENGINE_VERSION_EXPECTED:
            raise RuntimeError(
                f"engine_version mismatch: {self.engine_version} != expected "
                f"{ENGINE_VERSION_EXPECTED} — refusing to trade on an unverified engine")

    def golden_config(self, golden_config_path: Path, end_date: str):
        overrides, _ = self.rb.load_config_overrides(str(golden_config_path))
        config = replace(self.rb.ACTIVE_CONFIG, **overrides)
        if not getattr(config, "portfolio_include_disabled_cohorts", False) == PORTFOLIO_INCLUDE_DISABLED_COHORTS:
            raise RuntimeError("Golden config include-disabled flag mismatch")
        return replace(config, end_date=end_date)   # the ONE live delta


def assemble_candles(frozen_csv: Path, live_segment_csv: Path) -> pd.DataFrame:
    """Frozen history + live segment. Seam policy: frozen rows win at or before
    the frozen end; live rows win strictly after (no interleaving, no rewrite)."""
    frozen = pd.read_csv(frozen_csv)
    if live_segment_csv.exists():
        live = pd.read_csv(live_segment_csv)
        if len(live):
            frozen_end = pd.to_datetime(frozen["time"]).max()
            live = live[pd.to_datetime(live["time"]) > frozen_end]
            frozen = pd.concat([frozen, live], ignore_index=True)
    return frozen


def latest_closed_boundary(last_m1_time: pd.Timestamp) -> pd.Timestamp:
    """A 15m bar [B, B+15) is confirmed closed once M1 data reaches B+15.
    With next_open = close time of the last stored M1 bar, the latest closed
    boundary is uniformly floor(next_open, 15m) - 15m."""
    next_open = last_m1_time + pd.Timedelta(minutes=1)
    return next_open.floor("15min") - FIFTEEN_MIN


class LiveRunner:
    def __init__(self, config, session: LuxSession | None = None, pipeline=None,
                 candles_provider=None):
        self.config = config
        config.ensure_dirs()
        self.session = session
        self.state = RunnerState(config.state_dir)
        self._pipeline = pipeline                    # injectable for tests
        self._candles_provider = candles_provider    # injectable for rehearsal (P0)

    # ── pipeline (mirrors the Golden driver stage-for-stage) ─────────────────
    def golden_pipeline(self, candles_raw: pd.DataFrame, frontier_date: str,
                        artifacts: dict | None = None) -> pd.DataFrame:
        s = self.session
        rb, core = s.rb, s.core
        config = s.golden_config(self.config.golden_config_path, end_date=frontier_date)

        candles = rb.filter_date_range(candles_raw, config)
        candles = core.prepare_candles_for_simulation(candles)
        calendar_events = rb.load_news_calendar_events(config)
        news_events = rb.load_news_events(config)
        news_cache = s.prepare_news_cache(news_events, candles,
                                          config.news_flatten_minutes_before_blackout)
        detection = rb.resample_candles(candles, config.detection_timeframe)
        order_blocks = core.detect_order_blocks(
            detection, swing_length=config.swing_length, ob_filter=config.ob_filter,
            pip_size=config.pip_size, min_ob_size_pips=config.min_ob_size_pips,
            max_ob_size_pips=config.max_ob_size_pips)
        order_blocks = rb.tag_order_blocks_with_news(order_blocks, calendar_events)
        order_blocks = rb.filter_order_blocks_by_structure(order_blocks, config.structure_filter)
        order_blocks, _ = rb.filter_order_blocks_by_structure_direction(
            order_blocks, config.allowed_structure_directions)
        simulation_obs = core.prepare_order_blocks_for_simulation(order_blocks)

        entry = [x for x in rb.entry_scenarios(config) if x["mode"] == "triggered_edge"]
        if len(entry) != 1:
            raise RuntimeError(f"expected exactly one Golden entry scenario, got {entry}")
        mode = config.execution_modes[0]
        # job shape mirrors build_parallel_jobs exactly (job_id + label required
        # by execute_scenario_job's result wrapper)
        job = {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
               "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}
        out = rb.execute_scenario_job(job, config, candles, simulation_obs,
                                      order_blocks, news_cache)
        result = out["results"][0]
        if artifacts is not None:   # rehearsal capture only — no behavioural effect
            artifacts.update({"config": config, "candles": candles,
                              "order_blocks": order_blocks, "simulation_obs": simulation_obs,
                              "news_cache": news_cache, "summary": result["summary"]})
        return result["trades"]

    # ── one cycle ────────────────────────────────────────────────────────────
    def run_once(self, now_utc: datetime | None = None) -> dict:
        if self._candles_provider is not None:
            candles = self._candles_provider()
        else:
            frozen_csv = self.config.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
            candles = assemble_candles(frozen_csv, self.config.live_segment_csv)
        last_m1 = pd.to_datetime(candles["time"]).max()
        boundary = latest_closed_boundary(last_m1)
        boundary_str = str(boundary)

        if self.state.data["last_boundary"] == boundary_str:
            return {"status": "no_new_bar", "boundary": boundary_str}

        pipeline = self._pipeline or self.golden_pipeline
        frontier_date = str(boundary.date())
        trades = pipeline(candles, frontier_date)
        trades_str = trades.astype(str)

        prev = self.state.load_prev_frame()
        # frontier fill_time format matches the engine's candle time strings
        frontier_bar = _engine_time_string(boundary)
        intents = diff_frontier(prev, trades_str, frontier_bar) if prev is not None else []

        first_run = prev is None
        self.state.store_frame(trades_str, boundary_str)
        self.state.save()
        return {
            "status": "bootstrap" if first_run else "ok",
            "boundary": boundary_str,
            "intents": intents,
            "trades_rows": len(trades),
            "engine_version": self.session.engine_version if self.session else "injected",
            "note": ("first run establishes the baseline frame; no intents emitted"
                     if first_run else ""),
        }


def _engine_time_string(ts: pd.Timestamp) -> str:
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return str(ts)
