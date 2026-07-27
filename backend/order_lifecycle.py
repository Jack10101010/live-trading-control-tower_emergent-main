"""ARCH-2 — canonical order intent, identity, and lifecycle state machine.

One module owns three concepts that previously had no owner at all (execution
lifecycle state lived only in process-transient `ExecutionResult` objects):

  1. ORDER INTENT — the broker-neutral, immutable description of what an operator
     (or a future strategy) asked execution to do. It carries only fields the
     current command vocabulary and broker contract justify; it invents no
     strategy behaviour.
  2. IDENTITY — one canonical ID generator per entity type, visually
     distinguishable by prefix and unambiguous against every existing prefix in
     the repository (`cmd_` commands, `ev_` events, `tr_`/`ord_`/`pos_` fixture
     entities, `dpl_` deployments, `acctfp_` account fingerprints, `brk_` brokers):

         intent_<uuid4hex>   — order intents (this module)
         recon_<uuid4hex>    — reconciliation runs (reconciliation.py)

     IDs are immutable; an intent's `command_id`/`correlation_id` record its
     lineage explicitly rather than by prefix-sniffing.
  3. LIFECYCLE — a deterministic state machine with an explicit allowed-transition
     table. No implicit mutation: the ONLY way to move an intent between states is
     `transition()`, which validates against the table, enforces monotonic
     timestamps, and returns a transition record carrying reason + evidence for
     the durable audit history. Invalid transitions raise `LifecycleError` (the
     caller records the denial as an audit event; state is untouched).

STATES (only those justified by current evidence — the mock broker executes
synchronously and the command vocabulary contains close / cancel / modify
operations plus a future submit path):

    created                intent recorded, nothing checked yet
    validated              structural validation passed
    safety_denied          execution safety refused (terminal)
    ready                  authorized and feasible; not yet handed to an adapter
    submitting             submit request being sent (submit-kind intents)
    submitted              adapter accepted the submit request
    acknowledged           broker acknowledged the order (distinct from any fill)
    partially_filled       some quantity filled (distinct from filled)
    filled                 fully filled (terminal)
    open                   LIVE-2: market-order submission complete — the broker
                           acknowledged and the resulting position is open
                           (terminal for the SUBMISSION intent; the open position
                           itself is tracked by broker reads + reconciliation)
    modify_pending         modify requested, not yet confirmed
    modified               modify confirmed (terminal for a modify intent)
    cancel_pending         cancel requested, not yet confirmed
    cancelled              cancel confirmed (terminal)
    close_pending          close requested, not yet confirmed
    closed                 close confirmed (terminal)
    rejected               refused by validation/feasibility/adapter (terminal)
    expired                intent expiry passed before completion (terminal)
    unknown                in-flight state lost (e.g. restart); NEVER upgraded to
                           a healthy state without reconciliation evidence
    reconciliation_required unknown state queued for reconciliation
    reconciled             reconciliation produced evidence; resolves onward
    failed                 unrecoverable failure (terminal)

RESTART RECOVERY (deterministic, fail-closed, no fabricated completion):
    * a non-terminal intent that never reached an adapter (created/validated/
      ready) is marked `failed` with reason `restart_before_dispatch` — it is
      certain no broker action occurred;
    * a non-terminal intent that MAY have reached an adapter is marked `unknown`
      then `reconciliation_required` — recovery never guesses an outcome.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import security_config

# ── identity ──────────────────────────────────────────────────────────────────

INTENT_PREFIX = "intent_"
RECONCILIATION_PREFIX = "recon_"


def new_intent_id() -> str:
    """Collision-resistant, immutable, visually distinct intent identity."""
    return f"{INTENT_PREFIX}{uuid.uuid4().hex}"


def new_reconciliation_id() -> str:
    """Identity for one reconciliation run (used by reconciliation.py)."""
    return f"{RECONCILIATION_PREFIX}{uuid.uuid4().hex}"


# ── intent kinds (derived from the command vocabulary, never invented) ────────

KIND_SUBMIT = "submit"      # place a new order (no current command; future path)
KIND_MODIFY = "modify"      # modify an existing order/trade parameter
KIND_CANCEL = "cancel"      # cancel a resting order
KIND_CLOSE = "close"        # close (fully or partially) an open position
KNOWN_KINDS = frozenset({KIND_SUBMIT, KIND_MODIFY, KIND_CANCEL, KIND_CLOSE})

#: Which pending state each kind moves through — the request-vs-confirmation
#: distinction the lifecycle requires (a cancel REQUEST is not a cancellation).
PENDING_STATE_BY_KIND = {
    KIND_SUBMIT: "submitting",
    KIND_MODIFY: "modify_pending",
    KIND_CANCEL: "cancel_pending",
    KIND_CLOSE: "close_pending",
}

#: The confirmed terminal outcome of each kind's happy path.
CONFIRMED_STATE_BY_KIND = {
    KIND_SUBMIT: "filled",
    KIND_MODIFY: "modified",
    KIND_CANCEL: "cancelled",
    KIND_CLOSE: "closed",
}


# ── the canonical order intent ────────────────────────────────────────────────

MAX_METADATA_BYTES = 4 * 1024


@dataclass(frozen=True)
class OrderIntent:
    """The immutable, broker-neutral description of one execution ask.

    Every field is justified by the current command vocabulary (registry),
    the broker contract, or the command envelope — nothing is speculative.
    `metadata` is a bounded, JSON-safe dict for command payload echoes; it is
    validated at construction and only ever surfaced redacted.
    """
    intent_id: str
    command_name: str                    # canonical registry name (e.g. CloseTrade)
    kind: str                            # KIND_* — how the adapter must treat it
    command_id: str | None = None        # lineage: the operator command
    correlation_id: str | None = None    # lineage: groups related records
    idempotency_key: str | None = None
    deployment_id: str | None = None
    account_id: str | None = None
    instrument: str | None = None
    side: str | None = None              # long/short where the command implies one
    order_type: str | None = None        # market/limit/stop where implied
    quantity: float | None = None        # requested size (lots), where implied
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    time_in_force: str | None = None
    source: str = "operator"             # operator | strategy (future) | system
    created_at: str = ""
    expires_at: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.kind not in KNOWN_KINDS:
            raise ValueError(f"unknown intent kind: {self.kind!r}")
        if not self.intent_id.startswith(INTENT_PREFIX):
            raise ValueError("intent_id must carry the canonical intent_ prefix")
        import json
        try:
            encoded = json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise ValueError("intent metadata must be JSON-safe") from exc
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ValueError("intent metadata exceeds the bounded size")

    def safe_view(self) -> dict:
        """Redaction-safe projection for audit/telemetry (metadata masked)."""
        return {
            "intentId": self.intent_id,
            "commandName": self.command_name,
            "kind": self.kind,
            "commandId": self.command_id,
            "correlationId": self.correlation_id,
            "idempotencyKey": self.idempotency_key,
            "deploymentId": self.deployment_id,
            "accountId": self.account_id,
            "instrument": self.instrument,
            "side": self.side,
            "orderType": self.order_type,
            "quantity": self.quantity,
            "source": self.source,
            "createdAt": self.created_at,
            "expiresAt": self.expires_at,
            "metadata": security_config.redact_mapping(self.metadata),
        }


# ── lifecycle states and the explicit transition table ────────────────────────

CREATED = "created"
VALIDATED = "validated"
SAFETY_DENIED = "safety_denied"
READY = "ready"
SUBMITTING = "submitting"
SUBMITTED = "submitted"
ACKNOWLEDGED = "acknowledged"
PARTIALLY_FILLED = "partially_filled"
FILLED = "filled"
OPEN = "open"          # LIVE-2: acknowledged market order -> position open (terminal)
MODIFY_PENDING = "modify_pending"
MODIFIED = "modified"
CANCEL_PENDING = "cancel_pending"
CANCELLED = "cancelled"
CLOSE_PENDING = "close_pending"
CLOSED = "closed"
REJECTED = "rejected"
EXPIRED = "expired"
UNKNOWN = "unknown"
RECONCILIATION_REQUIRED = "reconciliation_required"
RECONCILED = "reconciled"
FAILED = "failed"

ALL_STATES = frozenset({
    CREATED, VALIDATED, SAFETY_DENIED, READY, SUBMITTING, SUBMITTED, ACKNOWLEDGED,
    PARTIALLY_FILLED, FILLED, OPEN, MODIFY_PENDING, MODIFIED, CANCEL_PENDING, CANCELLED,
    CLOSE_PENDING, CLOSED, REJECTED, EXPIRED, UNKNOWN, RECONCILIATION_REQUIRED,
    RECONCILED, FAILED,
})

TERMINAL_STATES = frozenset({
    SAFETY_DENIED, FILLED, OPEN, MODIFIED, CANCELLED, CLOSED, REJECTED, EXPIRED, FAILED,
})

#: The explicit allowed-transition table. A pair absent from this table is an
#: invalid transition, full stop. Notes:
#:   * broker acknowledgement (acknowledged) is distinct from any fill;
#:   * partial fill is distinct from fill;
#:   * every *_pending state is the REQUEST; its confirmed state is separate;
#:   * `unknown` can only move to reconciliation_required or failed — evidence
#:     (via `reconciled`) is the only path back to a healthy state;
#:   * terminal states appear in no left column.
ALLOWED_TRANSITIONS: frozenset = frozenset({
    (CREATED, VALIDATED), (CREATED, REJECTED), (CREATED, EXPIRED), (CREATED, FAILED),
    (VALIDATED, SAFETY_DENIED), (VALIDATED, READY), (VALIDATED, REJECTED),
    (VALIDATED, EXPIRED), (VALIDATED, FAILED),
    (READY, SUBMITTING), (READY, MODIFY_PENDING), (READY, CANCEL_PENDING),
    (READY, CLOSE_PENDING), (READY, EXPIRED), (READY, FAILED),
    (SUBMITTING, SUBMITTED), (SUBMITTING, REJECTED), (SUBMITTING, UNKNOWN), (SUBMITTING, FAILED),
    (SUBMITTED, ACKNOWLEDGED), (SUBMITTED, PARTIALLY_FILLED), (SUBMITTED, FILLED),
    (SUBMITTED, REJECTED), (SUBMITTED, EXPIRED), (SUBMITTED, UNKNOWN), (SUBMITTED, FAILED),
    (ACKNOWLEDGED, PARTIALLY_FILLED), (ACKNOWLEDGED, FILLED), (ACKNOWLEDGED, MODIFY_PENDING),
    (ACKNOWLEDGED, CANCEL_PENDING), (ACKNOWLEDGED, CLOSE_PENDING), (ACKNOWLEDGED, EXPIRED),
    (ACKNOWLEDGED, UNKNOWN), (ACKNOWLEDGED, FAILED),
    # LIVE-2: a broker-acknowledged market order whose position is confirmed open.
    (ACKNOWLEDGED, OPEN), (PARTIALLY_FILLED, OPEN),
    # LIVE-3: manual-management operations. The broker ACKNOWLEDGEMENT of a
    # modify/cancel/close request is NOT the final state — reconciliation
    # confirms the observed broker change into the terminal state.
    (MODIFY_PENDING, ACKNOWLEDGED), (CANCEL_PENDING, ACKNOWLEDGED),
    (CLOSE_PENDING, ACKNOWLEDGED),
    (ACKNOWLEDGED, MODIFIED), (ACKNOWLEDGED, CANCELLED), (ACKNOWLEDGED, CLOSED),
    (PARTIALLY_FILLED, PARTIALLY_FILLED), (PARTIALLY_FILLED, FILLED),
    (PARTIALLY_FILLED, CANCEL_PENDING), (PARTIALLY_FILLED, CLOSE_PENDING),
    (PARTIALLY_FILLED, UNKNOWN), (PARTIALLY_FILLED, FAILED),
    (MODIFY_PENDING, MODIFIED), (MODIFY_PENDING, REJECTED), (MODIFY_PENDING, UNKNOWN), (MODIFY_PENDING, FAILED),
    (CANCEL_PENDING, CANCELLED), (CANCEL_PENDING, REJECTED), (CANCEL_PENDING, UNKNOWN), (CANCEL_PENDING, FAILED),
    (CLOSE_PENDING, CLOSED), (CLOSE_PENDING, REJECTED), (CLOSE_PENDING, UNKNOWN), (CLOSE_PENDING, FAILED),
    (UNKNOWN, RECONCILIATION_REQUIRED), (UNKNOWN, FAILED),
    (RECONCILIATION_REQUIRED, RECONCILED), (RECONCILIATION_REQUIRED, FAILED),
    # `reconciled` resolves onward ONLY with evidence (enforced in transition()).
    (RECONCILED, ACKNOWLEDGED), (RECONCILED, PARTIALLY_FILLED), (RECONCILED, FILLED),
    (RECONCILED, OPEN),
    (RECONCILED, MODIFIED), (RECONCILED, CANCELLED), (RECONCILED, CLOSED), (RECONCILED, FAILED),
})

#: States where the intent may have reached an adapter — restart recovery must
#: treat these as unknown, never as complete.
IN_FLIGHT_STATES = frozenset({
    SUBMITTING, SUBMITTED, ACKNOWLEDGED, PARTIALLY_FILLED,
    MODIFY_PENDING, CANCEL_PENDING, CLOSE_PENDING,
})

#: States where it is CERTAIN no adapter was contacted.
PRE_DISPATCH_STATES = frozenset({CREATED, VALIDATED, READY})

#: Healthy states reachable from `reconciled` — each requires explicit evidence.
_EVIDENCE_REQUIRED_TARGETS = frozenset({
    ACKNOWLEDGED, PARTIALLY_FILLED, FILLED, OPEN, MODIFIED, CANCELLED, CLOSED,
})


class LifecycleError(ValueError):
    """An invalid lifecycle operation. `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True)
class Transition:
    """One immutable lifecycle transition record — the unit of the append-only
    durable history. Every transition carries its reason and evidence."""
    intent_id: str
    from_state: str
    to_state: str
    at: str
    reason: str
    evidence: str = ""
    broker_ref: str | None = None


def _parse_at(at: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(at).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        raise LifecycleError("invalid_timestamp", f"unparseable transition time: {at!r}")


def transition(intent_id: str, from_state: str, to_state: str, *, at: str,
               reason: str, evidence: str = "", broker_ref: str | None = None,
               previous_at: str | None = None) -> Transition:
    """Validate and build ONE lifecycle transition. Pure and deterministic.

    Raises `LifecycleError` when:
      * either state is unknown to the vocabulary
      * the pair is not in the explicit allowed-transition table
      * the source state is terminal (terminal states are protected)
      * the timestamp is unparseable or would move time backwards
      * the transition resolves `reconciled` to a healthy state without evidence
      * an `unknown` intent is aimed at a healthy state directly
    """
    if from_state not in ALL_STATES:
        raise LifecycleError("unknown_state", from_state)
    if to_state not in ALL_STATES:
        raise LifecycleError("unknown_state", to_state)
    if from_state in TERMINAL_STATES:
        raise LifecycleError("terminal_state_immutable",
                             f"{from_state} is terminal; no further transition is legal")
    if (from_state, to_state) not in ALLOWED_TRANSITIONS:
        raise LifecycleError("invalid_transition", f"{from_state} -> {to_state}")
    if not reason:
        raise LifecycleError("reason_required", "every transition must carry a reason")
    if from_state == RECONCILED and to_state in _EVIDENCE_REQUIRED_TARGETS and not evidence:
        raise LifecycleError("evidence_required",
                             f"resolving reconciled -> {to_state} requires evidence")
    ts = _parse_at(at)
    if previous_at is not None and ts < _parse_at(previous_at):
        raise LifecycleError("non_monotonic_timestamp",
                             f"{at} is before the previous transition at {previous_at}")
    return Transition(intent_id=intent_id, from_state=from_state, to_state=to_state,
                      at=at, reason=reason, evidence=evidence, broker_ref=broker_ref)


def recovery_plan(state: str) -> tuple[str, str] | None:
    """The deterministic restart-recovery action for one persisted state.

    Returns (target_state, reason) or None when no action is needed (terminal
    states and states already awaiting reconciliation are left untouched).
    Recovery NEVER fabricates completion: pre-dispatch intents fail closed,
    possibly-dispatched intents become unknown.
    """
    if state in TERMINAL_STATES or state in (RECONCILIATION_REQUIRED, RECONCILED, UNKNOWN):
        return None
    if state in PRE_DISPATCH_STATES:
        return (FAILED, "restart_before_dispatch")
    if state in IN_FLIGHT_STATES:
        return (UNKNOWN, "restart_recovery_in_flight")
    return None  # pragma: no cover — vocabulary is closed above
