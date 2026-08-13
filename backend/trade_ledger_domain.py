"""LIVE-4C — the canonical TRADE LEDGER domain.

The ledger records what ACTUALLY HAPPENED to a trade: the economic result.

    Scenario -> Recommendation -> Intent -> Order -> Position -> Deals
             -> Closed Trade -> Trade Ledger Entry

RESPONSIBILITY BOUNDARY
    Execution store  — command and execution lifecycle facts.
    Scenario store   — why the opportunity existed.
    Operational projection — current operational state.
    Trade ledger     — the final historical economic result.

    The ledger is NEVER an execution authority. Nothing here authorizes,
    routes, submits or mutates execution or scenario state.

VALUE SEMANTICS — the central discipline of this module
    A missing number is NEVER zero. Every financial, risk and quality group
    carries an explicit completeness marker, and each individual value is
    `None` when unavailable. `0.0` means "measured, and it was zero".

        COMPLETE      every component present
        PARTIAL       some components present, some missing
        PENDING       expected to arrive later (costs settle after close)
        UNAVAILABLE   the broker cannot supply it at all
        NOT_APPLICABLE  meaningless for this trade
        CONFLICTED    sources disagree
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "ct.trade-ledger.v1"
TRADE_PREFIX = "trd_"

# ── completeness vocabulary ───────────────────────────────────────────────────
COMPLETE = "complete"
PARTIAL = "partial"
PENDING = "pending"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"
CONFLICTED = "conflicted"

# ── provenance ────────────────────────────────────────────────────────────────
PROV_LIVE_MT5 = "live_mt5"
PROV_MOCK_FIXTURE = "mock-fixture"
PROV_DURABLE_STORE = "durable-store"
PROV_ABSENT = "absent"


class TradeLedgerError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _sorted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sorted(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_sorted(v) for v in obj]
    return obj


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


# ── identity ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeId:
    """A deterministic trade identity.

    Derived ONLY from immutable broker lineage — account fingerprint, broker
    position id, instrument and the opening deal id. Financial values are
    deliberately excluded, so identity survives:
      * restart and ledger rebuild,
      * reconciliation reruns,
      * LATE-ARRIVING COST EVIDENCE (which changes money, not identity),
      * repeated broker-history reads.
    """
    value: str

    def __post_init__(self):
        if not re.fullmatch(rf"{TRADE_PREFIX}[0-9a-f]{{16}}", str(self.value)):
            raise TradeLedgerError("invalid_trade_id",
                                   "trade id must be trd_ + 16 lowercase hex chars")

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def derive(*, account_fingerprint: str | None, position_id: str,
               instrument: str | None, opening_deal_id: str | None = None) -> "TradeId":
        if not (isinstance(position_id, str) and position_id.strip()):
            raise TradeLedgerError("invalid_lineage", "position id is required")
        natural = "|".join((
            str(account_fingerprint or "unknown-account").strip(),
            position_id.strip(),
            str(instrument or "unknown-instrument").strip(),
            str(opening_deal_id or "").strip(),
        ))
        return TradeId(f"{TRADE_PREFIX}{hashlib.sha256(natural.encode()).hexdigest()[:16]}")


# ── lifecycle ─────────────────────────────────────────────────────────────────

class TradeLedgerStatus:
    OBSERVED = "OBSERVED"
    RECONSTRUCTING = "RECONSTRUCTING"
    INCOMPLETE = "INCOMPLETE"
    READY_TO_FINALIZE = "READY_TO_FINALIZE"
    FINALIZED = "FINALIZED"
    AMENDED = "AMENDED"
    CONFLICTED = "CONFLICTED"


ALL_STATUSES = frozenset({
    TradeLedgerStatus.OBSERVED, TradeLedgerStatus.RECONSTRUCTING,
    TradeLedgerStatus.INCOMPLETE, TradeLedgerStatus.READY_TO_FINALIZE,
    TradeLedgerStatus.FINALIZED, TradeLedgerStatus.AMENDED,
    TradeLedgerStatus.CONFLICTED,
})

#: FINALIZED and AMENDED are "settled truth". They are never silently
#: overwritten: the ONLY way out of them is a recorded amendment (-> AMENDED)
#: or discovered conflicting evidence (-> CONFLICTED).
_TRANSITIONS: dict[str, tuple] = {
    TradeLedgerStatus.OBSERVED: (TradeLedgerStatus.RECONSTRUCTING,
                                 TradeLedgerStatus.INCOMPLETE,
                                 TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.RECONSTRUCTING: (TradeLedgerStatus.INCOMPLETE,
                                       TradeLedgerStatus.READY_TO_FINALIZE,
                                       TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.INCOMPLETE: (TradeLedgerStatus.RECONSTRUCTING,
                                   TradeLedgerStatus.READY_TO_FINALIZE,
                                   TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.READY_TO_FINALIZE: (TradeLedgerStatus.FINALIZED,
                                          TradeLedgerStatus.INCOMPLETE,
                                          TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.FINALIZED: (TradeLedgerStatus.AMENDED,
                                  TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.AMENDED: (TradeLedgerStatus.AMENDED,
                                TradeLedgerStatus.CONFLICTED),
    TradeLedgerStatus.CONFLICTED: (TradeLedgerStatus.RECONSTRUCTING,
                                   TradeLedgerStatus.INCOMPLETE,
                                   TradeLedgerStatus.READY_TO_FINALIZE),
}

ALLOWED_TRANSITIONS = frozenset(
    (src, dst) for src, targets in _TRANSITIONS.items() for dst in targets)


def allowed_transitions(status: str) -> frozenset:
    return frozenset(_TRANSITIONS.get(status, ()))


def can_transition(src: str, dst: str) -> bool:
    return (src, dst) in ALLOWED_TRANSITIONS


# ── outcomes and classification ───────────────────────────────────────────────

class TradeOutcome:
    WIN = "WIN"
    LOSS = "LOSS"
    BREAK_EVEN = "BREAK_EVEN"
    UNAVAILABLE = "UNAVAILABLE"          # PnL evidence absent — never guessed

    @staticmethod
    def of(net_or_gross: float | None) -> str:
        if net_or_gross is None:
            return TradeOutcome.UNAVAILABLE
        if net_or_gross > 0:
            return TradeOutcome.WIN
        if net_or_gross < 0:
            return TradeOutcome.LOSS
        return TradeOutcome.BREAK_EVEN


class ExitClassification:
    TAKE_PROFIT = "TAKE_PROFIT"
    STOP_LOSS = "STOP_LOSS"
    BREAK_EVEN = "BREAK_EVEN"
    MANUAL_CLOSE = "MANUAL_CLOSE"
    PARTIAL_CLOSE = "PARTIAL_CLOSE"
    STRATEGY_EXIT = "STRATEGY_EXIT"
    RISK_REDUCTION = "RISK_REDUCTION"
    BROKER_CLOSE = "BROKER_CLOSE"
    MARGIN_CLOSE = "MARGIN_CLOSE"
    ACCOUNT_CLOSEOUT = "ACCOUNT_CLOSEOUT"
    UNKNOWN = "UNKNOWN"


#: MT5 `DEAL_REASON_*`. Explicit broker evidence ALWAYS wins over price
#: comparison — a broker that tells us why never gets second-guessed.
BROKER_REASON_CLASSIFICATION = {
    "3": ExitClassification.STOP_LOSS,        # DEAL_REASON_SL
    "4": ExitClassification.TAKE_PROFIT,      # DEAL_REASON_TP
    "5": ExitClassification.MARGIN_CLOSE,     # DEAL_REASON_SO (stop out)
    "0": ExitClassification.MANUAL_CLOSE,     # DEAL_REASON_CLIENT
    "1": ExitClassification.MANUAL_CLOSE,     # DEAL_REASON_MOBILE
    "2": ExitClassification.MANUAL_CLOSE,     # DEAL_REASON_WEB
    "6": ExitClassification.STRATEGY_EXIT,    # DEAL_REASON_ROLLOVER
    "7": ExitClassification.ACCOUNT_CLOSEOUT,  # DEAL_REASON_VMARGIN
    "8": ExitClassification.BROKER_CLOSE,     # DEAL_REASON_SPLIT
}

#: Price-comparison tolerance in INSTRUMENT POINTS. Deterministic and
#: documented: an exit within this many points of the recorded protective level
#: is classified as that level. It is only ever consulted when the broker gave
#: no explicit reason.
DEFAULT_TOLERANCE_POINTS = 5.0
DEFAULT_POINT_SIZE = 0.0001              # FX 4th-decimal default


def classify_exit(*, broker_reason: str | None, exit_price: float | None,
                  stop_loss: float | None, take_profit: float | None,
                  entry_price: float | None = None,
                  fully_closed: bool = True,
                  point_size: float = DEFAULT_POINT_SIZE,
                  tolerance_points: float = DEFAULT_TOLERANCE_POINTS) -> str:
    """Deterministic exit classification from EXPLICIT evidence.

    Order of authority:
      1. an explicit broker reason (never overridden);
      2. proximity to a recorded protective level, within a documented
         instrument-precision tolerance;
      3. a partial close, which is a fact about quantity, not price;
      4. UNKNOWN — preferred over invented certainty.
    """
    if broker_reason is not None:
        mapped = BROKER_REASON_CLASSIFICATION.get(str(broker_reason).strip())
        if mapped is not None:
            return mapped
    if not fully_closed:
        return ExitClassification.PARTIAL_CLOSE
    if exit_price is None:
        return ExitClassification.UNKNOWN
    tolerance = abs(point_size * tolerance_points)
    if take_profit is not None and abs(exit_price - take_profit) <= tolerance:
        return ExitClassification.TAKE_PROFIT
    if stop_loss is not None and abs(exit_price - stop_loss) <= tolerance:
        # A stop sitting at (or through) entry is a break-even exit.
        if entry_price is not None and abs(stop_loss - entry_price) <= tolerance:
            return ExitClassification.BREAK_EVEN
        return ExitClassification.STOP_LOSS
    if entry_price is not None and abs(exit_price - entry_price) <= tolerance:
        return ExitClassification.BREAK_EVEN
    return ExitClassification.UNKNOWN


# ── the three independent origin axes (M-LEDGER-ORIGIN-1) ────────────────────
#
# These answer DIFFERENT questions and must never be collapsed:
#
#   TradeOrigin      "who caused this trade?"        CONTROL_TOWER | MANUAL_BROKER | ...
#   ExecutionOrigin  "which adapter produced it?"    mt5 | mock | unknown
#   storage class    "where is it kept?"             durable-store  (response metadata)
#   reconciliation   "how was it reconstructed?"     its own status fields
#
# The combination that matters: a CONTROL_TOWER-initiated trade executed by the
# MOCK adapter is NOT broker history, however genuine its initiation was. Before
# this contract existed a consumer could only see `provenance: "durable-store"`,
# which describes persistence and grants no authority whatsoever.


class ExecutionOrigin:
    """Which execution authority produced or observed the record.

    Derived from `TradeLineage.adapter`, which the reconstruction pipeline
    already carries (`server.py` passes `broker_adapter.active_kind()`). The
    vocabulary is exactly the adapter registry — `broker_adapter.known_kinds()`
    is `("mock", "mt5")` — plus an explicit UNKNOWN.

    UNKNOWN is a deliberate state, never a convenience default. It means the
    adapter was not recorded, and it FAILS CLOSED: no consumer may admit it, and
    nothing may promote it to MT5 on the strength of broker-looking fields.
    """

    MT5 = "mt5"
    MOCK = "mock"
    UNKNOWN = "unknown"

    #: Every value this system may persist. Anything else normalises to UNKNOWN.
    KNOWN = frozenset({MT5, MOCK, UNKNOWN})

    @classmethod
    def normalize(cls, adapter: str | None) -> str:
        """Map a recorded adapter kind to a canonical execution origin.

        Fails closed by construction: an absent, blank, unrecognised or
        wrong-typed adapter becomes UNKNOWN. There is deliberately no branch
        that can produce MT5 from anything other than the literal adapter kind.
        """
        if not isinstance(adapter, str):
            return cls.UNKNOWN
        value = adapter.strip().lower()
        return value if value in cls.KNOWN and value != cls.UNKNOWN else (
            value if value in (cls.MT5, cls.MOCK) else cls.UNKNOWN)


class TradeOrigin:
    CONTROL_TOWER = "CONTROL_TOWER"
    MANUAL_BROKER = "MANUAL_BROKER"
    EXTERNAL_SYSTEM = "EXTERNAL_SYSTEM"
    LEGACY_IMPORT = "LEGACY_IMPORT"
    UNKNOWN = "UNKNOWN"


# ── accounting summaries ──────────────────────────────────────────────────────

@dataclass(frozen=True)
class TradeCostSummary:
    """Realized costs. `completeness` distinguishes measured-zero from absent."""
    commission: float | None = None
    fees: float | None = None
    swap: float | None = None
    other_costs: float | None = None
    completeness: str = UNAVAILABLE

    @property
    def total(self) -> float | None:
        parts = [p for p in (self.commission, self.fees, self.swap, self.other_costs)
                 if p is not None]
        return round(sum(parts), 6) if parts else None

    def as_dict(self) -> dict:
        return _sorted({"commission": self.commission, "fees": self.fees,
                        "swap": self.swap, "otherCosts": self.other_costs,
                        "totalCosts": self.total, "costCompleteness": self.completeness})


@dataclass(frozen=True)
class TradeRiskSummary:
    """Initial-risk evidence and realized R.

    POLICY (canonical, tested): realized R is computed from **GROSS** realized
    PnL divided by the initial risk amount. Gross is used because cost evidence
    frequently arrives late or not at all, and an R that silently changes when
    a swap settles would not be comparable. `realized_r_basis` states this on
    every record."""
    initial_stop_price: float | None = None
    initial_entry_price: float | None = None
    initial_risk_amount: float | None = None
    planned_r: float | None = None
    realized_r: float | None = None
    realized_r_basis: str = "gross"
    completeness: str = UNAVAILABLE

    def as_dict(self) -> dict:
        return _sorted({"initialStopPrice": self.initial_stop_price,
                        "initialEntryPrice": self.initial_entry_price,
                        "initialRiskAmount": self.initial_risk_amount,
                        "plannedR": self.planned_r, "realizedR": self.realized_r,
                        "realizedRBasis": self.realized_r_basis,
                        "riskCompleteness": self.completeness})


@dataclass(frozen=True)
class TradeTimingSummary:
    opened_at: str | None = None
    closed_at: str | None = None
    duration_seconds: float | None = None
    completeness: str = UNAVAILABLE

    @staticmethod
    def of(opened_at: str | None, closed_at: str | None) -> "TradeTimingSummary":
        start, end = _parse(opened_at), _parse(closed_at)
        if start is None and end is None:
            return TradeTimingSummary(completeness=UNAVAILABLE)
        if start is None or end is None:
            return TradeTimingSummary(opened_at=opened_at, closed_at=closed_at,
                                      completeness=PARTIAL)
        return TradeTimingSummary(opened_at=opened_at, closed_at=closed_at,
                                  duration_seconds=round((end - start).total_seconds(), 3),
                                  completeness=COMPLETE)

    def as_dict(self) -> dict:
        return _sorted({"openedAt": self.opened_at, "closedAt": self.closed_at,
                        "durationSeconds": self.duration_seconds,
                        "timingCompleteness": self.completeness})


@dataclass(frozen=True)
class TradeExecutionQuality:
    requested_entry_price: float | None = None
    average_fill_price: float | None = None
    entry_slippage: float | None = None
    requested_exit_price: float | None = None
    exit_slippage: float | None = None
    completeness: str = UNAVAILABLE

    def as_dict(self) -> dict:
        return _sorted({"requestedEntryPrice": self.requested_entry_price,
                        "averageFillPrice": self.average_fill_price,
                        "entrySlippage": self.entry_slippage,
                        "requestedExitPrice": self.requested_exit_price,
                        "exitSlippage": self.exit_slippage,
                        "executionQualityCompleteness": self.completeness})


@dataclass(frozen=True)
class TradeLineage:
    """Explicit lineage. Every relationship is a recorded identifier — the UI
    never infers, string-matches or reconstructs a relationship."""
    trade_id: str
    scenario_id: str | None = None
    recommendation_id: str | None = None
    intent_ids: tuple = field(default_factory=tuple)
    internal_operation_ids: tuple = field(default_factory=tuple)
    broker_order_ids: tuple = field(default_factory=tuple)
    broker_position_ids: tuple = field(default_factory=tuple)
    broker_deal_ids: tuple = field(default_factory=tuple)
    node_id: str | None = None
    account_fingerprint: str | None = None
    broker: str | None = None
    adapter: str | None = None
    deployment: str | None = None
    instrument: str | None = None
    origin: str = TradeOrigin.UNKNOWN
    scenario_completeness: str = UNAVAILABLE

    def as_dict(self) -> dict:
        return _sorted({
            "tradeId": self.trade_id, "scenarioId": self.scenario_id,
            "recommendationId": self.recommendation_id,
            "intentIds": list(self.intent_ids),
            "internalOperationIds": list(self.internal_operation_ids),
            "brokerOrderIds": list(self.broker_order_ids),
            "brokerPositionIds": list(self.broker_position_ids),
            "brokerDealIds": list(self.broker_deal_ids),
            "nodeId": self.node_id, "accountFingerprint": self.account_fingerprint,
            "broker": self.broker, "adapter": self.adapter,
            "deployment": self.deployment, "instrument": self.instrument,
            "origin": self.origin, "scenarioCompleteness": self.scenario_completeness,
        })


@dataclass(frozen=True)
class ClosedTrade:
    """The reconstructed economic facts of one trade. Immutable."""
    trade_id: str
    instrument: str | None = None
    side: str | None = None
    total_entry_quantity: float | None = None
    total_exit_quantity: float | None = None
    average_entry_price: float | None = None
    average_exit_price: float | None = None
    account_currency: str | None = None
    gross_realized_pnl: float | None = None
    net_realized_pnl: float | None = None
    fully_closed: bool = False
    residual_quantity: float | None = None
    exit_classification: str = ExitClassification.UNKNOWN
    costs: TradeCostSummary = field(default_factory=TradeCostSummary)
    risk: TradeRiskSummary = field(default_factory=TradeRiskSummary)
    timing: TradeTimingSummary = field(default_factory=TradeTimingSummary)
    quality: TradeExecutionQuality = field(default_factory=TradeExecutionQuality)
    lineage: TradeLineage | None = None
    account_mode: str = "unknown"
    provenance: str = PROV_ABSENT

    @property
    def outcome(self) -> str:
        return TradeOutcome.of(self.net_realized_pnl
                               if self.net_realized_pnl is not None
                               else self.gross_realized_pnl)

    def as_dict(self) -> dict:
        return _sorted({
            "tradeId": self.trade_id, "instrument": self.instrument,
            "side": self.side,
            "totalEntryQuantity": self.total_entry_quantity,
            "totalExitQuantity": self.total_exit_quantity,
            "averageEntryPrice": self.average_entry_price,
            "averageExitPrice": self.average_exit_price,
            "accountCurrency": self.account_currency,
            "grossRealizedPnL": self.gross_realized_pnl,
            "netRealizedPnL": self.net_realized_pnl,
            "fullyClosed": self.fully_closed,
            "residualQuantity": self.residual_quantity,
            "exitClassification": self.exit_classification,
            "outcome": self.outcome,
            "accountMode": self.account_mode,
            **self.costs.as_dict(), **self.risk.as_dict(),
            **self.timing.as_dict(), **self.quality.as_dict(),
            "lineage": self.lineage.as_dict() if self.lineage else None,
            "provenance": self.provenance,
        })


@dataclass(frozen=True)
class TradeLedgerEntry:
    """One versioned ledger record. `version` increments on amendment; the
    previous version is never rewritten — it remains in the event history."""
    trade_id: str
    status: str = TradeLedgerStatus.OBSERVED
    version: int = 1
    trade: ClosedTrade | None = None
    warnings: tuple = field(default_factory=tuple)
    conflicts: tuple = field(default_factory=tuple)
    reconciliation_findings: tuple = field(default_factory=tuple)
    management_history: tuple = field(default_factory=tuple)
    observed_at: str | None = None
    finalized_at: str | None = None
    amended_at: str | None = None
    updated_at: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.status not in ALL_STATUSES:
            raise TradeLedgerError("unknown_status", str(self.status))
        if not re.fullmatch(rf"{TRADE_PREFIX}[0-9a-f]{{16}}", str(self.trade_id)):
            raise TradeLedgerError("invalid_trade_id", str(self.trade_id))

    @property
    def finalized(self) -> bool:
        return self.status in (TradeLedgerStatus.FINALIZED, TradeLedgerStatus.AMENDED)

    def as_dict(self) -> dict:
        return _sorted({
            "tradeId": self.trade_id, "status": self.status, "version": self.version,
            "finalized": self.finalized,
            "trade": self.trade.as_dict() if self.trade else None,
            "warnings": list(self.warnings), "conflicts": list(self.conflicts),
            "reconciliationFindings": [dict(sorted(f.items()))
                                       for f in self.reconciliation_findings],
            "managementHistory": [dict(sorted(m.items()))
                                  for m in self.management_history],
            "observedAt": self.observed_at, "finalizedAt": self.finalized_at,
            "amendedAt": self.amended_at, "updatedAt": self.updated_at,
            "schemaVersion": self.schema_version,
        })


# ── events ────────────────────────────────────────────────────────────────────

class LedgerEventType:
    BROKER_HISTORY_OBSERVED = "BrokerHistoryObserved"
    RECONSTRUCTION_STARTED = "TradeReconstructionStarted"
    EVIDENCE_LINKED = "TradeEvidenceLinked"
    MARKED_INCOMPLETE = "TradeMarkedIncomplete"
    READY_TO_FINALIZE = "TradeReadyToFinalize"
    FINALIZED = "TradeFinalized"
    COST_EVIDENCE_RECEIVED = "TradeCostEvidenceReceived"
    AMENDED = "TradeAmended"
    CONFLICT_DETECTED = "TradeConflictDetected"


ALL_EVENT_TYPES = frozenset(
    v for k, v in vars(LedgerEventType).items()
    if not k.startswith("_") and isinstance(v, str))

#: Which status an event records arrival at (used to rebuild deterministically).
EVENT_STATUS = {
    LedgerEventType.BROKER_HISTORY_OBSERVED: TradeLedgerStatus.OBSERVED,
    LedgerEventType.RECONSTRUCTION_STARTED: TradeLedgerStatus.RECONSTRUCTING,
    LedgerEventType.MARKED_INCOMPLETE: TradeLedgerStatus.INCOMPLETE,
    LedgerEventType.READY_TO_FINALIZE: TradeLedgerStatus.READY_TO_FINALIZE,
    LedgerEventType.FINALIZED: TradeLedgerStatus.FINALIZED,
    LedgerEventType.AMENDED: TradeLedgerStatus.AMENDED,
    LedgerEventType.CONFLICT_DETECTED: TradeLedgerStatus.CONFLICTED,
}


@dataclass(frozen=True)
class LedgerEvent:
    """One immutable, timestamped, serializable ledger fact."""
    event_id: str
    trade_id: str
    sequence: int
    event_type: str
    occurred_at: str
    recorded_at: str
    payload: dict = field(default_factory=dict)
    provenance: str = PROV_ABSENT
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.event_type not in ALL_EVENT_TYPES:
            raise TradeLedgerError("unknown_event_type", str(self.event_type))
        if _parse(self.occurred_at) is None or _parse(self.recorded_at) is None:
            raise TradeLedgerError("invalid_timestamp",
                                   f"{self.occurred_at} / {self.recorded_at}")
        try:
            json.dumps(self.payload)
        except (TypeError, ValueError) as exc:
            raise TradeLedgerError("payload_not_json_safe") from exc

    def as_dict(self) -> dict:
        return _sorted({
            "eventId": self.event_id, "tradeId": self.trade_id,
            "sequence": self.sequence, "eventType": self.event_type,
            "occurredAt": self.occurred_at, "recordedAt": self.recorded_at,
            "payload": dict(self.payload), "provenance": self.provenance,
            "schemaVersion": self.schema_version,
        })


def new_event_id(trade_id: str, sequence: int) -> str:
    """Deterministic event identity — the same (trade, sequence) always yields
    the same id, which is what makes ingestion idempotent."""
    digest = hashlib.sha256(f"{trade_id}|{sequence}".encode()).hexdigest()[:16]
    return f"evt_{digest}"


def rebuild_entry(events) -> TradeLedgerEntry | None:
    """Deterministically rebuild a ledger entry from its append-only events.

    Amendments increment the version; a finalized entry is never rewritten in
    place — the earlier version stays in the history and can be replayed."""
    ordered = sorted(events, key=lambda e: e.sequence)
    entry: TradeLedgerEntry | None = None
    for event in ordered:
        payload = dict(event.payload)
        if entry is None:
            entry = TradeLedgerEntry(trade_id=event.trade_id,
                                     status=TradeLedgerStatus.OBSERVED,
                                     observed_at=event.occurred_at,
                                     updated_at=event.occurred_at)
        changes: dict = {"updated_at": event.occurred_at}
        snapshot = payload.get("trade")
        if isinstance(snapshot, dict) and snapshot.get("_closed_trade") is not None:
            changes["trade"] = snapshot["_closed_trade"]
        for key, attr in (("warnings", "warnings"), ("conflicts", "conflicts"),
                          ("reconciliationFindings", "reconciliation_findings"),
                          ("managementHistory", "management_history")):
            if key in payload:
                value = payload[key]
                changes[attr] = tuple(value) if isinstance(value, (list, tuple)) else ()
        target = EVENT_STATUS.get(event.event_type)
        if target is not None and target != entry.status:
            if not can_transition(entry.status, target):
                raise TradeLedgerError("invalid_transition",
                                       f"{entry.status} -> {target}")
            changes["status"] = target
            if target == TradeLedgerStatus.FINALIZED:
                changes["finalized_at"] = event.occurred_at
            if target == TradeLedgerStatus.AMENDED:
                changes["amended_at"] = event.occurred_at
                changes["version"] = entry.version + 1
        elif event.event_type == LedgerEventType.AMENDED:
            changes["amended_at"] = event.occurred_at
            changes["version"] = entry.version + 1
        entry = replace(entry, **changes)
    return entry


# ── summary (ledger TOTALS only — no analytics) ───────────────────────────────

@dataclass(frozen=True)
class LedgerSummaryTotals:
    """Deliberately limited to counts and totals. LIVE-4C adds NO win rate, no
    expectancy, no drawdown, no equity curve and no grouping."""
    finalized_trade_count: int = 0
    incomplete_trade_count: int = 0
    conflicted_trade_count: int = 0
    amended_trade_count: int = 0
    gross_realized_pnl: float | None = None
    net_realized_pnl: float | None = None
    total_costs: float | None = None
    latest_closed_at: str | None = None
    account_currency: str | None = None

    @staticmethod
    def of(entries) -> "LedgerSummaryTotals":
        finalized = incomplete = conflicted = amended = 0
        gross_parts: list = []
        net_parts: list = []
        cost_parts: list = []
        latest: str | None = None
        currency: str | None = None
        for entry in entries:
            if entry.status == TradeLedgerStatus.FINALIZED:
                finalized += 1
            elif entry.status == TradeLedgerStatus.AMENDED:
                finalized += 1
                amended += 1
            elif entry.status == TradeLedgerStatus.INCOMPLETE:
                incomplete += 1
            elif entry.status == TradeLedgerStatus.CONFLICTED:
                conflicted += 1
            trade = entry.trade
            if trade is None or not entry.finalized:
                continue
            if trade.gross_realized_pnl is not None:
                gross_parts.append(trade.gross_realized_pnl)
            if trade.net_realized_pnl is not None:
                net_parts.append(trade.net_realized_pnl)
            if trade.costs.total is not None:
                cost_parts.append(trade.costs.total)
            currency = currency or trade.account_currency
            closed = trade.timing.closed_at
            if closed and (latest is None or closed > latest):
                latest = closed
        return LedgerSummaryTotals(
            finalized_trade_count=finalized, incomplete_trade_count=incomplete,
            conflicted_trade_count=conflicted, amended_trade_count=amended,
            gross_realized_pnl=round(sum(gross_parts), 6) if gross_parts else None,
            net_realized_pnl=round(sum(net_parts), 6) if net_parts else None,
            total_costs=round(sum(cost_parts), 6) if cost_parts else None,
            latest_closed_at=latest, account_currency=currency)

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
        })
