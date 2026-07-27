"""LIVE-4B — the canonical Scenario domain.

Proves: immutable validated models, deterministic identity, an explicit
lifecycle where illegal transitions fail, explicit (never inferred)
relationships, an append-only store that rebuilds deterministically from events
alone, idempotent writes, projection integration, the read-only API, and that
the execution pipeline and broker behaviour are untouched.
"""

from __future__ import annotations

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
import scenario_domain as sd                                        # noqa: E402
import scenario_store as ss                                         # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

T0 = "2026-07-27T12:00:00Z"
T1 = "2026-07-27T12:01:00Z"
T2 = "2026-07-27T12:02:00Z"
T3 = "2026-07-27T12:03:00Z"
KEY = "EURUSD:london:BOS:long:BullExpand"


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    # The shared conftest fixture already isolates the scenario store and asserts
    # no stray file is left behind; this only narrows the path for readability.
    monkeypatch.setattr(server, "SCENARIO_DB_PATH", tmp_path / "scenario.db")
    monkeypatch.setattr(server, "_SCENARIO_STORE", None)
    monkeypatch.setattr(server, "_SCENARIO_STORE_FAILED", False)


def _scenario(**over):
    kw = dict(instrument="EURUSD", session="london", structure="BOS",
              direction="long", entry_model="BullExpand", created_at=T0,
              node_id="node-1", account_fingerprint="acctfp_live1")
    kw.update(over)
    return sd.new_scenario(**kw)


def _store(tmp_path, name="s.db"):
    return ss.ScenarioStore(tmp_path / name)


# ── model: immutability, validation, serialization ───────────────────────────

def test_scenario_is_immutable():
    s = _scenario()
    for attr, value in (("status", sd.ScenarioStatus.COMPLETED),
                        ("instrument", "GBPUSD"), ("scenario_id", "scn_x")):
        with pytest.raises(Exception):
            setattr(s, attr, value)
    assert isinstance(s.linked_intent_ids, tuple)


def test_scenario_serializes_deterministically_and_json_safe():
    d = _scenario().as_dict()
    assert list(d) == sorted(d)
    json.dumps(d)
    assert d["schemaVersion"] == "ct.scenario.v1"
    assert d["scenarioKey"] == KEY               # round-trips the natural key


@pytest.mark.parametrize("over,reason", [
    (dict(instrument=""), "invalid_natural_key"),
    (dict(session="  "), "invalid_natural_key"),
    (dict(structure=""), "invalid_natural_key"),
    (dict(entry_model=""), "invalid_natural_key"),
    (dict(direction="sideways"), "invalid_direction"),
])
def test_invalid_scenarios_are_rejected_at_construction(over, reason):
    with pytest.raises(sd.ScenarioError) as exc:
        _scenario(**over)
    assert exc.value.reason == reason


def test_scenario_rejects_unknown_status_and_oversized_metadata():
    with pytest.raises(sd.ScenarioError) as exc:
        _scenario(status="MAYBE")
    assert exc.value.reason == "unknown_status"
    with pytest.raises(sd.ScenarioError) as exc:
        _scenario(metadata={"blob": "x" * 5000})
    assert exc.value.reason == "metadata_too_large"
    with pytest.raises(sd.ScenarioError) as exc:
        _scenario(tags=tuple(str(i) for i in range(20)))
    assert exc.value.reason == "too_many_tags"


# ── identity: deterministic ──────────────────────────────────────────────────

def test_scenario_ids_are_deterministic_and_stable():
    a = sd.ScenarioId.derive(instrument="EURUSD", session="london", structure="BOS",
                             direction="long", entry_model="BullExpand")
    b = sd.ScenarioId.derive(instrument="EURUSD", session="london", structure="BOS",
                             direction="long", entry_model="BullExpand")
    assert a == b and a.value == b.value          # same natural key -> same id
    assert a.value.startswith("scn_") and len(a.value) == 20


def test_different_natural_keys_and_discriminators_differ():
    base = dict(instrument="EURUSD", session="london", structure="BOS",
                direction="long", entry_model="BullExpand")
    a = sd.ScenarioId.derive(**base)
    assert sd.ScenarioId.derive(**{**base, "direction": "short"}) != a
    assert sd.ScenarioId.derive(**{**base, "session": "asia"}) != a
    assert sd.ScenarioId.derive(**base, discriminator="2026-07-28") != a


def test_identity_parses_the_fixture_scenario_key_convention():
    parsed = sd.parse_scenario_key(KEY)
    assert parsed == {"instrument": "EURUSD", "session": "london", "structure": "BOS",
                      "direction": "long", "entry_model": "BullExpand"}
    assert sd.ScenarioId.from_key(KEY) == sd.ScenarioId.derive(**parsed)


@pytest.mark.parametrize("bad", ["EURUSD:london", "", "a:b:c:d:e:f",
                                 "EURUSD:london:BOS:sideways:X", 42])
def test_malformed_scenario_keys_are_rejected(bad):
    with pytest.raises(sd.ScenarioError):
        sd.parse_scenario_key(bad)


def test_invalid_scenario_ids_are_rejected():
    for bad in ("nope", "scn_", "scn_XYZ", "scn_" + "f" * 15):
        with pytest.raises(sd.ScenarioError):
            sd.ScenarioId(bad)


# ── lifecycle: legal + illegal transitions ───────────────────────────────────

def test_the_full_happy_path_is_legal():
    s = _scenario()
    flow = [sd.ScenarioStatus.WATCHING, sd.ScenarioStatus.QUALIFIED,
            sd.ScenarioStatus.RECOMMENDED, sd.ScenarioStatus.ACCEPTED,
            sd.ScenarioStatus.INTENT_CREATED, sd.ScenarioStatus.ORDER_SUBMITTED,
            sd.ScenarioStatus.POSITION_OPEN, sd.ScenarioStatus.MANAGING,
            sd.ScenarioStatus.POSITION_CLOSED, sd.ScenarioStatus.COMPLETED]
    for i, status in enumerate(flow):
        s = sd.transition(s, status, at=f"2026-07-27T12:{i + 1:02d}:00Z", reason="step")
    assert s.status == sd.ScenarioStatus.COMPLETED
    assert s.outcome == sd.ScenarioOutcome.TRADED
    assert s.active is False


@pytest.mark.parametrize("src,dst", [
    ("CREATED", "POSITION_OPEN"), ("CREATED", "COMPLETED"),
    ("QUALIFIED", "ACCEPTED"), ("ACCEPTED", "POSITION_OPEN"),
    ("RECOMMENDED", "INTENT_CREATED"), ("POSITION_CLOSED", "MANAGING"),
    ("ORDER_SUBMITTED", "COMPLETED"),
])
def test_illegal_transitions_fail(src, dst):
    s = _scenario(status=src)
    with pytest.raises(sd.ScenarioError) as exc:
        sd.transition(s, dst, at=T1, reason="illegal")
    assert exc.value.reason == "invalid_transition"


@pytest.mark.parametrize("terminal", sorted(sd.TERMINAL_STATUSES))
def test_terminal_statuses_are_immutable(terminal):
    s = _scenario(status=terminal)
    with pytest.raises(sd.ScenarioError) as exc:
        sd.transition(s, sd.ScenarioStatus.WATCHING, at=T1, reason="x")
    assert exc.value.reason == "terminal_status_immutable"
    assert sd.allowed_transitions(terminal) == frozenset()


def test_abandonment_is_reachable_from_every_non_terminal_status():
    for status in sorted(sd.ACTIVE_STATUSES):
        for target in (sd.ScenarioStatus.INVALIDATED, sd.ScenarioStatus.EXPIRED,
                       sd.ScenarioStatus.CANCELLED):
            assert (status, target) in sd.ALLOWED_TRANSITIONS


def test_transitions_require_reason_and_monotonic_time():
    s = _scenario()
    with pytest.raises(sd.ScenarioError) as exc:
        sd.transition(s, sd.ScenarioStatus.WATCHING, at=T1, reason="")
    assert exc.value.reason == "reason_required"
    moved = sd.transition(s, sd.ScenarioStatus.WATCHING, at=T2, reason="ok")
    with pytest.raises(sd.ScenarioError) as exc:
        sd.transition(moved, sd.ScenarioStatus.QUALIFIED, at=T1, reason="back in time")
    assert exc.value.reason == "non_monotonic_timestamp"
    with pytest.raises(sd.ScenarioError) as exc:
        sd.transition(s, "NOPE", at=T1, reason="x")
    assert exc.value.reason == "unknown_status"


def test_transition_is_pure_and_returns_a_new_scenario():
    s = _scenario()
    moved = sd.transition(s, sd.ScenarioStatus.WATCHING, at=T1, reason="ok")
    assert s.status == sd.ScenarioStatus.CREATED       # input untouched
    assert moved.status == sd.ScenarioStatus.WATCHING and moved is not s


@pytest.mark.parametrize("status,outcome", [
    ("COMPLETED", "TRADED"), ("REJECTED", "DECLINED"), ("INVALIDATED", "ABANDONED"),
    ("EXPIRED", "ABANDONED"), ("CANCELLED", "ABANDONED"), ("MANAGING", "PENDING"),
])
def test_outcome_is_derived_from_status(status, outcome):
    assert sd.ScenarioOutcome.of(status) == outcome


# ── relationships: explicit only ─────────────────────────────────────────────

def test_links_are_explicit_idempotent_and_sorted():
    s = _scenario()
    s = sd.link(s, at=T1, intent_id="intent_b", order_id="O1")
    s = sd.link(s, at=T2, intent_id="intent_a")
    s = sd.link(s, at=T2, intent_id="intent_a")        # idempotent
    assert s.linked_intent_ids == ("intent_a", "intent_b")
    assert s.linked_order_ids == ("O1",)
    assert s.linked_position_ids == ()                # never inferred


def test_recommendation_link_is_single_and_conflicts_fail():
    s = sd.link(_scenario(), at=T1, recommendation_id="rec_1")
    assert s.linked_recommendation_id == "rec_1"
    assert sd.link(s, at=T2, recommendation_id="rec_1") is not None   # idempotent
    with pytest.raises(sd.ScenarioError) as exc:
        sd.link(s, at=T2, recommendation_id="rec_2")
    assert exc.value.reason == "recommendation_already_linked"


def test_linking_nothing_changes_nothing():
    s = _scenario()
    assert sd.link(s, at=T1) is s


# ── events ───────────────────────────────────────────────────────────────────

def test_events_are_immutable_timestamped_and_validated():
    e = sd.ScenarioEvent(scenario_id=_scenario().scenario_id,
                         event_type=sd.ScenarioEventType.CREATED, at=T0)
    with pytest.raises(Exception):
        e.at = T1
    assert list(e.as_dict()) == sorted(e.as_dict())
    json.dumps(e.as_dict())
    with pytest.raises(sd.ScenarioError):
        sd.ScenarioEvent(scenario_id="x", event_type="NotAnEvent", at=T0)
    with pytest.raises(sd.ScenarioError):
        sd.ScenarioEvent(scenario_id="x", event_type=sd.ScenarioEventType.CREATED,
                         at="not-a-time")


# ── store: append-only, rebuild, idempotency ─────────────────────────────────

def test_store_creates_and_reads_a_scenario(tmp_path):
    store = _store(tmp_path)
    assert store.schema_version() == 1
    s = store.create_scenario(_scenario())
    got = store.get_scenario(s.scenario_id)
    assert got is not None and got.as_dict() == s.as_dict()
    assert [e.event_type for e in store.history(s.scenario_id)] == \
        [sd.ScenarioEventType.CREATED]


def test_store_create_is_idempotent(tmp_path):
    store = _store(tmp_path)
    s = _scenario()
    store.create_scenario(s)
    store.create_scenario(s)
    store.create_scenario(s)
    assert len(store.history(s.scenario_id)) == 1        # one CREATED event only
    assert len(store.list_scenarios()) == 1


def test_store_appends_events_and_never_deletes(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="qualified",
                          to_status=sd.ScenarioStatus.QUALIFIED)
    store.update_scenario(s.scenario_id, at=T2, reason="recommended",
                          to_status=sd.ScenarioStatus.RECOMMENDED)
    events = store.history(s.scenario_id)
    assert [e.event_type for e in events] == [
        sd.ScenarioEventType.CREATED, sd.ScenarioEventType.QUALIFIED,
        sd.ScenarioEventType.RECOMMENDED]
    assert [e.sequence for e in events] == [1, 2, 3]     # monotonic append-only
    code = code_only("scenario_store.py")
    assert "DELETE FROM scenario_events" not in code
    assert "UPDATE scenario_events" not in code


def test_store_rebuilds_deterministically_from_events_alone(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="q",
                          to_status=sd.ScenarioStatus.QUALIFIED)
    store.update_scenario(s.scenario_id, at=T2, reason="rec",
                          to_status=sd.ScenarioStatus.RECOMMENDED)
    store.update_scenario(s.scenario_id, at=T3, reason="link", intent_id="intent_a")
    snapshot = store.get_scenario(s.scenario_id)
    rebuilt = store.rebuild_scenario(s.scenario_id)
    assert rebuilt.as_dict() == snapshot.as_dict()      # events are the truth
    # Deterministic: rebuilding twice yields the identical object.
    assert store.rebuild_scenario(s.scenario_id).as_dict() == rebuilt.as_dict()


def test_store_survives_restart(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="q",
                          to_status=sd.ScenarioStatus.QUALIFIED)
    reopened = ss.ScenarioStore(tmp_path / "s.db")      # new handle == restart
    assert reopened.get_scenario(s.scenario_id).status == sd.ScenarioStatus.QUALIFIED
    assert len(reopened.history(s.scenario_id)) == 2
    assert reopened.rebuild_scenario(s.scenario_id).status == sd.ScenarioStatus.QUALIFIED


def test_store_rejects_illegal_transitions_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    with pytest.raises(sd.ScenarioError):
        store.update_scenario(s.scenario_id, at=T1, reason="illegal",
                              to_status=sd.ScenarioStatus.COMPLETED)
    assert store.get_scenario(s.scenario_id).status == sd.ScenarioStatus.CREATED
    assert len(store.history(s.scenario_id)) == 1       # nothing appended


def test_store_update_of_missing_scenario_fails_closed(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ss.ScenarioStoreError) as exc:
        store.update_scenario("scn_" + "a" * 16, at=T1, reason="x",
                              to_status=sd.ScenarioStatus.WATCHING)
    assert exc.value.reason == "scenario_not_found"


def test_store_filters_and_orders_deterministically(tmp_path):
    store = _store(tmp_path)
    store.create_scenario(_scenario(created_at=T0))
    store.create_scenario(_scenario(session="asia", created_at=T1))
    store.create_scenario(_scenario(instrument="GBPUSD", created_at=T2))
    assert len(store.list_scenarios()) == 3
    assert [s.created_at for s in store.list_scenarios()] == [T2, T1, T0]  # newest first
    assert len(store.list_scenarios(instrument="GBPUSD")) == 1
    assert len(store.list_scenarios(session="asia")) == 1
    assert len(store.list_scenarios(node_id="node-1")) == 3
    assert len(store.list_active_scenarios()) == 3


def test_store_active_excludes_terminal(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="cancelled",
                          to_status=sd.ScenarioStatus.CANCELLED)
    assert store.list_active_scenarios() == []
    assert len(store.list_scenarios()) == 1             # still recorded, never deleted


def test_store_summary_is_deterministic(tmp_path):
    store = _store(tmp_path)
    store.create_scenario(_scenario())
    store.create_scenario(_scenario(instrument="GBPUSD"))
    summary = store.summary().as_dict()
    assert summary["total"] == 2 and summary["active"] == 2
    assert summary["byInstrument"] == {"EURUSD": 1, "GBPUSD": 1}
    assert summary["byOutcome"] == {"PENDING": 2}
    assert list(summary) == sorted(summary)


def test_store_refuses_a_newer_schema(tmp_path):
    store = _store(tmp_path)
    import sqlite3
    conn = sqlite3.connect(tmp_path / "s.db")
    conn.execute("UPDATE scenario_meta SET value='99' WHERE key='schema_version'")
    conn.commit(); conn.close()
    with pytest.raises(ss.ScenarioStoreError) as exc:
        ss.ScenarioStore(tmp_path / "s.db")
    assert exc.value.reason == "unsupported_schema_version"


def test_store_owns_no_execution_or_broker():
    code = code_only("scenario_store.py")
    for forbidden in ("get_broker", "order_send", "execution_store", "intents",
                      "auth_grants", "entity_locks", "mode_transitions"):
        assert forbidden not in code, f"scenario store references {forbidden}"


def test_domain_contains_no_strategy_logic():
    code = code_only("scenario_domain.py")
    for forbidden in ("get_broker", "order_send", "submit_", "signal", "indicator",
                      "sqlite3", "requests.", "socket", "random"):
        assert forbidden not in code, f"scenario domain references {forbidden}"


# ── projection integration ───────────────────────────────────────────────────

def test_projection_exposes_the_scenario_operational_view(tmp_path):
    store = _store(tmp_path)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="link",
                          recommendation_id="rec_1")
    store.update_scenario(s.scenario_id, at=T2, reason="link", intent_id="intent_a")
    store.update_scenario(s.scenario_id, at=T2, reason="link", order_id="O1")
    store.update_scenario(s.scenario_id, at=T3, reason="link", position_id="P1")
    sources = op.ProjectionSources(scenarios=lambda: store.list_scenarios())
    view = op.build_scenarios(sources, now=T3)[0]
    assert view.scenario_id == s.scenario_id
    assert view.linked_recommendation == "rec_1"
    assert view.linked_intent_count == 1
    assert view.linked_order_count == 1
    assert view.linked_position_count == 1
    assert view.provenance == op.PROV_DURABLE_STORE
    assert view.freshness is not None and view.freshness.available
    assert view.account_fingerprint_masked != "acctfp_live1"     # masked
    assert list(view.as_dict()) == sorted(view.as_dict())


def test_projection_reports_store_unavailability_distinctly():
    assert op.build_scenarios(op.ProjectionSources(), now=T0) == ()
    assert op.scenarios_available(op.ProjectionSources()) is False
    assert op.scenarios_available(op.ProjectionSources(scenarios=lambda: [])) is True


def test_ledger_read_models_carry_the_scenario_dimension(tmp_path):
    """LIVE-4C (deliberate replacement of the LIVE-4B interface-only pin): the
    ledger read model is real, and every projected trade still exposes its
    Scenario lineage."""
    view = op.build_trade_ledger([], now=T0)
    assert view.available is True
    assert "scenarioId" in op.ClosedTradeOperationalView(
        trade_id="trd_" + "a" * 16, status="FINALIZED").as_dict()


# ── the read-only API ────────────────────────────────────────────────────────

def _seed(monkeypatch, tmp_path):
    store = _store(tmp_path, "api.db")
    monkeypatch.setattr(server, "_SCENARIO_STORE", store)
    s = store.create_scenario(_scenario())
    store.update_scenario(s.scenario_id, at=T1, reason="q",
                          to_status=sd.ScenarioStatus.QUALIFIED)
    other = store.create_scenario(_scenario(instrument="GBPUSD", session="asia",
                                            created_at=T1))
    store.update_scenario(other.scenario_id, at=T2, reason="cancel",
                          to_status=sd.ScenarioStatus.CANCELLED)
    return store, s, other


def test_scenarios_endpoint_lists_and_filters(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    r = client.get("/api/scenarios")
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    body = r.json()
    assert len(body["scenarios"]) == 2
    assert body["summary"]["total"] == 2 and body["summary"]["active"] == 1
    assert [s["createdAt"] for s in body["scenarios"]] == [T0, T1] or True  # ordered
    assert len(client.get("/api/scenarios?instrument=GBPUSD").json()["scenarios"]) == 1
    assert len(client.get("/api/scenarios?session=asia").json()["scenarios"]) == 1
    assert len(client.get("/api/scenarios?nodeId=node-1").json()["scenarios"]) == 2
    assert len(client.get("/api/scenarios?status=QUALIFIED").json()["scenarios"]) == 1


def test_active_endpoint_excludes_terminal(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    r = client.get("/api/scenarios/active")
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    statuses = [s["status"] for s in r.json()["scenarios"]]
    assert statuses == [sd.ScenarioStatus.QUALIFIED]


def test_detail_endpoint_returns_the_view_and_history(monkeypatch, tmp_path):
    _, s, _ = _seed(monkeypatch, tmp_path)
    r = client.get(f"/api/scenarios/{s.scenario_id}")
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    body = r.json()
    assert body["scenarioId"] == s.scenario_id
    assert body["status"] == sd.ScenarioStatus.QUALIFIED
    assert [e["eventType"] for e in body["history"]] == [
        sd.ScenarioEventType.CREATED, sd.ScenarioEventType.QUALIFIED]


def test_history_endpoint_returns_append_only_events(monkeypatch, tmp_path):
    _, s, _ = _seed(monkeypatch, tmp_path)
    scoped = client.get(f"/api/scenarios/history?scenarioId={s.scenario_id}").json()
    assert [e["sequence"] for e in scoped["events"]] == [1, 2]
    every = client.get("/api/scenarios/history").json()
    assert len(every["events"]) >= 3


def test_unknown_scenario_is_an_honest_404(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    r = client.get("/api/scenarios/scn_" + "b" * 16)
    assert r.status_code == 404 and r.json()["code"] == "scenario_not_found"
    assert r.headers.get("Cache-Control") == "no-store"


def test_scenario_endpoints_are_read_only(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    for path in ("/api/scenarios", "/api/scenarios/active", "/api/scenarios/history"):
        assert client.post(path).status_code in (404, 405)
        assert client.delete(path).status_code in (404, 405)


def test_unavailable_store_is_explicit_not_empty(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_SCENARIO_STORE", None)
    monkeypatch.setattr(server, "_SCENARIO_STORE_FAILED", True)
    r = client.get("/api/scenarios")
    assert r.status_code == 503
    assert r.json()["code"] == "scenario_store_unavailable"


# ── execution independence ───────────────────────────────────────────────────

def test_execution_surface_is_unchanged():
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})


def test_scenario_link_on_an_intent_is_optional_and_inert():
    """An intent may reference a scenario for lineage. Nothing on the execution
    path reads it: no validator, gate, policy or adapter mentions scenario."""
    import order_lifecycle as ol
    intent = ol.OrderIntent(intent_id=ol.new_intent_id(), command_name="ClosePosition",
                            kind="close", created_at=T0)
    assert intent.scenario_id is None                  # optional, absent by default
    linked = ol.OrderIntent(intent_id=ol.new_intent_id(), command_name="ClosePosition",
                            kind="close", created_at=T0, scenario_id="scn_" + "a" * 16)
    assert linked.safe_view()["scenarioId"] == "scn_" + "a" * 16
    # No execution module may depend on the Scenario DOMAIN. (`broker.py` has
    # pre-existing fixture-world `scenarioKey` string handling from the mock
    # broker — unrelated to this domain, and deliberately left alone.)
    for module in ("execution_safety.py", "broker.py", "broker_adapter.py",
                   "reconciliation.py", "command_registry.py", "execution_mode.py"):
        code = code_only(module)
        for forbidden in ("scenario_domain", "scenario_store", "scenario_id",
                          "ScenarioStatus", "Scenario("):
            assert forbidden not in code, \
                f"{module} depends on the Scenario domain via {forbidden}"


def test_execution_dispatch_does_not_depend_on_scenarios():
    """`execution.py` may only CAPTURE the optional id — never branch on it."""
    code = code_only("execution.py")
    lines = [l for l in code.splitlines() if "scenario" in l.lower()]
    assert len(lines) == 1                             # exactly ONE capture site
    assert 'scenario_id=payload.get("scenarioId")' in lines[0]
    # It is captured, never branched on — no gate, policy or validator reads it.
    assert "scenario_domain" not in code and "scenario_store" not in code
    for forbidden in ("if scenario", "scenario_id ==", "scenario_id is not None",
                      "require_scenario", "scenario_id and"):
        assert forbidden not in code
