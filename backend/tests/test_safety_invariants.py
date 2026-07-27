"""AUDIT-FIX-1 — the standing safety invariants, as ALWAYS-RUNNING tests.

These are the properties that currently stand between this Control Tower and a live
broker. Before this file they were guarded either not at all, or by assertions inside
`test_control_tower_api.py` — a module that aborted at import because
`REACT_APP_BACKEND_URL` was unset, so none of them had executed in a long time.

Everything here runs unconditionally against the in-process app. Nothing here needs a
deployed backend, a network, or MetaTrader5.

The invariants:
  1. The active broker is the MockBroker, and `_ACTIVE` is `"mock"`.
  2. Importing the backend does not connect to MT5 (proved in a subprocess with the
     socket layer disabled).
  3. Every MT5 order operation is inert.
  4. `/api/commands/{name}` — the fixture command route — cannot reach a real broker
     and opens no socket.
  5. `execution_safety` (UI-18) remains deliberately unwired, pending its own audited
     remediation slice.
  6. Transport selection stays centralized and default-safe.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker as broker_layer                                        # noqa: E402
import server                                                        # noqa: E402
import transport as tmod                                             # noqa: E402
from conftest import code_only                                       # noqa: E402

client = TestClient(server.app)

#: A deployment that exists in the frozen fixture, so the command route reaches its
#: effect stage rather than short-circuiting on a validation failure.
FIXTURE_DEPLOYMENT = "dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ"


@pytest.fixture(autouse=True)
def isolated_event_store(tmp_path, monkeypatch):
    """Redirect the append-only event store into a temp file for EVERY test here.

    Two tests below drive `POST /api/commands/{name}`, which appends a BotEvent. The
    suite runs `-n 2 --dist loadscope`, so modules share a worker process and the
    repo-level `backend/events.db` — writing to it both creates a stray database
    (which `test_no_stray_events_db_in_repo` correctly forbids) and perturbs the
    event-store retention/sequence suites. Isolation keeps these invariants
    order-independent.
    """
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())


def _block_network(monkeypatch):
    """Make any outbound CONNECTION attempt raise.

    Deliberately patches `socket.socket.connect` / `create_connection` /
    `getaddrinfo` rather than the `socket.socket` class itself: replacing the class
    breaks stdlib internals (ssl, asyncio) and the in-process ASGI test client, which
    would make this a test of the harness rather than of the code under test. These
    three are the actual doors to the network.
    """
    def explode(*a, **k):
        raise AssertionError("an outbound connection was attempted — this path must "
                             "never touch the network")

    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket.socket, "connect_ex", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)


# ── 1. the active broker is the mock ──────────────────────────────────────────

def test_the_active_broker_is_mock():
    """`_ACTIVE` is the single constant standing between the command route and a real
    adapter. Pin it here as a first-class safety invariant."""
    assert broker_layer._ACTIVE == "mock"
    assert broker_layer.active_kind() == "mock"
    assert broker_layer.get_broker().kind == "mock"
    assert type(broker_layer.get_broker()).__name__ == "MockBroker"


def test_the_default_broker_is_never_the_mt5_adapter():
    assert not isinstance(broker_layer.get_broker(), broker_layer.MT5Adapter)


# ── 2. importing the backend does not connect to MT5 ──────────────────────────

def test_importing_the_backend_opens_no_socket():
    """`broker._REGISTRY` instantiates `MT5Adapter()` at import, whose `__init__`
    calls `_load_live_gateway()`. Prove that import path cannot reach the network:
    run it in a fresh interpreter with the socket layer disabled."""
    script = textwrap.dedent(
        """
        import sys, socket, ssl, asyncio          # stdlib first, before blocking
        def explode(*a, **k):
            raise AssertionError("the backend import graph attempted a connection")
        socket.socket.connect = explode
        socket.socket.connect_ex = explode
        socket.create_connection = explode
        socket.getaddrinfo = explode
        sys.path.insert(0, %r)
        sys.path.insert(0, %r)
        import broker, server                     # the full backend import graph
        assert broker.active_kind() == "mock"
        assert broker.get_broker("mt5").connection().state == "Disconnected"
        print("OK")
        """
    ) % (str(BACKEND_DIR), str(REPO_ROOT))

    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=120, cwd=str(BACKEND_DIR))
    assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr[-3000:]}"
    assert "OK" in proc.stdout


def test_the_mt5_adapter_reports_disconnected_and_holds_no_gateway():
    """On any host without MetaTrader5 the guarded import yields no gateway, and the
    adapter reports Disconnected rather than inventing a connection."""
    mt5 = broker_layer.get_broker("mt5")
    assert isinstance(mt5, broker_layer.MT5Adapter)
    assert mt5.connection().state == "Disconnected"
    assert mt5.health().connection == "Disconnected"


def test_the_live_gateway_loader_is_exception_guarded():
    """`_load_live_gateway` must never raise into the import graph — any failure
    (missing package, bad config, unreachable terminal) degrades to None."""
    source = code_only("broker.py")
    assert "def _load_live_gateway" in source
    assert "except Exception" in source
    # On a host without MetaTrader5 the guarded import yields None rather than raising.
    assert broker_layer._load_live_gateway() is None


# ── 3. MT5 order operations remain inert ──────────────────────────────────────

def test_every_mt5_order_operation_is_inert():
    """The adapter advertises the order surface but implements none of it. If any of
    these ever returns something real, this test fails and forces a review."""
    mt5 = broker_layer.get_broker("mt5")
    ctx = server._broker_context({}, "2026-07-27T00:00:00Z")

    assert mt5.submit_command("CloseTrade", ctx) == (None, None)
    assert mt5.cancel_order("ord_1", ctx) == (None, None)
    assert mt5.modify_order("ord_1", {"sl": 1.0}, ctx) == (None, None)
    assert mt5.flatten("dpl_1", ctx) == []


def test_mt5_order_operations_open_no_socket(monkeypatch):
    _block_network(monkeypatch)
    mt5 = broker_layer.get_broker("mt5")
    ctx = server._broker_context({}, "2026-07-27T00:00:00Z")
    mt5.submit_command("CloseTrade", ctx)
    mt5.cancel_order("ord_1", ctx)
    mt5.modify_order("ord_1", {}, ctx)
    mt5.flatten("dpl_1", ctx)
    mt5.connection()


def test_the_mt5_market_data_provider_is_registered_but_never_available():
    """Relocated from the external smoke suite: MT5 appears in the provider registry
    as a placeholder and must never report itself as available/connected."""
    body = client.get("/api/market-data/providers").json()
    by_id = {p["providerId"]: p for p in body["providers"]}
    assert "mt5" in by_id, "the mt5 provider should stay registered as a placeholder"
    assert by_id["mt5"]["available"] is False
    assert by_id["mt5"]["connection"] == "Disconnected"
    # And it is never the ACTIVE provider.
    assert body["active"] != "mt5"
    assert body["active"] == "fixture"


# ── 4. the fixture command route cannot reach a real broker ───────────────────

def test_the_fixture_command_route_stays_on_the_mock_and_opens_no_socket(monkeypatch):
    """Relocated + strengthened from the external smoke suite.

    `POST /api/commands/{name}` runs validate → policy → dispatch → broker. Prove the
    whole path is mock-bound and touches no network."""
    _block_network(monkeypatch)

    response = client.post(f"/api/commands/PauseDeployment",
                           json={"deploymentId": FIXTURE_DEPLOYMENT})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["mode"] == "mock"
    assert broker_layer.get_broker().kind == "mock"


def test_a_trade_command_is_rejected_at_validation_without_touching_a_broker(monkeypatch):
    """A trade command for a non-existent trade is refused at the VALIDATION stage —
    it never reaches dispatch, and no connection is attempted."""
    _block_network(monkeypatch)
    response = client.post("/api/commands/CloseTrade",
                           json={"tradeId": "tr_nope", "dryRun": True})
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert detail["status"] == "rejected"
    assert detail["stage"] == "validated"          # refused before broker dispatch
    assert detail["code"] == "trade_not_found"
    assert broker_layer.get_broker().kind == "mock"


def test_an_unknown_command_is_rejected_by_the_fixture_route():
    response = client.post("/api/commands/NotARealCommand", json={})
    assert response.status_code == 400
    assert "Unknown command" in response.json()["detail"]


# ── 5. execution_safety remains deliberately unwired ──────────────────────────

def test_execution_safety_is_deliberately_not_wired_into_any_production_module():
    """UI-18 is a CONTRACT-ONLY slice: `evaluate()` is defined, exhaustively tested and
    intentionally called by nothing.

    This guard exists so that wiring it in becomes a LOUD, deliberate act. When the
    dedicated remediation slice lands, this test is replaced by its positive
    counterpart (every non-read-only command path must consult `evaluate()`), not
    silently deleted.
    """
    importers = sorted(
        p.name for p in BACKEND_DIR.glob("*.py")
        if p.name != "execution_safety.py" and "execution_safety" in p.read_text()
    )
    assert importers == [], (
        "execution_safety is contract-only (UI-18). Wiring it into a command path "
        f"requires its own audited slice — found importers: {importers}"
    )


def test_execution_safety_still_denies_by_default():
    """The contract itself must keep failing closed while it waits to be wired."""
    import execution_safety as es

    decision = es.evaluate(es.CommandRequest(command_type="close_position"))
    assert decision.allowed is False
    decision = es.evaluate(es.CommandRequest(command_type="totally_unknown"))
    assert decision.allowed is False and decision.reason == es.DENY_UNKNOWN_COMMAND


# ── 6. transport selection stays centralized and default-safe ─────────────────

def test_the_default_transport_is_null_with_no_configuration():
    assert isinstance(tmod.default_transport(env={}), tmod.NullTransport)
    assert isinstance(tmod.default_transport(env={tmod.VAR_TRANSPORT_ENABLED: "1"}),
                      tmod.NullTransport)          # enabled but unconfigured


def test_a_real_transport_is_constructed_in_exactly_one_module():
    """Filesystem walk, not `git grep`: an untracked new module must count too."""
    constructors = sorted(
        p.name for p in BACKEND_DIR.glob("*.py") if "RestTransport(" in code_only(p.name)
    )
    assert constructors == ["rest_transport.py"], f"stray constructor: {constructors}"

    selectors = sorted(
        p.name for p in BACKEND_DIR.glob("*.py")
        if "select_rest_transport" in code_only(p.name)
    )
    assert selectors == ["rest_transport.py", "transport.py"], f"stray selector: {selectors}"


def test_the_deployed_command_transport_singleton_is_disabled():
    """The singleton the running app actually holds (built from `os.environ`), not a
    freshly constructed test double."""
    assert server._COMMAND_TRANSPORT.enabled is False
    assert isinstance(server._COMMAND_TRANSPORT._transport, tmod.NullTransport)


def test_the_server_never_builds_a_transport_directly():
    server_src = code_only("server.py")
    assert "RestTransport(" not in server_src
    assert "select_rest_transport" not in server_src
