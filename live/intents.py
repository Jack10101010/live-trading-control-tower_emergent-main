"""Deterministic, idempotent OrderIntents from the frontier diff.

Mirror model (per design §2.1-B): the frozen engine is the ONLY state machine.
The broker mirrors engine transitions at the frontier bar:
  engine fill at frontier      -> OPEN_POSITION (market, with engine stop/target)
  engine exit at frontier      -> CLOSE_POSITION
  engine stop move (BE/RR)     -> MODIFY_STOP
Intent identity = sha1(instance|trade_id|transition|frontier_bar) — replaying a
diff regenerates byte-identical ids, making duplicate suppression exact.
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from live import INSTANCE_ID

OPEN_POSITION = "OPEN_POSITION"
CLOSE_POSITION = "CLOSE_POSITION"
MODIFY_STOP = "MODIFY_STOP"

# Engine outcome spellings (verified against strategy_core.execution):
_DECIDED = {"WIN", "LOSS"}
_EXITED = {"WIN", "LOSS", "NEWS_FLATTEN", "PROTECTION_EXIT", "BE_EXIT"}


@dataclass(frozen=True)
class OrderIntent:
    intent_id: str
    action: str            # OPEN_POSITION | CLOSE_POSITION | MODIFY_STOP
    trade_id: str
    side: str              # long | short
    frontier_bar: str
    entry: float | None = None
    stop: float | None = None
    target: float | None = None
    reason: str = ""
    # Engine-realised R (cost-inclusive `net_r`), carried on CLOSE_POSITION only.
    # This is the sole production source for the daily-loss counter — the engine
    # is the state machine, so its own realised R is authoritative rather than a
    # broker P&L round-trip that the mirror model never performs.
    realized_r: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _intent_id(trade_id: str, transition: str, frontier_bar: str) -> str:
    return hashlib.sha1(f"{INSTANCE_ID}|{trade_id}|{transition}|{frontier_bar}".encode()).hexdigest()[:20]


def _row_map(frame) -> dict:
    return {str(r["trade_id"]): r for r in frame.to_dict("records")} if frame is not None else {}


def _side(row) -> str:
    return "long" if str(row.get("direction", "")) == "bullish" else "short"


def _f(row, key):
    v = row.get(key, None)
    try:
        f = float(v)
        return f if f == f else None
    except (TypeError, ValueError):
        return None


SKIP_INTRA_WINDOW = "SKIP_INTRA_WINDOW"   # filled AND exited inside one 15m window


def diff_frontier(prev_frame, cur_frame, frontier_bar: str) -> list[OrderIntent]:
    """Ordered intents for engine transitions between consecutive frames.

    Because consecutive frames differ ONLY by the new 15m window's candles
    (identical inputs otherwise), every row transition IS frontier activity —
    fills occur on 1m candles inside the window, so transitions are keyed on
    state change, not on timestamp equality. Deterministic: iterates cur_frame
    row order (the engine's own candidate ordering); one intent per
    (trade, transition); `frontier_bar` labels the intent identity."""
    prev = _row_map(prev_frame)
    intents: list[OrderIntent] = []
    for row in (cur_frame.to_dict("records") if cur_frame is not None else []):
        tid = str(row["trade_id"])
        before = prev.get(tid)
        outcome = str(row.get("outcome", ""))
        fill_time = str(row.get("fill_time", "") or "")
        was_outcome = str(before.get("outcome", "")) if before else ""
        was_filled = bool(before) and bool(str(before.get("fill_time", "") or ""))

        # 1) newly filled since the previous frame
        if fill_time and not was_filled:
            if outcome in _EXITED:
                # filled AND exited within the same window: cannot be mirrored
                # at 15m cadence — recorded, never sent to the broker.
                intents.append(OrderIntent(
                    intent_id=_intent_id(tid, f"intra_{outcome}", frontier_bar),
                    action=SKIP_INTRA_WINDOW, trade_id=tid, side=_side(row),
                    frontier_bar=frontier_bar, reason=f"intra_window_{outcome.lower()}"))
            else:
                intents.append(OrderIntent(
                    intent_id=_intent_id(tid, "fill", frontier_bar), action=OPEN_POSITION,
                    trade_id=tid, side=_side(row), frontier_bar=frontier_bar,
                    entry=_f(row, "entry"), stop=_f(row, "stop"), target=_f(row, "tp"),
                    reason="engine_fill"))
            continue

        # 2) open position exited since the previous frame
        if before and was_filled and was_outcome not in _EXITED and outcome in _EXITED:
            intents.append(OrderIntent(
                intent_id=_intent_id(tid, f"exit_{outcome}", frontier_bar), action=CLOSE_POSITION,
                trade_id=tid, side=_side(row), frontier_bar=frontier_bar,
                reason=f"engine_exit_{outcome.lower()}", realized_r=_f(row, "net_r")))
            continue

        # 3) stop moved on a still-open position (BE / move-stop)
        if before and was_filled and outcome not in _EXITED:
            old_stop, new_stop = _f(before, "stop"), _f(row, "stop")
            if old_stop is not None and new_stop is not None and new_stop != old_stop:
                intents.append(OrderIntent(
                    intent_id=_intent_id(tid, f"stop_{new_stop!r}", frontier_bar), action=MODIFY_STOP,
                    trade_id=tid, side=_side(row), frontier_bar=frontier_bar,
                    stop=new_stop, reason="engine_stop_move"))
    return intents
