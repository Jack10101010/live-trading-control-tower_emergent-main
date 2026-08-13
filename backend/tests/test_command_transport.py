"""UI-16 — the safe, read-only command transport.

Every test runs against an in-process FAKE transport or a loopback HTTP server bound
to 127.0.0.1 — never the VPS, the internet, or any external service. The properties
that matter most are negative: disabled by default, read-only only, no retry, no
mutating command, no secret leakage, and no execution dependency. The lifecycle
mapping (acknowledged-then-completed, rejected, acknowledged-but-failed, or
left-pending-when-unreachable) is pinned exactly.
"""

from __future__ import annotations

import json
import socket
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

import command_channel as cc                                         # noqa: E402
import command_transport as ct                                      # noqa: E402
import rest_transport as rt                                         # noqa: E402
import transport as t                                              # noqa: E402

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def envelope(**over):
    data = {
        "schema_version": cc.SCHEMA_VERSION,
        "command_type": cc.CMD_REQUEST_TELEMETRY,
        "idempotency_key": "idem-1",
        "requested_at": _iso(NOW),
        "expires_at": _iso(NOW + timedelta(minutes=5)),
        "payload": {},
    }
    data.update(over)
    return data


# ── a configurable in-process fake transport (non-null → service enabled) ─────

def _ok(payload=None):
    return t.TransportResult(ok=True, available=True, reason="ok",
                             payload=payload if payload is not None else {"ok": True})


def _fail(reason, detail="", available=True):
    return t.TransportResult(ok=False, available=available, reason=reason, detail=detail)


class FakeTransport(t.Transport):
    """Records calls and returns canned results. `kind` is non-null so the service
    treats it as enabled. Opens no socket."""

    kind = "https"

    def __init__(self, health_result=None, request_result=None):
        self._health = health_result if health_result is not None else _ok()
        self._request = request_result if request_result is not None else _ok()
        self.health_calls = 0
        self.request_calls = 0
        self.seen_ops: list = []
        self.seen_payloads: list = []

    def connect(self):
        return t.ConnectionResult(connected=True, reason="ok")

    def disconnect(self):
        return None

    def health(self):
        self.health_calls += 1
        self.seen_ops.append("health()")
        return self._health

    def request(self, operation, payload=None):
        self.request_calls += 1
        self.seen_ops.append(operation)
        self.seen_payloads.append(payload)
        return self._request

    def close(self):
        return None


def service(transport):
    return ct.CommandTransportService(transport)


# ── disabled default ───────────────────────────────────────────────────────────

def test_default_service_is_disabled_and_uses_null_transport():
    svc = ct.default_command_transport(env={})
    assert svc.enabled is False
    assert isinstance(svc._transport, t.NullTransport)


def test_disabled_service_records_but_transmits_nothing():
    null = t.NullTransport()
    svc = ct.CommandTransportService(null)
    rec = svc.submit(envelope(), now=NOW)
    # Recorded for audit, still pending, nothing acknowledged or completed.
    assert rec.state == cc.STATE_PENDING
    assert rec.acknowledgement is None and rec.outcome is None
    assert svc.view(rec.envelope.command_id)["transport"]["reason"] == ct.REASON_TRANSPORT_DISABLED
    assert svc.enabled is False


# ── valid read-only submission ────────────────────────────────────────────────

def test_valid_telemetry_submission_completes_via_the_telemetry_endpoint():
    fake = FakeTransport(request_result=_ok({"instance_id": "n1"}))
    svc = service(fake)
    rec = svc.submit(envelope(command_type=cc.CMD_REQUEST_TELEMETRY), now=NOW)
    assert rec.state == cc.STATE_COMPLETED
    assert fake.seen_ops == [ct.NODE_TELEMETRY_PATH]      # GET /telemetry, once
    assert fake.request_calls == 1 and fake.health_calls == 0


def test_request_health_and_noop_dispatch_to_the_health_probe():
    for ctype in (cc.CMD_REQUEST_HEALTH, cc.CMD_NOOP):
        fake = FakeTransport(health_result=_ok({"status": "ok"}))
        svc = service(fake)
        rec = svc.submit(envelope(command_type=ctype, idempotency_key=ctype), now=NOW)
        assert rec.state == cc.STATE_COMPLETED
        assert fake.health_calls == 1 and fake.request_calls == 0


def test_payload_is_never_transmitted():
    fake = FakeTransport()
    svc = service(fake)
    svc.submit(envelope(command_type=cc.CMD_REQUEST_TELEMETRY,
                        payload={"note": "context"}), now=NOW)
    # The transport is GET-only; the request payload is always None on the wire.
    assert fake.seen_payloads == [None]


# ── acknowledgement distinct from completion ──────────────────────────────────

def test_success_records_acknowledgement_and_completion_as_distinct_steps():
    svc = service(FakeTransport(request_result=_ok()))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.acknowledgement is not None and rec.acknowledgement.accepted is True
    assert rec.outcome is not None and rec.outcome.state == cc.STATE_COMPLETED
    # Two distinct lifecycle objects — ack is admission, outcome is the result.
    assert rec.acknowledgement.acknowledged_at is not None
    assert rec.outcome.completed_at is not None


def test_a_usable_answer_completes_but_an_unusable_one_only_acknowledges():
    """The defining distinction: a malformed answer is ACKNOWLEDGED (the node replied)
    yet NOT completed (the reply was unusable) — completion is not acknowledgement."""
    svc = service(FakeTransport(request_result=_fail("malformed_response", "bad json")))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.acknowledgement is not None and rec.acknowledgement.accepted is True
    assert rec.state == cc.STATE_FAILED and rec.state != cc.STATE_COMPLETED
    assert rec.outcome is not None and rec.outcome.state == cc.STATE_FAILED


# ── remote rejection ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("detail", ["HTTP 403", "HTTP 401", "HTTP 500"])
def test_a_non_2xx_answer_is_a_rejection(detail):
    svc = service(FakeTransport(request_result=_fail("http_status", detail)))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.state == cc.STATE_REJECTED
    assert rec.acknowledgement is not None and rec.acknowledgement.accepted is False
    assert rec.outcome is None                          # rejected, never completed


# ── timeout / unreachable ──────────────────────────────────────────────────────

def test_a_timeout_leaves_the_command_pending_and_unacknowledged():
    svc = service(FakeTransport(request_result=_fail("timeout", "timed out", available=False)))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.state == cc.STATE_PENDING                # never fabricated as success
    assert rec.acknowledgement is None and rec.outcome is None
    assert svc.view(rec.envelope.command_id)["transport"]["reason"] == ct.REASON_TIMEOUT


def test_a_connection_error_leaves_the_command_pending_unreachable():
    svc = service(FakeTransport(request_result=_fail("connection_error", "refused", available=False)))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.state == cc.STATE_PENDING
    assert svc.view(rec.envelope.command_id)["transport"]["reason"] == ct.REASON_UNREACHABLE


# ── malformed response variants ────────────────────────────────────────────────

@pytest.mark.parametrize("reason", ["malformed_response", "unexpected_content_type",
                                    "response_too_large", "invalid_operation"])
def test_every_unusable_answer_shape_fails_completion_after_acknowledgement(reason):
    svc = service(FakeTransport(request_result=_fail(reason, "detail")))
    rec = svc.submit(envelope(), now=NOW)
    assert rec.state == cc.STATE_FAILED
    assert rec.acknowledgement.accepted is True


# ── expired before send ────────────────────────────────────────────────────────

def test_a_command_expired_before_send_is_rejected_and_never_transmitted():
    fake = FakeTransport()
    svc = service(fake)
    past_req = _iso(NOW - timedelta(minutes=10))
    past_exp = _iso(NOW - timedelta(minutes=1))
    with pytest.raises(cc.CommandError) as exc:
        svc.submit(envelope(requested_at=past_req, expires_at=past_exp), now=NOW)
    assert exc.value.reason == cc.REASON_ALREADY_EXPIRED
    assert fake.health_calls == 0 and fake.request_calls == 0   # nothing sent


def test_missing_expiry_is_rejected_before_send():
    fake = FakeTransport()
    with pytest.raises(cc.CommandError) as exc:
        service(fake).submit(envelope(expires_at=""), now=NOW)
    assert exc.value.reason == cc.REASON_EXPIRY_REQUIRED
    assert fake.request_calls == 0


# ── duplicate / idempotent submission ─────────────────────────────────────────

def test_idempotent_resubmission_returns_the_same_record_without_resending():
    fake = FakeTransport(request_result=_ok())
    svc = service(fake)
    first = svc.submit(envelope(idempotency_key="dup"), now=NOW)
    second = svc.submit(envelope(idempotency_key="dup"), now=NOW)
    assert first.envelope.command_id == second.envelope.command_id
    assert fake.request_calls == 1                       # sent exactly once (no resend)
    assert len(svc.recent(100)) == 1


def test_a_reused_command_id_with_a_new_key_is_a_deterministic_rejection():
    fake = FakeTransport()
    svc = service(fake)
    svc.submit(envelope(idempotency_key="k1", command_id="cmd_fixed"), now=NOW)
    with pytest.raises(cc.CommandError) as exc:
        svc.submit(envelope(idempotency_key="k2", command_id="cmd_fixed"), now=NOW)
    assert exc.value.reason == cc.REASON_DUPLICATE_COMMAND_ID


# ── unknown / mutating command rejection ──────────────────────────────────────

@pytest.mark.parametrize("bad", ["pause", "resume", "arm", "disarm", "close_position",
                                 "order_send", "cancel", "kill", "flatten", "unknown"])
def test_unknown_or_mutating_command_is_rejected_and_never_transmitted(bad):
    fake = FakeTransport()
    svc = service(fake)
    with pytest.raises(cc.CommandError) as exc:
        svc.submit(envelope(command_type=bad, idempotency_key=bad), now=NOW)
    assert exc.value.reason == cc.REASON_UNKNOWN_TYPE
    assert fake.health_calls == 0 and fake.request_calls == 0


def test_the_transmit_map_has_no_entry_for_any_mutating_verb():
    # There is simply no read endpoint a mutating command could dispatch to.
    mapped = ct._HEALTH_TYPES | ct._TELEMETRY_TYPES
    assert mapped == cc.ALLOWED_TYPES
    for banned in ("pause", "resume", "arm", "order", "cancel", "kill"):
        assert banned not in mapped


# ── no retries ─────────────────────────────────────────────────────────────────

def test_a_failed_send_is_attempted_exactly_once():
    fake = FakeTransport(request_result=_fail("timeout", "timed out", available=False))
    svc = service(fake)
    svc.submit(envelope(), now=NOW)
    assert fake.request_calls == 1                       # one attempt, no retry
    # Re-querying status does not re-dispatch.
    svc.status(svc.recent(1)[0].envelope.command_id)
    assert fake.request_calls == 1


# ── no secret leakage ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("secret_payload", [
    {"api_token": "abc"}, {"password": "hunter2"},
    {"nested": {"authorization": "Bearer x"}},
])
def test_a_secret_bearing_payload_is_rejected_before_send(secret_payload):
    fake = FakeTransport()
    svc = service(fake)
    with pytest.raises(cc.CommandError) as exc:
        svc.submit(envelope(payload=secret_payload), now=NOW)
    assert exc.value.reason == cc.REASON_PAYLOAD_SECRET
    assert fake.request_calls == 0


def test_a_failure_detail_is_redacted_in_the_view():
    leaky = _fail("connection_error", "token=SUPERSECRETVALUE leaked in error",
                  available=False)
    svc = service(FakeTransport(request_result=leaky))
    rec = svc.submit(envelope(), now=NOW)
    view = svc.view(rec.envelope.command_id)
    assert "SUPERSECRETVALUE" not in json.dumps(view)


# ── no execution dependency ────────────────────────────────────────────────────

def test_module_imports_nothing_that_executes_or_mutates():
    source = (BACKEND_DIR / "command_transport.py").read_text()
    for forbidden in ("import execution", "import strategy", "import server",
                      "order_send", "open_position", "close_position", "place_order",
                      "arm(", "disarm", "kill_switch", "POST", "PUT", "DELETE",
                      "requests", "httpx"):
        assert forbidden not in source, f"command_transport references {forbidden}"


def test_exercising_the_service_opens_no_socket(monkeypatch):
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("command transport opened a socket")))
    svc = service(FakeTransport(request_result=_ok()))
    rec = svc.submit(envelope(), now=NOW)
    svc.acknowledge  # attribute access only; the record is already terminal
    svc.status(rec.envelope.command_id)
    svc.recent(10)
    svc.view(rec.envelope.command_id)


# ── recovery after failure ─────────────────────────────────────────────────────

def test_the_service_recovers_after_a_transport_failure():
    # First command times out (left pending); the service is not wedged, and a
    # second, distinct command sent once the transport is healthy completes.
    fake = FakeTransport(request_result=_fail("timeout", "timed out", available=False))
    svc = service(fake)
    first = svc.submit(envelope(idempotency_key="a"), now=NOW)
    assert first.state == cc.STATE_PENDING

    fake._request = _ok()                                # transport recovers
    second = svc.submit(envelope(idempotency_key="b"), now=NOW)
    assert second.state == cc.STATE_COMPLETED
    assert len(svc.recent(100)) == 2


# ── loopback integration: the real UI-13 RestTransport end to end ─────────────

class _FakeNode(BaseHTTPRequestHandler):
    expected_token = "node-token-" + "y" * 32
    telemetry_status = 200
    telemetry_body = b'{"instance_id": "loop-1"}'
    seen_auth: list = []

    def log_message(self, *a):
        return

    def do_GET(self):
        auth = self.headers.get("Authorization")
        type(self).seen_auth.append(auth)
        if auth != f"Bearer {self.expected_token}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "unauthorized"}')
            return
        if self.path == "/telemetry":
            self.send_response(self.telemetry_status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(self.telemetry_body)
            return
        # /health
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"status": "ok"}')


@pytest.fixture()
def loopback_node():
    _FakeNode.seen_auth = []
    _FakeNode.telemetry_status = 200
    server = HTTPServer(("127.0.0.1", 0), _FakeNode)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield f"http://{host}:{port}", _FakeNode.expected_token
    finally:
        server.shutdown()
        server.server_close()


def test_loopback_read_only_command_completes_over_the_real_transport(loopback_node):
    endpoint, token = loopback_node
    transport = rt.RestTransport(endpoint, token)
    svc = ct.CommandTransportService(transport)
    rec = svc.submit(envelope(command_type=cc.CMD_REQUEST_TELEMETRY), now=NOW)
    assert rec.state == cc.STATE_COMPLETED
    # The node WAS authenticated (UI-11/UI-13 bearer), and no token leaks anywhere.
    assert _FakeNode.seen_auth and _FakeNode.seen_auth[0] == f"Bearer {token}"
    assert token not in json.dumps(svc.view(rec.envelope.command_id))


def test_loopback_unauthorized_command_is_rejected(loopback_node):
    endpoint, _token = loopback_node
    transport = rt.RestTransport(endpoint, "wrong-token")
    svc = ct.CommandTransportService(transport)
    rec = svc.submit(envelope(command_type=cc.CMD_REQUEST_TELEMETRY), now=NOW)
    assert rec.state == cc.STATE_REJECTED                # node answered 401 → rejection
    assert rec.acknowledgement.accepted is False
