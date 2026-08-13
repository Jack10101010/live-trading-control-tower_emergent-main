"""B1 — the normalized contract for broker CLOSE evidence. Pure, stdlib only.

WHY THIS EXISTS
    The autonomous runner has never read deal history. Its only broker read is
    `snapshot()`, which describes OPEN positions and orders — so when a position
    closes it simply disappears, and the runner learns nothing about the exit.
    That is why its daily-loss rail could never be populated: `close_position`
    returns `{"ticket", "closed": True}` and nothing more, and the dominant close
    path (server-side SL/TP, stop-out, external close) produces no local event at
    all.

    Realised outcomes exist only in broker deal history. This module defines what
    a deal looks like once it has crossed the gateway boundary, so nothing
    downstream ever touches an SDK object.

WHAT THIS MODULE IS NOT
    Not a second Broker History subsystem. There is no exhaustiveness
    calibration, no source-cap detection, no reconstruction and no R arithmetic
    here — the backend owns those for the Control Tower, and the runner's problem
    is materially smaller: a bounded window over one instance's own deals.

    B1 deliberately stops at "here are the normalized records". Resolution
    against the ledger, R calculation, posting, idempotency and pruning are all
    Milestone B2 onward.

THE OUTCOME DISTINCTION THIS EXISTS TO PROTECT
    A failed read and a quiet market are DIFFERENT FACTS. The MT5 SDK signals
    failure by returning `None`, and the idiom `history_deals_get(...) or []`
    silently converts that into "no deals happened" — a defect this repository
    has already had to remove once from the Control Tower's history reader. The
    outcome enum makes the distinction unrepresentable-by-accident: there is no
    value of `deals` that means "unavailable".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum

# ── DEAL_ENTRY: how a deal moves exposure ────────────────────────────────────
# Values mirror the Control Tower's `broker_history` mapping exactly, so the two
# readers cannot drift into disagreeing about what an MT5 entry code means.
ENTRY_IN = "in"                  # opens or increases exposure
ENTRY_OUT = "out"                # closes or reduces exposure
ENTRY_INOUT = "inout"            # reversal — cannot be split into one trade
ENTRY_OUT_BY = "out_by"          # closed by an opposite position

#: MT5 DEAL_ENTRY codes. Anything absent is MALFORMED, never guessed: an
#: unrecognised entry code means we do not know whether the deal opened or closed
#: exposure, and inventing an answer would misattribute a realised outcome.
MT5_DEAL_ENTRY = {0: ENTRY_IN, 1: ENTRY_OUT, 2: ENTRY_INOUT, 3: ENTRY_OUT_BY}

#: MT5 DEAL_TYPE codes that are TRADES. Everything else — balance, credit,
#: correction, commission adjustments, bonuses — is not a trade and is filtered
#: out rather than rejected: those records are legitimately present in history
#: and are simply not this reader's subject.
DEAL_TYPE_BUY = "buy"
DEAL_TYPE_SELL = "sell"
MT5_DEAL_TYPE = {0: DEAL_TYPE_BUY, 1: DEAL_TYPE_SELL}


class DealReadOutcome(Enum):
    """The four states of a history read, three of which are statuses.

    `OK` with zero deals is the fourth: a genuinely quiet window. It is a real
    answer and is deliberately NOT a separate status, because the meaningful
    distinction is "was the read performed", not "did it find anything".
    """
    OK = "ok"                    # read performed; every record normalized cleanly
    MALFORMED = "malformed"      # read performed; SOME records unusable
    UNAVAILABLE = "unavailable"  # read could not be performed at all


@dataclass(frozen=True)
class DealRecord:
    """One normalized trade deal. Plain scalars only — no SDK object escapes.

    `execution_time_utc` is timezone-aware. The later accounting stage derives a
    UTC calendar date from it to choose the realised-R day bucket, so a naive or
    local-time value here would silently misdate a realised loss.
    """
    deal_id: str
    position_id: str
    order_id: str | None
    entry: str
    deal_type: str
    volume: float
    price: float
    profit: float
    commission: float | None
    fee: float | None
    swap: float | None
    execution_time_utc: datetime
    symbol: str
    magic: int | None
    comment: str | None

    @property
    def is_exit(self) -> bool:
        """Whether this deal REDUCED exposure. Only `out` qualifies: `inout` and
        `out_by` are terminal for a position but cannot be attributed to one
        trade by price, so B2 must quarantine them rather than account them."""
        return self.entry == ENTRY_OUT

    def utc_date_key(self) -> str:
        """The ISO day this deal belongs to, in UTC.

        Matches the runner's existing definition of a day
        (`datetime.now(timezone.utc).strftime("%Y-%m-%d")`). No account timezone
        and no configured reset are applied — those belong to the funded-rules
        milestone, and applying one here would change which day a loss counts in.
        """
        return self.execution_time_utc.astimezone(timezone.utc).strftime("%Y-%m-%d")


@dataclass(frozen=True)
class RejectedDeal:
    """A record that could not be normalized, kept as evidence.

    Rejected deals are surfaced, never dropped. A trade deal this reader cannot
    understand may still have moved real money, so it must reach an operator as
    a known unknown rather than vanish into a filter.
    """
    reason: str
    raw_ticket: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DealReadResult:
    """The outcome of one bounded history read.

    `deals` is meaningful for OK and MALFORMED. For UNAVAILABLE it is empty and
    carries no information — the read did not happen, so the absence of deals
    says nothing about the market.
    """
    outcome: DealReadOutcome
    deals: tuple = field(default_factory=tuple)
    rejected: tuple = field(default_factory=tuple)
    detail: str | None = None
    window_from: datetime | None = None
    window_to: datetime | None = None

    @property
    def performed(self) -> bool:
        """Whether the broker was successfully read. False ONLY for UNAVAILABLE.

        This is the property a caller must check before treating an empty result
        as "nothing happened".
        """
        return self.outcome is not DealReadOutcome.UNAVAILABLE

    @property
    def complete(self) -> bool:
        """Whether every record in the window normalized cleanly."""
        return self.outcome is DealReadOutcome.OK

    @property
    def empty(self) -> bool:
        return not self.deals


# ── normalization ────────────────────────────────────────────────────────────

# Rejection reasons. Stable, machine-readable, safe to display.
REJECT_NO_DEAL_ID = "missing_deal_id"
REJECT_NO_POSITION_ID = "missing_position_id"
REJECT_UNKNOWN_ENTRY = "unknown_deal_entry"
REJECT_UNKNOWN_TYPE = "unknown_deal_type"
REJECT_BAD_TIME = "unusable_execution_time"
REJECT_BAD_VOLUME = "non_finite_volume"
REJECT_BAD_PRICE = "non_finite_price"
REJECT_BAD_PROFIT = "non_finite_profit"


def _finite(value):
    """A finite float, or None. A bool is not a number here — `True` arriving as
    a volume is a malformed record, not a volume of 1."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _identity(value) -> str | None:
    """A usable identifier as a string, or None. `0` is not a ticket."""
    if isinstance(value, bool) or value is None:
        return None
    text = str(value).strip()
    if not text or text == "0":
        return None
    return text


#: A record whose own attribute access raised. `getattr(obj, name, default)`
#: swallows only AttributeError, so a property raising anything else propagates —
#: which would let ONE hostile record discard an entire window of valid deals.
REJECT_UNREADABLE = "record_attribute_unreadable"


def normalize_deal(raw) -> tuple:
    """Normalize ONE raw SDK deal into `(DealRecord | None, RejectedDeal | None)`.

    Total: never raises, whatever the SDK hands over — a hostile or partially
    populated object yields a rejection rather than propagating. Exactly one of
    the two returned slots is populated.

    Every financial field must be present and finite. Nothing is defaulted to
    zero: a missing profit is not a breakeven trade, and a placeholder here would
    become a fabricated realised-R value downstream.
    """
    try:
        return _normalize(raw)
    except Exception as exc:                                    # noqa: BLE001
        # Isolate the damage to the single record. Failing the whole read here
        # would turn one broken object into "the broker is unavailable", losing
        # every good deal beside it.
        try:
            ticket = _identity(getattr(raw, "ticket", None))
        except Exception:                                       # noqa: BLE001
            ticket = None
        return None, RejectedDeal(reason=REJECT_UNREADABLE, raw_ticket=ticket,
                                  detail=type(exc).__name__)


def _normalize(raw) -> tuple:
    ticket = _identity(getattr(raw, "ticket", None))

    def reject(reason, detail=None):
        return None, RejectedDeal(reason=reason, raw_ticket=ticket, detail=detail)

    if ticket is None:
        return reject(REJECT_NO_DEAL_ID)

    position_id = _identity(getattr(raw, "position_id", None))
    if position_id is None:
        # Without a position id the deal cannot be joined to an originating
        # intent, so its realised outcome can never be attributed.
        return reject(REJECT_NO_POSITION_ID)

    entry_raw = getattr(raw, "entry", None)
    if isinstance(entry_raw, bool) or entry_raw not in MT5_DEAL_ENTRY:
        return reject(REJECT_UNKNOWN_ENTRY, f"entry={entry_raw!r}")

    type_raw = getattr(raw, "type", None)
    if isinstance(type_raw, bool) or type_raw not in MT5_DEAL_TYPE:
        return reject(REJECT_UNKNOWN_TYPE, f"type={type_raw!r}")

    volume = _finite(getattr(raw, "volume", None))
    if volume is None:
        return reject(REJECT_BAD_VOLUME)
    price = _finite(getattr(raw, "price", None))
    if price is None:
        return reject(REJECT_BAD_PRICE)
    profit = _finite(getattr(raw, "profit", None))
    if profit is None:
        return reject(REJECT_BAD_PROFIT)

    epoch = _finite(getattr(raw, "time", None))
    if epoch is None:
        return reject(REJECT_BAD_TIME)
    try:
        when = datetime.fromtimestamp(epoch, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        return reject(REJECT_BAD_TIME, type(exc).__name__)

    magic_raw = getattr(raw, "magic", None)
    magic = (int(magic_raw) if isinstance(magic_raw, int)
             and not isinstance(magic_raw, bool) else None)
    comment = getattr(raw, "comment", None)

    return DealRecord(
        deal_id=ticket, position_id=position_id,
        order_id=_identity(getattr(raw, "order", None)),
        entry=MT5_DEAL_ENTRY[entry_raw], deal_type=MT5_DEAL_TYPE[type_raw],
        volume=volume, price=price, profit=profit,
        commission=_finite(getattr(raw, "commission", None)),
        fee=_finite(getattr(raw, "fee", None)),
        swap=_finite(getattr(raw, "swap", None)),
        execution_time_utc=when,
        symbol=str(getattr(raw, "symbol", "") or ""),
        magic=magic,
        comment=(str(comment) if comment else None),
    ), None


def is_trade_deal(raw) -> bool:
    """Whether a raw record is a TRADE rather than a balance operation.

    Balance, credit, correction and commission-adjustment deals carry DEAL_TYPE
    codes outside {0, 1}. They are legitimately present in history and are
    FILTERED (not rejected) — they are not this reader's subject, and treating
    them as malformed would fill the diagnostics with normal account activity.
    """
    type_raw = getattr(raw, "type", None)
    return not isinstance(type_raw, bool) and type_raw in MT5_DEAL_TYPE
