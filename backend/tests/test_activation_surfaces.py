"""M-ACTIVATE-READINESS-2 — six surfaces, one set of facts.

WHY THIS IS A SEPARATE SUITE

    Every endpoint here derives from the same stored observation, so it is easy
    to write a "consistency" test that proves only that a function is
    deterministic. The assertions below are chosen to be ones that would break
    if two PROJECTIONS drifted — which is the defect this programme kept
    finding: `/api/operations/nodes` dropping a node the ingest path had
    accepted, `/api/health` claiming a connected node the projection refused,
    `/api/dev/fixture-packages/active` mounted but answering 404.

    Where two surfaces intentionally differ, the difference is asserted as
    intentional rather than left for a reader to wonder about.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check                                            # noqa: E402
import activation_payloads as pay                                  # noqa: E402
import broker_provenance as bp                                     # noqa: E402
import node_provenance as np_                                      # noqa: E402
import server                                                      # noqa: E402
from fastapi.testclient import TestClient                          # noqa: E402

client = TestClient(server.app)

SURFACES = ("/api/health", "/api/live/status", "/api/live/connection",
            "/api/operations/nodes", "/api/operations/accounts",
            "/api/operations/summary")


@pytest.fixture()
def isolated_observations(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    prior = dict(server._LIVE_STATUS)
    server._LIVE_STATUS.clear()
    server._LIVE_GATE.clear()
    try:
        yield
    finally:
        server._LIVE_STATUS.clear()
        server._LIVE_STATUS.update(prior)
        server._LIVE_GATE.clear()


def _ingest(payload):
    return client.post("/api/live/ingest", json=payload)


def _all():
    return {path: client.get(path).json() for path in SURFACES}


# ══════════════════════════════════════════════════════════════════════════════
# Shared facts agree
# ══════════════════════════════════════════════════════════════════════════════

def test_every_surface_answers_before_any_observation_exists(isolated_observations):
    for path in SURFACES:
        response = client.get(path)
        assert response.status_code == 200, (path, response.status_code)


def test_instance_identity_agrees_across_surfaces(isolated_observations):
    assert _ingest(pay.account_payload()).status_code == 200
    bodies = _all()

    received = set(bodies["/api/live/status"].get("instances") or [])
    projected = {n["nodeId"] for n in bodies["/api/operations/nodes"]["nodes"]
                 if n["provenance"] == np_.PROV_NODE_TELEMETRY}
    summarised = {n["nodeId"] for n in bodies["/api/operations/summary"]["nodes"]
                  if n["provenance"] == np_.PROV_NODE_TELEMETRY}
    healthy = set(bodies["/api/health"].get("nodeTelemetry", {}).get("instances") or [])

    assert received == projected == summarised == healthy == {"vps-node-1"}
    assert bodies["/api/health"]["liveNodeConnected"] is True


def test_freshness_facts_agree_between_receipt_and_projection(isolated_observations):
    assert _ingest(pay.account_payload()).status_code == 200
    bodies = _all()
    entry = bodies["/api/live/status"]["statuses"]["vps-node-1"]
    node = bodies["/api/operations/nodes"]["nodes"][0]

    assert entry["freshness_basis"] == node["freshnessBasis"] == "received_at"
    assert bool(entry["stale"]) is (node["lifecycleState"] == np_.NODE_STALE)
    # The projection may only ever be MORE pessimistic than the envelope.
    assert node["freshness"]["stale"] >= bool(entry["stale"])


def test_account_facts_agree_between_accounts_and_summary(isolated_observations):
    assert _ingest(pay.account_payload()).status_code == 200
    bodies = _all()
    accounts = bodies["/api/operations/accounts"]["accounts"]
    summarised = bodies["/api/operations/summary"]["accounts"]

    # Everything EXCEPT the projection clock. The two endpoints are two HTTP
    # calls at two instants, so `projectionAt` and `ageSeconds` differ by
    # milliseconds by construction — comparing them would have made this test
    # fail for a reason that is not a defect, and the usual repair for that is
    # to delete the test.
    def _stable(record):
        record = dict(record)
        freshness = dict(record.pop("freshness") or {})
        return record, {k: freshness.get(k) for k in ("stale", "available", "status")}

    assert [_stable(a) for a in accounts] == [_stable(a) for a in summarised], (
        "two projections of one fact disagree")

    account = accounts[0]
    for field in ("provenance", "admitted", "admissionReasons",
                  "accountFingerprint", "server", "balance", "nodeId"):
        assert field in account, field
    assert account["admitted"] is True
    assert account["admissionReasons"] == []


def test_a_refused_account_is_refused_identically_on_every_surface(
        isolated_observations, monkeypatch):
    import activation_policy as ap
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    assert _ingest(pay.mismatched_account_payload()).status_code == 200
    bodies = _all()
    for path in ("/api/operations/accounts", "/api/operations/summary"):
        records = bodies[path]["accounts"]
        assert [r["admitted"] for r in records] == [False], path
        assert ap.R_ACCOUNT_IDENTITY_MISMATCH in records[0]["admissionReasons"], path


def test_the_health_surface_never_claims_broker_truth_from_a_node(isolated_observations):
    """`liveNodeConnected` is a NODE fact. It must not move `tradingReady` or
    imply an account."""
    assert _ingest(pay.canonical_payload()).status_code == 200
    health = client.get("/api/health").json()
    assert health["liveNodeConnected"] is True
    assert health["tradingReady"] is False
    assert health["brokerKind"] == "mock"
    admitted = [a for a in client.get("/api/operations/accounts").json()["accounts"]
                if bp.is_broker_truth(a["provenance"])]
    assert admitted == []


# ══════════════════════════════════════════════════════════════════════════════
# Intentional differences, stated
# ══════════════════════════════════════════════════════════════════════════════

def test_intentional_differences_are_documented_not_accidental(isolated_observations):
    """Three places where two surfaces legitimately say different things.

    1. `/api/health.backendMode` reports whether the DEV ASSET IS READABLE, not
       whether a fixture is being served. Presence is not activation.
    2. `/api/operations/nodes` leaves the tower's own facts null on a node card;
       `/api/health` reports those same facts about the tower itself. Same
       words, different subjects.
    3. `/api/operations/accounts` carries REFUSED records; the UI gate drops
       them. The endpoint reports what was observed, the gate decides what may
       be believed.
    """
    assert _ingest(pay.account_payload()).status_code == 200
    health = client.get("/api/health").json()
    node = client.get("/api/operations/nodes").json()["nodes"][0]

    assert health["backendMode"] in ("fixture", "runtime")
    assert node["adapter"] is None and health["brokerKind"] == "mock", (
        "the node card must not carry the tower's adapter")
    assert node["connectionState"] is None


# ══════════════════════════════════════════════════════════════════════════════
# The end-to-end contradiction: one surface made inconsistent on purpose
# ══════════════════════════════════════════════════════════════════════════════

def test_the_checker_FAILS_when_one_surface_is_made_inconsistent(
        isolated_observations, monkeypatch):
    """PHASE 9's requirement, executed rather than asserted in prose.

    `/api/operations/nodes` is emptied while `/api/live/status` still reports the
    node. Nothing else changes, no endpoint errors, every response is a valid
    200 — exactly the shape of the defect this check exists for.
    """
    assert _ingest(pay.account_payload()).status_code == 200

    def _get(base, path, token):
        body = client.get(path).json()
        if path == "/api/operations/nodes":
            body = {**body, "nodes": []}
        return 200, body

    monkeypatch.setattr(activation_check, "_get", _get)
    report = activation_check.run(base="http://testserver")
    failures = {c.name for c in report.failed}
    assert "ui.node_inventories_agree" in failures, [c.name for c in report.checks]
    assert report.verdict == activation_check.FAIL


def test_the_checker_passes_when_the_surfaces_agree(isolated_observations, monkeypatch):
    """The positive control for the test above."""
    assert _ingest(pay.account_payload()).status_code == 200
    monkeypatch.setattr(activation_check, "_get",
                        lambda base, path, token: (200, client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    assert "ui.node_inventories_agree" not in {c.name for c in report.failed}
