"""M-LIVE-STATE-4 — real one-boundary Model B advance, compared to the oracle.

The walk is a fold (`ctx` built once, `SimState` the sole carrier), so ONE
full-history run can snapshot state at every boundary we want to test. That
matters: re-running the 11-year oracle per test boundary would cost ~850 s each.

Nothing in the engine is modified. `simulate_trades` is replaced at the point
`scripts.run_backtest` looks it up, with a wrapper that rebuilds the identical
`_p` dict via `inspect.signature(...).bind(...).apply_defaults()` — which
reproduces the real function's `dict(locals())` exactly — then runs the same
`_build_context` / `_init_state` / `apply_bar` / `finalize` sequence, snapshotting
in between. Same parameters, same code path, same result.

Absolute ordinal space = position in the FULL-history M1 candle frame. A window
frame's origin is the absolute ordinal of its first candle, so carried state
rebases by subtraction exactly as `live.live_state` specifies.

Read-only w.r.t. production. No MT5, no Control Tower, no files written outside
bench/out/.
"""
from __future__ import annotations

import copy
import inspect
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out" / "modelb"
OUT.mkdir(parents=True, exist_ok=True)


class Captured:
    """Holds what the patched simulate_trades observed."""

    def __init__(self):
        self.ctx = None
        self.final_state = None
        self.snapshots: dict = {}
        self.trades = None
        self.walk_seconds = 0.0


def patch_simulate_trades(rb, execution, cap: Captured, snapshot_times: set | None = None,
                          walk: bool = True):
    """Swap in a capturing fold. Returns the original for restoration."""
    original = rb.simulate_trades
    sig = inspect.signature(original)

    def capturing(*args, **kwargs):
        bound = sig.bind(*args, **kwargs)
        bound.apply_defaults()
        _p = dict(bound.arguments)                 # == dict(locals()) at entry
        ctx = execution._build_context(_p)
        if not isinstance(ctx, dict):
            return ctx                             # empty-order-block early return
        cap.ctx = ctx
        if not walk:
            # Context only: caller wants the frame, not the simulation.
            return execution._trade_frame([], _p["target_set"])
        state = execution._init_state(ctx)
        t0 = time.perf_counter()
        want = snapshot_times or set()
        for i, candle in enumerate(ctx["candle_records"]):
            execution.apply_bar(state, candle, i, ctx)
            t = str(candle["time"])
            if t in want:
                cap.snapshots[t] = copy.deepcopy(state)
        cap.walk_seconds = time.perf_counter() - t0
        cap.final_state = state
        out = execution.finalize(state, ctx)
        cap.trades = out
        return out

    rb.simulate_trades = capturing
    return original


def build_session(lux_root: Path):
    sys.path.insert(0, str(REPO))
    from live.runner import LuxSession
    session = LuxSession(lux_root)
    import strategy_core.execution as execution
    return session, execution


def run_pipeline(session, cfg, raw, end_date: str, start_date: str | None,
                 cap: Captured, execution, snapshot_times=None, walk=True):
    """The golden pipeline, optionally windowed, with the fold captured."""
    from dataclasses import replace
    rb, core = session.rb, session.core
    config = session.golden_config(cfg.golden_config_path, end_date=end_date)
    if start_date:
        config = replace(config, start_date=start_date)

    candles = core.prepare_candles_for_simulation(rb.filter_date_range(raw, config))
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

    original = patch_simulate_trades(rb, execution, cap, snapshot_times, walk)
    try:
        rb.execute_scenario_job(job, config, candles, sim_obs, obs, news_cache)
    finally:
        rb.simulate_trades = original
    return candles, obs


def ordinal_map(ctx) -> dict:
    """time-string -> absolute ordinal, from the canonical full-history frame."""
    return {str(c["time"]): i for i, c in enumerate(ctx["candle_records"])}


def frame_origin_for(window_ctx, absolute_of: dict):
    """FrameOrigin of a window frame within the canonical absolute space."""
    from live.live_state import FrameOrigin
    recs = window_ctx["candle_records"]
    first, last = str(recs[0]["time"]), str(recs[-1]["time"])
    if first not in absolute_of:
        raise KeyError(f"window first candle {first} absent from the canonical frame")
    return FrameOrigin(origin_ordinal=absolute_of[first], length=len(recs),
                       first_time=first, last_time=last)


def state_to_absolute(state, origin, execution):
    """SimState -> canonical absolute dicts, via the approved registry seam."""
    from live.live_state import (LiveState, active_trade_to_absolute,
                                 ghost_state_to_absolute, pending_to_absolute)
    ghosts = {}
    for k, g in (getattr(state.ghost_tracker, "_states", {}) or {}).items():
        ghosts[str(k)] = ghost_state_to_absolute(dict(vars(g)), origin)
    return LiveState(
        pending=[pending_to_absolute(p, origin) for p in state.pending],
        active_trades=[active_trade_to_absolute(t, origin) for t in state.active_trades],
        ghost_states=ghosts, ob_cursor=state.ob_cursor,
        be_applied_any=bool(state._be_applied_any))


def restore_state(absolute: "LiveState", origin, ctx, execution):
    """Canonical absolute state -> a real SimState usable by apply_bar."""
    from live.live_state import (active_trade_to_local, ghost_state_to_local,
                                 pending_to_local)
    from strategy_core.ghost_tracker import GhostTracker
    ghost_config, profile = ctx["_state_seed"]
    tracker = GhostTracker(ghost_config)
    for k, g in absolute.ghost_states.items():
        local = ghost_state_to_local(g, origin)
        obj = object.__new__(_ghost_state_cls())
        obj.__dict__.update(local)
        tracker._states[k] = obj
    return execution.SimState(
        trades=[], pending=[pending_to_local(p, origin) for p in absolute.pending],
        active_trades=[active_trade_to_local(t, origin) for t in absolute.active_trades],
        ob_cursor=absolute.ob_cursor, ghost_tracker=tracker,
        _be_applied_any=absolute.be_applied_any, profile=profile)


def _ghost_state_cls():
    from strategy_core.ghost_tracker import GhostState
    return GhostState


def summarise(state, origin, execution) -> dict:
    a = state_to_absolute(state, origin, execution)
    return {"pending": len(a.pending), "active_trades": len(a.active_trades),
            "ghosts": len(a.ghost_states), "ob_cursor": a.ob_cursor,
            "be_applied_any": a.be_applied_any}
