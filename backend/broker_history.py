"""LIVE-4C — the canonical READ-ONLY broker-history contract.

AUDIT FINDING THAT MOTIVATES THIS MODULE
    Before LIVE-4C the broker abstraction did NOT expose enough historical
    evidence to reconstruct a closed trade. Grounded in production code:

      * `history_deals_get` was reached via `getattr` inside
        `broker.recent_executions` only — optional, 1-day window, 50-deal cap;
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
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
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
    detail: str | None = None
    provenance: str = PROV_ABSENT
    schema_version: str = SCHEMA_VERSION

    @property
    def usable(self) -> bool:
        """Deal evidence is the minimum for any reconstruction."""
        return self.availability == AVAILABLE and self.deals_available

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
            "usable": self.usable, "detail": self.detail,
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
    history_deals = getattr(sdk, "history_deals_get", None)
    if callable(history_deals):
        try:
            raw_deals = history_deals(window_from, window_to) or []
            deals_available = True
            for raw in list(raw_deals)[:limit]:
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
    history_orders = getattr(sdk, "history_orders_get", None)
    if callable(history_orders):
        try:
            raw_orders = history_orders(window_from, window_to) or []
            orders_available = True
            for raw in list(raw_orders)[:limit]:
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
        detail="mock fixture history; costs are not recorded by the fixture world",
        provenance=PROV_MOCK_FIXTURE)
