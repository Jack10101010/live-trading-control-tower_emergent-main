"""LIVE-4D — the canonical Recommendation and decision domain.

Proves: immutable validated models with bounded fields, deterministic identity
that distinguishes revised proposals, an explicit lifecycle where ACCEPTED
never implies executed, append-only decision history with pseudonymized actors,
a rebuildable store, Scenario/Intent/Ledger linkage, honest fixture import,
supersession, explicit expiry, visible conflicts, and that NOTHING about
execution, authorization, broker writes or ledger accounting changed.
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import operational_projection as op                                 # noqa: E402
import recommendation_domain as rd                                  # noqa: E402
import recommendation_service as rsvc                               # noqa: E402
import recommendation_store as rstore                               # noqa: E402
import scenario_domain as sd                                        # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)


def statements_only(rel: str) -> str:
    """Executable statements only — every comment AND docstring removed.

    `code_only` strips comments and the module docstring but deliberately keeps
    function and class docstrings. A guard that forbids a bare WORD ("cancel",
    "ledger", "intents") would then match the very prose that explains why the
    module does not do that thing. These guards assert on what the code DOES, so
    they parse the module and drop the documentation entirely.
    """
    tree = ast.parse((BACKEND_DIR / rel).read_text())
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not (isinstance(body, list) and body):
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            body[0] = ast.Pass() if len(body) == 1 else None
            if body[0] is None:
                body.pop(0)
    return ast.unparse(tree)


T0 = "2026-07-27T12:00:00Z"
T1 = "2026-07-27T12:01:00Z"
T2 = "2026-07-27T12:02:00Z"
T3 = "2026-07-27T12:03:00Z"
SCN = "scn_" + "a" * 16


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    monkeypatch.setattr(server, "RECOMMENDATION_DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", False)
    yield
    stray = BACKEND_DIR / "recommendation_state.db"
    assert not stray.exists(), "a test created backend/recommendation_state.db"


# ── builders ──────────────────────────────────────────────────────────────────

def _terms(**over):
    ex = dict(order_type="market", quantity=0.5,
              quantity_policy=rd.QuantityPolicy.FIXED,
              entry_price_policy=rd.EntryPricePolicy.MARKET)
    rk = dict(stop_loss=1.0950, take_profit=1.1100, risk_percent=0.5, planned_r=2.0)
    ex.update(over.pop("execution", {}))
    rk.update(over.pop("risk", {}))
    return rd.RecommendationTerms(
        execution=rd.RecommendationExecutionTerms(**ex),
        risk=rd.RecommendationRiskTerms(**rk),
        rationale=over.pop("rationale", "BOS retest"),
        confidence=over.pop("confidence", 0.72), **over)


def _rec(**over):
    kw = dict(scenario_id=SCN, instrument="EURUSD", direction="long",
              source=rd.RecommendationSource.OPERATOR, created_at=T0,
              terms=_terms(), node_id="node-1")
    kw.update(over)
    return rd.new_recommendation(**kw)


def _scenario(**over):
    kw = dict(instrument="EURUSD", session="london", structure="BOS",
              direction="long", entry_model="BullExpand", created_at=T0,
              node_id="node-1")
    kw.update(over)
    return sd.new_scenario(**kw)


def _store(tmp_path, name="r.db"):
    return rstore.RecommendationStore(tmp_path / name)


def _service(store, scenario=None, now=T0):
    return rsvc.RecommendationService(
        store_fn=lambda: store,
        scenario_reader=lambda _sid: scenario,
        now_iso_fn=lambda: now, execution_mode_fn=lambda: "observe")


# ── domain: immutability, validation, bounds ─────────────────────────────────

def test_recommendation_is_immutable_and_json_safe():
    r = _rec()
    for attr in ("status", "instrument", "recommendation_id"):
        with pytest.raises(Exception):
            setattr(r, attr, "x")
    d = r.as_dict()
    assert list(d) == sorted(d)
    json.dumps(d)
    assert isinstance(r.linked_intent_ids, tuple)


@pytest.mark.parametrize("over,reason", [
    (dict(instrument=""), "invalid_instrument"),
    (dict(direction="sideways"), "invalid_direction"),
])
def test_invalid_recommendations_are_rejected(over, reason):
    with pytest.raises(rd.RecommendationError) as exc:
        _rec(**over)
    assert exc.value.reason == reason


def test_unavailable_terms_are_not_zero():
    empty = rd.RecommendationTerms()
    assert empty.execution.availability == rd.UNAVAILABLE
    assert empty.risk.availability == rd.UNAVAILABLE
    d = empty.as_dict()
    assert d["stopLoss"] is None and d["quantity"] is None      # never 0
    partial = rd.RecommendationTerms(
        execution=rd.RecommendationExecutionTerms(order_type="market"))
    assert partial.execution.availability == rd.PENDING


@pytest.mark.parametrize("kwargs,reason", [
    (dict(rationale="x" * 2001), "rationale_too_long"),
    (dict(confidence=1.5), "invalid_confidence"),
    (dict(confidence=-0.1), "invalid_confidence"),
    (dict(tags=tuple(str(i) for i in range(20))), "too_many_tags"),
    (dict(tags=("x" * 60,)), "invalid_tag"),
    (dict(metadata={"blob": "x" * 5000}), "metadata_too_large"),
])
def test_terms_bounds_are_enforced(kwargs, reason):
    with pytest.raises(rd.RecommendationError) as exc:
        rd.RecommendationTerms(**kwargs)
    assert exc.value.reason == reason


@pytest.mark.parametrize("term", ["stop_loss", "take_profit", "risk_amount"])
def test_non_positive_risk_terms_are_rejected(term):
    with pytest.raises(rd.RecommendationError):
        rd.RecommendationRiskTerms(**{term: 0})
    with pytest.raises(rd.RecommendationError):
        rd.RecommendationRiskTerms(**{term: -1})


# ── identity ─────────────────────────────────────────────────────────────────

def test_identity_is_deterministic_and_restart_stable():
    a, b = _rec(), _rec()
    assert a.recommendation_id == b.recommendation_id
    assert a.recommendation_id.startswith("rcm_") and len(a.recommendation_id) == 20


def test_one_scenario_supports_many_distinct_recommendations():
    """Identity is NOT derived from the Scenario alone."""
    first = _rec(discriminator="proposal-1")
    second = _rec(discriminator="proposal-2")
    assert first.scenario_id == second.scenario_id
    assert first.recommendation_id != second.recommendation_id


def test_revised_terms_produce_a_distinct_identity():
    original = _rec()
    revised = _rec(terms=_terms(risk={"stop_loss": 1.0960}))
    assert revised.recommendation_id != original.recommendation_id


def test_identity_ignores_lifecycle_and_decisions():
    r = _rec()
    proposed = rd.transition(r, rd.RecommendationStatus.PROPOSED, at=T1, reason="p")
    accepted = rd.transition(proposed, rd.RecommendationStatus.ACCEPTED, at=T2, reason="a")
    assert accepted.recommendation_id == r.recommendation_id


def test_invalid_recommendation_ids_are_rejected():
    for bad in ("nope", "rcm_", "rcm_XYZ", "rcm_" + "f" * 15):
        with pytest.raises(rd.RecommendationError):
            rd.RecommendationId(bad)


# ── lifecycle ────────────────────────────────────────────────────────────────

def test_the_full_happy_path_is_legal():
    r = _rec()
    flow = [rd.RecommendationStatus.PROPOSED, rd.RecommendationStatus.PENDING_DECISION,
            rd.RecommendationStatus.ACCEPTED, rd.RecommendationStatus.INTENT_CREATED,
            rd.RecommendationStatus.PARTIALLY_EXECUTED, rd.RecommendationStatus.EXECUTED]
    for i, status in enumerate(flow):
        r = rd.transition(r, status, at=f"2026-07-27T12:{i + 1:02d}:00Z", reason="step")
    assert r.status == rd.RecommendationStatus.EXECUTED
    assert r.outcome == rd.RecommendationOutcome.EXECUTED


def test_accepted_does_not_imply_executed():
    """THE central invariant: acceptance is a decision, not an execution."""
    r = rd.transition(rd.transition(_rec(), rd.RecommendationStatus.PROPOSED,
                                    at=T1, reason="p"),
                      rd.RecommendationStatus.ACCEPTED, at=T2, reason="a")
    assert r.status == rd.RecommendationStatus.ACCEPTED
    assert r.outcome == rd.RecommendationOutcome.NOT_EXECUTED
    with pytest.raises(rd.RecommendationError) as exc:
        rd.transition(r, rd.RecommendationStatus.EXECUTED, at=T3, reason="x")
    assert exc.value.reason == "invalid_transition"


@pytest.mark.parametrize("src,dst", [
    ("DRAFT", "ACCEPTED"), ("DRAFT", "EXECUTED"), ("PROPOSED", "INTENT_CREATED"),
    ("ACCEPTED", "EXECUTED"), ("PENDING_DECISION", "INTENT_CREATED"),
    ("INTENT_CREATED", "ACCEPTED"),
])
def test_illegal_transitions_fail(src, dst):
    with pytest.raises(rd.RecommendationError) as exc:
        rd.transition(_rec(status=src), dst, at=T1, reason="illegal")
    assert exc.value.reason == "invalid_transition"


@pytest.mark.parametrize("terminal", sorted(rd.TERMINAL_STATUSES))
def test_terminal_statuses_are_immutable(terminal):
    with pytest.raises(rd.RecommendationError) as exc:
        rd.transition(_rec(status=terminal), rd.RecommendationStatus.PROPOSED,
                      at=T1, reason="x")
    assert exc.value.reason == "terminal_status_immutable"
    assert rd.allowed_transitions(terminal) == frozenset()


def test_abandonment_is_reachable_from_every_active_status():
    for status in sorted(rd.ACTIVE_STATUSES):
        for target in (rd.RecommendationStatus.EXPIRED,
                       rd.RecommendationStatus.WITHDRAWN,
                       rd.RecommendationStatus.SUPERSEDED,
                       rd.RecommendationStatus.CANCELLED):
            assert rd.can_transition(status, target)


def test_transitions_require_reason_and_monotonic_time():
    r = _rec()
    with pytest.raises(rd.RecommendationError) as exc:
        rd.transition(r, rd.RecommendationStatus.PROPOSED, at=T1, reason="")
    assert exc.value.reason == "reason_required"
    moved = rd.transition(r, rd.RecommendationStatus.PROPOSED, at=T2, reason="ok")
    with pytest.raises(rd.RecommendationError) as exc:
        rd.transition(moved, rd.RecommendationStatus.ACCEPTED, at=T1, reason="back")
    assert exc.value.reason == "non_monotonic_timestamp"


def test_transition_is_pure():
    r = _rec()
    moved = rd.transition(r, rd.RecommendationStatus.PROPOSED, at=T1, reason="ok")
    assert r.status == rd.RecommendationStatus.DRAFT and moved is not r


@pytest.mark.parametrize("status,outcome", [
    ("EXECUTED", "EXECUTED"), ("PARTIALLY_EXECUTED", "PARTIALLY_EXECUTED"),
    ("REJECTED", "REJECTED"), ("EXPIRED", "EXPIRED"), ("WITHDRAWN", "WITHDRAWN"),
    ("SUPERSEDED", "SUPERSEDED"), ("FAILED", "EXECUTION_FAILED"),
    ("ACCEPTED", "NOT_EXECUTED"), ("PROPOSED", "NOT_EXECUTED"),
])
def test_outcome_describes_disposition_not_performance(status, outcome):
    assert rd.RecommendationOutcome.of(status) == outcome


# ── decisions ────────────────────────────────────────────────────────────────

def _decision(**over):
    kw = dict(decision_id="dec_1", recommendation_id="rcm_" + "a" * 16,
              decision_type=rd.RecommendationDecisionType.ACCEPT,
              actor_type=rd.ActorType.OPERATOR, actor_id="op_1",
              occurred_at=T1, sequence=1, reason="ok")
    kw.update(over)
    return rd.RecommendationDecision(**kw)


def test_decisions_are_immutable_and_validated():
    d = _decision()
    with pytest.raises(Exception):
        d.decision_type = "REJECT"
    with pytest.raises(rd.RecommendationError):
        _decision(decision_type="MAYBE")
    with pytest.raises(rd.RecommendationError):
        _decision(actor_type="ROBOT")
    with pytest.raises(rd.RecommendationError):
        _decision(occurred_at="not-a-time")


def test_operator_identity_is_pseudonymized_never_raw():
    view = _decision(actor_id="op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ").safe_view()
    assert view["actor"].startswith("actor_")
    assert "op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ" not in json.dumps(view)
    # Stable for correlation, distinct across actors.
    assert rd.pseudonymize_actor("a") == rd.pseudonymize_actor("a")
    assert rd.pseudonymize_actor("a") != rd.pseudonymize_actor("b")


@pytest.mark.parametrize("ref", ["Bearer abc123def", "password=hunter2",
                                 "api_key:xyz", "token=abc"])
def test_authorization_reference_refuses_credential_material(ref):
    with pytest.raises(rd.RecommendationError) as exc:
        _decision(authorization_reference=ref)
    assert exc.value.reason == "authorization_reference_looks_secret"


def test_authorization_reference_length_is_bounded():
    with pytest.raises(rd.RecommendationError) as exc:
        _decision(authorization_reference="a" * 200)
    assert exc.value.reason == "authorization_reference_too_long"


def test_latest_decision_is_deterministic_under_any_order():
    a = _decision(decision_id="dec_a", sequence=1,
                  decision_type=rd.RecommendationDecisionType.DEFER)
    b = _decision(decision_id="dec_b", sequence=2,
                  decision_type=rd.RecommendationDecisionType.ACCEPT)
    assert rd.latest_decision([a, b]).decision_id == "dec_b"
    assert rd.latest_decision([b, a]).decision_id == "dec_b"
    assert rd.latest_decision([]) is None


def test_conflicting_decisions_are_visible():
    a = _decision(decision_id="dec_a", sequence=1,
                  decision_type=rd.RecommendationDecisionType.ACCEPT)
    b = _decision(decision_id="dec_b", sequence=1,
                  decision_type=rd.RecommendationDecisionType.REJECT)
    conflicts = rd.conflicting_decisions([a, b])
    assert conflicts and "conflicting_decisions_at_sequence_1" in conflicts[0]
    assert rd.conflicting_decisions([a]) == ()


# ── store ────────────────────────────────────────────────────────────────────

def test_store_creates_and_is_idempotent(tmp_path):
    store = _store(tmp_path)
    assert store.schema_version() == 1
    r = _rec()
    store.create_recommendation(r)
    store.create_recommendation(r)
    store.create_recommendation(r)
    assert len(store.list_recommendations()) == 1
    assert len(store.history(r.recommendation_id)) == 1


def test_store_appends_events_and_never_deletes(tmp_path):
    store = _store(tmp_path)
    r = store.create_recommendation(_rec())
    rid = r.recommendation_id
    store.record_decision(rid, decision=_decision(recommendation_id=rid,
                          decision_type=rd.RecommendationDecisionType.DEFER),
                          to_status=rd.RecommendationStatus.PROPOSED, at=T1)
    events = store.history(rid)
    assert [e.event_type for e in events] == [
        rd.RecommendationEventType.CREATED, rd.RecommendationEventType.PROPOSED]
    assert [e.sequence for e in events] == [1, 2]
    code = code_only("recommendation_store.py")
    assert "DELETE FROM recommendation_events" not in code
    assert "UPDATE recommendation_events" not in code
    assert "DELETE FROM recommendation_decisions" not in code


def test_store_rebuilds_deterministically_from_events(tmp_path):
    store = _store(tmp_path)
    r = store.create_recommendation(_rec())
    rid = r.recommendation_id
    store.record_decision(rid, decision=_decision(recommendation_id=rid, sequence=1,
                          decision_type=rd.RecommendationDecisionType.DEFER),
                          to_status=rd.RecommendationStatus.PROPOSED, at=T1)
    store.record_decision(rid, decision=_decision(decision_id="dec_2",
                          recommendation_id=rid, sequence=2),
                          to_status=rd.RecommendationStatus.ACCEPTED, at=T2)
    store.link_intent(rid, "intent_1", at=T3)
    snapshot = store.get_recommendation(rid)
    rebuilt = store.rebuild_recommendation(rid)
    assert rebuilt.status == snapshot.status
    assert rebuilt.linked_intent_ids == snapshot.linked_intent_ids
    assert store.rebuild_recommendation(rid).as_dict() == rebuilt.as_dict()


def test_store_survives_restart(tmp_path):
    store = _store(tmp_path)
    rid = store.create_recommendation(_rec()).recommendation_id
    store.record_decision(rid, decision=_decision(recommendation_id=rid,
                          decision_type=rd.RecommendationDecisionType.DEFER),
                          to_status=rd.RecommendationStatus.PROPOSED, at=T1)
    reopened = rstore.RecommendationStore(tmp_path / "r.db")
    assert reopened.get_recommendation(rid).status == rd.RecommendationStatus.PROPOSED
    assert len(reopened.history(rid)) == 2


def test_store_rejects_illegal_transition_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    rid = store.create_recommendation(_rec()).recommendation_id
    with pytest.raises(rd.RecommendationError):
        store.record_decision(rid, decision=_decision(recommendation_id=rid),
                              to_status=rd.RecommendationStatus.EXECUTED, at=T1)
    assert store.get_recommendation(rid).status == rd.RecommendationStatus.DRAFT
    assert len(store.history(rid)) == 1


def test_store_refuses_newer_schema(tmp_path):
    _store(tmp_path)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "r.db")
    conn.execute("UPDATE recommendation_meta SET value='99' WHERE key='schema_version'")
    conn.commit(); conn.close()
    with pytest.raises(rstore.RecommendationStoreError) as exc:
        rstore.RecommendationStore(tmp_path / "r.db")
    assert exc.value.reason == "unsupported_schema_version"


def test_store_owns_nothing_else():
    code = statements_only("recommendation_store.py")
    for forbidden in ("get_broker", "order_send", "execution_store", "intents",
                      "scenarios", "ledger_entries", "auth_grants"):
        assert forbidden not in code, f"recommendation store references {forbidden}"


def test_store_filters_and_paginates(tmp_path):
    store = _store(tmp_path)
    store.create_recommendation(_rec(discriminator="a"))
    store.create_recommendation(_rec(discriminator="b", instrument="GBPUSD",
                                     scenario_id="scn_" + "b" * 16))
    store.create_recommendation(_rec(discriminator="c", direction="short"))
    assert len(store.list_recommendations()) == 3
    assert len(store.list_recommendations(instrument="GBPUSD")) == 1
    assert len(store.list_recommendations(direction="short")) == 1
    assert len(store.list_recommendations(scenario_id=SCN)) == 2
    assert len(store.list_recommendations(limit=2)) == 2
    assert len(store.list_recommendations(limit=2, offset=2)) == 1


def test_store_links_many_intents_idempotently(tmp_path):
    store = _store(tmp_path)
    rid = store.create_recommendation(_rec()).recommendation_id
    store.record_decision(rid, decision=_decision(recommendation_id=rid,
                          decision_type=rd.RecommendationDecisionType.DEFER),
                          to_status=rd.RecommendationStatus.PROPOSED, at=T1)
    store.record_decision(rid, decision=_decision(decision_id="d2",
                          recommendation_id=rid, sequence=2),
                          to_status=rd.RecommendationStatus.ACCEPTED, at=T2)
    store.link_intent(rid, "intent_b", at=T3)
    store.link_intent(rid, "intent_a", at=T3)
    store.link_intent(rid, "intent_a", at=T3)              # idempotent
    item = store.get_recommendation(rid)
    assert item.linked_intent_ids == ("intent_a", "intent_b")
    assert item.status == rd.RecommendationStatus.INTENT_CREATED
    assert len(store.list_recommendations(linked_intent="intent_a")) == 1


# ── service ──────────────────────────────────────────────────────────────────

def test_service_requires_an_existing_scenario(tmp_path):
    service = _service(_store(tmp_path), scenario=None)
    with pytest.raises(rsvc.RecommendationServiceError) as exc:
        service.create(_rec())
    assert exc.value.reason == rsvc.CONFLICT_SCENARIO_MISSING


@pytest.mark.parametrize("over,marker", [
    (dict(instrument="GBPUSD"), rsvc.CONFLICT_INSTRUMENT_MISMATCH),
    (dict(direction="short"), rsvc.CONFLICT_DIRECTION_MISMATCH),
])
def test_service_rejects_scenario_lineage_conflicts(tmp_path, over, marker):
    service = _service(_store(tmp_path), scenario=_scenario())
    with pytest.raises(rsvc.RecommendationServiceError) as exc:
        service.create(_rec(**over))
    assert exc.value.reason == "scenario_lineage_conflict"
    assert marker in exc.value.detail


def test_creating_a_recommendation_never_mutates_the_scenario(tmp_path):
    scenario = _scenario()
    before = scenario.as_dict()
    service = _service(_store(tmp_path), scenario=scenario)
    service.create(_rec())
    assert scenario.as_dict() == before          # untouched, status unchanged


def test_service_records_decisions_and_accept_does_not_execute(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    r = service.create(_rec())
    rid = r.recommendation_id
    service.propose(rid)
    accepted = service.record_decision(
        rid, decision_type=rd.RecommendationDecisionType.ACCEPT,
        actor_type=rd.ActorType.OPERATOR, actor_id="op_1", reason="conditions met",
        authorization_reference="auth_ref_1")
    assert accepted.status == rd.RecommendationStatus.ACCEPTED
    assert accepted.outcome == rd.RecommendationOutcome.NOT_EXECUTED
    assert accepted.linked_intent_ids == ()      # nothing was submitted
    decisions = store.decisions(rid)
    assert [d.decision_type for d in decisions] == ["DEFER", "ACCEPT"]
    assert decisions[-1].execution_mode == "observe"


def test_service_rejects_and_defers(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    rid = service.create(_rec()).recommendation_id
    service.propose(rid)
    rejected = service.record_decision(
        rid, decision_type=rd.RecommendationDecisionType.REJECT,
        actor_type=rd.ActorType.OPERATOR, actor_id="op_1", reason="invalidated")
    assert rejected.status == rd.RecommendationStatus.REJECTED
    assert rejected.outcome == rd.RecommendationOutcome.REJECTED


def test_service_enforces_legal_transitions_and_requires_reason(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    rid = service.create(_rec()).recommendation_id
    with pytest.raises(rsvc.RecommendationServiceError) as exc:
        service.record_decision(rid, decision_type=rd.RecommendationDecisionType.ACCEPT,
                                actor_type=rd.ActorType.OPERATOR, actor_id="op",
                                reason="")
    assert exc.value.reason == "reason_required"
    with pytest.raises(rsvc.RecommendationServiceError) as exc:
        service.record_decision(rid, decision_type=rd.RecommendationDecisionType.ACCEPT,
                                actor_type=rd.ActorType.OPERATOR, actor_id="op",
                                reason="too early")
    assert exc.value.reason == "invalid_transition"


def test_service_refuses_intent_claimed_by_another_recommendation(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    first = service.create(_rec(discriminator="a"))
    second = service.create(_rec(discriminator="b"))
    service.link_intent(first.recommendation_id, "intent_1")
    with pytest.raises(rsvc.RecommendationServiceError) as exc:
        service.link_intent(second.recommendation_id, "intent_1")
    assert exc.value.reason == rsvc.CONFLICT_INTENT_CLAIMED


def test_service_performs_no_execution_or_authorization():
    code = statements_only("recommendation_service.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "_ORCHESTRATOR", "execution_safety", "AuthorizationGrant",
                      "execution_mode.ExecutionModeOwner", "ledger"):
        assert forbidden not in code, f"service references {forbidden}"


# ── expiry ───────────────────────────────────────────────────────────────────

def test_past_due_is_projected_but_never_persisted_at_read_time(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario(), now="2026-07-28T00:00:00Z")
    r = service.create(_rec(expiry_at="2026-07-27T13:00:00Z"))
    rid = r.recommendation_id
    assert store.get_recommendation(rid).past_due("2026-07-28T00:00:00Z") is True
    # A read reports past-due; the STORED status is untouched.
    assert store.get_recommendation(rid).status == rd.RecommendationStatus.DRAFT
    expired = service.expire_due()
    assert [e.recommendation_id for e in expired] == [rid]
    assert store.get_recommendation(rid).status == rd.RecommendationStatus.EXPIRED
    assert rd.RecommendationEventType.EXPIRED in [e.event_type
                                                  for e in store.history(rid)]


def test_expiry_does_not_cancel_orders_or_invalidate_scenarios():
    code = statements_only("recommendation_service.py")
    start = code.index("def expire_due")
    end = code.index("def withdraw")
    body = code[start:end]
    for forbidden in ("cancel", "broker", "scenario_store", "invalidate"):
        assert forbidden not in body.lower(), f"expiry references {forbidden}"


def test_recommendation_without_expiry_is_never_past_due():
    assert _rec().past_due("2099-01-01T00:00:00Z") is False


# ── supersession ─────────────────────────────────────────────────────────────

def test_supersession_preserves_history_and_links_forward(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    original = service.create(_rec(discriminator="v1"))
    service.propose(original.recommendation_id)
    service.record_decision(original.recommendation_id,
                            decision_type=rd.RecommendationDecisionType.ACCEPT,
                            actor_type=rd.ActorType.OPERATOR, actor_id="op",
                            reason="accept v1", authorization_reference="auth_1")
    service.link_intent(original.recommendation_id, "intent_v1")
    revised = _rec(discriminator="v2", terms=_terms(risk={"stop_loss": 1.096}))
    old, new = service.supersede(original.recommendation_id, revised,
                                 reason="revised stop")
    assert old.status == rd.RecommendationStatus.SUPERSEDED
    assert old.superseded_by == new.recommendation_id
    assert new.supersedes == old.recommendation_id
    # Prior decisions remain immutable and intents stay with their creator.
    assert [d.decision_type for d in store.decisions(original.recommendation_id)] \
        == ["DEFER", "ACCEPT"]
    assert store.get_recommendation(original.recommendation_id).linked_intent_ids \
        == ("intent_v1",)
    assert store.get_recommendation(new.recommendation_id).linked_intent_ids == ()


def test_supersession_cycle_and_cross_scenario_are_rejected():
    r = _rec()
    with pytest.raises(rd.RecommendationError) as exc:
        rd.supersede(r, r, at=T1, reason="self")
    assert exc.value.reason == "supersession_cycle"
    other = _rec(scenario_id="scn_" + "b" * 16, discriminator="other")
    with pytest.raises(rd.RecommendationError) as exc:
        rd.supersede(r, other, at=T1, reason="cross")
    assert exc.value.reason == "cross_scenario_supersession"


# ── fixture adapter ──────────────────────────────────────────────────────────

FIXTURE_REC = {
    "recommendationId": "rec_01J8ZB3C6F9K2N5Q8T1W4Z7A0D",
    "scenarioKey": "EURUSD:london:BOS:long:BullExpand",
    "proposedChange": {"field": "target", "from": 2.5, "to": 2.75},
    "status": "deployed", "createdAt": "2026-06-29T14:00:00Z",
    "actor": "op_1", "evidence": {"confidence": 0.96},
}


def test_fixture_adapter_imports_policy_change_evidence_honestly():
    """AUDIT: fixture recommendations are POLICY-CHANGE proposals, not trade
    proposals. They import with trade terms UNAVAILABLE and a visible warning —
    no entry, stop or quantity is ever synthesised."""
    r = rsvc.adapt_fixture_recommendation(
        FIXTURE_REC, scenario_id_for_key=server.scenario_identity_for_key)
    assert r.source == rd.RecommendationSource.IMPORT
    assert r.instrument == "EURUSD" and r.direction == "long"
    assert r.terms.execution.availability == rd.UNAVAILABLE
    assert r.terms.risk.availability == rd.UNAVAILABLE
    assert r.terms.execution.quantity is None and r.terms.risk.stop_loss is None
    assert r.source_reference == FIXTURE_REC["recommendationId"]
    assert r.terms.metadata["kind"] == rsvc.FIXTURE_KIND_POLICY_CHANGE
    assert "not a trade proposal" in r.terms.metadata["importWarning"]
    assert r.terms.confidence == 0.96


def test_fixture_adapter_uses_the_canonical_scenario_identity():
    r = rsvc.adapt_fixture_recommendation(
        FIXTURE_REC, scenario_id_for_key=server.scenario_identity_for_key)
    expected = sd.ScenarioId.from_key(FIXTURE_REC["scenarioKey"]).value
    assert r.scenario_id == expected


def test_fixture_import_is_deterministic():
    a = rsvc.adapt_fixture_recommendation(
        FIXTURE_REC, scenario_id_for_key=server.scenario_identity_for_key)
    b = rsvc.adapt_fixture_recommendation(
        FIXTURE_REC, scenario_id_for_key=server.scenario_identity_for_key)
    assert a.recommendation_id == b.recommendation_id


@pytest.mark.parametrize("bad", [
    {}, {"recommendationId": "r"}, {"recommendationId": "r", "scenarioKey": "x"},
    {"recommendationId": "r", "scenarioKey": "EURUSD:london:BOS:long:BullExpand"},
])
def test_malformed_fixture_evidence_fails_visibly(bad):
    with pytest.raises(Exception):
        rsvc.adapt_fixture_recommendation(
            bad, scenario_id_for_key=server.scenario_identity_for_key)


def test_fixture_import_never_creates_a_scenario(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=None)         # no scenario exists
    out = rsvc.import_fixture_recommendations(
        [FIXTURE_REC], service=service,
        scenario_id_for_key=server.scenario_identity_for_key)
    assert out["imported"] == []
    assert out["skipped"][0]["reason"] == rsvc.CONFLICT_SCENARIO_MISSING
    assert store.list_recommendations() == []


def test_fixture_import_succeeds_when_the_scenario_exists(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    out = rsvc.import_fixture_recommendations(
        [FIXTURE_REC], service=service,
        scenario_id_for_key=server.scenario_identity_for_key)
    assert len(out["imported"]) == 1 and out["skipped"] == []
    assert out["kind"] == rsvc.FIXTURE_KIND_POLICY_CHANGE


def test_domain_has_no_fixture_coupling():
    code = statements_only("recommendation_domain.py")
    for forbidden in ("scenarioKey", "proposedChange", "world.v1", "fixture"):
        assert forbidden not in code, f"domain couples to {forbidden}"


# ── intent linkage (additive, inert) ─────────────────────────────────────────

def test_intent_carries_optional_recommendation_lineage():
    import order_lifecycle as ol
    plain = ol.OrderIntent(intent_id=ol.new_intent_id(), command_name="SubmitMarketOrder",
                           kind="submit", created_at=T0)
    assert plain.recommendation_id is None
    linked = ol.OrderIntent(intent_id=ol.new_intent_id(), command_name="SubmitMarketOrder",
                            kind="submit", created_at=T0,
                            recommendation_id="rcm_" + "a" * 16)
    assert linked.safe_view()["recommendationId"] == "rcm_" + "a" * 16


def test_execution_never_reads_recommendation_state_for_decisions():
    code = code_only("execution.py")
    lines = [l for l in code.splitlines() if "recommendation" in l.lower()]
    assert len(lines) == 1                             # the single capture site
    assert 'recommendation_id=payload.get("recommendationId")' in lines[0]
    for forbidden in ("if recommendation", "recommendation_id ==",
                      "recommendation_domain", "recommendation_store",
                      "RecommendationStatus"):
        assert forbidden not in code


def test_no_execution_module_depends_on_the_recommendation_domain():
    for module in ("execution_safety.py", "broker.py", "broker_adapter.py",
                   "reconciliation.py", "command_registry.py", "execution_mode.py",
                   "command_authorization.py", "scenario_domain.py",
                   "scenario_store.py", "trade_ledger_domain.py"):
        code = code_only(module)
        for forbidden in ("recommendation_domain", "recommendation_store",
                          "recommendation_service", "RecommendationStatus"):
            assert forbidden not in code, f"{module} depends on {forbidden}"


# ── ledger lineage ───────────────────────────────────────────────────────────

def _ledger_inputs(intents):
    import broker_history as bh
    import trade_reconstruction as tr
    def deal(i, entry, dt, vol, px, at, profit=None):
        return bh.BrokerDealRecord(deal_id=i, order_id="O1", position_id="P1",
                                   symbol="EURUSD", deal_type=dt, entry=entry,
                                   volume=vol, price=px, profit=profit, at=at,
                                   costs=bh.BrokerCostEvidence(),
                                   provenance="mock-fixture")
    history = bh.BrokerHistorySnapshot(
        at=T0, availability=bh.AVAILABLE, account_mode=bh.MODE_HEDGING,
        account_currency="USD", account_fingerprint="acctfp_1",
        deals=(deal("D1", bh.ENTRY_IN, "buy", 1.0, 1.10, "2026-07-01T01:00:00Z"),
               deal("D2", bh.ENTRY_OUT, "sell", 1.0, 1.11, "2026-07-01T03:00:00Z",
                    profit=100.0)),
        deals_available=True, provenance="mock-fixture")
    return tr.reconstruct(tr.ReconstructionInput(history=history, intents=intents))


def test_ledger_lineage_records_an_explicit_recommendation():
    result = _ledger_inputs(({"intent_id": "i1", "broker_ref": "P1", "kind": "submit",
                              "recommendation_id": "rcm_" + "b" * 16},))[0]
    assert result.trade.lineage.recommendation_id == "rcm_" + "b" * 16


def test_missing_recommendation_stays_unavailable_and_does_not_block():
    import trade_reconstruction as tr
    result = _ledger_inputs(())[0]
    assert result.trade.lineage.recommendation_id is None
    assert tr.WARN_RECOMMENDATION_UNLINKED in result.warnings
    assert result.finalizable is True                  # legacy trades may finalize


def test_conflicting_recommendations_block_but_never_change_accounting():
    import trade_reconstruction as tr
    result = _ledger_inputs((
        {"intent_id": "i1", "broker_ref": "P1", "kind": "submit",
         "recommendation_id": "rcm_" + "b" * 16},
        {"intent_id": "i2", "broker_ref": "P1", "kind": "close",
         "recommendation_id": "rcm_" + "c" * 16}))[0]
    assert tr.BLOCK_RECOMMENDATION_CONFLICT in result.blocking
    assert result.trade.lineage.recommendation_id is None
    assert result.trade.gross_realized_pnl == 100.0    # accounting untouched


# ── projection ───────────────────────────────────────────────────────────────

def test_projection_renders_recorded_values(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=_scenario())
    r = service.create(_rec())
    service.propose(r.recommendation_id)
    views = op.build_recommendations(
        store.list_recommendations(), now=T3,
        decisions_for=store.decisions,
        warnings_for=lambda x: service.warnings_for(x, now=T3))
    v = views[0]
    assert v.recommendation_id == r.recommendation_id
    assert v.stop_loss == 1.0950 and v.take_profit == 1.1100
    assert v.quantity == 0.5 and v.planned_r == 2.0
    assert v.latest_decision is not None
    assert v.latest_decision.actor is None or v.latest_decision.actor.startswith("actor_")
    assert v.provenance == op.PROV_DURABLE_STORE
    assert list(v.as_dict()) == sorted(v.as_dict())


def test_projection_reports_unavailable_store_distinctly():
    assert op.build_recommendations(None, now=T0) == ()
    summary = op.build_recommendation_summary(None, now=T0)
    assert summary.availability == op.AVAILABILITY_UNAVAILABLE
    assert op.build_recommendations([], now=T0) == ()      # readable but empty


def test_projection_surfaces_warnings(tmp_path):
    store = _store(tmp_path)
    service = _service(store, scenario=None)
    store.create_recommendation(_rec())                   # scenario missing
    views = op.build_recommendations(
        store.list_recommendations(), now=T3, decisions_for=store.decisions,
        warnings_for=lambda x: service.warnings_for(x, now=T3))
    assert rsvc.CONFLICT_SCENARIO_MISSING in views[0].warnings


def test_summary_has_no_performance_metrics(tmp_path):
    store = _store(tmp_path)
    store.create_recommendation(_rec())
    d = op.build_recommendation_summary(store.summary(), now=T0).as_dict()
    assert d["activeCount"] == 1
    for banned in ("winRate", "expectancy", "drawdown", "equityCurve", "pnl"):
        assert banned not in d


# ── API ──────────────────────────────────────────────────────────────────────

def _seed(monkeypatch, tmp_path):
    store = _store(tmp_path, "api.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", store)
    monkeypatch.setattr(server, "_scenario_reader", lambda _sid: _scenario())
    r = store.create_recommendation(_rec())
    return store, r


@pytest.mark.parametrize("path", ["/api/trade-recommendations/summary",
                                  "/api/trade-recommendations",
                                  "/api/trade-recommendations/active"])
def test_recommendation_endpoints_are_read_only_and_no_store(monkeypatch, tmp_path, path):
    _seed(monkeypatch, tmp_path)
    r = client.get(path)
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    assert client.post(path).status_code in (404, 405)
    assert client.delete(path).status_code in (404, 405)


def test_list_filters_and_paginates(monkeypatch, tmp_path):
    store, _ = _seed(monkeypatch, tmp_path)
    store.create_recommendation(_rec(discriminator="b", instrument="GBPUSD",
                                     scenario_id="scn_" + "b" * 16))
    body = client.get("/api/trade-recommendations?limit=10").json()
    assert body["totalCount"] == 2 and body["limit"] == 10
    assert len(client.get(
        "/api/trade-recommendations?instrument=GBPUSD").json()["recommendations"]) == 1
    assert len(client.get(
        "/api/trade-recommendations?direction=long").json()["recommendations"]) == 2
    assert client.get(
        "/api/trade-recommendations?limit=99999").json()["limit"] == server.MAX_RECOMMENDATION_PAGE


def test_detail_history_and_decisions_endpoints(monkeypatch, tmp_path):
    store, r = _seed(monkeypatch, tmp_path)
    rid = r.recommendation_id
    store.record_decision(rid, decision=_decision(recommendation_id=rid,
                          decision_type=rd.RecommendationDecisionType.DEFER),
                          to_status=rd.RecommendationStatus.PROPOSED, at=T1)
    detail = client.get(f"/api/trade-recommendations/{rid}")
    assert detail.status_code == 200
    assert detail.json()["recommendationId"] == rid
    assert len(detail.json()["decisions"]) == 1
    history = client.get(f"/api/trade-recommendations/{rid}/history")
    assert history.status_code == 200 and len(history.json()["events"]) == 2
    decisions = client.get(f"/api/trade-recommendations/{rid}/decisions")
    assert decisions.status_code == 200
    assert decisions.json()["decisions"][0]["actor"].startswith("actor_")
    assert decisions.json()["conflicts"] == []


def test_honest_404_and_unavailable(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    for path in (f"/api/trade-recommendations/rcm_{'0' * 16}",
                 f"/api/trade-recommendations/rcm_{'0' * 16}/history",
                 f"/api/trade-recommendations/rcm_{'0' * 16}/decisions"):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["code"] == "recommendation_not_found"
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", True)
    r = client.get("/api/trade-recommendations")
    assert r.status_code == 503
    assert r.json()["code"] == "recommendation_store_unavailable"


def test_there_is_no_decision_write_route(monkeypatch, tmp_path):
    """PART 14: no public write surface exists — decisions enter through the
    internal service only."""
    _, r = _seed(monkeypatch, tmp_path)
    rid = r.recommendation_id
    for path in (f"/api/trade-recommendations/{rid}/decisions",
                 f"/api/trade-recommendations/{rid}/accept",
                 f"/api/trade-recommendations/{rid}/reject",
                 "/api/trade-recommendations"):
        for verb in (client.post, client.put, client.patch, client.delete):
            assert verb(path).status_code in (404, 405), path


def test_the_pre_existing_fixture_route_is_untouched():
    """The canonical domain is namespaced under /api/trade-recommendations so it
    cannot shadow the fixture world's POLICY-CHANGE route."""
    r = client.get("/api/recommendations")
    assert r.status_code == 200
    body = r.json()
    assert isinstance(body, list)                       # the fixture array shape
    assert body and "proposedChange" in body[0]


# ── structural safety ────────────────────────────────────────────────────────

def test_execution_surface_and_broker_writes_are_unchanged():
    import broker as broker_layer
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})
    caps = broker_layer.capability_dict(broker_layer.MT5Adapter().capabilities())
    for w in ("supportsLiveWrite", "supportsMarketExecution", "supportsModify",
              "supportsCancelOrder", "supportsClosePosition"):
        assert caps[w] is True
    for w in ("supportsPendingOrders", "supportsPartialClose", "supportsHedging",
              "supportsNetting", "supportsReplay"):
        assert caps[w] is False


def test_no_strategy_runtime_or_autonomous_generation():
    for module in ("recommendation_domain.py", "recommendation_store.py",
                   "recommendation_service.py"):
        code = statements_only(module)
        for forbidden in ("signal", "indicator", "random", "schedule", "while True",
                          "threading", "asyncio"):
            assert forbidden not in code.lower(), f"{module} references {forbidden}"


def test_domain_and_service_are_side_effect_free_of_broker_and_ledger():
    for module in ("recommendation_domain.py", "recommendation_service.py"):
        code = statements_only(module)
        for forbidden in ("get_broker", "order_send", "trade_ledger",
                          "broker_history", "socket", "requests."):
            assert forbidden not in code, f"{module} references {forbidden}"
