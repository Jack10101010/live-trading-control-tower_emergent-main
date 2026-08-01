"""Model B — canonical index semantics for carried live state.

THE PROBLEM THIS EXISTS TO SOLVE
--------------------------------
The bar loop indexes candles positionally. `candle_index` is the offset into
`ctx['candle_records']`, which is the CURRENT frame. Under the canonical
full-history engine there is only ever one frame, so a positional index is
effectively absolute and nothing notices.

Model B rolls a two-year window forward, so the frame is rebuilt every cycle and
`candle_index` restarts near zero. Carried state, however, stores indices taken
from the PREVIOUS frame, and the engine compares the two directly:

    strategy_core/execution.py
      2566  active_trade["_be_stop_active_from"] = candle_index + int(be_delay_candles)
      2568  if row.get("be_armed") and candle_index >= active_trade.get("_be_stop_active_from", 0)
      2620  candle_index >= item.get("trigger_candle_index") + item.get("trigger_delay_candles", 0)
      2691  candle_index >= _cr_trig_ci + _cr_delay
      3125  candle_index > item.get("tapped_candle_index", -1) + 1
      1369  row["bars_to_fill"] = candle_index - item["created_candle_index"]
      1531  row["bars_to_exit"] = candle_index - row["fill_candle_index"]

Every one of those is a comparison or difference between a STORED index and the
LIVE index. Shift the frame and each is wrong by exactly the shift — silently.
A triggered-edge delay of 3 candles evaluated against a frame shifted by 15 fires
immediately or never. That is a changed trading decision, not a rounding error.

THE FIX, AND WHY IT CHANGES NO TRADING RULE
-------------------------------------------
Every affected expression is either a difference of two indices or a comparison
of (stored + delay) against the live index. All of them are INVARIANT under
adding the same constant to both sides. So the rule needs no change at all: the
carried indices simply have to be expressed in the same origin as the frame they
are evaluated against.

Carried state therefore stores ABSOLUTE ordinals — positions in the canonical,
gap-inclusive M1 series — and rebases them to frame-local on restore:

    local = absolute - frame_origin_ordinal

Timestamps were rejected as the primary representation because the delays are
counted in CANDLES, not minutes, and the series has weekend and holiday gaps:
"3 candles later" is not "3 minutes later", so any clock-derived value would have
to be mapped back through the candle series anyway. Timestamps are still carried
for IDENTITY (they are stable, human-checkable, and survive re-indexing), which
makes this the hybrid of Phase 2 option D: timestamps identify, ordinals count.

SENTINELS ARE NOT ORDINALS. `tapped_candle_index` defaults to -1 and
`_be_stop_active_from` to 0, both meaning "unset". Rebasing a sentinel would
turn it into a real position and silently arm or disarm logic. They are excluded
explicitly rather than by hoping the arithmetic works out.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: Fields in carried state whose value is a position in the candle frame.
#: Sourced from the reader audit above; every one is compared against the live
#: `candle_index`, so every one must be rebased together or none of them.
PENDING_INDEX_FIELDS = (
    "created_candle_index",
    "trigger_candle_index",
    "tapped_candle_index",
    "revisit_touch_candle_index",
    "revisit_confirm_candle_index",
)
ACTIVE_TRADE_INDEX_FIELDS = (
    "fill_candle_index",
    "_be_stop_active_from",
    "be_arm_candle_index",
    "exit_candle_index",
)

#: Values that mean "unset" and must NEVER be shifted. -1 is the documented
#: default for `tapped_candle_index`; 0 is the default in the
#: `_be_stop_active_from` lookup. Empty string appears in the revisit fields,
#: which are initialised to "" rather than to a number.
SENTINELS = (None, "", -1)


class IndexRebaseError(ValueError):
    """A carried index cannot be expressed in the target frame.

    Raised rather than clamped: a negative local index would mean the carried
    state refers to a candle the window no longer contains, and silently
    clamping it to 0 would fabricate a delay that already expired.
    """


@dataclass
class FrameOrigin:
    """Maps between absolute ordinals and frame-local indices.

    `origin_ordinal` is the absolute ordinal of the frame's FIRST candle, so
    local 0 corresponds to it. `length` bounds the frame for validation.
    """

    origin_ordinal: int
    length: int
    first_time: str = ""
    last_time: str = ""

    def to_local(self, absolute: int) -> int:
        local = int(absolute) - self.origin_ordinal
        if local < 0:
            raise IndexRebaseError(
                f"absolute ordinal {absolute} precedes the frame origin "
                f"{self.origin_ordinal} — the window no longer contains that candle; "
                "the carried state must be rebuilt from the oracle, not clamped")
        if local >= self.length:
            raise IndexRebaseError(
                f"absolute ordinal {absolute} is beyond the frame end "
                f"({self.origin_ordinal + self.length - 1})")
        return local

    def to_absolute(self, local: int) -> int:
        return self.origin_ordinal + int(local)


def _map_fields(record: dict, fields: tuple, fn) -> dict:
    out = dict(record)
    for name in fields:
        if name not in out:
            continue
        value = out[name]
        if value in SENTINELS or isinstance(value, bool):
            continue                      # sentinel / not a position
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            continue                      # "" and other non-positional values
        out[name] = fn(numeric)
    return out


def pending_to_absolute(item: dict, origin: FrameOrigin) -> dict:
    return _map_fields(item, PENDING_INDEX_FIELDS, origin.to_absolute)


def pending_to_local(item: dict, origin: FrameOrigin) -> dict:
    return _map_fields(item, PENDING_INDEX_FIELDS, origin.to_local)


def active_trade_to_absolute(trade: dict, origin: FrameOrigin) -> dict:
    return _map_fields(trade, ACTIVE_TRADE_INDEX_FIELDS, origin.to_absolute)


def active_trade_to_local(trade: dict, origin: FrameOrigin) -> dict:
    return _map_fields(trade, ACTIVE_TRADE_INDEX_FIELDS, origin.to_local)


@dataclass
class LiveState:
    """The five carried SimState fields plus the frame binding.

    `trades` and `profile` are deliberately absent: `trades` is an append-only
    output stream never read inside the loop (already persisted as the diff
    frame), and `profile` is diagnostics.
    """

    pending: list = field(default_factory=list)
    active_trades: list = field(default_factory=list)
    ghost_states: dict = field(default_factory=dict)
    ob_cursor: int = 0
    be_applied_any: bool = False
    last_boundary: str = ""
    #: Absolute ordinal of the candle the state was last advanced to. Stored so a
    #: restore into a shifted frame can be validated rather than assumed.
    last_candle_ordinal: int = -1

    def to_absolute(self, origin: FrameOrigin) -> "LiveState":
        return LiveState(
            pending=[pending_to_absolute(p, origin) for p in self.pending],
            active_trades=[active_trade_to_absolute(t, origin) for t in self.active_trades],
            ghost_states=dict(self.ghost_states), ob_cursor=self.ob_cursor,
            be_applied_any=self.be_applied_any, last_boundary=self.last_boundary,
            last_candle_ordinal=self.last_candle_ordinal)

    def to_local(self, origin: FrameOrigin) -> "LiveState":
        return LiveState(
            pending=[pending_to_local(p, origin) for p in self.pending],
            active_trades=[active_trade_to_local(t, origin) for t in self.active_trades],
            ghost_states=dict(self.ghost_states), ob_cursor=self.ob_cursor,
            be_applied_any=self.be_applied_any, last_boundary=self.last_boundary,
            last_candle_ordinal=self.last_candle_ordinal)
