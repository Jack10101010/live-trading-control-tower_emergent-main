"""UI-14 — read-only remote node integration.

Most tests drive the client with a controllable fake `Transport`; one exercises the
real selection path against a loopback fake node server. The properties that matter
are truthfulness properties: provenance is always `remote-node`, a failure is a
state and never a fixture substitution, freshness follows the shared rule, the
client only ever READS, and no credential can surface in the result.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import live_telemetry as lt                                           # noqa: E402
import node_client as nc                                              # noqa: E402
import rest_transport as rt                                           # noqa: E402
import server                                                         # noqa: E402
import transport as tmod                                             # noqa: E402
from conftest import code_only                                        # noqa: E402

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def snapshot(published_at=None, **over):
    snap = {
        "schema_version": lt.SCHEMA_VERSION,
        "instance_id": "live-eurusd-golden-001",
        "published_at": published_at or NOW.isoformat().replace("+00:00", "Z"),
        "cycle": {}, "runtime": {}, "engine": {}, "account": {}, "arming": {},
        "market": {}, "reconciliation": {}, "risk": {}, "positions": [], "execution": {},
    }
    snap.update(over)
    return snap


class FakeTransport(tmod.Transport):
    """Records every operation and returns scripted results."""
    kind = "https"

    def __init__(self, *, health=None, telemetry=None):
        self._health = health or tmod.TransportResult(ok=True, available=True, reason="ok")
        self._telemetry = telemetry
        self.operations: list = []
        self.closed = 0

    def connect(self):
        return tmod.ConnectionResult(connected=self._health.ok, reason=self._health.reason)

    def disconnect(self):
        self.operations.append(("disconnect", None))

    def health(self):
        self.operations.append(("health", None))
        return self._health

    def request(self, operation, payload=None):
        self.operations.append((operation, payload))
        return self._telemetry

    def close(self):
        self.closed += 1


def ok_result(payload):
    return tmod.TransportResult(ok=True, available=True, reason="ok", payload=payload)


# ── disabled (the default) ─────────────────────────────────────────────────────

def test_disabled_by_default_reports_disabled_and_pulls_nothing():
    result = nc.poll_node(env={}, now=NOW)
    assert result["enabled"] is False
    assert result["state"] == nc.STATE_DISABLED
    assert result["provenance"] == nc.PROVENANCE
    assert result["telemetry"] is None


def test_disabled_never_touches_the_network(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("disabled integration opened a socket")))
    nc.poll_node(env={}, now=NOW)               # NullTransport => no socket


# ── healthy authenticated node ─────────────────────────────────────────────────

def test_healthy_authenticated_node_is_healthy():
    tr = FakeTransport(telemetry=ok_result(snapshot()))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_HEALTHY
    assert result["enabled"] is True
    assert result["provenance"] == nc.PROVENANCE
    assert result["telemetry"]["available"] is True
    assert result["telemetry"]["instanceId"] == "live-eurusd-golden-001"
    assert result["telemetry"]["stale"] is False
    assert result["telemetry"]["ageSeconds"] == 0.0


def test_health_probe_precedes_telemetry_and_only_reads():
    tr = FakeTransport(telemetry=ok_result(snapshot()))
    nc.poll_node(transport=tr, now=NOW)
    kinds = [op for op, _ in tr.operations]
    assert kinds[0] == "health"
    # The only request path is the read-only telemetry path; no POST, no mutation.
    assert tr.operations[1] == (nc.NODE_TELEMETRY_PATH, None)
    assert all(op in ("health", nc.NODE_TELEMETRY_PATH, "disconnect") for op, _ in tr.operations)


# ── unauthorized ───────────────────────────────────────────────────────────────

def test_unauthorized_when_health_is_401():
    tr = FakeTransport(health=tmod.TransportResult(ok=False, available=True,
                                                   reason="http_status", detail="HTTP 401"))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_UNAUTHORIZED
    assert result["telemetry"] is None          # no fixture fallback
    assert result["health"]["ok"] is False


def test_unauthorized_when_telemetry_is_403():
    tr = FakeTransport(telemetry=tmod.TransportResult(ok=False, available=True,
                                                      reason="http_status", detail="HTTP 403"))
    assert nc.poll_node(transport=tr, now=NOW)["state"] == nc.STATE_UNAUTHORIZED


# ── unreachable ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["timeout", "connection_error"])
def test_unreachable_on_timeout_or_connection_error(reason):
    tr = FakeTransport(health=tmod.TransportResult(ok=False, available=False, reason=reason))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_UNREACHABLE
    assert result["telemetry"] is None


def test_a_transport_that_raises_is_unreachable_not_a_crash():
    class Boom(tmod.Transport):
        kind = "https"
        def connect(self): ...
        def disconnect(self): ...
        def close(self): ...
        def health(self): raise RuntimeError("kaboom")
        def request(self, op, payload=None): raise RuntimeError("kaboom")
    result = nc.poll_node(transport=Boom(), now=NOW)
    assert result["state"] == nc.STATE_UNREACHABLE
    assert "kaboom" not in str(result)          # detail is a type name, not the message text? verify redaction-safe


# ── degraded: malformed / non-2xx / wrong shape ───────────────────────────────

def test_malformed_telemetry_is_degraded_not_accepted():
    tr = FakeTransport(telemetry=ok_result({"not": "a valid snapshot"}))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_DEGRADED
    assert result["reason"] == "malformed_telemetry"
    assert result["telemetry"] is None


def test_non_2xx_telemetry_is_degraded():
    tr = FakeTransport(telemetry=tmod.TransportResult(ok=False, available=True,
                                                      reason="http_status", detail="HTTP 500"))
    assert nc.poll_node(transport=tr, now=NOW)["state"] == nc.STATE_DEGRADED


def test_unsupported_schema_is_degraded():
    tr = FakeTransport(telemetry=ok_result(snapshot(schema_version="ct.node-telemetry.v9")))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_DEGRADED


# ── stale and future telemetry ────────────────────────────────────────────────

def test_stale_telemetry_is_stale_not_healthy():
    old = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    tr = FakeTransport(telemetry=ok_result(snapshot(published_at=old)))
    result = nc.poll_node(transport=tr, now=NOW)
    assert result["state"] == nc.STATE_STALE
    assert result["telemetry"]["stale"] is True


def test_future_dated_telemetry_is_not_healthy():
    """Clock skew / a bad node clock must not read as fresh (shared UI-2 rule)."""
    for offset in (timedelta(hours=1), timedelta(days=3650)):
        future = (NOW + offset).isoformat().replace("+00:00", "Z")
        tr = FakeTransport(telemetry=ok_result(snapshot(published_at=future)))
        result = nc.poll_node(transport=tr, now=NOW)
        assert result["state"] == nc.STATE_STALE, offset
        assert result["telemetry"]["ageSeconds"] >= 0.0


# ── provenance + no fixture fallback ──────────────────────────────────────────

def test_provenance_is_always_remote_node_across_every_state():
    cases = [
        FakeTransport(telemetry=ok_result(snapshot())),                               # healthy
        FakeTransport(health=tmod.TransportResult(ok=False, available=True,
                                                  reason="http_status", detail="HTTP 401")),
        FakeTransport(health=tmod.TransportResult(ok=False, available=False, reason="timeout")),
        FakeTransport(telemetry=ok_result({"bad": 1})),                               # degraded
    ]
    for tr in cases:
        assert nc.poll_node(transport=tr, now=NOW)["provenance"] == "remote-node"


def test_no_state_ever_carries_fixture_or_local_data():
    for tr in (FakeTransport(health=tmod.TransportResult(ok=False, available=False, reason="timeout")),
               FakeTransport(telemetry=ok_result({"bad": 1}))):
        result = nc.poll_node(transport=tr, now=NOW)
        body = str(result).lower()
        assert "fixture" not in body and result["telemetry"] is None
        assert result["provenance"] != "fixture" and result["provenance"] != "local"


# ── recovery after failure ─────────────────────────────────────────────────────

def test_recovers_to_healthy_after_a_transient_failure():
    failing = FakeTransport(health=tmod.TransportResult(ok=False, available=False, reason="timeout"))
    assert nc.poll_node(transport=failing, now=NOW)["state"] == nc.STATE_UNREACHABLE
    recovered = FakeTransport(telemetry=ok_result(snapshot()))
    assert nc.poll_node(transport=recovered, now=NOW)["state"] == nc.STATE_HEALTHY


# ── no secret leakage ──────────────────────────────────────────────────────────

def test_a_token_in_transport_detail_is_redacted_from_the_result():
    leaky = FakeTransport(health=tmod.TransportResult(
        ok=False, available=False, reason="connection_error",
        detail="failed: Authorization: Bearer super-secret-token-value-000000"))
    result = nc.poll_node(transport=leaky, now=NOW)
    assert "super-secret-token-value" not in str(result)
    assert "***redacted***" in str(result["detail"])


def test_result_is_value_free_no_endpoint_or_payload_dump():
    tr = FakeTransport(telemetry=ok_result(snapshot(runtime={"mode": "dry_run"})))
    result = nc.poll_node(transport=tr, now=NOW)
    # Only the freshness envelope is surfaced, never the raw snapshot dict.
    assert set(result["telemetry"]) == {
        "available", "instanceId", "schemaVersion", "publishedAt", "ageSeconds",
        "staleAfterSeconds", "stale", "problem"}


# ── loopback integration through the REAL selection + RestTransport ───────────

class _FakeNode(BaseHTTPRequestHandler):
    token = "node-token-" + "y" * 32
    telemetry_body = None

    def log_message(self, *a):
        return

    def do_GET(self):
        if self.headers.get("Authorization") != f"Bearer {self.token}":
            self.send_response(401); self.send_header("Content-Type", "application/json")
            self.end_headers(); self.wfile.write(b'{"error":"unauthorized"}'); return
        import json as _json
        body = (b'{"ok": true}' if self.path == "/health"
                else _json.dumps(_FakeNode.telemetry_body).encode())
        self.send_response(200); self.send_header("Content-Type", "application/json")
        self.end_headers(); self.wfile.write(body)


@pytest.fixture()
def fake_node_server():
    _FakeNode.telemetry_body = snapshot(
        published_at=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    srv = HTTPServer(("127.0.0.1", 0), _FakeNode)
    th = threading.Thread(target=srv.serve_forever, daemon=True); th.start()
    try:
        yield f"http://127.0.0.1:{srv.server_address[1]}"
    finally:
        srv.shutdown(); srv.server_close(); th.join(timeout=2)


def test_loopback_integration_healthy_through_real_selection(fake_node_server):
    env = {
        tmod.VAR_TRANSPORT_ENABLED: "1",
        "NODE_TRANSPORT": "https",
        "NODE_ENDPOINT": fake_node_server,
        "NODE_API_TOKEN": _FakeNode.token,
        "ALLOW_INSECURE_LOCALHOST": "1",
    }
    # Sanity: the real selector returns a RestTransport for this env.
    assert isinstance(tmod.default_transport(env=env), rt.RestTransport)
    result = nc.poll_node(transport=tmod.default_transport(env=env))
    assert result["state"] == nc.STATE_HEALTHY
    assert result["provenance"] == "remote-node"


def test_loopback_wrong_token_is_unauthorized(fake_node_server):
    env = {
        tmod.VAR_TRANSPORT_ENABLED: "1", "NODE_TRANSPORT": "https",
        "NODE_ENDPOINT": fake_node_server, "NODE_API_TOKEN": "the-wrong-token-000000000000000",
        "ALLOW_INSECURE_LOCALHOST": "1",
    }
    result = nc.poll_node(transport=tmod.default_transport(env=env))
    assert result["state"] == nc.STATE_UNAUTHORIZED


# ── the API route ──────────────────────────────────────────────────────────────

client = TestClient(server.app)


def test_route_reports_disabled_by_default():
    body = client.get("/api/live/remote").json()
    assert body["enabled"] is False
    assert body["state"] == "disabled"
    assert body["provenance"] == "remote-node"
    assert body["telemetry"] is None


def test_route_leaks_no_credential_or_endpoint():
    text = client.get("/api/live/remote").text
    for forbidden in ("Bearer", "NODE_API_TOKEN", "password", "Traceback", "/Users/"):
        assert forbidden not in text


def test_route_is_read_only_no_mutating_verb():
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/api/live/remote").status_code == 405


# ── structural: read-only, no mutation, redaction wired ───────────────────────

def test_module_has_no_mutation_or_command_surface():
    # Code only: the module DOCUMENTS what it forbids ("no mutation, no command, no
    # acknowledgement"), which is prose, not a surface. Strip the docstring + comments.
    import ast
    text = (BACKEND_DIR / "node_client.py").read_text()
    doc = ast.get_docstring(ast.parse(text), clean=False)
    if doc:
        text = text.replace(doc, "", 1)
    code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#")).lower()
    for forbidden in ('method="post"', "acknowledge", "order_send", "arm(",
                      "disarm", "kill_switch", "submit", "close_position",
                      "open_position", "modify_position", "requests.", "httpx"):
        assert forbidden not in code, forbidden
    # CODE ONLY — the docstring names redact_text as a rule, so a whole-file grep
    # is satisfied by prose even with every call site deleted.
    assert "security_config.redact_text" in code_only("node_client.py")


def test_a_uri_credential_in_a_transport_detail_is_also_redacted():
    """Companion to the bearer-token case above: a `user:pass@host` URI embedded in
    a transport error must not survive into the surfaced result either."""
    leaky = FakeTransport(health=tmod.TransportResult(
        ok=False, available=False, reason="connection_error",
        detail="connect failed for https://operator:S3CRET-PASSWORD@node.internal/health"))
    result = nc.poll_node(transport=leaky, now=NOW)
    assert "S3CRET-PASSWORD" not in str(result)
    assert "***redacted***" in str(result["detail"])


def test_poll_node_actually_calls_the_canonical_telemetry_functions(monkeypatch):
    """BEHAVIOURAL: prove REUSE of the UI-2 contract rather than a re-implementation.

    A source grep is satisfied by the module docstring, which names both functions;
    wrapping them proves they are genuinely on the code path."""
    seen: list[str] = []
    real_validate = nc.live_telemetry.validate_snapshot
    real_observation = nc.live_telemetry.observation

    def validate(payload):
        seen.append("validate_snapshot")
        return real_validate(payload)

    def observation(snap, **kw):
        seen.append("observation")
        return real_observation(snap, **kw)

    monkeypatch.setattr(nc.live_telemetry, "validate_snapshot", validate)
    monkeypatch.setattr(nc.live_telemetry, "observation", observation)

    tr = FakeTransport(telemetry=ok_result(snapshot()))
    result = nc.poll_node(transport=tr, now=NOW)

    assert result["state"] in (nc.STATE_HEALTHY, nc.STATE_STALE)
    assert seen == ["validate_snapshot", "observation"]


def test_module_does_not_duplicate_the_telemetry_model():
    """It REUSES validate_snapshot + observation, never redefining the schema."""
    source = code_only("node_client.py")      # docstring names both; strip it
    assert "live_telemetry.validate_snapshot" in source
    assert "live_telemetry.observation" in source
    assert "SCHEMA_VERSION =" not in source
