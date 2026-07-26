"""UI-13 — the disabled authenticated REST transport.

Every network test runs against an in-process fake HTTP server bound to
127.0.0.1 — never the VPS, the internet, or any external service. The properties
that matter most are negative: the default stays NullTransport, the bearer token
never escapes, there are no retries, and failures are returned, never raised.
"""

from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import rest_transport as rt                                          # noqa: E402
import transport as t                                               # noqa: E402

TOKEN = "node-token-" + "x" * 32


# ── a configurable loopback fake node ─────────────────────────────────────────

class _FakeNodeHandler(BaseHTTPRequestHandler):
    # class-level knobs, set per test
    expected_token = TOKEN
    status = 200
    body = b'{"ok": true}'
    content_type = "application/json"
    delay_s = 0.0
    seen_auth: list = []
    seen_cookies: list = []
    request_count = 0

    def log_message(self, *a):            # silence the server
        return

    def do_GET(self):
        import time
        type(self).request_count += 1
        auth = self.headers.get("Authorization")
        type(self).seen_auth.append(auth)
        type(self).seen_cookies.append(self.headers.get("Cookie"))
        if self.delay_s:
            time.sleep(self.delay_s)
        # Enforce the bearer token like a real node would.
        if auth != f"Bearer {self.expected_token}":
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"error": "unauthorized"}')
            return
        self.send_response(self.status)
        if self.content_type:
            self.send_header("Content-Type", self.content_type)
        self.end_headers()
        self.wfile.write(self.body)


@pytest.fixture()
def fake_node():
    """Start a fresh loopback server per test; reset the handler knobs."""
    _FakeNodeHandler.expected_token = TOKEN
    _FakeNodeHandler.status = 200
    _FakeNodeHandler.body = b'{"ok": true, "value": 1}'
    _FakeNodeHandler.content_type = "application/json"
    _FakeNodeHandler.delay_s = 0.0
    _FakeNodeHandler.seen_auth = []
    _FakeNodeHandler.seen_cookies = []
    _FakeNodeHandler.request_count = 0
    server = HTTPServer(("127.0.0.1", 0), _FakeNodeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    try:
        yield {"handler": _FakeNodeHandler, "endpoint": f"http://127.0.0.1:{port}"}
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def transport_for(endpoint, token=TOKEN, **kw):
    return rt.RestTransport(endpoint, token, **kw)


# ── valid authenticated request ───────────────────────────────────────────────

def test_valid_authenticated_request_succeeds(fake_node):
    tr = transport_for(fake_node["endpoint"])
    result = tr.request("/status")
    assert result.ok is True and result.available is True
    assert result.reason == rt.REASON_OK
    assert result.payload == {"ok": True, "value": 1}


def test_health_and_connect_probe_the_endpoint(fake_node):
    tr = transport_for(fake_node["endpoint"])
    assert tr.health().ok is True
    conn = tr.connect()
    assert conn.connected is True and conn.reason == rt.REASON_OK


def test_authorization_header_is_sent_as_bearer(fake_node):
    transport_for(fake_node["endpoint"]).request("/status")
    auth_headers = fake_node["handler"].seen_auth
    assert auth_headers and auth_headers[-1] == f"Bearer {TOKEN}"


def test_no_cookie_header_is_ever_sent(fake_node):
    transport_for(fake_node["endpoint"]).request("/status")
    assert all(c is None for c in fake_node["handler"].seen_cookies)


# ── auth rejection ────────────────────────────────────────────────────────────

def test_wrong_token_is_a_401_without_leaking_the_token(fake_node):
    tr = transport_for(fake_node["endpoint"], token="the-wrong-token-value-000000")
    result = tr.request("/status")
    assert result.ok is False and result.available is True
    assert result.reason == rt.REASON_HTTP_STATUS
    assert "the-wrong-token-value" not in str(result)


def test_missing_token_server_side_is_a_401(fake_node):
    fake_node["handler"].expected_token = "a-different-token-entirely-00000000"
    result = transport_for(fake_node["endpoint"]).request("/status")
    assert result.reason == rt.REASON_HTTP_STATUS


# ── token never leaks ─────────────────────────────────────────────────────────

def test_token_is_masked_in_repr_and_str():
    tr = transport_for("http://127.0.0.1:1", token=TOKEN)
    assert TOKEN not in repr(tr)
    assert TOKEN not in str(tr)
    assert TOKEN not in repr(tr._token) and TOKEN not in str(tr._token)


def test_token_absent_from_describe_and_status():
    tr = transport_for("http://127.0.0.1:1", token=TOKEN)
    assert TOKEN not in tr.describe()
    status = tr.status()
    assert TOKEN not in str(status)
    # status is value-free: no endpoint, no probe, no token.
    assert status == {"kind": "https", "available": None, "reason": "not_probed"}


def test_token_absent_from_every_result_across_outcomes(fake_node):
    tr = transport_for(fake_node["endpoint"])
    for path in ("/status", "/missing", "not-a-path"):
        assert TOKEN not in str(tr.request(path))
    fake_node["handler"].status = 500
    assert TOKEN not in str(tr.request("/status"))


# ── HTTP safety: timeout, refusal, malformed, content-type, size, non-2xx ─────

def test_timeout_is_reported_not_raised(fake_node):
    fake_node["handler"].delay_s = 0.5
    tr = transport_for(fake_node["endpoint"], read_timeout=0.1)
    result = tr.request("/slow")
    assert result.ok is False and result.available is False
    assert result.reason == rt.REASON_TIMEOUT


def test_connection_refused_is_reported_not_raised():
    # A port nothing listens on. Bind+close to get a definitely-free port.
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    tr = transport_for(f"http://127.0.0.1:{port}", connect_timeout=0.5, read_timeout=0.5)
    result = tr.request("/status")
    assert result.ok is False and result.available is False
    assert result.reason == rt.REASON_CONNECTION_ERROR


def test_malformed_json_is_reported(fake_node):
    fake_node["handler"].body = b"{not valid json"
    result = transport_for(fake_node["endpoint"]).request("/status")
    assert result.ok is False and result.available is True
    assert result.reason == rt.REASON_MALFORMED_RESPONSE


def test_wrong_content_type_is_rejected(fake_node):
    fake_node["handler"].content_type = "text/html"
    fake_node["handler"].body = b"<html>not json</html>"
    result = transport_for(fake_node["endpoint"]).request("/status")
    assert result.reason == rt.REASON_UNEXPECTED_CONTENT_TYPE


def test_oversized_response_is_rejected(fake_node):
    fake_node["handler"].body = b'{"x": "' + b"A" * 4096 + b'"}'
    tr = transport_for(fake_node["endpoint"], max_response_bytes=1024)
    result = tr.request("/status")
    assert result.ok is False and result.available is True
    assert result.reason == rt.REASON_RESPONSE_TOO_LARGE


@pytest.mark.parametrize("status", [400, 403, 404, 500, 503])
def test_non_2xx_responses_map_to_http_status(fake_node, status):
    fake_node["handler"].status = status
    result = transport_for(fake_node["endpoint"]).request("/status")
    assert result.ok is False and result.available is True
    assert result.reason == rt.REASON_HTTP_STATUS
    assert str(status) in result.detail


def test_invalid_operation_is_rejected_without_a_request(fake_node):
    tr = transport_for(fake_node["endpoint"])
    before = fake_node["handler"].request_count
    for bad in ("no-slash", "http://evil/x", "/a/../b", "/has space", 42, None):
        result = tr.request(bad)
        assert result.reason == rt.REASON_INVALID_OPERATION
    assert fake_node["handler"].request_count == before   # never hit the network


# ── no retries ────────────────────────────────────────────────────────────────

def test_a_failed_request_is_attempted_exactly_once(fake_node):
    fake_node["handler"].status = 500
    transport_for(fake_node["endpoint"]).request("/status")
    assert fake_node["handler"].request_count == 1        # no retry loop


# ── idempotency ───────────────────────────────────────────────────────────────

def test_close_and_disconnect_are_idempotent(fake_node):
    tr = transport_for(fake_node["endpoint"])
    assert tr.disconnect() is None
    assert tr.disconnect() is None
    assert tr.close() is None
    assert tr.close() is None
    # still usable after teardown calls (stateless HTTP)
    assert tr.request("/status").ok is True


# ── selection / activation rules ──────────────────────────────────────────────

def base_env(endpoint):
    return {
        t.VAR_TRANSPORT_ENABLED: "1",
        "NODE_TRANSPORT": "https",
        "NODE_ENDPOINT": endpoint,
        "NODE_API_TOKEN": TOKEN,
        "ALLOW_INSECURE_LOCALHOST": "1",
    }


def test_default_transport_is_null_without_the_enable_flag(fake_node):
    env = base_env(fake_node["endpoint"])
    del env[t.VAR_TRANSPORT_ENABLED]
    assert isinstance(t.default_transport(env=env), t.NullTransport)


@pytest.mark.parametrize("flag", ["0", "false", "no", "", "maybe", "ture"])
def test_non_truthy_enable_flag_stays_null(fake_node, flag):
    env = base_env(fake_node["endpoint"]); env[t.VAR_TRANSPORT_ENABLED] = flag
    assert isinstance(t.default_transport(env=env), t.NullTransport)


def test_enabled_with_valid_https_config_selects_rest_transport(fake_node):
    tr = t.default_transport(env=base_env(fake_node["endpoint"]))
    assert isinstance(tr, rt.RestTransport)
    assert tr.request("/status").ok is True     # and it actually works, on loopback


@pytest.mark.parametrize("mutate", [
    lambda e: e.pop("NODE_ENDPOINT"),
    lambda e: e.pop("NODE_API_TOKEN"),
    lambda e: e.update(NODE_TRANSPORT="mtls"),
    lambda e: e.update(NODE_TRANSPORT="none"),
    lambda e: e.update(NODE_ENDPOINT="http://evil.example"),      # remote http = error
    lambda e: e.update(NODE_ENDPOINT="not-a-url"),
    lambda e: e.update(NODE_CONNECT_TIMEOUT="banana"),            # invalid config
])
def test_missing_or_invalid_config_falls_back_to_null(fake_node, mutate):
    env = base_env(fake_node["endpoint"]); mutate(env)
    assert isinstance(t.default_transport(env=env), t.NullTransport)


def test_selection_never_raises_on_hostile_env():
    for env in ({}, {t.VAR_TRANSPORT_ENABLED: "1"},
                {t.VAR_TRANSPORT_ENABLED: "1", "NODE_ENDPOINT": "http://[::1"},
                {t.VAR_TRANSPORT_ENABLED: "1", "NODE_TRANSPORT": "https",
                 "NODE_ENDPOINT": "http://127.0.0.1:1", "NODE_API_TOKEN": "\x00"}):
        t.default_transport(env=env)          # must not raise


# ── structural: read-only, no mutation surface, redaction wired ───────────────

def _code_only(rel: str) -> str:
    """Source with the module docstring and comment lines removed, so guards match
    real CODE and not prohibition prose (the module documents what it forbids)."""
    import ast
    text = (BACKEND_DIR / rel).read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


def test_transport_is_get_only_no_mutation_methods():
    code = _code_only("rest_transport.py")
    assert 'method="GET"' in code
    for forbidden in ('method="POST"', 'method="PUT"', 'method="DELETE"',
                      'method="PATCH"', ".add_data(", "data=json", "urlencode"):
        assert forbidden not in code, forbidden


def test_no_retry_or_streaming_or_cookie_machinery():
    code = _code_only("rest_transport.py").lower()
    for forbidden in ("for attempt", "while true", "retry", "cookiejar",
                      "httpcookieprocessor", ".stream", "iter_content", "websocket"):
        assert forbidden not in code, forbidden


def test_redaction_is_applied_to_error_detail():
    assert "security_config.redact_text" in (BACKEND_DIR / "rest_transport.py").read_text()


def test_no_vps_address_or_hardcoded_remote_host():
    code = _code_only("rest_transport.py").lower()
    for token in ("10.", "192.168.", "vps", ".internal", "amazonaws"):
        assert token not in code, token
    # No hard-coded host/scheme literals in CODE: endpoints arrive via config only.
    for token in ("http://", "https://", "wss://", "ws://"):
        assert token not in code, token
