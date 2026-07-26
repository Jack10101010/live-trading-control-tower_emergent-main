"""UI-17 — the authenticated read-only operator command routes.

These routes expose the UI-16 command transport through `/api/operator/commands`.
The properties pinned here are: the routes are deny-by-default protected (UI-11), only
the read-only vocabulary is representable, expiry + idempotency are required, the
disabled/unreachable transport stays truthful, responses are redaction-safe, and the
surface never touches the fixture-world `/api/commands/{name}` execution path or the
event store.
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

import auth_policy                                                   # noqa: E402
import command_transport as ctm                                     # noqa: E402
import server                                                       # noqa: E402
import transport as t                                               # noqa: E402

client = TestClient(server.app)

TOKEN = "operator-token-" + "z" * 32


# ── canned in-process fake transport (non-null → service enabled) ─────────────

def _ok(payload=None):
    return t.TransportResult(ok=True, available=True, reason="ok",
                             payload=payload if payload is not None else {"ok": True})


def _fail(reason, detail="", available=True):
    return t.TransportResult(ok=False, available=available, reason=reason, detail=detail)


class FakeTransport(t.Transport):
    kind = "https"

    def __init__(self, health_result=None, request_result=None):
        self._health = health_result if health_result is not None else _ok()
        self._request = request_result if request_result is not None else _ok()
        self.health_calls = 0
        self.request_calls = 0

    def connect(self):
        return t.ConnectionResult(connected=True, reason="ok")

    def disconnect(self):
        return None

    def health(self):
        self.health_calls += 1
        return self._health

    def request(self, operation, payload=None):
        self.request_calls += 1
        return self._request

    def close(self):
        return None


@pytest.fixture(autouse=True)
def reset_service(monkeypatch):
    """Every test starts from a fresh, DISABLED service and auth OFF, unless it
    overrides them. Prevents cross-test bleed through the module singleton."""
    monkeypatch.setattr(server, "_COMMAND_TRANSPORT",
                        ctm.CommandTransportService(t.NullTransport()))
    monkeypatch.setattr(server, "_AUTH_POLICY", auth_policy.load_policy({}))
    yield


def use_transport(monkeypatch, transport):
    """Wire a fresh service around `transport` and return the transport (so tests can
    assert on its call counters)."""
    svc = ctm.CommandTransportService(transport)
    monkeypatch.setattr(server, "_COMMAND_TRANSPORT", svc)
    return transport


def enforce_auth(monkeypatch):
    policy = auth_policy.load_policy({
        auth_policy.VAR_ENABLED: "true", auth_policy.VAR_TOKEN: TOKEN})
    assert policy.enforcing
    monkeypatch.setattr(server, "_AUTH_POLICY", policy)


def bearer(token=TOKEN):
    return {"Authorization": f"Bearer {token}"}


def submit_body(command_type="request_health", idempotency_key="idem-1", **over):
    body = {"commandType": command_type, "idempotencyKey": idempotency_key}
    body.update(over)
    return body


# ── authentication ─────────────────────────────────────────────────────────────

def test_routes_are_protected_and_reject_unauthenticated_requests(monkeypatch):
    enforce_auth(monkeypatch)
    assert client.post("/api/operator/commands", json=submit_body()).status_code == 401
    assert client.get("/api/operator/commands").status_code == 401
    assert client.get("/api/operator/commands/cmd_x").status_code == 401


def test_a_wrong_or_malformed_token_is_still_unauthorized(monkeypatch):
    enforce_auth(monkeypatch)
    for hdr in ({"Authorization": "Bearer wrong-token"},
                {"Authorization": "Basic abc"}, {"Authorization": TOKEN}):
        assert client.post("/api/operator/commands", json=submit_body(),
                           headers=hdr).status_code == 401


def test_the_correct_token_is_authorized(monkeypatch):
    enforce_auth(monkeypatch)
    use_transport(monkeypatch, FakeTransport(health_result=_ok()))
    r = client.post("/api/operator/commands", json=submit_body(), headers=bearer())
    assert r.status_code == 200
    assert r.json()["state"] == "completed"


def test_the_operator_routes_are_not_in_the_public_allowlist():
    # Deny-by-default: only /api/health is public; the operator surface is protected.
    for path in ("/api/operator/commands",):
        assert auth_policy.is_protected(path)


# ── valid read-only submission (auth off by default) ──────────────────────────

def test_a_valid_command_completes_when_the_transport_is_enabled(monkeypatch):
    fake = use_transport(monkeypatch, FakeTransport(request_result=_ok({"instance_id": "n1"})))
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="request_telemetry"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "completed"
    assert body["commandType"] == "request_telemetry"
    assert body["enabled"] is True
    assert body["acknowledged"] is True and body["completed"] is True


@pytest.mark.parametrize("ctype", ["noop", "request_health", "request_telemetry"])
def test_every_permitted_type_is_accepted(monkeypatch, ctype):
    use_transport(monkeypatch, FakeTransport(request_result=_ok(), health_result=_ok()))
    r = client.post("/api/operator/commands", json=submit_body(command_type=ctype))
    assert r.status_code == 200 and r.json()["state"] == "completed"


# ── forbidden command rejection ────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["pause", "resume", "arm", "disarm", "close_position",
                                 "order_send", "cancel", "kill", "flatten", "unknown", ""])
def test_forbidden_or_unknown_commands_are_rejected(monkeypatch, bad):
    fake = use_transport(monkeypatch, FakeTransport())
    r = client.post("/api/operator/commands", json=submit_body(command_type=bad))
    assert r.status_code == 422
    assert r.json()["code"] == "unknown_command_type"
    assert fake.health_calls == 0 and fake.request_calls == 0   # never transmitted


def test_missing_idempotency_key_is_rejected(monkeypatch):
    use_transport(monkeypatch, FakeTransport())
    r = client.post("/api/operator/commands", json={"commandType": "noop"})
    assert r.status_code == 422 and r.json()["code"] == "idempotency_key_required"


# ── disabled / unreachable transport stays truthful ───────────────────────────

def test_the_disabled_default_transmits_nothing_but_records_truthfully():
    # reset_service leaves a NullTransport → disabled.
    r = client.post("/api/operator/commands", json=submit_body())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["state"] == "pending"                    # never a fabricated success
    assert body["transport"]["reason"] == "command_transport_disabled"


def test_an_unreachable_transport_leaves_the_command_pending(monkeypatch):
    use_transport(monkeypatch, FakeTransport(
        request_result=_fail("timeout", "timed out", available=False)))
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="request_telemetry"))
    assert r.status_code == 200
    body = r.json()
    assert body["state"] == "pending" and body["acknowledged"] is False
    assert body["transport"]["reason"] == "node_timeout"


def test_a_remote_rejection_maps_to_rejected(monkeypatch):
    use_transport(monkeypatch, FakeTransport(
        request_result=_fail("http_status", "HTTP 403")))
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="request_telemetry"))
    assert r.json()["state"] == "rejected"


# ── idempotent duplicate handling ──────────────────────────────────────────────

def test_a_duplicate_idempotency_key_returns_the_same_command_without_resending(monkeypatch):
    fake = use_transport(monkeypatch, FakeTransport(request_result=_ok()))
    first = client.post("/api/operator/commands",
                        json=submit_body(command_type="request_telemetry", idempotency_key="dup"))
    second = client.post("/api/operator/commands",
                         json=submit_body(command_type="request_telemetry", idempotency_key="dup"))
    assert first.json()["commandId"] == second.json()["commandId"]
    assert fake.request_calls == 1                       # sent exactly once


# ── status and recent-history routes ──────────────────────────────────────────

def test_status_route_returns_the_record_or_404(monkeypatch):
    use_transport(monkeypatch, FakeTransport(request_result=_ok()))
    submitted = client.post("/api/operator/commands",
                            json=submit_body(command_type="request_telemetry")).json()
    cid = submitted["commandId"]
    got = client.get(f"/api/operator/commands/{cid}")
    assert got.status_code == 200 and got.json()["commandId"] == cid
    assert client.get("/api/operator/commands/cmd_missing").status_code == 404


def test_recent_route_lists_newest_first_and_reports_enablement(monkeypatch):
    use_transport(monkeypatch, FakeTransport(request_result=_ok(), health_result=_ok()))
    client.post("/api/operator/commands", json=submit_body(command_type="noop", idempotency_key="a"))
    client.post("/api/operator/commands", json=submit_body(command_type="request_telemetry", idempotency_key="b"))
    recent = client.get("/api/operator/commands").json()
    assert recent["enabled"] is True
    assert len(recent["commands"]) == 2
    assert recent["commands"][0]["idempotencyKey"] == "b"   # newest first


# ── no execution / fixture-command dependency ─────────────────────────────────

def test_the_operator_surface_is_a_distinct_path_from_the_fixture_command_route():
    # The fixture-world mock lives at /api/commands/{name}; the operator surface is
    # /api/operator/commands. They must never be the same route.
    paths = {getattr(r, "path", None) for r in server.app.routes}
    assert "/api/operator/commands" in paths
    assert "/api/commands/{name}" in paths                # the fixture route still exists
    assert "/api/operator/commands" != "/api/commands/{name}"


def test_submitting_an_operator_command_appends_no_bot_event(monkeypatch):
    # The fixture command path appends a BotEvent; the operator surface must NOT —
    # it neither executes nor writes to the audit/event store.
    use_transport(monkeypatch, FakeTransport(request_result=_ok()))
    before = client.get("/api/events").json()
    client.post("/api/operator/commands", json=submit_body(command_type="request_telemetry"))
    after = client.get("/api/events").json()
    assert after == before                               # no event was created


# ── no secret leakage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret_payload", [
    {"api_token": "abc"}, {"password": "hunter2"},
    {"nested": {"authorization": "Bearer x"}},
])
def test_a_secret_bearing_payload_is_rejected(monkeypatch, secret_payload):
    fake = use_transport(monkeypatch, FakeTransport())
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="noop", payload=secret_payload))
    assert r.status_code == 422 and r.json()["code"] == "payload_contains_secret"
    assert fake.request_calls == 0


def test_a_transport_failure_detail_is_redacted_in_the_response(monkeypatch):
    leaky = _fail("connection_error", "token=SUPERSECRETVALUE leaked", available=False)
    use_transport(monkeypatch, FakeTransport(request_result=leaky))
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="request_telemetry"))
    assert "SUPERSECRETVALUE" not in r.text


def test_no_response_carries_a_token_field(monkeypatch):
    enforce_auth(monkeypatch)
    use_transport(monkeypatch, FakeTransport(request_result=_ok()))
    r = client.post("/api/operator/commands",
                    json=submit_body(command_type="request_telemetry"), headers=bearer())
    assert TOKEN not in r.text                            # the auth token never echoes back
