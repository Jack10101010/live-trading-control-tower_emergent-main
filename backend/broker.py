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
    ACK_COMMUNICATION_FAILED,
    ACK_FILLED,
    ACK_NOT_SUBMITTED,
    ACK_PARTIAL,
    ACK_REJECTED,
    ACK_TIMEOUT,
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
    ACK_CONFIRMED,
    CancelPendingOrderRequest,
    ClosePositionRequest,
    ConnectionState,
    MarketOrderAck,
    MarketOrderRequest,
    ModifyPositionProtectionRequest,
    OperationAck,
    RESULT_NOT_CONNECTED,
    RESULT_UNAVAILABLE,
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
            supportsCancelOrder=True, supportsClosePosition=True,
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

    # ── LIVE-2: deterministic fixture market-order execution ─────────────────
    def submit_market_order(self, request: MarketOrderRequest,
                            ctx: BrokerContext) -> BrokerResult:
        """Deterministic FIXTURE execution: same request -> same acknowledgement.
        The ticket derives from the intent id (no randomness, no clock); no live
        broker is touched and no market price is invented (price stays None —
        the mock reports what it knows, which is no market)."""
        import hashlib
        ticket = "mockord_" + hashlib.sha256(request.intent_id.encode()).hexdigest()[:12]
        ack = MarketOrderAck(
            intent_id=request.intent_id,
            status=ACK_FILLED,
            broker_order_ticket=ticket,
            broker_deal_ticket="mockdeal_" + ticket[-12:],
            requested_volume=request.quantity,
            filled_volume=request.quantity,
            price=None,                     # the mock invents no market price
            provenance="mock-fixture",
            detail="mock fixture execution — no live broker action",
            at=ctx.now,
        )
        return BrokerResult(ok=True, code="ok", detail="mock fixture market order",
                            data=ack.as_dict(), broker_ref=ticket)

    # ── LIVE-3: deterministic fixture manual-management operations ───────────
    # Each mutates the runtime overlay through ctx (like submit_command does),
    # so the fixture world's reconcile_snapshot genuinely reflects the change
    # and reconciliation can confirm the operation end-to-end. Deterministic
    # tickets; no invented prices or fills.

    def _op_ticket(self, intent_id: str) -> str:
        import hashlib
        return "mockop_" + hashlib.sha256(intent_id.encode()).hexdigest()[:12]

    def _op_ack(self, request, operation: str, status: str, ctx: BrokerContext,
                reason: str | None = None, detail: str | None = None) -> BrokerResult:
        from broker_adapter import ACK_CONFIRMED, OperationAck
        entity = getattr(request, "position_ref", None) or getattr(request, "order_ref", None)
        ticket = self._op_ticket(request.intent_id)
        ack = OperationAck(intent_id=request.intent_id, operation=operation,
                           status=status, entity_ref=entity,
                           broker_order_ticket=ticket if status == ACK_CONFIRMED else None,
                           reason=reason, detail=detail,
                           provenance="mock-fixture", at=ctx.now)
        ok = status == ACK_CONFIRMED
        return BrokerResult(ok=ok, code="ok" if ok else (reason or "rejected"),
                            detail=detail or "", data=ack.as_dict(),
                            broker_ref=ticket if ok else None)

    def modify_position_protection(self, request, ctx: BrokerContext) -> BrokerResult:
        from broker_adapter import ACK_CONFIRMED, ACK_REJECTED
        cur = ctx.trade_current(request.position_ref)
        if not cur:
            return self._op_ack(request, "modify_position_protection", ACK_REJECTED,
                                ctx, reason="rejected", detail="position not found")
        scalars = {}
        before = {"sl": cur.get("sl"), "tp": cur.get("tp")}
        if request.stop_loss is not None:
            scalars["sl"] = request.stop_loss
        if request.take_profit is not None:
            scalars["tp"] = request.take_profit
        ctx.append_trade_management(
            request.position_ref,
            ctx.mgmt_entry("PROTECTION_MODIFIED", ctx.now, before, dict(scalars),
                           request.reason or "manual protection modification"),
            scalars=scalars)
        return self._op_ack(request, "modify_position_protection", ACK_CONFIRMED, ctx)

    def cancel_pending_order(self, request, ctx: BrokerContext) -> BrokerResult:
        from broker_adapter import ACK_CONFIRMED, ACK_REJECTED
        cur = ctx.trade_by_order_id(request.order_ref)
        if not cur:
            return self._op_ack(request, "cancel_pending_order", ACK_REJECTED,
                                ctx, reason="rejected", detail="pending order not found")
        ctx.append_trade_management(
            cur["tradeId"],
            ctx.mgmt_entry("ORDER_CANCELLED", ctx.now, {"state": cur.get("state")},
                           {"state": "cancelled"},
                           request.reason or "manual cancellation"),
            scalars={"state": "cancelled"})
        return self._op_ack(request, "cancel_pending_order", ACK_CONFIRMED, ctx)

    def close_position(self, request, ctx: BrokerContext) -> BrokerResult:
        from broker_adapter import ACK_CONFIRMED, ACK_REJECTED
        cur = ctx.trade_current(request.position_ref)
        if not cur:
            return self._op_ack(request, "close_position", ACK_REJECTED,
                                ctx, reason="rejected", detail="position not found")
        ctx.close_trade(request.position_ref, ctx.now,
                        request.reason or "manual close")
        return self._op_ack(request, "close_position", ACK_CONFIRMED, ctx)

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

def load_live_gateway_diagnostic():
    """Import the live-slice MT5 gateway, returning `(gateway, reason)`.

    LIVE-5B: the reason is the point. This function used to be a bare
    `except Exception: return None`, which meant that on the VPS a
    `live.config` failure, a bad LUX_ROOT or a partially-installed SDK were all
    reported to the operator as "MetaTrader5 package unavailable on this host" —
    a statement that was simply false and gave them nothing to act on.

    `reason` is None on success and otherwise a specific, machine-readable
    string naming the failing STEP. It never contains a credential.
    """
    import os
    import sys
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if root not in sys.path:
        sys.path.insert(0, root)
    try:
        from live.config import LiveConfig
    except Exception as exc:                                # noqa: BLE001
        return None, (f"live.config could not be imported "
                      f"({type(exc).__name__}); check LUX_ROOT and the LIVE_* "
                      f"environment variables")
    try:
        from live.mt5_gateway import MT5Gateway
    except Exception as exc:                                # noqa: BLE001
        return None, (f"live.mt5_gateway could not be imported "
                      f"({type(exc).__name__})")
    try:
        config = LiveConfig()
    except Exception as exc:                                # noqa: BLE001
        return None, (f"LiveConfig could not be constructed "
                      f"({type(exc).__name__}); a LIVE_* variable is malformed")
    try:
        gateway = MT5Gateway(config)
    except Exception as exc:                                # noqa: BLE001
        return None, f"MT5Gateway could not be constructed ({type(exc).__name__})"
    if not gateway.available:
        return None, ("the MetaTrader5 package is not importable on this host "
                      "(it ships as a Windows wheel only)")
    return gateway, None


def _load_live_gateway():
    """Back-compat shape: the gateway or None. Callers wanting the REASON should
    use `load_live_gateway_diagnostic()`."""
    gateway, _reason = load_live_gateway_diagnostic()
    return gateway


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
            # LIVE-1: the ConnectionPolicy must approve BEFORE any terminal access.
            # A deny loads nothing, performs zero MT5 API calls, and every read
            # reports the machine-readable policy reason.
            import connection_policy
            decision = connection_policy.evaluate_local_broker("mt5")
            if not decision.allowed:
                import logging
                logging.getLogger("broker").warning(
                    "AUDIT mt5_gateway_denied reason=%s profile=%s",
                    decision.reason, decision.profile)
                self._policy_denied_reason = decision.reason
                self._gateway_cache = None
            else:
                self._policy_denied_reason = None
                gateway, reason = load_live_gateway_diagnostic()
                # LIVE-5B: keep the SPECIFIC reason so every read can explain
                # itself instead of blaming the package.
                self._gateway_unavailable_reason = reason
                self._gateway_cache = gateway
                if reason:
                    import logging
                    logging.getLogger("broker").warning(
                        "AUDIT mt5_gateway_unavailable reason=%s", reason)
            self._gateway_loaded = True
        return self._gateway_cache

    def _unavailable_detail(self) -> str:
        """The most specific reason this adapter cannot reach a terminal."""
        return (getattr(self, "_policy_denied_reason", None)
                or getattr(self, "_gateway_unavailable_reason", None)
                or "MetaTrader5 package unavailable on this host")

    def _state(self) -> tuple[str, str]:
        if self._gateway is None:
            return ConnectionState.DISCONNECTED, self._unavailable_detail()
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
        # LIVE-3: the MT5 adapter supports EXACTLY FOUR live writes — market-order
        # submission, position-protection modification, pending-order cancellation
        # and position close. Every OTHER mutation capability remains False:
        # pending-order placement, partial close, hedging-mode changes and all
        # other mutations are structurally unavailable (inert verbs, absent
        # capabilities).
        return BrokerCapability(
            supportsLiveWrite=True,
            supportsMarketExecution=True,
            supportsModify=True,            # LIVE-3: SL/TP protection modification
            supportsCancelOrder=True,       # LIVE-3: pending-order cancellation
            supportsClosePosition=True,     # LIVE-3: full position close
            supportsPendingOrders=False,
            supportsPartialClose=False, supportsHedging=False, supportsNetting=False,
            supportsReplay=False,
        )

    def translate_symbol(self, canonical: str) -> str:
        return self._symbols.to_broker(canonical)

    def to_canonical(self, broker_symbol: str) -> str:
        return self._symbols.to_canonical(broker_symbol)

    # ── LIVE-1 canonical read operations ─────────────────────────────────────
    # Every read is TOTAL: any missing package/terminal/login/malformed data
    # yields a canonical BrokerResult (machine code, redaction-safe detail),
    # never a traceback. MT5 SDK objects never leave this class.

    @staticmethod
    def _mask_login(login) -> str:
        s = str(login) if login is not None else ""
        return f"mt5_****{s[-4:]}" if len(s) >= 4 else "mt5_****"

    def _guarded(self, operation: str, fn):
        """Run one read against the gateway with total failure handling."""
        decision_gateway = self._gateway
        if decision_gateway is None:
            reason = getattr(self, "_policy_denied_reason", None)
            if reason:
                return BrokerResult(ok=False, code="connection_denied", detail=reason)
            return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                detail="MetaTrader5 package unavailable on this host")
        if not decision_gateway.connected:
            return BrokerResult(ok=False, code=RESULT_NOT_CONNECTED,
                                detail="MT5 terminal not connected")
        try:
            return fn(decision_gateway)
        except Exception as exc:            # malformed MT5 data / timeout / hostile object
            import logging
            logging.getLogger("broker").warning("AUDIT mt5_read_failed op=%s err=%s",
                                                operation, type(exc).__name__)
            return BrokerResult(ok=False, code="error",
                                detail=f"{operation} failed: {type(exc).__name__}")

    def account_identity(self) -> BrokerResult:
        def read(gw):
            ident = gw.account_identity()
            if ident is None:
                return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                    detail="account identity unavailable")
            fp = getattr(ident, "fingerprint", None)
            login = getattr(ident, "login", None)
            return BrokerResult(ok=True, code="ok", data={
                "accountId": self._mask_login(login), "fingerprint": fp,
                "brokerId": self.broker_id, "provenance": "live_mt5"})
        return self._guarded("account_identity", read)

    def account_snapshot(self, ctx: BrokerContext) -> BrokerResult:
        from broker_adapter import BrokerAccountInfo
        def read(gw):
            acct = gw.sdk.account_info()
            if acct is None:
                return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                    detail="account information unavailable")
            ident = gw.account_identity()
            server_time = gw.server_time_utc()
            info = BrokerAccountInfo(
                login_masked=self._mask_login(getattr(acct, "login", None)),
                fingerprint=getattr(ident, "fingerprint", None) if ident else None,
                broker_company=getattr(acct, "company", None),
                server=getattr(acct, "server", None),
                currency=getattr(acct, "currency", None),
                balance=getattr(acct, "balance", None),
                equity=getattr(acct, "equity", None),
                margin=getattr(acct, "margin", None),
                margin_free=getattr(acct, "margin_free", None),
                margin_level=getattr(acct, "margin_level", None),
                leverage=getattr(acct, "leverage", None),
                at=server_time.isoformat().replace("+00:00", "Z") if server_time else None,
                trade_mode=getattr(ident, "trade_mode", None) if ident else None,
            )
            return BrokerResult(ok=True, code="ok", data=info.as_dict())
        return self._guarded("account_snapshot", read)

    def recent_executions(self, ctx: BrokerContext) -> BrokerResult:
        from datetime import datetime, timedelta, timezone
        from broker_adapter import BrokerDeal
        def read(gw):
            history = getattr(gw.sdk, "history_deals_get", None)
            if history is None:
                # Partial capability: the SDK build offers no deal history. Explicit.
                return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                    detail="deal history not available from this terminal")
            end = datetime.now(timezone.utc)
            raw = history(end - timedelta(days=1), end)
            if raw is None:
                # `None` is how the SDK reports a FAILED read. Coercing it to an
                # empty list reported a failure as "no executions today".
                return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                    detail="deal history read failed")
            # `history_deals_get` is UNORDERED, so the previous `deals[:50]` kept
            # an ARBITRARY fifty and presented them as the recent ones. Order
            # first — most recent first — so the cap keeps what the name promises.
            deals = sorted(raw, key=lambda d: (getattr(d, "time", 0) or 0,
                                               str(getattr(d, "ticket", ""))),
                           reverse=True)
            #: A display bound, not an exhaustiveness claim. Stated in `detail`
            #: so a truncated view can never be mistaken for the whole day.
            cap = 50
            out = []
            for d in deals[:cap]:
                out.append(BrokerDeal(
                    deal_id=str(getattr(d, "ticket", "")),
                    order_ref=str(getattr(d, "order", "")) or None,
                    symbol=self.to_canonical(getattr(d, "symbol", "") or ""),
                    side="long" if getattr(d, "type", 0) == 0 else "short",
                    volume=getattr(d, "volume", None),
                    price=getattr(d, "price", None),
                    profit=getattr(d, "profit", None),
                    at=None if getattr(d, "time", None) is None else
                       datetime.fromtimestamp(d.time, tz=timezone.utc)
                       .isoformat().replace("+00:00", "Z"),
                ).as_dict())
            return BrokerResult(
                ok=True, code="ok", data=out,
                detail=(f"showing {cap} most recent of {len(deals)} deals in the "
                        "last 24h" if len(deals) > cap else ""))
        return self._guarded("recent_executions", read)

    def reconcile_snapshot(self, ctx: BrokerContext) -> BrokerResult:
        """One coherent LIVE read for the canonical reconciliation authority."""
        def read(gw):
            ident = gw.account_identity()
            server_time = gw.server_time_utc()
            return BrokerResult(ok=True, code="ok", data={
                "positions": self.positions(ctx),
                "orders": self.orders(ctx),
                "accounts": self.accounts(ctx),
                "connection": ConnectionState.CONNECTED,
                "accountIdentity": getattr(ident, "fingerprint", None) if ident else None,
                "at": server_time.isoformat().replace("+00:00", "Z") if server_time
                      else ctx.now,
                "provenance": "live_mt5",
            })
        return self._guarded("reconcile_snapshot", read)

    # ── LIVE-2: the ONE live write — market-order submission ─────────────────
    def submit_market_order(self, request: MarketOrderRequest,
                            ctx: BrokerContext) -> BrokerResult:
        """Submit exactly one market order through the gateway's proven typed
        write path (`open_position` -> `mt5_results` classification). Total:
        every failure mode answers a canonical BrokerResult; the MT5 result
        object never escapes; an exception in this path means the outcome is
        UNKNOWABLE and maps to `communication_failed` (reconcile — never guess).

        This is the ONLY write the adapter implements. Every other mutation
        (pending orders, modify, cancel, close, flatten) remains inert."""
        gw = self._gateway
        if gw is None:
            reason = getattr(self, "_policy_denied_reason", None)
            if reason:
                return BrokerResult(ok=False, code="connection_denied", detail=reason)
            return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                                detail="MetaTrader5 package unavailable on this host")
        if not gw.connected:
            return BrokerResult(ok=False, code=RESULT_NOT_CONNECTED,
                                detail="MT5 terminal not connected")
        try:
            result = gw.open_position(request.side, float(request.quantity),
                                      request.stop_loss, request.take_profit,
                                      request.intent_id)
            return self._map_submit_result(request, result, ctx)
        except Exception as exc:
            # The gateway catches order_send exceptions itself (typed EXCEPTION
            # disposition), so reaching here means the submission OUTCOME IS
            # UNKNOWABLE — communication_failed, queued for reconciliation.
            import logging
            logging.getLogger("broker").warning(
                "AUDIT mt5_submit_failed op=submit_market_order err=%s",
                type(exc).__name__)
            ack = MarketOrderAck(intent_id=request.intent_id,
                                 status=ACK_COMMUNICATION_FAILED,
                                 requested_volume=request.quantity,
                                 reason="communication_failed",
                                 detail=type(exc).__name__,
                                 provenance="live_mt5", at=ctx.now)
            return BrokerResult(ok=False, code="communication_failed",
                                detail=type(exc).__name__, data=ack.as_dict())

    def _map_submit_result(self, request: MarketOrderRequest, result,
                           ctx: BrokerContext) -> BrokerResult:
        """Map one typed MT5SubmitResult into the canonical acknowledgement.
        Plain scalars only; the SDK evidence was already snapshotted by
        `mt5_results` and no MT5 object crosses this boundary."""
        from live import mt5_results as _mr
        disp = result.disposition
        order_ticket = (str(result.broker_order_ticket)
                        if _mr.usable_ticket(result.broker_order_ticket) else None)
        deal_ticket = (str(result.broker_deal_ticket)
                       if _mr.usable_ticket(result.broker_deal_ticket) else None)
        common = dict(intent_id=request.intent_id,
                      broker_order_ticket=order_ticket,
                      broker_deal_ticket=deal_ticket,
                      requested_volume=result.requested_volume,
                      filled_volume=result.filled_volume,
                      price=result.price,
                      provenance="live_mt5", at=ctx.now)
        if disp == _mr.MT5SubmitDisposition.FILLED:
            ack = MarketOrderAck(status=ACK_FILLED, **common)
            return BrokerResult(ok=True, code="ok", detail="broker filled",
                                data=ack.as_dict(), broker_ref=order_ticket or deal_ticket)
        if disp == _mr.MT5SubmitDisposition.PARTIALLY_FILLED:
            ack = MarketOrderAck(status=ACK_PARTIAL, **common)
            return BrokerResult(ok=True, code="ok", detail="broker partially filled",
                                data=ack.as_dict(), broker_ref=order_ticket or deal_ticket)
        if disp == _mr.MT5SubmitDisposition.REJECTED:
            ack = MarketOrderAck(status=ACK_REJECTED, reason="broker_rejected",
                                 detail=result.diagnostic, **common)
            return BrokerResult(ok=False, code="rejected", detail=result.diagnostic,
                                data=ack.as_dict())
        if disp == _mr.MT5SubmitDisposition.NOT_SUBMITTED:
            ack = MarketOrderAck(status=ACK_NOT_SUBMITTED, reason="submission_failed",
                                 detail=result.diagnostic, **common)
            return BrokerResult(ok=False, code="not_submitted", detail=result.diagnostic,
                                data=ack.as_dict())
        if disp == _mr.MT5SubmitDisposition.EXCEPTION:
            # The gateway's diagnostic preserves the exception MESSAGE as broker
            # evidence; the canonical boundary carries only the exception TYPE
            # (no message leakage past the adapter).
            exc_type = (result.diagnostic or "").split(":", 1)[0] or "Exception"
            ack = MarketOrderAck(status=ACK_COMMUNICATION_FAILED,
                                 reason="communication_failed",
                                 detail=exc_type, **common)
            return BrokerResult(ok=False, code="communication_failed",
                                detail=exc_type, data=ack.as_dict())
        # AMBIGUOUS (timeout / connection / placed / unknown retcode): the order
        # MAY exist at the broker — never guess; reconcile.
        ack = MarketOrderAck(status=ACK_TIMEOUT, reason="timeout",
                             detail=result.diagnostic, **common)
        return BrokerResult(ok=False, code="timeout", detail=result.diagnostic,
                            data=ack.as_dict(),
                            broker_ref=order_ticket or deal_ticket)

    # ── LIVE-3: the three manual-management writes (typed gateway; canonical acks) ─

    def _guarded_action(self, operation: str, request, entity_ref: str, fn):
        """Run one manual-management write with total failure handling. Every
        failure mode answers a canonical BrokerResult carrying an OperationAck;
        an exception in this path means the outcome is UNKNOWABLE
        (communication_failed, type name only)."""
        from broker_adapter import (ACK_COMMUNICATION_FAILED, ACK_NOT_SUBMITTED,
                                    OperationAck)
        gw = self._gateway
        if gw is None:
            reason = getattr(self, "_policy_denied_reason", None)
            code = "connection_denied" if reason else RESULT_UNAVAILABLE
            detail = reason or "MetaTrader5 package unavailable on this host"
            ack = OperationAck(intent_id=request.intent_id, operation=operation,
                               status=ACK_NOT_SUBMITTED, entity_ref=entity_ref,
                               reason="submission_failed", detail=detail,
                               provenance="live_mt5")
            return BrokerResult(ok=False, code=code, detail=detail, data=ack.as_dict())
        if not gw.connected:
            ack = OperationAck(intent_id=request.intent_id, operation=operation,
                               status=ACK_NOT_SUBMITTED, entity_ref=entity_ref,
                               reason="submission_failed",
                               detail="MT5 terminal not connected",
                               provenance="live_mt5")
            return BrokerResult(ok=False, code=RESULT_NOT_CONNECTED,
                                detail="MT5 terminal not connected", data=ack.as_dict())
        try:
            return fn(gw)
        except Exception as exc:
            import logging
            logging.getLogger("broker").warning(
                "AUDIT mt5_operation_failed op=%s err=%s", operation,
                type(exc).__name__)
            ack = OperationAck(intent_id=request.intent_id, operation=operation,
                               status=ACK_COMMUNICATION_FAILED, entity_ref=entity_ref,
                               reason="communication_failed",
                               detail=type(exc).__name__, provenance="live_mt5")
            return BrokerResult(ok=False, code="communication_failed",
                                detail=type(exc).__name__, data=ack.as_dict())

    def _map_action_result(self, operation: str, request, entity_ref: str,
                           result, ctx: BrokerContext) -> BrokerResult:
        """Map one typed MT5ActionResult into the canonical OperationAck."""
        from broker_adapter import (ACK_COMMUNICATION_FAILED, ACK_CONFIRMED,
                                    ACK_NOT_SUBMITTED, ACK_REJECTED, ACK_TIMEOUT,
                                    OperationAck)
        from live import mt5_results as _mr
        order_ticket = (str(result.broker_order_ticket)
                        if _mr.usable_ticket(result.broker_order_ticket) else None)
        deal_ticket = (str(result.broker_deal_ticket)
                       if _mr.usable_ticket(result.broker_deal_ticket) else None)
        common = dict(intent_id=request.intent_id, operation=operation,
                      entity_ref=entity_ref, broker_order_ticket=order_ticket,
                      broker_deal_ticket=deal_ticket, provenance="live_mt5",
                      at=ctx.now)
        disp = result.disposition
        if disp == _mr.MT5ActionDisposition.DONE:
            ack = OperationAck(status=ACK_CONFIRMED, **common)
            return BrokerResult(ok=True, code="ok", detail="broker acknowledged",
                                data=ack.as_dict(),
                                broker_ref=order_ticket or deal_ticket or entity_ref)
        if disp == _mr.MT5ActionDisposition.REJECTED:
            ack = OperationAck(status=ACK_REJECTED, reason="broker_rejected",
                               detail=result.diagnostic, **common)
            return BrokerResult(ok=False, code="rejected", detail=result.diagnostic,
                                data=ack.as_dict())
        if disp == _mr.MT5ActionDisposition.NOT_SUBMITTED:
            ack = OperationAck(status=ACK_NOT_SUBMITTED, reason="submission_failed",
                               detail=result.diagnostic, **common)
            return BrokerResult(ok=False, code="not_submitted", detail=result.diagnostic,
                                data=ack.as_dict())
        if disp == _mr.MT5ActionDisposition.EXCEPTION:
            exc_type = (result.diagnostic or "").split(":", 1)[0] or "Exception"
            ack = OperationAck(status=ACK_COMMUNICATION_FAILED,
                               reason="communication_failed", detail=exc_type, **common)
            return BrokerResult(ok=False, code="communication_failed", detail=exc_type,
                                data=ack.as_dict())
        ack = OperationAck(status=ACK_TIMEOUT, reason="timeout",
                           detail=result.diagnostic, **common)
        return BrokerResult(ok=False, code="timeout", detail=result.diagnostic,
                            data=ack.as_dict(),
                            broker_ref=order_ticket or deal_ticket)

    @staticmethod
    def _numeric_ticket(ref: str):
        try:
            return int(str(ref).strip())
        except (TypeError, ValueError):
            return None

    def modify_position_protection(self, request, ctx: BrokerContext) -> BrokerResult:
        op = "modify_position_protection"
        def write(gw):
            ticket = self._numeric_ticket(request.position_ref)
            if ticket is None:
                from live import mt5_results as _mr
                return self._map_action_result(
                    op, request, request.position_ref,
                    _mr.action_not_submitted("non-numeric position reference"), ctx)
            result = gw.modify_position_protection(ticket, request.stop_loss,
                                                   request.take_profit)
            return self._map_action_result(op, request, request.position_ref, result, ctx)
        return self._guarded_action(op, request, request.position_ref, write)

    def cancel_pending_order(self, request, ctx: BrokerContext) -> BrokerResult:
        op = "cancel_pending_order"
        def write(gw):
            ticket = self._numeric_ticket(request.order_ref)
            if ticket is None:
                from live import mt5_results as _mr
                return self._map_action_result(
                    op, request, request.order_ref,
                    _mr.action_not_submitted("non-numeric order reference"), ctx)
            result = gw.cancel_pending_order(ticket)
            return self._map_action_result(op, request, request.order_ref, result, ctx)
        return self._guarded_action(op, request, request.order_ref, write)

    def close_position(self, request, ctx: BrokerContext) -> BrokerResult:
        op = "close_position"
        def write(gw):
            ticket = self._numeric_ticket(request.position_ref)
            if ticket is None:
                from live import mt5_results as _mr
                return self._map_action_result(
                    op, request, request.position_ref,
                    _mr.action_not_submitted("non-numeric position reference"), ctx)
            result = gw.close_position_full(ticket)
            return self._map_action_result(op, request, request.position_ref, result, ctx)
        return self._guarded_action(op, request, request.position_ref, write)

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
# LIVE-1: `_ACTIVE` is the DEFAULT constant (mock). The runtime resolves the
# selected adapter through `active_kind()` (which reads the explicit selection
# variable), so `get_broker()` follows an operator's adapter choice.
_ACTIVE = _adapter_boundary.ACTIVE_KIND  # "mock" — default when nothing is selected


def get_broker(kind: str | None = None) -> Broker:
    """Back-compat entry point. Delegates to the single centralized factory, using
    the SELECTED adapter kind (mock unless explicitly changed)."""
    return _adapter_boundary.get_adapter(kind or _adapter_boundary.active_kind())


def active_kind() -> str:
    return _adapter_boundary.active_kind()


def capability_dict(cap: BrokerCapability) -> dict:
    return asdict(cap)
