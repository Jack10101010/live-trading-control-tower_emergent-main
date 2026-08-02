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

# ── TWO ORDINAL SPACES, NOT ONE ──────────────────────────────────────────────
# The recursive audit found positional fields living in two DIFFERENT frames:
#
#   M1  — `candle_index` and everything compared against it. This is the walk's
#         frame (`ctx['candle_records']`) and the one a rolling window rebases.
#   M15 — `origin_index`, `detection_index`, `pivot_index` on an order-block
#         record. These are positions in the DETECTION frame produced by
#         `resample_candles(...)`, which has ~1/15th the rows.
#
# Rebasing both with one origin would corrupt the order-block records silently.
# They are therefore registered separately, and M15 fields are NOT rebased at
# all: nothing in the bar loop compares them against `candle_index` — they are
# provenance carried through to the trade row.
SPACE_M1 = "m1"
SPACE_M15_DETECTION = "m15_detection"

#: NOT positional. Counts and deltas are invariant under a frame shift, so
#: rebasing them would actively break arithmetic that is currently correct.
#: Listed explicitly because several are named like positions
#: (`fill_delay_candles_val`, `te_delay`) and a name-shaped heuristic would eat
#: them. This is why conversion uses a registry and never guesses from shape.
NON_POSITIONAL_COUNTS = (
    "trigger_delay_candles", "be_delay_candles", "fill_delay_candles_val",
    "te_delay", "_delay_remaining", "bars_to_fill", "bars_to_exit",
    "ghost_fill_delay_candles", "swing_length", "pivot_index_offset",
)

#: Fields in carried state whose value is a position in the M1 candle frame.
#: Sourced from the reader audit; every one is compared against the live
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
#: NESTED. An active trade carries the emitted trade `row`, which holds eight
#: positional fields of its own. The top-level audit missed these entirely --
#: they only surface by walking into active_trade["row"].
TRADE_ROW_INDEX_FIELDS = (
    "fill_candle_index", "exit_candle_index", "arm_candle_index",
    "be_arm_candle_index", "be_exit_candle_index", "ob_exit_candle_index",
    "tapped_candle_index", "trigger_candle_index",
)
#: NESTED. GhostState keeps its own positions.
GHOST_INDEX_FIELDS = ("fill_candle_index", "trigger_candle_index")

#: Order-block provenance in the M15 detection frame. Registered so the seam can
#: assert it KNOWS about them and deliberately leaves them alone, rather than
#: silently ignoring an unrecognised `*_index`.
OB_M15_INDEX_FIELDS = ("origin_index", "detection_index", "pivot_index")

#: Every positional field name the seam recognises, with its space. Anything
#: matching the positional-name pattern but absent here is REJECTED rather than
#: passed through, so a new engine field cannot bypass conversion unnoticed.
FIELD_SPACES: dict = {
    **{f: SPACE_M1 for f in set(PENDING_INDEX_FIELDS) | set(ACTIVE_TRADE_INDEX_FIELDS)
       | set(TRADE_ROW_INDEX_FIELDS) | set(GHOST_INDEX_FIELDS)},
    **{f: SPACE_M15_DETECTION for f in OB_M15_INDEX_FIELDS},
}

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
        """Frame-local index. MAY be negative — deliberately.

        This originally rejected anything before the frame origin, on the
        assumption that a carried index must resolve to a candle still present
        in the window. Running it against the real engine disproved that
        assumption immediately: a two-year window carrying 34 pending plans hit
        an ordinal from ~2018, eight years behind the window.

        The assumption was wrong because `candle_records` is NEVER indexed by a
        carried value — it is only ever `enumerate`d. Every reader of a carried
        index is a comparison or a difference:

            bars_to_fill = candle_index - created_candle_index
            candle_index >= trigger_candle_index + trigger_delay_candles
            candle_index >= _be_stop_active_from
            candle_index >  tapped_candle_index + 1

        All four are exact under a negative operand, and the differences are
        invariant. Worked through for a real case: the oracle computes
        4,314,000 - 1,168,182 = 3,145,818; the window computes
        744,489 - (-2,401,329) = 3,145,818. Identical, which is precisely what
        equivalence requires.

        Rejecting negatives would have forced a full-oracle rebuild on any node
        carrying an old pending plan — i.e. almost always — which would have
        defeated Model B entirely for the wrong reason. CLAMPING remains
        forbidden: clamping to 0 would fabricate an expired delay. Ordinals past
        the frame END are still rejected, because those are genuinely impossible
        rather than merely historical.
        """
        local = int(absolute) - self.origin_ordinal
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


import re

#: A field that LOOKS positional. Used only to catch unregistered fields, never
#: to decide conversion -- shape guessing is exactly what the registry prevents.
_POSITIONAL_NAME = re.compile(r"(^|_)(candle_index|_index)$|candle_index")


def unregistered_positional_fields(record: dict, known: tuple) -> list:
    """Field names that look positional but are not registered anywhere.

    A new positional field added to the engine would otherwise be carried
    verbatim across a frame shift and silently mis-evaluated -- the precise bug
    this module exists to prevent. Callers treat a non-empty result as fatal.
    """
    out = []
    for name in record:
        if name in known or name in NON_POSITIONAL_COUNTS or name in FIELD_SPACES:
            continue
        if _POSITIONAL_NAME.search(name):
            out.append(name)
    return out


def assert_all_positional_fields_registered(record: dict, known: tuple, path: str = "") -> None:
    unknown = unregistered_positional_fields(record, known)
    if unknown:
        raise IndexRebaseError(
            f"unregistered positional field(s) at {path or '<root>'}: {sorted(unknown)} — "
            "add them to the registry with an explicit ordinal space; they must not be "
            "carried across a frame shift unconverted")


def trade_row_to_absolute(row: dict, origin: FrameOrigin) -> dict:
    return _map_fields(row, TRADE_ROW_INDEX_FIELDS, origin.to_absolute)


def trade_row_to_local(row: dict, origin: FrameOrigin) -> dict:
    return _map_fields(row, TRADE_ROW_INDEX_FIELDS, origin.to_local)


def ghost_state_to_absolute(state: dict, origin: FrameOrigin) -> dict:
    return _map_fields(state, GHOST_INDEX_FIELDS, origin.to_absolute)


def ghost_state_to_local(state: dict, origin: FrameOrigin) -> dict:
    return _map_fields(state, GHOST_INDEX_FIELDS, origin.to_local)


def pending_to_absolute(item: dict, origin: FrameOrigin) -> dict:
    return _map_fields(item, PENDING_INDEX_FIELDS, origin.to_absolute)


def pending_to_local(item: dict, origin: FrameOrigin) -> dict:
    return _map_fields(item, PENDING_INDEX_FIELDS, origin.to_local)


def _active_trade_map(trade: dict, origin: FrameOrigin, fn, row_fn) -> dict:
    """Convert an active trade AND its nested emitted `row`.

    The nested row is the part the top-level audit missed: an active trade holds
    {"ob", "plan", "row", "metrics", ...} and `row` carries eight positional
    fields that end up in the trade output.
    """
    out = _map_fields(trade, ACTIVE_TRADE_INDEX_FIELDS, fn)
    row = out.get("row")
    if isinstance(row, dict):
        assert_all_positional_fields_registered(row, TRADE_ROW_INDEX_FIELDS, "active_trade.row")
        out["row"] = row_fn(row, origin)
    ob = out.get("ob")
    if isinstance(ob, dict):
        # M15 detection-frame provenance: registered, deliberately NOT rebased.
        assert_all_positional_fields_registered(ob, OB_M15_INDEX_FIELDS, "active_trade.ob")
    return out


def active_trade_to_absolute(trade: dict, origin: FrameOrigin) -> dict:
    return _active_trade_map(trade, origin, origin.to_absolute, trade_row_to_absolute)


def active_trade_to_local(trade: dict, origin: FrameOrigin) -> dict:
    return _active_trade_map(trade, origin, origin.to_local, trade_row_to_local)


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
