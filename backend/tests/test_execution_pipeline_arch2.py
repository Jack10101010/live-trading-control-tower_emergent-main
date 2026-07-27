"""ARCH-2 — the complete pre-live execution pipeline, telemetry and API boundary.

Integration proofs against the in-process app: every execution path passes
validation → safety → resolution → adapter dispatch → broker result → lifecycle
persistence → audit; no stage skips; dispatch requires durability AND a prior
safety allow; the fault route is gated; telemetry derives; readiness is never a
constant; read-only operator behaviour is unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker as broker_layer                                        # noqa: E402
import execution as ex                                              # noqa: E402
import execution_telemetry as xt                                    # noqa: E402
import order_lifecycle as ol                                        # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

OPEN_TRADE = "tr_01J8ZC4H2N8M6K1E3R5T7V9W0XZ"       # fixture: state "managing"


@pytest.fixture(autouse=True)
def isolated_event_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())


# ── the full canonical pipeline ───────────────────────────────────────────────

def test_broker_dispatched_command_runs_every_stage_in_order_with_durable_lifecycle():
    r = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE})
    assert r.status_code == 200, r.text
    body = r.json()
    stages = [s["stage"] for s in body["execution"]["stages"]]
    assert stages == [
        ex.STAGE_RECEIVED, ex.STAGE_VALIDATED, ex.STAGE_SAFETY, ex.STAGE_RESOLVED,
        ex.STAGE_BROKER_DISPATCH, ex.STAGE_BROKER_RESULT, ex.STAGE_LIFECYCLE,
        ex.STAGE_RUNTIME_UPDATE, ex.STAGE_AUDIT_EVENT, ex.STAGE_COMPLETED,
    ]
    # Safety strictly precedes dispatch; lifecycle persistence follows the result.
    assert stages.index(ex.STAGE_SAFETY) < stages.index(ex.STAGE_BROKER_DISPATCH)
    assert stages.index(ex.STAGE_BROKER_RESULT) < stages.index(ex.STAGE_LIFECYCLE)

    # The durable record: command → intent linkage and the full transition history.
    store = server._execution_store()
    rows = store.intents_by_state()
    assert len(rows) == 1
    row = rows[0]
    assert row["command_id"] == body["commandId"]
    assert row["command_name"] == "SLToBE" and row["kind"] == ol.KIND_MODIFY
    assert row["state"] == ol.MODIFIED
    transitions = [(t["from_state"], t["to_state"]) for t in store.transitions_of(row["intent_id"])]
    assert transitions == [
        (ol.CREATED, ol.CREATED), (ol.CREATED, ol.VALIDATED),
        (ol.VALIDATED, ol.READY), (ol.READY, ol.MODIFY_PENDING),
        (ol.MODIFY_PENDING, ol.MODIFIED),
    ]


def test_broker_dispatch_is_denied_when_the_store_is_unavailable(monkeypatch):
    """No broker dispatch may occur that the durable lifecycle cannot record."""
    monkeypatch.setattr(server, "_EXECUTION_STORE", None)
    monkeypatch.setattr(server, "_EXECUTION_STORE_FAILED", True)   # simulate corrupt store
    dispatched = []
    original = broker_layer.MockBroker.submit_command

    def spy(self, name, ctx):
        dispatched.append(name)
        return original(self, name, ctx)

    monkeypatch.setattr(broker_layer.MockBroker, "submit_command", spy)
    r = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "execution_store_unavailable"
    assert dispatched == [], "broker was dispatched without durable lifecycle"


def test_non_broker_commands_do_not_write_order_lifecycle():
    r = client.post("/api/commands/PauseDeployment",
                    json={"deploymentId": "dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ"})
    assert r.status_code == 200
    assert server._execution_store().intents_by_state() == []


def test_duplicate_submission_is_idempotent_and_creates_one_intent():
    headers = {"Idempotency-Key": "arch2-dup-1"}
    r1 = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE}, headers=headers)
    r2 = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE}, headers=headers)
    assert r1.status_code == r2.status_code == 200
    assert r2.json()["deduplicated"] is True
    assert len(server._execution_store().intents_by_state()) == 1


def test_audit_append_failure_cannot_silently_lose_the_lifecycle_record(monkeypatch):
    """If the BotEvent append blows up AFTER dispatch, the durable lifecycle has
    already recorded the mutation — no silent state change."""
    def boom(event, idem_key=None):
        raise RuntimeError("audit store down")
    monkeypatch.setattr(server, "_append_event", boom)
    quiet = TestClient(server.app, raise_server_exceptions=False)
    r = quiet.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE})
    assert r.status_code == 500                       # loudly surfaced, not swallowed
    rows = server._execution_store().intents_by_state()
    assert len(rows) == 1 and rows[0]["state"] == ol.MODIFIED   # durable evidence exists


def test_dry_run_executes_no_effect_and_persists_no_lifecycle():
    r = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE, "dryRun": True})
    assert r.status_code == 200 and r.json()["dryRun"] is True
    assert server._execution_store().intents_by_state() == []


def test_fixture_route_cannot_reach_a_live_adapter(monkeypatch):
    """With a non-mock adapter active, the safety context reverts to deny-by-default
    and every broker-dispatched command is denied at the safety stage."""
    monkeypatch.setattr(broker_layer, "active_kind", lambda: "mt5")
    r = client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE})
    assert r.status_code == 422
    assert r.json()["detail"]["status"] == "denied"
    assert r.json()["detail"]["stage"] == ex.STAGE_SAFETY


# ── fault injection is test-only and off by default ──────────────────────────

def test_fault_injection_routes_are_disabled_by_default(monkeypatch):
    monkeypatch.delenv(server._FAULT_INJECTION_VAR, raising=False)
    for method, path in (("get", "/api/broker/faults"), ("post", "/api/broker/faults")):
        r = getattr(client, method)(path) if method == "get" else client.post(path, json={})
        assert r.status_code == 404
        assert r.json()["detail"]["code"] == "fault_injection_disabled"


def test_fault_injection_can_be_enabled_explicitly_for_tests(monkeypatch):
    monkeypatch.setenv(server._FAULT_INJECTION_VAR, "1")
    assert client.get("/api/broker/faults").status_code == 200
    assert client.post("/api/broker/faults", json={"clear": True}).status_code == 200


# ── canonical reconciliation on the sync cadence ─────────────────────────────

def test_broker_sync_delegates_to_the_canonical_reconciliation_authority():
    before = server._execution_store().latest_reconciliation()
    r = client.post("/api/broker/sync")
    assert r.status_code == 200
    latest = server._execution_store().latest_reconciliation()
    assert latest is not None and latest != before
    assert latest["recon_id"].startswith("recon_")
    assert latest["clean"] == 1                       # mock world reconciles clean


# ── telemetry + API boundary ─────────────────────────────────────────────────

def test_execution_state_route_is_derived_truthful_and_no_store():
    client.post("/api/commands/SLToBE", json={"tradeId": OPEN_TRADE})
    r = client.get("/api/execution/state")
    assert r.status_code == 200
    assert r.headers["cache-control"] == "no-store"
    body = r.json()
    assert body["adapter"]["kind"] == "mock"
    assert body["adapter"]["provenance"] == "mock-fixture"     # explicit labelling
    assert body["account"]["provenance"] == "mock-fixture"
    assert body["store"]["available"] is True
    assert body["recentFills"][0]["commandName"] == "SLToBE"
    # readiness derives from named gates and is honestly False in the mock world
    gates = body["readiness"]["gates"]
    assert body["readiness"]["tradingReady"] is False
    assert gates["liveAdapterActive"] is False
    assert set(body["availability"]["denialReasons"]) <= set(gates)


def test_readiness_is_derived_not_constant():
    all_green = {"liveAdapterActive": True, "adapterConnected": True,
                 "executionStoreAvailable": True, "reconciliationClean": True,
                 "nodeHealthy": True}
    assert xt.trading_ready(all_green) is True        # derivable, not hard-coded
    assert xt.trading_ready({**all_green, "liveAdapterActive": False}) is False
    gates = xt.readiness_gates(adapter_kind="mock", adapter_connection="Connected",
                               reconciliation={"criticalUnresolved": False, "stale": False},
                               node_healthy=True, store_available=True)
    assert gates["liveAdapterActive"] is False        # the mock can never be live


def test_health_trading_ready_is_now_derived():
    code = code_only("server.py")
    assert '"tradingReady": False' not in code        # the constant is gone
    assert client.get("/api/health").json()["tradingReady"] is False   # derived, still false


def test_degraded_store_is_reported_distinctly():
    class Broken:
        def counts_by_state(self):
            raise RuntimeError("io")
        def intents_by_state(self, *a, **k):
            raise RuntimeError("io")
    body = xt.build(store=Broken(), adapter_kind="mock", adapter_connection="Connected",
                    adapter_provenance="mock-fixture", account_identity=None,
                    execution_mode="observe", reconciliation_posture=None,
                    node_healthy=False, now="2026-07-27T00:00:00Z")
    assert body["store"]["available"] is False
    assert body["store"]["degradedDetail"]            # degraded, not silently empty
    assert body["readiness"]["tradingReady"] is False


def test_execution_state_route_is_auth_protected():
    import auth_policy
    assert auth_policy.is_protected("/api/execution/state")


# ── read-only operator behaviour preserved ───────────────────────────────────

def test_read_only_operator_commands_are_unchanged(monkeypatch):
    import command_transport as ctm
    import transport as tmod
    monkeypatch.setattr(server, "_COMMAND_TRANSPORT",
                        ctm.CommandTransportService(tmod.NullTransport()))
    r = client.post("/api/operator/commands",
                    json={"commandType": "request_health", "idempotencyKey": "arch2-k1"})
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pending" and body["enabled"] is False
    assert body["transport"]["reason"] == "command_transport_disabled"
    # And the operator surface writes nothing into the order-lifecycle store.
    assert server._execution_store().intents_by_state() == []
