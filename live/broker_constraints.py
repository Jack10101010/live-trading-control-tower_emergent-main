"""Pure, deterministic broker-constraint normalization (LX-1 Slice 2).

Transforms broker symbol metadata + a live tick + requested order values into a
normalized, broker-valid open request — or a typed failure. It is PURE: no
filesystem, no network, no state, no broker access, and no dependency on the
executor, gateway, or strategy. It imports nothing beyond the standard library.

Design invariants:
- Never raise for a broker-validity problem — return a typed failure instead.
- Never INCREASE exposure: volume is quantized DOWN to the broker's volume step.
- Never widen or move a strategy stop: a stop that violates a broker constraint
  is REJECTED (freeze/attention), never silently adjusted — so strategy
  semantics are preserved exactly.
- Deterministic: decimal quantization/rounding, no floating-point drift.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum


class NormReason(Enum):
    OK = "ok"                              # valid, unchanged
    OK_NORMALIZED = "ok_normalized"        # valid, numerically normalized
    INVALID_SIDE = "invalid_side"
    NON_FINITE = "non_finite"
    INVALID_VOLUME = "invalid_volume"
    VOLUME_BELOW_MIN = "volume_below_min"
    VOLUME_ABOVE_MAX = "volume_above_max"
    MISSING_SYMBOL_META = "missing_symbol_meta"
    MISSING_TICK = "missing_tick"
    STALE_TICK = "stale_tick"
    INVALID_PRICE = "invalid_price"
    STOP_WRONG_SIDE = "stop_wrong_side"
    STOP_TOO_CLOSE = "stop_too_close"
    UNSUPPORTED_FILLING = "unsupported_filling"


# Failures that mean "the world is not in a state to trade" (freeze) rather than
# "this specific request is invalid" (reject and move on).
_FREEZE_REASONS = frozenset({
    NormReason.MISSING_SYMBOL_META, NormReason.MISSING_TICK, NormReason.STALE_TICK,
})


@dataclass(frozen=True)
class NormalizedOrder:
    symbol: str
    side: str
    volume: float
    price: float
    sl: float
    tp: float
    type_filling: object      # an sdk filling constant, passed through from input
    deviation: int


@dataclass(frozen=True)
class NormResult:
    ok: bool
    reason: NormReason
    order: NormalizedOrder | None = None
    diagnostic: str = ""
    retryable: bool = False
    freeze: bool = False

    @classmethod
    def fail(cls, reason: NormReason, diagnostic: str) -> "NormResult":
        # Broker-validity failures are never blindly retryable.
        return cls(ok=False, reason=reason, diagnostic=diagnostic,
                   retryable=False, freeze=reason in _FREEZE_REASONS)

    @classmethod
    def success(cls, order: NormalizedOrder, normalized: bool) -> "NormResult":
        return cls(ok=True,
                   reason=NormReason.OK_NORMALIZED if normalized else NormReason.OK,
                   order=order)


# ── numeric helpers (deterministic) ───────────────────────────────────────────

def _finite(x) -> bool:
    return (isinstance(x, (int, float)) and not isinstance(x, bool)
            and math.isfinite(x))


def _quantize_down(value: float, step: float) -> float:
    """Floor `value` to a multiple of `step` (never rounds up -> never increases
    exposure). The caller (normalize_open) has already rejected step <= 0
    (D-S2-1), so this assumes a valid positive step. Decimal-based (no float
    drift)."""
    n = (Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN)
    return float(n * Decimal(str(step)))


def _round_digits(value: float, digits: int) -> float:
    q = Decimal(1).scaleb(-digits)   # 10**-digits
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def _select_filling(filling_mode: int, filling_preference):
    """Pick the first broker-supported filling constant, in the caller's
    preference order. `filling_preference` is an ordered iterable of
    (order_filling_const, symbol_filling_flag); a mode is supported iff its flag
    bit is set in `filling_mode`. Returns None if none supported."""
    for order_const, symbol_flag in filling_preference:
        if order_const is None or not symbol_flag:
            continue
        if int(filling_mode) & int(symbol_flag):
            return order_const
    return None


def _stop_violation(kind: str, val: float, side: str, price: float,
                    min_dist: Decimal):
    """Return a NormReason if the stop is invalid, else None. `val == 0.0` means
    'no stop' and is always accepted. Never adjusts the value. `min_dist` is a
    Decimal and the distance is compared in Decimal (D-S2-4), so a stop exactly
    at the minimum distance is accepted (no binary-float boundary error)."""
    if val == 0.0:
        return None
    if side == "long":
        correct_side = (val < price) if kind == "sl" else (val > price)
    else:  # short
        correct_side = (val > price) if kind == "sl" else (val < price)
    if not correct_side:
        return NormReason.STOP_WRONG_SIDE
    dist = abs(Decimal(str(price)) - Decimal(str(val)))
    if dist < min_dist:
        return NormReason.STOP_TOO_CLOSE
    return None


# ── the normalization entry point ─────────────────────────────────────────────

def normalize_open(symbol_info, tick, side: str, requested_volume,
                   requested_sl, requested_tp, *, filling_preference,
                   deviation: int = 20, max_tick_age_s=None,
                   now_epoch=None) -> NormResult:
    """Normalize an OPEN market request against broker constraints.

    `symbol_info`/`tick` are duck-typed MT5 objects (fields read via getattr).
    `filling_preference` is the ordered (order_const, symbol_flag) list. Stale-
    tick detection is applied only when both `max_tick_age_s` and `now_epoch`
    are supplied (gateway wiring of a clock is a later slice)."""
    # ── side (fail-closed: only the two canonical intent sides; no aliasing) ──
    if side not in ("long", "short"):
        return NormResult.fail(NormReason.INVALID_SIDE, f"invalid side {side!r}")

    # ── symbol metadata ──
    if symbol_info is None:
        return NormResult.fail(NormReason.MISSING_SYMBOL_META, "symbol_info is None")
    digits = getattr(symbol_info, "digits", None)
    point = getattr(symbol_info, "point", None)
    vmin = getattr(symbol_info, "volume_min", None)
    vmax = getattr(symbol_info, "volume_max", None)
    vstep = getattr(symbol_info, "volume_step", None)
    if not all(_finite(x) for x in (digits, point, vmin, vmax, vstep)):
        return NormResult.fail(NormReason.MISSING_SYMBOL_META,
                               "incomplete symbol metadata (digits/point/volume_*)")
    if vstep <= 0:                        # D-S2-1: finite-but-invalid step fails closed
        return NormResult.fail(NormReason.MISSING_SYMBOL_META,
                               f"invalid volume_step {vstep} (must be > 0)")
    digits = int(digits)
    stops_level = getattr(symbol_info, "trade_stops_level", 0) or 0
    freeze_level = getattr(symbol_info, "trade_freeze_level", 0) or 0
    filling_mode = getattr(symbol_info, "filling_mode", 0) or 0

    # ── tick ──
    if tick is None:
        return NormResult.fail(NormReason.MISSING_TICK, "no tick")
    bid = getattr(tick, "bid", None)
    ask = getattr(tick, "ask", None)
    # D-S2-3: bid/ask must be finite AND strictly positive (a zero/negative feed
    # price is bad data, not a usable quote) — fail closed, never substitute.
    if not (_finite(bid) and _finite(ask) and bid > 0 and ask > 0):
        return NormResult.fail(NormReason.MISSING_TICK, "tick missing positive finite bid/ask")
    if max_tick_age_s is not None and now_epoch is not None:
        ttime = getattr(tick, "time", None)
        if _finite(ttime):
            age = float(now_epoch) - float(ttime)
            if age > max_tick_age_s:
                return NormResult.fail(NormReason.STALE_TICK,
                                       f"tick age {age:.0f}s > {max_tick_age_s}s")

    # ── volume (never increases exposure) ──
    if not _finite(requested_volume):
        return NormResult.fail(NormReason.NON_FINITE, "requested volume not finite")
    if requested_volume <= 0:
        return NormResult.fail(NormReason.INVALID_VOLUME, f"volume {requested_volume} <= 0")
    if requested_volume > vmax:
        return NormResult.fail(NormReason.VOLUME_ABOVE_MAX,
                               f"volume {requested_volume} > volume_max {vmax}")
    volume = _quantize_down(requested_volume, vstep)
    if volume < vmin or volume <= 0:
        return NormResult.fail(NormReason.VOLUME_BELOW_MIN,
                               f"volume {requested_volume}->{volume} < volume_min {vmin}")

    # ── price (side-selected, digit-rounded) ──
    raw_price = ask if side == "long" else bid
    if not _finite(raw_price):
        return NormResult.fail(NormReason.INVALID_PRICE, "no side price")
    price = _round_digits(raw_price, digits)

    # ── stops (validate; never widen/move) ──
    sl_in = float(requested_sl) if _finite(requested_sl) else 0.0
    tp_in = float(requested_tp) if _finite(requested_tp) else 0.0
    sl = _round_digits(sl_in, digits) if sl_in != 0.0 else 0.0
    tp = _round_digits(tp_in, digits) if tp_in != 0.0 else 0.0
    # D-S2-4: minimum stop distance in Decimal (compared against the final
    # rounded SL/TP), so an exactly-minimum-distance stop is accepted.
    min_dist = max(Decimal(str(stops_level)), Decimal(str(freeze_level))) * Decimal(str(point))
    for kind, val in (("sl", sl), ("tp", tp)):
        v = _stop_violation(kind, val, side, price, min_dist)
        if v is not None:
            return NormResult.fail(v, f"{kind}={val} vs price={price} min_dist={min_dist}")

    # ── filling policy ──
    type_filling = _select_filling(filling_mode, filling_preference)
    if type_filling is None:
        return NormResult.fail(NormReason.UNSUPPORTED_FILLING,
                               f"no supported filling in mode {filling_mode}")

    normalized = (volume != float(requested_volume) or price != float(raw_price)
                  or sl != sl_in or tp != tp_in)
    order = NormalizedOrder(symbol=getattr(symbol_info, "name", None) or "",
                            side=side, volume=volume, price=price, sl=sl, tp=tp,
                            type_filling=type_filling, deviation=int(deviation))
    return NormResult.success(order, normalized)
