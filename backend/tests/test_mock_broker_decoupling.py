"""M-MOCK-DECOUPLE-1 — the MockBroker is a self-contained test double.

THE COUPLING THIS REMOVED
    `server._broker_context()` bound `accounts`, `live_trades`, `trade_current`
    and `trade_by_order_id` to the UI fixture world, for EVERY adapter. An AST
    scan shows only `MockBroker` consumed them and `MT5Adapter` reads nothing
    but `ctx.now` — yet one request to `/api/operations/accounts` under the
    development-default mock adapter loaded all 22 authored collections to
    answer a question about a stub.

    `ctx.brokers()` was in the interface and was never called by anything. It
    was the last reason the fixture's broker collection was reachable from the
    broker path, and it is gone.

WHAT THIS SUITE PROTECTS
    Not UI honesty — the ordinary frontend already rejects these records as
    `mock-fixture` and continues to. It protects the RUNTIME's independence from
    `world.v1.json`, which is the last blocker before M-WORLD-0.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broker_adapter                                                # noqa: E402
import broker_provenance as bp                                       # noqa: E402
import fixture_preview_service as fps                                # noqa: E402
import mock_broker_data as mbd                                       # noqa: E402
import server                                                        # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

client = TestClient(server.app)


def _child(script: str, tmp_path: Path, env_extra: dict | None = None):
    env = {**os.environ, "CONTROL_TOWER_STATE_DIR": str(tmp_path / "state")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          cwd=str(BACKEND_DIR), capture_output=True, text=True,
                          timeout=180, env=env)


def _fn(module_name: str, func_name: str) -> ast.FunctionDef:
    tree = ast.parse((BACKEND_DIR / module_name).read_text())
    return next(n for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                and n.name == func_name)


# ══════════════════════════════════════════════════════════════════════════════
# 1–2  The broker path cannot reach the fixture
# ══════════════════════════════════════════════════════════════════════════════

def test_1_broker_context_never_calls_the_fixture_accessor():
    """AST, not string matching: prose explaining the removal is not a call."""
    for name in ("_broker_context", "_execution_env"):
        node = _fn("server.py", name)
        called = {getattr(c.func, "id", None) for c in ast.walk(node)
                  if isinstance(c, ast.Call)}
        assert "_preview_world" not in called, f"{name} reaches the fixture"
        assert "_optional_fixture" not in called, f"{name} reaches the fixture"


def test_1b_no_fixture_accessor_is_captured_in_a_broker_closure():
    """A lambda closing over the accessor would defeat the direct-call check."""
    node = _fn("server.py", "_broker_context")
    for lam in [n for n in ast.walk(node) if isinstance(n, ast.Lambda)]:
        called = {getattr(c.func, "id", None) for c in ast.walk(lam)
                  if isinstance(c, ast.Call)}
        assert not ({"_preview_world", "_optional_fixture"} & called), ast.dump(lam)


def test_2_mock_broker_module_does_not_import_the_fixture():
    tree = ast.parse((BACKEND_DIR / "broker.py").read_text())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert "fixture_preview_service" not in imported
    assert "fixture_world" not in imported


# ══════════════════════════════════════════════════════════════════════════════
# 3–4  The dataset is inert source
# ══════════════════════════════════════════════════════════════════════════════

def test_3_mock_broker_data_performs_no_file_io():
    tree = ast.parse((BACKEND_DIR / "mock_broker_data.py").read_text())
    called = {getattr(c.func, "id", None) for c in ast.walk(tree)
              if isinstance(c, ast.Call)}
    for forbidden in ("open", "load", "loads", "read_text", "read"):
        assert forbidden not in called, forbidden
    source = (BACKEND_DIR / "mock_broker_data.py").read_text()
    assert "json" not in source.split('"""')[2], "no JSON parsing in the dataset"


def test_4_mock_broker_data_imports_no_fixture_module():
    tree = ast.parse((BACKEND_DIR / "mock_broker_data.py").read_text())
    imported = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            imported.update(a.name.split(".")[0] for a in n.names)
        elif isinstance(n, ast.ImportFrom) and n.module:
            imported.add(n.module.split(".")[0])
    assert imported <= {"__future__", "copy", "types"}, sorted(imported)


# ══════════════════════════════════════════════════════════════════════════════
# 5–7  Route and adapter separation
# ══════════════════════════════════════════════════════════════════════════════

def test_5_ordinary_operational_routes_never_load_the_fixture(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        for p in ('/api/operations/accounts', '/api/operations/positions',
                  '/api/operations/orders', '/api/operations/summary',
                  '/api/broker/positions', '/api/broker/orders',
                  '/api/broker/accounts', '/api/broker-health'):
            c.get(p)
        print('LOADCOUNT=%d' % fps.load_count())
    """, tmp_path)
    assert "LOADCOUNT=0" in result.stdout, result.stdout + result.stderr


def test_6_preview_routes_do_not_import_the_mock_dataset():
    """A test double and an authored UI preview are different things with
    different lifetimes; collapsing them is what created the coupling."""
    tree = ast.parse((BACKEND_DIR / "server.py").read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.name.startswith("dev_"):
            continue
        used = {n.value.id for n in ast.walk(node)
                if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)}
        assert "mock_broker_data" not in used, node.name


def test_7_mt5_adapter_consumes_none_of_the_record_callables():
    """The premise of the whole decoupling, re-proved rather than assumed."""
    tree = ast.parse((BACKEND_DIR / "broker.py").read_text())
    mt5 = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "MT5Adapter")
    ctx_fields = {n.attr for n in ast.walk(mt5)
                  if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                  and n.value.id == "ctx"}
    assert not (ctx_fields & {"accounts", "live_trades", "trade_current",
                              "trade_by_order_id"}), sorted(ctx_fields)


def test_7b_the_dead_brokers_callable_is_gone_from_the_interface():
    assert not hasattr(broker_adapter.BrokerContext, "brokers")
    assert "brokers" not in broker_adapter.BrokerContext.__dataclass_fields__


# ══════════════════════════════════════════════════════════════════════════════
# 8–9  Moving the data grants NO authority
# ══════════════════════════════════════════════════════════════════════════════

def test_8_mock_records_remain_inadmissible():
    """The load-bearing property. Relocating records from a JSON fixture into
    Python source must not promote them: they are the same invented figures."""
    accounts = client.get("/api/operations/accounts").json()["accounts"]
    assert accounts, "the mock adapter must still project an account"
    for account in accounts:
        assert account["provenance"] == bp.PROV_MOCK_FIXTURE
        assert bp.is_broker_truth(account["provenance"]) is False
        assert account["provenance"] not in (bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5)


def test_8b_the_honesty_sentinels_are_preserved_exactly():
    """Half a dozen guards scan for these numbers. Making the mock "less
    conspicuous" would blind every one of them."""
    assert mbd.MOCK_ACCOUNTS[0]["balance"] == 100000.0
    assert mbd.MOCK_ACCOUNTS[0]["equity"] == 100412.0
    body = str(client.get("/api/operations/accounts").json())
    assert "100000" in body and "mock-fixture" in body


def test_9_production_cannot_select_the_mock_adapter(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import broker_adapter, environment
        try:
            broker_adapter.get_adapter('mock')
            print('MOCK_CONSTRUCTED')
        except Exception as exc:
            print('REFUSED=%s' % type(exc).__name__)
        print('WORLDMAYLOAD=%s' % environment.policy().world_may_load)
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5"})
    assert "REFUSED" in result.stdout, result.stdout + result.stderr
    assert "WORLDMAYLOAD=False" in result.stdout, result.stdout


# ══════════════════════════════════════════════════════════════════════════════
# 10–12  Startup, missing fixture, mutation safety
# ══════════════════════════════════════════════════════════════════════════════

def test_10_server_import_remains_fixture_read_free(tmp_path):
    result = _child("""
        import sys, builtins; sys.path.insert(0, '.')
        _real = builtins.open; reads = []
        def counting(f, *a, **k):
            try:
                if 'world.v1.json' in str(f): reads.append(str(f))
            except Exception: pass
            return _real(f, *a, **k)
        builtins.open = counting
        import server
        builtins.open = _real
        print('READS=%d' % len(reads))
    """, tmp_path)
    assert "READS=0" in result.stdout, result.stdout + result.stderr


@pytest.mark.parametrize("state,paths", [
    ("absent", ["/nonexistent/world.v1.json"]),
    ("corrupt", None),          # written by the test body
])
def test_11_the_mock_broker_works_with_no_usable_fixture(tmp_path, state, paths):
    """Missing and corrupt both prove the same property: the broker path has no
    opinion about `world.v1.json` because it never looks at it."""
    if paths is None:
        broken = tmp_path / "world.v1.json"
        broken.write_text("{ not json ]")
        paths = [str(broken)]
    result = _child(f"""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        fps.configure({paths!r})
        c = TestClient(server.app)
        codes = [c.get(p).status_code for p in
                 ('/api/operations/accounts', '/api/operations/positions',
                  '/api/operations/orders', '/api/operations/summary')]
        acct = c.get('/api/operations/accounts').json()['accounts'][0]
        print('CODES=%s PROV=%s LOADCOUNT=%d'
              % (codes, acct['provenance'], fps.load_count()))
    """, tmp_path)
    assert "CODES=[200, 200, 200, 200]" in result.stdout, (
        f"{state}: {result.stdout}{result.stderr}")
    assert "PROV=mock-fixture" in result.stdout, result.stdout
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_12_module_constants_are_never_mutated_by_a_caller():
    """`MockBroker` operates against a mutable overlay store; a shared mutable
    base would let one request alter another's balance."""
    first = mbd.accounts()
    first[0]["balance"] = -1
    first.append({"accountId": "INJECTED"})
    second = mbd.accounts()
    assert second[0]["balance"] == 100000.0
    assert all(a.get("accountId") != "INJECTED" for a in second)

    trades = mbd.live_trades()
    trades[0]["state"] = "vanished"
    trades[0]["size"] = 999
    assert mbd.live_trades()[0]["state"] == "managing"
    assert mbd.live_trades()[0]["size"] != 999

    # The constants themselves are read-only mappings.
    with pytest.raises(TypeError):
        mbd.MOCK_ACCOUNTS[0]["balance"] = 0


def test_12b_repeated_requests_are_deterministic():
    first = client.get("/api/operations/accounts").json()
    second = client.get("/api/operations/accounts").json()
    del first["projectionTimestamp"], second["projectionTimestamp"]
    for a in (*first["accounts"], *second["accounts"]):
        a.pop("freshness", None)
    assert first == second


# ══════════════════════════════════════════════════════════════════════════════
# 13–14  No duplicate dataset; the boundary shrank
# ══════════════════════════════════════════════════════════════════════════════

def test_13_no_second_mock_dataset_exists():
    """One test double, in one place. A second copy is how two "mock accounts"
    drift apart and a test starts asserting against the wrong one."""
    offenders = []
    for path in BACKEND_DIR.rglob("*.py"):
        if "__pycache__" in str(path) or path.name == "mock_broker_data.py":
            continue
        text = path.read_text(errors="ignore")
        if "acct_mock_" in text and "mock_broker_data" not in text:
            offenders.append(path.name)
    assert offenders == [], offenders


def test_14_the_fixture_boundary_exception_list_shrank():
    import fixture_surfaces as fsurf
    assert fsurf.BOUNDARY_HELPERS == frozenset({"_operator_id"}), (
        sorted(fsurf.BOUNDARY_HELPERS))
    # And the routes whose exemption existed only because of the broker context
    # no longer appear in the derived fixture set at all.
    surfaces = fsurf.analyse(BACKEND_DIR / "server.py")
    for path in ("/api/broker/positions", "/api/broker/orders",
                 "/api/broker/accounts", "/api/operations/accounts"):
        assert path not in surfaces, f"{path} reads the fixture again"
