"""Short-window validation for the Strategy Tester companion.

    python -m tools.oracle.companion_backtest --start 2026-06-23 --end 2026-08-01

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
It is a PREDICTION of what TradingView's Strategy Tester will report for
`pine/generated/tradingview_strategy_companion_15m.pine`, produced by running
the same lifecycle rules over the same 15-minute bars in Python, together with
a model of TradingView's broker emulator.

It is NOT TradingView's output. Nothing here has been near TradingView. Two
things could still differ on the chart:

  * THE FEED. TradingView builds its own 15-minute bars from its own broker's
    1-minute quotes; this runs on the frozen+live MT5 series. A one-tick
    difference in a bar's low is enough to arm a setup here and not there.
  * THE EMULATOR. The four-price intrabar assumption below is TradingView's
    DOCUMENTED behaviour, not its source code.

So the numbers below are the expectation this build was written to meet. Where
the chart disagrees, the chart is the observation and this is the hypothesis.

WHY BOTH MODES RUN OVER THE SAME OBJECTS
----------------------------------------
The two modes are not two strategies. They differ in exactly one decision — what
to do with a bar whose range both touches the entry edge and breaches the far
edge — so they are run over identical detections, identical bars and identical
gates, and the difference in the trade list is therefore attributable to that
one decision and to nothing else.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle import replay_prefill as rp
from tools.oracle import replay_regime
from tools.oracle import target_table as tt
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

DEFAULT_OUT = (CT_ROOT / "artifacts" / "tradingview_oracle"
               / "companion_backtest.json")

STRICT = "STRICT / PROVABLE"
PRACTICAL = "PRACTICAL / TV EMULATOR"

#: `pine/src/s10_inputs.pinefrag`, `i_maxWaitBars`.
MAX_WAIT_BARS = 120
#: `strategy(commission_value = ...)`, cash per contract PER SIDE.
COMMISSION_PER_SIDE = 0.00003


def four_price_path(o: float, h: float, l: float, c: float) -> list[float]:
    """TradingView's intrabar assumption, which is the whole ballgame.

    The emulator does not know the order of ticks inside a bar either. Its
    documented rule is: an UP bar moved open -> low -> high -> close, a DOWN bar
    moved open -> high -> low -> close. On an up bar a long therefore meets its
    stop before its target, and vice versa — conservative, and, more
    importantly, DECIDED, which is the property this whole exercise is about.

    Every trade whose stop and target both fall inside one bar is resolved by
    this assumption and by nothing observable, so each one is counted.
    """
    return [o, l, h, c] if c >= o else [o, h, l, c]


def _crosses(a: float, b: float, level: float) -> bool:
    return min(a, b) <= level <= max(a, b)


def walk(path: list[float], level: float) -> int:
    """Index of the first path segment that reaches `level`, or -1."""
    for i in range(len(path) - 1):
        if _crosses(path[i], path[i + 1], level):
            return i
    return -1


class Setup:
    """One order block, tracked exactly as `ScOb` in s90_strategy.pinefrag."""

    def __init__(self, ob_id, top, bottom, is_long, is_choch, born, trig, buf):
        depth = top - bottom
        self.id = ob_id
        self.top, self.bottom = top, bottom
        self.is_long, self.is_choch = is_long, is_choch
        self.entry = top if is_long else bottom
        self.arm = (top - depth * trig / 100.0 if is_long
                    else bottom + depth * trig / 100.0)
        self.stop = bottom - buf if is_long else top + buf
        self.born = born
        self.armed_bar = None
        self.arm_bar_straddled = False
        self.working = False          # an order is live for the NEXT bar
        self.order = None             # ("stop"|"limit", price, rr)
        self.opp_counted = False
        self.emu_dependent = False
        self.fill_bar_broke = False
        self.done = False


def simulate(mode, obs, bars, cell, buf, trig, delay_note, window=None):
    """The per-bar pass of s90, plus TradingView's side of the contract."""
    n = dict(detected=0, armed=0, invalidated=0, undecidable=0, blocked=0,
             news_blocked=0, orders=0, filled=0, expired=0, delayed=0,
             omitted=0, emulator=0, opposite=0, ambiguous_exit=0, gap_fill=0,
             fill_bar_also_broke=0)
    trades, live, open_pos = [], [], []

    idx = list(bars.index)
    by_bar = {}
    for ob in obs:
        by_bar.setdefault(ob["born"], []).append(ob)

    for b in range(len(idx)):
        row = bars.iloc[b]
        o, h, l, c = (float(row.open), float(row.high),
                      float(row.low), float(row.close))
        path = four_price_path(o, h, l, c)

        # ── 1. THE BROKER'S TURN, before the script's ────────────────────────
        # An order placed at the close of bar b-1 is live for the whole of bar
        # b. `process_orders_on_close = false` is what makes that true, and it
        # is what keeps the 3-minute delay from ever being violated.
        broke = {}
        for s in live:
            if s.done or s.order is None:
                continue
            broke[s.id] = (l < s.bottom) if s.is_long else (h > s.top)
            kind, price, rr = s.order
            # A GAP is tested BEFORE the intrabar walk, not after it. A bar that
            # opened beyond the level may never trade AT the level at all, so
            # looking for the level inside the bar first misses exactly the
            # case the gap rule exists for — and silently, as a non-fill.
            gapped = ((kind == "stop" and (o > price if s.is_long else o < price))
                      or (kind == "limit"
                          and (o < price if s.is_long else o > price)))
            seg = 0 if gapped else walk(path, price)
            if seg < 0:
                continue
            fill = o if gapped else price
            if gapped:
                n["gap_fill"] += 1
            n["filled"] += 1
            s.done = True
            s.working = False
            tgt = (s.entry + abs(s.entry - s.stop) * rr if s.is_long
                   else s.entry - abs(s.entry - s.stop) * rr)
            if broke.get(s.id):
                n["fill_bar_also_broke"] += 1
                s.fill_bar_broke = True
            open_pos.append({"setup": s, "fill": fill, "target": tgt, "rr": rr,
                             "fill_bar": b, "seg": seg})

        # ── 2. open positions resolve, on the same bar if the path says so ───
        still = []
        for p in open_pos:
            s = p["setup"]
            start = p["seg"] if p["fill_bar"] == b else 0
            sub = path[start:]
            hit_stop, hit_tgt = walk(sub, s.stop), walk(sub, p["target"])
            if hit_stop >= 0 and hit_tgt >= 0:
                n["ambiguous_exit"] += 1
            if hit_stop < 0 and hit_tgt < 0:
                still.append(p)
                continue
            won = hit_tgt >= 0 and (hit_stop < 0 or hit_tgt < hit_stop)
            risk = abs(s.entry - s.stop)
            gross = p["rr"] if won else -1.0
            cost_r = 2 * COMMISSION_PER_SIDE / risk if risk > 0 else 0.0
            trades.append({
                "id": s.id, "dir": "Long" if s.is_long else "Short",
                "structure": "CHoCH" if s.is_choch else "BOS",
                "fill_time": str(idx[p["fill_bar"]]), "exit_time": str(idx[b]),
                "entry": p["fill"], "stop": s.stop, "target": p["target"],
                "rr": p["rr"], "risk_price": risk,
                "gross_r": gross, "cost_r": cost_r, "net_r": gross - cost_r,
                "outcome": "WIN" if won else "LOSS",
                "bars_held": b - p["fill_bar"],
                "emu_dependent": s.emu_dependent,
                "fill_bar_broke": s.fill_bar_broke,
                "ambiguous": hit_stop >= 0 and hit_tgt >= 0})
        open_pos = still

        # ── 3. THE SCRIPT'S TURN, at the close of bar b ──────────────────────
        for ob in by_bar.get(b, []):
            n["detected"] += 1
            live.append(Setup(ob["id"], ob["top"], ob["bottom"], ob["is_long"],
                              ob["is_choch"], b, trig, buf))

        keep = []
        for s in live:
            if s.done:
                continue
            armed = s.armed_bar is not None
            hit_arm = l <= s.arm if s.is_long else h >= s.arm
            through = l < s.bottom if s.is_long else h > s.top
            straddle = l <= s.entry <= h

            if not armed and b > s.born and hit_arm and not through:
                s.armed_bar = b
                s.arm_bar_straddled = straddle
                armed = True
                n["armed"] += 1
                # Production's fill could have been INSIDE this bar; a bar that
                # has already closed cannot be traded by either mode. The order
                # placed instead waits for a LATER bar to revisit the edge —
                # same price, different hour, so a different session, a
                # different market state and possibly a different matrix cell.
                if straddle:
                    n["delayed"] += 1
                    if mode == STRICT:
                        n["omitted"] += 1
                    else:
                        n["emulator"] += 1
                        s.emu_dependent = True

            if through and not straddle:
                n["invalidated"] += 1
                s.working, s.order = False, None
                continue

            # NEITHER MODE CAN TRADE THIS ONE. The broker's turn is above, so
            # if a live order could fill on this bar it already has. Reaching
            # here means no order was live — and the block is broken, which
            # production reads as INVALIDATED_BEFORE_EDGE_ENTRY unless its fill
            # came first. M15 cannot say which, so neither mode claims either.
            if through and straddle:
                n["undecidable"] += 1
                s.working, s.order = False, None
                continue

            # STRICT's one refusal, after the arm is counted: the arm is real,
            # and reporting "never armed" would understate what production had.
            if mode == STRICT and s.arm_bar_straddled:
                s.working, s.order = False, None
                continue

            rr, ok, sess, state, news_ok, news_known = cell(idx[b], s)
            eligible = ok and news_ok and rr is not None

            if armed and b - s.armed_bar > MAX_WAIT_BARS:
                n["expired"] += 1
                s.working, s.order = False, None
                continue

            in_window = window is None or (window[0] <= idx[b] < window[1])
            if armed and eligible and in_window:
                use_stop = c < s.entry if s.is_long else c > s.entry
                if not s.working:
                    n["orders"] += 1
                s.working = True
                s.order = ("stop" if use_stop else "limit", s.entry, rr)
            elif s.working:
                s.working, s.order = False, None
                if not ok:
                    n["blocked"] += 1
                if not news_ok:
                    n["news_blocked"] += 1

            if s.working and not s.opp_counted and open_pos:
                if any(p["setup"].is_long != s.is_long for p in open_pos):
                    s.opp_counted = True
                    n["opposite"] += 1
            keep.append(s)
        live = keep

    return n, trades, open_pos


def build(engine, cfg, start, end):
    import pandas as pd

    ext_path = (CT_ROOT / "artifacts" / "tradingview_oracle"
                / "execution_feed_extension.csv")
    ext = pd.read_csv(ext_path) if ext_path.is_file() else None
    prepared, obs, news, _seam = rp._load(engine, cfg, None, None,
                                          extension=ext)

    lo, hi = pd.Timestamp(start, tz="UTC"), pd.Timestamp(end, tz="UTC")

    with _in_lux(Path(engine.lux_root)):
        from strategy_core.sessions import (_SESSION_OUTSIDE, _SESSION_SCHEDULE,
                                            _london_hour, _session_for_hour)
        detection = engine.rb.resample_candles(prepared, cfg.detection_timeframe)
        _daily, panel = replay_regime.replay(
            engine.core, [str(t) for t in detection["time"]],
            detection["open"].tolist(), detection["high"].tolist(),
            detection["low"].tolist(), detection["close"].tolist(),
            None, "EURUSD")

    state_by_day = {r["date"]: (r.get("market_state"), bool(r.get("confirmed")))
                    for r in panel}
    if not any("-" in k for k in state_by_day):
        raise RuntimeError("panel dates are not dates — every state lookup "
                           "would miss and every cell would fall back to base")

    table = tt.build(engine, cfg)
    bars = detection.set_index(pd.to_datetime(detection["time"], utc=True))
    bars = bars[(bars.index >= lo) & (bars.index < hi)]
    idx = list(bars.index)

    frame = pd.DataFrame(obs)
    frame["detection_time"] = pd.to_datetime(frame["detection_time"], utc=True)
    win = frame[(frame.detection_time >= lo) & (frame.detection_time < hi)]

    pos = {t: i for i, t in enumerate(idx)}
    ob_rows = []
    for k, ob in enumerate(win.itertuples(), 1):
        b = pos.get(ob.detection_time)
        if b is None:
            continue
        ob_rows.append({"id": f"OB{k}", "top": float(ob.top),
                        "bottom": float(ob.bottom),
                        "is_long": ob.direction == "bullish",
                        "is_choch": "choch" in str(ob.structure_tag).lower(),
                        "born": b,
                        "detected": ob.detection_time.strftime("%Y-%m-%d %H:%M")})

    # ── news, on the same minute grid the Pine walks ────────────────────────
    #
    # DERIVED, NOT ASSUMED. The embedded schedule has a first and a last event,
    # and a bar outside that span is UNKNOWN — a missing input, not a refusal,
    # so it never blocks and is counted instead. Hard-coding "news never blocks
    # in this window" would have been true today and silently wrong the moment
    # the schedule is refreshed, which is exactly when it would matter.
    # The BLACKOUT WINDOWS come with the events. Production widens each event
    # by `news_blackout_minutes_before/after` when it loads them and stores the
    # result as `window_start`/`window_end`; re-deriving the widening here from
    # config would be a second implementation of a rule that already exists, and
    # the first version of this function got the two field names wrong and
    # silently produced ZERO-WIDTH windows that blocked almost nothing.
    win_ms = []
    for ev in (news or []):
        if isinstance(ev, dict) and "window_start" in ev:
            win_ms.append((pd.Timestamp(ev["window_start"], tz="UTC"),
                           pd.Timestamp(ev["window_end"], tz="UTC")))
    if win_ms:
        assert any(b > a for a, b in win_ms), (
            "every blackout window is zero-width — the widening was lost")
    news_first = min((a for a, _ in win_ms), default=None)
    news_last = max((b for _, b in win_ms), default=None)

    def news_state(bar_start):
        """(blocks, known) for the 15 minutes beginning at `bar_start`."""
        if not win_ms:
            return False, False
        bar_end = bar_start + pd.Timedelta(minutes=15)
        for a, b in win_ms:
            if a < bar_end and b > bar_start:
                return True, True
        known = news_first <= bar_start and bar_end <= news_last
        return False, known

    def cell(ts, s):
        key = _session_for_hour(_london_hour(ts))[1]
        state, conf = state_by_day.get(ts.strftime("%Y-%m-%d"), (None, False))
        rr, _src, ok, _why = tt.resolve(
            table, key, "CHoCH" if s.is_choch else "BOS",
            "Long" if s.is_long else "Short", state, conf)
        blocks, known = news_state(ts)
        return rr, ok, key, (state or "(warmup)"), not blocks, known

    buf = float(cfg.stop_buffer_pips) * float(cfg.pip_size)
    trig = float(getattr(cfg, "triggered_edge_entry_threshold_pct", 25.0) or 25.0)

    covered = bool(win_ms) and news_first is not None and (
        news_first <= lo and hi <= news_last)
    out = {"window": {"start": start, "end": end, "bars": len(idx)},
           "detections": len(ob_rows),
           "news": {"events": len(win_ms),
                    "first": str(news_first), "last": str(news_last),
                    "window_covered": covered},
           "modes": {}}
    for mode in (STRICT, PRACTICAL):
        # The Pine has the same gate as an input; the harness applies it here
        # so the two are asked the same question. Without it the harness would
        # be predicting a different backtest from the one the chart will run.
        n, trades, still_open = simulate(mode, ob_rows, bars, cell, buf, trig,
                                         None, window=(lo, hi))
        wins = [t for t in trades if t["outcome"] == "WIN"]
        losses = [t for t in trades if t["outcome"] == "LOSS"]
        gross = sum(t["net_r"] for t in trades)
        up = sum(t["net_r"] for t in wins)
        down = -sum(t["net_r"] for t in losses)
        eq, peak, dd = 0.0, 0.0, 0.0
        for t in trades:
            eq += t["net_r"]
            peak = max(peak, eq)
            dd = min(dd, eq - peak)
        out["modes"][mode] = {
            "counters": n, "trades": trades, "still_open": len(still_open),
            "wins": len(wins), "losses": len(losses),
            "net_r": round(gross, 4),
            "profit_factor": (round(up / down, 4) if down > 0
                              else (None if up == 0 else float("inf"))),
            "max_drawdown_r": round(dd, 4),
            "rr_distribution": sorted({t["rr"] for t in trades}),
        }
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-06-23")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--write", action="store_true")
    a = ap.parse_args(argv)

    try:
        engine = load_engine()
        cfg, _ = resolve_config(engine)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    out = build(engine, cfg, a.start, a.end)
    if a.write:
        DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
        DEFAULT_OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")

    w = out["window"]
    print(f"window {w['start']} .. {w['end']}   {w['bars']} bars   "
          f"{out['detections']} detections")
    nw = out["news"]
    if not nw["window_covered"]:
        print(f"  NEWS: schedule runs {str(nw['first'])[:16]} .. "
              f"{str(nw['last'])[:16]} and does NOT cover this window, so news "
              f"is UNKNOWN throughout.\n        UNKNOWN never blocks — a news "
              f"count of zero here means UNEVALUATED, not clear.")
    for mode, m in out["modes"].items():
        n = m["counters"]
        print(f"\n=== {mode} ===")
        print(f"  detected {n['detected']}   armed {n['armed']}   "
              f"orders {n['orders']}   filled {n['filled']}   "
              f"still open {m['still_open']}")
        print(f"  invalidated {n['invalidated']}   undecidable "
              f"{n['undecidable']}   expired {n['expired']}")
        print(f"  blocked(cohort) {n['blocked']}   blocked(news) "
              f"{n['news_blocked']}   opposite-dir {n['opposite']}")
        print(f"  arm bar reached the entry {n['delayed']}   "
              f"omitted(STRICT) {n['omitted']}   "
              f"emulator-dependent(PRACTICAL) {n['emulator']}")
        print(f"  ambiguous exits {n['ambiguous_exit']}   "
              f"gap fills {n['gap_fill']}   "
              f"fill bar also broke the far edge {n['fill_bar_also_broke']}")
        print(f"  wins {m['wins']}   losses {m['losses']}   "
              f"net {m['net_r']:+.3f}R   PF {m['profit_factor']}   "
              f"maxDD {m['max_drawdown_r']:.3f}R")
        print(f"  RR values used: {m['rr_distribution']}")
        for t in m["trades"]:
            print(f"    {t['fill_time'][:16]}  {t['dir']:<5} {t['structure']:<5}"
                  f" rr {t['rr']:.1f}  {t['outcome']:<4} "
                  f"net {t['net_r']:+.3f}R  cost {t['cost_r']:.4f}R"
                  f"{'  [fill bar also BROKE the block]' if t['fill_bar_broke'] else ''}"
                  f"{'  [emulator-dependent fill]' if t['emu_dependent'] else ''}"
                  f"{'  [emulator-ordered exit]' if t['ambiguous'] else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
