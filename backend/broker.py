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

from dataclasses import dataclass, field, asdict
from typing import Any, Callable

# ARCH-2: the canonical broker adapter contract lives in `broker_adapter.py` — the
# single owner of the adapter interface, capability model, connection-state model,
# result envelope and error model. This module holds the ADAPTER IMPLEMENTATIONS
# (MockBroker, the inert MT5Adapter) and re-exports the contract names so existing
# importers keep working unchanged.
from broker_adapter import (  # noqa: F401 — re-exported contract surface
    NOT_IMPLEMENTED,
    UNSUPPORTED,
    BrokerAccount,
    BrokerAdapter,
    BrokerCapability,
    BrokerConnection,
    BrokerContext,
    BrokerError,
    BrokerHealth,
    BrokerOrder,
    BrokerPosition,
    BrokerResult,
    BrokerSymbol,
    ConnectionState,
    UnknownAdapterError,
)
import broker_adapter as _adapter_boundary


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
# Broker interface — the single execution boundary. ARCH-2: the interface is the
# canonical `BrokerAdapter` contract in `broker_adapter.py`; `Broker` is kept as a
# back-compat alias so every existing import and isinstance check keeps working.
# ---------------------------------------------------------------------------

Broker = BrokerAdapter


# ---------------------------------------------------------------------------
# Mock Broker — the authoritative implementation. All execution semantics that
# used to live inline in the runtime now live here, unchanged in behaviour,
# operating through the injected BrokerContext (which maps 1:1 to the runtime
# overlay primitives).
# ---------------------------------------------------------------------------

# Trade/order commands the broker owns (execution boundary). Deployment/package
# lifecycle stays in the runtime control-plane.
# ARCH-1: DERIVED from the single canonical `command_registry` (the `broker_dispatched`
# flag) rather than re-declared here, so this set can never drift from the catalogue.
import command_registry as _registry

BROKER_COMMANDS = _registry.broker_dispatched_names()


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

    def account_identity(self) -> BrokerResult:
        # The mock's identity is fixed and explicitly labelled as the mock's own —
        # never confusable with a real account fingerprint.
        return BrokerResult(ok=True, code="ok", detail="mock fixture account",
                            data={"accountId": "acc_mock", "brokerId": self.broker_id,
                                  "provenance": "mock-fixture"})

    def reconcile_snapshot(self, ctx: BrokerContext) -> BrokerResult:
        """One coherent snapshot for the canonical reconciliation authority."""
        return BrokerResult(ok=True, code="ok", detail="mock fixture snapshot",
                            data={
                                "positions": self.positions(ctx),
                                "orders": self.orders(ctx),
                                "accounts": self.accounts(ctx),
                                "connection": self._conn.state,
                                "accountIdentity": "acc_mock",
                                "at": ctx.now,
                                "provenance": "mock-fixture",
                            })

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

def _load_live_gateway():
    """Import the live-slice MT5 gateway (repo-root `live/` package). Returns a
    connected-capable gateway or None. All broker-specific logic stays in the
    gateway + this adapter; import is guarded so CT runs unchanged off-VPS."""
    try:
        import os
        import sys
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if root not in sys.path:
            sys.path.insert(0, root)
        from live.config import LiveConfig
        from live.mt5_gateway import MT5Gateway
        gateway = MT5Gateway(LiveConfig())
        return gateway if gateway.available else None
    except Exception:  # pragma: no cover - any import/env failure ⇒ skeleton mode
        return None


class MT5Adapter(Broker):
    """Real MT5 connectivity via the live-slice gateway when the MetaTrader5
    package is present (Windows VPS); graceful skeleton behaviour everywhere
    else. CT-side execution commands remain unsupported in M3 Phase 1 — the VPS
    executor owns order operations; CT observes positions/orders/health."""

    kind = "mt5"
    broker_id = "brk_mt5_live"

    def __init__(self):
        # ARCH-2: construction performs NO gateway work. The gateway is loaded
        # lazily on first use (and on this host resolves to None), so constructing
        # the adapter — which the lazy factory only does on demand anyway — can
        # never touch a terminal or a socket.
        self._symbols = SymbolTranslator({"EURUSD": "EURUSD.r", "GBPUSD": "GBPUSD.r", "XAUUSD": "XAUUSD.a"})
        self._gateway_cache: Any = None
        self._gateway_loaded = False

    @property
    def _gateway(self):
        if not self._gateway_loaded:
            self._gateway_cache = _load_live_gateway()
            self._gateway_loaded = True
        return self._gateway_cache

    def _state(self) -> tuple[str, str]:
        if self._gateway is None:
            return ConnectionState.DISCONNECTED, "MetaTrader5 package unavailable on this host"
        if self._gateway.connected:
            return ConnectionState.CONNECTED, "MT5 terminal connected"
        return ConnectionState.DISCONNECTED, "gateway available; not connected"

    def connect(self) -> BrokerConnection:
        if self._gateway is None:
            return BrokerConnection(state=ConnectionState.DISCONNECTED,
                                    detail="MetaTrader5 package unavailable on this host")
        ok, detail = self._gateway.connect()
        return BrokerConnection(state=ConnectionState.CONNECTED if ok else ConnectionState.DISCONNECTED,
                                detail=detail)

    def disconnect(self) -> BrokerConnection:
        if self._gateway is not None:
            self._gateway.disconnect()
        return BrokerConnection(state=ConnectionState.DISCONNECTED, detail="disconnected")

    def connection(self) -> BrokerConnection:
        state, detail = self._state()
        return BrokerConnection(state=state, detail=detail)

    def health(self) -> BrokerHealth:
        state, detail = self._state()
        return BrokerHealth(brokerId=self.broker_id, kind=self.kind, connection=state, detail=detail)

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

    def _snapshot(self) -> dict | None:
        if self._gateway is None or not self._gateway.connected:
            return None
        ok, snap = self._gateway.snapshot()
        return snap if ok else None

    def accounts(self, ctx: BrokerContext) -> list:
        snap = self._snapshot()
        if not snap or not snap.get("account"):
            return []
        a = snap["account"]
        return [{"accountId": f"mt5_{a['login']}", "broker": self.broker_id,
                 "balance": a["balance"], "equity": a["equity"], "currency": a["currency"]}]

    def positions(self, ctx: BrokerContext) -> list:
        snap = self._snapshot()
        if not snap:
            return []
        return [{"positionId": str(p["ticket"]), "symbol": self.to_canonical(p["symbol"]),
                 "volume": p["volume"], "side": "long" if p["type"] == 0 else "short",
                 "entryPrice": p["price_open"], "stop": p["sl"], "target": p["tp"],
                 "unrealizedPnl": p["profit"], "comment": p["comment"], "magic": p["magic"]}
                for p in snap["positions"]]

    def orders(self, ctx: BrokerContext) -> list:
        snap = self._snapshot()
        if not snap:
            return []
        return [{"orderId": str(o["ticket"]), "symbol": self.to_canonical(o["symbol"]),
                 "type": o["type"], "volume": o["volume"], "price": o["price_open"],
                 "stop": o["sl"], "target": o["tp"], "comment": o["comment"]}
                for o in snap["orders"]]

    # CT-side execution commands are deliberately unsupported in M3 Phase 1:
    # the VPS executor is the only order writer (single-writer safety).
    def submit_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None

    def cancel_order(self, order_id: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None

    def modify_order(self, order_id: str, changes: dict, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        return None, None

    def flatten(self, deployment_id: str, ctx: BrokerContext) -> list[str]:
        return []

    def sync(self, ctx: BrokerContext) -> dict:
        state, detail = self._state()
        if state != ConnectionState.CONNECTED:
            return {"ok": False, "brokerId": self.broker_id, "connection": state,
                    "error": {"code": "NOT_CONNECTED", "detail": detail}}
        return {"ok": True, "brokerId": self.broker_id, "connection": state,
                "positions": self.positions(ctx), "orders": self.orders(ctx),
                "accounts": self.accounts(ctx)}


# ---------------------------------------------------------------------------
# Registry — the active broker. Mock only (no live). Registered so an adapter is
# selectable by config later without touching runtime call-sites.
# ---------------------------------------------------------------------------

# ARCH-2: adapter selection and construction are CENTRALIZED in
# `broker_adapter.get_adapter` — lazy, cached, fail-closed on unknown kinds. The
# import-time `_REGISTRY = {"mock": MockBroker(), "mt5": MT5Adapter()}` is gone:
# importing this module constructs no adapter and probes no gateway. `_ACTIVE`
# remains the authoritative active-kind constant (delegating to the boundary) so
# existing assertions and call sites keep working.
_ACTIVE = _adapter_boundary.ACTIVE_KIND  # "mock" — the Control Tower runs entirely against MockBroker


def get_broker(kind: str | None = None) -> Broker:
    """Back-compat entry point. Delegates to the single centralized factory."""
    return _adapter_boundary.get_adapter(kind or _ACTIVE)


def active_kind() -> str:
    return _adapter_boundary.active_kind()


def capability_dict(cap: BrokerCapability) -> dict:
    return asdict(cap)
