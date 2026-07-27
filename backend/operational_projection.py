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

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

SCHEMA_VERSION = "ct.operational-projection.v1"

# ── provenance vocabulary (where a fact came from) ────────────────────────────
PROV_LIVE_MT5 = "live_mt5"
PROV_MOCK_FIXTURE = "mock-fixture"
PROV_NODE_TELEMETRY = "node-telemetry"
PROV_DURABLE_STORE = "durable-store"
PROV_ABSENT = "absent"

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
    provenance: str = PROV_ABSENT
    freshness: Freshness | None = None

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
            "connectionState": self.connection_state,
            "provenance": self.provenance,
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


def _provenance_for(adapter_kind: str) -> str:
    return PROV_LIVE_MT5 if adapter_kind == "mt5" else PROV_MOCK_FIXTURE


# ─────────────────────────────────────────────────────────────────────────────
# Builders — pure, deterministic, injected clock.
# ─────────────────────────────────────────────────────────────────────────────

def build_accounts(sources: ProjectionSources, *, now: str,
                   stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    kind = sources.adapter_kind()
    prov = _provenance_for(kind)
    conn = sources.connection_state()
    acct = sources.account_snapshot()
    snap = sources.broker_snapshot()
    snap_at = (snap or {}).get("at")
    if not isinstance(acct, dict):
        # No dedicated account snapshot. Fall back to the accounts the broker
        # snapshot itself reports (real evidence, e.g. the fixture world's
        # accounts) — still derived, never invented. If neither exists, the view
        # is explicitly UNAVAILABLE: no zeroed balances, no invented account.
        listed = (snap or {}).get("accounts") or []
        first = listed[0] if isinstance(listed, list) and listed else None
        if isinstance(first, dict):
            return (AccountOperationalView(
                account_fingerprint=first.get("accountId") or (snap or {}).get("accountIdentity"),
                broker=first.get("brokerId") or first.get("broker"),
                server=None,
                balance=first.get("balance"), equity=first.get("equity"),
                margin=None, margin_level=None, leverage=None,
                currency=first.get("baseCurrency") or first.get("currency"),
                unrealized_pnl=None, realized_pnl_today=None, open_risk=None,
                connection_state=conn, provenance=prov,
                freshness=freshness(now=now, source_at=snap_at, available=True,
                                    stale_after_s=stale_after_s,
                                    detail="derived from broker snapshot accounts")),)
        return (AccountOperationalView(
            connection_state=conn, provenance=prov,
            freshness=freshness(now=now, source_at=snap_at, available=False,
                                stale_after_s=stale_after_s,
                                detail="account snapshot unavailable")),)
    unrealized = None
    if isinstance(snap, dict):
        pnls = [_entity_view(p).get("pnl") for p in (snap.get("positions") or [])]
        numeric = [p for p in pnls if isinstance(p, (int, float)) and not isinstance(p, bool)]
        unrealized = round(sum(numeric), 2) if numeric else None
    return (AccountOperationalView(
        account_fingerprint=acct.get("fingerprint") or (snap or {}).get("accountIdentity"),
        broker=acct.get("broker_company"),
        server=acct.get("server"),
        balance=acct.get("balance"), equity=acct.get("equity"),
        margin=acct.get("margin"), margin_level=acct.get("margin_level"),
        leverage=acct.get("leverage"), currency=acct.get("currency"),
        unrealized_pnl=unrealized,
        # Not derivable from current evidence — reported as absent, not zero.
        realized_pnl_today=None, open_risk=None,
        connection_state=conn, provenance=prov,
        freshness=freshness(now=now, source_at=acct.get("at") or snap_at,
                            available=True, stale_after_s=stale_after_s)),)


def build_orders(sources: ProjectionSources, *, now: str) -> tuple:
    """Active ORDERS: tower intents still in flight, joined to the broker's own
    working orders where a reference matches. Orders and positions stay
    separate concepts — a filled market order becomes a POSITION, not an order."""
    kind = sources.adapter_kind()
    prov = _provenance_for(kind)
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
    prov = _provenance_for(kind)
    snap = sources.broker_snapshot()
    if not isinstance(snap, dict):
        return ()                              # unavailable -> no invented positions
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


def build_nodes(sources: ProjectionSources, *, now: str,
                stale_after_s: float = DEFAULT_STALE_AFTER_S) -> tuple:
    """One view per node that has published telemetry. When no node has
    published, a single explicitly-UNAVAILABLE view is returned rather than an
    empty list that could read as 'all healthy'."""
    kind = sources.adapter_kind()
    prov_broker = _provenance_for(kind)
    snap = sources.broker_snapshot()
    posture = sources.reconciliation_posture() or {}
    recon_state = ("unavailable" if not posture else
                   "critical" if posture.get("criticalUnresolved") else
                   "stale" if posture.get("stale") else "clean")
    mode = sources.execution_mode()
    auth = sources.authorization()
    orders = build_orders(sources, now=now)
    positions = build_positions(sources, now=now)
    scenarios = build_scenarios(sources, now=now)
    entries = sources.node_entries() or []

    def _warnings(fresh: Freshness, health: str | None) -> tuple:
        w = []
        if not fresh.available:
            w.append("node telemetry unavailable")
        elif fresh.stale:
            w.append("node telemetry stale")
        if recon_state == "critical":
            w.append("unresolved critical reconciliation")
        elif recon_state == "stale":
            w.append("reconciliation stale")
        if health and health not in ("healthy",):
            w.append(f"node health: {health}")
        if not isinstance(snap, dict):
            w.append("broker snapshot unavailable")
        return tuple(w)

    if not entries:
        fresh = freshness(now=now, source_at=None, available=False,
                          stale_after_s=stale_after_s,
                          detail="no node has published telemetry")
        return (NodeOperationalView(
            node_id="unknown", adapter=kind, broker=(snap or {}).get("provenance"),
            connection_state=sources.connection_state(),
            health=None, execution_mode=mode,
            authorization_summary=auth, reconciliation_state=recon_state,
            open_position_count=len(positions), open_order_count=len(orders),
            active_scenario_count=len(scenarios),
            account_fingerprint_masked=_mask_fingerprint((snap or {}).get("accountIdentity")),
            warnings=_warnings(fresh, None),
            provenance=PROV_ABSENT, freshness=fresh),)

    out = []
    for entry in entries:
        snapshot = entry.get("snapshot") or {}
        node_id = entry.get("instance_id") or "unknown"
        published = entry.get("published_at")
        fresh = freshness(now=now, source_at=published, available=True,
                          stale_after_s=stale_after_s)
        health = "healthy" if not entry.get("stale", True) else "stale"
        account = (snapshot.get("account") or {})
        identity = (account.get("identity") or {})
        engine = (snapshot.get("engine") or {})
        runtime = (snapshot.get("runtime") or {})
        out.append(NodeOperationalView(
            node_id=node_id, instance_id=entry.get("instance_id"),
            deployment=engine.get("symbol"),
            adapter=kind, broker=(snap or {}).get("provenance") or prov_broker,
            account_fingerprint_masked=_mask_fingerprint(
                identity.get("fingerprint") or (snap or {}).get("accountIdentity")),
            connection_state=sources.connection_state(),
            heartbeat_age_seconds=entry.get("age_seconds"),
            health=health, execution_mode=mode, authorization_summary=auth,
            reconciliation_state=recon_state,
            open_position_count=len(positions), open_order_count=len(orders),
            active_scenario_count=len(scenarios),
            last_activity=published,
            telemetry_age_seconds=entry.get("age_seconds"),
            warnings=_warnings(fresh, None if health == "healthy" else health)
                     + (("node reports mode " + str(runtime.get("mode")),)
                        if runtime.get("mode") else ()),
            provenance=PROV_NODE_TELEMETRY, freshness=fresh))
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
    warnings = tuple(sorted({w for n in nodes for w in n.warnings}))
    return OperationalSummary(
        projection_timestamp=str(now), nodes=nodes, accounts=accounts,
        active_orders=orders, open_positions=positions, active_scenarios=scenarios,
        reconciliation_issues=issues, active_operations=operations,
        warnings=warnings, freshness=fresh)
