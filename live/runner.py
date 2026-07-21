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

import hashlib
import io
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from live.config import ENGINE_VERSION_EXPECTED, PORTFOLIO_INCLUDE_DISABLED_COHORTS
from live.intents import diff_frontier
from live.state import RunnerState

FIFTEEN_MIN = pd.Timedelta(minutes=15)

# C3 — input identity. The version literal is part of the hashed preimage, so any
# format change yields a non-equal revision (fail-safe recompute; no migration).
INPUT_REVISION_VERSION = "ct.input-revision.v1"
LIVE_ABSENT = "absent"


class PhaseProfiler:
    """Driver-side timing profiler (C1-A instrumentation).

    Conforms to ``strategy_core.ports.Profiler`` (``now``/``increment``) but is
    NEVER injected into core execution — it only wraps the driver-side stage
    calls in ``golden_pipeline``. Its readings are operational telemetry and
    must never enter deterministic artifacts or parity outputs.
    """

    __slots__ = ("_timings", "_counters")

    def __init__(self) -> None:
        self._timings: dict[str, float] = {}
        self._counters: dict[str, int] = {}

    def now(self) -> float:
        return time.monotonic()

    def increment(self, key: str, amount: int = 1) -> None:
        self._counters[key] = self._counters.get(key, 0) + amount

    def record(self, name: str, seconds: float) -> None:
        # accumulate so a repeated phase name sums rather than overwrites
        self._timings[name] = round(self._timings.get(name, 0.0) + seconds, 6)

    def snapshot(self) -> dict:
        return {"phase_timings": dict(self._timings), "counters": dict(self._counters)}


@contextmanager
def _timed(profiler: PhaseProfiler | None, name: str):
    """Time a driver-side stage into ``profiler`` when present; a no-op otherwise.

    Wall-clock only; never influences the wrapped stage's inputs or outputs, so
    a timed run and an untimed run are byte-identical in their results."""
    if profiler is None:
        yield
        return
    start = profiler.now()
    try:
        yield
    finally:
        profiler.record(name, profiler.now() - start)


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


@dataclass(frozen=True)
class InputSnapshot:
    """One evaluation's complete deterministic input (C3).

    ``candles`` is parsed from exactly the bytes whose SHA-256 digests compose
    ``input_revision``; that pairing is fixed at construction and never revisited.
    ``input_revision`` is the SOLE authoritative identity — no component hashes,
    metadata or diagnostics live here. Single-use per ``run_once`` evaluation.

    Immutability: the dataclass is frozen, ``input_revision`` is computed exactly
    once at capture and never recomputed (in particular never re-derived from the
    DataFrame), and the ``candles`` reference is never replaced. The contained
    DataFrame is itself mutable — pandas offers no frozen frame — but that cannot
    violate the invariant: the revision describes the *captured bytes*, is never
    recomputed from the frame, and the snapshot is consumed once and discarded.
    """

    candles: pd.DataFrame
    input_revision: str


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _input_revision_preimage(frozen_sha: str, live_sha: str, engine_version: str) -> str:
    """The locked canonical preimage: fixed field order, LF-joined, NO trailing
    newline. ``engine_version`` is stripped and lowercased."""
    return "\n".join((
        f"version={INPUT_REVISION_VERSION}",
        f"frozen_sha256={frozen_sha}",
        f"live_sha256={live_sha}",
        f"engine_version={engine_version.strip().lower()}",
    ))


def _assemble_from_bytes(frozen_bytes: bytes, live_bytes: bytes | None) -> pd.DataFrame:
    """THE single assembly implementation. Seam policy (unchanged): frozen rows win
    at or before the frozen end; live rows win strictly after (no interleaving, no
    rewrite). Parses only the supplied bytes — never re-opens a path."""
    frozen = pd.read_csv(io.BytesIO(frozen_bytes))
    if live_bytes is not None:
        live = pd.read_csv(io.BytesIO(live_bytes))
        if len(live):
            frozen_end = pd.to_datetime(frozen["time"]).max()
            live = live[pd.to_datetime(live["time"]) > frozen_end]
            frozen = pd.concat([frozen, live], ignore_index=True)
    return frozen


def snapshot_from_bytes(frozen_bytes: bytes, live_bytes: bytes | None, *,
                        engine_version: str) -> InputSnapshot:
    """Hash the exact captured bytes, then parse those SAME bytes. There is no
    public constructor accepting a caller-supplied revision, so a DataFrame can
    never be paired with a revision that does not describe it."""
    frozen_sha = _sha256_bytes(frozen_bytes)
    live_sha = _sha256_bytes(live_bytes) if live_bytes is not None else LIVE_ABSENT
    preimage = _input_revision_preimage(frozen_sha, live_sha, engine_version)
    revision = hashlib.sha256(preimage.encode("utf-8")).hexdigest()
    return InputSnapshot(candles=_assemble_from_bytes(frozen_bytes, live_bytes),
                         input_revision=revision)


def capture_input_snapshot(frozen_csv: Path, live_segment_csv: Path, *,
                           engine_version: str) -> InputSnapshot:
    """Production capture: read each configured input file ONCE and snapshot it.
    No path is re-opened after hashing, so there is no TOCTOU window."""
    frozen_bytes = Path(frozen_csv).read_bytes()
    live_path = Path(live_segment_csv)
    live_bytes = live_path.read_bytes() if live_path.exists() else None
    return snapshot_from_bytes(frozen_bytes, live_bytes, engine_version=engine_version)


def assemble_candles(frozen_csv: Path, live_segment_csv: Path) -> pd.DataFrame:
    """Frozen history + live segment (public contract unchanged). Delegates to the
    single assembly implementation; returns only the DataFrame and never fabricates
    an InputRevision."""
    frozen_bytes = Path(frozen_csv).read_bytes()
    live_path = Path(live_segment_csv)
    live_bytes = live_path.read_bytes() if live_path.exists() else None
    return _assemble_from_bytes(frozen_bytes, live_bytes)


def latest_closed_boundary(last_m1_time: pd.Timestamp) -> pd.Timestamp:
    """Candidate evaluation boundary — the OPEN timestamp of the latest fully
    CLOSED 15-minute detection bar (C2 formalized contract; formula unchanged).

    Semantic meaning: returns ``B`` such that the 15m bar ``[B, B+15m)`` is the
    most recent interval guaranteed closed given the data on hand. This is the
    candidate the live driver evaluates, and the value the C3 gate compares
    against the durably-stored ``last_boundary``.

    M1 source assumption: ``last_m1_time`` is the OPEN time of the last stored,
    already-closed 1-minute bar — the live segment only ever contains closed M1
    bars — so that bar's close (the current forming instant) is
    ``last_m1_time + 1min``. A 15m bar ``[B, B+15m)`` is confirmed closed once
    data reaches ``B+15m``; the latest such boundary is uniformly
    ``floor(last_m1_time + 1min, 15min) - 15min``.

    Detection timeframe: fixed at 15 minutes (the Golden ``detection_timeframe``);
    the 1-minute source granularity is fixed by the MT5-bridge data seam. Neither
    is a parameter — there is exactly one production timeframe.

    Timezone behaviour: purely arithmetic (floor + fixed offsets); the result
    carries the input's timezone with NO conversion — tz-aware UTC in yields
    tz-aware UTC out, tz-naive in yields tz-naive out.

    NaT propagation: ``NaT`` in yields ``NaT`` out (an empty candle frame's
    ``.max()`` is ``NaT``); the helper adds no empty-input guard — emptiness is
    the caller's concern.

    Determinism & purity: same input always yields the same output. No wall-clock
    read, no I/O, no broker/MT5 access, no mutable state, no telemetry, no side
    effects.
    """
    next_open = last_m1_time + pd.Timedelta(minutes=1)
    return next_open.floor("15min") - FIFTEEN_MIN


class LiveRunner:
    def __init__(self, config, session: LuxSession | None = None, pipeline=None,
                 input_provider=None):
        self.config = config
        config.ensure_dirs()
        self.session = session
        self.state = RunnerState(config.state_dir)
        self._pipeline = pipeline                # injectable for tests
        # () -> InputSnapshot. None = production capture from the configured paths.
        # Providers must return a complete InputSnapshot built by the capture API;
        # plain DataFrames and caller-supplied revisions are not supported.
        self._input_provider = input_provider

    def _engine_version(self) -> str:
        return self.session.engine_version if self.session else "injected"

    # ── pipeline (mirrors the Golden driver stage-for-stage) ─────────────────
    def golden_pipeline(self, candles_raw: pd.DataFrame, frontier_date: str,
                        artifacts: dict | None = None, *,
                        profiler: PhaseProfiler | None = None) -> pd.DataFrame:
        # `profiler` (keyword-only, optional) records driver-side stage timings
        # externally; it never alters stage inputs/outputs and is NOT written
        # into `artifacts`, which stays deterministic and parity-safe.
        s = self.session
        rb, core = s.rb, s.core
        config = s.golden_config(self.config.golden_config_path, end_date=frontier_date)

        with _timed(profiler, "filter_date_range"):
            candles = rb.filter_date_range(candles_raw, config)
        with _timed(profiler, "prepare_candles"):
            candles = core.prepare_candles_for_simulation(candles)
        with _timed(profiler, "load_news_calendar"):
            calendar_events = rb.load_news_calendar_events(config)
        with _timed(profiler, "load_news_events"):
            news_events = rb.load_news_events(config)
        with _timed(profiler, "prepare_news_cache"):
            news_cache = s.prepare_news_cache(news_events, candles,
                                              config.news_flatten_minutes_before_blackout)
        with _timed(profiler, "resample"):
            detection = rb.resample_candles(candles, config.detection_timeframe)
        with _timed(profiler, "detect_order_blocks"):
            order_blocks = core.detect_order_blocks(
                detection, swing_length=config.swing_length, ob_filter=config.ob_filter,
                pip_size=config.pip_size, min_ob_size_pips=config.min_ob_size_pips,
                max_ob_size_pips=config.max_ob_size_pips)
        with _timed(profiler, "tag_obs_news"):
            order_blocks = rb.tag_order_blocks_with_news(order_blocks, calendar_events)
        with _timed(profiler, "filter_obs"):
            order_blocks = rb.filter_order_blocks_by_structure(order_blocks, config.structure_filter)
            order_blocks, _ = rb.filter_order_blocks_by_structure_direction(
                order_blocks, config.allowed_structure_directions)
        with _timed(profiler, "prepare_obs"):
            simulation_obs = core.prepare_order_blocks_for_simulation(order_blocks)

        entry = [x for x in rb.entry_scenarios(config) if x["mode"] == "triggered_edge"]
        if len(entry) != 1:
            raise RuntimeError(f"expected exactly one Golden entry scenario, got {entry}")
        mode = config.execution_modes[0]
        # job shape mirrors build_parallel_jobs exactly (job_id + label required
        # by execute_scenario_job's result wrapper)
        job = {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
               "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}
        with _timed(profiler, "execute_scenario_job"):
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
        if self._input_provider is not None:
            snapshot = self._input_provider()
        else:
            frozen_csv = self.config.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
            snapshot = capture_input_snapshot(
                frozen_csv, self.config.live_segment_csv,
                engine_version=self._engine_version())
        candles = snapshot.candles
        input_revision = snapshot.input_revision
        last_m1 = pd.to_datetime(candles["time"]).max()
        boundary = latest_closed_boundary(last_m1)
        boundary_str = str(boundary)

        # C3 gate: skip ONLY when the candidate boundary AND the exact input
        # revision are both unchanged. A missing stored revision (pre-C3 state) is
        # unknown and therefore always re-evaluates — fail-safe by construction.
        if (self.state.data["last_boundary"] == boundary_str
                and self.state.data.get("last_recomputed_input_revision") == input_revision):
            return {"status": "no_new_bar", "boundary": boundary_str}

        frontier_date = str(boundary.date())
        # Real path is timed via a driver-side profiler; the injected test/rehearsal
        # pipeline keeps its 2-arg contract and is only wall-clock wrapped.
        _t0 = time.monotonic()
        if self._pipeline is not None:
            trades = self._pipeline(candles, frontier_date)
            phase_timings: dict = {}
        else:
            profiler = PhaseProfiler()
            trades = self.golden_pipeline(candles, frontier_date, profiler=profiler)
            phase_timings = profiler.snapshot()["phase_timings"]
        pipeline_s = round(time.monotonic() - _t0, 6)
        trades_str = trades.astype(str)

        prev = self.state.load_prev_frame()
        # frontier fill_time format matches the engine's candle time strings
        frontier_bar = _engine_time_string(boundary)
        intents = diff_frontier(prev, trades_str, frontier_bar) if prev is not None else []

        first_run = prev is None
        # LR-1: durably reserve every generated intent as PENDING BEFORE the single
        # atomic commit below, so the boundary can never become durable without its
        # intents. No separate save may split them.
        for intent in intents:
            self.state.reserve_pending(intent)
        self.state.store_frame(trades_str, boundary_str, input_revision)
        self.state.save()   # ONE atomic commit: boundary + revision + prev_frame + PENDING intents
        return {
            "status": "bootstrap" if first_run else "ok",
            "boundary": boundary_str,
            "intents": intents,
            "trades_rows": len(trades),
            "engine_version": self._engine_version(),
            "phase_timings": phase_timings,   # C1-A telemetry (non-deterministic; ops only)
            "pipeline_s": pipeline_s,
            "note": ("first run establishes the baseline frame; no intents emitted"
                     if first_run else ""),
        }


def _engine_time_string(ts: pd.Timestamp) -> str:
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts
    return str(ts)
