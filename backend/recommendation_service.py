"""LIVE-4D — the narrowly scoped RECOMMENDATION SERVICE.

The ONLY write path into the recommendation domain. It validates lineage,
enforces legal lifecycle transitions, records immutable decisions and returns
the updated read model.

WHAT IT MUST NEVER DO (structurally pinned by tests)
    call a broker write, submit an execution command, mutate execution mode,
    grant authorization, create strategy signals, or mutate ledger accounting.

ACCEPTANCE IS NOT EXECUTION
    Recording an ACCEPT decision moves the proposal to ACCEPTED and stops.
    It submits nothing. Execution still requires the existing authorization,
    execution-mode and safety gates, which this module never touches and never
    consults on behalf of a caller.

WRITE-SURFACE POLICY (audited, PART 14)
    The existing operator command vocabulary (`command_channel.ALLOWED_TYPES`)
    is the READ-ONLY trio and contains no decision verb, so LIVE-4D adds NO
    public write route. Decisions enter through this service, callable from
    tests and future orchestration behind the existing authenticated boundary.
    There is deliberately no browser-to-broker path and no ACCEPT-to-submit
    coupling.
"""

from __future__ import annotations

from typing import Any

import recommendation_domain as rd
import recommendation_store as rs

# ── conflict / warning codes (visible, never normalized away) ────────────────
CONFLICT_SCENARIO_MISSING = "scenario_unavailable"
CONFLICT_INSTRUMENT_MISMATCH = "instrument_differs_from_scenario"
CONFLICT_DIRECTION_MISMATCH = "direction_differs_from_scenario"
CONFLICT_NODE_MISMATCH = "node_differs_from_scenario"
CONFLICT_ACCOUNT_MISMATCH = "account_differs_from_scenario"
CONFLICT_DECISIONS = "conflicting_decisions"
CONFLICT_INTENT_CLAIMED = "intent_linked_to_another_recommendation"
CONFLICT_SUPERSESSION_CYCLE = "supersession_cycle"
CONFLICT_EXECUTED_WITHOUT_INTENT = "execution_observed_without_linked_intent"
WARN_INTENT_UNLINKED = "no_linked_intent"
WARN_PAST_DUE = "past_expiry_not_yet_recorded"
WARN_ACCEPTED_WITHOUT_AUTHORIZATION = "accepted_without_authorization_reference"

#: Decision types whose policy requires an authorization reference for audit.
_REQUIRE_AUTHORIZATION = frozenset({rd.RecommendationDecisionType.ACCEPT,
                                    rd.RecommendationDecisionType.AUTO_ACCEPT})


class RecommendationServiceError(ValueError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_scenario_lineage(recommendation: rd.Recommendation,
                              scenario: Any) -> tuple:
    """Compare a proposal against its Scenario. Returns explicit conflicts —
    contradictions are surfaced, never normalized away."""
    if scenario is None:
        return (CONFLICT_SCENARIO_MISSING,)
    conflicts: list = []
    if scenario.instrument != recommendation.instrument:
        conflicts.append(
            f"{CONFLICT_INSTRUMENT_MISMATCH}: {scenario.instrument} != "
            f"{recommendation.instrument}")
    if scenario.direction != recommendation.direction:
        conflicts.append(
            f"{CONFLICT_DIRECTION_MISMATCH}: {scenario.direction} != "
            f"{recommendation.direction}")
    if recommendation.node_id and scenario.node_id \
            and scenario.node_id != recommendation.node_id:
        conflicts.append(CONFLICT_NODE_MISMATCH)
    if recommendation.account_fingerprint and scenario.account_fingerprint \
            and scenario.account_fingerprint != recommendation.account_fingerprint:
        conflicts.append(CONFLICT_ACCOUNT_MISMATCH)
    return tuple(conflicts)


class RecommendationService:
    """The one write path. Pure over its injected store and scenario reader."""

    def __init__(self, *, store_fn, scenario_reader=None, now_iso_fn,
                 execution_mode_fn=None):
        self._store_fn = store_fn
        self._scenario_reader = scenario_reader or (lambda _sid: None)
        self._now_iso = now_iso_fn
        self._execution_mode = execution_mode_fn or (lambda: None)

    def _store(self) -> rs.RecommendationStore:
        store = self._store_fn()
        if store is None:
            raise RecommendationServiceError("recommendation_store_unavailable")
        return store

    # ── creation ─────────────────────────────────────────────────────────────
    def create(self, recommendation: rd.Recommendation, *,
               require_scenario: bool = True) -> rd.Recommendation:
        """Record a NEW proposal. The Scenario must exist and must agree on
        instrument and direction — creating a Recommendation NEVER advances or
        mutates the Scenario's own lifecycle."""
        store = self._store()
        scenario = self._scenario_reader(recommendation.scenario_id)
        if scenario is None and require_scenario:
            raise RecommendationServiceError(CONFLICT_SCENARIO_MISSING,
                                             recommendation.scenario_id)
        conflicts = validate_scenario_lineage(recommendation, scenario) \
            if scenario is not None else ()
        blocking = [c for c in conflicts
                    if c.startswith((CONFLICT_INSTRUMENT_MISMATCH,
                                     CONFLICT_DIRECTION_MISMATCH))]
        if blocking:
            raise RecommendationServiceError("scenario_lineage_conflict",
                                             "; ".join(blocking))
        return store.create_recommendation(recommendation)

    def propose(self, recommendation_id: str, *, reason: str = "proposed",
                actor_id: str | None = None) -> rd.Recommendation:
        """Move a DRAFT proposal to PROPOSED."""
        return self._decide(recommendation_id,
                            decision_type=rd.RecommendationDecisionType.DEFER,
                            to_status=rd.RecommendationStatus.PROPOSED,
                            actor_type=rd.ActorType.SYSTEM, actor_id=actor_id,
                            reason=reason, authorization_reference=None)

    # ── decisions ────────────────────────────────────────────────────────────
    def record_decision(self, recommendation_id: str, *, decision_type: str,
                        actor_type: str, actor_id: str | None, reason: str,
                        authorization_reference: str | None = None,
                        metadata: dict | None = None) -> rd.Recommendation:
        """Record ONE immutable decision and apply its lifecycle effect.

        ACCEPT moves the proposal to ACCEPTED and NOTHING ELSE — no order is
        submitted, no authorization is granted, no execution mode is changed."""
        if decision_type not in rd.ALL_DECISION_TYPES:
            raise RecommendationServiceError("unknown_decision_type",
                                             str(decision_type))
        to_status = rd.DECISION_STATUS.get(decision_type)
        return self._decide(recommendation_id, decision_type=decision_type,
                            to_status=to_status, actor_type=actor_type,
                            actor_id=actor_id, reason=reason,
                            authorization_reference=authorization_reference,
                            metadata=metadata)

    def _decide(self, recommendation_id: str, *, decision_type: str,
                to_status: str | None, actor_type: str, actor_id: str | None,
                reason: str, authorization_reference: str | None,
                metadata: dict | None = None) -> rd.Recommendation:
        store = self._store()
        current = store.get_recommendation(recommendation_id)
        if current is None:
            raise RecommendationServiceError("recommendation_not_found",
                                             recommendation_id)
        if not (isinstance(reason, str) and reason.strip()):
            raise RecommendationServiceError("reason_required")
        if to_status is not None and to_status != current.status \
                and not rd.can_transition(current.status, to_status):
            raise RecommendationServiceError(
                "invalid_transition", f"{current.status} -> {to_status}")
        now = self._now_iso()
        sequence = store.next_decision_sequence(recommendation_id)
        decision = rd.RecommendationDecision(
            decision_id=rd.new_decision_id(recommendation_id, sequence),
            recommendation_id=recommendation_id, decision_type=decision_type,
            actor_type=actor_type, actor_id=actor_id, occurred_at=now,
            sequence=sequence, reason=reason,
            authorization_reference=authorization_reference,
            execution_mode=self._execution_mode(),
            provenance=actor_type.lower(), metadata=dict(metadata or {}))
        return store.record_decision(recommendation_id, decision=decision,
                                     to_status=to_status, at=now)

    # ── linkage ──────────────────────────────────────────────────────────────
    def link_intent(self, recommendation_id: str, intent_id: str) -> rd.Recommendation:
        """Record an EXPLICIT intent link. One Recommendation may link to many
        Intents; an Intent already claimed by ANOTHER Recommendation is a
        visible conflict, never a silent reassignment."""
        store = self._store()
        claimed = [r for r in store.list_recommendations(linked_intent=intent_id,
                                                         limit=10)
                   if r.recommendation_id != recommendation_id]
        if claimed:
            raise RecommendationServiceError(
                CONFLICT_INTENT_CLAIMED,
                f"{intent_id} already linked to {claimed[0].recommendation_id}")
        return store.link_intent(recommendation_id, intent_id, at=self._now_iso())

    def observe_execution(self, recommendation_id: str, *, status: str,
                          reason: str, evidence: dict | None = None) -> rd.Recommendation:
        """Mirror OBSERVED execution progress onto the proposal's disposition.
        The execution store and ledger remain authoritative."""
        return self._store().observe_execution(
            recommendation_id, status=status, at=self._now_iso(), reason=reason,
            evidence=evidence)

    # ── expiry (explicit, never at read time) ────────────────────────────────
    def expire_due(self, *, limit: int = 200) -> list:
        """Record expiry for proposals whose expiry has demonstrably passed.

        Explicit and bounded: a projection may REPORT past-due, but only this
        call PERSISTS `EXPIRED`. It cancels no broker order and invalidates no
        Scenario, and it is not wired to any background loop."""
        store = self._store()
        now = self._now_iso()
        expired: list = []
        for item in store.list_active_recommendations(limit=limit):
            if item.past_due(now):
                expired.append(store.mark_expired(item.recommendation_id, at=now))
        return expired

    def withdraw(self, recommendation_id: str, *, reason: str,
                 actor_id: str | None = None) -> rd.Recommendation:
        return self._decide(recommendation_id,
                            decision_type=rd.RecommendationDecisionType.WITHDRAW,
                            to_status=rd.RecommendationStatus.WITHDRAWN,
                            actor_type=rd.ActorType.OPERATOR, actor_id=actor_id,
                            reason=reason, authorization_reference=None)

    def supersede(self, previous_id: str, replacement: rd.Recommendation, *,
                  reason: str) -> tuple:
        """Replace a proposal with a revised one, preserving all history.
        Linked Intents stay attached to the Recommendation that created them."""
        store = self._store()
        return store.supersede_recommendation(previous_id, replacement,
                                              at=self._now_iso(), reason=reason)

    # ── read-side conflict/warning derivation ────────────────────────────────
    def warnings_for(self, recommendation: rd.Recommendation, *, now: str) -> tuple:
        """Explicit, derived warnings and conflicts for one proposal."""
        store = self._store()
        warnings: list = []
        scenario = self._scenario_reader(recommendation.scenario_id)
        warnings.extend(validate_scenario_lineage(recommendation, scenario))
        decisions = store.decisions(recommendation.recommendation_id)
        warnings.extend(rd.conflicting_decisions(decisions))
        if not recommendation.linked_intent_ids:
            if recommendation.status in (rd.RecommendationStatus.EXECUTED,
                                         rd.RecommendationStatus.PARTIALLY_EXECUTED):
                warnings.append(CONFLICT_EXECUTED_WITHOUT_INTENT)
            elif recommendation.status == rd.RecommendationStatus.INTENT_CREATED:
                warnings.append(WARN_INTENT_UNLINKED)
        if recommendation.past_due(now):
            warnings.append(WARN_PAST_DUE)
        if recommendation.status == rd.RecommendationStatus.ACCEPTED:
            latest = rd.latest_decision(decisions)
            if latest is not None and latest.decision_type in _REQUIRE_AUTHORIZATION \
                    and not latest.authorization_reference:
                warnings.append(WARN_ACCEPTED_WITHOUT_AUTHORIZATION)
        if recommendation.superseded_by == recommendation.recommendation_id:
            warnings.append(CONFLICT_SUPERSESSION_CYCLE)
        return tuple(sorted(set(warnings)))


# ─────────────────────────────────────────────────────────────────────────────
# Fixture adapter (PART 8) — isolated, deterministic, and HONEST.
#
# AUDIT FINDING: the fixture world's `recommendations` are NOT trade proposals.
# They are POLICY-CHANGE proposals — `proposedChange: {field, from, to}` with
# `resultingPackageVersion`, `promotionHistory` and research evidence, and
# statuses like `deployed` / `validating`. They carry no entry price, no stop,
# no quantity and no order type.
#
# They are therefore imported as EVIDENCE with `source=IMPORT`, trade terms
# explicitly UNAVAILABLE, the fixture id preserved, and a warning recording
# what they actually are. Nothing here pretends the fixture world is live
# strategy output, and no trade terms are synthesised.
# ─────────────────────────────────────────────────────────────────────────────

FIXTURE_KIND_POLICY_CHANGE = "policy_change_proposal"
FIXTURE_WARNING = ("imported fixture evidence: a POLICY-CHANGE proposal, not a "
                   "trade proposal; trade terms are unavailable")


def adapt_fixture_recommendation(raw: dict, *, scenario_id_for_key,
                                 node_id: str | None = None) -> rd.Recommendation:
    """Map ONE fixture recommendation into a canonical Recommendation.

    Malformed evidence fails VISIBLY (`RecommendationServiceError`) rather than
    being silently skipped or padded with invented terms."""
    if not isinstance(raw, dict):
        raise RecommendationServiceError("malformed_fixture_recommendation",
                                         "expected an object")
    fixture_id = raw.get("recommendationId")
    scenario_key = raw.get("scenarioKey")
    if not (isinstance(fixture_id, str) and fixture_id.strip()):
        raise RecommendationServiceError("malformed_fixture_recommendation",
                                         "recommendationId is required")
    if not (isinstance(scenario_key, str) and scenario_key.strip()):
        raise RecommendationServiceError("malformed_fixture_recommendation",
                                         "scenarioKey is required")
    created_at = raw.get("createdAt")
    if not (isinstance(created_at, str) and created_at.strip()):
        raise RecommendationServiceError("malformed_fixture_recommendation",
                                         "createdAt is required")
    # Canonical Scenario identity comes from the ONE scenario identity rule —
    # this adapter never invents its own scheme.
    scenario_id, instrument, direction = scenario_id_for_key(scenario_key)

    evidence = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
    confidence = evidence.get("confidence")
    if confidence is not None and not isinstance(confidence, (int, float)):
        confidence = None
    change = raw.get("proposedChange") if isinstance(raw.get("proposedChange"), dict) else {}
    rationale = (f"policy change: {change.get('field')} "
                 f"{change.get('from')} -> {change.get('to')}") if change else None

    terms = rd.RecommendationTerms(
        # NO trade terms are synthesised: the fixture records none.
        execution=rd.RecommendationExecutionTerms(),
        risk=rd.RecommendationRiskTerms(),
        rationale=rationale,
        confidence=float(confidence) if confidence is not None else None,
        tags=(FIXTURE_KIND_POLICY_CHANGE,),
        metadata={"fixtureRecommendationId": fixture_id,
                  "fixtureStatus": raw.get("status"),
                  "fixtureScenarioKey": scenario_key,
                  "proposedChange": change,
                  "kind": FIXTURE_KIND_POLICY_CHANGE,
                  "importWarning": FIXTURE_WARNING})
    return rd.new_recommendation(
        scenario_id=scenario_id, instrument=instrument, direction=direction,
        source=rd.RecommendationSource.IMPORT, created_at=created_at, terms=terms,
        discriminator=fixture_id, node_id=node_id,
        source_reference=fixture_id, provenance="fixture-import")


def import_fixture_recommendations(raws, *, service: RecommendationService,
                                   scenario_id_for_key,
                                   node_id: str | None = None) -> dict:
    """Deterministically import fixture recommendations as evidence.

    Bounded and explicit: it creates NO Scenario. A recommendation whose
    Scenario does not exist is reported as skipped with its reason, never
    silently materialized."""
    imported: list = []
    skipped: list = []
    for raw in sorted(raws, key=lambda r: str((r or {}).get("recommendationId") or "")):
        try:
            candidate = adapt_fixture_recommendation(
                raw, scenario_id_for_key=scenario_id_for_key, node_id=node_id)
        except RecommendationServiceError as exc:
            skipped.append({"fixtureId": (raw or {}).get("recommendationId"),
                            "reason": exc.reason, "detail": exc.detail})
            continue
        try:
            imported.append(service.create(candidate))
        except RecommendationServiceError as exc:
            skipped.append({"fixtureId": candidate.source_reference,
                            "reason": exc.reason, "detail": exc.detail})
    return {"imported": [r.recommendation_id for r in imported],
            "skipped": skipped, "kind": FIXTURE_KIND_POLICY_CHANGE,
            "warning": FIXTURE_WARNING}
