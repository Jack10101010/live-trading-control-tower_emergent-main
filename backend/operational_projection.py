"""LIVE-4A — the canonical OPERATIONAL PROJECTION. One owner, read-only.

Before LIVE-4A the Control Tower answered a dozen independent endpoints and each
UI panel re-derived its own version of "what is happening". This module is the
single place where broker truth, node truth, execution truth, reconciliation
truth and governance truth are combined into immutable operational read models.

WHAT THIS MODULE IS
  * a DERIVATION. It consumes already-authoritative sources and produces frozen
    view objects. It is not an authority over anything.

WHAT THIS MODULE MUST NEVER DO
  * write to a broker (it holds no adapter and issues no command)
  * make an execution decision (no safety evaluation, no authorization grant)
  * own persistence (it never writes to the execution store)
  * cache mutable state (every build is computed from the sources it is given)
  * invent a value (absence is reported as absence — see AVAILABILITY_*)

DETERMINISM AND PURITY
  `build_*` functions take an explicit `ProjectionSources` bundle of read-only
  callables plus an injected `now`. They read no clock, no environment and no
  global, so the same sources at the same `now` always produce byte-identical
  output. That is what makes the projection testable and restart-safe: it holds
  nothing across calls.

FRESHNESS AND PROVENANCE
  Every model carries `provenance` (where the fact came from) and every model
  whose source is time-sensitive carries a `Freshness` (projection timestamp,
  source timestamp, age, stale). UNAVAILABLE (the source could not be read) and
  STALE (the source was read but is too old) are DISTINCT states and are never
  collapsed into a zero or a healthy-looking default.
"""

from __future__ import annotations

import math

import activation_policy
import broker_provenance as brokerprov
import node_provenance as nodeprov
import trade_ledger_domain as _tld

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = "ct.operational-projection.v1"

# ── provenance vocabulary (where a fact came from) ────────────────────────────
# M-MT5-READ-1: the BROKER-ORIGIN values are re-exported from the policy seam
# rather than restated, so there is exactly one definition of each string. The
# names stay identical, so every existing import keeps working.
PROV_LIVE_MT5 = brokerprov.PROV_LIVE_MT5
PROV_NODE_MT5 = brokerprov.PROV_NODE_MT5
PROV_MOCK_FIXTURE = brokerprov.PROV_MOCK_FIXTURE
PROV_ABSENT = brokerprov.PROV_ABSENT
#: The node telemetry ENVELOPE itself (a node view: heartbeat, cycle, health).
#: Distinct from `node_mt5`, which names BROKER truth the node observed. A node
#: can be reporting perfectly while having sampled no account at all, and the
#: two facts must not share one label.
PROV_NODE_TELEMETRY = "node-telemetry"
PROV_DURABLE_STORE = "durable-store"

# ── availability vocabulary (distinct, never collapsed) ───────────────────────
AVAILABILITY_OK = "ok"
AVAILABILITY_STALE = "stale"                 # read succeeded, too old to trust
AVAILABILITY_UNAVAILABLE = "unavailable"     # read failed / source absent

#: Default staleness threshold, aligned with the reconciliation authority.
DEFAULT_STALE_AFTER_S = 120.0


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _sorted(obj: Any) -> Any:
    """Deterministic serialization: dicts get sorted keys, recursively."""
    if isinstance(obj, dict):
        return {k: _sorted(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_sorted(v) for v in obj]
    return obj


@dataclass(frozen=True)
class Freshness:
    """How old the underlying fact is, and whether that makes it untrustworthy.

    `available=False` means the source could NOT be read (unavailable) — which is
    deliberately different from a source that was read but is `stale`."""
    projection_at: str
    source_at: str | None = None
    age_seconds: float | None = None
    stale: bool = True                       # absent evidence is stale, not fresh
    available: bool = False
    stale_after_seconds: float = DEFAULT_STALE_AFTER_S
    detail: str | None = None

    @property
    def status(self) -> str:
        if not self.available:
            return AVAILABILITY_UNAVAILABLE
        return AVAILABILITY_STALE if self.stale else AVAILABILITY_OK

    def as_dict(self) -> dict:
        return _sorted({
            "projectionAt": self.projection_at,
            "sourceAt": self.source_at,
            "ageSeconds": self.age_seconds,
            "stale": self.stale,
            "available": self.available,
            "staleAfterSeconds": self.stale_after_seconds,
            "status": self.status,
            "detail": self.detail,
        })


def freshness(*, now: str, source_at: Any, available: bool = True,
              stale_after_s: float = DEFAULT_STALE_AFTER_S,
              detail: str | None = None) -> Freshness:
    """Derive freshness. An unreadable source is UNAVAILABLE; a readable source
    with a missing/unparseable timestamp is STALE (never fresh)."""
    ref = _parse(now)
    src = _parse(source_at)
    if not available:
        return Freshness(projection_at=str(now), source_at=None, age_seconds=None,
                         stale=True, available=False,
                         stale_after_seconds=stale_after_s, detail=detail)
    if src is None or ref is None:
        return Freshness(projection_at=str(now), source_at=(str(source_at) if source_at else None),
                         age_seconds=None, stale=True, available=True,
                         stale_after_seconds=stale_after_s,
                         detail=detail or "source timestamp missing or unparseable")
    age = round(abs((ref - src).total_seconds()), 3)
    return Freshness(projection_at=str(now), source_at=_iso(src), age_seconds=age,
                     stale=age > stale_after_s, available=True,
                     stale_after_seconds=stale_after_s, detail=detail)


# ─────────────────────────────────────────────────────────────────────────────
# Canonical immutable read models
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NodeOperationalView:
    """One execution node, as IT reported itself.

    M-NODE-READ-1 — WHOSE FACTS THESE ARE.

    Seven of the nine original fields were the Control Tower describing ITSELF
    under the node's name: `adapter` was the Mac's `broker_layer.active_kind()`,
    `connection_state` the Mac adapter's connection, `reconciliation_state` the
    Mac execution store's posture, `execution_mode` the Mac's governed mode,
    `authorization_summary` the Mac's grant, `active_scenario_count` the Mac's
    scenario store, and `broker` the Mac broker snapshot's provenance. All of it
    was stamped `node-telemetry` and displayed on a card titled with the VPS
    node's instance id.

    Restoring these cards without fixing that would have put the Mac's own state
    on screen as the node's — a fabrication with a real node's name on it, which
    is worse than the invisibility it replaced.

    The Mac-sourced fields are kept in the shape (consumers read them) but are
    now `None` on a node view, and the node's OWN facts are carried in the
    additive fields below. Every one of those traces to a field the node
    published in `ct.node-telemetry.v1`.
    """
    node_id: str
    instance_id: str | None = None
    deployment: str | None = None
    adapter: str | None = None
    broker: str | None = None
    account_fingerprint_masked: str | None = None
    connection_state: str | None = None
    heartbeat_age_seconds: float | None = None
    health: str | None = None
    execution_mode: str | None = None
    authorization_summary: dict | None = None
    reconciliation_state: str | None = None
    open_position_count: int | None = None
    open_order_count: int | None = None
    active_scenario_count: int | None = None
    last_activity: str | None = None
    telemetry_age_seconds: float | None = None
    warnings: tuple = field(default_factory=tuple)

    # ── M-NODE-READ-1: node-published operational truth (all additive) ────────
    #: `absent` | `current` | `stale` | `degraded`. See `node_provenance`.
    lifecycle_state: str = nodeprov.NODE_ABSENT
    #: The node's own reasons for reporting itself degraded. Empty is not
    #: "healthy" — it means the node reported no failure condition.
    degraded_reasons: tuple = field(default_factory=tuple)
    #: `engine.deployment_profile` — what the node believes it is running as.
    deployment_profile: str | None = None
    strategy_family: str | None = None
    engine_version: str | None = None
    engine_version_expected: str | None = None
    config_fingerprint: str | None = None
    input_revision: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    data_seam: str | None = None
    #: `cycle.*` — where the node is in its own loop.
    cycle_status: str | None = None
    cycle_sequence: int | None = None
    last_boundary: str | None = None
    last_bar_time: str | None = None
    cycle_note: str | None = None
    #: `runtime.*` — the node's own operating mode and safety switches. These
    #: describe the NODE, and are never read as this tower's execution posture.
    node_mode: str | None = None
    submission_disabled: bool | None = None
    kill_switch_active: bool | None = None
    #: What the node said about its own MT5 terminal, only when it said
    #: something: `observed` | `unreachable` | None. Never derived from silence.
    mt5_observation: str | None = None
    #: M-TEL-1 freshness decomposition, passed through verbatim. The frontend
    #: must render these and never recompute a verdict from timestamps.
    published_at: str | None = None
    received_at: str | None = None
    liveness_age_seconds: float | None = None
    liveness_stale: bool | None = None
    data_stale: bool | None = None
    freshness_basis: str | None = None
    stale_after_seconds: float | None = None
    #: True when this instance predates the versioned contract and its snapshot
    #: was adapted. Genuine data, with a stated and visible limitation.
    legacy_source: bool = False

    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "nodeId": self.node_id,
            "instanceId": self.instance_id,
            "deployment": self.deployment,
            "adapter": self.adapter,
            "broker": self.broker,
            "accountFingerprintMasked": self.account_fingerprint_masked,
            "connectionState": self.connection_state,
            "heartbeatAgeSeconds": self.heartbeat_age_seconds,
            "health": self.health,
            "executionMode": self.execution_mode,
            "authorizationSummary": self.authorization_summary,
            "reconciliationState": self.reconciliation_state,
            "openPositionCount": self.open_position_count,
            "openOrderCount": self.open_order_count,
            "activeScenarioCount": self.active_scenario_count,
            "lastActivity": self.last_activity,
            "telemetryAgeSeconds": self.telemetry_age_seconds,
            "warnings": list(self.warnings),
            "lifecycleState": self.lifecycle_state,
            "degradedReasons": list(self.degraded_reasons),
            "deploymentProfile": self.deployment_profile,
            "strategyFamily": self.strategy_family,
            "engineVersion": self.engine_version,
            "engineVersionExpected": self.engine_version_expected,
            "configFingerprint": self.config_fingerprint,
            "inputRevision": self.input_revision,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "dataSeam": self.data_seam,
            "cycleStatus": self.cycle_status,
            "cycleSequence": self.cycle_sequence,
            "lastBoundary": self.last_boundary,
            "lastBarTime": self.last_bar_time,
            "cycleNote": self.cycle_note,
            "nodeMode": self.node_mode,
            "submissionDisabled": self.submission_disabled,
            "killSwitchActive": self.kill_switch_active,
            "mt5Observation": self.mt5_observation,
            "publishedAt": self.published_at,
            "receivedAt": self.received_at,
            "livenessAgeSeconds": self.liveness_age_seconds,
            "livenessStale": self.liveness_stale,
            "dataStale": self.data_stale,
            "freshnessBasis": self.freshness_basis,
            "staleAfterSeconds": self.stale_after_seconds,
            "legacySource": self.legacy_source,
            "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class AccountOperationalView:
    account_fingerprint: str | None = None
    broker: str | None = None
    server: str | None = None
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    margin_level: float | None = None
    leverage: int | None = None
    currency: str | None = None
    unrealized_pnl: float | None = None
    realized_pnl_today: float | None = None      # None = not derivable from evidence
    open_risk: float | None = None               # None = not derivable from evidence
    connection_state: str | None = None
    # ── M-MT5-READ-1, all additive and all defaulting to "not reported" ────────
    #: Free margin. The node publishes `free_margin` and NOT `margin`; the two
    #: are different quantities and neither is derivable from the other without
    #: equity arithmetic this tower is not entitled to perform.
    free_margin: float | None = None
    #: The terminal's own permission flags, as the node observed them. Tri-state:
    #: None means "not reported", which is not the same as False ("forbidden").
    trade_allowed: bool | None = None
    trade_expert: bool | None = None
    #: Which execution node relayed this account. None for a locally-read one.
    #: Part of the record's IDENTITY: two nodes may legitimately report two
    #: different accounts, and neither may overwrite the other.
    node_id: str | None = None
    #: When the OBSERVER sampled the account, as the observer stated it. Kept
    #: separate from `freshness`, which is the tower's own arrival clock.
    observed_at: str | None = None
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None
    # ── M-ACTIVATE-READINESS-1 ────────────────────────────────────────────────
    #: Whether this observation may be presented as operational truth.
    #:
    #: SEPARATE FROM PROVENANCE, deliberately. Provenance answers "who observed
    #: this?" and is earned by evidence; admission answers "is this the account
    #: this deployment is supposed to be looking at?" and is a question about
    #: configuration. Collapsing them would mean an identity mismatch either
    #: rewrote the record's origin (a lie about where it came from) or passed
    #: unremarked (a lie about what it is).
    #:
    #: A rejected record is still RETURNED, carrying its reasons. Dropping it
    #: would show "unavailable" while a node is loudly reporting real money from
    #: the wrong account — the operator needs to see that, precisely.
    admitted: bool = True
    #: Named refusal codes from `activation_policy`, never a generic failure.
    admission_reasons: tuple = ()

    def as_dict(self) -> dict:
        return _sorted({
            "accountFingerprint": self.account_fingerprint,
            "broker": self.broker,
            "server": self.server,
            "balance": self.balance,
            "equity": self.equity,
            "margin": self.margin,
            "marginLevel": self.margin_level,
            "leverage": self.leverage,
            "currency": self.currency,
            "unrealizedPnL": self.unrealized_pnl,
            "realizedPnLToday": self.realized_pnl_today,
            "openRisk": self.open_risk,
            "freeMargin": self.free_margin,
            "tradeAllowed": self.trade_allowed,
            "tradeExpert": self.trade_expert,
            "connectionState": self.connection_state,
            "nodeId": self.node_id,
            "observedAt": self.observed_at,
            "provenance": self.provenance,
            "admitted": self.admitted,
            "admissionReasons": list(self.admission_reasons),
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class OrderOperationalView:
    """An ORDER: a tower-originated execution intent and/or a broker working
    order. Deliberately a separate concept from a POSITION — the two are never
    merged, even when one produced the other."""
    intent_id: str | None = None
    broker_order_reference: str | None = None
    broker_ticket: str | None = None
    instrument: str | None = None
    side: str | None = None
    order_type: str | None = None
    quantity: float | None = None
    requested_price: float | None = None
    current_state: str | None = None
    lifecycle: tuple = field(default_factory=tuple)
    broker_status: str | None = None
    scenario_id: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    reconciliation: dict | None = None
    node_id: str | None = None
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "intentId": self.intent_id,
            "brokerOrderReference": self.broker_order_reference,
            "brokerTicket": self.broker_ticket,
            "instrument": self.instrument,
            "side": self.side,
            "orderType": self.order_type,
            "quantity": self.quantity,
            "requestedPrice": self.requested_price,
            "currentState": self.current_state,
            "lifecycle": [dict(sorted(t.items())) for t in self.lifecycle],
            "brokerStatus": self.broker_status,
            "scenarioId": self.scenario_id,
            "timestamps": {"createdAt": self.created_at, "updatedAt": self.updated_at},
            "reconciliation": self.reconciliation,
            "nodeId": self.node_id,
            "provenance": self.provenance,
        })


@dataclass(frozen=True)
class PositionOperationalView:
    broker_position_reference: str | None = None
    instrument: str | None = None
    side: str | None = None
    quantity: float | None = None
    entry_price: float | None = None
    current_price: float | None = None
    unrealized_pnl: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    age_seconds: float | None = None
    lifecycle: tuple = field(default_factory=tuple)
    protection_state: str | None = None
    reconciliation: dict | None = None
    scenario_id: str | None = None
    node_id: str | None = None
    account_fingerprint: str | None = None
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "brokerPositionReference": self.broker_position_reference,
            "instrument": self.instrument,
            "side": self.side,
            "quantity": self.quantity,
            "entryPrice": self.entry_price,
            "currentPrice": self.current_price,
            "unrealizedPnL": self.unrealized_pnl,
            "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit,
            "ageSeconds": self.age_seconds,
            "lifecycle": [dict(sorted(t.items())) for t in self.lifecycle],
            "protectionState": self.protection_state,
            "reconciliation": self.reconciliation,
            "scenarioId": self.scenario_id,
            "nodeId": self.node_id,
            "accountFingerprint": self.account_fingerprint,
            "provenance": self.provenance,
        })


@dataclass(frozen=True)
class ScenarioOperationalView:
    """LIVE-4B — the projected view of a canonical Scenario.

    Replaces the LIVE-4A placeholder, which could only echo a `scenarioKey`
    string found in a command payload. This view derives from the SCENARIO
    STORE: the scenario is a real entity with a lifecycle, an append-only
    history and explicit links."""
    scenario_id: str
    instrument: str | None = None
    direction: str | None = None
    session: str | None = None
    structure: str | None = None
    entry_model: str | None = None
    timeframe: str | None = None
    status: str | None = None
    outcome: str | None = None
    node_id: str | None = None
    account_fingerprint_masked: str | None = None
    linked_recommendation: str | None = None
    linked_intent_count: int = 0
    linked_order_count: int = 0
    linked_position_count: int = 0
    created_at: str | None = None
    updated_at: str | None = None
    expires_at: str | None = None
    age_seconds: float | None = None
    active: bool = False
    tags: tuple = field(default_factory=tuple)
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "scenarioId": self.scenario_id,
            "instrument": self.instrument,
            "direction": self.direction,
            "session": self.session,
            "structure": self.structure,
            "entryModel": self.entry_model,
            "timeframe": self.timeframe,
            "status": self.status,
            "outcome": self.outcome,
            "nodeId": self.node_id,
            "accountFingerprintMasked": self.account_fingerprint_masked,
            "linkedRecommendation": self.linked_recommendation,
            "linkedIntentCount": self.linked_intent_count,
            "linkedOrderCount": self.linked_order_count,
            "linkedPositionCount": self.linked_position_count,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "expiresAt": self.expires_at,
            "ageSeconds": self.age_seconds,
            "active": self.active,
            "tags": list(self.tags),
            "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class OperationalSummary:
    projection_timestamp: str
    nodes: tuple = field(default_factory=tuple)
    accounts: tuple = field(default_factory=tuple)
    active_orders: tuple = field(default_factory=tuple)
    open_positions: tuple = field(default_factory=tuple)
    active_scenarios: tuple = field(default_factory=tuple)
    reconciliation_issues: tuple = field(default_factory=tuple)
    active_operations: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)
    freshness: Freshness | None = None
    schema_version: str = SCHEMA_VERSION

    def as_dict(self) -> dict:
        return _sorted({
            "schemaVersion": self.schema_version,
            "projectionTimestamp": self.projection_timestamp,
            "nodes": [n.as_dict() for n in self.nodes],
            "accounts": [a.as_dict() for a in self.accounts],
            "activeOrders": [o.as_dict() for o in self.active_orders],
            "openPositions": [p.as_dict() for p in self.open_positions],
            "activeScenarios": [s.as_dict() for s in self.active_scenarios],
            "reconciliationIssues": [dict(sorted(i.items())) for i in self.reconciliation_issues],
            "activeOperations": [dict(sorted(o.items())) for o in self.active_operations],
            "warnings": list(self.warnings),
            "counts": {
                "nodes": len(self.nodes), "accounts": len(self.accounts),
                "activeOrders": len(self.active_orders),
                "openPositions": len(self.open_positions),
                "activeScenarios": len(self.active_scenarios),
                "reconciliationIssues": len(self.reconciliation_issues),
                "activeOperations": len(self.active_operations),
            },
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-4C — REAL trade-ledger read models.
#
# These replace the LIVE-4A/4B placeholder interfaces. They project already
# recorded ledger facts: nothing here recomputes a financial value, and an
# unavailable number stays unavailable (never zero).
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ClosedTradeOperationalView:
    """One ledger entry as the operator sees it. Every financial field is the
    value the ledger RECORDED — the projection performs no accounting."""
    trade_id: str
    status: str
    version: int = 1
    instrument: str | None = None
    side: str | None = None
    quantity: float | None = None
    average_entry_price: float | None = None
    average_exit_price: float | None = None
    gross_realized_pnl: float | None = None
    total_costs: float | None = None
    net_realized_pnl: float | None = None
    realized_r: float | None = None
    exit_classification: str | None = None
    outcome: str | None = None
    account_currency: str | None = None
    opened_at: str | None = None
    closed_at: str | None = None
    duration_seconds: float | None = None
    scenario_id: str | None = None
    origin: str | None = None
    #: M-LEDGER-ORIGIN-1: which adapter produced/observed the record. A SEPARATE
    #: axis from `origin` (initiation) and from `provenance` (storage class).
    #: Only this field may grant admission to broker history or analytics.
    execution_origin: str | None = None
    cost_completeness: str | None = None
    risk_completeness: str | None = None
    warnings: tuple = field(default_factory=tuple)
    conflicts: tuple = field(default_factory=tuple)
    finalized: bool = False
    detail: dict | None = None
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "tradeId": self.trade_id, "status": self.status, "version": self.version,
            "instrument": self.instrument, "side": self.side,
            "quantity": self.quantity,
            "averageEntryPrice": self.average_entry_price,
            "averageExitPrice": self.average_exit_price,
            "grossRealizedPnL": self.gross_realized_pnl,
            "totalCosts": self.total_costs,
            "netRealizedPnL": self.net_realized_pnl,
            "realizedR": self.realized_r,
            "exitClassification": self.exit_classification,
            "outcome": self.outcome, "accountCurrency": self.account_currency,
            "openedAt": self.opened_at, "closedAt": self.closed_at,
            "durationSeconds": self.duration_seconds,
            "scenarioId": self.scenario_id, "origin": self.origin,
            "executionOrigin": self.execution_origin,
            "costCompleteness": self.cost_completeness,
            "riskCompleteness": self.risk_completeness,
            "warnings": list(self.warnings), "conflicts": list(self.conflicts),
            "finalized": self.finalized, "detail": self.detail,
            "provenance": self.provenance,
        })


@dataclass(frozen=True)
class LedgerOperationalSummary:
    """Ledger TOTALS only. LIVE-4C adds no win rate, expectancy, drawdown,
    equity curve or grouping — those are deliberately out of scope."""
    finalized_trade_count: int = 0
    incomplete_trade_count: int = 0
    conflicted_trade_count: int = 0
    amended_trade_count: int = 0
    gross_realized_pnl: float | None = None
    net_realized_pnl: float | None = None
    total_costs: float | None = None
    latest_closed_at: str | None = None
    account_currency: str | None = None
    availability: str = AVAILABILITY_UNAVAILABLE
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "finalizedTradeCount": self.finalized_trade_count,
            "incompleteTradeCount": self.incomplete_trade_count,
            "conflictedTradeCount": self.conflicted_trade_count,
            "amendedTradeCount": self.amended_trade_count,
            "grossRealizedPnL": self.gross_realized_pnl,
            "netRealizedPnL": self.net_realized_pnl,
            "totalCosts": self.total_costs,
            "latestClosedAt": self.latest_closed_at,
            "accountCurrency": self.account_currency,
            "availability": self.availability, "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class TradeLedgerOperationalView:
    """The ledger read surface: totals plus the projected trades."""
    available: bool = False
    code: str | None = None
    summary: LedgerOperationalSummary = field(default_factory=LedgerOperationalSummary)
    trades: tuple = field(default_factory=tuple)
    total_count: int = 0
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "available": self.available, "code": self.code,
            "summary": self.summary.as_dict(),
            "trades": [t.as_dict() for t in self.trades],
            "totalCount": self.total_count, "provenance": self.provenance,
        })


def _ledger_entry_view(entry: Any, *, provenance: str) -> ClosedTradeOperationalView:
    """Project one recorded ledger entry. Reads only what the ledger stored."""
    raw = entry.as_dict()
    trade = raw.get("trade") or {}
    lineage = trade.get("lineage") or {}
    return ClosedTradeOperationalView(
        trade_id=raw.get("tradeId"), status=raw.get("status"),
        version=int(raw.get("version") or 1),
        instrument=trade.get("instrument"), side=trade.get("side"),
        quantity=trade.get("totalExitQuantity") or trade.get("totalEntryQuantity"),
        average_entry_price=trade.get("averageEntryPrice"),
        average_exit_price=trade.get("averageExitPrice"),
        gross_realized_pnl=trade.get("grossRealizedPnL"),
        total_costs=trade.get("totalCosts"),
        net_realized_pnl=trade.get("netRealizedPnL"),
        realized_r=trade.get("realizedR"),
        exit_classification=trade.get("exitClassification"),
        outcome=trade.get("outcome"),
        account_currency=trade.get("accountCurrency"),
        opened_at=trade.get("openedAt"), closed_at=trade.get("closedAt"),
        duration_seconds=trade.get("durationSeconds"),
        scenario_id=lineage.get("scenarioId"), origin=lineage.get("origin"),
        # Derived at projection time from the adapter the ledger recorded, via
        # the canonical taxonomy — never a raw string from the payload.
        execution_origin=_tld.ExecutionOrigin.normalize(lineage.get("adapter")),
        cost_completeness=trade.get("costCompleteness"),
        risk_completeness=trade.get("riskCompleteness"),
        warnings=tuple(raw.get("warnings") or ()),
        conflicts=tuple(raw.get("conflicts") or ()),
        finalized=bool(raw.get("finalized")),
        detail=raw, provenance=provenance)


def build_trade_ledger(entries=None, totals=None, *, now: str,
                       provenance: str = PROV_DURABLE_STORE,
                       total_count: int | None = None,
                       stale_after_s: float = DEFAULT_STALE_AFTER_S,
                       code: str | None = None) -> TradeLedgerOperationalView:
    """Project the ledger. `entries is None` means the LEDGER STORE COULD NOT BE
    READ — reported explicitly, never as an empty (healthy-looking) ledger."""
    if entries is None:
        return TradeLedgerOperationalView(
            available=False, code=code or "ledger_store_unavailable",
            summary=LedgerOperationalSummary(
                availability=AVAILABILITY_UNAVAILABLE, provenance=PROV_ABSENT,
                freshness=freshness(now=now, source_at=None, available=False,
                                    stale_after_s=stale_after_s,
                                    detail="ledger store unavailable")))
    views = tuple(_ledger_entry_view(e, provenance=provenance) for e in entries)
    summary = LedgerOperationalSummary(availability=AVAILABILITY_OK,
                                       provenance=provenance)
    if totals is not None:
        t = totals.as_dict()
        summary = LedgerOperationalSummary(
            finalized_trade_count=t.get("finalizedTradeCount", 0),
            incomplete_trade_count=t.get("incompleteTradeCount", 0),
            conflicted_trade_count=t.get("conflictedTradeCount", 0),
            amended_trade_count=t.get("amendedTradeCount", 0),
            gross_realized_pnl=t.get("grossRealizedPnL"),
            net_realized_pnl=t.get("netRealizedPnL"),
            total_costs=t.get("totalCosts"),
            latest_closed_at=t.get("latestClosedAt"),
            account_currency=t.get("accountCurrency"),
            availability=AVAILABILITY_OK, provenance=provenance,
            freshness=freshness(now=now, source_at=t.get("latestClosedAt"),
                                available=True, stale_after_s=stale_after_s))
    return TradeLedgerOperationalView(
        available=True, code=None, summary=summary, trades=views,
        total_count=total_count if total_count is not None else len(views),
        provenance=provenance)


# ─────────────────────────────────────────────────────────────────────────────
# LIVE-4D — Recommendation read models. Derived only; NO performance metrics.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecommendationDecisionView:
    """One decision as the operator sees it. The actor is already pseudonymized
    by the domain — a raw operator id never reaches a read model."""
    decision_id: str
    decision_type: str
    actor_type: str
    actor: str | None = None
    occurred_at: str | None = None
    sequence: int = 1
    reason: str | None = None
    authorization_reference: str | None = None
    execution_mode: str | None = None
    #: LIVE-4E audit fields. `identity_assurance` states how much the recorded
    #: actor is worth — the UI shows it so nobody reads an asserted identity as
    #: an authenticated one.
    note: str | None = None
    against_version: int | None = None
    correlation_id: str | None = None
    identity_assurance: str = "unknown"

    def as_dict(self) -> dict:
        return _sorted({
            "decisionId": self.decision_id, "decisionType": self.decision_type,
            "actorType": self.actor_type, "actor": self.actor,
            "occurredAt": self.occurred_at, "sequence": self.sequence,
            "reason": self.reason,
            "authorizationReference": self.authorization_reference,
            "executionMode": self.execution_mode, "note": self.note,
            "againstVersion": self.against_version,
            "correlationId": self.correlation_id,
            "identityAssurance": self.identity_assurance,
        })


@dataclass(frozen=True)
class RecommendationOperationalView:
    recommendation_id: str
    scenario_id: str
    instrument: str | None = None
    direction: str | None = None
    source: str | None = None
    status: str | None = None
    outcome: str | None = None
    latest_decision: RecommendationDecisionView | None = None
    actor_type: str | None = None
    proposed_entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    quantity: float | None = None
    risk_amount: float | None = None
    risk_percent: float | None = None
    planned_r: float | None = None
    rationale: str | None = None
    confidence: float | None = None
    linked_intent_count: int = 0
    linked_intent_ids: tuple = field(default_factory=tuple)
    superseded_by: str | None = None
    supersedes: str | None = None
    created_at: str | None = None
    expires_at: str | None = None
    age_seconds: float | None = None
    past_due: bool = False
    active: bool = False
    node_id: str | None = None
    account_fingerprint_masked: str | None = None
    execution_terms_availability: str | None = None
    risk_terms_availability: str | None = None
    #: LIVE-4E: the optimistic-concurrency version an operator must decide
    #: against, plus whether a decision is possible at all and why not.
    version: int = 0
    decidable: bool = False
    undecidable_reason: str | None = None
    warnings: tuple = field(default_factory=tuple)
    tags: tuple = field(default_factory=tuple)
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "recommendationId": self.recommendation_id,
            "scenarioId": self.scenario_id, "instrument": self.instrument,
            "direction": self.direction, "source": self.source,
            "status": self.status, "outcome": self.outcome,
            "latestDecision": self.latest_decision.as_dict()
                              if self.latest_decision else None,
            "actorType": self.actor_type,
            "proposedEntry": self.proposed_entry, "stopLoss": self.stop_loss,
            "takeProfit": self.take_profit, "quantity": self.quantity,
            "riskAmount": self.risk_amount, "riskPercent": self.risk_percent,
            "plannedR": self.planned_r, "rationale": self.rationale,
            "confidence": self.confidence,
            "linkedIntentCount": self.linked_intent_count,
            "linkedIntentIds": list(self.linked_intent_ids),
            "supersededBy": self.superseded_by, "supersedes": self.supersedes,
            "createdAt": self.created_at, "expiresAt": self.expires_at,
            "ageSeconds": self.age_seconds, "pastDue": self.past_due,
            "active": self.active, "nodeId": self.node_id,
            "accountFingerprintMasked": self.account_fingerprint_masked,
            "executionTermsAvailability": self.execution_terms_availability,
            "riskTermsAvailability": self.risk_terms_availability,
            "version": self.version, "decidable": self.decidable,
            "undecidableReason": self.undecidable_reason,
            "warnings": list(self.warnings), "tags": list(self.tags),
            "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class RecommendationOperationalSummary:
    active_count: int = 0
    pending_decision_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    expired_count: int = 0
    withdrawn_count: int = 0
    superseded_count: int = 0
    execution_linked_count: int = 0
    latest_created_at: str | None = None
    availability: str = AVAILABILITY_UNAVAILABLE
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "activeCount": self.active_count,
            "pendingDecisionCount": self.pending_decision_count,
            "acceptedCount": self.accepted_count,
            "rejectedCount": self.rejected_count,
            "expiredCount": self.expired_count,
            "withdrawnCount": self.withdrawn_count,
            "supersededCount": self.superseded_count,
            "executionLinkedCount": self.execution_linked_count,
            "latestCreatedAt": self.latest_created_at,
            "availability": self.availability, "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


def build_recommendations(recommendations=None, *, now: str,
                          decisions_for=None, warnings_for=None,
                          stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    """Project recommendations. `recommendations is None` means the store could
    not be read — the caller reports that explicitly (an empty tuple never
    claims 'no recommendations exist')."""
    if recommendations is None:
        return ()
    decisions_lookup = decisions_for or (lambda _rid: [])
    warnings_lookup = warnings_for or (lambda _r: ())
    out = []
    for item in recommendations:
        try:
            decisions = list(decisions_lookup(item.recommendation_id))
            latest = None
            if decisions:
                newest = sorted(decisions,
                                key=lambda d: (d.sequence, d.occurred_at,
                                               d.decision_id))[-1]
                view = newest.safe_view()
                latest = RecommendationDecisionView(
                    decision_id=view["decisionId"],
                    decision_type=view["decisionType"],
                    actor_type=view["actorType"], actor=view["actor"],
                    occurred_at=view["occurredAt"], sequence=view["sequence"],
                    reason=view["reason"],
                    authorization_reference=view["authorizationReference"],
                    execution_mode=view["executionMode"],
                    note=view.get("note"),
                    against_version=view.get("againstVersion"),
                    correlation_id=view.get("correlationId"),
                    identity_assurance=view.get("identityAssurance", "unknown"))
            terms = item.terms
            out.append(RecommendationOperationalView(
                recommendation_id=item.recommendation_id,
                scenario_id=item.scenario_id, instrument=item.instrument,
                direction=item.direction, source=item.source, status=item.status,
                outcome=item.outcome, latest_decision=latest,
                actor_type=latest.actor_type if latest else None,
                proposed_entry=terms.execution.requested_entry_price,
                stop_loss=terms.risk.stop_loss, take_profit=terms.risk.take_profit,
                quantity=terms.execution.quantity,
                risk_amount=terms.risk.risk_amount,
                risk_percent=terms.risk.risk_percent,
                version=item.version, decidable=item.decidable,
                undecidable_reason=item.undecidable_reason,
                planned_r=terms.risk.planned_r, rationale=terms.rationale,
                confidence=terms.confidence,
                linked_intent_count=len(item.linked_intent_ids),
                linked_intent_ids=tuple(item.linked_intent_ids),
                superseded_by=item.superseded_by, supersedes=item.supersedes,
                created_at=item.created_at, expires_at=item.expiry_at,
                age_seconds=item.age_seconds(now), past_due=item.past_due(now),
                active=item.active, node_id=item.node_id,
                account_fingerprint_masked=_mask_fingerprint(item.account_fingerprint),
                execution_terms_availability=terms.execution.availability,
                risk_terms_availability=terms.risk.availability,
                warnings=tuple(warnings_lookup(item)), tags=tuple(terms.tags),
                provenance=PROV_DURABLE_STORE,
                freshness=freshness(now=now, source_at=item.updated_at,
                                    available=True, stale_after_s=stale_after_s)))
        except Exception:
            continue                     # a malformed row is skipped, never faked
    out.sort(key=lambda v: (v.created_at or "", v.recommendation_id), reverse=True)
    return tuple(out)


def build_recommendation_summary(totals=None, *, now: str,
                                 stale_after_s: float = DEFAULT_STALE_AFTER_S
                                 ) -> RecommendationOperationalSummary:
    """Counts only. No win rate, expectancy or any performance metric."""
    if totals is None:
        return RecommendationOperationalSummary(
            availability=AVAILABILITY_UNAVAILABLE, provenance=PROV_ABSENT,
            freshness=freshness(now=now, source_at=None, available=False,
                                stale_after_s=stale_after_s,
                                detail="recommendation store unavailable"))
    t = totals.as_dict()
    return RecommendationOperationalSummary(
        active_count=t["activeCount"],
        pending_decision_count=t["pendingDecisionCount"],
        accepted_count=t["acceptedCount"], rejected_count=t["rejectedCount"],
        expired_count=t["expiredCount"], withdrawn_count=t["withdrawnCount"],
        superseded_count=t["supersededCount"],
        execution_linked_count=t["executionLinkedCount"],
        latest_created_at=t["latestCreatedAt"],
        availability=AVAILABILITY_OK, provenance=PROV_DURABLE_STORE,
        freshness=freshness(now=now, source_at=t["latestCreatedAt"],
                            available=True, stale_after_s=stale_after_s))


# ─────────────────────────────────────────────────────────────────────────────
# Sources — the read-only bundle the builder derives from.
# Every entry is a CALLABLE the runtime supplies, so this module imports no
# server, no adapter and no store, and can be exercised with plain fakes.
# ─────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ProjectionSources:
    #: () -> dict | None — adapter reconcile_snapshot data (positions/orders/
    #: accounts/connection/accountIdentity/at/provenance). None = unavailable.
    broker_snapshot: Callable[[], dict | None] = lambda: None
    #: () -> dict | None — adapter account_snapshot data. None = unavailable.
    account_snapshot: Callable[[], dict | None] = lambda: None
    #: () -> str — active adapter kind ("mock" | "mt5").
    adapter_kind: Callable[[], str] = lambda: "mock"
    #: () -> str — adapter connection state.
    connection_state: Callable[[], str] = lambda: "Disconnected"
    #: () -> list[dict] — node telemetry observation envelopes.
    node_entries: Callable[[], list] = list
    #: () -> list[dict] — execution-store intent rows.
    intents: Callable[[], list] = list
    #: (intent_id) -> list[dict] — append-only transition history.
    transitions: Callable[[str], list] = lambda _i: []
    #: () -> dict — reconciliation safety posture.
    reconciliation_posture: Callable[[], dict] = dict
    #: () -> dict | None — latest reconciliation run (with items).
    latest_reconciliation: Callable[[], dict | None] = lambda: None
    #: () -> str — governed execution mode.
    execution_mode: Callable[[], str] = lambda: "observe"
    #: () -> dict | None — active authorization grant safe_view.
    authorization: Callable[[], dict | None] = lambda: None
    #: () -> list[dict] — active entity locks.
    entity_locks: Callable[[], list] = list
    #: LIVE-4B: () -> list — canonical Scenario objects from the scenario store,
    #: or None when the store is unavailable (reported explicitly, never as an
    #: empty "no scenarios" answer).
    scenarios: Callable[[], Any] = lambda: None


#: Lifecycle states treated as an ACTIVE order (tower-side work in flight).
ACTIVE_ORDER_STATES = ("submitting", "submitted", "acknowledged", "partially_filled",
                       "modify_pending", "cancel_pending", "close_pending",
                       "unknown", "reconciliation_required")

#: The LIVE-3 entity-management commands (their intents mutate an existing entity).
_ENTITY_COMMANDS = ("ModifyPositionProtection", "CancelPendingOrder", "ClosePosition")


def _mask_fingerprint(value: Any) -> str | None:
    """Masked account identity — never a full fingerprint in a read model."""
    if not value:
        return None
    s = str(value)
    return f"{s[:4]}…{s[-4:]}" if len(s) > 10 else "…"


def _entity_view(entry: dict) -> dict:
    """Normalize a broker position/order record across adapter shapes."""
    return {
        "id": str(entry.get("positionId") or entry.get("orderId") or entry.get("id") or ""),
        "symbol": entry.get("canonicalSymbol") or entry.get("symbol"),
        "side": entry.get("side"),
        "size": entry.get("size", entry.get("volume")),
        "entry": entry.get("entry", entry.get("entryPrice", entry.get("price"))),
        "sl": entry.get("sl", entry.get("stop")),
        "tp": entry.get("tp", entry.get("target")),
        "state": entry.get("state"),
        "pnl": entry.get("unrealizedPnl", entry.get("profit")),
        "deploymentId": entry.get("deploymentId"),
    }


def _payload_of(row: dict) -> dict:
    import json
    try:
        meta = json.loads(row.get("metadata_json") or "{}")
        payload = meta.get("payload")
        return payload if isinstance(payload, dict) else {}
    except (ValueError, TypeError):
        return {}


def _scenario_of(row: dict, payload: dict) -> str | None:
    """The only scenario evidence that exists: a `scenarioKey` echoed in a
    command payload. Absent -> None (never invented)."""
    key = payload.get("scenarioKey")
    return str(key) if key else None


def _provenance_for(adapter_kind: str, *, observed: bool = True) -> str:
    """M-MT5-READ-1: provenance is earned by OBSERVATION, not by configuration.

    Delegates to the single policy seam. `observed` must be the caller's
    evidence that the adapter actually returned this record's data; the previous
    signature had no way to express that, which is how a configured-but-silent
    MT5 adapter came to stamp `live_mt5` on an all-null account.
    """
    return brokerprov.for_local_adapter(adapter_kind, observed=observed)


# ─────────────────────────────────────────────────────────────────────────────
# Builders — pure, deterministic, injected clock.
# ─────────────────────────────────────────────────────────────────────────────

def _num_or_none(value: Any) -> float | None:
    """A finite number, or None. A bool is not a number; a numeric STRING is not
    a number either.

    DELIBERATELY STRICTER THAN `_num` BELOW, and the two are kept apart on
    purpose. `_num` coerces (`float("4211.5")` succeeds) and lets NaN through;
    that is tolerable for the local runtime snapshot, which this process built
    itself. This one reads a payload that arrived over a network from another
    machine, where a string where a number belongs is a contract violation and
    NaN is not a balance. Coercing either would turn a malformed packet into a
    displayed figure.

    `null is not zero` is the rule this whole projection exists to protect, and
    it has to hold at every boundary the data crosses — including this one.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _bool_or_none(value: Any) -> bool | None:
    """Tri-state. `None` means the observer did not report — NOT `False`.

    Collapsing an unreported `trade_allowed` to False would tell an operator the
    terminal forbids trading, which is a different and alarming claim from "the
    node did not say".
    """
    return value if isinstance(value, bool) else None


def _node_accounts(sources: ProjectionSources, *, now: str,
                   stale_after_s: float) -> tuple:
    """Accounts an EXECUTION NODE observed on its MT5 terminal, relayed via
    `ct.node-telemetry.v1`.

    THE ADMISSION RULE
        A node view is produced only when the node's own `account.health` or
        `account.identity` says `available: true`. The arrival of a snapshot is
        NOT evidence of an account observation: the node samples account state
        only on cycles that contain an OPEN, so a perfectly healthy node
        publishes `available: false` most of the time. Treating that as an
        account reading would invent one, and treating it as "no account exists"
        would be equally false — so such a node simply contributes nothing here
        and the surface falls through to UNAVAILABLE.

    WHAT IS DELIBERATELY NOT DERIVED
        `margin`, `marginLevel` and `leverage` are not published by the node and
        stay null. `realizedPnLToday` stays null even though the node publishes
        `risk.daily_realized_r`: R is a risk multiple, not the account currency,
        and converting one to the other here would fabricate a monetary figure
        out of a ratio. `unrealizedPnL` stays null because the node's positions
        carry no price or profit at all (see `live/telemetry.safe_positions`).

    FRESHNESS IS THE TOWER'S CLOCK
        `Freshness` is computed from `received_at` — when THIS tower received the
        packet — not from the node's `published_at`. A node cannot make itself
        look fresh by publishing a manipulated timestamp. The envelope's own
        `stale` verdict (which is liveness-stale OR data-stale) is then OR-ed in,
        so this can only ever be more pessimistic than the shared rule, never
        more permissive.
    """
    try:
        entries = sources.node_entries() or []
    except Exception:
        return ()
    # M-ACTIVATE-READINESS-1 — the deployment's pinned identity, if any. Read
    # once per projection so every record in one answer is judged against the
    # same expectation.
    expected_account, expected_server = activation_policy.expected_identity()
    out: list[AccountOperationalView] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, dict):
            continue
        account = snapshot.get("account")
        account = account if isinstance(account, dict) else {}
        identity = account.get("identity") if isinstance(account.get("identity"), dict) else {}
        health = account.get("health") if isinstance(account.get("health"), dict) else {}
        health_seen = health.get("available") is True
        identity_seen = identity.get("available") is True
        if not (health_seen or identity_seen):
            continue                    # the node observed no account this cycle
        node_id = entry.get("instance_id")
        admissible, admission_reasons = activation_policy.account_admissible(
            entry, expected_account=expected_account, expected_server=expected_server)
        # Liveness on the tower's clock; the envelope's verdict can only tighten it.
        fresh = freshness(now=now, source_at=entry.get("received_at"),
                          available=True, stale_after_s=stale_after_s,
                          detail=f"relayed by execution node {node_id}")
        if entry.get("stale") is not False and fresh.available:
            fresh = Freshness(projection_at=fresh.projection_at,
                              source_at=fresh.source_at,
                              age_seconds=fresh.age_seconds, stale=True,
                              available=True,
                              stale_after_seconds=fresh.stale_after_seconds,
                              detail=fresh.detail)
        out.append(AccountOperationalView(
            # Already a one-way SHA-256 fingerprint when the node built it
            # (`live/telemetry.account_fingerprint`). The login never leaves the
            # VPS, so there is nothing here to redact a second time.
            account_fingerprint=identity.get("fingerprint"),
            # The node reports the SERVER it is connected to. It does not report
            # a broker company name, and deriving one from the server string
            # would be a guess.
            broker=None,
            server=identity.get("server"),
            balance=_num_or_none(health.get("balance")) if health_seen else None,
            equity=_num_or_none(health.get("equity")) if health_seen else None,
            free_margin=_num_or_none(health.get("free_margin")) if health_seen else None,
            margin=None, margin_level=None, leverage=None,
            currency=health.get("currency") if health_seen else identity.get("currency"),
            unrealized_pnl=None, realized_pnl_today=None, open_risk=None,
            trade_allowed=_bool_or_none(health.get("trade_allowed")) if health_seen else None,
            trade_expert=_bool_or_none(health.get("trade_expert")) if health_seen else None,
            # This tower holds no connection to the account; the node does.
            connection_state=None,
            node_id=node_id,
            observed_at=health.get("observed_at") if health_seen else None,
            provenance=brokerprov.for_node_observation(observed=True),
            # Provenance is earned (the node observed a terminal); admission is
            # checked (it observed the RIGHT one). An unpinned deployment cannot
            # perform the second check, so `admitted` is True and the checker
            # says WARN rather than PASS — an unverifiable claim is not a
            # verified one.
            admitted=admissible,
            admission_reasons=admission_reasons,
            freshness=fresh))
    # Stable, total ordering by node then account, so a render key never depends
    # on arrival order and one node's row can never take another's place.
    out.sort(key=lambda a: (a.node_id or "", a.account_fingerprint or ""))
    return tuple(out)


def _local_admission(fingerprint, server, expected_account, expected_server) -> tuple:
    """The identity pin, applied to a LOCALLY-read account.

    The pin was originally enforced only on node-relayed records, on the
    reasoning that this Mac cannot read a terminal. That reasoning is about the
    current host, not about the code — on any host where `live_mt5` is
    producible the pin would have been silently unenforced while the runbook
    told the operator it was pinned. The same rule now applies to both sources.
    """
    reasons = []
    if expected_account:
        if not fingerprint:
            reasons.append(activation_policy.R_ACCOUNT_IDENTITY_UNVERIFIABLE)
        elif fingerprint != expected_account:
            reasons.append(activation_policy.R_ACCOUNT_IDENTITY_MISMATCH)
    if expected_server:
        if not server:
            reasons.append(activation_policy.R_ACCOUNT_IDENTITY_UNVERIFIABLE)
        elif server != expected_server:
            reasons.append(activation_policy.R_ACCOUNT_SERVER_MISMATCH)
    return (not reasons), tuple(dict.fromkeys(reasons))


def _local_accounts(sources: ProjectionSources, *, now: str,
                    stale_after_s: float) -> tuple:
    """Accounts THIS PROCESS'S OWN broker adapter observed. Never consults nodes.

    Returns `()` when the adapter observed nothing at all, which is the ordinary
    case on the operator's Mac: the MetaTrader5 binding is Windows local-terminal
    IPC, so `MT5Adapter._gateway` is None and every read is unavailable.

    Extracted from `build_accounts` so that "what the local adapter saw" and
    "what the nodes relayed" are two independent answers that can be COMPARED.
    While they were interleaved in one branching function, the winner was decided
    by control flow, which is why a contradiction between them had nowhere to be
    reported: one branch simply returned before the other ran.
    """
    kind = sources.adapter_kind()
    conn = sources.connection_state()
    acct = sources.account_snapshot()
    snap = sources.broker_snapshot()
    snap_at = (snap or {}).get("at")
    expected_account, expected_server = activation_policy.expected_identity()

    # OBSERVATION IS EVIDENCE, NOT A TYPE CHECK.
    #
    # `observed=True` used to follow from "the adapter returned a dict at all",
    # which meant an adapter answering with an empty or all-null sample produced
    # `provenance: live_mt5, balance: null` — the very record shape
    # `broker_provenance` exists to eliminate, arriving through the other door.
    # The node path has always required a positive `available is True`; the
    # local path now requires at least one field with something in it.
    def _carries(sample: dict, fields: tuple) -> bool:
        return any(sample.get(f) is not None for f in fields)

    if not isinstance(acct, dict):
        # No dedicated account snapshot. Fall back to the accounts the broker
        # snapshot itself reports (real evidence, e.g. the fixture world's
        # accounts) — still derived, never invented.
        listed = (snap or {}).get("accounts") or []
        first = listed[0] if isinstance(listed, list) and listed else None
        if isinstance(first, dict) and _carries(
                first, ("accountId", "balance", "equity", "brokerId", "broker")):
            fingerprint = first.get("accountId") or (snap or {}).get("accountIdentity")
            admitted, reasons = _local_admission(
                fingerprint, None, expected_account, expected_server)
            return (AccountOperationalView(
                account_fingerprint=fingerprint,
                broker=first.get("brokerId") or first.get("broker"),
                server=None,
                balance=_num_or_none(first.get("balance")),
                equity=_num_or_none(first.get("equity")),
                margin=None, margin_level=None, leverage=None,
                currency=first.get("baseCurrency") or first.get("currency"),
                unrealized_pnl=None, realized_pnl_today=None, open_risk=None,
                connection_state=conn,
                provenance=_provenance_for(kind, observed=True),
                admitted=admitted, admission_reasons=reasons,
                freshness=freshness(now=now, source_at=snap_at, available=True,
                                    stale_after_s=stale_after_s,
                                    detail="derived from broker snapshot accounts")),)
        return ()

    if not _carries(acct, ("fingerprint", "balance", "equity", "server",
                           "margin", "leverage", "currency")):
        return ()                    # a sample carrying nothing observed nothing

    unrealized = None
    if isinstance(snap, dict):
        pnls = [_entity_view(p).get("pnl") for p in (snap.get("positions") or [])]
        numeric = [p for p in pnls if isinstance(p, (int, float)) and not isinstance(p, bool)]
        unrealized = round(sum(numeric), 2) if numeric else None
    fingerprint = acct.get("fingerprint") or (snap or {}).get("accountIdentity")
    admitted, reasons = _local_admission(fingerprint, acct.get("server"),
                                         expected_account, expected_server)
    return (AccountOperationalView(
        account_fingerprint=fingerprint,
        broker=acct.get("broker_company"),
        server=acct.get("server"),
        balance=_num_or_none(acct.get("balance")),
        equity=_num_or_none(acct.get("equity")),
        margin=_num_or_none(acct.get("margin")),
        margin_level=_num_or_none(acct.get("margin_level")),
        leverage=acct.get("leverage"), currency=acct.get("currency"),
        unrealized_pnl=unrealized,
        # Not derivable from current evidence — reported as absent, not zero.
        realized_pnl_today=None, open_risk=None,
        connection_state=conn, provenance=_provenance_for(kind, observed=True),
        admitted=admitted, admission_reasons=reasons,
        freshness=freshness(now=now, source_at=acct.get("at") or snap_at,
                            available=True, stale_after_s=stale_after_s)),)


def build_accounts(sources: ProjectionSources, *, now: str,
                   stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    """Accounts, from whichever authority actually observed one.

    M-MT5-READ-1 — ORDER OF AUTHORITY, and why it is this way round.

    This process's own broker adapter and the execution nodes are asked
    INDEPENDENTLY, and `activation_policy.reconcile_account_sources` decides
    between them. Precedence is by AUTHORITY, not locality: a local read wins
    only if it is genuine broker truth. `mock-fixture` is not, so the fixture's
    $100,000 account can never shadow a node relaying real MT5 truth — the
    defect M-NODE-READ-1 found, where real data was suppressed by invented data
    and the frontend gate then reported "unavailable".

    M-ACTIVATE-READINESS-1 — WHY THE TWO SOURCES ARE NO LONGER INTERLEAVED.

    The previous shape consulted the nodes only inside the branch where the
    local adapter had failed, so when BOTH were genuine the local one returned
    first and the node was never asked. That is unreachable on today's Mac — the
    local adapter cannot be a real terminal — but it made the contradiction case
    structurally unrepresentable, and `reconcile_account_sources` had no call
    site. A documented rule with no call site is the exact defect this programme
    keeps finding.

    Both sources are now computed and compared. Where they describe the SAME
    account with DIFFERENT money, both records survive and `build_summary`
    raises the contradiction as a warning. Nothing is averaged and nothing is
    silently preferred.

    Where neither observed anything, the answer is one explicitly UNAVAILABLE
    view — never a zeroed balance and never an invented account.
    """
    local = _local_accounts(sources, now=now, stale_after_s=stale_after_s)
    relayed = _node_accounts(sources, now=now, stale_after_s=stale_after_s)

    admitted, _warnings = activation_policy.reconcile_account_sources(relayed, local)
    if admitted:
        return admitted
    # Nothing genuine anywhere. An inadmissible local record (the mock adapter's)
    # still travels to the endpoint carrying its honest `mock-fixture` stamp —
    # every gate rejects it, and suppressing it here would hide from an operator
    # that the process is running on a mock adapter at all.
    if local:
        return local

    snap = sources.broker_snapshot()
    return (AccountOperationalView(
        connection_state=sources.connection_state(),
        # NOT `live_mt5`. Nothing was read, so nothing may claim an origin.
        provenance=_provenance_for(sources.adapter_kind(), observed=False),
        freshness=freshness(now=now, source_at=(snap or {}).get("at"), available=False,
                            stale_after_s=stale_after_s,
                            detail="account snapshot unavailable")),)


def build_orders(sources: ProjectionSources, *, now: str) -> tuple:
    """Active ORDERS: tower intents still in flight, joined to the broker's own
    working orders where a reference matches. Orders and positions stay
    separate concepts — a filled market order becomes a POSITION, not an order."""
    kind = sources.adapter_kind()
    # Only ever applied to an order the broker actually reported (`bo` below),
    # so the observation is genuine by construction.
    prov = _provenance_for(kind, observed=True)
    snap = sources.broker_snapshot() or {}
    broker_orders = {}
    for o in (snap.get("orders") or []):
        v = _entity_view(o)
        if v["id"]:
            broker_orders[v["id"]] = v
    posture = sources.reconciliation_posture() or {}
    out = []
    for row in sources.intents():
        if row.get("state") not in ACTIVE_ORDER_STATES:
            continue
        payload = _payload_of(row)
        ref = row.get("broker_ref")
        bo = broker_orders.get(str(ref)) if ref else None
        history = tuple(
            {"state": t.get("to_state"), "at": t.get("at"), "reason": t.get("reason"),
             "brokerRef": t.get("broker_ref")}
            for t in sources.transitions(row.get("intent_id") or ""))
        needs_recon = row.get("state") in ("unknown", "reconciliation_required")
        out.append(OrderOperationalView(
            intent_id=row.get("intent_id"),
            broker_order_reference=ref,
            broker_ticket=ref,
            instrument=row.get("instrument") or payload.get("instrument")
                       or (bo or {}).get("symbol"),
            side=row.get("side") or payload.get("side") or (bo or {}).get("side"),
            order_type=row.get("order_type"),
            quantity=row.get("quantity") if row.get("quantity") is not None
                     else payload.get("quantity"),
            requested_price=row.get("entry"),
            current_state=row.get("state"),
            lifecycle=history,
            broker_status=(bo or {}).get("state") if bo else (
                "not_reported_by_broker" if ref else None),
            scenario_id=_scenario_of(row, payload),
            created_at=row.get("created_at"), updated_at=row.get("updated_at"),
            reconciliation={
                "required": bool(needs_recon),
                "criticalUnresolved": bool(posture.get("criticalUnresolved")),
                "lastRunId": posture.get("lastRunId"),
            },
            provenance=prov if bo else PROV_DURABLE_STORE))
    out.sort(key=lambda v: (v.created_at or "", v.intent_id or ""))
    return tuple(out)


def build_positions(sources: ProjectionSources, *, now: str) -> tuple:
    """Open POSITIONS as the broker reports them, enriched with tower lifecycle
    where an intent references the same entity. Broker truth leads."""
    kind = sources.adapter_kind()
    snap = sources.broker_snapshot()
    if not isinstance(snap, dict):
        return ()                              # unavailable -> no invented positions
    # Reached only with a snapshot in hand: every position below came from it.
    prov = _provenance_for(kind, observed=True)
    posture = sources.reconciliation_posture() or {}
    locks = {str(l.get("entity_ref")): l for l in (sources.entity_locks() or [])}
    # Tower lifecycle by entity reference (LIVE-3 operations name their entity).
    by_entity: dict[str, list] = {}
    for row in sources.intents():
        payload = _payload_of(row)
        ref = str(payload.get("positionRef") or "")
        if ref:
            by_entity.setdefault(ref, []).append(row)
    out = []
    for raw in (snap.get("positions") or []):
        v = _entity_view(raw)
        rows = by_entity.get(v["id"], [])
        newest = max(rows, key=lambda r: r.get("updated_at") or "", default=None)
        history = tuple(
            {"state": t.get("to_state"), "at": t.get("at"), "reason": t.get("reason")}
            for t in (sources.transitions(newest.get("intent_id") or "") if newest else []))
        has_sl = isinstance(v["sl"], (int, float)) and not isinstance(v["sl"], bool) and v["sl"] > 0
        has_tp = isinstance(v["tp"], (int, float)) and not isinstance(v["tp"], bool) and v["tp"] > 0
        protection = ("protected" if has_sl and has_tp else
                      "stop_only" if has_sl else
                      "target_only" if has_tp else "unprotected")
        payload = _payload_of(newest) if newest else {}
        out.append(PositionOperationalView(
            broker_position_reference=v["id"],
            instrument=v["symbol"], side=v["side"], quantity=v["size"],
            entry_price=v["entry"],
            current_price=None,          # no price feed in the snapshot — never invented
            unrealized_pnl=v["pnl"],
            stop_loss=v["sl"] if has_sl else None,
            take_profit=v["tp"] if has_tp else None,
            age_seconds=None,            # broker snapshots carry no open time
            lifecycle=history,
            protection_state=protection,
            reconciliation={
                "required": bool(newest and newest.get("state") in
                                 ("unknown", "reconciliation_required")),
                "criticalUnresolved": bool(posture.get("criticalUnresolved")),
                "locked": v["id"] in locks,
                "lockOperation": (locks.get(v["id"]) or {}).get("operation"),
            },
            scenario_id=_scenario_of(newest or {}, payload),
            account_fingerprint=snap.get("accountIdentity"),
            provenance=prov))
    out.sort(key=lambda p: p.broker_position_reference or "")
    return tuple(out)


def build_scenarios(sources: ProjectionSources, *, now: str,
                    stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    """LIVE-4B — project canonical Scenarios from the SCENARIO STORE.

    This replaces the LIVE-4A placeholder, which could only echo a `scenarioKey`
    string found in a command payload. Nothing is generated here: the store is
    read and its scenarios are projected. When the store is unavailable the
    result is empty and the caller surfaces that explicitly (see
    `scenarios_available`) — an empty projection never claims "no scenarios".
    """
    scenarios = sources.scenarios()
    if scenarios is None:
        return ()
    out = []
    for scenario in scenarios:
        try:
            out.append(ScenarioOperationalView(
                scenario_id=scenario.scenario_id,
                instrument=scenario.instrument,
                direction=scenario.direction,
                session=scenario.session,
                structure=scenario.structure,
                entry_model=scenario.entry_model,
                timeframe=scenario.timeframe,
                status=scenario.status,
                outcome=scenario.outcome,
                node_id=scenario.node_id,
                account_fingerprint_masked=_mask_fingerprint(scenario.account_fingerprint),
                linked_recommendation=scenario.linked_recommendation_id,
                linked_intent_count=len(scenario.linked_intent_ids),
                linked_order_count=len(scenario.linked_order_ids),
                linked_position_count=len(scenario.linked_position_ids),
                created_at=scenario.created_at,
                updated_at=scenario.updated_at,
                expires_at=scenario.expires_at,
                age_seconds=scenario.age_seconds(now),
                active=scenario.active,
                tags=tuple(scenario.tags),
                provenance=PROV_DURABLE_STORE,
                freshness=freshness(now=now, source_at=scenario.updated_at,
                                    available=True, stale_after_s=stale_after_s)))
        except Exception:
            continue                       # a malformed scenario is skipped, never faked
    out.sort(key=lambda v: (v.created_at or "", v.scenario_id))
    return tuple(out)


def scenarios_available(sources: ProjectionSources) -> bool:
    """Whether the scenario store could be read at all — so an empty list is
    never confused with an unavailable store."""
    return sources.scenarios() is not None


def _node_position_count(snapshot: Any) -> int | None:
    """How many positions the NODE says it holds, or None if it did not say.

    v1 requires a `positions` list, so a validated snapshot always has one and a
    genuine empty list is a real measurement of zero — the one case where 0 is
    the honest answer. A malformed or missing list is not: it yields None.
    """
    if not isinstance(snapshot, dict):
        return None
    positions = snapshot.get("positions")
    return len(positions) if isinstance(positions, list) else None


def build_nodes(sources: ProjectionSources, *, now: str,
                stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    """One view per node that has published telemetry, built ONLY from what that
    node published.

    M-NODE-READ-1 — WHAT WAS REMOVED AND WHY.

    This function used to read the local adapter kind, the local adapter's
    connection state, the local execution store's reconciliation posture, the
    local governed execution mode, the local authorization grant, the local
    scenario store and the local broker snapshot — and attach all of it to a
    view stamped `node-telemetry`, titled with the VPS node's instance id.

    None of that is node truth. It is the Control Tower describing itself in the
    node's voice, which is the same class of defect as a fixture balance: a
    confident statement whose stated source did not make it.

    Every local read is gone from the per-node branch. What remains is the
    node's own `cycle`, `runtime`, `engine`, `positions` and freshness
    decomposition, plus the node's own reported failure conditions.

    NOTE ON THE ABSENT BRANCH: when no node has published, ONE explicitly
    unavailable view is still returned rather than an empty tuple, because an
    empty list reads as "the fleet is fine and empty". That view now carries no
    borrowed local state either — it asserts nothing about any node.
    """
    entries = sources.node_entries() or []

    if not entries:
        fresh = freshness(now=now, source_at=None, available=False,
                          stale_after_s=stale_after_s,
                          detail="no node has published telemetry")
        return (NodeOperationalView(
            node_id="unknown",
            # Every field describing a node stays None: nothing has been
            # observed, and the tower's own adapter, connection, execution mode,
            # authorization and reconciliation posture are facts about the
            # TOWER. Presenting them here answered a question about a node that
            # has never reported, using evidence from somewhere else entirely.
            lifecycle_state=nodeprov.NODE_ABSENT,
            open_position_count=None, open_order_count=None,
            active_scenario_count=None,
            warnings=("node telemetry unavailable",),
            provenance=nodeprov.PROV_ABSENT, freshness=fresh),)

    out = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        snapshot = entry.get("snapshot")
        if not isinstance(snapshot, dict):
            # A stored record whose snapshot cannot be read is not a node
            # observation. Skipping it is right: `_node_observation_entries`
            # only yields records that already passed validation, so this is
            # defence in depth, and inventing a view here would be worse than
            # the node appearing absent.
            continue
        node_id = entry.get("instance_id") or "unknown"
        engine = snapshot.get("engine") if isinstance(snapshot.get("engine"), dict) else {}
        runtime = snapshot.get("runtime") if isinstance(snapshot.get("runtime"), dict) else {}
        cycle = snapshot.get("cycle") if isinstance(snapshot.get("cycle"), dict) else {}
        account = snapshot.get("account") if isinstance(snapshot.get("account"), dict) else {}
        identity = account.get("identity") if isinstance(account.get("identity"), dict) else {}

        # FRESHNESS IS NOT RECOMPUTED. `entry` is the canonical M-TEL-1
        # envelope, already judged on the tower's ARRIVAL clock with the
        # phase-aware budget. This projection passes the verdict through and
        # adds no opinion of its own.
        stale = bool(entry.get("stale", True))
        fresh = freshness(now=now, source_at=entry.get("received_at") or entry.get("published_at"),
                          available=True, stale_after_s=stale_after_s,
                          detail=f"node telemetry received from {node_id}")
        if stale and fresh.available and not fresh.stale:
            fresh = Freshness(projection_at=fresh.projection_at, source_at=fresh.source_at,
                              age_seconds=fresh.age_seconds, stale=True, available=True,
                              stale_after_seconds=fresh.stale_after_seconds,
                              detail=fresh.detail)

        reasons = nodeprov.degraded_reasons(snapshot)
        lifecycle = nodeprov.classify_lifecycle(snapshot, stale=stale)
        warnings = list(reasons)
        if stale:
            warnings.append("node telemetry stale — last reported, not current")
        if entry.get("legacy_source"):
            warnings.append("legacy payload — this node predates the versioned contract")

        out.append(NodeOperationalView(
            node_id=node_id, instance_id=entry.get("instance_id"),
            deployment=nodeprov.clip_detail(engine.get("symbol")),
            # The tower's adapter kind, broker snapshot, connection state,
            # execution mode, authorization grant, reconciliation posture and
            # scenario count are ALL facts about the tower. None of them
            # belongs on a node's card, so none of them is set.
            adapter=None, broker=None, connection_state=None,
            execution_mode=None, authorization_summary=None,
            reconciliation_state=None, active_scenario_count=None,
            # Account identity ONLY from the node's own snapshot. This
            # previously fell back to `(snap or {}).get("accountIdentity")` —
            # the Mac broker snapshot's account — so under the mock adapter the
            # fixture's account appeared, masked, on the node's card.
            account_fingerprint_masked=_mask_fingerprint(identity.get("fingerprint")),
            heartbeat_age_seconds=entry.get("liveness_age_seconds"),
            health=lifecycle,
            open_position_count=_node_position_count(snapshot),
            open_order_count=None,          # v1 carries no orders section
            last_activity=entry.get("published_at"),
            telemetry_age_seconds=entry.get("age_seconds"),
            warnings=tuple(warnings),

            lifecycle_state=lifecycle,
            degraded_reasons=reasons,
            deployment_profile=nodeprov.clip_detail(engine.get("deployment_profile")),
            strategy_family=nodeprov.clip_detail(engine.get("strategy_family")),
            engine_version=nodeprov.clip_detail(engine.get("engine_version_actual")),
            engine_version_expected=nodeprov.clip_detail(engine.get("engine_version_expected")),
            config_fingerprint=nodeprov.clip_detail(engine.get("config_fingerprint")),
            input_revision=nodeprov.clip_detail(engine.get("input_revision")),
            symbol=nodeprov.clip_detail(engine.get("symbol")),
            timeframe=nodeprov.clip_detail(engine.get("timeframe")),
            data_seam=nodeprov.clip_detail(engine.get("data_seam")),
            cycle_status=nodeprov.clip_detail(cycle.get("status")),
            cycle_sequence=cycle.get("sequence") if isinstance(cycle.get("sequence"), int)
                           and not isinstance(cycle.get("sequence"), bool) else None,
            last_boundary=nodeprov.clip_detail(cycle.get("last_boundary")),
            last_bar_time=nodeprov.clip_detail(cycle.get("last_bar_time")),
            cycle_note=nodeprov.clip_detail(cycle.get("note")),
            node_mode=nodeprov.clip_detail(runtime.get("mode")),
            submission_disabled=_bool_or_none(runtime.get("submission_disabled")),
            kill_switch_active=_bool_or_none(runtime.get("kill_switch_active")),
            mt5_observation=nodeprov.mt5_connection_observation(snapshot),
            published_at=entry.get("published_at"),
            received_at=entry.get("received_at"),
            liveness_age_seconds=entry.get("liveness_age_seconds"),
            liveness_stale=_bool_or_none(entry.get("liveness_stale")),
            data_stale=_bool_or_none(entry.get("data_stale")),
            freshness_basis=entry.get("freshness_basis"),
            stale_after_seconds=entry.get("stale_after_seconds"),
            legacy_source=bool(entry.get("legacy_source")),
            provenance=nodeprov.for_node_observation(validated=True),
            freshness=fresh))
    # Stable, total ordering by instance id: a render key never depends on
    # arrival order, and one node can never occupy another's position.
    out.sort(key=lambda n: n.node_id)
    return tuple(out)


def build_summary(sources: ProjectionSources, *, now: str,
                  stale_after_s: float = DEFAULT_STALE_AFTER_S) -> OperationalSummary:
    """The one whole-system projection. Deterministic and idempotent: it holds
    nothing between calls and derives every field from `sources` at `now`."""
    snap = sources.broker_snapshot()
    nodes = build_nodes(sources, now=now, stale_after_s=stale_after_s)
    accounts = build_accounts(sources, now=now, stale_after_s=stale_after_s)
    orders = build_orders(sources, now=now)
    positions = build_positions(sources, now=now)
    scenarios = build_scenarios(sources, now=now)
    latest = sources.latest_reconciliation()
    issues = tuple(
        {"class": i.get("class"), "entityId": i.get("entity_id"),
         "critical": bool(i.get("critical")), "detail": i.get("detail"),
         "resolved": bool(i.get("resolved"))}
        for i in ((latest or {}).get("items") or []) if not i.get("resolved"))
    operations = tuple(
        {"entityRef": l.get("entity_ref"), "intentId": l.get("intent_id"),
         "operation": l.get("operation"), "acquiredAt": l.get("acquired_at")}
        for l in (sources.entity_locks() or []))
    fresh = freshness(now=now, source_at=(snap or {}).get("at"),
                      available=isinstance(snap, dict), stale_after_s=stale_after_s,
                      detail=None if isinstance(snap, dict) else "broker snapshot unavailable")
    # M-NODE-READ-1 — TOWER WARNINGS ARE RAISED WHERE THEY BELONG.
    #
    # `warnings` was the union of NODE warnings, and `build_nodes` manufactured
    # the tower's own facts into node warnings so they would arrive here: the
    # local reconciliation posture and the local broker snapshot's absence were
    # both attributed to whichever node happened to be reporting.
    #
    # The facts are real and an operator needs them. They are simply not facts
    # about a node, so they are raised here, at the whole-system level, from the
    # tower's own sources — and the node warnings that remain are the node's own
    # words about itself.
    posture = sources.reconciliation_posture() or {}
    tower_warnings = []
    if not isinstance(snap, dict):
        tower_warnings.append("broker snapshot unavailable")
    if posture.get("criticalUnresolved"):
        tower_warnings.append("unresolved critical reconciliation")
    elif posture.get("stale"):
        tower_warnings.append("reconciliation stale")
    elif not posture:
        tower_warnings.append("reconciliation posture unavailable")
    # M-ACTIVATE-READINESS-1 — a disagreement between two GENUINE sources is a
    # fact about the whole system, not about either source, so it is raised here
    # alongside the other tower-level warnings. Derived from the projected
    # accounts, so it describes exactly what the operator is looking at.
    tower_warnings.extend(activation_policy.account_contradictions(accounts))
    warnings = tuple(sorted({w for n in nodes for w in n.warnings} | set(tower_warnings)))
    return OperationalSummary(
        projection_timestamp=str(now), nodes=nodes, accounts=accounts,
        active_orders=orders, open_positions=positions, active_scenarios=scenarios,
        reconciliation_issues=issues, active_operations=operations,
        warnings=warnings, freshness=fresh)


# ═════════════════════════════════════════════════════════════════════════════
# LIVE-5A — the LIVE RUNTIME projection.
#
# The dashboard's live view is built HERE, from the runtime supervisor's cached
# snapshot, because the projection owner is the only component allowed to decide
# what the UI sees. Two rules shape every model below:
#
#   1. Nothing is invented. A field the broker did not supply is None with an
#      availability that says so; zero is never a substitute for absent.
#   2. Nothing is recomputed downstream. Spread, ages and counts are derived
#      once, here, so React renders values rather than calculating them.
# ═════════════════════════════════════════════════════════════════════════════

#: Runtime states, re-exported so a consumer needs one import, not two.
RUNTIME_STARTING = "STARTING"
RUNTIME_CONNECTED = "CONNECTED"
RUNTIME_STALE = "STALE"
RUNTIME_DEGRADED = "DEGRADED"
RUNTIME_RECONNECTING = "RECONNECTING"
RUNTIME_STOPPED = "STOPPED"
RUNTIME_UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class BrokerRuntimeView:
    """The broker card. Every figure is broker-supplied or None."""
    connected: bool = False
    connection_state: str | None = None
    ping_ms: float | None = None
    server: str | None = None
    server_time: str | None = None
    account_fingerprint: str | None = None
    account_currency: str | None = None
    balance: float | None = None
    equity: float | None = None
    margin: float | None = None
    free_margin: float | None = None
    margin_level: float | None = None
    leverage: int | None = None
    adapter_kind: str | None = None
    execution_mode: str | None = None
    #: LIVE-5B operational visibility. `login` is ALREADY masked by the adapter
    #: (`login_masked`); `account_type` is demo/contest/real, and is the field an
    #: operator checks before enabling LIVE.
    login: str | None = None
    account_type: str | None = None
    broker_company: str | None = None
    gateway_latency_ms: float | None = None
    reconnect_count: int = 0
    last_heartbeat_at: str | None = None
    heartbeat_age_seconds: float | None = None
    availability: str = AVAILABILITY_UNAVAILABLE
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

    def as_dict(self) -> dict:
        return _sorted({
            "connected": self.connected,
            "connectionState": self.connection_state,
            "pingMs": self.ping_ms, "server": self.server,
            "serverTime": self.server_time,
            "accountFingerprint": self.account_fingerprint,
            "accountCurrency": self.account_currency,
            "balance": self.balance, "equity": self.equity,
            "margin": self.margin, "freeMargin": self.free_margin,
            "marginLevel": self.margin_level, "leverage": self.leverage,
            "adapterKind": self.adapter_kind,
            "executionMode": self.execution_mode,
            "login": self.login, "accountType": self.account_type,
            "brokerCompany": self.broker_company,
            "gatewayLatencyMs": self.gateway_latency_ms,
            "reconnectCount": self.reconnect_count,
            "lastHeartbeatAt": self.last_heartbeat_at,
            "heartbeatAgeSeconds": self.heartbeat_age_seconds,
            "availability": self.availability, "provenance": self.provenance,
            "freshness": self.freshness.as_dict() if self.freshness else None,
        })


@dataclass(frozen=True)
class MarketSymbolView:
    """One symbol's live market card."""
    symbol: str
    broker_symbol: str | None = None
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    spread: float | None = None
    quote_at: str | None = None
    quote_age_seconds: float | None = None
    candle_timeframe: str | None = None
    candle_closed_at: str | None = None
    candle_age_seconds: float | None = None
    candle_close: float | None = None
    live: bool = False
    availability: str = AVAILABILITY_UNAVAILABLE
    candle_availability: str = AVAILABILITY_UNAVAILABLE
    provenance: str = PROV_ABSENT

    def as_dict(self) -> dict:
        return _sorted({
            "symbol": self.symbol, "brokerSymbol": self.broker_symbol,
            "bid": self.bid, "ask": self.ask, "last": self.last,
            "spread": self.spread, "quoteAt": self.quote_at,
            "quoteAgeSeconds": self.quote_age_seconds,
            "candleTimeframe": self.candle_timeframe,
            "candleClosedAt": self.candle_closed_at,
            "candleAgeSeconds": self.candle_age_seconds,
            "candleClose": self.candle_close, "live": self.live,
            "availability": self.availability,
            "candleAvailability": self.candle_availability,
            "provenance": self.provenance,
        })


@dataclass(frozen=True)
class RuntimeStatusView:
    """The runtime card — PART 11's health, as the UI sees it."""
    state: str = RUNTIME_UNKNOWN
    projection_age_seconds: float | None = None
    broker_age_seconds: float | None = None
    last_tick_at: str | None = None
    last_success_at: str | None = None
    tick_count: int = 0
    consecutive_failures: int = 0
    interval_seconds: float | None = None
    running: bool = False
    reconnect_attempts: int = 0
    reconnect_successes: int = 0
    last_failure_at: str | None = None
    last_failure_detail: str | None = None
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return _sorted({
            "state": self.state,
            "projectionAgeSeconds": self.projection_age_seconds,
            "reconnectAttempts": self.reconnect_attempts,
            "reconnectSuccesses": self.reconnect_successes,
            "lastFailureAt": self.last_failure_at,
            "lastFailureDetail": self.last_failure_detail,
            "brokerAgeSeconds": self.broker_age_seconds,
            "lastTickAt": self.last_tick_at,
            "lastSuccessAt": self.last_success_at,
            "tickCount": self.tick_count,
            "consecutiveFailures": self.consecutive_failures,
            "intervalSeconds": self.interval_seconds, "running": self.running,
            "warnings": list(self.warnings),
        })


@dataclass(frozen=True)
class ExecutionRuntimeView:
    """The execution card: how much live work is in flight right now."""
    active_positions: int = 0
    pending_orders: int = 0
    open_recommendations: int = 0
    active_scenarios: int = 0
    positions_available: bool = False
    recommendations_available: bool = False
    scenarios_available: bool = False

    def as_dict(self) -> dict:
        return _sorted({
            "activePositions": self.active_positions,
            "pendingOrders": self.pending_orders,
            "openRecommendations": self.open_recommendations,
            "activeScenarios": self.active_scenarios,
            "positionsAvailable": self.positions_available,
            "recommendationsAvailable": self.recommendations_available,
            "scenariosAvailable": self.scenarios_available,
        })


@dataclass(frozen=True)
class LiveRuntimeView:
    """The whole live dashboard in one immutable projection.

    ONE API response backs every live card, so the browser makes one request per
    cycle instead of fourteen, and every card on screen is guaranteed to describe
    the same instant.
    """
    projection_timestamp: str
    runtime: RuntimeStatusView = field(default_factory=RuntimeStatusView)
    broker: BrokerRuntimeView = field(default_factory=BrokerRuntimeView)
    symbols: tuple = field(default_factory=tuple)
    execution: ExecutionRuntimeView = field(default_factory=ExecutionRuntimeView)
    available: bool = False
    code: str | None = None
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return _sorted({
            "projectionTimestamp": self.projection_timestamp,
            "runtime": self.runtime.as_dict(), "broker": self.broker.as_dict(),
            "symbols": [s.as_dict() for s in self.symbols],
            "execution": self.execution.as_dict(),
            "available": self.available, "code": self.code,
            "warnings": list(self.warnings),
        })


def build_live_runtime(snapshot=None, *, now: str, health=None,
                       execution_mode: str | None = None,
                       recommendation_totals=None, scenario_count=None,
                       adapter_kind: str | None = None) -> LiveRuntimeView:
    """Project the runtime supervisor's cached snapshot for the UI.

    `snapshot is None` means the supervisor has not produced a tick yet, which is
    reported as UNAVAILABLE — never as a healthy runtime with zero everything.
    Performs NO broker read and NO recomputation of anything the snapshot already
    measured.
    """
    if snapshot is None:
        return LiveRuntimeView(
            projection_timestamp=str(now), available=False,
            code="runtime_not_started",
            runtime=RuntimeStatusView(
                state=(health.state if health else RUNTIME_STARTING),
                running=bool(health.running) if health else False,
                warnings=("runtime has produced no tick",)),
            warnings=("runtime has produced no tick",))

    market = snapshot.market
    heartbeat = market.heartbeat if market is not None else None
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    resolved_health = health if health is not None else snapshot.health

    heartbeat_age = (heartbeat.age_seconds(now) if heartbeat else None)
    connected = bool(heartbeat and heartbeat.connection == "Connected")
    broker_availability = (heartbeat.availability(now) if heartbeat
                           else AVAILABILITY_UNAVAILABLE)
    # M-MT5-READ-1 — the SECOND instance of the configuration-provenance defect,
    # found by the guard written for the first. This surface already refused to
    # stamp an origin with no evidence (`provenance if (account or connected)`),
    # but it made that judgement with its own inline `kind == "mt5"` comparison
    # sitting beside it. Two copies of a rule are two chances to fix only one of
    # them, so the evidence is now named once and the decision made in the seam.
    #
    # The evidence: the supervisor's tick carried an account, or the heartbeat
    # says the broker is connected. A tick that produced neither observed no
    # broker, however healthy the supervisor loop itself is.
    observed_broker = bool(account or connected)
    provenance = _provenance_for(adapter_kind or snapshot_adapter(snapshot),
                                 observed=observed_broker)

    broker = BrokerRuntimeView(
        connected=connected,
        connection_state=(heartbeat.connection if heartbeat else None),
        ping_ms=(heartbeat.latency_ms if heartbeat else None),
        server=account.get("server"),
        server_time=snapshot.server_time,
        account_fingerprint=_mask_fingerprint(account.get("accountFingerprint")),
        account_currency=account.get("currency"),
        balance=_num(account.get("balance")), equity=_num(account.get("equity")),
        margin=_num(account.get("margin")),
        free_margin=_num(account.get("marginFree")
                         if "marginFree" in account else account.get("margin_free")),
        margin_level=_num(account.get("marginLevel")
                          if "marginLevel" in account else account.get("margin_level")),
        leverage=_int(account.get("leverage")),
        adapter_kind=(adapter_kind or snapshot_adapter(snapshot)),
        execution_mode=execution_mode,
        login=account.get("login_masked") or account.get("accountId"),
        account_type=account.get("trade_mode"),
        broker_company=account.get("broker_company"),
        gateway_latency_ms=(heartbeat.latency_ms if heartbeat else None),
        reconnect_count=int(getattr(resolved_health, "reconnect_attempts", 0) or 0),
        last_heartbeat_at=(heartbeat.at if heartbeat else None),
        heartbeat_age_seconds=heartbeat_age,
        availability=(broker_availability if observed_broker
                      else AVAILABILITY_UNAVAILABLE),
        # Already `absent` when nothing was observed — the seam decided that
        # above, so there is no second gate here to fall out of step with it.
        provenance=provenance,
        freshness=freshness(now=now,
                            source_at=(heartbeat.at if heartbeat else None),
                            available=bool(heartbeat and heartbeat.at)))

    symbols = []
    for sub in (market.subscriptions if market else ()):
        quote, candle = sub.quote, sub.candle
        symbols.append(MarketSymbolView(
            symbol=sub.symbol, broker_symbol=sub.broker_symbol,
            bid=(quote.bid if quote else None),
            ask=(quote.ask if quote else None),
            last=(quote.last if quote else None),
            spread=(quote.spread if quote else None),
            quote_at=(quote.at if quote else None),
            quote_age_seconds=(quote.age_seconds(now) if quote else None),
            candle_timeframe=(candle.timeframe if candle else None),
            candle_closed_at=(candle.closed_at if candle else None),
            candle_age_seconds=(candle.age_seconds(now) if candle else None),
            candle_close=(candle.close if candle else None),
            live=bool(sub.active),
            availability=(quote.availability(now) if quote
                          else AVAILABILITY_UNAVAILABLE),
            candle_availability=(candle.availability(now) if candle
                                 else AVAILABILITY_UNAVAILABLE),
            provenance=((quote.provider if quote and quote.provider else None)
                        or PROV_ABSENT)))

    raw = snapshot.broker_snapshot if isinstance(snapshot.broker_snapshot, dict) else None
    positions = (raw or {}).get("positions")
    orders = (raw or {}).get("orders")
    totals = recommendation_totals if isinstance(recommendation_totals, dict) else None
    execution = ExecutionRuntimeView(
        active_positions=len(positions or ()),
        pending_orders=len(orders or ()),
        open_recommendations=int((totals or {}).get("activeCount") or 0),
        active_scenarios=int(scenario_count or 0),
        positions_available=raw is not None,
        recommendations_available=totals is not None,
        scenarios_available=scenario_count is not None)

    runtime = RuntimeStatusView(
        state=resolved_health.state,
        projection_age_seconds=resolved_health.projection_age_seconds,
        broker_age_seconds=(resolved_health.broker_age_seconds
                            if resolved_health.broker_age_seconds is not None
                            else heartbeat_age),
        last_tick_at=resolved_health.last_tick_at,
        last_success_at=resolved_health.last_success_at,
        tick_count=resolved_health.tick_count,
        consecutive_failures=resolved_health.consecutive_failures,
        interval_seconds=resolved_health.interval_s,
        running=resolved_health.running,
        reconnect_attempts=getattr(resolved_health, "reconnect_attempts", 0),
        reconnect_successes=getattr(resolved_health, "reconnect_successes", 0),
        last_failure_at=getattr(resolved_health, "last_failure_at", None),
        last_failure_detail=getattr(resolved_health, "last_failure_detail", None),
        warnings=tuple(resolved_health.warnings))

    return LiveRuntimeView(
        projection_timestamp=str(now), runtime=runtime, broker=broker,
        symbols=tuple(symbols), execution=execution, available=True,
        warnings=tuple(snapshot.warnings))


def snapshot_adapter(snapshot) -> str | None:
    market = getattr(snapshot, "market", None)
    return getattr(market, "adapter_kind", None) if market is not None else None


def _num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
