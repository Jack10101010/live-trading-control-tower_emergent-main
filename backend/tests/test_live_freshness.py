"""Server-authoritative node freshness for GET /api/live/status.

The defect these cover: the endpoint returned the last received payload verbatim,
with no notion of age. A node could die and the API would keep serving its final
payload indefinitely, indistinguishable from a live one. Freshness is now computed
by the SERVER, from the SERVER clock, so a node cannot assert its own health.

Thresholds are reused from `live/status.py` / `live/deploy_check.py` (900s while a
recompute is in flight, 120s idle) rather than invented here.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

BACKEND = Path(__file__).resolve().parent.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

import server as srv  # noqa: E402

client = TestClient(srv.app)
INST = "live-eurusd-golden-001"


@pytest.fixture(autouse=True)
def _clean():
    srv._LIVE_STATUS.clear()
    srv._LIVE_META.clear()
    yield
    srv._LIVE_STATUS.clear()
    srv._LIVE_META.clear()


def payload(phase="idle", instance=INST, boundary="2026-07-29 10:00:00+00:00"):
    return {"instance_id": instance, "symbol": "EURUSD", "mode": "dry_run",
            "engine_version": "5bb6372c", "phase": phase,
            "at": datetime.now(timezone.utc).isoformat(),
            "runner": {"status": "no_new_bar", "boundary": boundary},
            "intents": [], "execution": {}}


def age_by(seconds, instance=INST):
    """Rewind the SERVER-recorded last_seen; never the node's self-reported `at`."""
    m = srv._LIVE_META[instance]
    m["last_seen"] = (datetime.fromisoformat(m["last_seen"])
                      - timedelta(seconds=seconds)).isoformat()


# ── first startup ─────────────────────────────────────────────────────────────
def test_no_nodes_at_first_startup():
    r = client.get("/api/live/status")
    assert r.status_code == 200
    assert r.json()["instances"] == [] and r.json()["statuses"] == {}
    assert "server_time" in r.json()


def test_unknown_instance_is_404_not_a_fabricated_healthy_node():
    assert client.get(f"/api/live/status?instance_id={INST}").status_code == 404


def test_freshness_for_never_seen_instance_is_not_online():
    f = srv._live_freshness("nobody")
    assert f["state"] == "unknown" and f["online"] is False and f["stale"] is True
    assert f["age_seconds"] is None and f["ingest_count"] == 0
    assert "no telemetry received since backend start" in f["detail"]


# ── healthy node ──────────────────────────────────────────────────────────────
def test_healthy_node_reports_online():
    assert client.post("/api/live/ingest", json=payload()).status_code == 200
    f = client.get(f"/api/live/status?instance_id={INST}").json()["freshness"]
    assert f["state"] == "healthy" and f["online"] is True and f["stale"] is False
    assert f["age_seconds"] < 5 and f["ingest_count"] == 1
    assert f["first_seen"] and f["last_seen"] and f["last_successful_ingest"]


def test_existing_payload_fields_are_preserved_verbatim():
    """Additive contract: nothing a previous client read may move or change."""
    p = payload()
    client.post("/api/live/ingest", json=p)
    body = client.get(f"/api/live/status?instance_id={INST}").json()
    for k, v in p.items():
        assert body[k] == v, f"field {k} was altered"
    assert "freshness" in body


# ── stale transitions, at the exact policy boundaries ─────────────────────────
def test_idle_node_goes_stale_after_120s():
    client.post("/api/live/ingest", json=payload(phase="idle"))
    age_by(119)
    assert srv._live_freshness(INST)["stale"] is False
    age_by(2)                                    # now 121s
    f = srv._live_freshness(INST)
    assert f["stale"] is True and f["state"] == "stale" and f["online"] is False
    assert f["stale_limit_seconds"] == 120
    assert "no telemetry for" in f["detail"]


def test_recomputing_node_is_allowed_the_full_bar_budget():
    """A ~1119s warm recompute must not be mislabelled dead at 120s."""
    client.post("/api/live/ingest", json=payload(phase="cycle_running"))
    age_by(600)
    f = srv._live_freshness(INST)
    assert f["stale"] is False and f["stale_limit_seconds"] == 900


def test_recomputing_node_past_the_bar_budget_is_stale():
    """Overrunning 900s IS stalled — same reading as live.status."""
    client.post("/api/live/ingest", json=payload(phase="cycle_running"))
    age_by(901)
    assert srv._live_freshness(INST)["stale"] is True


def test_absent_phase_falls_back_conservatively():
    """An older node build omits `phase`; do not declare it dead at 120s."""
    p = payload(); p.pop("phase")
    client.post("/api/live/ingest", json=p)
    age_by(200)
    f = srv._live_freshness(INST)
    assert f["phase"] is None and f["stale_limit_seconds"] == 900 and f["stale"] is False


# ── the actual defect ─────────────────────────────────────────────────────────
def test_stale_payload_is_never_presented_as_current():
    """THE regression guard: a dead node's last payload must be labelled stale."""
    client.post("/api/live/ingest", json=payload(phase="idle"))
    age_by(3600)
    body = client.get(f"/api/live/status?instance_id={INST}").json()
    assert body["runner"]["boundary"] == "2026-07-29 10:00:00+00:00"   # payload retained
    assert body["freshness"]["stale"] is True                          # but labelled
    assert body["freshness"]["online"] is False


def test_node_cannot_assert_its_own_freshness():
    """Age comes from the server clock, not the node's `at`. A skewed or lying
    node must not be able to make itself look fresh."""
    client.post("/api/live/ingest", json=payload(phase="idle"))
    age_by(3600)
    srv._LIVE_STATUS[INST]["at"] = datetime.now(timezone.utc).isoformat()  # node claims "now"
    assert srv._live_freshness(INST)["stale"] is True


# ── reconnect ─────────────────────────────────────────────────────────────────
def test_reconnect_after_outage_restores_health_and_keeps_first_seen():
    client.post("/api/live/ingest", json=payload())
    first = srv._LIVE_META[INST]["first_seen"]
    age_by(3600)
    assert srv._live_freshness(INST)["stale"] is True
    client.post("/api/live/ingest", json=payload())               # node returns
    f = srv._live_freshness(INST)
    assert f["stale"] is False and f["state"] == "healthy"
    assert f["first_seen"] == first and f["ingest_count"] == 2


def test_multiple_sequential_ingests_advance_last_seen_only():
    client.post("/api/live/ingest", json=payload())
    first = srv._LIVE_META[INST]["first_seen"]
    for _ in range(4):
        client.post("/api/live/ingest", json=payload())
    f = srv._live_freshness(INST)
    assert f["ingest_count"] == 5 and f["first_seen"] == first
    assert f["last_seen"] >= first


# ── backend restart: no persistence, by design ────────────────────────────────
def test_backend_restart_reports_no_telemetry_not_stale_cache():
    client.post("/api/live/ingest", json=payload())
    srv._LIVE_STATUS.clear(); srv._LIVE_META.clear()              # simulate restart
    assert client.get(f"/api/live/status?instance_id={INST}").status_code == 404
    assert client.get("/api/live/status").json()["instances"] == []


# ── multi-node + contract ─────────────────────────────────────────────────────
def test_multiple_nodes_are_aged_independently():
    client.post("/api/live/ingest", json=payload(instance="node-a"))
    client.post("/api/live/ingest", json=payload(instance="node-b"))
    age_by(3600, "node-a")
    listing = client.get("/api/live/status").json()
    assert listing["freshness"]["node-a"]["stale"] is True
    assert listing["freshness"]["node-b"]["stale"] is False
    assert sorted(listing["instances"]) == ["node-a", "node-b"]


def test_ingest_still_rejects_payload_without_instance_id():
    assert client.post("/api/live/ingest", json={"no": "id"}).status_code == 400


def test_ingest_response_contract_unchanged():
    body = client.post("/api/live/ingest", json=payload()).json()
    assert body["ok"] is True and "seq" in body and "deduplicated" in body
