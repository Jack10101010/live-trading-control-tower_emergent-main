"""LIVE-4D — the canonical RECOMMENDATION and DECISION domain.

A Recommendation records a PROPOSAL to act on a Scenario:

    "The system or operator believes this Scenario should be acted on under
     these proposed terms."

It completes the decision lineage:

    Scenario -> Recommendation -> Decision -> Intent -> Order -> Position
             -> Deals -> Trade Ledger

WHAT A RECOMMENDATION IS NOT
    a broker order, an execution intent, proof a trade occurred, a strategy
    runtime, an authorization grant, or an execution command. **Accepting a
    Recommendation does not execute anything** — execution still requires the
    existing authorization, execution-mode and safety gates, untouched.

RESPONSIBILITY BOUNDARY
    Scenario      — why the opportunity exists (authoritative for opportunity).
    Recommendation— what action is proposed (authoritative for the proposal).
    Decision      — what an actor decided about that proposal.
    Intent/Order  — what execution actually did.
    Trade Ledger  — the final economic result.

VALUE SEMANTICS
    A missing term is `None` and is reported as UNAVAILABLE. Zero is never a
    substitute for absent: a proposal with no stop is not a proposal with a
    stop at 0.

SECURITY
    Only the minimum actor evidence needed for audit is modelled. There is no
    field for a session token, bearer token or authorization secret — an
    `authorization_reference` is an opaque identifier, never credential
    material. Operator identity is redacted in every read view.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import security_config

SCHEMA_VERSION = "ct.recommendation.v1"
RECOMMENDATION_PREFIX = "rcm_"
DECISION_PREFIX = "dec_"

DIRECTIONS = frozenset({"long", "short"})

# ── availability vocabulary (zero is never a substitute for absent) ──────────
AVAILABLE = "available"
UNAVAILABLE = "unavailable"
NOT_APPLICABLE = "not_applicable"
PENDING = "pending"
CONFLICTED = "conflicted"

# ── bounds (PART 20) ─────────────────────────────────────────────────────────
MAX_METADATA_BYTES = 4 * 1024
MAX_RATIONALE_CHARS = 2000
MAX_TAGS = 16
MAX_TAG_CHARS = 48
MAX_AUTHORIZATION_REF_CHARS = 128
MAX_NOTE_CHARS = 1000                      # LIVE-4E: operator decision note
MAX_CORRELATION_ID_CHARS = 64

# ── LIVE-4E: how much the system actually knows about who acted ──────────────
#: The API boundary authenticated the caller AND the caller asserted this id.
IDENTITY_AUTHENTICATED = "authenticated"
#: The caller asserted this id but the boundary could not authenticate it (the
#: shared-token gate is not per-operator identity). Recorded honestly so an
#: audit trail never claims more assurance than it has.
IDENTITY_ASSERTED = "asserted"
IDENTITY_UNKNOWN = "unknown"

ALL_IDENTITY_ASSURANCES = frozenset({
    IDENTITY_AUTHENTICATED, IDENTITY_ASSERTED, IDENTITY_UNKNOWN,
})


class RecommendationError(ValueError):
    """An invalid recommendation operation. `reason` is a stable machine code."""

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


def _finite_positive(value: Any) -> bool:
    import math
    return (not isinstance(value, bool) and isinstance(value, (int, float))
            and math.isfinite(value) and value > 0)


# ── proposal terms ────────────────────────────────────────────────────────────

class EntryPricePolicy:
    MARKET = "market"                 # take whatever the market gives
    LIMIT_AT = "limit_at"             # only at or better than the stated price
    UNAVAILABLE = "unavailable"


class QuantityPolicy:
    FIXED = "fixed"                   # the stated quantity, exactly
    RISK_DERIVED = "risk_derived"     # derived from the risk terms at submit time
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RecommendationExecutionTerms:
    """HOW the proposal says to execute. Every field may be absent."""
    order_type: str | None = None                 # market | limit | stop | None
    requested_entry_price: float | None = None
    entry_price_policy: str = EntryPricePolicy.UNAVAILABLE
    quantity: float | None = None
    quantity_policy: str = QuantityPolicy.UNAVAILABLE
    time_in_force: str | None = None
    slippage_tolerance: float | None = None

    def __post_init__(self):
        for name in ("requested_entry_price", "quantity", "slippage_tolerance"):
            v = getattr(self, name)
            if v is not None and not _finite_positive(v):
                raise RecommendationError("invalid_execution_term",
                                          f"{name} must be finite and positive")

    @property
    def availability(self) -> str:
        present = [self.order_type, self.requested_entry_price, self.quantity]
        if all(p is None for p in present):
            return UNAVAILABLE
        return AVAILABLE if all(p is not None for p in present) else PENDING

    def as_dict(self) -> dict:
        return _sorted({
            "orderType": self.order_type,
            "requestedEntryPrice": self.requested_entry_price,
            "entryPricePolicy": self.entry_price_policy,
            "quantity": self.quantity,
            "quantityPolicy": self.quantity_policy,
            "timeInForce": self.time_in_force,
            "slippageTolerance": self.slippage_tolerance,
            "executionTermsAvailability": self.availability,
        })


@dataclass(frozen=True)
class RecommendationRiskTerms:
    """The proposal's RISK terms. Absent terms stay absent — never zeroed."""
    stop_loss: float | None = None
    take_profit: float | None = None
    risk_amount: float | None = None
    risk_percent: float | None = None
    planned_r: float | None = None

    def __post_init__(self):
        for name in ("stop_loss", "take_profit", "risk_amount"):
            v = getattr(self, name)
            if v is not None and not _finite_positive(v):
                raise RecommendationError("invalid_risk_term",
                                          f"{name} must be finite and positive")
        if self.risk_percent is not None and not (0 < self.risk_percent <= 100):
            raise RecommendationError("invalid_risk_term",
                                      "risk_percent must be in (0, 100]")

    @property
    def availability(self) -> str:
        if self.stop_loss is None and self.risk_amount is None \
                and self.risk_percent is None:
            return UNAVAILABLE
        return AVAILABLE if self.stop_loss is not None else PENDING

    def as_dict(self) -> dict:
        return _sorted({
            "stopLoss": self.stop_loss, "takeProfit": self.take_profit,
            "riskAmount": self.risk_amount, "riskPercent": self.risk_percent,
            "plannedR": self.planned_r,
            "riskTermsAvailability": self.availability,
        })


@dataclass(frozen=True)
class RecommendationTerms:
    """The complete proposal: execution terms, risk terms and the reasoning."""
    execution: RecommendationExecutionTerms = field(
        default_factory=RecommendationExecutionTerms)
    risk: RecommendationRiskTerms = field(default_factory=RecommendationRiskTerms)
    rationale: str | None = None
    confidence: float | None = None
    tags: tuple = field(default_factory=tuple)
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.rationale is not None and len(self.rationale) > MAX_RATIONALE_CHARS:
            raise RecommendationError("rationale_too_long", str(len(self.rationale)))
        if self.confidence is not None:
            if isinstance(self.confidence, bool) or \
                    not isinstance(self.confidence, (int, float)) or \
                    not (0.0 <= self.confidence <= 1.0):
                raise RecommendationError("invalid_confidence",
                                          "confidence must be within [0, 1]")
        if len(self.tags) > MAX_TAGS:
            raise RecommendationError("too_many_tags", str(len(self.tags)))
        for tag in self.tags:
            if not isinstance(tag, str) or not tag.strip() or len(tag) > MAX_TAG_CHARS:
                raise RecommendationError("invalid_tag", str(tag)[:40])
        try:
            encoded = json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise RecommendationError("metadata_not_json_safe") from exc
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise RecommendationError("metadata_too_large")

    def normalized(self) -> str:
        """A stable, order-independent string of the IMMUTABLE proposal terms.
        Used for identity, so a REVISED proposal yields a distinct id."""
        return json.dumps({"execution": self.execution.as_dict(),
                           "risk": self.risk.as_dict()}, sort_keys=True)

    def as_dict(self) -> dict:
        return _sorted({
            **self.execution.as_dict(), **self.risk.as_dict(),
            "rationale": self.rationale, "confidence": self.confidence,
            "tags": list(self.tags),
            "metadata": security_config.redact_mapping(self.metadata),
        })


# ── identity ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecommendationId:
    """Deterministic identity derived from the IMMUTABLE proposal identity:
    scenario + source + discriminator + normalized terms.

    Deliberately NOT derived from the Scenario alone (a Scenario may produce
    many Recommendations) and never from lifecycle status or realized PnL, so
    the id is stable across restart, rebuild and every decision."""
    value: str

    def __post_init__(self):
        if not re.fullmatch(rf"{RECOMMENDATION_PREFIX}[0-9a-f]{{16}}", str(self.value)):
            raise RecommendationError(
                "invalid_recommendation_id",
                "recommendation id must be rcm_ + 16 lowercase hex chars")

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def derive(*, scenario_id: str, source: str, terms: RecommendationTerms,
               discriminator: str = "") -> "RecommendationId":
        if not (isinstance(scenario_id, str) and scenario_id.strip()):
            raise RecommendationError("invalid_lineage", "scenario id is required")
        if not (isinstance(source, str) and source.strip()):
            raise RecommendationError("invalid_source", "source is required")
        natural = "|".join((scenario_id.strip(), source.strip(),
                            str(discriminator).strip(), terms.normalized()))
        digest = hashlib.sha256(natural.encode("utf-8")).hexdigest()[:16]
        return RecommendationId(f"{RECOMMENDATION_PREFIX}{digest}")


def pseudonymize_actor(actor_id: Any) -> str | None:
    """Deterministic, non-reversible pseudonym for an operator identity.

    Read models never expose a raw operator id. The pseudonym is stable, so an
    auditor can correlate decisions by the same actor without the identity
    itself leaving the store."""
    if not actor_id:
        return None
    digest = hashlib.sha256(str(actor_id).encode("utf-8")).hexdigest()[:12]
    return f"actor_{digest}"


def new_decision_id(recommendation_id: str, sequence: int) -> str:
    """Deterministic decision identity, so replaying the same decision is a
    no-op rather than a duplicate fact."""
    digest = hashlib.sha256(f"{recommendation_id}|{sequence}".encode()).hexdigest()[:16]
    return f"{DECISION_PREFIX}{digest}"


# ── lifecycle ─────────────────────────────────────────────────────────────────

class RecommendationStatus:
    DRAFT = "DRAFT"
    PROPOSED = "PROPOSED"
    PENDING_DECISION = "PENDING_DECISION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"
    SUPERSEDED = "SUPERSEDED"
    INTENT_CREATED = "INTENT_CREATED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    EXECUTED = "EXECUTED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


ALL_STATUSES = frozenset({
    RecommendationStatus.DRAFT, RecommendationStatus.PROPOSED,
    RecommendationStatus.PENDING_DECISION, RecommendationStatus.ACCEPTED,
    RecommendationStatus.REJECTED, RecommendationStatus.EXPIRED,
    RecommendationStatus.WITHDRAWN, RecommendationStatus.SUPERSEDED,
    RecommendationStatus.INTENT_CREATED, RecommendationStatus.PARTIALLY_EXECUTED,
    RecommendationStatus.EXECUTED, RecommendationStatus.FAILED,
    RecommendationStatus.CANCELLED,
})

#: Terminal: the proposal's disposition is settled and cannot change again.
TERMINAL_STATUSES = frozenset({
    RecommendationStatus.REJECTED, RecommendationStatus.EXPIRED,
    RecommendationStatus.WITHDRAWN, RecommendationStatus.SUPERSEDED,
    RecommendationStatus.EXECUTED, RecommendationStatus.FAILED,
    RecommendationStatus.CANCELLED,
})

ACTIVE_STATUSES = frozenset(ALL_STATUSES - TERMINAL_STATUSES)

#: Abandonment reachable from any non-terminal status.
_ABANDON = (RecommendationStatus.EXPIRED, RecommendationStatus.WITHDRAWN,
            RecommendationStatus.SUPERSEDED, RecommendationStatus.CANCELLED)

#: EXPLICIT forward-progress table. ACCEPTED does NOT imply execution: it must
#: pass through INTENT_CREATED, which only the execution boundary can record.
_FORWARD: dict[str, tuple] = {
    RecommendationStatus.DRAFT: (RecommendationStatus.PROPOSED,),
    RecommendationStatus.PROPOSED: (RecommendationStatus.PENDING_DECISION,
                                    RecommendationStatus.ACCEPTED,
                                    RecommendationStatus.REJECTED),
    RecommendationStatus.PENDING_DECISION: (RecommendationStatus.ACCEPTED,
                                            RecommendationStatus.REJECTED),
    RecommendationStatus.ACCEPTED: (RecommendationStatus.INTENT_CREATED,),
    RecommendationStatus.INTENT_CREATED: (RecommendationStatus.PARTIALLY_EXECUTED,
                                          RecommendationStatus.EXECUTED,
                                          RecommendationStatus.FAILED),
    RecommendationStatus.PARTIALLY_EXECUTED: (RecommendationStatus.EXECUTED,
                                              RecommendationStatus.FAILED),
}


def allowed_transitions(status: str) -> frozenset:
    if status in TERMINAL_STATUSES:
        return frozenset()
    return frozenset(_FORWARD.get(status, ())) | frozenset(_ABANDON)


ALLOWED_TRANSITIONS = frozenset(
    (src, dst) for src in ALL_STATUSES for dst in allowed_transitions(src))


def can_transition(src: str, dst: str) -> bool:
    return (src, dst) in ALLOWED_TRANSITIONS


# ── decisions ─────────────────────────────────────────────────────────────────

class RecommendationDecisionType:
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    DEFER = "DEFER"
    EXPIRE = "EXPIRE"
    WITHDRAW = "WITHDRAW"
    SUPERSEDE = "SUPERSEDE"
    AUTO_ACCEPT = "AUTO_ACCEPT"
    AUTO_REJECT = "AUTO_REJECT"
    SYSTEM_INVALIDATE = "SYSTEM_INVALIDATE"


ALL_DECISION_TYPES = frozenset({
    RecommendationDecisionType.ACCEPT, RecommendationDecisionType.REJECT,
    RecommendationDecisionType.DEFER, RecommendationDecisionType.EXPIRE,
    RecommendationDecisionType.WITHDRAW, RecommendationDecisionType.SUPERSEDE,
    RecommendationDecisionType.AUTO_ACCEPT, RecommendationDecisionType.AUTO_REJECT,
    RecommendationDecisionType.SYSTEM_INVALIDATE,
})

#: Which status a decision moves the recommendation to.
DECISION_STATUS = {
    RecommendationDecisionType.ACCEPT: RecommendationStatus.ACCEPTED,
    RecommendationDecisionType.AUTO_ACCEPT: RecommendationStatus.ACCEPTED,
    RecommendationDecisionType.REJECT: RecommendationStatus.REJECTED,
    RecommendationDecisionType.AUTO_REJECT: RecommendationStatus.REJECTED,
    RecommendationDecisionType.DEFER: RecommendationStatus.PENDING_DECISION,
    RecommendationDecisionType.EXPIRE: RecommendationStatus.EXPIRED,
    RecommendationDecisionType.WITHDRAW: RecommendationStatus.WITHDRAWN,
    RecommendationDecisionType.SUPERSEDE: RecommendationStatus.SUPERSEDED,
    RecommendationDecisionType.SYSTEM_INVALIDATE: RecommendationStatus.CANCELLED,
}


# ── LIVE-4E: the OPERATOR decision lifecycle ──────────────────────────────────
#
# `_FORWARD` above is the FULL lifecycle, including states only the execution
# boundary can record. This second, strictly smaller table is the lifecycle an
# OPERATOR can cause, and it is the ONLY one the write API may use:
#
#     decidable  ->  ACCEPTED | REJECTED | EXPIRED
#
# Three properties follow, each pinned by a test:
#
#   1. INTENT_CREATED, PARTIALLY_EXECUTED, EXECUTED and FAILED are NOT reachable
#      by any operator decision. They are OBSERVATION states written only from
#      execution evidence, so an operator cannot move a proposal into a state
#      that asserts a trade happened.
#   2. Accepting records a decision and nothing else — no order is submitted, no
#      authorization is granted, no execution mode changes.
#   3. SUPERSEDE and WITHDRAW are deliberately NOT operator-surface transitions.
#      Supersession requires constructing a REPLACEMENT proposal, i.e. a
#      creation surface this slice does not expose; both stay internal.

#: The statuses on which an operator decision may be taken at all.
DECIDABLE_STATUSES = frozenset({
    RecommendationStatus.PROPOSED, RecommendationStatus.PENDING_DECISION,
})

#: The CLOSED set of operator decision types. Every other type in
#: `ALL_DECISION_TYPES` is internal (DEFER, WITHDRAW, SUPERSEDE) or automated
#: (AUTO_ACCEPT, AUTO_REJECT, SYSTEM_INVALIDATE) and the gate refuses it.
OPERATOR_DECISION_TYPES = frozenset({
    RecommendationDecisionType.ACCEPT, RecommendationDecisionType.REJECT,
    RecommendationDecisionType.EXPIRE,
})

#: src -> the statuses an OPERATOR decision may move it to. Nothing else.
OPERATOR_TRANSITIONS: dict[str, frozenset] = {
    status: frozenset({RecommendationStatus.ACCEPTED,
                       RecommendationStatus.REJECTED,
                       RecommendationStatus.EXPIRED})
    for status in DECIDABLE_STATUSES
}

#: Every status an operator decision can ever produce.
OPERATOR_REACHABLE_STATUSES = frozenset(
    dst for targets in OPERATOR_TRANSITIONS.values() for dst in targets)


def operator_can_transition(src: str, dst: str) -> bool:
    """True only when an OPERATOR may cause `src -> dst`. Strictly narrower than
    `can_transition`: every operator transition is also a domain transition, but
    not the reverse."""
    return dst in OPERATOR_TRANSITIONS.get(src, frozenset())


def undecidable_reason(status: str) -> str | None:
    """Why this proposal cannot be decided, or None when it can be.

    The UI renders this verbatim, so a disabled control always states its own
    reason instead of silently doing nothing."""
    if status in DECIDABLE_STATUSES:
        return None
    if status == RecommendationStatus.DRAFT:
        return "not_yet_proposed"
    if status == RecommendationStatus.ACCEPTED:
        return "already_accepted"
    if status in TERMINAL_STATUSES:
        return f"already_terminal:{status}"
    if status in (RecommendationStatus.INTENT_CREATED,
                  RecommendationStatus.PARTIALLY_EXECUTED):
        return f"execution_in_progress:{status}"
    return f"not_decidable:{status}"


class ActorType:
    OPERATOR = "OPERATOR"
    SYSTEM = "SYSTEM"
    STRATEGY = "STRATEGY"
    RISK_ENGINE = "RISK_ENGINE"
    IMPORT = "IMPORT"
    UNKNOWN = "UNKNOWN"


ALL_ACTOR_TYPES = frozenset({
    ActorType.OPERATOR, ActorType.SYSTEM, ActorType.STRATEGY,
    ActorType.RISK_ENGINE, ActorType.IMPORT, ActorType.UNKNOWN,
})


@dataclass(frozen=True)
class RecommendationDecision:
    """One immutable decision about a proposal. Append-only: decisions are
    never overwritten, and the latest EFFECTIVE decision is derived."""
    decision_id: str
    recommendation_id: str
    decision_type: str
    actor_type: str
    actor_id: str | None
    occurred_at: str
    sequence: int = 1
    reason: str | None = None
    authorization_reference: str | None = None
    execution_mode: str | None = None
    provenance: str = "operator"
    metadata: dict = field(default_factory=dict)
    #: LIVE-4E: the operator's free-text note. Distinct from `reason`: `reason`
    #: is the REQUIRED justification (and, for a rejection, the rejection
    #: reason); `note` is optional colour. Both are bounded and neither is ever
    #: interpreted — they are recorded and displayed as text.
    note: str | None = None
    #: The version this decision was taken against — the event sequence the
    #: deciding operator had actually read.
    against_version: int | None = None
    #: Correlation id threaded from the request, so a decision can be tied back
    #: to the journal entry and the HTTP request that produced it.
    correlation_id: str | None = None
    #: Whether the acting identity was AUTHENTICATED by the API boundary or
    #: merely ASSERTED by the caller. Never inferred — see
    #: `recommendation_authorization`. History must not overclaim.
    identity_assurance: str = "unknown"

    def __post_init__(self):
        if self.note is not None and len(str(self.note)) > MAX_NOTE_CHARS:
            raise RecommendationError("note_too_long")
        if self.identity_assurance not in ALL_IDENTITY_ASSURANCES:
            raise RecommendationError("unknown_identity_assurance",
                                      str(self.identity_assurance))
        if self.correlation_id is not None:
            corr = str(self.correlation_id)
            if len(corr) > MAX_CORRELATION_ID_CHARS:
                raise RecommendationError("correlation_id_too_long")
            if not re.fullmatch(r"[A-Za-z0-9_.:-]+", corr):
                raise RecommendationError("invalid_correlation_id", corr)
        if self.against_version is not None and (
                isinstance(self.against_version, bool)
                or not isinstance(self.against_version, int)
                or self.against_version < 0):
            raise RecommendationError("invalid_against_version",
                                      str(self.against_version))
        if self.decision_type not in ALL_DECISION_TYPES:
            raise RecommendationError("unknown_decision_type", str(self.decision_type))
        if self.actor_type not in ALL_ACTOR_TYPES:
            raise RecommendationError("unknown_actor_type", str(self.actor_type))
        if _parse(self.occurred_at) is None:
            raise RecommendationError("invalid_timestamp", str(self.occurred_at))
        if self.authorization_reference is not None:
            ref = str(self.authorization_reference)
            if len(ref) > MAX_AUTHORIZATION_REF_CHARS:
                raise RecommendationError("authorization_reference_too_long")
            # An authorization REFERENCE is an opaque id. Anything resembling
            # credential material is refused outright (PART 20).
            if any(marker in ref.lower() for marker in
                   ("bearer ", "password", "secret", "token=", "apikey", "api_key")):
                raise RecommendationError("authorization_reference_looks_secret")
        try:
            json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise RecommendationError("metadata_not_json_safe") from exc

    def safe_view(self) -> dict:
        """Redaction-safe projection: the actor is pseudonymized and no
        credential material can appear."""
        return _sorted({
            "decisionId": self.decision_id,
            "recommendationId": self.recommendation_id,
            "decisionType": self.decision_type,
            "actorType": self.actor_type,
            "actor": pseudonymize_actor(self.actor_id),
            "occurredAt": self.occurred_at, "sequence": self.sequence,
            "reason": self.reason,
            "authorizationReference": self.authorization_reference,
            "executionMode": self.execution_mode,
            "provenance": self.provenance,
            "metadata": security_config.redact_mapping(self.metadata),
            "note": self.note,
            "againstVersion": self.against_version,
            "correlationId": self.correlation_id,
            "identityAssurance": self.identity_assurance,
        })

    def as_dict(self) -> dict:
        return self.safe_view()


def latest_decision(decisions) -> RecommendationDecision | None:
    """The latest EFFECTIVE decision: highest sequence, ties broken by
    timestamp then id — deterministic under any ingestion order."""
    items = list(decisions)
    if not items:
        return None
    return sorted(items, key=lambda d: (d.sequence, d.occurred_at, d.decision_id))[-1]


def conflicting_decisions(decisions) -> tuple:
    """Distinct decision types recorded at the SAME sequence are contradictory
    and must stay visible — they are never normalized away."""
    by_sequence: dict[int, set] = {}
    for decision in decisions:
        by_sequence.setdefault(decision.sequence, set()).add(decision.decision_type)
    return tuple(sorted(
        f"conflicting_decisions_at_sequence_{seq}: {sorted(types)}"
        for seq, types in by_sequence.items() if len(types) > 1))


# ── outcome (proposal disposition, NOT financial performance) ─────────────────

class RecommendationOutcome:
    NOT_EXECUTED = "NOT_EXECUTED"
    EXECUTED = "EXECUTED"
    PARTIALLY_EXECUTED = "PARTIALLY_EXECUTED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    WITHDRAWN = "WITHDRAWN"
    SUPERSEDED = "SUPERSEDED"
    EXECUTION_FAILED = "EXECUTION_FAILED"
    UNKNOWN = "UNKNOWN"

    @staticmethod
    def of(status: str) -> str:
        """Disposition of the PROPOSAL. A profitable trade never makes a
        Recommendation 'accepted', and a rejected proposal has no trade
        outcome — the two vocabularies are deliberately separate."""
        return {
            RecommendationStatus.EXECUTED: RecommendationOutcome.EXECUTED,
            RecommendationStatus.PARTIALLY_EXECUTED: RecommendationOutcome.PARTIALLY_EXECUTED,
            RecommendationStatus.REJECTED: RecommendationOutcome.REJECTED,
            RecommendationStatus.EXPIRED: RecommendationOutcome.EXPIRED,
            RecommendationStatus.WITHDRAWN: RecommendationOutcome.WITHDRAWN,
            RecommendationStatus.SUPERSEDED: RecommendationOutcome.SUPERSEDED,
            RecommendationStatus.FAILED: RecommendationOutcome.EXECUTION_FAILED,
            RecommendationStatus.CANCELLED: RecommendationOutcome.WITHDRAWN,
            RecommendationStatus.DRAFT: RecommendationOutcome.NOT_EXECUTED,
            RecommendationStatus.PROPOSED: RecommendationOutcome.NOT_EXECUTED,
            RecommendationStatus.PENDING_DECISION: RecommendationOutcome.NOT_EXECUTED,
            RecommendationStatus.ACCEPTED: RecommendationOutcome.NOT_EXECUTED,
            RecommendationStatus.INTENT_CREATED: RecommendationOutcome.NOT_EXECUTED,
        }.get(status, RecommendationOutcome.UNKNOWN)


# ── source ────────────────────────────────────────────────────────────────────

class RecommendationSource:
    OPERATOR = "OPERATOR"
    STRATEGY = "STRATEGY"          # reserved; no strategy runtime exists
    RISK_ENGINE = "RISK_ENGINE"
    IMPORT = "IMPORT"              # imported evidence (e.g. the fixture world)
    UNKNOWN = "UNKNOWN"


# ── lineage ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecommendationLineage:
    """Explicit lineage — never inferred, never string-matched in the UI."""
    recommendation_id: str
    scenario_id: str
    node_id: str | None = None
    account_fingerprint: str | None = None
    instrument: str | None = None
    direction: str | None = None
    linked_intent_ids: tuple = field(default_factory=tuple)
    superseded_by: str | None = None
    supersedes: str | None = None
    source_reference: str | None = None      # e.g. a fixture recommendation id

    def as_dict(self) -> dict:
        return _sorted({
            "recommendationId": self.recommendation_id,
            "scenarioId": self.scenario_id, "nodeId": self.node_id,
            "accountFingerprint": _mask(self.account_fingerprint),
            "instrument": self.instrument, "direction": self.direction,
            "linkedIntentIds": list(self.linked_intent_ids),
            "supersededBy": self.superseded_by, "supersedes": self.supersedes,
            "sourceReference": self.source_reference,
        })


def _mask(value: Any) -> str | None:
    """Account fingerprints are masked in every read model."""
    if not value:
        return None
    s = str(value)
    return f"{s[:4]}…{s[-4:]}" if len(s) > 10 else "…"


# ── the canonical Recommendation ──────────────────────────────────────────────

@dataclass(frozen=True)
class Recommendation:
    """The immutable proposal to act on one Scenario."""
    recommendation_id: str
    scenario_id: str
    instrument: str
    direction: str
    source: str = RecommendationSource.UNKNOWN
    status: str = RecommendationStatus.DRAFT
    node_id: str | None = None
    account_fingerprint: str | None = None
    terms: RecommendationTerms = field(default_factory=RecommendationTerms)
    created_at: str = ""
    updated_at: str = ""
    decided_at: str | None = None
    withdrawn_at: str | None = None
    expired_at: str | None = None
    expiry_at: str | None = None
    superseded_by: str | None = None
    supersedes: str | None = None
    linked_intent_ids: tuple = field(default_factory=tuple)
    source_reference: str | None = None
    provenance: str = "operator"
    #: LIVE-4E: the optimistic-concurrency version — the sequence of the LAST
    #: event applied to this proposal. It is derived from the append-only event
    #: log, never assigned independently, so it cannot drift from history. A
    #: caller that read version N and writes against N is guaranteed nothing
    #: else was recorded in between.
    version: int = 0

    def __post_init__(self):
        if not re.fullmatch(rf"{RECOMMENDATION_PREFIX}[0-9a-f]{{16}}",
                            str(self.recommendation_id)):
            raise RecommendationError("invalid_recommendation_id",
                                      str(self.recommendation_id))
        if not (isinstance(self.scenario_id, str) and self.scenario_id.strip()):
            raise RecommendationError("invalid_lineage", "scenario id is required")
        if not (isinstance(self.instrument, str) and self.instrument.strip()):
            raise RecommendationError("invalid_instrument", "instrument is required")
        if self.direction not in DIRECTIONS:
            raise RecommendationError("invalid_direction", str(self.direction))
        if self.status not in ALL_STATUSES:
            raise RecommendationError("unknown_status", str(self.status))

    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def outcome(self) -> str:
        return RecommendationOutcome.of(self.status)

    def age_seconds(self, now: str) -> float | None:
        start, ref = _parse(self.created_at), _parse(now)
        return None if start is None or ref is None else \
            round((ref - start).total_seconds(), 3)

    def past_due(self, now: str) -> bool:
        """Whether expiry has passed. A PROJECTION may report this; only an
        explicit expiry event may persist EXPIRED (PART 18)."""
        expiry, ref = _parse(self.expiry_at), _parse(now)
        return (expiry is not None and ref is not None and ref >= expiry
                and self.status not in TERMINAL_STATUSES)

    @property
    def decidable(self) -> bool:
        """LIVE-4E: whether an OPERATOR may take a decision on this proposal."""
        return self.status in DECIDABLE_STATUSES

    @property
    def undecidable_reason(self) -> str | None:
        """Why it is not decidable, or None. Rendered verbatim by the UI."""
        return undecidable_reason(self.status)

    def lineage(self) -> RecommendationLineage:
        return RecommendationLineage(
            recommendation_id=self.recommendation_id, scenario_id=self.scenario_id,
            node_id=self.node_id, account_fingerprint=self.account_fingerprint,
            instrument=self.instrument, direction=self.direction,
            linked_intent_ids=self.linked_intent_ids,
            superseded_by=self.superseded_by, supersedes=self.supersedes,
            source_reference=self.source_reference)

    def as_dict(self) -> dict:
        return _sorted({
            "recommendationId": self.recommendation_id,
            "scenarioId": self.scenario_id, "instrument": self.instrument,
            "direction": self.direction, "source": self.source,
            "status": self.status, "outcome": self.outcome, "active": self.active,
            "nodeId": self.node_id,
            "accountFingerprint": _mask(self.account_fingerprint),
            **self.terms.as_dict(),
            "createdAt": self.created_at, "updatedAt": self.updated_at,
            "decidedAt": self.decided_at, "withdrawnAt": self.withdrawn_at,
            "expiredAt": self.expired_at, "expiryAt": self.expiry_at,
            "supersededBy": self.superseded_by, "supersedes": self.supersedes,
            "linkedIntentIds": list(self.linked_intent_ids),
            "sourceReference": self.source_reference,
            "provenance": self.provenance, "schemaVersion": SCHEMA_VERSION,
            "version": self.version,
            "decidable": self.decidable,
            "undecidableReason": self.undecidable_reason,
        })


def new_recommendation(*, scenario_id: str, instrument: str, direction: str,
                       source: str, created_at: str,
                       terms: RecommendationTerms | None = None,
                       discriminator: str = "", **rest) -> Recommendation:
    """Construct a recommendation with a DERIVED deterministic identity.
    Explicit only — nothing in this repository creates one automatically."""
    resolved = terms or RecommendationTerms()
    rid = RecommendationId.derive(scenario_id=scenario_id, source=source,
                                  terms=resolved, discriminator=discriminator)
    rest.setdefault("updated_at", created_at)
    return Recommendation(recommendation_id=rid.value, scenario_id=scenario_id,
                          instrument=instrument, direction=direction, source=source,
                          terms=resolved, created_at=created_at, **rest)


def transition(recommendation: Recommendation, to_status: str, *, at: str,
               reason: str) -> Recommendation:
    """Return a NEW recommendation in `to_status`. Pure — the input is untouched.

    ACCEPTED never jumps to EXECUTED: execution evidence must arrive through
    INTENT_CREATED, which only the execution boundary records."""
    if to_status not in ALL_STATUSES:
        raise RecommendationError("unknown_status", str(to_status))
    if recommendation.status in TERMINAL_STATUSES:
        raise RecommendationError("terminal_status_immutable",
                                  f"{recommendation.status} is terminal")
    if not can_transition(recommendation.status, to_status):
        raise RecommendationError("invalid_transition",
                                  f"{recommendation.status} -> {to_status}")
    if not (isinstance(reason, str) and reason.strip()):
        raise RecommendationError("reason_required")
    new_at, prev = _parse(at), _parse(recommendation.updated_at)
    if new_at is None:
        raise RecommendationError("invalid_timestamp", str(at))
    if prev is not None and new_at < prev:
        raise RecommendationError("non_monotonic_timestamp",
                                  f"{at} precedes {recommendation.updated_at}")
    changes: dict = {"status": to_status, "updated_at": at}
    if to_status in (RecommendationStatus.ACCEPTED, RecommendationStatus.REJECTED):
        changes["decided_at"] = at
    if to_status == RecommendationStatus.WITHDRAWN:
        changes["withdrawn_at"] = at
    if to_status == RecommendationStatus.EXPIRED:
        changes["expired_at"] = at
    return replace(recommendation, **changes)


def link_intent(recommendation: Recommendation, intent_id: str, *,
                at: str) -> Recommendation:
    """Record an EXPLICIT intent link. Idempotent and sorted. One
    Recommendation may link to MANY intents."""
    if not (isinstance(intent_id, str) and intent_id.strip()):
        raise RecommendationError("invalid_intent_id", str(intent_id))
    if intent_id in recommendation.linked_intent_ids:
        return recommendation
    return replace(recommendation, updated_at=at,
                   linked_intent_ids=tuple(sorted(
                       recommendation.linked_intent_ids + (intent_id,))))


def supersede(previous: Recommendation, replacement: Recommendation, *,
              at: str, reason: str) -> tuple:
    """Supersede `previous` with `replacement`, preserving all prior history.

    Rejects a cycle and rejects supersession across Scenarios — a replacement
    proposal must concern the SAME opportunity."""
    if previous.recommendation_id == replacement.recommendation_id:
        raise RecommendationError("supersession_cycle", "a recommendation "
                                  "cannot supersede itself")
    if previous.scenario_id != replacement.scenario_id:
        raise RecommendationError("cross_scenario_supersession",
                                  f"{previous.scenario_id} != {replacement.scenario_id}")
    if replacement.superseded_by == previous.recommendation_id:
        raise RecommendationError("supersession_cycle", "circular supersession")
    superseded = transition(previous, RecommendationStatus.SUPERSEDED, at=at,
                            reason=reason)
    superseded = replace(superseded,
                         superseded_by=replacement.recommendation_id)
    linked = replace(replacement, supersedes=previous.recommendation_id,
                     updated_at=at)
    return superseded, linked


# ── events ────────────────────────────────────────────────────────────────────

class RecommendationEventType:
    CREATED = "RecommendationCreated"
    PROPOSED = "RecommendationProposed"
    DECISION_RECORDED = "RecommendationDecisionRecorded"
    ACCEPTED = "RecommendationAccepted"
    REJECTED = "RecommendationRejected"
    DEFERRED = "RecommendationDeferred"
    EXPIRED = "RecommendationExpired"
    WITHDRAWN = "RecommendationWithdrawn"
    SUPERSEDED = "RecommendationSuperseded"
    INTENT_LINKED = "RecommendationIntentLinked"
    EXECUTION_OBSERVED = "RecommendationExecutionObserved"
    FAILED = "RecommendationFailed"


ALL_EVENT_TYPES = frozenset(
    v for k, v in vars(RecommendationEventType).items()
    if not k.startswith("_") and isinstance(v, str))

#: Which event records arrival at which status (used to rebuild).
STATUS_EVENT = {
    RecommendationStatus.DRAFT: RecommendationEventType.CREATED,
    RecommendationStatus.PROPOSED: RecommendationEventType.PROPOSED,
    RecommendationStatus.PENDING_DECISION: RecommendationEventType.DEFERRED,
    RecommendationStatus.ACCEPTED: RecommendationEventType.ACCEPTED,
    RecommendationStatus.REJECTED: RecommendationEventType.REJECTED,
    RecommendationStatus.EXPIRED: RecommendationEventType.EXPIRED,
    RecommendationStatus.WITHDRAWN: RecommendationEventType.WITHDRAWN,
    RecommendationStatus.SUPERSEDED: RecommendationEventType.SUPERSEDED,
    RecommendationStatus.INTENT_CREATED: RecommendationEventType.INTENT_LINKED,
    RecommendationStatus.PARTIALLY_EXECUTED: RecommendationEventType.EXECUTION_OBSERVED,
    RecommendationStatus.EXECUTED: RecommendationEventType.EXECUTION_OBSERVED,
    RecommendationStatus.FAILED: RecommendationEventType.FAILED,
    RecommendationStatus.CANCELLED: RecommendationEventType.WITHDRAWN,
}


@dataclass(frozen=True)
class RecommendationEvent:
    """One immutable, timestamped, serializable recommendation fact."""
    event_id: str
    recommendation_id: str
    sequence: int
    event_type: str
    occurred_at: str
    recorded_at: str
    payload: dict = field(default_factory=dict)
    provenance: str = "operator"
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self):
        if self.event_type not in ALL_EVENT_TYPES:
            raise RecommendationError("unknown_event_type", str(self.event_type))
        if _parse(self.occurred_at) is None or _parse(self.recorded_at) is None:
            raise RecommendationError("invalid_timestamp",
                                      f"{self.occurred_at} / {self.recorded_at}")
        try:
            json.dumps(self.payload)
        except (TypeError, ValueError) as exc:
            raise RecommendationError("payload_not_json_safe") from exc

    def as_dict(self) -> dict:
        return _sorted({
            "eventId": self.event_id, "recommendationId": self.recommendation_id,
            "sequence": self.sequence, "eventType": self.event_type,
            "occurredAt": self.occurred_at, "recordedAt": self.recorded_at,
            "payload": security_config.redact_mapping(self.payload),
            "provenance": self.provenance, "schemaVersion": self.schema_version,
        })


def new_event_id(recommendation_id: str, sequence: int) -> str:
    digest = hashlib.sha256(f"{recommendation_id}|{sequence}".encode()).hexdigest()[:16]
    return f"rev_{digest}"


def rebuild(events) -> Recommendation | None:
    """Deterministically rebuild a recommendation from its append-only events.

    The CREATED event carries the construction payload; later events replay
    status transitions and intent links in sequence order."""
    ordered = sorted(events, key=lambda e: e.sequence)
    recommendation: Recommendation | None = None
    for event in ordered:
        payload = dict(event.payload)
        if event.event_type == RecommendationEventType.CREATED:
            snapshot = payload.get("recommendation") or {}
            recommendation = Recommendation(
                recommendation_id=event.recommendation_id,
                scenario_id=snapshot["scenarioId"],
                instrument=snapshot["instrument"], direction=snapshot["direction"],
                source=snapshot.get("source", RecommendationSource.UNKNOWN),
                status=RecommendationStatus.DRAFT,
                node_id=snapshot.get("nodeId"),
                account_fingerprint=snapshot.get("accountFingerprintRaw"),
                terms=_terms_from(snapshot),
                created_at=event.occurred_at, updated_at=event.occurred_at,
                expiry_at=snapshot.get("expiryAt"),
                supersedes=snapshot.get("supersedes"),
                source_reference=snapshot.get("sourceReference"),
                provenance=snapshot.get("provenance", "operator"))
            continue
        if recommendation is None:
            raise RecommendationError("history_without_creation",
                                      event.recommendation_id)
        intent_id = payload.get("intentId")
        if intent_id:
            recommendation = link_intent(recommendation, intent_id,
                                         at=event.occurred_at)
        superseded_by = payload.get("supersededBy")
        if superseded_by:
            recommendation = replace(recommendation, superseded_by=superseded_by)
        status = payload.get("status")
        if status and status != recommendation.status:
            recommendation = transition(recommendation, status,
                                        at=event.occurred_at,
                                        reason=payload.get("reason") or "replay")
    return recommendation


def _terms_from(snapshot: dict) -> RecommendationTerms:
    return RecommendationTerms(
        execution=RecommendationExecutionTerms(
            order_type=snapshot.get("orderType"),
            requested_entry_price=snapshot.get("requestedEntryPrice"),
            entry_price_policy=snapshot.get("entryPricePolicy",
                                            EntryPricePolicy.UNAVAILABLE),
            quantity=snapshot.get("quantity"),
            quantity_policy=snapshot.get("quantityPolicy",
                                         QuantityPolicy.UNAVAILABLE),
            time_in_force=snapshot.get("timeInForce"),
            slippage_tolerance=snapshot.get("slippageTolerance")),
        risk=RecommendationRiskTerms(
            stop_loss=snapshot.get("stopLoss"),
            take_profit=snapshot.get("takeProfit"),
            risk_amount=snapshot.get("riskAmount"),
            risk_percent=snapshot.get("riskPercent"),
            planned_r=snapshot.get("plannedR")),
        rationale=snapshot.get("rationale"), confidence=snapshot.get("confidence"),
        tags=tuple(snapshot.get("tags") or ()),
        metadata=dict(snapshot.get("metadata") or {}))


# ── summary ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class RecommendationSummary:
    """Deterministic counts. NO performance metrics (PART 12 non-goal)."""
    active_count: int = 0
    pending_decision_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    expired_count: int = 0
    withdrawn_count: int = 0
    superseded_count: int = 0
    execution_linked_count: int = 0
    latest_created_at: str | None = None

    @staticmethod
    def of(recommendations) -> "RecommendationSummary":
        counts = {"active": 0, "pending": 0, "accepted": 0, "rejected": 0,
                  "expired": 0, "withdrawn": 0, "superseded": 0, "linked": 0}
        latest: str | None = None
        for item in recommendations:
            if item.active:
                counts["active"] += 1
            status = item.status
            if status == RecommendationStatus.PENDING_DECISION:
                counts["pending"] += 1
            elif status == RecommendationStatus.ACCEPTED:
                counts["accepted"] += 1
            elif status == RecommendationStatus.REJECTED:
                counts["rejected"] += 1
            elif status == RecommendationStatus.EXPIRED:
                counts["expired"] += 1
            elif status in (RecommendationStatus.WITHDRAWN,
                            RecommendationStatus.CANCELLED):
                counts["withdrawn"] += 1
            elif status == RecommendationStatus.SUPERSEDED:
                counts["superseded"] += 1
            if item.linked_intent_ids:
                counts["linked"] += 1
            if item.created_at and (latest is None or item.created_at > latest):
                latest = item.created_at
        return RecommendationSummary(
            active_count=counts["active"],
            pending_decision_count=counts["pending"],
            accepted_count=counts["accepted"], rejected_count=counts["rejected"],
            expired_count=counts["expired"], withdrawn_count=counts["withdrawn"],
            superseded_count=counts["superseded"],
            execution_linked_count=counts["linked"], latest_created_at=latest)

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
        })
