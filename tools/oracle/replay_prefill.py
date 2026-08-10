"""Stage S7 — PRE_FILL_PATH_STATE: the reference replay of production's pending
loop, from admission to the fill-gate boundary.

    python -m tools.oracle.replay_prefill --selfcheck --start 2025-09-30

WHAT THIS COVERS
----------------
`strategy_core/execution.py::simulate_trades`, the `for item in pending:` loop.
S7 ends the instant the fill-gate condition becomes true. Everything the
condition GUARDS — session filter, regime filter, portfolio policy, cohort and
state-target eligibility — is S8 and is deliberately absent here.

LINE NUMBERS MOVE. They are quoted throughout as landmarks, not as anchors, and
they are correct for engine `eb251e5b` (commit b96fa7a, M-CAP-OPT-3):

    pending loop        2713          fill gate           2880
    news pause          2726          invalidation        3199

The apparatus does not depend on them: `selfcheck` re-derives every terminal from
the deployed engine, so a production edit shows up as a MISMATCH rather than as a
stale comment. Under engine `f9f6cf7e` the same landmarks were 2480 / 2644 / 2493
/ 2958.

THE EXECUTION BASIS IS ONE MINUTE, NOT FIFTEEN
----------------------------------------------
This is the single most important fact about S7 and it is not visible from the
stage description. `run_backtest` resamples to `detection_timeframe` (15m) ONLY
to detect order blocks; `simulate_trades` is handed `candles`, which is the raw
`candle_file` — `EURUSD_1m_extended_2015_2026.csv`. So admission, arming, the
delay counter and the touch test all run bar-by-bar on ONE-MINUTE candles, and
`triggered_edge_delay_candles = 3` means three MINUTES, not three chart bars.

`execution_timeframe = "1min"` in the config agrees, but nothing reads it except
a config hash — the basis is set by which frame is passed, not by that field.

Consequences recorded rather than smoothed over:
  * a 15-minute chart cannot order the events inside one of its own bars, so it
    cannot decide arm-then-delay-then-touch. See `decidability_at()`.
  * `candle_index` here is a 1-minute index and is NOT comparable to a chart
    `bar_index` (limitation L-09, at a different granularity).

INERT UNDER THE DEPLOYED CONFIGURATION
--------------------------------------
Three production branches exist in the source and cannot execute:
  * SESSION_FILTERED       — `session_filter_enabled = False`
  * REGIME_BLOCKED         — `regime_gate_enabled = False`, so `regime_emit` is None
  * REVERSE_TOUCH_CANCEL   — hard-forced False in `simulation_kwargs`
They are NOT mirrored. `resolve_prefill_config` refuses to run if any of them
becomes reachable, so the silence is a checked claim rather than an omission.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

#: Terminal pre-fill classifications. Everything here happens BEFORE any policy
#: check, so none of them is an eligibility decision.
PENDING = "PENDING"                       # still alive at the end of the window
GATE_READY = "FILL_GATE_READY"            # reached the S8 boundary
NEWS_TOUCH_CANCEL = "NEWS_TOUCH_CANCEL"   # touched inside a blackout
NEVER_TRIGGERED = "NEVER_TRIGGERED"       # window ended, never armed
NEVER_FILLED = "NEVER_FILLED_AFTER_TRIGGER"
#: price went CLEAN THROUGH the block — `execution.py:2958`, outcome `INVALID`,
#: cancel_reason `invalidated_before_edge_entry`.
INVALIDATED = "INVALIDATED_BEFORE_EDGE_ENTRY"
SUPPRESSED = "SUPPRESSED"                 # never entered `pending` at all

TERMINALS = (GATE_READY, NEWS_TOUCH_CANCEL, INVALIDATED, NEVER_TRIGGERED,
             NEVER_FILLED, SUPPRESSED)

#: Per-setup lifecycle events, in the order production can produce them.
EVENTS = ("ADMITTED", "TAPPED_BEFORE_TRIGGER", "ARMED", "DELAY_SATISFIED",
          "NEWS_PAUSED", "NEWS_REARMED", "NEWS_TOUCH_CANCEL", "INVALIDATED",
          "GATE_READY")

#: Branches that exist in production and cannot execute under the deployed
#: configuration. Mirroring them would be speculative Pine; ignoring them
#: silently would be a gap. Recorded, and checked on every run.
INERT_BRANCHES = {
    "SESSION_FILTERED": {
        "source": "strategy_core/execution.py:2647",
        "flag": "session_filter_enabled",
        "reason": "session_filter_enabled = False",
        "stage": "S8",
    },
    "REGIME_BLOCKED": {
        "source": "strategy_core/execution.py:2662",
        "flag": "regime_gate_enabled",
        "reason": "regime_gate_enabled = False, so regime_emit_for_run returns None",
        "stage": "S8",
    },
    "REVERSE_TOUCH_CANCEL": {
        "source": "strategy_core/execution.py:2600-2631",
        "flag": "reverse_touch_cancel_enabled",
        "reason": "hard-forced False in run_backtest.simulation_kwargs",
        "stage": "S7",
    },
    "USED_OB_RETRACE_CANCEL": {
        "source": "strategy_core/execution.py:2970-2997",
        "flag": "triggered_edge_cancel_on_retrace",
        "reason": "triggered_edge_cancel_on_retrace = False",
        "stage": "S7",
    },
    "FIRST_FAILED_TAG_CANCEL": {
        "source": "strategy_core/execution.py:2999-3041",
        "flag": "triggered_edge_cancel_on_first_failed_tag",
        "reason": "triggered_edge_cancel_on_first_failed_tag = False",
        "stage": "S7",
    },
    "EXITED_OB_BEFORE_ARM": {
        "source": "strategy_core/execution.py:2535-2556",
        "flag": "triggered_edge_cancel_if_exits_ob_before_arm",
        "reason": "not passed by simulation_kwargs; simulate_trades defaults it "
                  "to False",
        "stage": "S7",
    },
    "REVISIT_CONFIRMATION": {
        "source": "strategy_core/execution.py:2562-2598",
        "flag": "triggered_edge_require_revisit_confirmation",
        "reason": "not passed by simulation_kwargs; simulate_trades defaults it "
                  "to False, so the fill gate's revisit clause is vacuously true",
        "stage": "S7",
    },
}


class PrefillError(RuntimeError):
    pass


# ── configuration ────────────────────────────────────────────────────────────

def resolve_prefill_config(cfg) -> dict:
    """The pre-fill parameters, resolved from production and CHECKED.

    Every value the loop reads is pinned here rather than sampled at the call
    site, so a production change surfaces as a refusal instead of as a quietly
    different replay.
    """
    models = list(getattr(cfg, "entry_models", []) or [])
    if models != ["triggered_edge"]:
        raise PrefillError(
            f"entry_models is {models!r}; S7 mirrors the deployed "
            "`triggered_edge` path only. Another model changes admission, the "
            "arm test and the touch comparison together.")
    thresholds = list(getattr(cfg, "triggered_edge_trigger_thresholds", []) or [])
    delays = list(getattr(cfg, "triggered_edge_candle_delays", []) or [])
    if len(thresholds) != 1 or len(delays) != 1:
        raise PrefillError(
            f"the deployed entry scenario must be a single (threshold, delay) "
            f"pair; got thresholds={thresholds} delays={delays}. More than one "
            "means several pending populations coexist and 'the' pre-fill path "
            "is not well defined.")

    modes = list(getattr(cfg, "execution_modes", []) or [])
    if modes != ["allow_multi_position"]:
        raise PrefillError(
            f"execution_modes is {modes!r}. Only `allow_multi_position` makes "
            "`can_fill` unconditionally True; every other mode reads "
            "`active_trades`, which is execution state Pine cannot know "
            "(limitation L-12), and S7 would stop being reproducible.")

    for name, meta in INERT_BRANCHES.items():
        if bool(getattr(cfg, meta["flag"], False)) and name != "REVERSE_TOUCH_CANCEL":
            raise PrefillError(
                f"{name} is no longer inert ({meta['flag']} is True). The parity "
                "contract records it as not mirrored in Pine; implement it "
                "before parity can be claimed again.")

    return {
        "entry_model": "triggered_edge",
        "trigger_threshold_pct": float(thresholds[0]),
        "delay_candles": int(delays[0]),
        "entry_level_pct": float(getattr(cfg, "triggered_edge_entry_level_pct", 0) or 0.0),
        "trade_direction": str(getattr(cfg, "trade_direction", "both")),
        "pip_size": float(cfg.pip_size),
        "stop_buffer": float(cfg.stop_buffer_pips) * float(cfg.pip_size),
        "execution_mode": "allow_multi_position",
        "can_fill_is_constant": True,
        "news_blackout_enabled": bool(cfg.news_blackout_enabled),
        "news_pause_pending_orders": bool(cfg.news_pause_pending_orders),
        "news_cancel_if_touched_during_blackout": bool(
            cfg.news_cancel_if_touched_during_blackout),
        "detection_timeframe": str(cfg.detection_timeframe),
        "execution_basis": "candle_file",
        "inert_branches": {k: dict(v) for k, v in INERT_BRANCHES.items()},
    }


# ── the arithmetic, written out so Pine can mirror it ────────────────────────

def planned_entry(top, bottom, direction, entry_level_pct):
    """`_planned_trade`'s entry for the triggered_edge model.

    With `triggered_edge_entry_level_pct = 0` this is exactly the OB edge, but
    the term is kept because it is CONFIG, not a law.
    """
    depth = float(top) - float(bottom)
    shift = depth * (float(entry_level_pct) / 100.0)
    return (float(top) - shift) if direction == "bullish" else (float(bottom) + shift)


def planned_risk(top, bottom, direction, entry, stop_buffer):
    """Production REFUSES to admit an order block whose risk is non-positive
    (`_planned_trade` returns None). That is an admission decision, so it belongs
    to S7 rather than to the plan."""
    if direction == "bullish":
        return entry - (float(bottom) - stop_buffer)
    return (float(top) + stop_buffer) - entry


def direction_allowed(direction, trade_direction):
    value = str(trade_direction or "both").strip().lower()
    if value in {"both", "all", ""}:
        return True
    if value in {"long", "buy", "bull", "bullish"}:
        return direction == "bullish"
    if value in {"short", "sell", "bear", "bearish"}:
        return direction == "bearish"
    return True


def penetration(top, bottom, direction, high, low):
    """`_penetration_price` — how far price came INTO the block, never negative."""
    if direction == "bullish":
        return max(0.0, float(top) - float(low))
    return max(0.0, float(high) - float(bottom))


def trigger_touched(top, bottom, direction, high, low, threshold_pct):
    """`_triggered_edge_touched`. Note `>=`, and note that a zero-depth block can
    never arm — production guards `depth <= 0` before the comparison."""
    depth = float(top) - float(bottom)
    if depth <= 0:
        return False
    return penetration(top, bottom, direction, high, low) >= depth * (
        float(threshold_pct) / 100.0)


def pre_arm_tap(direction, entry, high, low):
    """`_is_filled` — the ONE-SIDED test, used before the order is armed."""
    return (float(low) <= entry) if direction == "bullish" else (float(high) >= entry)


def gate_touch(direction, entry, high, low, armed):
    """The touch half of the fill gate.

    ARMED triggered-edge orders use a CONTAINMENT test (`low <= entry <= high`);
    everything else uses the one-sided test. Getting this backwards is the
    easiest way to invent fills that production never had, because containment
    is strictly stronger for a bar that gapped past the level.
    """
    if armed:
        return float(low) <= entry <= float(high)
    return pre_arm_tap(direction, entry, high, low)


#: The execution oracle's chart timeframe, in seconds. One minute.
EXECUTION_BAR_SECONDS = 60


def derive_detection_frame(candles, bucket_seconds=900):
    """Mirror of `pine/src/x30_detection_frame.pinefrag`, in Python.

    THIS IS THE CROSS-TIMEFRAME HANDOFF. The execution oracle runs on a 1-minute
    chart but every stage before S7 was computed on 15-minute bars, so the 1-minute
    build has to produce that frame itself. It does NOT ask TradingView for
    15-minute bars: production does not either. `run_backtest` builds its
    detection frame with `resample_candles(candles, "15min")`, i.e. pandas
    `.resample()` — LEFT-labelled, LEFT-closed, with EMPTY BUCKETS DROPPED — and
    asking a second aggregator for the same thing would introduce a second
    authority to disagree with.

    Returned rows are (bucket_label_epoch_ms, open, high, low, close, bar_count)
    and are emitted ONLY once the bucket is closed, which is what makes the Pine
    causal: a bucket is finalised on the first 1-minute bar of the NEXT bucket,
    so nothing downstream ever reads a bar that could still change.

    `test_oracle_dual_target.py` asserts this against production's own
    `resample_candles` over real data, bar for bar.
    """
    step = int(bucket_seconds) * 1000
    out = []
    cur = None
    for c in candles:
        t = c["time"]
        ms = int(t.value // 1_000_000) if hasattr(t, "value") else int(t)
        bucket = (ms // step) * step
        h, l, cl = float(c["high"]), float(c["low"]), float(c["close"])
        if cur is None:
            cur = [bucket, float(c["open"]), h, l, cl, 1]
        elif bucket == cur[0]:
            cur[2] = max(cur[2], h)
            cur[3] = min(cur[3], l)
            cur[4] = cl
            cur[5] += 1
        else:
            out.append(tuple(cur))          # the bucket just CLOSED
            cur = [bucket, float(c["open"]), h, l, cl, 1]
    # The final bucket is deliberately NOT emitted: on a live chart it is still
    # open, and emitting it is precisely the lookahead this design avoids.
    return out


def invalidated(top, bottom, direction, high, low):
    """`execution.py:2958` — price went CLEAN THROUGH the block.

    Note what it compares: the block's FAR edge, not the entry, and STRICTLY.
    A bar that merely reaches the far edge leaves the setup alive.
    """
    if direction == "bullish":
        return float(low) < float(bottom)
    return float(high) > float(top)


def delay_satisfied(candle_index, trigger_candle_index, delay_candles):
    """`candle_index >= trigger_candle_index + delay`, with production's
    `trigger_candle_index is None` escape hatch preserved."""
    if trigger_candle_index is None:
        return True
    return candle_index >= trigger_candle_index + delay_candles


# ── the replay ───────────────────────────────────────────────────────────────

def replay(obs, candles, cfg, news_match=None):
    """Walk the pending loop over `candles`.

    `obs` is a list of order-block records (as `prepare_order_blocks_for_simulation`
    leaves them, sorted by detection_time). `candles` is a list of dicts with
    time/open/high/low/close — the EXECUTION frame, which in production is the
    1-minute series. `news_match(time)` returns the blackout event or None; pass
    None to run with news disabled and record that in the result.

    Returns (setups, stats). One setup per admitted order block, carrying its
    full event list; a setup that was never admitted is recorded too, with the
    reason, because "production declined to track this block" is an S7 answer.
    """
    threshold = cfg["trigger_threshold_pct"]
    delay = cfg["delay_candles"]
    news_on = cfg["news_blackout_enabled"] and news_match is not None

    setups = []
    pending = []
    cursor = 0
    n_obs = len(obs)
    max_concurrent = 0

    for i, c in enumerate(candles):
        ctime, chigh, clow = c["time"], float(c["high"]), float(c["low"])

        # ── admission. `<=` means a block detected ON this candle is admitted
        # on this candle, so it can arm and reach the gate the same minute.
        while cursor < n_obs and obs[cursor]["detection_time"] <= ctime:
            ob = obs[cursor]
            cursor += 1
            direction = ob["direction"]
            top, bottom = float(ob["top"]), float(ob["bottom"])
            s = {
                "ob_id": ob.get("ob_id"),
                "direction": direction,
                "structure_tag": ob.get("structure_tag"),
                "top": top, "bottom": bottom,
                "detection_time": str(ob["detection_time"]),
                "admitted_index": i,
                "admitted": False, "suppressed_reason": None,
                "entry": None, "armed": False, "trigger_candle_index": None,
                "trigger_time": None, "tapped_before_trigger": False,
                "news_paused": False, "news_pause_count": 0,
                "delay_satisfied_index": None,
                "terminal": None, "terminal_index": None,
                "events": [],
            }
            setups.append(s)
            if not direction_allowed(direction, cfg["trade_direction"]):
                s["suppressed_reason"] = "DIRECTION_NOT_ALLOWED"
                s["terminal"] = "SUPPRESSED"
                s["terminal_index"] = i
                continue
            entry = planned_entry(top, bottom, direction, cfg["entry_level_pct"])
            if planned_risk(top, bottom, direction, entry, cfg["stop_buffer"]) <= 0:
                s["suppressed_reason"] = "NON_POSITIVE_RISK"
                s["terminal"] = "SUPPRESSED"
                s["terminal_index"] = i
                continue
            s["entry"] = entry
            s["admitted"] = True
            s["events"].append(("ADMITTED", i))
            pending.append(s)

        if not pending:
            continue
        max_concurrent = max(max_concurrent, len(pending))

        # Production looks the blackout up ONCE per candle, and only when there
        # is something pending — mirrored so the event stream lines up.
        event = news_match(ctime) if news_on else None

        alive = []
        for s in pending:
            entry, direction = s["entry"], s["direction"]

            # ── news pause. This sits BEFORE the arm logic, which is why news
            # gates S7 and not only S8.
            if event is not None and cfg["news_pause_pending_orders"]:
                if not s["news_paused"]:
                    s["news_paused"] = True
                    s["news_pause_count"] += 1
                    s["events"].append(("NEWS_PAUSED", i))
                # The touched-during-blackout cancel reuses the SAME arm/delay
                # clause as the fill gate:
                #     entry_model != "triggered_edge" or (armed and delay_ok)
                # For the deployed triggered_edge model an UNARMED order can
                # therefore never be news-cancelled, however deep price goes.
                # Reading that clause as "unarmed passes" cancelled two setups
                # production filled.
                armed_ok = s["armed"] and delay_satisfied(
                    i, s["trigger_candle_index"], delay)
                if cfg["news_cancel_if_touched_during_blackout"] and armed_ok \
                        and gate_touch(direction, entry, chigh, clow, True):
                    s["events"].append(("NEWS_TOUCH_CANCEL", i))
                    s["terminal"] = NEWS_TOUCH_CANCEL
                    s["terminal_index"] = i
                    continue
                alive.append(s)
                continue

            if s["news_paused"]:
                s["news_paused"] = False
                s["events"].append(("NEWS_REARMED", i))

            # ── arm. Only when not already armed; the elif is production's, and
            # it means a pre-trigger tap is recorded ONLY on a candle that did
            # not arm.
            if not s["armed"]:
                if trigger_touched(s["top"], s["bottom"], direction, chigh, clow,
                                   threshold):
                    s["armed"] = True
                    s["trigger_candle_index"] = i
                    s["trigger_time"] = str(ctime)
                    s["events"].append(("ARMED", i))
                elif pre_arm_tap(direction, entry, chigh, clow):
                    if not s["tapped_before_trigger"]:
                        s["tapped_before_trigger"] = True
                        s["events"].append(("TAPPED_BEFORE_TRIGGER", i))

            # ── fill gate. `can_fill` is unconditionally True under
            # allow_multi_position, and revisit confirmation is inert, so the
            # gate reduces to armed AND delay AND containment-touch.
            ok_delay = s["armed"] and delay_satisfied(
                i, s["trigger_candle_index"], delay)
            if ok_delay and s["delay_satisfied_index"] is None:
                s["delay_satisfied_index"] = i
                s["events"].append(("DELAY_SATISFIED", i))
            if ok_delay and gate_touch(direction, entry, chigh, clow, True):
                s["events"].append(("GATE_READY", i))
                s["terminal"] = GATE_READY
                s["terminal_index"] = i
                continue

            # ── invalidation (execution.py:2958). Runs on every candle the gate
            # did NOT fire on, armed or not: price went CLEAN THROUGH the block,
            # so the setup is dead. Strict `<` / `>`, and against the block's far
            # edge — not the entry.
            #
            # This branch was missed on the first pass and it is not cosmetic:
            # two setups were reported as news-cancelled when production had
            # already invalidated them. It is also the reason the news branch
            # must come first — a blackout candle `continue`s before reaching
            # here, so price can pass through a block during a blackout without
            # invalidating it.
            if invalidated(s["top"], s["bottom"], direction, chigh, clow):
                s["events"].append(("INVALIDATED", i))
                s["terminal"] = INVALIDATED
                s["terminal_index"] = i
                continue
            alive.append(s)
        pending = alive

    for s in pending:
        s["terminal"] = NEVER_FILLED if s["armed"] else NEVER_TRIGGERED

    stats = {
        "execution_candles": len(candles),
        "order_blocks_seen": cursor,
        "setups": len(setups),
        "admitted": sum(1 for s in setups if s["admitted"]),
        "suppressed": sum(1 for s in setups if s["terminal"] == "SUPPRESSED"),
        "armed": sum(1 for s in setups if s["armed"]),
        "tapped_before_trigger": sum(1 for s in setups
                                     if s["tapped_before_trigger"]),
        "news_paused": sum(1 for s in setups if s["news_pause_count"]),
        "gate_ready": sum(1 for s in setups if s["terminal"] == GATE_READY),
        "news_touch_cancel": sum(1 for s in setups
                                 if s["terminal"] == NEWS_TOUCH_CANCEL),
        "invalidated_before_edge_entry": sum(1 for s in setups
                                             if s["terminal"] == INVALIDATED),
        "never_triggered": sum(1 for s in setups
                               if s["terminal"] == NEVER_TRIGGERED),
        "never_filled_after_trigger": sum(1 for s in setups
                                          if s["terminal"] == NEVER_FILLED),
        "max_concurrent_pending": max_concurrent,
        "news_applied": news_on,
    }
    return setups, stats


# ── how much of this survives a 15-minute chart ──────────────────────────────

def decidability_at(setups, minutes: int = 15):
    """Can a chart of `minutes` bars decide the pre-fill path?

    The question is not whether the ARM bar can be found — a 15-minute bar's low
    is the minimum of its minutes' lows, so a 25% penetration inside the bar is
    visible in the bar. It is whether the ORDER of arm, delay expiry and touch
    within one chart bar can be recovered. It cannot: the chart carries four
    numbers per bar and the sequence needs the minutes.

    So this counts the setups whose decisive events fall inside ONE chart bar,
    i.e. the ones a chart of this timeframe would have to guess at.
    """
    same_bar = collapsed_delay = cross_bar = 0
    for s in setups:
        if not s["armed"] or s["terminal_index"] is None:
            continue
        if s["terminal"] not in (GATE_READY, NEWS_TOUCH_CANCEL):
            continue
        arm = s["trigger_candle_index"]
        end = s["terminal_index"]
        if arm // minutes == end // minutes:
            same_bar += 1
        else:
            cross_bar += 1
        # the delay itself is shorter than one chart bar whenever
        # delay_candles < minutes, which is the structural problem
        if s["delay_satisfied_index"] is not None and \
                arm // minutes == s["delay_satisfied_index"] // minutes:
            collapsed_delay += 1
    total = same_bar + cross_bar
    return {
        "chart_minutes": minutes,
        "resolved_setups": total,
        "arm_and_outcome_in_one_chart_bar": same_bar,
        "arm_and_outcome_in_different_chart_bars": cross_bar,
        "delay_expires_inside_the_arm_chart_bar": collapsed_delay,
        "share_undecidable": (same_bar / total) if total else None,
        "_note": "a setup whose arm and outcome share one chart bar cannot be "
                 "resolved from that bar's OHLC — the chart has no way to order "
                 "the events inside it",
    }


# ── self-check against production ────────────────────────────────────────────

#: How production's ONE row per order block maps onto a pre-fill terminal.
#:
#: The default is deliberately GATE_READY — everything production did NOT stop
#: before the gate reached the gate. That makes the mapping fail LOUDLY when a
#: pre-fill branch is missing rather than quietly: an unmapped terminal reads as
#: "reached the gate" and mismatches every setup that hit it, which is exactly
#: how INVALIDATED_BEFORE_EDGE_ENTRY was found.
PRODUCTION_OUTCOME_TERMINAL = {
    "INVALID": INVALIDATED,
}


def _production_terminal(row):
    outcome = str(row.get("outcome") or "")
    if outcome in PRODUCTION_OUTCOME_TERMINAL:
        return PRODUCTION_OUTCOME_TERMINAL[outcome]
    if outcome == "UNFILLED":
        reason = str(row.get("triggered_edge_cancel_reason")
                     or row.get("cancel_reason") or "")
        return NEVER_FILLED if "never_filled" in reason else NEVER_TRIGGERED
    if str(row.get("cancel_reason") or "") == "news_touch_cancel":
        return NEWS_TOUCH_CANCEL
    return GATE_READY


def news_matcher(engine):
    """Production's own calendar test, bound once. The replay never
    re-implements which minutes are inside a blackout — only what the pending
    loop does about it."""
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.news import _news_blackout_match
    return _news_blackout_match


def selfcheck(engine, cfg, obs, candles, news_events, limit_examples=8):
    """Run production's own `simulate_trades` over the same inputs and compare
    the pre-fill terminal of every order block.

    This is the only thing that makes the replay a REFERENCE rather than a second
    opinion. It cannot compare fills, prices or outcomes — those are S8 and later
    — so it compares exactly the question S7 asks: did this setup reach the
    policy boundary, and if not, why not.
    """
    import pandas as pd

    prefill = resolve_prefill_config(cfg)
    match = news_matcher(engine)
    obs_frame = obs if hasattr(obs, "to_dict") else pd.DataFrame(obs)
    obs_records = (obs.to_dict("records") if hasattr(obs, "to_dict")
                   else list(obs))
    ours, stats = replay(obs_records, candles, prefill,
                         news_match=(lambda t: match(t, news_events))
                         if news_events is not None else None)

    frame = pd.DataFrame(candles)
    with _in_lux(Path(engine.lux_root)):
        kwargs = engine.rb.simulation_kwargs(
            cfg, "allow_multi_position", news_events, prefill["stop_buffer"])
        kwargs.update(entry_model="triggered_edge",
                      entry_threshold_pct=prefill["trigger_threshold_pct"],
                      triggered_edge_delay_candles=prefill["delay_candles"],
                      inputs_prepared=True)
        # The policy emitters are what S8 mirrors; leaving them None keeps this
        # comparison on the pre-fill boundary, where a difference can only be an
        # S7 difference.
        kwargs.update(regime_emit=None, portfolio_emit=None,
                      state_policy_emit=None)
        trades = engine.core.simulate_trades(frame, obs_frame, **kwargs)

    theirs = {}
    for row in (trades.to_dict("records") if hasattr(trades, "to_dict")
                else trades):
        theirs[row.get("ob_id")] = _production_terminal(row)

    mismatches = []
    compared = 0
    for s in ours:
        if not s["admitted"]:
            continue
        want = theirs.get(s["ob_id"])
        if want is None:
            mismatches.append({"ob_id": s["ob_id"], "ours": s["terminal"],
                               "production": "<no row>"})
            continue
        compared += 1
        if want != s["terminal"]:
            mismatches.append({"ob_id": s["ob_id"], "ours": s["terminal"],
                               "production": want,
                               "detection_time": s["detection_time"],
                               "armed": s["armed"]})

    return {
        "compared": compared,
        "mismatches": len(mismatches),
        "examples": mismatches[:limit_examples],
        "stats": stats,
        "production_rows": len(theirs),
        "decidability": decidability_at(ours, 15),
        "config": prefill,
    }


def _load(engine, cfg, start=None, end=None, extension=None):
    """Load the production execution and detection frames, honouring an optional
    window so a self-check can run on a slice.

    `extension` is a provenanced snapshot of the LIVE 1-minute segment (see
    `tools.oracle.snapshot_execution_feed`). Production's frozen candle file
    ends months before the running node does, so without it the recording
    cannot contain a recent trade at all. The seam rule is production's own,
    from `live_state/market_data/provenance.json`:

        frozen rows win at or before the frozen end; live rows win after

    `filter_date_range` is applied to the FROZEN part only. It clips to
    `cfg.end_date`, which is the frozen file's own end, and applying it to the
    extension would throw away exactly the rows the extension exists to add.

    Returns (prepared, obs, news, seam) where `seam` is None for a frozen-only
    load and otherwise records where the feed changes — a fact the recording
    must publish rather than blend away.
    """
    import pandas as pd

    lux = Path(engine.lux_root)
    seam = None
    with _in_lux(lux):
        raw = engine.rb.load_candles(lux / "data" / "candles" / cfg.candle_file)
        raw = engine.rb.filter_date_range(raw, cfg)
        if extension is not None and len(extension):
            ext = extension.copy()
            ext["time"] = pd.to_datetime(ext["time"], utc=True)
            frozen_end = pd.to_datetime(raw["time"], utc=True).max()
            after = ext[ext["time"] > frozen_end]
            if len(after):
                cols = [c for c in raw.columns if c in after.columns]
                after = after[cols]
                if "volume" in raw.columns and "volume" not in after.columns:
                    after = after.assign(volume=0.0)
                raw = pd.concat([raw, after[raw.columns]], ignore_index=True)
                seam = {"frozen_end_ms": int(frozen_end.value // 1_000_000),
                        "frozen_end": str(frozen_end),
                        "live_rows": int(len(after)),
                        "live_last": str(after["time"].max())}
        prepared = engine.core.prepare_candles_for_simulation(raw)
        if start:
            prepared = prepared[prepared["time"] >= pd.Timestamp(start, tz="UTC")]
        if end:
            prepared = prepared[prepared["time"] < pd.Timestamp(end, tz="UTC")]
        prepared = prepared.reset_index(drop=True)
        det = engine.rb.resample_candles(prepared, cfg.detection_timeframe)
        obs = engine.rb.detect_order_blocks(
            det, swing_length=cfg.swing_length, ob_filter=cfg.ob_filter,
            pip_size=cfg.pip_size, min_ob_size_pips=cfg.min_ob_size_pips,
            max_ob_size_pips=cfg.max_ob_size_pips)
        obs = engine.rb.filter_order_blocks_by_structure(obs, cfg.structure_filter)
        obs = engine.core.prepare_order_blocks_for_simulation(obs)
        news = engine.rb.load_news_events(cfg) if cfg.news_blackout_enabled else None
    return prepared, obs, news, seam


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--selfcheck", action="store_true")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args(argv)

    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)

    prepared, obs, news, _seam = _load(engine, cfg, args.start, args.end)
    print(f"execution candles : {len(prepared):,}  "
          f"({prepared['time'].iloc[0]} .. {prepared['time'].iloc[-1]})")
    print(f"order blocks      : {len(obs):,}")
    print(f"news events       : {0 if news is None else len(news):,}")

    rep = selfcheck(engine, cfg, obs, prepared.to_dict("records"), news)
    print()
    for k, v in rep["stats"].items():
        print(f"  {k:32s} {v}")
    print()
    print(f"  compared setups                  {rep['compared']:,}")
    print(f"  mismatches vs production         {rep['mismatches']}")
    for ex in rep["examples"]:
        print(f"      {ex}")
    d = rep["decidability"]
    print()
    print(f"  15m-undecidable setups           "
          f"{d['arm_and_outcome_in_one_chart_bar']}/{d['resolved_setups']}"
          + (f"  ({d['share_undecidable']:.1%})" if d["share_undecidable"]
             is not None else ""))

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, indent=1, sort_keys=True, default=str)
                            + "\n", encoding="utf-8")
        print(f"\nwritten: {args.out}")
    return 0 if rep["mismatches"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
