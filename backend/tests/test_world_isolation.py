"""M-WORLD-ISOLATE-1 — the fixture world is reached by asking, never by importing.

THE DEFECT
    `server.py` executed `WORLD = fixture_world.load(FIXTURE_SEARCH_PATHS)` at
    module scope. Importing the backend opened and parsed `world.v1.json`,
    allocated 22 collections of authored records, and bound them to a global any
    module could reach with `import server; server.WORLD`.

    HARDEN-1 made that load survivable when the file is missing. M-ENV-1 made it
    impossible in production. Neither made it EXPLICIT: a development process
    still paid for it on every start, twenty-seven tests received fixture data
    purely because they imported the backend, and the only thing between an
    ordinary route and authored records was an unwritten convention.

THE PROPERTY
    Importing the backend performs ZERO fixture file reads. That cannot be
    established by reading the source — the interesting failure is an import
    side effect somewhere else entirely — so it is measured in a child process
    with `open` instrumented.
"""
from __future__ import annotations

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

import fixture_preview_service as fps                                # noqa: E402
import fixture_world                                                 # noqa: E402
import server                                                        # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

client = TestClient(server.app)


def _child(script: str, tmp_path: Path, env_extra: dict | None = None):
    """Run a probe in a FRESH interpreter.

    In-process assertions cannot prove an import-time property: by the time a
    test runs, `server` is long imported and whatever it did is done.
    """
    env = {**os.environ, "CONTROL_TOWER_STATE_DIR": str(tmp_path / "state")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          cwd=str(BACKEND_DIR), capture_output=True, text=True,
                          timeout=180, env=env)


_COUNT_READS = """
    import sys, builtins
    sys.path.insert(0, '.')
    _real = builtins.open
    reads = []
    def counting(file, *a, **k):
        try:
            if 'world.v1.json' in str(file): reads.append(str(file))
        except Exception: pass
        return _real(file, *a, **k)
    builtins.open = counting
    import server
    builtins.open = _real
    import fixture_preview_service as fps
    print('READS=%d LOADCOUNT=%d LOADED=%s HASWORLD=%s'
          % (len(reads), fps.load_count(), fps.is_loaded(), hasattr(server, 'WORLD')))
"""


# ══════════════════════════════════════════════════════════════════════════════
# 1–3  Importing the backend does not touch the fixture
# ══════════════════════════════════════════════════════════════════════════════

def test_1_server_import_performs_zero_fixture_file_reads(tmp_path):
    result = _child(_COUNT_READS, tmp_path)
    assert "READS=0" in result.stdout, result.stdout + result.stderr
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_2_ordinary_startup_allocates_no_fixture_world(tmp_path):
    result = _child(_COUNT_READS, tmp_path)
    assert "LOADED=False" in result.stdout, result.stdout
    # The global itself is gone — not merely unloaded. A lazy proxy left behind
    # under the old name would preserve the accident this milestone removed.
    assert "HASWORLD=False" in result.stdout, result.stdout


def test_3_ordinary_endpoints_never_invoke_the_loader(tmp_path):
    """The boundary middleware runs on EVERY `/api` request. If its availability
    probe loaded the fixture, the first health check would undo the milestone.

    `/api/health` was exactly that case and is fixed: it derived `backendMode`
    from `_preview_world().available`, so the first health check pulled all 22
    collections into memory.

    The broker-backed operations endpoints are DELIBERATELY EXCLUDED here and
    covered by `test_3b` below, which documents why.
    """
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        for path in ('/api/health', '/api/live-runtime', '/api/live/status',
                     '/api/operations/nodes', '/api/live/connection',
                     '/api/integration/diagnostics', '/api/feature-flags',
                     # M-MOCK-DECOUPLE-1: the broker-backed operations surface
                     # joins this list. It used to be excluded because the mock
                     # adapter pulled the UI fixture through the broker context.
                     '/api/operations/accounts', '/api/operations/positions',
                     '/api/operations/orders', '/api/operations/summary'):
            c.get(path)
        print('LOADCOUNT=%d' % fps.load_count())
    """, tmp_path)
    assert "LOADCOUNT=0" in result.stdout, result.stdout + result.stderr


def test_3b_the_mock_broker_never_invokes_the_fixture_loader(tmp_path):
    """M-MOCK-DECOUPLE-1 — INVERTED. This test formerly asserted the opposite.

    `_broker_context` bound `accounts`, `live_trades`, `trade_current` and
    `trade_by_order_id` to the UI fixture world for EVERY adapter, though only
    `MockBroker` consumed them and `MT5Adapter` reads nothing but `ctx.now`. A
    single request to `/api/operations/accounts` under the development-default
    mock adapter therefore loaded all 22 authored collections to answer a
    question about a stub.

    The mock adapter now carries its own dataset (`mock_broker_data`), so the
    whole broker path is fixture-free. The old assertion (`AFTER=1`) is kept in
    the history as the thing that had to change, not in the suite.
    """
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps, broker_adapter
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        c.get('/api/operations/accounts')
        c.get('/api/operations/positions')
        # Constructing the adapter directly must not load it either.
        broker_adapter.get_adapter('mock')
        print('KIND=%s LOADCOUNT=%d' % (broker_adapter.active_kind(), fps.load_count()))
    """, tmp_path)
    assert "KIND=mock" in result.stdout, result.stdout + result.stderr
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_3c_the_mock_broker_serves_its_own_records_with_no_fixture_present(tmp_path):
    """The decoupling proved end to end: no fixture file, full mock surface."""
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        fps.configure(['/nonexistent/world.v1.json'])
        c = TestClient(server.app)
        codes = [c.get(p).status_code for p in
                 ('/api/operations/accounts', '/api/operations/positions',
                  '/api/operations/orders', '/api/operations/summary')]
        acct = c.get('/api/operations/accounts').json()['accounts'][0]
        print('CODES=%s PROV=%s BAL=%s' % (codes, acct['provenance'], acct['balance']))
    """, tmp_path)
    assert "CODES=[200, 200, 200, 200]" in result.stdout, result.stdout + result.stderr
    # Still mock-stamped, still carrying the sentinel every honesty guard scans
    # for. Moving the data out of the fixture must not promote its authority.
    assert "PROV=mock-fixture" in result.stdout, result.stdout
    assert "BAL=100000.0" in result.stdout, result.stdout


# ══════════════════════════════════════════════════════════════════════════════
# 4–5  Explicit previews load; production cannot
# ══════════════════════════════════════════════════════════════════════════════

def test_4_an_explicit_preview_request_loads_lazily(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        before = fps.load_count()
        r = c.get('/api/dev/fixture-events')
        print('BEFORE=%d AFTER=%d STATUS=%d' % (before, fps.load_count(), r.status_code))
    """, tmp_path)
    assert "BEFORE=0" in result.stdout, result.stdout + result.stderr
    assert "AFTER=1" in result.stdout, result.stdout
    assert "STATUS=200" in result.stdout, result.stdout


def test_5_production_cannot_invoke_the_loader(tmp_path):
    """Fail-closed at the environment boundary, before any path is examined."""
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import fixture_preview_service as fps
        fps.configure([str(fps.fixture_asset_path())])
        try:
            fps.get_world()
            print('LOADED_IN_PRODUCTION')
        except fps.FixturePreviewUnavailable as exc:
            print('REFUSED code=%s loadcount=%d' % (exc.code, fps.load_count()))
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5",
    })
    assert "REFUSED" in result.stdout, result.stdout + result.stderr
    assert f"code={fps.CODE_NOT_PERMITTED}" in result.stdout, result.stdout
    assert "loadcount=0" in result.stdout, result.stdout


# ══════════════════════════════════════════════════════════════════════════════
# 6–8  Missing and corrupt fixtures are LOCAL failures
# ══════════════════════════════════════════════════════════════════════════════

def test_6_a_missing_fixture_does_not_break_ordinary_startup(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        fps.configure(['/nonexistent/world.v1.json'])
        c = TestClient(server.app)
        ok = all(c.get(p).status_code == 200
                 for p in ('/api/health', '/api/live-runtime', '/api/feature-flags'))
        try:
            fps.get_world(); preview = 'LOADED'
        except fps.FixturePreviewUnavailable as exc:
            preview = 'REFUSED:' + exc.code
        print('ORDINARY_OK=%s PREVIEW=%s' % (ok, preview))
    """, tmp_path)
    assert "ORDINARY_OK=True" in result.stdout, result.stdout + result.stderr
    assert "PREVIEW=REFUSED" in result.stdout, result.stdout


def test_7_a_corrupt_fixture_does_not_break_ordinary_startup(tmp_path):
    corrupt = tmp_path / "world.v1.json"
    corrupt.write_text("{ this is not json ]")
    result = _child(f"""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        fps.configure([{str(corrupt)!r}])
        c = TestClient(server.app)
        ok = c.get('/api/health').status_code == 200
        try:
            fps.get_world(); preview = 'LOADED'
        except fps.FixturePreviewUnavailable as exc:
            preview = 'REFUSED:' + exc.code
        # A failed load must NOT be cached: fixing the file and retrying has to
        # work without restarting the process.
        print('ORDINARY_OK=%s PREVIEW=%s CACHED=%s' % (ok, preview, fps.is_loaded()))
    """, tmp_path)
    assert "ORDINARY_OK=True" in result.stdout, result.stdout + result.stderr
    assert "PREVIEW=REFUSED" in result.stdout, result.stdout
    assert "CACHED=False" in result.stdout, result.stdout


def test_8_preview_failure_is_explicit_and_machine_readable():
    fps.reset()
    fps.configure(["/nonexistent/world.v1.json"])
    try:
        with pytest.raises(fps.FixturePreviewUnavailable) as caught:
            fps.get_world()
        assert caught.value.code == fps.CODE_UNAVAILABLE
        assert "development previews only" in caught.value.detail
        # It names what it searched, without disclosing host layout.
        assert "/nonexistent" not in caught.value.detail
    finally:
        fps.configure(server.FIXTURE_SEARCH_PATHS)
        fps.reset()


# ══════════════════════════════════════════════════════════════════════════════
# 9–10  Determinism and isolation between callers
# ══════════════════════════════════════════════════════════════════════════════

def test_9_repeated_preview_responses_are_deterministic():
    first = client.get("/api/dev/fixture-events")
    second = client.get("/api/dev/fixture-events")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


def test_10_a_preview_caller_cannot_mutate_the_shared_world():
    """`collection()` deep-copies. With one shared global, a handler that
    mutated what it was handed poisoned every later reader and made test order
    significant."""
    fps.reset()
    first = fps.collection("accounts")
    assert first, "fixture must expose accounts for this test to mean anything"
    first[0]["balance"] = -1
    first.append({"accountId": "INJECTED"})
    second = fps.collection("accounts")
    assert second[0]["balance"] != -1
    assert all(a.get("accountId") != "INJECTED" for a in second)
    fps.reset()


def test_10b_install_for_test_does_not_leak_into_the_next_test():
    """The autouse conftest fixture resets around every test. Without it, a test
    that poisons the world leaves the poison in place for the rest of the run —
    the order dependence this milestone removes."""
    assert not fps.is_loaded(), (
        "a previous test leaked a loaded fixture world into this one")


# ══════════════════════════════════════════════════════════════════════════════
# 11–14  No ordinary reader remains, in either language
# ══════════════════════════════════════════════════════════════════════════════

def test_11_no_module_reads_a_world_global_from_server():
    """`server.WORLD` is gone; nothing may reintroduce it or reach for it."""
    assert not hasattr(server, "WORLD")
    offenders = []
    for path in BACKEND_DIR.rglob("*.py"):
        if "__pycache__" in str(path) or path.name.startswith("test_"):
            continue
        # The service legitimately owns the private cache `_WORLD`; it is the
        # one module allowed to hold the world, and it exposes it only through
        # functions (asserted separately by test 20).
        if path.name == "fixture_preview_service.py":
            continue
        for line in path.read_text(errors="ignore").splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue          # prose explaining the removal is not a read
            if "server.WORLD" in line or stripped.startswith("WORLD ="):
                offenders.append(f"{path.name}: {stripped[:70]}")
    assert offenders == [], offenders


def test_11b_only_explicit_preview_handlers_reach_the_fixture():
    """Every fixture-reading route is either a `/api/dev/*` preview or one of
    the legacy fixture routes already gated by the derived boundary. A NEW
    ordinary route reading the fixture fails here."""
    import fixture_surfaces
    surfaces = fixture_surfaces.analyse(BACKEND_DIR / "server.py")
    gated = fixture_surfaces.gated_paths(surfaces)
    ungated_readers = set(surfaces) - gated - set(fixture_surfaces.PRODUCTION_BACKED)
    assert ungated_readers == set(), sorted(ungated_readers)
    # And every gated path is refused when the fixture is unavailable.
    assert gated <= set(server.FIXTURE_ONLY_PATHS)


def test_12_no_ordinary_frontend_hook_calls_a_fixture_endpoint():
    """Ordinary hooks may not import the preview hooks. The existing structural
    guard in `fleetProvenance.test.ts` owns the component half; this asserts the
    hook module keeps every fixture fetch behind a `useFixture*Preview` name."""
    hooks = (REPO_ROOT / "frontend" / "src" / "hooks" / "useRepository.ts").read_text()
    import re
    # Every `queryFn: api.<fixtureFn>` must sit inside a preview-named function.
    fixture_fns = ("api.fleet", "api.packages", "api.activePackage",
                   "api.recommendations", "api.fixtureEvents")
    for match in re.finditer(r"export function (\w+)\(", hooks):
        name = match.group(1)
        body = hooks[match.end():hooks.find("\nexport function", match.end())]
        used = [f for f in fixture_fns if f in body]
        if used and "Preview" not in name:
            raise AssertionError(f"ordinary hook {name} fetches {used}")


def test_13_dev_preview_routes_are_named_as_such_or_explicitly_gated():
    import fixture_surfaces
    surfaces = fixture_surfaces.analyse(BACKEND_DIR / "server.py")
    for path in fixture_surfaces.gated_paths(surfaces):
        assert path.startswith("/api/dev/") or path in server.FIXTURE_ONLY_PATHS, path


def test_14_legacy_fixture_endpoints_refuse_when_the_fixture_is_unavailable():
    fps.install_for_test(fixture_world.FixtureWorld(None))
    try:
        for path in ("/api/dev/fixture-world", "/api/dev/fixture-fleet", "/api/dev/fixture-packages", "/api/dev/fixture-trades"):
            response = client.get(path)
            assert response.status_code == 501, (path, response.status_code)
            assert response.json()["code"] == fixture_world.CODE_FIXTURE_ABSENT
    finally:
        fps.reset()


# ══════════════════════════════════════════════════════════════════════════════
# 15–17  Broker, provider and environment stay decoupled
# ══════════════════════════════════════════════════════════════════════════════

def test_15_mock_broker_construction_does_not_load_the_ui_fixture(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import broker_adapter, fixture_preview_service as fps
        adapter = broker_adapter.get_adapter('mock')
        print('KIND=%s LOADCOUNT=%d' % (adapter.kind, fps.load_count()))
    """, tmp_path)
    assert "KIND=mock" in result.stdout, result.stdout + result.stderr
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_16_market_data_provider_registry_loads_no_fixture_at_import(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import market_data, fixture_preview_service as fps
        print('LOADCOUNT=%d' % fps.load_count())
    """, tmp_path)
    assert "LOADCOUNT=0" in result.stdout, result.stdout + result.stderr


def test_17_the_production_environment_matrix_is_unchanged(tmp_path):
    """M-ENV-1 behaviour must be bit-identical: production still refuses the
    mock adapter and still cannot activate the fixture."""
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import environment
        print('ENV=%s' % environment.resolve())
        print('WORLDMAYLOAD=%s' % environment.policy().world_may_load)
        try:
            environment.require_broker_adapter_admissible('mock')
            print('MOCK_ALLOWED')
        except Exception as exc:
            print('MOCK_REFUSED=%s' % type(exc).__name__)
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5",
    })
    assert "ENV=production" in result.stdout, result.stdout + result.stderr
    assert "WORLDMAYLOAD=False" in result.stdout, result.stdout
    assert "MOCK_REFUSED" in result.stdout, result.stdout


# ══════════════════════════════════════════════════════════════════════════════
# 18–20  Nothing regressed, nothing widened
# ══════════════════════════════════════════════════════════════════════════════

def test_18_operational_surfaces_still_reject_fixture_provenance():
    """The honesty guards from earlier milestones must remain green: this
    milestone moved WHERE fixture data lives, not WHETHER it is admitted."""
    import broker_provenance as bp
    import node_provenance as np_
    assert bp.is_broker_truth(bp.PROV_MOCK_FIXTURE) is False
    assert np_.is_authoritative_node_observation({"provenance": "fixture-node"}) is False


def test_19_every_mock_record_on_an_operational_surface_is_inadmissible():
    """The property that actually protects an operator — and NOT byte-absence.

    An earlier draft of this guard asserted the string `100000` never appears in
    `/api/operations/accounts`. That was a stronger claim than this architecture
    has ever made, and asserting it would have been asserting the wrong thing.

    Under the development-default mock adapter the projection genuinely emits
    the fixture's balance — stamped `mock-fixture`. M-FLEET-2 established the
    contract deliberately: the ENDPOINT may carry a record, and the GATE decides
    whether it may be rendered. Endpoint identity is not provenance, in both
    directions; a record's presence on `/api/operations/*` proves nothing about
    its admissibility, which is exactly why the gate exists.

    So the honest assertion is that every record carrying an invented figure is
    refused by the gate, which is what an operator's screen depends on.
    """
    import broker_provenance as bp
    for path in ("/api/operations/accounts", "/api/operations/summary"):
        body = client.get(path).json()
        for account in (body.get("accounts") or []):
            if "100000" in str(account) or "100412" in str(account):
                assert not bp.is_broker_truth(account["provenance"]), (
                    f"{path} carries an invented balance under an ADMISSIBLE "
                    f"provenance: {account['provenance']}")


def test_19b_no_fixture_sentinel_reaches_a_node_or_telemetry_surface():
    """Node surfaces have no mock-adapter path at all, so byte-absence IS the
    right assertion there — M-NODE-READ-1 removed every tower-sourced field."""
    for path in ("/api/operations/nodes", "/api/live/status", "/api/live/connection"):
        text = str(client.get(path).json())
        for sentinel in ("100000", "100412", "InTrade",
                         "op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ"):
            assert sentinel not in text, f"{sentinel} on {path}"


def test_20_the_fixture_service_exposes_no_module_global_world():
    """A lazy proxy bound to a module attribute would preserve the accident:
    ordinary code could still reach fixture data without naming this module."""
    public = {name for name in dir(fps) if not name.startswith("_")}
    for name in sorted(public):
        value = getattr(fps, name)
        assert not isinstance(value, fixture_world.FixtureWorld), (
            f"fixture_preview_service.{name} is a module-global world")
    # The cache exists, but it is private and reachable only through functions.
    assert "_WORLD" in dir(fps)


def test_20b_status_does_not_trigger_a_load():
    """A diagnostics endpoint that loaded the fixture in order to report on it
    would undo the isolation on the first health check."""
    fps.reset()
    status = fps.status()
    assert status["loaded"] is False
    assert status["loadCount"] == 0
    assert fps.is_loaded() is False
