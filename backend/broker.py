"""
Broker abstraction layer (Phase 6).

The runtime communicates with execution ONLY through the `Broker` interface.
Everything runs against `MockBroker` — the authoritative implementation — exactly
as before; the interface is the seam that a real adapter (MT5, others) plugs into
later. There is ZERO live trading and ZERO MT5 communication here.

`broker.py` imports nothing from `server.py`. The runtime injects the overlay
primitives it needs through a `BrokerContext`, so the broker never reaches into
server internals and no MT5-specific structure leaks into the runtime.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------

class ConnectionState:
    DISCONNECTED = "Disconnected"
    CONNECTING = "Connecting"
    CONNECTED = "Connected"
    DEGRADED = "Degraded"
    RECONNECTING = "Reconnecting"
    OFFLINE = "Offline"


# ---------------------------------------------------------------------------
# Canonical broker models — runtime-facing, broker-agnostic. No MT5 structures.
# ---------------------------------------------------------------------------

@dataclass
class BrokerCapability:
    supportsMarketExecution: bool = False
    supportsPendingOrders: bool = False
    supportsModify: bool = False
    supportsPartialClose: bool = False
    supportsHedging: bool = False
    supportsNetting: bool = False
    supportsReplay: bool = False


@dataclass
class BrokerConnection:
    state: str = ConnectionState.DISCONNECTED
    since: str | None = None
    detail: str = ""


@dataclass
class BrokerHealth:
    brokerId: str
    kind: str
    connection: str
    latencyMs: int | None = None
    lastSyncAt: str | None = None
    detail: str = ""


@dataclass
class BrokerSymbol:
    canonical: str
    brokerSymbol: str


@dataclass
class BrokerAccount:
    accountId: str
    brokerId: str
    type: str
    baseCurrency: str
    balance: float | None = None
    equity: float | None = None


@dataclass
class BrokerOrder:
    orderId: str
    canonicalSymbol: str
    brokerSymbol: str
    side: str
    size: float | None
    state: str
    deploymentId: str | None = None


@dataclass
class BrokerPosition:
    positionId: str
    canonicalSymbol: str
    brokerSymbol: str
    side: str
    size: float | None
    entry: float | None
    sl: float | None
    tp: float | None
    state: str
    deploymentId: str | None = None


@dataclass
class BrokerError:
    code: str
    message: str
    recoverable: bool = True


# Sentinels for unsupported operations (MT5 skeleton returns these).
UNSUPPORTED = BrokerError(code="unsupported", message="Operation not supported by this broker", recoverable=False)
NOT_IMPLEMENTED = BrokerError(code="not_implemented", message="Adapter is a skeleton — no implementation", recoverable=False)


# ---------------------------------------------------------------------------
# Symbol translation — runtime always uses canonical symbols; the broker maps
# to/from its own aliases (e.g. EURUSD ↔ EURUSD.r / EURUSD.a).
# ---------------------------------------------------------------------------

class SymbolTranslator:
    def __init__(self, aliases: dict[str, str] | None = None):
        # canonical -> broker alias
        self._to_broker = dict(aliases or {})
        self._to_canonical = {v: k for k, v in self._to_broker.items()}

    def to_broker(self, canonical: str) -> str:
        return self._to_broker.get(canonical, canonical)

    def to_canonical(self, broker_symbol: str) -> str:
        if broker_symbol in self._to_canonical:
            return self._to_canonical[broker_symbol]
        # strip common broker suffixes (.r/.a/.pro/...) as a fallback
        base = broker_symbol.split(".")[0]
        return base or broker_symbol


# ---------------------------------------------------------------------------
# Runtime → broker injection: the primitives the broker needs to execute against
# the runtime overlay, provided by the runtime so the broker stays decoupled.
# ---------------------------------------------------------------------------

@dataclass
class BrokerContext:
    now: str
    reason: str | None
    payload: dict
    operator_id: str
    trade_current: Callable[[str], dict | None]
    trade_by_order_id: Callable[[str], dict | None]
    append_trade_management: Callable[..., None]
    mgmt_entry: Callable[..., dict]
    close_trade: Callable[[str, str, str | None], tuple]
    snake_upper: Callable[[str], str]
    live_trades: Callable[[], list]
    accounts: Callable[[], list]
    brokers: Callable[[], list]


# ---------------------------------------------------------------------------
# Broker interface — the single execution boundary.
# ---------------------------------------------------------------------------

class Broker(ABC):
    kind: str = "broker"
    broker_id: str = "brk_unknown"

    @abstractmethod
    def connect(self) -> BrokerConnection: ...

    @abstractmethod
    def disconnect(self) -> BrokerConnection: ...

    @abstractmethod
    def connection(self) -> BrokerConnection: ...

    @abstractmethod
    def health(self) -> BrokerHealth: ...

    @abstractmethod
    def capabilities(self) -> BrokerCapability: ...

    @abstractmethod
    def accounts(self, ctx: BrokerContext) -> list: ...

    @abstractmethod
    def positions(self, ctx: BrokerContext) -> list: ...

    @abstractmethod
    def orders(self, ctx: BrokerContext) -> list: ...

    @abstractmethod
    def submit_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        """Execute a trade/order command. Returns (before, after) for the audit event."""

    @abstractmethod
    def cancel_order(self, order_id: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]: ...

    @abstractmethod
    def modify_order(self, order_id: str, changes: dict, ctx: BrokerContext) -> tuple[dict | None, dict | None]: ...

    @abstractmethod
    def flatten(self, deployment_id: str, ctx: BrokerContext) -> list[str]: ...

    @abstractmethod
    def sync(self, ctx: BrokerContext) -> dict: ...

    def translate_symbol(self, canonical: str) -> str:
        return canonical

    def to_canonical(self, broker_symbol: str) -> str:
        return broker_symbol


# ---------------------------------------------------------------------------
# Mock Broker — the authoritative implementation. All execution semantics that
# used to live inline in the runtime now live here, unchanged in behaviour,
# operating through the injected BrokerContext (which maps 1:1 to the runtime
# overlay primitives).
# ---------------------------------------------------------------------------

# Trade/order commands the broker owns (execution boundary). Deployment/package
# lifecycle stays in the runtime control-plane.
BROKER_COMMANDS = {
    "CloseTrade", "SLToBE", "MoveTradeSL", "MoveTradeTP", "PartialClose",
    "ReduceTradeRisk", "SetAutoManagement",
    "CancelOrder", "ReduceOrderRisk", "ConvertOrderToGhost",
}


class MockBroker(Broker):
    kind = "mock"
    broker_id = "brk_mock"

    def __init__(self):
        self._conn = BrokerConnection(state=ConnectionState.CONNECTED, detail="In-process mock broker")
        self._symbols = SymbolTranslator()  # identity — mock uses canonical symbols

    # --- lifecycle ---
    def connect(self) -> BrokerConnection:
        self._conn = BrokerConnection(state=ConnectionState.CONNECTED, detail="In-process mock broker")
        return self._conn

    def disconnect(self) -> BrokerConnection:
        self._conn = BrokerConnection(state=ConnectionState.DISCONNECTED, detail="Mock broker disconnected")
        return self._conn

    def connection(self) -> BrokerConnection:
        return self._conn

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            brokerId=self.broker_id, kind=self.kind, connection=self._conn.state,
            latencyMs=0, detail="Mock broker — full capability, no live execution",
        )

    def capabilities(self) -> BrokerCapability:
        return BrokerCapability(
            supportsMarketExecution=True, supportsPendingOrders=True, supportsModify=True,
            supportsPartialClose=True, supportsHedging=True, supportsNetting=True, supportsReplay=True,
        )

    def translate_symbol(self, canonical: str) -> str:
        return self._symbols.to_broker(canonical)

    def to_canonical(self, broker_symbol: str) -> str:
        return self._symbols.to_canonical(broker_symbol)

    # --- reads (derived from the runtime view via ctx) ---
    def accounts(self, ctx: BrokerContext) -> list:
        return [
            asdict(BrokerAccount(
                accountId=a.get("accountId", ""), brokerId=a.get("brokerId", ""),
                type=a.get("type", ""), baseCurrency=a.get("baseCurrency", ""),
                balance=a.get("balance"), equity=a.get("equity"),
            ))
            for a in ctx.accounts()
        ]

    def positions(self, ctx: BrokerContext) -> list:
        out = []
        for t in ctx.live_trades():
            if t.get("state") in ("managing", "InTrade", "open", "triggered", "filled"):
                pair = (t.get("scenarioKey", "") or "").split(":")[0]
                out.append(asdict(BrokerPosition(
                    positionId=t.get("tradeId", ""), canonicalSymbol=pair,
                    brokerSymbol=self.translate_symbol(pair),
                    side=("long" if "long" in (t.get("scenarioKey", "") or "") else "short"),
                    size=t.get("size"), entry=t.get("entry"), sl=t.get("sl"), tp=t.get("tp"),
                    state=t.get("state", ""), deploymentId=t.get("deploymentId"),
                )))
        return out

    def orders(self, ctx: BrokerContext) -> list:
        out = []
        for t in ctx.live_trades():
            if t.get("state") == "pending":
                pair = (t.get("scenarioKey", "") or "").split(":")[0]
                out.append(asdict(BrokerOrder(
                    orderId=t.get("brokerOrderId") or t.get("tradeId", ""), canonicalSymbol=pair,
                    brokerSymbol=self.translate_symbol(pair),
                    side=("long" if "long" in (t.get("scenarioKey", "") or "") else "short"),
                    size=t.get("size"), state=t.get("state", ""), deploymentId=t.get("deploymentId"),
                )))
        return out

    def sync(self, ctx: BrokerContext) -> dict:
        return {
            "ok": True, "brokerId": self.broker_id, "connection": self._conn.state,
            "positions": len(self.positions(ctx)), "orders": len(self.orders(ctx)), "at": ctx.now,
        }

    # --- execution (moved verbatim from the runtime; behaviour identical) ---
    def submit_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        payload, now, reason = ctx.payload, ctx.now, ctx.reason

        if name in ("CancelOrder", "ReduceOrderRisk", "ConvertOrderToGhost"):
            return self._order_command(name, ctx)

        if name == "CloseTrade":
            tid = payload.get("tradeId")
            cur = ctx.trade_current(tid) if tid else None
            if not cur:
                return None, None
            return ctx.close_trade(tid, now, reason)

        if name == "SLToBE":
            tid = payload.get("tradeId")
            cur = ctx.trade_current(tid) if tid else None
            if not cur:
                return None, None
            before = {"sl": cur.get("sl")}
            after = {"sl": cur.get("entry")}
            ctx.append_trade_management(tid, ctx.mgmt_entry("SL_TO_BE", now, before, after, reason or "moved to break-even"),
                                        scalars={"sl": cur.get("entry"), "protectionStatus": "protected (SL@BE)"})
            return before, after

        if name in ("MoveTradeSL", "MoveTradeTP"):
            tid = payload.get("tradeId")
            cur = ctx.trade_current(tid) if tid else None
            if not cur:
                return None, None
            field = "sl" if name == "MoveTradeSL" else "tp"
            price = payload.get("price")
            before = {field: cur.get(field)}
            if price is None:
                ctx.append_trade_management(tid, ctx.mgmt_entry(ctx.snake_upper(name), now, before, None, reason or "move requested (no price)"))
                return before, None
            after = {field: price}
            ctx.append_trade_management(tid, ctx.mgmt_entry(ctx.snake_upper(name), now, before, after, reason),
                                        scalars={field: price})
            return before, after

        if name in ("PartialClose", "ReduceTradeRisk"):
            tid = payload.get("tradeId")
            cur = ctx.trade_current(tid) if tid else None
            if not cur:
                return None, None
            size = cur.get("size")
            new_size = round(size / 2, 4) if isinstance(size, (int, float)) else size
            before = {"size": size}
            after = {"size": new_size}
            kind = "PARTIAL_CLOSE" if name == "PartialClose" else "RISK_REDUCED"
            ctx.append_trade_management(tid, ctx.mgmt_entry(kind, now, before, after, reason or "reduced"),
                                        scalars={"size": new_size})
            return before, after

        if name == "SetAutoManagement":
            tid = payload.get("tradeId")
            cur = ctx.trade_current(tid) if tid else None
            if not cur:
                return None, None
            enabled = bool(payload.get("enabled"))
            before = {"autoManaged": cur.get("autoManaged")}
            after = {"autoManaged": enabled}
            ctx.append_trade_management(tid, ctx.mgmt_entry("AUTO_MGMT", now, before, after, reason),
                                        scalars={"autoManaged": enabled})
            return before, after

        return None, None

    def _order_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        payload, now, reason = ctx.payload, ctx.now, ctx.reason
        order_id = payload.get("orderId")
        cur = ctx.trade_by_order_id(order_id) if order_id else None
        if not cur:
            return None, None
        tid = cur["tradeId"]
        if name == "CancelOrder":
            return self.cancel_order(order_id, ctx)
        if name == "ReduceOrderRisk":
            size = cur.get("size")
            new_size = round(size / 2, 4) if isinstance(size, (int, float)) else size
            before, after = {"size": size}, {"size": new_size}
            ctx.append_trade_management(tid, ctx.mgmt_entry("ORDER_RISK_REDUCED", now, before, after, reason or "order risk reduced"),
                                        scalars={"size": new_size})
            return before, after
        # ConvertOrderToGhost
        before = {"state": cur.get("state"), "lane": cur.get("lane")}
        after = {"state": "ghost", "lane": "ghost"}
        ctx.append_trade_management(tid, ctx.mgmt_entry("ORDER_TO_GHOST", now, before, after, reason or "converted to ghost"),
                                    scalars={"state": "ghost", "lane": "ghost"})
        return before, after

    def cancel_order(self, order_id: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        cur = ctx.trade_by_order_id(order_id) if order_id else None
        if not cur:
            return None, None
        tid = cur["tradeId"]
        before = {"state": cur.get("state")}
        after = {"state": "cancelled"}
        ctx.append_trade_management(tid, ctx.mgmt_entry("ORDER_CANCELLED", ctx.now, before, after, ctx.reason or "order cancelled"),
                                    scalars={"state": "cancelled"})
        return before, after

    def modify_order(self, order_id: str, changes: dict, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        cur = ctx.trade_by_order_id(order_id) if order_id else None
        if not cur:
            return None, None
        tid = cur["tradeId"]
        before = {k: cur.get(k) for k in changes}
        ctx.append_trade_management(tid, ctx.mgmt_entry("ORDER_MODIFIED", ctx.now, before, changes, ctx.reason or "order modified"),
                                    scalars=dict(changes))
        return before, changes

    def flatten(self, deployment_id: str, ctx: BrokerContext) -> list[str]:
        closed: list[str] = []
        for t in ctx.live_trades():
            if t.get("deploymentId") == deployment_id and t.get("state") not in ("closed",):
                ctx.close_trade(t["tradeId"], ctx.now, "flattened with deployment")
                closed.append(t["tradeId"])
        return closed


# ---------------------------------------------------------------------------
# MT5 adapter — SKELETON ONLY. No terminal, no DLL, no socket, no credentials,
# no Python bridge, no implementation. Establishes the seam; every op is a safe
# no-op returning Unsupported / NotImplemented / empty. Realistic capability
# placeholders. Never connects.
# ---------------------------------------------------------------------------

class MT5Adapter(Broker):
    kind = "mt5"
    broker_id = "brk_mt5_skeleton"

    def __init__(self):
        # MT5 typically exposes broker-suffixed symbols; placeholders only.
        self._symbols = SymbolTranslator({"EURUSD": "EURUSD.r", "GBPUSD": "GBPUSD.r", "XAUUSD": "XAUUSD.a"})

    def connect(self) -> BrokerConnection:
        # Skeleton never connects to a terminal.
        return BrokerConnection(state=ConnectionState.DISCONNECTED, detail="MT5 adapter skeleton — not implemented")

    def disconnect(self) -> BrokerConnection:
        return BrokerConnection(state=ConnectionState.DISCONNECTED, detail="MT5 adapter skeleton")

    def connection(self) -> BrokerConnection:
        return BrokerConnection(state=ConnectionState.DISCONNECTED, detail="MT5 adapter skeleton — no terminal")

    def health(self) -> BrokerHealth:
        return BrokerHealth(brokerId=self.broker_id, kind=self.kind, connection=ConnectionState.DISCONNECTED,
                            detail="Skeleton — no MT5 communication")

    def capabilities(self) -> BrokerCapability:
        # Realistic MT5 placeholders (netting/hedging depend on account type; replay is N/A live).
        return BrokerCapability(
            supportsMarketExecution=True, supportsPendingOrders=True, supportsModify=True,
            supportsPartialClose=True, supportsHedging=True, supportsNetting=True, supportsReplay=False,
        )

    def translate_symbol(self, canonical: str) -> str:
        return self._symbols.to_broker(canonical)

    def to_canonical(self, broker_symbol: str) -> str:
        return self._symbols.to_canonical(broker_symbol)

    def accounts(self, ctx: BrokerContext) -> list:
        return []

    def positions(self, ctx: BrokerContext) -> list:
        return []

    def orders(self, ctx: BrokerContext) -> list:
        return []

    def submit_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None  # Unsupported — skeleton performs no execution.

    def cancel_order(self, order_id: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None

    def modify_order(self, order_id: str, changes: dict, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None

    def flatten(self, deployment_id: str, ctx: BrokerContext) -> list[str]:
        return []

    def sync(self, ctx: BrokerContext) -> dict:
        return {"ok": False, "brokerId": self.broker_id, "connection": ConnectionState.DISCONNECTED,
                "error": asdict(NOT_IMPLEMENTED)}


# ---------------------------------------------------------------------------
# Registry — the active broker. Mock only (no live). Registered so an adapter is
# selectable by config later without touching runtime call-sites.
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Broker] = {"mock": MockBroker(), "mt5": MT5Adapter()}
_ACTIVE = "mock"  # authoritative: the Control Tower runs entirely against MockBroker


def get_broker(kind: str | None = None) -> Broker:
    return _REGISTRY[kind or _ACTIVE]


def active_kind() -> str:
    return _ACTIVE


def capability_dict(cap: BrokerCapability) -> dict:
    return asdict(cap)
