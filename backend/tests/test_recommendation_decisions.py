"""LIVE-4E — the operator decision surface.

Proves: a closed operator transition table that cannot reach any execution
state, atomic decisions, optimistic concurrency where exactly one of two racing
operators wins and the other is TOLD, immutable newest-first history, honest
identity assurance, no anonymous mutation, a minimal write API with real
conflict responses — and that acceptance still executes nothing.

The two hazards this slice fixes were reproduced against LIVE-4D before the fix:
a losing concurrent decision was reported as a SUCCESS and left no trace, and a
decision committed in a separate transaction from its event. Both are pinned
here so they cannot return.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import recommendation_authorization as auth                       # noqa: E402
import recommendation_decision_service as rds                     # noqa: E402
import recommendation_domain as rd                                # noqa: E402
import recommendation_store as rstore                             # noqa: E402
import server                                                     # noqa: E402
from test_recommendation_domain import statements_only            # noqa: E402

client = TestClient(server.app)

T0 = "2026-07-27T12:00:00Z"
T1 = "2026-07-27T12:01:00Z"
NOW = "2026-07-27T12:05:00Z"
SCN = "scn_" + "a" * 16
HEADERS = {"X-Operator-Id": "op_jane", "X-Correlation-Id": "req_1"}


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    monkeypatch.setattr(server, "RECOMMENDATION_DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", False)
    yield
    for name in ("recommendation_state.db", "execution_state.db"):
        assert not (BACKEND_DIR / name).exists(), f"a test created backend/{name}"


# ── builders ─────────────────────────────────────────────────────────────────

def _store(tmp_path, name="d.db"):
    return rstore.RecommendationStore(tmp_path / name)


def _terms():
    return rd.RecommendationTerms(
        execution=rd.RecommendationExecutionTerms(order_type="market", quantity=0.5),
        risk=rd.RecommendationRiskTerms(stop_loss=1.0950, take_profit=1.1100))


def _seed(store, *, discriminator="", propose=True):
    """A recommendation in a decidable state, created the way production does."""
    r = store.create_recommendation(rd.new_recommendation(
        scenario_id=SCN, instrument="EURUSD", direction="long",
        source=rd.RecommendationSource.OPERATOR, created_at=T0,
        terms=_terms(), node_id="node-1", discriminator=discriminator))
    if propose:
        store._status_write(r.recommendation_id, rd.RecommendationStatus.PROPOSED,
                            rd.RecommendationEventType.PROPOSED, at=T1,
                            reason="proposed")
    return r.recommendation_id


def _service(store, now=NOW, audit=None):
    return rds.RecommendationDecisionService(
        store_fn=lambda: store, now_iso_fn=lambda: now,
        execution_mode_fn=lambda: "observe", audit_fn=audit)


def _principal(actor="op_jane", authenticated=False, correlation=None):
    return auth.resolve_principal(asserted_actor=actor,
                                  boundary_authenticated=authenticated,
                                  correlation_id=correlation)


# ── PART 2: the operator transition table ────────────────────────────────────

def test_operator_transitions_are_exactly_the_three_decisions():
    """The prompt's table, reconciled with production: an operator decision may
    produce ACCEPTED, REJECTED or EXPIRED and nothing else."""
    assert rd.OPERATOR_REACHABLE_STATUSES == frozenset({
        rd.RecommendationStatus.ACCEPTED, rd.RecommendationStatus.REJECTED,
        rd.RecommendationStatus.EXPIRED})
    assert rd.OPERATOR_DECISION_TYPES == frozenset({"ACCEPT", "REJECT", "EXPIRE"})
    assert rd.DECIDABLE_STATUSES == frozenset({
        rd.RecommendationStatus.PROPOSED, rd.RecommendationStatus.PENDING_DECISION})


def test_no_operator_decision_can_reach_an_execution_state():
    """THE structural guarantee of this slice. EXECUTED, PARTIALLY_EXECUTED,
    INTENT_CREATED and FAILED are OBSERVATION states: an operator cannot move a
    proposal into any state that asserts a trade happened."""
    forbidden = {rd.RecommendationStatus.INTENT_CREATED,
                 rd.RecommendationStatus.PARTIALLY_EXECUTED,
                 rd.RecommendationStatus.EXECUTED, rd.RecommendationStatus.FAILED}
    assert rd.OPERATOR_REACHABLE_STATUSES.isdisjoint(forbidden)
    for src in rd.ALL_STATUSES:
        for dst in forbidden:
            assert not rd.operator_can_transition(src, dst), f"{src} -> {dst}"


def test_the_operator_table_is_a_strict_subset_of_the_domain_table():
    """Every operator transition is legal in the domain; the reverse is false —
    which is what makes the operator surface narrower, not different."""
    operator_pairs = {(src, dst) for src, targets in rd.OPERATOR_TRANSITIONS.items()
                      for dst in targets}
    assert operator_pairs
    assert operator_pairs <= rd.ALLOWED_TRANSITIONS
    assert operator_pairs != rd.ALLOWED_TRANSITIONS


@pytest.mark.parametrize("status,reason", [
    ("DRAFT", "not_yet_proposed"),
    ("ACCEPTED", "already_accepted"),
    ("REJECTED", "already_terminal:REJECTED"),
    ("EXPIRED", "already_terminal:EXPIRED"),
    ("SUPERSEDED", "already_terminal:SUPERSEDED"),
    ("EXECUTED", "already_terminal:EXECUTED"),
    ("INTENT_CREATED", "execution_in_progress:INTENT_CREATED"),
])
def test_undecidable_states_explain_themselves(status, reason):
    assert rd.undecidable_reason(status) == reason


@pytest.mark.parametrize("status", ["PROPOSED", "PENDING_DECISION"])
def test_decidable_states_have_no_blocking_reason(status):
    assert rd.undecidable_reason(status) is None


# ── PART 3/4: the decision service and its audit record ──────────────────────

def test_accept_records_a_decision_and_nothing_else(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    before_version = store.current_version(rid)

    recommendation, decision = service.accept(
        rid, principal=_principal(correlation="req_9"), reason="conditions met",
        note="clean BOS retest", expected_version=before_version)

    assert recommendation.status == rd.RecommendationStatus.ACCEPTED
    # Acceptance is NOT execution.
    assert recommendation.outcome == rd.RecommendationOutcome.NOT_EXECUTED
    assert recommendation.linked_intent_ids == ()
    assert recommendation.version == before_version + 1
    # Everything PART 4 requires is on the record.
    assert decision.decision_type == "ACCEPT"
    assert decision.actor_type == rd.ActorType.OPERATOR
    assert decision.occurred_at == NOW
    assert decision.reason == "conditions met"
    assert decision.note == "clean BOS retest"
    assert decision.against_version == before_version
    assert decision.sequence == before_version + 1
    assert decision.correlation_id == "req_9"
    assert decision.execution_mode == "observe"


def test_every_decision_records_previous_and_new_status(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    previous = store.get_recommendation(rid).status
    recommendation, _ = _service(store).reject(
        rid, principal=_principal(), reason="structure invalidated")
    event = store.history(rid)[-1]
    assert previous == rd.RecommendationStatus.PROPOSED
    assert event.payload["status"] == recommendation.status == "REJECTED"
    assert event.payload["againstVersion"] == recommendation.version - 1


def test_a_decision_requires_a_reason(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    for empty in ("", "   ", None):
        with pytest.raises(rds.DecisionServiceError) as exc:
            _service(store).accept(rid, principal=_principal(), reason=empty)
        assert exc.value.reason == rds.REJECT_REASON_REQUIRED
    assert store.get_recommendation(rid).status == "PROPOSED"     # nothing written
    assert store.decisions(rid) == []


def test_the_note_is_optional_and_the_rejection_reason_is_kept(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    _, decision = _service(store).reject(rid, principal=_principal(),
                                         reason="spread too wide")
    assert decision.note is None
    assert decision.reason == "spread too wide"


def test_expire_is_an_operator_decision_not_a_background_job(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    recommendation, decision = _service(store).expire(
        rid, principal=_principal(), reason="session closed")
    assert recommendation.status == "EXPIRED"
    assert decision.decision_type == "EXPIRE"


def test_unknown_and_internal_decision_types_are_refused(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    service = _service(store)
    for banned in ("DEFER", "WITHDRAW", "SUPERSEDE", "AUTO_ACCEPT", "AUTO_REJECT",
                   "SYSTEM_INVALIDATE", "EXECUTE", "nonsense"):
        with pytest.raises((rds.DecisionServiceError,
                            auth.DecisionAuthorizationError)):
            service.decide(rid, decision_type=banned, principal=_principal(),
                           reason="should not be possible")
    assert store.decisions(rid) == []


def test_decision_on_a_missing_recommendation(tmp_path):
    with pytest.raises(rds.DecisionServiceError) as exc:
        _service(_store(tmp_path)).accept("rcm_" + "0" * 16,
                                          principal=_principal(), reason="x")
    assert exc.value.reason == rds.REJECT_NOT_FOUND


# ── PART 12: illegal sequences ───────────────────────────────────────────────

def test_double_acceptance_is_refused(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    service.accept(rid, principal=_principal(), reason="first")
    with pytest.raises(rds.DecisionServiceError) as exc:
        service.accept(rid, principal=_principal(), reason="second")
    assert exc.value.reason == rds.REJECT_NOT_DECIDABLE
    assert exc.value.detail == "already_accepted"
    assert len(store.decisions(rid)) == 1


def test_reject_after_accept_is_refused(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    service.accept(rid, principal=_principal(), reason="accepted")
    with pytest.raises(rds.DecisionServiceError) as exc:
        service.reject(rid, principal=_principal(), reason="changed my mind")
    assert exc.value.reason == rds.REJECT_NOT_DECIDABLE
    assert store.get_recommendation(rid).status == "ACCEPTED"


def test_accept_after_reject_is_refused(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    service.reject(rid, principal=_principal(), reason="rejected")
    with pytest.raises(rds.DecisionServiceError) as exc:
        service.accept(rid, principal=_principal(), reason="reconsidered")
    assert exc.value.detail.startswith("already_terminal")
    assert store.get_recommendation(rid).status == "REJECTED"


def test_a_draft_cannot_be_decided(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store, propose=False)
    with pytest.raises(rds.DecisionServiceError) as exc:
        _service(store).accept(rid, principal=_principal(), reason="too early")
    assert exc.value.detail == "not_yet_proposed"


# ── PART 5: concurrency ──────────────────────────────────────────────────────

def _race(store, rid, version, first, second):
    """Two operators decide simultaneously against the SAME version."""
    results = {}
    barrier = threading.Barrier(2)

    def run(name, decision_type):
        service = _service(store)
        barrier.wait()
        try:
            recommendation, _ = service.decide(
                rid, decision_type=decision_type,
                principal=_principal(actor=name), reason=f"{name} decided",
                expected_version=version)
            results[name] = ("ok", recommendation.status)
        except rds.DecisionServiceError as exc:
            results[name] = ("conflict" if exc.conflict else "error", exc.reason)

    threads = [threading.Thread(target=run, args=("op_a", first)),
               threading.Thread(target=run, args=("op_b", second))]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def test_two_operators_deciding_differently_produce_one_winner(tmp_path):
    """THE case this slice exists for. Before LIVE-4E both callers were told
    they had succeeded and the loser's decision vanished entirely."""
    store = _store(tmp_path)
    rid = _seed(store)
    version = store.current_version(rid)

    results = _race(store, rid, version, "ACCEPT", "REJECT")

    winners = [name for name, (outcome, _) in results.items() if outcome == "ok"]
    losers = [name for name, (outcome, _) in results.items() if outcome == "conflict"]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    # The loser is TOLD, with an actionable code.
    assert results[losers[0]][1] == rds.CONFLICT_VERSION

    # The state is valid and matches the winner.
    stored = store.get_recommendation(rid)
    assert stored.status == results[winners[0]][1]
    assert stored.status in ("ACCEPTED", "REJECTED")
    # History is complete and consistent: exactly one decision, one event.
    decisions = store.decisions(rid)
    assert len(decisions) == 1
    assert decisions[0].actor_id == winners[0]
    assert [e.sequence for e in store.history(rid)] == [1, 2, 3]
    assert stored.version == 3


def test_two_identical_decisions_still_produce_one_record(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    results = _race(store, rid, store.current_version(rid), "ACCEPT", "ACCEPT")
    assert sum(1 for outcome, _ in results.values() if outcome == "ok") == 1
    assert len(store.decisions(rid)) == 1


def test_a_stale_version_is_refused(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    current = store.current_version(rid)
    with pytest.raises(rds.DecisionServiceError) as exc:
        _service(store).accept(rid, principal=_principal(), reason="stale",
                               expected_version=current - 1)
    assert exc.value.reason == rds.CONFLICT_VERSION
    assert exc.value.conflict is True
    assert store.get_recommendation(rid).status == "PROPOSED"


def test_a_future_version_is_refused(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    with pytest.raises(rds.DecisionServiceError) as exc:
        _service(store).accept(rid, principal=_principal(), reason="ahead",
                               expected_version=store.current_version(rid) + 5)
    assert exc.value.reason == rds.CONFLICT_VERSION


def test_omitting_the_version_still_works(tmp_path):
    """Optimistic concurrency is opt-in: an internal caller with no read to
    protect may write without a precondition."""
    store = _store(tmp_path)
    rid = _seed(store)
    recommendation, _ = _service(store).accept(rid, principal=_principal(),
                                               reason="no precondition")
    assert recommendation.status == "ACCEPTED"


def test_version_advances_by_exactly_one_per_event(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    assert store.current_version(rid) == 2               # created + proposed
    recommendation, _ = _service(store).accept(rid, principal=_principal(),
                                               reason="accept")
    assert recommendation.version == 3
    assert store.current_version(rid) == 3
    assert store.get_recommendation(rid).version == 3


def test_an_idempotent_retry_replays_instead_of_recording_twice(tmp_path):
    """A retry after a SUCCESSFUL commit must replay — by then the proposal is
    terminal, and re-judging it would tell the caller its own decision was
    illegal."""
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    _, first = service.reject(rid, principal=_principal(), reason="invalidated",
                              idempotency_key="key-1")
    recommendation, replay = service.reject(rid, principal=_principal(),
                                            reason="invalidated",
                                            idempotency_key="key-1")
    assert replay.decision_id == first.decision_id
    assert len(store.decisions(rid)) == 1
    assert recommendation.status == "REJECTED"


def test_reusing_a_key_for_a_different_decision_is_a_conflict(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    service.reject(rid, principal=_principal(), reason="no", idempotency_key="k")
    with pytest.raises(rds.DecisionServiceError) as exc:
        service.accept(rid, principal=_principal(), reason="yes",
                       idempotency_key="k")
    assert exc.value.reason == rds.CONFLICT_IDEMPOTENCY
    assert exc.value.conflict is True


# ── PART 4: atomicity and immutability ───────────────────────────────────────

def test_a_failed_write_leaves_no_partial_decision(tmp_path, monkeypatch):
    """Before LIVE-4E the decision row committed in its own transaction, so a
    failure in between left a committed decision that history could not see."""
    store = _store(tmp_path)
    rid = _seed(store)

    def explode(*_a, **_k):
        raise sqlite3.OperationalError("simulated failure mid-write")

    monkeypatch.setattr(store, "_write", explode)
    with pytest.raises(rds.DecisionServiceError):
        _service(store).accept(rid, principal=_principal(), reason="will fail")
    monkeypatch.undo()

    assert store.decisions(rid) == []
    assert [e.event_type for e in store.history(rid)] == [
        rd.RecommendationEventType.CREATED, rd.RecommendationEventType.PROPOSED]
    assert store.get_recommendation(rid).status == "PROPOSED"
    assert store.current_version(rid) == 2


def test_history_is_append_only_with_no_mutable_rows():
    code = statements_only("recommendation_store.py")
    for forbidden in ("UPDATE recommendation_events", "DELETE FROM recommendation_events",
                      "UPDATE recommendation_decisions",
                      "DELETE FROM recommendation_decisions"):
        assert forbidden not in code, f"store contains {forbidden}"


def test_a_decision_is_never_overwritten(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    _, decision = service.accept(rid, principal=_principal(), reason="original")
    stored = store.get_decision(decision.decision_id)
    # Any later attempt is refused, so the original stays byte-identical.
    with pytest.raises(rds.DecisionServiceError):
        service.accept(rid, principal=_principal(), reason="rewritten")
    assert store.get_decision(decision.decision_id).as_dict() == stored.as_dict()


def test_a_contradictory_decision_at_an_occupied_sequence_is_refused(tmp_path):
    """The direct-store path is guarded too, not just the service."""
    store = _store(tmp_path)
    rid = _seed(store)
    _service(store).accept(rid, principal=_principal(), reason="accept")
    occupied = store.current_version(rid)
    intruder = rd.RecommendationDecision(
        decision_id="dec_intruder", recommendation_id=rid, decision_type="REJECT",
        actor_type=rd.ActorType.OPERATOR, actor_id="op_b", occurred_at=NOW,
        sequence=occupied, reason="contradiction")
    with pytest.raises(rstore.RecommendationStoreError) as exc:
        store.record_decision(rid, decision=intruder, to_status=None, at=NOW)
    assert exc.value.reason == "decision_sequence_conflict"


def test_the_store_rebuilds_from_events_after_a_decision(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    _service(store).accept(rid, principal=_principal(), reason="accept")
    snapshot = store.get_recommendation(rid)
    rebuilt = store.rebuild_recommendation(rid)
    assert rebuilt.status == snapshot.status
    assert rebuilt.as_dict()["status"] == "ACCEPTED"


def test_history_is_newest_first(tmp_path):
    store = _store(tmp_path)
    service = _service(store)
    rid = _seed(store)
    service.accept(rid, principal=_principal(), reason="accept")
    ordered = service.history(rid)
    assert [d.sequence for d in ordered] == sorted(
        [d.sequence for d in ordered], reverse=True)


def test_schema_migrates_from_v1_and_backfills_version(tmp_path):
    """A LIVE-4D database opens under LIVE-4E without losing anything, and the
    version is backfilled from the append-only log rather than guessed."""
    path = tmp_path / "legacy.db"
    store = rstore.RecommendationStore(path)
    rid = _seed(store)
    # Simulate a v1 database: drop the LIVE-4E state and re-stamp the version.
    conn = sqlite3.connect(path)
    conn.execute("UPDATE recommendations SET version=0")
    conn.execute("UPDATE recommendation_meta SET value='1' WHERE key='schema_version'")
    conn.commit()
    conn.close()

    reopened = rstore.RecommendationStore(path)
    assert reopened.schema_version() == 2
    assert reopened.get_recommendation(rid).version == 2      # backfilled
    assert len(reopened.history(rid)) == 2
    recommendation, _ = _service(reopened).accept(
        rid, principal=_principal(), reason="works after migration")
    assert recommendation.status == "ACCEPTED"


def test_a_newer_schema_still_fails_closed(tmp_path):
    path = tmp_path / "future.db"
    rstore.RecommendationStore(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE recommendation_meta SET value='99' WHERE key='schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(rstore.RecommendationStoreError) as exc:
        rstore.RecommendationStore(path)
    assert exc.value.reason == "unsupported_schema_version"


# ── PART 6: authorization ────────────────────────────────────────────────────

@pytest.mark.parametrize("actor", [None, "", "   ", "ab"])
def test_an_anonymous_or_unusable_identity_is_refused(actor):
    """No public anonymous mutation. Absent identity is a REFUSAL, never a
    fallback to 'system' — attributing a decision to a machine account that did
    not take it would corrupt the audit trail more quietly than refusing."""
    with pytest.raises(auth.DecisionAuthorizationError) as exc:
        auth.resolve_principal(asserted_actor=actor, boundary_authenticated=False)
    assert exc.value.reason in (auth.DENY_NO_ACTOR, auth.DENY_INVALID_ACTOR)


@pytest.mark.parametrize("actor", ["has space", "op/../etc", "op\nid", "x" * 80,
                                   "<script>", "op;drop"])
def test_a_malformed_identity_is_refused(actor):
    with pytest.raises(auth.DecisionAuthorizationError) as exc:
        auth.resolve_principal(asserted_actor=actor, boundary_authenticated=False)
    assert exc.value.reason == auth.DENY_INVALID_ACTOR


@pytest.mark.parametrize("actor", ["bearer_abc", "op_password", "my_token_1",
                                   "api_key_9"])
def test_an_identity_that_looks_like_a_credential_is_refused(actor):
    with pytest.raises(auth.DecisionAuthorizationError) as exc:
        auth.resolve_principal(asserted_actor=actor, boundary_authenticated=False)
    assert exc.value.reason == auth.DENY_ACTOR_LOOKS_SECRET


def test_identity_assurance_is_recorded_honestly(tmp_path):
    """The audit found the auth boundary is a SHARED token, so it cannot
    identify a person. The decision records what is actually known."""
    asserted = auth.resolve_principal(asserted_actor="op_jane",
                                      boundary_authenticated=False)
    authenticated = auth.resolve_principal(asserted_actor="op_jane",
                                           boundary_authenticated=True)
    assert asserted.identity_assurance == rd.IDENTITY_ASSERTED
    assert authenticated.identity_assurance == rd.IDENTITY_AUTHENTICATED
    assert asserted.authenticated is False and authenticated.authenticated is True

    store = _store(tmp_path)
    rid = _seed(store)
    _, decision = _service(store).accept(rid, principal=asserted, reason="ok")
    assert decision.identity_assurance == rd.IDENTITY_ASSERTED


def test_the_raw_operator_id_never_reaches_a_read_view(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)
    principal = _principal(actor="op_jane_the_trader")
    _, decision = _service(store).accept(rid, principal=principal, reason="ok")
    assert "op_jane_the_trader" not in str(decision.safe_view())
    assert decision.safe_view()["actor"].startswith("actor_")
    assert "op_jane_the_trader" not in repr(principal)
    assert "op_jane_the_trader" not in str(principal.safe_view())


def test_only_operators_may_take_operator_decisions():
    for actor_type in (rd.ActorType.SYSTEM, rd.ActorType.STRATEGY,
                       rd.ActorType.RISK_ENGINE, rd.ActorType.IMPORT):
        principal = auth.resolve_principal(
            asserted_actor="svc_bot", boundary_authenticated=True,
            actor_type=actor_type)
        with pytest.raises(auth.DecisionAuthorizationError) as exc:
            auth.assert_decision_allowed(principal, decision_type="ACCEPT")
        assert exc.value.reason == "actor_type_not_permitted"


def test_the_gate_reuses_the_existing_auth_policy_and_invents_none():
    code = statements_only("recommendation_authorization.py")
    assert "auth_policy" in code                     # reuses it
    for forbidden in ("jwt", "hashlib.pbkdf2", "session", "login", "sqlite3",
                      "CREATE TABLE", "os.environ"):
        assert forbidden not in code, f"gate invents {forbidden}"
    # It never touches the credential itself.
    assert "extract_bearer_token" not in code
    assert "verify(" not in code


def test_granting_a_decision_grants_no_execution_right():
    code = statements_only("recommendation_authorization.py")
    for forbidden in ("command_authorization", "AuthorizationGrant", "get_broker",
                      "order_send", "execution_safety", "ExecutionModeOwner"):
        assert forbidden not in code, f"gate references {forbidden}"


# ── PART 7: the write API ────────────────────────────────────────────────────

def _api_seed(monkeypatch, tmp_path, **kw):
    store = _store(tmp_path, "api.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", store)
    return store, _seed(store, **kw)


def test_the_write_surface_is_exactly_three_routes():
    paths = {(r.path, tuple(sorted(r.methods - {"HEAD", "OPTIONS"})))
             for r in server.app.routes
             if "trade-recommendations" in getattr(r, "path", "")}
    writes = {p for p, methods in paths if "POST" in methods}
    assert writes == {
        "/api/trade-recommendations/{recommendation_id}/accept",
        "/api/trade-recommendations/{recommendation_id}/reject",
        "/api/trade-recommendations/{recommendation_id}/expire"}
    # No DELETE, PUT or PATCH anywhere in the namespace.
    for _path, methods in paths:
        assert not ({"DELETE", "PUT", "PATCH"} & set(methods))


def test_an_anonymous_decision_is_forbidden(monkeypatch, tmp_path):
    _, rid = _api_seed(monkeypatch, tmp_path)
    res = client.post(f"/api/trade-recommendations/{rid}/accept",
                      json={"reason": "no identity"})
    assert res.status_code == 403
    assert res.json()["code"] == auth.DENY_NO_ACTOR


def test_accept_via_the_api_records_a_decision_and_says_it_executed_nothing(
        monkeypatch, tmp_path):
    store, rid = _api_seed(monkeypatch, tmp_path)
    version = store.current_version(rid)
    res = client.post(f"/api/trade-recommendations/{rid}/accept",
                      json={"reason": "conditions met", "note": "clean",
                            "expectedVersion": version},
                      headers={**HEADERS, "Idempotency-Key": "k1"})
    assert res.status_code == 200
    body = res.json()
    assert body["status"] == "ACCEPTED"
    assert body["version"] == version + 1
    assert body["executed"] is False
    assert "does not submit" in body["notice"] or "No order" in body["notice"]
    assert body["decision"]["actor"].startswith("actor_")
    assert body["decision"]["identityAssurance"] == rd.IDENTITY_ASSERTED
    assert "op_jane" not in res.text
    # The proposal itself is untouched apart from the decision.
    assert store.get_recommendation(rid).linked_intent_ids == ()


def test_the_api_returns_409_with_the_current_truth_on_conflict(monkeypatch, tmp_path):
    store, rid = _api_seed(monkeypatch, tmp_path)
    res = client.post(f"/api/trade-recommendations/{rid}/reject",
                      json={"reason": "stale", "expectedVersion": 1},
                      headers=HEADERS)
    assert res.status_code == 409
    body = res.json()
    assert body["code"] == rds.CONFLICT_VERSION
    assert body["executed"] is False
    # The caller is given what it needs to retry.
    assert body["currentStatus"] == "PROPOSED"
    assert body["currentVersion"] == store.current_version(rid)


def test_the_api_refuses_a_decision_without_a_reason(monkeypatch, tmp_path):
    _, rid = _api_seed(monkeypatch, tmp_path)
    res = client.post(f"/api/trade-recommendations/{rid}/accept", json={},
                      headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["code"] == rds.REJECT_REASON_REQUIRED


def test_the_api_refuses_a_second_decision(monkeypatch, tmp_path):
    _, rid = _api_seed(monkeypatch, tmp_path)
    assert client.post(f"/api/trade-recommendations/{rid}/accept",
                       json={"reason": "first"}, headers=HEADERS).status_code == 200
    res = client.post(f"/api/trade-recommendations/{rid}/reject",
                      json={"reason": "second"}, headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["code"] == rds.REJECT_NOT_DECIDABLE
    assert res.json()["detail"] == "already_accepted"


def test_the_api_replays_an_idempotent_retry(monkeypatch, tmp_path):
    store, rid = _api_seed(monkeypatch, tmp_path)
    headers = {**HEADERS, "Idempotency-Key": "retry-1"}
    body = {"reason": "conditions met"}
    first = client.post(f"/api/trade-recommendations/{rid}/accept",
                        json=body, headers=headers)
    second = client.post(f"/api/trade-recommendations/{rid}/accept",
                         json=body, headers=headers)
    assert first.status_code == second.status_code == 200
    assert (first.json()["decision"]["decisionId"]
            == second.json()["decision"]["decisionId"])
    assert len(store.decisions(rid)) == 1


def test_the_api_rejects_a_malformed_version(monkeypatch, tmp_path):
    _, rid = _api_seed(monkeypatch, tmp_path)
    res = client.post(f"/api/trade-recommendations/{rid}/accept",
                      json={"reason": "x", "expectedVersion": "soon"},
                      headers=HEADERS)
    assert res.status_code == 422
    assert res.json()["code"] == "invalid_expected_version"


def test_the_api_404s_an_unknown_recommendation(monkeypatch, tmp_path):
    _api_seed(monkeypatch, tmp_path)
    res = client.post(f"/api/trade-recommendations/rcm_{'0' * 16}/accept",
                      json={"reason": "x"}, headers=HEADERS)
    assert res.status_code == 404
    assert res.json()["code"] == rds.REJECT_NOT_FOUND


def test_a_decision_appears_in_the_read_surfaces(monkeypatch, tmp_path):
    _, rid = _api_seed(monkeypatch, tmp_path)
    client.post(f"/api/trade-recommendations/{rid}/accept",
                json={"reason": "conditions met", "note": "n"}, headers=HEADERS)
    detail = client.get(f"/api/trade-recommendations/{rid}").json()
    assert detail["status"] == "ACCEPTED"
    assert detail["decidable"] is False
    assert detail["undecidableReason"] == "already_accepted"
    decisions = client.get(f"/api/trade-recommendations/{rid}/decisions").json()
    assert decisions["decisions"][-1]["decisionType"] == "ACCEPT"
    assert decisions["decisions"][-1]["note"] == "n"
    assert decisions["conflicts"] == []


def test_a_recorded_decision_is_journalled(monkeypatch, tmp_path):
    """The decision is visible in the SYSTEM-WIDE event stream too, and the
    journal entry says plainly that nothing executed."""
    _, rid = _api_seed(monkeypatch, tmp_path)
    captured = []
    monkeypatch.setattr(server._DECISION_SERVICE, "_audit", captured.append)
    client.post(f"/api/trade-recommendations/{rid}/accept",
                json={"reason": "conditions met"}, headers=HEADERS)
    assert captured, "no journal entry was emitted"
    entry = captured[-1]
    assert entry["code"] == "RECOMMENDATION_ACCEPT"
    assert entry["who"].startswith("actor_")
    assert "no order was submitted" in entry["humanExplanation"].lower()


def test_a_failing_journal_never_loses_a_committed_decision(tmp_path):
    store = _store(tmp_path)
    rid = _seed(store)

    def explode(_entry):
        raise RuntimeError("journal is down")

    service = _service(store, audit=explode)
    recommendation, _ = service.accept(rid, principal=_principal(), reason="ok")
    assert recommendation.status == "ACCEPTED"
    assert len(store.decisions(rid)) == 1


# ── structural: nothing about execution changed ──────────────────────────────

def test_the_decision_service_cannot_execute_anything():
    code = statements_only("recommendation_decision_service.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "SubmitMarketOrder", "ClosePosition", "_ORCHESTRATOR",
                      "execution_safety", "AuthorizationGrant", "command_registry",
                      "trade_ledger", "broker_history", "scheduler", "threading",
                      "asyncio", "socket", "requests."):
        assert forbidden not in code, f"decision service references {forbidden}"


def test_the_execution_command_surface_is_unchanged():
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})


def test_broker_write_capabilities_are_unchanged():
    import broker as broker_layer
    caps = broker_layer.capability_dict(broker_layer.MT5Adapter().capabilities())
    for enabled in ("supportsLiveWrite", "supportsMarketExecution", "supportsModify",
                    "supportsCancelOrder", "supportsClosePosition"):
        assert caps[enabled] is True
    for disabled in ("supportsPendingOrders", "supportsPartialClose",
                     "supportsHedging", "supportsNetting", "supportsReplay"):
        assert caps[disabled] is False


def test_no_execution_module_learned_about_decisions():
    for module in ("execution.py", "execution_safety.py", "broker.py",
                   "broker_adapter.py", "reconciliation.py", "command_registry.py",
                   "execution_mode.py", "command_authorization.py",
                   "trade_ledger_domain.py", "trade_reconstruction.py",
                   "scenario_domain.py", "scenario_store.py"):
        code = statements_only(module)
        for forbidden in ("recommendation_decision_service",
                          "recommendation_authorization",
                          "RecommendationDecisionService", "DecisionPrincipal"):
            assert forbidden not in code, f"{module} depends on {forbidden}"


def test_accepting_never_creates_an_intent_or_touches_the_execution_store(
        monkeypatch, tmp_path):
    """End-to-end: accept through the API, then assert the execution side is
    exactly as it was."""
    store, rid = _api_seed(monkeypatch, tmp_path)
    execution_store = server._execution_store()
    before = len(execution_store.intents_by_state()) if execution_store else 0

    res = client.post(f"/api/trade-recommendations/{rid}/accept",
                      json={"reason": "conditions met"}, headers=HEADERS)
    assert res.status_code == 200

    after = len(execution_store.intents_by_state()) if execution_store else 0
    assert after == before
    assert store.get_recommendation(rid).linked_intent_ids == ()
    assert store.get_recommendation(rid).status == "ACCEPTED"
    # And the proposal is NOT in any state that claims execution.
    assert store.get_recommendation(rid).status not in (
        "INTENT_CREATED", "PARTIALLY_EXECUTED", "EXECUTED")


def test_the_pre_existing_fixture_route_is_still_untouched():
    body = client.get("/api/recommendations").json()
    assert isinstance(body, list) and body and "proposedChange" in body[0]
