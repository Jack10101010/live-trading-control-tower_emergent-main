"""UI-12 — the remote-transport contract (architecture only).

The property that matters most is a NEGATIVE one: nothing here can open a
connection. NullTransport is the default everywhere, every operation is total and
non-raising, and exercising the whole surface touches no socket. These tests pin
that, and pin the interface shape a future real transport must honour.
"""

from __future__ import annotations

import socket
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import security_config as sc                                          # noqa: E402
import transport as t                                                # noqa: E402


# ── interface contract ────────────────────────────────────────────────────────

def test_transport_base_is_abstract_and_cannot_be_instantiated():
    with pytest.raises(TypeError):
        t.Transport()                     # abstract: has unimplemented methods


def test_transport_declares_the_full_operation_set():
    for op in ("connect", "disconnect", "health", "request", "close"):
        method = getattr(t.Transport, op, None)
        assert callable(method), op
        assert getattr(method, "__isabstractmethod__", False), f"{op} must be abstract"


def test_stream_is_deliberately_absent():
    """A streaming op would be speculative surface today (the node publishes
    periodic snapshots, not a stream). Its absence is intentional and documented."""
    assert not hasattr(t.Transport, "stream")
    assert "stream()" in (BACKEND_DIR / "transport.py").read_text()  # the rationale


def test_a_concrete_transport_must_implement_every_operation():
    class Partial(t.Transport):
        def connect(self): ...
        # deliberately missing the rest
    with pytest.raises(TypeError):
        Partial()


def test_result_types_are_frozen_value_objects():
    cr = t.ConnectionResult(connected=False, reason="x")
    tr = t.TransportResult(ok=False, available=False, reason="x")
    with pytest.raises(Exception):
        cr.connected = True               # frozen
    with pytest.raises(Exception):
        tr.ok = True


# ── NullTransport behaviour ────────────────────────────────────────────────────

@pytest.fixture()
def null():
    return t.NullTransport()


def test_null_transport_is_a_transport(null):
    assert isinstance(null, t.Transport)
    assert null.kind == t.KIND_NULL


def test_null_connect_is_never_connected(null):
    result = null.connect()
    assert isinstance(result, t.ConnectionResult)
    assert result.connected is False
    assert result.reason == t.REASON_UNAVAILABLE
    assert result.detail                  # a clear, non-empty explanation


def test_null_health_reports_unavailable(null):
    result = null.health()
    assert isinstance(result, t.TransportResult)
    assert result.available is False and result.ok is False
    assert result.reason == t.REASON_UNAVAILABLE


def test_null_request_reports_unavailable_and_echoes_the_operation(null):
    result = null.request("fetch_node_status", payload={"any": "thing"})
    assert result.available is False and result.ok is False
    assert result.reason == t.REASON_UNAVAILABLE
    assert "fetch_node_status" in result.detail
    assert result.payload is None         # nothing fabricated


def test_null_request_never_inspects_or_returns_the_payload(null):
    secret = {"token": "SHOULD-NOT-SURFACE"}
    result = null.request("op", payload=secret)
    assert "SHOULD-NOT-SURFACE" not in str(result)


def test_null_disconnect_and_close_are_idempotent_noops(null):
    assert null.disconnect() is None
    assert null.disconnect() is None      # again
    assert null.close() is None
    assert null.close() is None           # again, and after disconnect


def test_null_operations_never_raise(null):
    # Every method, including with hostile input, returns rather than raises.
    null.connect(); null.disconnect(); null.health(); null.close()
    for payload in (None, {}, [], "x", 123, object(), {"a": {"b": {"c": 1}}}):
        assert null.request("op", payload).available is False
    for op in ("", "  ", "x" * 10_000, "unicode:☃"):
        assert null.request(op).available is False


def test_null_describe_and_status_are_value_free(null):
    assert "NullTransport" in null.describe()
    status = null.status()
    assert status == {"kind": "null", "available": False, "reason": t.REASON_UNAVAILABLE}
    # No endpoint, credential or payload leaks through the diagnostic surface.
    assert "http" not in str(status).lower()


# ── default dependency wiring ──────────────────────────────────────────────────

def test_default_transport_is_null_transport():
    assert isinstance(t.default_transport(), t.NullTransport)


def test_default_transport_ignores_config_and_stays_null():
    """A future slice selects a real transport here; UI-12 always returns Null,
    regardless of what config is passed."""
    for cfg in (None, {}, {"NODE_ENDPOINT": "https://node.example"}, object()):
        assert isinstance(t.default_transport(cfg), t.NullTransport)


def test_shared_null_singleton_is_a_transport():
    assert isinstance(t.NULL_TRANSPORT, t.NullTransport)


def test_null_transport_satisfies_the_ui9_transport_adapter_protocol():
    """UI-9 declared `TransportAdapter` as a forward placeholder; UI-12 is its
    canonical expansion, so existing references keep working."""
    assert isinstance(t.NullTransport(), sc.TransportAdapter)
    assert isinstance(t.NullTransport().describe(), str)


# ── no networking, proven two ways ─────────────────────────────────────────────

def test_module_imports_no_network_library():
    # Only NON-COMMENT code lines are checked: the module names the future kinds
    # ("https", "websocket") as vocabulary strings and prohibits networking in
    # prose, neither of which is an implementation.
    code = "\n".join(
        line for line in (BACKEND_DIR / "transport.py").read_text().splitlines()
        if not line.lstrip().startswith("#")
    )
    for forbidden in ("import socket", "import ssl", "import urllib", "urllib.request",
                      "import requests", "import httpx", "import websocket",
                      "import websockets", "import asyncio", "urlopen",
                      "create_connection", "SSLContext", ".connect(("):
        assert forbidden not in code, f"transport.py references {forbidden}"
    import transport as module
    for attr in ("socket", "ssl", "requests", "httpx", "urllib", "websocket"):
        assert not hasattr(module, attr), attr


def test_no_operation_opens_a_socket(monkeypatch):
    """Runtime proof: make any real socket use blow up, then exercise the entire
    NullTransport surface. If it never touches the network, nothing raises."""
    def explode(*a, **k):
        raise AssertionError("NullTransport attempted a network operation")

    monkeypatch.setattr(socket, "socket", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    if hasattr(socket, "getaddrinfo"):
        monkeypatch.setattr(socket, "getaddrinfo", explode)

    n = t.default_transport()
    n.connect(); n.health(); n.request("op", {"p": 1}); n.disconnect(); n.close()
    n.describe(); n.status()              # none of these may touch the network


def test_no_vps_or_endpoint_string_is_embedded():
    source = (BACKEND_DIR / "transport.py").read_text().lower()
    # No concrete host, port or scheme baked in — a real endpoint arrives via
    # config in a future slice, never hard-coded here.
    for token in ("http://", "https://", "wss://", "ws://", ".vps", "10.", "192.168.",
                  ":8000", ":8443"):
        assert token not in source, token


# ── no behaviour regression: transport is imported by no existing caller ──────

def test_transport_selection_stays_centralized_and_default_safe():
    """The invariant UI-12 established and UI-14 must preserve.

    UI-12's original guard asserted that NO production module imported `transport`
    yet. UI-14 legitimately supersedes that premise: `node_client.py` is the
    approved read-only integration seam and imports the abstract `Transport`
    contract. What must still hold is the underlying SAFETY property, enforced here:

      1. the default/disabled path resolves to NullTransport (no networking);
      2. an invalid enabled configuration also resolves to NullTransport;
      3. transport SELECTION stays centralized — no production module constructs a
         concrete transport directly; a real transport is built ONLY inside
         `rest_transport.select_rest_transport`, reached ONLY via
         `default_transport()`;
      4. the approved seam (`node_client`) selects through `default_transport()`
         and never imports the concrete adapter or builds one itself.
    """
    # 1 + 2: default and invalid-enabled both yield NullTransport.
    assert isinstance(t.default_transport(env={}), t.NullTransport)
    assert isinstance(t.default_transport(env={t.VAR_TRANSPORT_ENABLED: "1"}),
                      t.NullTransport)                      # enabled but no config

    # 3: a concrete transport is constructed in exactly one place.
    import subprocess
    ctor_hits = subprocess.run(
        ["git", "grep", "-l", "RestTransport(", "--", "backend/"],
        cwd=REPO_ROOT, capture_output=True, text=True).stdout.split()
    non_test = [h for h in ctor_hits if "test" not in h]
    assert non_test == ["backend/rest_transport.py"], f"stray construction: {non_test}"

    # 4: node_client is the approved seam — it goes through default_transport() and
    #    neither imports the concrete adapter nor constructs a transport itself.
    node_client_src = (BACKEND_DIR / "node_client.py").read_text()
    assert "default_transport(" in node_client_src
    assert "import rest_transport" not in node_client_src
    assert "RestTransport(" not in node_client_src

    # And server.py wires no transport directly — it only reaches it via node_client.
    server_src = (BACKEND_DIR / "server.py").read_text()
    assert "RestTransport(" not in server_src
    assert "select_rest_transport" not in server_src
