"""ARCH-2 — the canonical broker adapter boundary.

This module is the ONE authoritative broker adapter contract:

  * one adapter interface (`BrokerAdapter`, the ABC every adapter implements)
  * one canonical capability model (`BrokerCapability`)
  * one canonical connection-state model (`ConnectionState`)
  * one canonical result envelope (`BrokerResult`)
  * one canonical error model (`BrokerError`)
  * one centralized, LAZY adapter factory (`get_adapter`) — the only place an
    adapter may be constructed

It contains NO adapter implementation, NO MT5-specific concept, NO socket, and no
import of any gateway. Concrete adapters live in `broker.py` (MockBroker, the inert
MT5Adapter) and are imported lazily inside the factory, so importing this module —
or any module that depends on it — constructs no broker and touches no network.

DESIGN RULES
  * The runtime and orchestrator talk ONLY to this contract. No broker-specific
    result shape exists outside an adapter; anything an adapter returns crosses the
    boundary as the canonical models or a `BrokerResult` envelope.
  * Adapter selection is centralized and fail-closed: an unknown or unavailable
    adapter kind raises `UnknownAdapterError`; nothing falls back to a guessed
    adapter.
  * Construction is lazy and cached: no adapter exists until `get_adapter()` is
    first called for its kind, so importing the backend performs no broker work
    (previously `broker._REGISTRY` constructed MockBroker AND MT5Adapter — which
    probes for a live gateway — at import time; ARCH-2 removes that).
  * Lifecycle operations a given adapter does not implement return an EXPLICIT
    inert `BrokerResult` (`ok=False, code="unavailable"`) — never a silent pass and
    never an exception a caller might mistake for connectivity.

FUTURE ADAPTERS (cTrader, JForex, a real MT5 write path) implement this same
interface; the factory gains a kind; nothing else in the system changes. That is
the whole point of the boundary.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Canonical connection-state model — the single owner of these constants.
# (`execution.py` and `broker_sync.py` previously compared against bare string
# literals; they now import these.)
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
    """The one canonical capability model. `command_registry` names which command
    requires which capability field; adapters declare what they support.

    LIVE-1: `supportsLiveWrite` is the REAL-broker write capability. It is False
    for EVERY adapter (the mock's "writes" are fixture simulations against the
    runtime overlay, never a broker; the MT5 adapter is structurally read-only).
    Execution against a live broker therefore remains impossible by capability."""
    supportsLiveWrite: bool = False
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
    """The one canonical error model. `code` is a stable machine string."""
    code: str
    message: str
    recoverable: bool = True


# Sentinels for unsupported operations (the inert MT5 skeleton returns these).
UNSUPPORTED = BrokerError(code="unsupported", message="Operation not supported by this broker", recoverable=False)
NOT_IMPLEMENTED = BrokerError(code="not_implemented", message="Adapter is a skeleton — no implementation", recoverable=False)


# ---------------------------------------------------------------------------
# Canonical result envelope — how every lifecycle operation answers.
# ---------------------------------------------------------------------------

#: Stable result codes (machine-readable; never carry a value or a secret).
RESULT_OK = "ok"
RESULT_UNAVAILABLE = "unavailable"          # adapter does not implement / is inert
RESULT_NOT_CONNECTED = "not_connected"
RESULT_REJECTED = "rejected"
RESULT_ERROR = "error"


@dataclass(frozen=True)
class BrokerResult:
    """The one canonical envelope for a broker lifecycle operation.

    `ok` — did the operation succeed; `code` — stable machine code; `detail` — a
    redaction-safe human hint; `data` — the canonical payload (models/plain data,
    never a broker-native structure); `broker_ref` — the broker's own reference for
    the affected entity, when one exists.
    """
    ok: bool
    code: str
    detail: str = ""
    data: Any = None
    broker_ref: str | None = None


def inert_result(operation: str) -> BrokerResult:
    """The explicit answer of an adapter that does not implement an operation.
    Fail-closed and unmistakable: `ok=False`, code `unavailable`."""
    return BrokerResult(ok=False, code=RESULT_UNAVAILABLE,
                        detail=f"adapter does not implement {operation}; operation is inert")


# ---------------------------------------------------------------------------
# Runtime → adapter injection (unchanged shape; the runtime supplies overlay
# primitives so adapters never import server internals).
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
# The canonical adapter interface.
# ---------------------------------------------------------------------------

class BrokerAdapter(ABC):
    """The single execution boundary every broker adapter implements.

    The abstract core is the operation set the existing `Broker` interface already
    proved (connection, health, capabilities, accounts/positions/orders inspection,
    command submission, cancel/modify, flatten, sync). The additional lifecycle
    operations below have EXPLICIT inert defaults: an adapter that has not
    implemented them answers `unavailable` rather than pretending.
    """

    kind: str = "broker"
    broker_id: str = "brk_unknown"

    # -- connection / identity / capabilities --------------------------------
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

    def account_identity(self) -> BrokerResult:
        """The broker-side account identity (id / fingerprint), value-free.
        Inert by default; adapters override when they can actually report one."""
        return inert_result("account_identity")

    # -- inspection ----------------------------------------------------------
    @abstractmethod
    def accounts(self, ctx: BrokerContext) -> list: ...

    @abstractmethod
    def positions(self, ctx: BrokerContext) -> list: ...

    @abstractmethod
    def orders(self, ctx: BrokerContext) -> list: ...

    def recent_executions(self, ctx: BrokerContext) -> BrokerResult:
        """Recent fills/executions. Inert by default (no adapter reports these yet)."""
        return inert_result("recent_executions")

    def account_snapshot(self, ctx: BrokerContext) -> BrokerResult:
        """A point-in-time account snapshot. Inert by default."""
        return inert_result("account_snapshot")

    # -- command execution (fixture control-plane path) ----------------------
    @abstractmethod
    def submit_command(self, name: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]:
        """Execute a trade/order command. Returns (before, after) for the audit event."""

    @abstractmethod
    def cancel_order(self, order_id: str, ctx: BrokerContext) -> tuple[dict | None, dict | None]: ...

    @abstractmethod
    def modify_order(self, order_id: str, changes: dict, ctx: BrokerContext) -> tuple[dict | None, dict | None]: ...

    @abstractmethod
    def flatten(self, deployment_id: str, ctx: BrokerContext) -> list[str]: ...

    # -- future canonical order lifecycle (inert until a live slice) ---------
    def submit_order(self, intent: Any, ctx: BrokerContext) -> BrokerResult:
        """Submit a canonical OrderIntent. Inert by default — NO adapter implements
        live submission in ARCH-2, including the mock (the fixture world executes
        through `submit_command`)."""
        return inert_result("submit_order")

    def close_position(self, position_id: str, ctx: BrokerContext) -> BrokerResult:
        """Close a position by id. Inert by default."""
        return inert_result("close_position")

    # -- reconciliation ------------------------------------------------------
    @abstractmethod
    def sync(self, ctx: BrokerContext) -> dict: ...

    def reconcile_snapshot(self, ctx: BrokerContext) -> BrokerResult:
        """One coherent snapshot for the canonical reconciliation authority:
        positions, orders, accounts, connection state, and the adapter's own
        timestamp. Inert by default; adapters that can report state override."""
        return inert_result("reconcile_snapshot")

    # -- symbols -------------------------------------------------------------
    def translate_symbol(self, canonical: str) -> str:
        return canonical

    def to_canonical(self, broker_symbol: str) -> str:
        return broker_symbol


# ---------------------------------------------------------------------------
# Centralized, lazy, fail-closed adapter selection — the ONLY constructor path.
# ---------------------------------------------------------------------------

class UnknownAdapterError(LookupError):
    """Selection failed closed: the requested adapter kind does not exist."""


class AdapterDeniedError(PermissionError):
    """The ConnectionPolicy refused adapter construction. `reason` is the
    policy's machine-readable code; nothing was initialized."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


#: The DEFAULT adapter kind. The Control Tower runs against the mock adapter
#: unless an operator explicitly selects another known kind (LIVE-1).
ACTIVE_KIND = "mock"

#: LIVE-1: explicit adapter selection. Allowed values: mock | mt5. Anything else
#: DENIES (get_adapter raises; nothing constructs, nothing falls back silently).
VAR_ADAPTER = "CONTROL_TOWER_BROKER_ADAPTER"

_CACHE: dict[str, BrokerAdapter] = {}


def known_kinds() -> tuple[str, ...]:
    return ("mock", "mt5")


def get_adapter(kind: str | None = None) -> BrokerAdapter:
    """Return the adapter for `kind` (default: the active kind), constructing it
    LAZILY on first request and caching it. Unknown kinds fail closed with
    `UnknownAdapterError` — there is no fallback adapter.

    This is the single construction path. `broker.get_broker()` delegates here;
    nothing else may instantiate an adapter class.
    """
    resolved = kind or active_kind()
    if resolved in _CACHE:
        return _CACHE[resolved]
    if resolved not in known_kinds():
        raise UnknownAdapterError(f"unknown broker adapter kind: {resolved!r}")
    # LIVE-1: the ConnectionPolicy must approve BEFORE a broker-touching adapter
    # is constructed. The mock is an in-process fixture (no external system to
    # police); MT5 touches a terminal, so a policy deny constructs NOTHING and
    # performs zero MT5 API calls.
    if resolved == "mt5":
        import connection_policy
        decision = connection_policy.evaluate_local_broker("mt5")
        if not decision.allowed:
            import logging
            logging.getLogger("broker_adapter").warning(
                "AUDIT adapter_denied kind=mt5 reason=%s profile=%s",
                decision.reason, decision.profile)
            raise AdapterDeniedError(decision.reason)
    # Lazy import: the adapters module is only loaded when an adapter is actually
    # requested, and each adapter is only constructed when ITS kind is requested.
    import broker as _adapters
    if resolved == "mock":
        _CACHE[resolved] = _adapters.MockBroker()
    else:
        _CACHE[resolved] = _adapters.MT5Adapter()
    return _CACHE[resolved]


def active_kind() -> str:
    """LIVE-1: the selected adapter kind. Unset/blank -> the mock default. A value
    outside `known_kinds()` is returned VERBATIM so every construction attempt
    fails closed in `get_adapter` (unknown values deny; nothing falls back)."""
    import os
    raw = (os.environ.get(VAR_ADAPTER) or "").strip().lower()
    return raw if raw else ACTIVE_KIND


# ── LIVE-1 canonical read models (immutable; MT5 types never escape the adapter) ─

@dataclass(frozen=True)
class BrokerAccountInfo:
    """Canonical account snapshot. `login_masked` shows only the last 4 digits."""
    login_masked: str
    fingerprint: str | None
    broker_company: str | None
    server: str | None
    currency: str | None
    balance: float | None
    equity: float | None
    margin: float | None
    margin_free: float | None
    margin_level: float | None
    leverage: int | None
    at: str | None = None

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return dict(sorted(asdict(self).items()))     # deterministic serialization


@dataclass(frozen=True)
class BrokerDeal:
    """Canonical executed deal (history read)."""
    deal_id: str
    order_ref: str | None
    symbol: str | None
    side: str | None
    volume: float | None
    price: float | None
    profit: float | None
    at: str | None

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return dict(sorted(asdict(self).items()))


@dataclass(frozen=True)
class SymbolSpec:
    """Canonical symbol specification."""
    canonical: str
    broker_symbol: str
    digits: int | None = None
    point: float | None = None
    trade_allowed: bool | None = None

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return dict(sorted(asdict(self).items()))


@dataclass(frozen=True)
class TerminalInfo:
    """Canonical terminal state (value-free: no paths, no build details)."""
    connected: bool
    trade_allowed: bool | None = None
    company: str | None = None

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return dict(sorted(asdict(self).items()))
