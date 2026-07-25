"""UI-0 — truthful runtime-health contract for `GET /api/health`.

The endpoint previously returned the frozen fixture `meta.asOf` as `asOf`, which
read as a freshness timestamp and was not one. These tests pin the honest
semantics: real server clock, explicit fixture/mock disclosure, no trading-ready
claim, and no implication that a mock broker means MT5 is connected.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import server                                                          # noqa: E402

client = TestClient(server.app)


def _health() -> dict:
    r = client.get("/api/health")
    assert r.status_code == 200
    return r.json()


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


# ── server time is the real clock, never the fixture's ────────────────────────

def test_server_time_is_current_not_fixture_as_of():
    h = _health()
    fixture_as_of = server.WORLD.get("meta", {}).get("asOf")
    assert h["serverTime"] != fixture_as_of
    now = datetime.now(timezone.utc)
    assert abs((_parse(h["serverTime"]) - now)) < timedelta(minutes=5)


def test_fixture_timestamp_is_still_reported_but_clearly_scoped():
    h = _health()
    # Kept for provenance, but nested where it cannot be read as freshness.
    assert h["fixture"]["asOf"] == server.WORLD.get("meta", {}).get("asOf")
    assert "asOf" not in h, "a top-level asOf reads as freshness — it must not return"


def test_server_time_advances_between_calls():
    first = _parse(_health()["serverTime"])
    second = _parse(_health()["serverTime"])
    assert second >= first


# ── the response discloses fixture/mock mode truthfully ──────────────────────

def test_reports_fixture_backend_mode_and_mock_broker():
    h = _health()
    assert h["status"] == "ok"
    assert h["scope"] == "process"
    assert h["backendMode"] == "fixture"
    assert h["brokerKind"] == "mock"
    assert h["dataSources"] == {
        "world": "fixture",
        "broker": "mock",
        "nodeTelemetry": "unavailable",
    }
    assert h["fixture"]["available"] is True


def test_never_claims_trading_readiness():
    assert _health()["tradingReady"] is False


def test_mock_broker_does_not_imply_a_connected_live_node():
    h = _health()
    assert h["brokerKind"] == "mock"
    assert h["liveNodeConnected"] is False
    assert h["nodeTelemetry"] == {"connected": False, "instances": []}


# ── live-node presence is derived from real telemetry only ───────────────────

def test_live_node_connected_tracks_actual_published_telemetry():
    """`liveNodeConnected` must be true only when a node really published."""
    original = dict(server._LIVE_STATUS)
    try:
        server._LIVE_STATUS["ui0-test-instance"] = {"instance_id": "ui0-test-instance"}
        h = _health()
        assert h["liveNodeConnected"] is True
        assert h["nodeTelemetry"]["instances"] == ["ui0-test-instance"]
        assert h["dataSources"]["nodeTelemetry"] == "available"
        # Still not trading-ready: this backend has no live execution path.
        assert h["tradingReady"] is False
    finally:
        server._LIVE_STATUS.clear()
        server._LIVE_STATUS.update(original)


def test_health_exposes_no_credentials_or_paths():
    body = client.get("/api/health").text
    for forbidden in ("password", "PASSWORD", "MT5_LOGIN", "Traceback", "/Users/"):
        assert forbidden not in body
