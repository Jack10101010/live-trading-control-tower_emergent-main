"""LIVE-1 — the read-only MT5 adapter vertical slice.

Proves: adapter selection (mock default, mt5 explicit, unknown denies, lazy, no
import side effects), ConnectionPolicy gating (no MT5 call before approval),
canonical read mapping (MT5 types never escape; deterministic serialization),
every write inert, capability write-free, live telemetry with provenance, live
reconciliation feed, and graceful failure handling — under BOTH an unavailable
terminal and a connected (fake-SDK) terminal.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _fake_mt5                                                    # noqa: E402
import broker as broker_layer                                       # noqa: E402
import broker_adapter as ba                                         # noqa: E402
import connection_policy as cpol                                    # noqa: E402
import server                                                      # noqa: E402,F401 — store isolation fixture
from conftest import code_only                                      # noqa: E402
from live.mt5_gateway import MT5Gateway                             # noqa: E402


class _Cfg:
    mt5_login = None
    mt5_password = None
    mt5_server = None
    broker_symbol = "EURUSD.r"


def _ctx():
    return SimpleNamespace(now="2026-07-27T00:00:00Z")


def _connected_mt5(monkeypatch, *, with_account=True, with_history=False,
                   positions=None):
    """An MT5Adapter wired to a connected FAKE SDK gateway (no real terminal)."""
    fake = _fake_mt5.FakeMT5()
    if with_account:
        fake._account = _fake_mt5.make_account()
    if positions is not None:
        fake._positions = positions
    if with_history:
        fake.history_deals_get = lambda a, b: [SimpleNamespace(
            ticket=5001, order=4001, symbol="EURUSD.r", type=0, volume=0.1,
            price=1.1, profit=2.5, time=1_769_500_000)]
    gw = MT5Gateway(_Cfg(), sdk=fake)
    ok, _ = gw.connect()
    assert ok
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = gw
    adapter._gateway_loaded = True
    adapter._policy_denied_reason = None
    return adapter, fake


# ── adapter selection ─────────────────────────────────────────────────────────

def test_mock_is_the_default_adapter(monkeypatch):
    monkeypatch.delenv(ba.VAR_ADAPTER, raising=False)
    assert ba.active_kind() == "mock"
    assert broker_layer.get_broker().kind == "mock"


def test_mt5_is_selectable_only_by_the_explicit_variable(monkeypatch):
    monkeypatch.setattr(ba, "_CACHE", {})
    monkeypatch.setenv(ba.VAR_ADAPTER, "mt5")
    assert ba.active_kind() == "mt5"
    assert broker_layer.get_broker().kind == "mt5"


def test_unknown_adapter_value_denies(monkeypatch):
    monkeypatch.setattr(ba, "_CACHE", {})
    monkeypatch.setenv(ba.VAR_ADAPTER, "ctrader")
    assert ba.active_kind() == "ctrader"          # returned verbatim...
    with pytest.raises(ba.UnknownAdapterError):   # ...so construction fails closed
        broker_layer.get_broker()


def test_adapter_is_lazily_constructed_and_cached(monkeypatch):
    monkeypatch.setattr(ba, "_CACHE", {})
    assert ba._CACHE == {}
    a = ba.get_adapter("mt5")
    assert list(ba._CACHE) == ["mt5"]             # mock NOT constructed
    assert ba.get_adapter("mt5") is a             # cached


def test_importing_the_backend_performs_no_mt5_work():
    script = textwrap.dedent(
        """
        import sys, socket, ssl
        def explode(*a, **k):
            raise AssertionError("backend import attempted a connection")
        socket.socket.connect = explode
        socket.create_connection = explode
        socket.getaddrinfo = explode
        sys.path.insert(0, %r); sys.path.insert(0, %r)
        import broker_adapter, broker, server
        assert broker_adapter._CACHE == {}, "an adapter was constructed at import"
        print("OK")
        """
    ) % (str(BACKEND_DIR), str(REPO_ROOT))
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True,
                          text=True, timeout=120, cwd=str(BACKEND_DIR))
    assert proc.returncode == 0, proc.stderr[-2000:]
    assert "OK" in proc.stdout


# ── ConnectionPolicy gates all MT5 access ─────────────────────────────────────

def test_denied_policy_constructs_nothing_and_calls_no_mt5(monkeypatch):
    monkeypatch.setattr(ba, "_CACHE", {})
    monkeypatch.setattr(cpol, "evaluate_local_broker",
                        lambda kind, env=None: cpol.PolicyDecision(
                            False, "profile_not_approved", "remote_live"))
    with pytest.raises(ba.AdapterDeniedError) as exc:
        ba.get_adapter("mt5")
    assert exc.value.reason == "profile_not_approved"
    assert "mt5" not in ba._CACHE                 # nothing cached


def test_gateway_property_is_policy_gated_and_makes_no_calls_when_denied(monkeypatch):
    calls = []
    monkeypatch.setattr(broker_layer, "_load_live_gateway",
                        lambda: calls.append(1) or None)
    monkeypatch.setattr(cpol, "evaluate_local_broker",
                        lambda kind, env=None: cpol.PolicyDecision(
                            False, "profile_not_approved", "remote_live"))
    adapter = broker_layer.MT5Adapter()
    result = adapter.account_snapshot(_ctx())
    assert calls == [], "the gateway was loaded despite a policy deny"
    assert result.ok is False and result.code == "connection_denied"
    assert result.detail == "profile_not_approved"


def test_local_loopback_is_the_only_approved_profile_for_the_broker(monkeypatch):
    assert cpol.evaluate_local_broker("mt5", env={}).reason == cpol.ALLOW_LOCAL_BROKER
    for profile in (cpol.PROFILE_REMOTE_PRE_LIVE, cpol.PROFILE_REMOTE_LIVE):
        d = cpol.evaluate_local_broker("mt5", env={cpol.VAR_PROFILE: profile})
        assert not d.allowed and d.reason == cpol.DENY_PROFILE_NOT_APPROVED
    unknown = cpol.evaluate_local_broker("jforex", env={})
    assert unknown.reason == cpol.DENY_ADAPTER_UNKNOWN


# ── canonical read mapping (MT5 types never escape) ───────────────────────────

def test_account_snapshot_maps_to_canonical_and_masks_login(monkeypatch):
    adapter, _ = _connected_mt5(monkeypatch)
    r = adapter.account_snapshot(_ctx())
    assert r.ok and isinstance(r.data, dict)
    assert r.data["login_masked"] == "mt5_****0001" and "login" not in r.data
    assert r.data["equity"] == 10_000.0 and r.data["currency"] == "EUR"
    # deterministic serialization (sorted keys), no MT5 SimpleNamespace escapes.
    assert list(r.data) == sorted(r.data)
    assert all(not isinstance(v, SimpleNamespace) for v in r.data.values())


def test_positions_and_orders_map_to_canonical(monkeypatch):
    pos = [_fake_mt5.make_position(9001, 0, 0.2, symbol="EURUSD.r")]
    adapter, _ = _connected_mt5(monkeypatch, positions=pos)
    positions = adapter.positions(_ctx())
    assert positions and positions[0]["positionId"] == "9001"
    assert positions[0]["symbol"] == "EURUSD"      # canonical, not the broker alias
    orders = adapter.orders(_ctx())
    assert isinstance(orders, list)


def test_recent_executions_maps_history_deals(monkeypatch):
    adapter, _ = _connected_mt5(monkeypatch, with_history=True)
    r = adapter.recent_executions(_ctx())
    assert r.ok and r.data and r.data[0]["deal_id"] == "5001"
    assert r.data[0]["symbol"] == "EURUSD" and list(r.data[0]) == sorted(r.data[0])


def test_reconcile_snapshot_is_a_coherent_live_read(monkeypatch):
    adapter, _ = _connected_mt5(monkeypatch)
    r = adapter.reconcile_snapshot(_ctx())
    assert r.ok and r.data["provenance"] == "live_mt5"
    assert set(r.data) >= {"positions", "orders", "accounts", "connection",
                           "accountIdentity", "at", "provenance"}


def test_canonical_models_are_immutable_and_deterministic():
    info = ba.BrokerAccountInfo(login_masked="mt5_****1", fingerprint=None,
                                broker_company="B", server="S", currency="EUR",
                                balance=1.0, equity=1.0, margin=0.0, margin_free=1.0,
                                margin_level=None, leverage=100)
    with pytest.raises(Exception):
        info.balance = 2.0
    assert list(info.as_dict()) == sorted(info.as_dict())
    assert ba.SymbolSpec("EURUSD", "EURUSD.r").as_dict()["canonical"] == "EURUSD"
    assert ba.TerminalInfo(connected=True).as_dict()["connected"] is True


def test_no_mt5_type_name_escapes_the_adapter_boundary():
    # Structural: broker.py maps MT5 objects; no canonical caller mentions them.
    for module in ("execution.py", "execution_context.py", "execution_telemetry.py",
                   "reconciliation.py", "server.py"):
        code = code_only(module)
        for forbidden in ("MetaTrader5", "positions_get", "account_info(",
                          "history_deals_get", "symbol_info_tick", "mt5_gateway"):
            assert forbidden not in code, f"{module} references MT5 internal {forbidden}"


# ── writes stay inert; capability write-free; execution impossible ────────────

def test_every_write_operation_is_inert():
    mt5 = broker_layer.get_broker("mt5")
    assert mt5.submit_command("CloseTrade", _ctx()) == (None, None)
    assert mt5.cancel_order("1", _ctx()) == (None, None)
    assert mt5.modify_order("1", {}, _ctx()) == (None, None)
    assert mt5.flatten("d", _ctx()) == []
    assert mt5.submit_order(None, _ctx()).code == ba.RESULT_UNAVAILABLE
    assert mt5.close_position("1", _ctx()).code == ba.RESULT_UNAVAILABLE


def test_write_capability_is_exactly_the_live2_market_order():
    """LIVE-1 pinned every write capability False. LIVE-2 DELIBERATELY adds
    exactly one: MT5 market-order submission. The mock still has no live-write
    capability, and every OTHER MT5 mutation capability remains absent."""
    mock_caps = broker_layer.capability_dict(broker_layer.get_broker("mock").capabilities())
    assert mock_caps["supportsLiveWrite"] is False        # fixture, never a live writer
    mt5caps = broker_layer.capability_dict(broker_layer.get_broker("mt5").capabilities())
    assert mt5caps["supportsLiveWrite"] is True           # LIVE-2: the ONE write
    assert mt5caps["supportsMarketExecution"] is True
    for w in ("supportsPendingOrders", "supportsModify",
              "supportsPartialClose", "supportsHedging", "supportsNetting"):
        assert mt5caps[w] is False


def test_execution_safety_still_denies_every_execution_command_under_mt5(monkeypatch):
    import execution_safety as es
    import server
    monkeypatch.setenv(ba.VAR_ADAPTER, "mt5")
    monkeypatch.setattr(ba, "_CACHE", {})
    ctx = server._execution_context()             # non-mock -> deny-by-default
    for cmd in ("CloseTrade", "CancelOrder", "GlobalKill", "PauseDeployment"):
        d = es.evaluate(es.CommandRequest(command_type=cmd),
                        ctx.to_safety_context(cmd))
        assert d.allowed is False


# ── failure handling: no crash, machine code, execution unavailable ───────────

def test_missing_package_yields_unavailable_not_a_crash(monkeypatch):
    monkeypatch.setattr(broker_layer, "_load_live_gateway", lambda: None)
    mt5 = broker_layer.MT5Adapter()
    for call in (lambda: mt5.account_snapshot(_ctx()),
                 lambda: mt5.account_identity(),
                 lambda: mt5.recent_executions(_ctx()),
                 lambda: mt5.reconcile_snapshot(_ctx())):
        r = call()
        assert r.ok is False and r.code == ba.RESULT_UNAVAILABLE


def test_disconnected_terminal_is_not_connected(monkeypatch):
    fake = _fake_mt5.FakeMT5()
    gw = MT5Gateway(_Cfg(), sdk=fake)             # never connect()
    mt5 = broker_layer.MT5Adapter()
    mt5._gateway_cache = gw; mt5._gateway_loaded = True; mt5._policy_denied_reason = None
    r = mt5.account_snapshot(_ctx())
    assert r.ok is False and r.code == ba.RESULT_NOT_CONNECTED


def test_login_failure_maps_to_not_connected(monkeypatch):
    fake = _fake_mt5.FakeMT5()
    fake.initialize = lambda **k: False           # login/init fails
    gw = MT5Gateway(_Cfg(), sdk=fake)
    ok, detail = gw.connect()
    assert ok is False
    mt5 = broker_layer.MT5Adapter()
    mt5._gateway_cache = gw; mt5._gateway_loaded = True; mt5._policy_denied_reason = None
    assert mt5.account_snapshot(_ctx()).code == ba.RESULT_NOT_CONNECTED


def test_malformed_mt5_data_is_caught_and_reported(monkeypatch):
    adapter, fake = _connected_mt5(monkeypatch)
    fake.account_info = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
    r = adapter.account_snapshot(_ctx())
    assert r.ok is False and r.code == "error"
    assert "boom" not in r.detail                 # no traceback / message leakage
    assert "RuntimeError" in r.detail


def test_partial_read_missing_history_is_explicit(monkeypatch):
    adapter, fake = _connected_mt5(monkeypatch)   # fake has no history_deals_get
    if hasattr(fake, "history_deals_get"):
        delattr(fake, "history_deals_get")
    r = adapter.recent_executions(_ctx())
    assert r.ok is False and r.code == ba.RESULT_UNAVAILABLE


def test_account_unavailable_is_explicit(monkeypatch):
    adapter, fake = _connected_mt5(monkeypatch, with_account=False)
    fake._account = None
    r = adapter.account_snapshot(_ctx())
    assert r.ok is False and r.code == ba.RESULT_UNAVAILABLE
