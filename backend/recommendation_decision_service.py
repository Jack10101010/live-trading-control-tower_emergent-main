"""LIVE-4E — the internal RecommendationDecisionService.

The ONE write path for an operator decision on a Recommendation. It validates
transition legality, enforces optimistic concurrency, attributes the actor,
stamps the time, records an optional note and a required reason, and appends an
immutable audit event — all atomically.

WHAT ACCEPTING DOES
    It records that an operator accepted the proposal. That is the entire
    effect. This module contains no broker adapter, no order submission, no
    authorization grant, no execution-mode change and no scheduler. The
    structural tests assert the absence of every one of those, because the whole
    point of the slice is that a human clicking Accept cannot move money.

WHY ACCEPT DOES NOT ADVANCE TO INTENT_CREATED
    `ACCEPTED -> INTENT_CREATED` is a legal DOMAIN transition, but it is not an
    OPERATOR transition: only the execution boundary may record that an intent
    now exists, because only it knows one does. If this service advanced the
    status, an accepted-but-never-submitted proposal would claim an intent that
    was never created.

CONCURRENCY
    Two operators deciding at once is the case this is built around. The
    compare-and-set happens inside the store's `BEGIN IMMEDIATE` transaction, so
    exactly one decision commits and the other is TOLD it lost
    (`version_conflict`). Neither is silently dropped and neither is silently
    "succeeded".
"""

from __future__ import annotations

import recommendation_authorization as auth
import recommendation_domain as rd
import recommendation_store as rs

#: Stable machine codes for every refusal this service can produce.
CONFLICT_VERSION = "version_conflict"
CONFLICT_SEQUENCE = "decision_sequence_conflict"
CONFLICT_IDEMPOTENCY = "idempotency_key_reused"
REJECT_NOT_FOUND = "recommendation_not_found"
REJECT_INVALID_TRANSITION = "invalid_transition"
REJECT_NOT_DECIDABLE = "not_decidable"
REJECT_REASON_REQUIRED = "reason_required"
REJECT_UNKNOWN_DECISION = "unknown_decision_type"

#: Which status each operator decision produces. A strict subset of
#: `rd.DECISION_STATUS`, and the only mapping this service will honour.
OPERATOR_DECISION_STATUS = {
    rd.RecommendationDecisionType.ACCEPT: rd.RecommendationStatus.ACCEPTED,
    rd.RecommendationDecisionType.REJECT: rd.RecommendationStatus.REJECTED,
    rd.RecommendationDecisionType.EXPIRE: rd.RecommendationStatus.EXPIRED,
}


class DecisionServiceError(RuntimeError):
    """A refused decision. `reason` is a stable machine code; `detail` is safe
    to show an operator and never contains credential material."""

    def __init__(self, reason: str, detail: str = "", *, conflict: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail
        #: True when the caller lost a race or replayed inconsistently, i.e. the
        #: request was well-formed but the world moved. Maps to HTTP 409.
        self.conflict = conflict


class RecommendationDecisionService:
    """Internal service. Constructed with explicit collaborators — it reaches
    for no global, opens no connection of its own and reads no clock."""

    def __init__(self, *, store_fn, now_iso_fn, execution_mode_fn=None,
                 audit_fn=None):
        self._store_fn = store_fn
        self._now_iso = now_iso_fn
        self._execution_mode = execution_mode_fn or (lambda: None)
        #: Optional sink for the SYSTEM-WIDE journal, so a decision is visible
        #: in the operational event stream as well as its own history. A failing
        #: sink must never lose a committed decision (see `_journal`).
        self._audit = audit_fn

    def _store(self) -> rs.RecommendationStore:
        store = self._store_fn()
        if store is None:
            raise DecisionServiceError("recommendation_store_unavailable")
        return store

    # ── the one write path ───────────────────────────────────────────────────
    def decide(self, recommendation_id: str, *, decision_type: str,
               principal: auth.DecisionPrincipal, reason: str,
               expected_version: int | None = None, note: str | None = None,
               idempotency_key: str | None = None):
        """Record ONE operator decision. Returns `(recommendation, decision)`.

        Every precondition is checked before anything is written, and the write
        itself is atomic, so a refusal leaves the proposal exactly as it was.
        """
        auth.assert_decision_allowed(principal, decision_type=decision_type)
        if decision_type not in OPERATOR_DECISION_STATUS:
            raise DecisionServiceError(REJECT_UNKNOWN_DECISION, str(decision_type))
        if not (isinstance(reason, str) and reason.strip()):
            raise DecisionServiceError(
                REJECT_REASON_REQUIRED,
                "a decision must record why it was taken")
        to_status = OPERATOR_DECISION_STATUS[decision_type]

        store = self._store()
        current = store.get_recommendation(recommendation_id)
        if current is None:
            raise DecisionServiceError(REJECT_NOT_FOUND, recommendation_id)

        # Idempotency is resolved BEFORE any state precondition. A retry that
        # follows a SUCCESSFUL commit must replay the original outcome: by then
        # the proposal is terminal, and judging the retry against the new state
        # would tell the caller its own successful decision was illegal.
        if idempotency_key:
            prior = store.find_decision_by_idempotency_key(
                recommendation_id, idempotency_key)
            if prior is not None:
                if prior.decision_type != decision_type:
                    raise DecisionServiceError(
                        CONFLICT_IDEMPOTENCY,
                        f"key already recorded {prior.decision_type}",
                        conflict=True)
                return current, prior
        # A clear, specific refusal beats a generic invalid_transition: the UI
        # shows the operator WHY the proposal is closed to decisions.
        blocked = current.undecidable_reason
        if blocked is not None:
            raise DecisionServiceError(REJECT_NOT_DECIDABLE, blocked)
        if not rd.operator_can_transition(current.status, to_status):
            raise DecisionServiceError(REJECT_INVALID_TRANSITION,
                                       f"{current.status} -> {to_status}")
        try:
            recommendation, decision = store.record_operator_decision(
                recommendation_id, decision_type=decision_type,
                to_status=to_status, actor_type=principal.actor_type,
                actor_id=principal.actor_id, reason=reason.strip(),
                at=self._now_iso(), expected_version=expected_version,
                note=(note.strip() if isinstance(note, str) and note.strip()
                      else None),
                correlation_id=principal.correlation_id,
                identity_assurance=principal.identity_assurance,
                execution_mode=self._execution_mode(),
                idempotency_key=idempotency_key)
        except rs.RecommendationStoreError as exc:
            raise DecisionServiceError(
                exc.reason, exc.detail,
                conflict=exc.reason in (CONFLICT_VERSION, CONFLICT_SEQUENCE,
                                        CONFLICT_IDEMPOTENCY)) from exc
        except rd.RecommendationError as exc:
            raise DecisionServiceError(exc.reason, exc.detail) from exc

        self._journal(recommendation, decision, principal)
        return recommendation, decision

    # Convenience wrappers. They exist so a caller states its intent in one
    # word and cannot pass an arbitrary decision type by accident.
    def accept(self, recommendation_id: str, **kw):
        """Record an ACCEPT. This submits NOTHING — see the module docstring."""
        return self.decide(recommendation_id,
                           decision_type=rd.RecommendationDecisionType.ACCEPT, **kw)

    def reject(self, recommendation_id: str, **kw):
        return self.decide(recommendation_id,
                           decision_type=rd.RecommendationDecisionType.REJECT, **kw)

    def expire(self, recommendation_id: str, **kw):
        return self.decide(recommendation_id,
                           decision_type=rd.RecommendationDecisionType.EXPIRE, **kw)

    # ── audit ────────────────────────────────────────────────────────────────
    def _journal(self, recommendation, decision, principal) -> None:
        """Mirror the decision into the system-wide journal, best effort.

        The decision is ALREADY committed to its own append-only history by the
        time this runs. A journal failure must therefore never propagate: losing
        the mirror is a visibility problem, losing the decision would be a
        correctness one.
        """
        if self._audit is None:
            return
        try:
            self._audit({
                "category": "recommendation",
                "code": f"RECOMMENDATION_{decision.decision_type}",
                "humanExplanation": (
                    f"Operator decision {decision.decision_type} recorded on "
                    f"recommendation {recommendation.recommendation_id} "
                    f"(now {recommendation.status}). This records a decision "
                    f"only; no order was submitted, modified or executed."),
                "who": rd.pseudonymize_actor(principal.actor_id),
                "causedBy": decision.correlation_id or decision.decision_id,
                "before": None,
                "after": {"status": recommendation.status,
                          "version": recommendation.version,
                          "identityAssurance": decision.identity_assurance},
            })
        except Exception:                                   # noqa: BLE001
            pass

    # ── read helpers (no mutation) ───────────────────────────────────────────
    def history(self, recommendation_id: str) -> list:
        """Decision history, NEWEST FIRST — the order the UI displays."""
        return sorted(self._store().decisions(recommendation_id),
                      key=lambda d: (d.sequence, d.occurred_at, d.decision_id),
                      reverse=True)

    def current_version(self, recommendation_id: str) -> int:
        return self._store().current_version(recommendation_id)
