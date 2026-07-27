"""ARCH-2 — the canonical broker adapter boundary.

Pins: centralized lazy construction, fail-closed unknown kinds, no broker/network
work at import, the mock as the only active adapter, MT5 inert behind the same
interface, capability checks derived from the canonical model, and no direct
gateway calls from the server or orchestrator.
"""

from __future__ import annotations

import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker as broker_layer                                        # noqa: E402
import broker_adapter as ba                                         # noqa: E402
from conftest import code_only                                      # noqa: E402


# ── centralized, lazy, fail-closed construction ───────────────────────────────

def test_only_the_centralized_factory_constructs_adapters():
    """Adapter classes are instantiated in exactly one place: the factory.
    Filesystem walk so untracked modules count too."""
    constructors = sorted(
        p.name for p in BACKEND_DIR.glob("*.py")
        if ("MockBroker()" in code_only(p.name) or "MT5Adapter()" in code_only(p.name))
    )
    assert constructors == ["broker_adapter.py"], f"stray adapter construction: {constructors}"


def test_get_broker_delegates_to_the_factory_and_caches():
    a = broker_layer.get_broker()
    b = broker_layer.get_broker("mock")
    assert a is b                                     # cached, single instance
    assert a is ba.get_adapter("mock")                # same object through both paths


def test_unknown_adapter_kind_fails_closed():
    with pytest.raises(ba.UnknownAdapterError):
        ba.get_adapter("ctrader")
    with pytest.raises(ba.UnknownAdapterError):
        ba.get_adapter("jforex")


def test_adapter_construction_is_lazy(monkeypatch):
    """A fresh factory cache constructs nothing until a kind is requested, and
    requesting one kind must not construct the other."""
    monkeypatch.setattr(ba, "_CACHE", {})
    assert ba._CACHE == {}
    ba.get_adapter("mock")
    assert list(ba._CACHE) == ["mock"]                # MT5 was NOT constructed


def test_importing_the_backend_constructs_no_adapter_and_opens_no_socket():
    script = textwrap.dedent(
        """
        import sys, socket, ssl, asyncio
        def explode(*a, **k):
            raise AssertionError("backend import attempted a connection")
        socket.socket.connect = explode
        socket.socket.connect_ex = explode
        socket.create_connection = explode
        socket.getaddrinfo = explode
        sys.path.insert(0, %r)
        sys.path.insert(0, %r)
        import broker_adapter, broker, server
        assert broker_adapter._CACHE == {}, "an adapter was constructed at import"
        print("OK")
        """
    ) % (str(BACKEND_DIR), str(REPO_ROOT))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=120, cwd=str(BACKEND_DIR))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout


# ── mock is the only active adapter ───────────────────────────────────────────

def test_mock_is_the_only_active_adapter():
    assert ba.ACTIVE_KIND == "mock"
    assert broker_layer.active_kind() == "mock"
    assert broker_layer._ACTIVE == "mock"
    assert broker_layer.get_broker().kind == "mock"


def test_mock_account_identity_is_explicitly_mock_provenance():
    result = broker_layer.get_broker().account_identity()
    assert result.ok and result.data["provenance"] == "mock-fixture"


# ── MT5 stays inert behind the same interface ────────────────────────────────

def test_mt5_construction_performs_no_gateway_work(monkeypatch):
    monkeypatch.setattr(ba, "_CACHE", {})
    calls = []
    monkeypatch.setattr(broker_layer, "_load_live_gateway",
                        lambda: calls.append(1) or None)
    adapter = ba.get_adapter("mt5")
    assert calls == [], "construction loaded the gateway"
    adapter.connection()                              # first USE loads (and gets None)
    assert calls == [1]


def test_mt5_execution_operations_fail_closed_without_a_gateway():
    # LIVE-2/3 (deliberate evolution of the ARCH-2 inertness pin): four ops are
    # now REAL, but with no gateway every one answers a canonical fail-closed
    # envelope and touches nothing.
    mt5 = ba.get_adapter("mt5")
    assert isinstance(mt5, broker_layer.MT5Adapter)
    assert mt5.connection().state == ba.ConnectionState.DISCONNECTED
    assert mt5.submit_order(None, None).code == ba.RESULT_UNAVAILABLE  # still inert
    close = mt5.close_position(
        ba.ClosePositionRequest(intent_id="intent_t", position_ref="1",
                                instrument="EURUSD"), None)
    assert close.ok is False and close.code in (ba.RESULT_UNAVAILABLE,
                                                "connection_denied")
    assert mt5.recent_executions(None).code == ba.RESULT_UNAVAILABLE
    assert mt5.account_snapshot(None).code == ba.RESULT_UNAVAILABLE
    assert mt5.reconcile_snapshot(None).code == ba.RESULT_UNAVAILABLE
    assert mt5.account_identity().code == ba.RESULT_UNAVAILABLE


def test_mt5_operations_open_no_socket(monkeypatch):
    def explode(*a, **k):
        raise AssertionError("MT5 adapter attempted a connection")
    monkeypatch.setattr(socket.socket, "connect", explode)
    monkeypatch.setattr(socket, "create_connection", explode)
    monkeypatch.setattr(socket, "getaddrinfo", explode)
    mt5 = ba.get_adapter("mt5")
    mt5.connection(); mt5.health(); mt5.capabilities()
    mt5.submit_order(None, None)
    mt5.close_position(ba.ClosePositionRequest(intent_id="intent_t",
                                               position_ref="1",
                                               instrument="EURUSD"), None)


# ── canonical models / no duplicate ownership ─────────────────────────────────

def test_the_contract_models_have_one_owner_and_broker_reexports_them():
    assert broker_layer.ConnectionState is ba.ConnectionState
    assert broker_layer.BrokerCapability is ba.BrokerCapability
    assert broker_layer.BrokerError is ba.BrokerError
    assert broker_layer.BrokerContext is ba.BrokerContext
    assert broker_layer.Broker is ba.BrokerAdapter    # the interface alias


def test_no_module_compares_connection_state_with_a_bare_literal():
    # The two former literal sites now import the canonical constants.
    assert 'ConnectionState.CONNECTED' in code_only("execution.py")
    assert '== "Connected"' not in code_only("execution.py")
    sync_code = code_only("broker_sync.py")
    assert "ConnectionState.CONNECTED" in sync_code and "ConnectionState.DEGRADED" in sync_code


def test_capability_checks_derive_from_the_canonical_model():
    caps = broker_layer.capability_dict(broker_layer.get_broker().capabilities())
    assert set(caps) == {f.name for f in
                         __import__("dataclasses").fields(ba.BrokerCapability)}
    import command_registry as reg
    # The registry's capability requirement names a real canonical field.
    for name in reg.broker_dispatched_names():
        required = reg.requires_capability(name)
        assert required is None or required in caps


def test_no_server_or_orchestrator_code_calls_an_mt5_gateway_directly():
    for module in ("server.py", "execution.py"):
        code = code_only(module)
        for forbidden in ("mt5_gateway", "MT5Gateway", "_load_live_gateway",
                          "MetaTrader5", "from live.", "from live import"):
            assert forbidden not in code, f"{module} references {forbidden}"
        # a bare `import live` (the node package) — precise, so `import
        # live_telemetry` (a backend module) does not false-positive
        import re
        assert not re.search(r"^\s*import live(\s|$|\.)", code, re.M), module


def test_inert_result_is_fail_closed_and_unmistakable():
    r = ba.inert_result("anything")
    assert r.ok is False and r.code == ba.RESULT_UNAVAILABLE and "inert" in r.detail
