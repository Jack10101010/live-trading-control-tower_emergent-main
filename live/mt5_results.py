"""Pure, deterministic classification of an MT5 ``order_send`` outcome (LX-1
Slice 3).

Stdlib-only; no broker/executor/strategy logic, no filesystem, no network. It
turns the raw result of a *single* ``order_send`` call — or the exception it
raised, or a ``None`` return, or a pre-submit rejection where no call happened —
into one immutable typed ``MT5SubmitResult``, so callers never re-interpret a
loose ``(bool, str)`` tuple. Broker evidence is captured as plain scalars; the
(mutable, possibly unserializable) SDK result object is never retained.

Safety posture: the classifier is conservative. Only an explicit success/partial/
reject retcode is treated as such; anything unknown, missing, timed-out, or
connection-lost is AMBIGUOUS+freeze, and an exception is EXCEPTION+freeze. Nothing
here is ever ``retryable`` (this slice never auto-resubmits).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class MT5SubmitDisposition(Enum):
    FILLED = "filled"
    PARTIALLY_FILLED = "partially_filled"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    EXCEPTION = "exception"
    NOT_SUBMITTED = "not_submitted"     # order_send was never called (pre-submit reject)


# Deterministic broker rejects: the request was refused with NO position created.
REJECT_RETCODE_NAMES = (
    "TRADE_RETCODE_REJECT", "TRADE_RETCODE_INVALID", "TRADE_RETCODE_INVALID_VOLUME",
    "TRADE_RETCODE_INVALID_PRICE", "TRADE_RETCODE_INVALID_STOPS",
    "TRADE_RETCODE_TRADE_DISABLED", "TRADE_RETCODE_MARKET_CLOSED",
    "TRADE_RETCODE_NO_MONEY", "TRADE_RETCODE_INVALID_FILL", "TRADE_RETCODE_REQUOTE",
    "TRADE_RETCODE_PRICE_CHANGED", "TRADE_RETCODE_PRICE_OFF",
    "TRADE_RETCODE_TOO_MANY_REQUESTS", "TRADE_RETCODE_INVALID_EXPIRATION",
    "TRADE_RETCODE_INVALID_ORDER", "TRADE_RETCODE_LIMIT_ORDERS",
    "TRADE_RETCODE_LIMIT_VOLUME",
)
# Outcomes where execution cannot be safely known -> freeze + reconcile.
AMBIGUOUS_RETCODE_NAMES = (
    "TRADE_RETCODE_TIMEOUT", "TRADE_RETCODE_CONNECTION", "TRADE_RETCODE_PLACED",
)

# Plain scalars we snapshot off an SDK result (secret-free; no object retained).
_EVIDENCE_FIELDS = ("retcode", "order", "deal", "volume", "price", "bid", "ask",
                    "comment", "request_id", "retcode_external")

_MISSING = object()


@dataclass(frozen=True)
class RetcodeMap:
    """The sdk retcode constants relevant to classification, extracted by the
    gateway (keeps this module free of any MetaTrader5 import)."""
    done: Any
    done_partial: Any
    reject: frozenset
    ambiguous: frozenset


@dataclass(frozen=True)
class MT5SubmitResult:
    disposition: MT5SubmitDisposition
    retcode: Any = None
    broker_order_ticket: Any = None
    broker_deal_ticket: Any = None
    requested_volume: float | None = None
    filled_volume: float | None = None
    remaining_volume: float | None = None
    price: float | None = None
    comment: Any = None
    diagnostic: str = ""
    retryable: bool = False
    freeze: bool = False
    raw_evidence: dict = field(default_factory=dict)

    @property
    def submitted(self) -> bool:
        """True iff order_send was actually called (every disposition except
        NOT_SUBMITTED)."""
        return self.disposition is not MT5SubmitDisposition.NOT_SUBMITTED

    def to_ledger_detail(self) -> dict:
        """Serializable, secret-free evidence for the durable ledger."""
        return {"disposition": self.disposition.value, "retcode": self.retcode,
                "order": self.broker_order_ticket, "deal": self.broker_deal_ticket,
                "requested_volume": self.requested_volume,
                "filled_volume": self.filled_volume,
                "remaining_volume": self.remaining_volume, "price": self.price,
                "comment": self.comment, "diagnostic": self.diagnostic}


def _f(x):
    try:
        v = float(x)
        return v if math.isfinite(v) else None
    except (TypeError, ValueError):
        return None


def usable_ticket(t) -> bool:
    """A usable broker order ticket: an INTEGER-valued identifier, finite and
    strictly > 0 (D-S3-1/D-S3-2). Broker tickets are integers, so a fractional
    float (1.5) is NOT usable even though it is finite and positive. None / 0 /
    negative / NaN / +-inf / bool / string / malformed / fractional -> not
    usable, so a 'success' retcode without a real ticket can never assert a
    position."""
    if isinstance(t, bool) or t is None:
        return False
    if isinstance(t, int):
        return t > 0
    if isinstance(t, float):
        return math.isfinite(t) and t > 0 and t.is_integer()   # reject fractional
    return False


def _finite_positive_volume(v) -> bool:
    """A usable filled volume: numeric, finite, strictly > 0 (never bool)."""
    if isinstance(v, bool) or v is None:
        return False
    if isinstance(v, (int, float)):
        return math.isfinite(v) and v > 0
    return False


def build_retcode_map(sdk) -> RetcodeMap:
    """Read the relevant retcode constants off the injected sdk (attribute reads
    only — no broker call). Constants absent on the sdk are dropped, so they can
    never match a real retcode."""
    def g(name):
        return getattr(sdk, name, _MISSING)

    reject = frozenset(v for v in (g(n) for n in REJECT_RETCODE_NAMES) if v is not _MISSING)
    ambiguous = frozenset(v for v in (g(n) for n in AMBIGUOUS_RETCODE_NAMES) if v is not _MISSING)
    return RetcodeMap(done=g("TRADE_RETCODE_DONE"), done_partial=g("TRADE_RETCODE_DONE_PARTIAL"),
                      reject=reject, ambiguous=ambiguous)


def extract_evidence(result) -> dict:
    """Immutable plain-data snapshot of an sdk order_send result (never retains
    the object). Absent attributes are simply omitted. ``None`` -> ``{}``."""
    if result is None:
        return {}
    return {name: getattr(result, name) for name in _EVIDENCE_FIELDS if hasattr(result, name)}


def not_submitted(diagnostic: str, requested_volume=None) -> MT5SubmitResult:
    """No order_send occurred (normalization / precondition reject). Distinct
    from every broker disposition; never freezes (the engine re-diffs)."""
    return MT5SubmitResult(disposition=MT5SubmitDisposition.NOT_SUBMITTED,
                           requested_volume=_f(requested_volume), diagnostic=diagnostic)


def from_exception(exc: BaseException, requested_volume=None) -> MT5SubmitResult:
    """order_send raised: the broker outcome is unknown -> EXCEPTION, freeze,
    never retry, preserve exception type+message, fabricate no broker evidence."""
    return MT5SubmitResult(disposition=MT5SubmitDisposition.EXCEPTION,
                           requested_volume=_f(requested_volume),
                           diagnostic=f"{type(exc).__name__}: {exc}", freeze=True)


def classify(evidence: dict | None, requested_volume, retcodes: RetcodeMap) -> MT5SubmitResult:
    """Classify a non-exception order_send outcome. ``evidence`` is the plain
    dict from ``extract_evidence`` (``{}``/None means the sdk returned None or a
    result without a retcode -> AMBIGUOUS+freeze)."""
    req = _f(requested_volume)
    ev = dict(evidence or {})
    if "retcode" not in ev:
        return MT5SubmitResult(disposition=MT5SubmitDisposition.AMBIGUOUS, requested_volume=req,
                               diagnostic="order_send returned no result/retcode",
                               freeze=True, raw_evidence=ev)
    retcode = ev.get("retcode")
    order = ev.get("order")
    deal = ev.get("deal")
    price = _f(ev.get("price"))
    filled = _f(ev.get("volume"))
    comment = ev.get("comment")
    common = dict(retcode=retcode, broker_order_ticket=order, broker_deal_ticket=deal,
                  requested_volume=req, price=price, comment=comment, raw_evidence=ev)

    def _remaining():
        return max(req - filled, 0.0) if (req is not None and filled is not None) else None

    if retcode == retcodes.done or retcode == retcodes.done_partial:
        is_partial = retcode == retcodes.done_partial
        # D-S3-1 evidence gate: a success retcode may assert FILLED/PARTIALLY_FILLED
        # ONLY with a usable order ticket AND a finite, strictly-positive filled
        # volume. Otherwise the broker "reported success" but the evidence is
        # insufficient to safely assert position state -> AMBIGUOUS + freeze
        # (never REJECTED, never a fabricated/assumed volume). All present
        # evidence (retcode/comment/tickets/raw) is preserved via **common.
        if not (usable_ticket(order) and filled is not None and filled > 0):
            return MT5SubmitResult(
                disposition=MT5SubmitDisposition.AMBIGUOUS, freeze=True,
                diagnostic=(f"{'partial ' if is_partial else ''}success retcode {retcode} "
                            f"with insufficient evidence (ticket={order!r}, volume={ev.get('volume')!r})"),
                **common)
        if is_partial:
            return MT5SubmitResult(disposition=MT5SubmitDisposition.PARTIALLY_FILLED,
                                   filled_volume=filled, remaining_volume=_remaining(),
                                   diagnostic="partial fill", freeze=True, **common)
        return MT5SubmitResult(disposition=MT5SubmitDisposition.FILLED, filled_volume=filled,
                               remaining_volume=_remaining(), diagnostic="filled", **common)
    if retcode in retcodes.reject:
        return MT5SubmitResult(disposition=MT5SubmitDisposition.REJECTED,
                               diagnostic=f"rejected retcode {retcode}", **common)
    if retcode in retcodes.ambiguous:
        return MT5SubmitResult(disposition=MT5SubmitDisposition.AMBIGUOUS,
                               diagnostic=f"ambiguous retcode {retcode}", freeze=True, **common)
    # Unknown / unmapped retcode -> conservative AMBIGUOUS + freeze.
    return MT5SubmitResult(disposition=MT5SubmitDisposition.AMBIGUOUS,
                           diagnostic=f"unknown retcode {retcode}", freeze=True, **common)


# ── LIVE-3: typed classification of a non-submit broker ACTION ────────────────
# (SL/TP modification, pending-order removal, position close). Same conservative
# posture as submit classification: only an explicit DONE is success; rejects are
# the enumerated deterministic refusals; anything unknown/missing/timeout is
# AMBIGUOUS+freeze; an exception is EXCEPTION+freeze. No SDK object retained.

class MT5ActionDisposition(Enum):
    DONE = "done"
    REJECTED = "rejected"
    AMBIGUOUS = "ambiguous"
    EXCEPTION = "exception"
    NOT_SUBMITTED = "not_submitted"     # order_send was never called


@dataclass(frozen=True)
class MT5ActionResult:
    disposition: MT5ActionDisposition
    retcode: Any = None
    broker_order_ticket: Any = None
    broker_deal_ticket: Any = None
    comment: Any = None
    diagnostic: str = ""
    freeze: bool = False


def action_not_submitted(diagnostic: str) -> MT5ActionResult:
    return MT5ActionResult(disposition=MT5ActionDisposition.NOT_SUBMITTED,
                           diagnostic=diagnostic)


def action_from_exception(exc: BaseException) -> MT5ActionResult:
    return MT5ActionResult(disposition=MT5ActionDisposition.EXCEPTION,
                           diagnostic=f"{type(exc).__name__}: {exc}", freeze=True)


def classify_action(evidence: dict | None, retcodes: RetcodeMap) -> MT5ActionResult:
    """Classify one non-submit order_send outcome from snapshotted evidence."""
    if not evidence:
        return MT5ActionResult(disposition=MT5ActionDisposition.AMBIGUOUS,
                               diagnostic="order_send returned no result", freeze=True)
    retcode = evidence.get("retcode")
    order = evidence.get("order")
    deal = evidence.get("deal")
    comment = evidence.get("comment")
    common = dict(retcode=retcode, comment=comment,
                  broker_order_ticket=order if usable_ticket(order) else None,
                  broker_deal_ticket=deal if usable_ticket(deal) else None)
    if retcode == retcodes.done:
        return MT5ActionResult(disposition=MT5ActionDisposition.DONE,
                               diagnostic="broker confirmed", **common)
    if retcode in retcodes.reject:
        return MT5ActionResult(disposition=MT5ActionDisposition.REJECTED,
                               diagnostic=f"broker rejected: retcode {retcode}", **common)
    return MT5ActionResult(disposition=MT5ActionDisposition.AMBIGUOUS, freeze=True,
                           diagnostic=f"unclassifiable retcode {retcode}", **common)
