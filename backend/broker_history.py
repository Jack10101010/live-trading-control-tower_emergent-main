"""LIVE-4C — the canonical READ-ONLY broker-history contract.

AUDIT FINDING THAT MOTIVATES THIS MODULE
    Before LIVE-4C the broker abstraction did NOT expose enough historical
    evidence to reconstruct a closed trade. Grounded in production code:

      * `history_deals_get` was reached via `getattr` inside
        `broker.recent_executions` only — optional, 1-day window, 50-deal cap
        (that cap survives as a DISPLAY bound, but is now applied to canonically
        ordered deals and disclosed, rather than keeping an arbitrary fifty);
      * `history_orders_get` was never called anywhere;
      * the canonical `BrokerDeal` carried no position id, no DEAL_ENTRY
        direction, and no commission / fee / swap;
      * account margin mode (hedging vs netting) was never read anywhere;
      * `MockBroker` had no deal evidence at all (it inherited the inert
        `recent_executions` default).

    This module introduces the missing contract. It reads ONLY fields the
    MetaTrader5 SDK genuinely provides, each accessed defensively, and reports
    anything absent as explicitly UNAVAILABLE rather than inventing a value.

RULES
  * READ-ONLY. There is no write method here, and this module never imports an
    execution owner, never routes a command and never mutates any state.
  * Broker-neutral, immutable, deterministically serializable models.
  * Every model carries provenance, availability and a source timestamp.
  * A field the broker cannot supply is `None` WITH an availability marker —
    never coerced to `0`, and never guessed.
  * A read that cannot be proven EXHAUSTIVE says so. Availability and
    completeness are separate facts: a read can succeed and still be partial,
    and only a complete one may be persisted. See the BH section at the foot of
    this module for the reader that enforces it.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

SCHEMA_VERSION = "ct.broker-history.v1"

# ── availability vocabulary (never collapsed into each other) ─────────────────
AVAILABLE = "available"
UNAVAILABLE = "unavailable"          # the broker cannot supply this at all
PENDING = "pending"                  # expected later (e.g. costs settle after close)
NOT_APPLICABLE = "not_applicable"
CONFLICTED = "conflicted"

# ── provenance ────────────────────────────────────────────────────────────────
PROV_LIVE_MT5 = "live_mt5"
PROV_MOCK_FIXTURE = "mock-fixture"
PROV_ABSENT = "absent"

# ── account margin mode ───────────────────────────────────────────────────────
MODE_NETTING = "netting"
MODE_HEDGING = "hedging"
MODE_EXCHANGE = "exchange"
MODE_UNKNOWN = "unknown"

#: MT5 `ACCOUNT_MARGIN_MODE_*` values. Real SDK constants; read defensively.
_MT5_MARGIN_MODES = {0: MODE_NETTING, 1: MODE_EXCHANGE, 2: MODE_HEDGING}

# ── deal entry direction (MT5 DEAL_ENTRY_*) ───────────────────────────────────
ENTRY_IN = "in"                      # opens or increases exposure
ENTRY_OUT = "out"                    # closes or reduces exposure
ENTRY_INOUT = "inout"                # reversal
ENTRY_OUT_BY = "out_by"              # closed by an opposite position
ENTRY_UNKNOWN = "unknown"

_MT5_DEAL_ENTRY = {0: ENTRY_IN, 1: ENTRY_OUT, 2: ENTRY_INOUT, 3: ENTRY_OUT_BY}

#: MT5 `DEAL_TYPE_BUY = 0`, `DEAL_TYPE_SELL = 1`. Anything else is not a trade
#: deal (balance, credit, commission adjustments…) and is reported as unknown.
_MT5_DEAL_TYPE = {0: "buy", 1: "sell"}


def _sorted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sorted(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_sorted(v) for v in obj]
    return obj


def _num(value: Any) -> float | None:
    """A finite number, or None. Never coerces a missing value to zero."""
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _epoch_iso(value: Any) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc) \
            .isoformat().replace("+00:00", "Z")
    except (ValueError, OSError, OverflowError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Canonical immutable models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BrokerCostEvidence:
    """Realized trading costs for ONE deal. Each component is independently
    available or not: a broker that reports commission but settles swap later
    yields `commission=..., swap=None, availability=pending`."""
    commission: float | None = None
    fee: float | None = None
    swap: float | None = None
    availability: str = UNAVAILABLE
    provenance: str = PROV_ABSENT

    @property
    def total(self) -> float | None:
        parts = [p for p in (self.commission, self.fee, self.swap) if p is not None]
        return round(sum(parts), 6) if parts else None

    @property
    def complete(self) -> bool:
        return (self.availability == AVAILABLE
                and None not in (self.commission, self.fee, self.swap))

    def as_dict(self) -> dict:
        return _sorted({"commission": self.commission, "fee": self.fee,
                        "swap": self.swap, "total": self.total,
                        "availability": self.availability,
                        "complete": self.complete, "provenance": self.provenance})


@dataclass(frozen=True)
class BrokerDealRecord:
    """ONE executed deal (fill) as the broker reports it.

    Richer than the LIVE-1 `BrokerDeal`: it carries the position id and the
    DEAL_ENTRY direction, both of which reconstruction requires and neither of
    which the earlier model had."""
    deal_id: str
    order_id: str | None = None
    position_id: str | None = None
    symbol: str | None = None
    deal_type: str | None = None          # buy | sell | None (non-trade deal)
    entry: str = ENTRY_UNKNOWN            # in | out | inout | out_by | unknown
    volume: float | None = None
    price: float | None = None
    profit: float | None = None
    costs: BrokerCostEvidence = field(default_factory=BrokerCostEvidence)
    at: str | None = None
    reason: str | None = None             # broker-supplied close reason, verbatim
    comment: str | None = None
    magic: int | None = None
    provenance: str = PROV_ABSENT

    @property
    def is_trade_deal(self) -> bool:
        """Only buy/sell deals move exposure. Balance / credit / correction
        entries are deliberately excluded from reconstruction."""
        return self.deal_type in ("buy", "sell")

    def as_dict(self) -> dict:
        return _sorted({
            "dealId": self.deal_id, "orderId": self.order_id,
            "positionId": self.position_id, "symbol": self.symbol,
            "dealType": self.deal_type, "entry": self.entry,
            "volume": self.volume, "price": self.price, "profit": self.profit,
            "costs": self.costs.as_dict(), "at": self.at, "reason": self.reason,
            "comment": self.comment, "magic": self.magic,
            "isTradeDeal": self.is_trade_deal, "provenance": self.provenance,
        })


@dataclass(frozen=True)
class BrokerHistoricalOrder:
    """ONE historical order as the broker reports it. Present only when the
    terminal exposes order history; otherwise the snapshot reports it
    unavailable rather than synthesising orders from deals."""
    order_id: str
    position_id: str | None = None
    symbol: str | None = None
    order_type: str | None = None
    side: str | None = None
    requested_volume: float | None = None
    filled_volume: float | None = None
    requested_price: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    state: str | None = None
    created_at: str | None = None
    done_at: str | None = None
    comment: str | None = None
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "orderId": self.order_id, "positionId": self.position_id,
            "symbol": self.symbol, "orderType": self.order_type, "side": self.side,
            "requestedVolume": self.requested_volume,
            "filledVolume": self.filled_volume,
            "requestedPrice": self.requested_price, "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit, "state": self.state,
            "createdAt": self.created_at, "doneAt": self.done_at,
            "comment": self.comment, "provenance": self.provenance,
        })


@dataclass(frozen=True)
class BrokerClosedPositionEvidence:
    """Evidence that a position identifier is no longer open. Derived by
    comparing observed open positions with position ids seen in deal history —
    NOT a broker-reported "closed position" object, because MT5 exposes none."""
    position_id: str
    symbol: str | None = None
    last_seen_open_at: str | None = None
    observed_closed_at: str | None = None
    still_open: bool = False
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "positionId": self.position_id, "symbol": self.symbol,
            "lastSeenOpenAt": self.last_seen_open_at,
            "observedClosedAt": self.observed_closed_at,
            "stillOpen": self.still_open, "provenance": self.provenance,
        })


@dataclass(frozen=True)
class BrokerHistorySnapshot:
    """One coherent read of broker history. Immutable and self-describing:
    `availability` says whether the read succeeded at all, and each capability
    flag says which evidence class the terminal actually supplied."""
    at: str
    availability: str = UNAVAILABLE
    account_mode: str = MODE_UNKNOWN
    account_currency: str | None = None
    account_fingerprint: str | None = None
    deals: tuple = field(default_factory=tuple)
    orders: tuple = field(default_factory=tuple)
    closed_positions: tuple = field(default_factory=tuple)
    open_position_ids: tuple = field(default_factory=tuple)
    window_from: str | None = None
    window_to: str | None = None
    deals_available: bool = False
    orders_available: bool = False
    costs_available: bool = False
    #: Whether the deal/order reads were PROVABLY exhaustive. Distinct from
    #: `*_available`: a read can succeed and still be incomplete, and only a
    #: complete read may advance a ledger watermark.
    deals_complete: bool = False
    orders_complete: bool = False
    #: Why an exhaustive read could not be proven, and the evidence behind it.
    incomplete_reason: str | None = None
    read_evidence: dict | None = None
    detail: str | None = None
    provenance: str = PROV_ABSENT
    schema_version: str = SCHEMA_VERSION

    @property
    def usable(self) -> bool:
        """Deal evidence is the minimum for any reconstruction.

        Deliberately UNCHANGED: `usable` answers "is there deal evidence to read",
        which projections and dashboards legitimately want even from a partial
        window. Persisting is the stricter question — see `ingestable`.
        """
        return self.availability == AVAILABLE and self.deals_available

    @property
    def ingestable(self) -> bool:
        """Whether this snapshot may be PERSISTED to the trade ledger.

        Ingesting a truncated window is the failure this milestone exists to
        prevent: a watermark advanced past discarded deals makes the loss
        permanent and invisible. Completeness is therefore required to write,
        while `usable` remains sufficient to display.
        """
        return self.usable and self.deals_complete

    def as_dict(self) -> dict:
        return _sorted({
            "at": self.at, "availability": self.availability,
            "accountMode": self.account_mode,
            "accountCurrency": self.account_currency,
            "accountFingerprint": self.account_fingerprint,
            "deals": [d.as_dict() for d in self.deals],
            "orders": [o.as_dict() for o in self.orders],
            "closedPositions": [p.as_dict() for p in self.closed_positions],
            "openPositionIds": list(self.open_position_ids),
            "window": {"from": self.window_from, "to": self.window_to},
            "capabilities": {"deals": self.deals_available,
                             "orders": self.orders_available,
                             "costs": self.costs_available},
            "completeness": {"deals": self.deals_complete,
                             "orders": self.orders_complete,
                             "reason": self.incomplete_reason},
            "readEvidence": self.read_evidence,
            "usable": self.usable, "ingestable": self.ingestable,
            "detail": self.detail,
            "provenance": self.provenance, "schemaVersion": self.schema_version,
        })


def unavailable_snapshot(*, at: str, detail: str,
                         provenance: str = PROV_ABSENT) -> BrokerHistorySnapshot:
    """The explicit answer when history cannot be read. Never an empty snapshot
    that could be mistaken for 'no trades happened'."""
    return BrokerHistorySnapshot(at=at, availability=UNAVAILABLE, detail=detail,
                                 provenance=provenance)


# ─────────────────────────────────────────────────────────────────────────────
# MT5 reader — maps ONLY fields the MetaTrader5 SDK genuinely provides.
# ─────────────────────────────────────────────────────────────────────────────

def read_mt5_history(gateway, *, at: str, window_from: datetime,
                     window_to: datetime, symbol_to_canonical=None,
                     limit: int = 1000) -> BrokerHistorySnapshot:
    """Read MT5 history through an already-connected gateway.

    Every SDK entry point is probed with `getattr` because terminal builds
    differ: a build without `history_deals_get` yields `deals_available=False`
    rather than an exception or a fabricated empty history. No SDK object
    escapes; only plain scalars are mapped.

    `limit` is a SAFETY CEILING, no longer a truncation cap. It is the count at
    or above which a window is treated as not provably exhaustive and subdivided.
    It never discards records: reaching it causes more reads, or an explicit
    incomplete result. Calibration lowers it further if the terminal is shown to
    enforce a smaller cap of its own.
    """
    sdk = getattr(gateway, "sdk", None)
    if sdk is None or not getattr(gateway, "connected", False):
        return unavailable_snapshot(at=at, detail="MT5 gateway not connected",
                                    provenance=PROV_LIVE_MT5)
    canonical = symbol_to_canonical or (lambda s: s)

    # -- account mode + currency (never previously read anywhere) -------------
    account_mode, currency, fingerprint = MODE_UNKNOWN, None, None
    try:
        account = sdk.account_info()
        if account is not None:
            raw_mode = getattr(account, "margin_mode", None)
            if isinstance(raw_mode, int) and not isinstance(raw_mode, bool):
                account_mode = _MT5_MARGIN_MODES.get(raw_mode, MODE_UNKNOWN)
            currency = getattr(account, "currency", None)
            login = getattr(account, "login", None)
            fingerprint = f"mt5_****{str(login)[-4:]}" if login is not None else None
    except Exception:
        pass                                    # absent account info -> unknown

    # -- deals ----------------------------------------------------------------
    deals: list = []
    deals_available = False
    costs_available = False
    deals_complete = False
    incomplete_reason = None
    deal_evidence = None
    history_deals = getattr(sdk, "history_deals_get", None)
    if callable(history_deals):
        try:
            # `history_deals_get` returns an UNORDERED collection with no cursor
            # and no count. The previous `list(raw_deals)[:limit]` silently threw
            # away everything past `limit`. The read is now exhaustive-or-explicit.
            deal_read = read_interval_exhaustively(
                history_deals, window_from=window_from, window_to=window_to,
                ceiling=limit, count_source=getattr(sdk, "history_deals_total", None))
            # An SDK failure surfaces as READ_ERROR — never as an empty history.
            deals_available = deal_read.status != READ_ERROR
            deals_complete = deal_read.complete
            incomplete_reason = deal_read.incomplete_reason
            deal_evidence = deal_read.as_dict()
            for raw in deal_read.records:
                commission = _num(getattr(raw, "commission", None))
                fee = _num(getattr(raw, "fee", None))
                swap = _num(getattr(raw, "swap", None))
                has_cost = any(c is not None for c in (commission, fee, swap))
                costs_available = costs_available or has_cost
                deal_type_raw = getattr(raw, "type", None)
                entry_raw = getattr(raw, "entry", None)
                position_id = getattr(raw, "position_id", None)
                deals.append(BrokerDealRecord(
                    deal_id=str(getattr(raw, "ticket", "")),
                    order_id=(str(getattr(raw, "order", "")) or None),
                    position_id=(str(position_id) if position_id else None),
                    symbol=canonical(getattr(raw, "symbol", "") or "") or None,
                    deal_type=_MT5_DEAL_TYPE.get(deal_type_raw)
                              if isinstance(deal_type_raw, int)
                              and not isinstance(deal_type_raw, bool) else None,
                    entry=_MT5_DEAL_ENTRY.get(entry_raw, ENTRY_UNKNOWN)
                          if isinstance(entry_raw, int)
                          and not isinstance(entry_raw, bool) else ENTRY_UNKNOWN,
                    volume=_num(getattr(raw, "volume", None)),
                    price=_num(getattr(raw, "price", None)),
                    profit=_num(getattr(raw, "profit", None)),
                    costs=BrokerCostEvidence(
                        commission=commission, fee=fee, swap=swap,
                        availability=AVAILABLE if has_cost else UNAVAILABLE,
                        provenance=PROV_LIVE_MT5),
                    at=_epoch_iso(getattr(raw, "time", None)),
                    reason=(str(getattr(raw, "reason", ""))
                            if getattr(raw, "reason", None) is not None else None),
                    comment=getattr(raw, "comment", None) or None,
                    magic=getattr(raw, "magic", None),
                    provenance=PROV_LIVE_MT5))
        except Exception:
            deals_available = False             # a failed read is NOT an empty history

    # -- orders (only if the build exposes order history) ---------------------
    orders: list = []
    orders_available = False
    orders_complete = False
    order_evidence = None
    history_orders = getattr(sdk, "history_orders_get", None)
    if callable(history_orders):
        try:
            # Same defect, previously unnamed: orders were truncated identically.
            order_read = read_interval_exhaustively(
                history_orders, window_from=window_from, window_to=window_to,
                ceiling=limit,
                count_source=getattr(sdk, "history_orders_total", None))
            orders_available = order_read.status != READ_ERROR
            orders_complete = order_read.complete
            order_evidence = order_read.as_dict()
            for raw in order_read.records:
                position_id = getattr(raw, "position_id", None)
                orders.append(BrokerHistoricalOrder(
                    order_id=str(getattr(raw, "ticket", "")),
                    position_id=(str(position_id) if position_id else None),
                    symbol=canonical(getattr(raw, "symbol", "") or "") or None,
                    order_type=(str(getattr(raw, "type", ""))
                                if getattr(raw, "type", None) is not None else None),
                    side=None,                  # MT5 encodes side inside type
                    requested_volume=_num(getattr(raw, "volume_initial", None)),
                    filled_volume=_num(getattr(raw, "volume_current", None)),
                    requested_price=_num(getattr(raw, "price_open", None)),
                    stop_loss=_num(getattr(raw, "sl", None)),
                    take_profit=_num(getattr(raw, "tp", None)),
                    state=(str(getattr(raw, "state", ""))
                           if getattr(raw, "state", None) is not None else None),
                    created_at=_epoch_iso(getattr(raw, "time_setup", None)),
                    done_at=_epoch_iso(getattr(raw, "time_done", None)),
                    comment=getattr(raw, "comment", None) or None,
                    provenance=PROV_LIVE_MT5))
        except Exception:
            orders_available = False

    # -- which position ids are still open ------------------------------------
    open_ids: list = []
    try:
        ok, snap = gateway.snapshot()
        if ok and isinstance(snap, dict):
            open_ids = [str(p.get("ticket")) for p in (snap.get("positions") or [])
                        if p.get("ticket") is not None]
    except Exception:
        pass

    closed = _derive_closed_positions(deals, open_ids, provenance=PROV_LIVE_MT5)
    return BrokerHistorySnapshot(
        at=at, availability=AVAILABLE, account_mode=account_mode,
        account_currency=currency, account_fingerprint=fingerprint,
        deals=tuple(deals), orders=tuple(orders), closed_positions=closed,
        open_position_ids=tuple(sorted(open_ids)),
        window_from=window_from.isoformat().replace("+00:00", "Z"),
        window_to=window_to.isoformat().replace("+00:00", "Z"),
        deals_available=deals_available, orders_available=orders_available,
        costs_available=costs_available,
        deals_complete=deals_complete, orders_complete=orders_complete,
        incomplete_reason=incomplete_reason,
        read_evidence={"deals": deal_evidence, "orders": order_evidence},
        detail=None if deals_available else "terminal exposes no deal history",
        provenance=PROV_LIVE_MT5)


def _derive_closed_positions(deals, open_position_ids, *, provenance) -> tuple:
    """A position id seen in deal history but absent from the open book is
    evidence of closure. This is DERIVED, and labelled as such — MT5 exposes no
    closed-position object."""
    open_set = {str(i) for i in open_position_ids}
    by_position: dict[str, list] = {}
    for deal in deals:
        if deal.position_id and deal.is_trade_deal:
            by_position.setdefault(deal.position_id, []).append(deal)
    out = []
    for position_id in sorted(by_position):
        group = sorted(by_position[position_id], key=lambda d: (d.at or "", d.deal_id))
        still_open = position_id in open_set
        out.append(BrokerClosedPositionEvidence(
            position_id=position_id,
            symbol=group[0].symbol,
            last_seen_open_at=group[0].at,
            observed_closed_at=None if still_open else (group[-1].at),
            still_open=still_open, provenance=provenance))
    return tuple(out)


# ─────────────────────────────────────────────────────────────────────────────
# Mock reader — deterministic history from fixture closed-trade evidence.
# ─────────────────────────────────────────────────────────────────────────────

def read_mock_history(closed_trades, *, at: str,
                      account_currency: str | None = None,
                      account_fingerprint: str | None = None,
                      open_position_ids=()) -> BrokerHistorySnapshot:
    """Deterministic mock history derived from fixture trades that carry close
    evidence. Two deals per trade (one IN, one OUT) with identifiers derived
    from the trade id, so repeated reads are byte-identical.

    Costs are reported UNAVAILABLE: the fixture world records none, and the mock
    must not invent commission or swap."""
    deals: list = []
    for trade in sorted(closed_trades, key=lambda t: str(t.get("tradeId") or "")):
        trade_id = str(trade.get("tradeId") or "")
        if not trade_id:
            continue
        entry_price = _num(trade.get("entry"))
        exit_price = _num(trade.get("closePrice"))
        volume = _num(trade.get("size"))
        if entry_price is None or exit_price is None or volume is None:
            continue                            # insufficient evidence -> skipped
        key = (trade.get("scenarioKey") or "")
        side = "buy" if ":long:" in key or key.endswith(":long") else "sell"
        symbol = key.split(":")[0] if key else trade.get("instrument")
        position_id = str(trade.get("brokerOrderId") or trade_id)
        gross = ((exit_price - entry_price) if side == "buy"
                 else (entry_price - exit_price)) * volume
        common = dict(position_id=position_id, symbol=symbol, volume=volume,
                      costs=BrokerCostEvidence(availability=UNAVAILABLE,
                                               provenance=PROV_MOCK_FIXTURE),
                      provenance=PROV_MOCK_FIXTURE)
        deals.append(BrokerDealRecord(
            deal_id=f"{position_id}-in", order_id=position_id,
            deal_type=side, entry=ENTRY_IN, price=entry_price, profit=None,
            at=trade.get("openedAt"), **common))
        deals.append(BrokerDealRecord(
            deal_id=f"{position_id}-out", order_id=position_id,
            deal_type=("sell" if side == "buy" else "buy"), entry=ENTRY_OUT,
            price=exit_price, profit=round(gross, 6),
            at=trade.get("closedAt"), **common))
    closed = _derive_closed_positions(deals, open_position_ids,
                                      provenance=PROV_MOCK_FIXTURE)
    return BrokerHistorySnapshot(
        at=at, availability=AVAILABLE,
        # The fixture world is a hedging-style book (each trade is its own
        # position), and it is labelled as the mock so nothing reads it as live.
        account_mode=MODE_HEDGING,
        account_currency=account_currency, account_fingerprint=account_fingerprint,
        deals=tuple(deals), orders=(), closed_positions=closed,
        open_position_ids=tuple(sorted(str(i) for i in open_position_ids)),
        deals_available=True, orders_available=False, costs_available=False,
        # Exhaustive BY CONSTRUCTION: the fixture is an in-memory list read in
        # full, with no window, no cap and no pagination to defeat. Stated
        # explicitly because `deals_complete` defaults to False, and leaving it
        # to the default would silently make the whole mock path non-ingestable.
        deals_complete=True, orders_complete=False,
        detail="mock fixture history; costs are not recorded by the fixture world",
        provenance=PROV_MOCK_FIXTURE)


# ═══════════════════════════════════════════════════════════════════════════
# BH — EXHAUSTIVE HISTORY READS.
#
# THE DEFECT THIS REPLACES
#     `history_deals_get(window_from, window_to)` returns an UNORDERED
#     collection, and the reader did:
#
#         for raw in list(raw_deals)[:limit]:
#
#     which silently discards valid deals from the requested interval. A future
#     ingestion watermark advanced past a truncated window would make that loss
#     permanent AND invisible — the reason ledger continuity is blocked.
#
#     A second, quieter defect sat beside it: `history_deals(...) or []` mapped
#     an SDK `None` — which is how the SDK reports failure — onto an empty list
#     while ALSO setting `deals_available = True`. A failed read was therefore
#     reported as a successful empty history.
#
# WHAT THE SOURCE ACTUALLY GUARANTEES (audited from the adapter, not assumed)
#     stable unique id .... YES  — `deal_id = str(raw.ticket)`, immutable
#     timestamps .......... SECOND granularity AT THIS BOUNDARY: the mapper reads
#                           `raw.time` and never `time_msc`, so whatever the
#                           terminal stores, sub-second discrimination is not
#                           available to this reader. One second is therefore the
#                           finest window this code can act on — which is a fact
#                           about the ADAPTER, not a proven property of the source
#     bounded queries ..... YES  — an explicit (from, to) datetime window
#     overlap queries ..... YES  — windows may overlap freely
#     ordering ............ NO   — nothing sorts, nothing documents order
#     pagination .......... NO   — no offset, cursor or count parameter
#     total count ......... NO
#     server-side cap ..... UNKNOWN and UNDETECTABLE from the client
#     update timestamps ... NO   — historical corrections are undetectable
#     deletion semantics .. NO
#
# THE CONSEQUENCE OF "server-side cap UNKNOWN"
#     If a window returns a large number of records we cannot distinguish "that
#     is all there is" from "the terminal capped the result". So the ceiling here
#     is a SAFETY CEILING, not pagination: reaching it means the read is not
#     provably exhaustive, and the only honest responses are to subdivide the
#     window or to declare the interval incomplete. It is never a licence to
#     keep the first N.
#
# TERMINATION
#     Windows halve until either the record count is below the ceiling or the
#     window reaches ONE SECOND — the finest interval this adapter can express,
#     since it reads only second-granular `time`. Subdividing below that cannot
#     separate records it is unable to tell apart, so the interval is returned
#     INCOMPLETE with an explicit reason rather than subdivided pointlessly.
#     This is the case the design refuses to fake.
#
#     The safety property does NOT depend on that precision argument: at every
#     floor, ceiling and failure the reader returns a NON-complete status. It can
#     fail to prove exhaustiveness; it cannot silently discard.
# ═══════════════════════════════════════════════════════════════════════════

#: Read outcomes. `complete` is the ONLY status a ledger may ingest from.
READ_COMPLETE = "complete"
READ_UNAVAILABLE = "unavailable"          # source absent / not connected
READ_ERROR = "error"                      # source attempted and failed
READ_INVALID_INTERVAL = "invalid_interval"
READ_INCOMPLETE = "incomplete"            # exhaustiveness not provable

#: Why an interval could not be proven exhaustive.
INCOMPLETE_DENSE_SECOND = "dense_second_bucket_exceeds_ceiling"
INCOMPLETE_PARTITION_CEILING = "partition_ceiling_reached"
INCOMPLETE_PARTIAL_FAILURE = "partition_failed"
#: The source's own record count exceeds what the walk retrieved. Direct proof
#: of truncation, and the ONLY check that survives an indivisible dense second.
INCOMPLETE_COUNT_MISMATCH = "source_count_exceeds_retrieved"

#: A window returning at least this many records is not provably exhaustive.
DEFAULT_SAFETY_CEILING = 1000
#: Hard stop on how many source calls one interval may cost.
DEFAULT_MAX_PARTITIONS = 512
#: The atomic interval: the source reports whole seconds.
MIN_WINDOW_SECONDS = 1
#: Halvings calibration may follow when one side carries every record. A 7-day
#: interval reaches one second in ~20, so this bounds cost without truncating
#: the search in practice.
MAX_CALIBRATION_DEPTH = 24


@dataclass(frozen=True)
class HistoryReadStats:
    """Evidence about HOW an interval was read. Diagnostics render this."""
    requested_from: str | None = None
    requested_to: str | None = None
    source_calls: int = 0
    partitions: int = 0
    max_depth: int = 0
    raw_records: int = 0
    unique_records: int = 0
    duplicates_removed: int = 0
    rejected_records: int = 0
    earliest_at: str | None = None
    latest_at: str | None = None
    duration_ms: float | None = None
    ceiling: int = DEFAULT_SAFETY_CEILING
    ceiling_reached: bool = False
    #: A cap the SOURCE was proven to enforce, via subdivision consistency.
    detected_source_cap: int | None = None
    #: The source's own count for the interval, when the build exposes one.
    #: None means UNAVAILABLE — never conflate that with zero.
    source_reported_count: int | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "requestedFrom": self.requested_from,
            "requestedTo": self.requested_to,
            "sourceCalls": self.source_calls, "partitions": self.partitions,
            "maxDepth": self.max_depth, "rawRecords": self.raw_records,
            "uniqueRecords": self.unique_records,
            "duplicatesRemoved": self.duplicates_removed,
            "rejectedRecords": self.rejected_records,
            "earliestAt": self.earliest_at, "latestAt": self.latest_at,
            "durationMs": self.duration_ms, "ceiling": self.ceiling,
            "ceilingReached": self.ceiling_reached,
            "detectedSourceCap": self.detected_source_cap,
            "sourceReportedCount": self.source_reported_count,
        })


@dataclass(frozen=True)
class HistoryReadResult:
    """An interval read, with its completeness stated rather than implied.

    `records` is meaningful ONLY when `complete` is true. Every other status
    carries records that may be partial, which is why callers that persist
    anything must check `complete` first.
    """
    status: str
    records: tuple = field(default_factory=tuple)
    stats: HistoryReadStats = field(default_factory=HistoryReadStats)
    incomplete_reason: str | None = None
    detail: str | None = None

    @property
    def complete(self) -> bool:
        """True only for a provably exhaustive read. An empty COMPLETE result is
        a real answer ("nothing happened"); an empty INCOMPLETE one is not."""
        return self.status == READ_COMPLETE

    @property
    def empty(self) -> bool:
        return not self.records

    def as_dict(self) -> dict:
        return _sorted({
            "status": self.status, "complete": self.complete,
            "recordCount": len(self.records),
            "incompleteReason": self.incomplete_reason, "detail": self.detail,
            "stats": self.stats.as_dict(),
        })


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")



def detect_source_cap(fetch, *, window_from: datetime, window_to: datetime,
                      identify, _depth: int = 0) -> int | None:
    """Prove whether the source truncates, by SUBDIVISION CONSISTENCY.

    A configured safety ceiling only protects against a cap at or above it. A
    source that silently caps BELOW the ceiling never trips subdivision, so the
    read looks complete while dropping everything past the cap — the original
    defect, merely relocated. Measured on a 5,000-record interval with a
    ceiling of 1,000: a 500-record server cap lost 4,500 records and still
    reported `complete`.

    The detection is exact, not heuristic. Query the whole interval, then query
    its two halves. Every record in a half lies inside the whole, so with an
    honest source the halves can never reveal an id the whole did not return.
    If they do, the whole response was truncated, and the number it returned IS
    the cap.

    One split is not enough. When every record falls in ONE half, a capped
    source returns the same subset for that half as for the whole, so nothing is
    revealed and the cap stays hidden — measured: 2,000 records with a 300 cap
    went undetected and 1,700 were lost. So when one side carries everything,
    calibration recurses into it until the records genuinely straddle a midpoint
    and the cap becomes visible.

    Residual limit, stated because it cannot be removed here: if every record
    shares ONE second, no subdivision can separate them, and a capped response is
    observationally identical to an honest one. Only `count_source` closes that
    case.

    Returns the observed cap, or None when no truncation is demonstrable. Total —
    any failure yields None and lets the main read surface the error.
    """
    def ids(lo, hi):
        try:
            raw = fetch(lo, hi)
        except Exception:                                       # noqa: BLE001
            return None
        if raw is None:
            return None
        found = set()
        for record in raw:
            key = identify(record)
            if key is not None:
                found.add(str(key))
        return found

    whole = ids(window_from, window_to)
    if not whole:
        return None                     # empty or failed: nothing to prove

    span = (window_to - window_from).total_seconds()
    if span < 2:
        return None                     # indivisible at this adapter's precision

    mid = window_from + timedelta(seconds=int(span // 2))
    left = ids(window_from, mid)
    right = ids(mid, window_to)
    if left is None or right is None:
        return None

    if (left | right) - whole:
        return len(whole)                      # proven: the whole was truncated

    # Nothing revealed. If one side holds every record, a cap could still be
    # hiding behind an identical response — narrow onto the occupied side.
    if _depth < MAX_CALIBRATION_DEPTH:
        if left and not right:
            return detect_source_cap(fetch, window_from=window_from,
                                     window_to=mid, identify=identify,
                                     _depth=_depth + 1)
        if right and not left:
            return detect_source_cap(fetch, window_from=mid,
                                     window_to=window_to, identify=identify,
                                     _depth=_depth + 1)
    return None


def read_interval_exhaustively(fetch, *, window_from: datetime,
                               window_to: datetime,
                               identify=lambda raw: getattr(raw, "ticket", None),
                               timestamp_of=lambda raw: getattr(raw, "time", None),
                               ceiling: int = DEFAULT_SAFETY_CEILING,
                               max_partitions: int = DEFAULT_MAX_PARTITIONS,
                               calibrate: bool = True,
                               count_source=None,
                               monotonic=None) -> HistoryReadResult:
    """Read `[window_from, window_to]` exhaustively, or say why it could not be.

    `fetch(lo, hi)` is the source call. It must return a sequence, or `None` to
    signal failure — `None` is NEVER treated as an empty interval, because that
    conflation is precisely what let a failed read look like a quiet market.

    Windows are treated as INCLUSIVE at both ends. The adapter cannot verify the
    terminal's boundary convention, so the algorithm is made boundary-AGNOSTIC
    instead: adjacent partitions share their edge second and duplicates are
    removed by stable id. Losing a deal at a boundary is unrecoverable;
    re-reading one is free.

    `count_source(lo, hi)` is OPTIONAL and returns the source's own record count
    (MT5 exposes `history_deals_total`). When supplied it is the strongest check
    available: retrieving fewer unique records than the source itself reports is
    direct proof of truncation, and it is the ONLY check that survives the case
    subdivision cannot reach — every record sharing one indivisible second, where
    a capped response and an honest one are observationally identical. Absent, the
    reader falls back to calibration alone and that residual blind spot remains;
    it is recorded in the result rather than papered over.
    """
    import time as _time
    clock = monotonic or _time.monotonic
    started = clock()

    if window_from > window_to:
        return HistoryReadResult(
            status=READ_INVALID_INTERVAL,
            stats=HistoryReadStats(requested_from=_iso(window_from),
                                   requested_to=_iso(window_to), ceiling=ceiling),
            detail="window_from is after window_to")

    # Lower the ceiling to any cap the source demonstrably enforces. Without
    # this the ceiling only defends against caps at or above it, and a smaller
    # one truncates silently while still reporting `complete`.
    detected_cap = None
    calibration_calls = 0
    if calibrate:
        probe = {"n": 0}
        def counted(lo, hi):
            probe["n"] += 1
            return fetch(lo, hi)
        detected_cap = detect_source_cap(counted, window_from=window_from,
                                         window_to=window_to, identify=identify)
        calibration_calls = probe["n"]
        if detected_cap is not None and detected_cap < ceiling:
            ceiling = detected_cap

    state = {"calls": calibration_calls, "partitions": 0, "raw": 0, "depth": 0,
             "ceiling_reached": False}
    by_id: dict = {}
    rejected = 0
    failure: dict | None = None

    def visit(lo: datetime, hi: datetime, depth: int) -> str | None:
        """Read one partition. Returns an incomplete-reason, or None on success."""
        nonlocal rejected, failure
        if state["partitions"] >= max_partitions:
            state["ceiling_reached"] = True
            return INCOMPLETE_PARTITION_CEILING
        state["partitions"] += 1
        state["depth"] = max(state["depth"], depth)
        state["calls"] += 1
        try:
            raw = fetch(lo, hi)
        except Exception as exc:                                # noqa: BLE001
            failure = {"detail": f"{type(exc).__name__}: {exc}"}
            return INCOMPLETE_PARTIAL_FAILURE
        if raw is None:
            # The SDK's failure signal. Distinct from an empty interval.
            failure = {"detail": "source returned None (read failed)"}
            return INCOMPLETE_PARTIAL_FAILURE

        records = list(raw)
        state["raw"] += len(records)

        span = (hi - lo).total_seconds()
        if len(records) >= ceiling:
            state["ceiling_reached"] = True
            if span > MIN_WINDOW_SECONDS:
                # Not provably exhaustive: subdivide. Both halves INCLUDE the
                # midpoint second so a deal sitting exactly on the boundary
                # cannot fall between them.
                mid = lo + timedelta(seconds=int(span // 2))
                if mid <= lo:
                    mid = lo + timedelta(seconds=MIN_WINDOW_SECONDS)
                if mid >= hi:
                    return INCOMPLETE_DENSE_SECOND
                left = visit(lo, mid, depth + 1)
                if left:
                    return left
                return visit(mid, hi, depth + 1)
            # One second, still at the ceiling. This adapter reads only
            # second-granular `time`, so a narrower window cannot separate
            # records it cannot tell apart. Exhaustiveness is unprovable here —
            # refuse rather than pretend.
            return INCOMPLETE_DENSE_SECOND

        for raw_record in records:
            key = identify(raw_record)
            if key is None:
                rejected += 1                  # unusable id: cannot dedup it
                continue
            by_id[str(key)] = raw_record
        return None

    reason = visit(window_from, window_to, 0)
    duration = round((clock() - started) * 1000.0, 3)

    stamps = []
    for record in by_id.values():
        value = timestamp_of(record)
        if value is not None:
            stamps.append(value)

    # CANONICAL ORDER: (timestamp, stable id). Source order is never trusted —
    # nothing sorts it and nothing documents it, so a shuffled source must not
    # change the output.
    ordered = tuple(sorted(
        by_id.values(),
        key=lambda r: (timestamp_of(r) if timestamp_of(r) is not None else 0,
                       str(identify(r)))))

    reported = None
    if count_source is not None:
        try:
            raw_total = count_source(window_from, window_to)
            if isinstance(raw_total, int) and not isinstance(raw_total, bool) \
                    and raw_total >= 0:
                reported = raw_total
        except Exception:                                       # noqa: BLE001
            reported = None          # unavailable, not zero

    stats = HistoryReadStats(
        requested_from=_iso(window_from), requested_to=_iso(window_to),
        source_calls=state["calls"], partitions=state["partitions"],
        max_depth=state["depth"], raw_records=state["raw"],
        unique_records=len(by_id),
        duplicates_removed=max(0, state["raw"] - len(by_id) - rejected),
        rejected_records=rejected,
        earliest_at=(str(min(stamps)) if stamps else None),
        latest_at=(str(max(stamps)) if stamps else None),
        duration_ms=duration, ceiling=ceiling,
        ceiling_reached=state["ceiling_reached"],
        detected_source_cap=detected_cap, source_reported_count=reported)

    # Cross-check against the source's OWN count where the build provides one.
    if reported is not None and len(by_id) < reported:
        return HistoryReadResult(
            status=READ_INCOMPLETE, records=ordered, stats=stats,
            incomplete_reason=INCOMPLETE_COUNT_MISMATCH,
            detail=(f"source reports {reported} records for this interval but "
                    f"only {len(by_id)} were retrieved"))

    if reason:
        return HistoryReadResult(
            status=(READ_ERROR if reason == INCOMPLETE_PARTIAL_FAILURE
                    else READ_INCOMPLETE),
            records=ordered, stats=stats, incomplete_reason=reason,
            detail=(failure or {}).get("detail"))
    return HistoryReadResult(status=READ_COMPLETE, records=ordered, stats=stats)
