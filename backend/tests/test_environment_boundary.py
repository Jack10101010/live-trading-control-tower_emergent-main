"""M-ENV-1 — the deployment-environment admissibility boundary.

These tests prove BEHAVIOUR, not constants. The production cases actually import
`server` in a child process with a production environment and assert the process
refuses to start with the exact reason code — because the guarantee under test is
"the application does not serve requests", which cannot be shown by inspecting a
frozenset.

SAFETY: no test here connects to MT5, constructs a broker adapter, or places an
order. Every production case is a REJECTION case, which aborts during module
import — long before any adapter is constructed. The one admissible production
configuration is exercised through the pure policy function only; a real
production process is deliberately never booted.
"""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
import pytest                                                       # noqa: E402

import broker_adapter                                               # noqa: E402
import environment                                                  # noqa: E402
import fixture_preview_service
import fixture_world                                                # noqa: E402
import market_data                                                  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent


# ── DEVELOPMENT (1–7) ────────────────────────────────────────────────────────
def test_1_unset_environment_resolves_to_development(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    assert environment.resolve() == environment.ENV_DEVELOPMENT
    assert environment.is_production() is False


def test_1b_blank_and_whitespace_resolve_to_development(monkeypatch):
    for blank in ("", "   ", "\t"):
        monkeypatch.setenv(environment.VAR_ENVIRONMENT, blank)
        assert environment.resolve() == environment.ENV_DEVELOPMENT


def test_2_explicit_development_resolves_to_development(monkeypatch):
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, "development")
    assert environment.resolve() == environment.ENV_DEVELOPMENT
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, "  DEVELOPMENT  ")
    assert environment.resolve() == environment.ENV_DEVELOPMENT


def test_3_mock_broker_default_remains_available_in_development(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    monkeypatch.delenv(broker_adapter.VAR_ADAPTER, raising=False)
    assert broker_adapter.active_kind() == "mock"       # unchanged development default
    environment.enforce_startup()                        # and startup accepts it


def test_4_fixture_market_provider_default_remains_available(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    monkeypatch.delenv(environment.VAR_MARKET_DATA_PROVIDER, raising=False)
    assert environment.configured_market_data_provider() is None
    assert "fixture" in environment.policy().market_data_providers
    environment.enforce_startup()


def test_5_replay_and_mock_live_remain_admissible_in_development(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    pol = environment.policy()
    assert pol.replay_permitted is True
    for provider in ("replay", "mock_live"):
        assert provider in pol.market_data_providers
        monkeypatch.setenv(environment.VAR_MARKET_DATA_PROVIDER, provider)
        environment.enforce_startup()                    # no raise


def test_6_world_loading_remains_available_in_development(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    assert environment.policy().world_may_load is True
    # The real loader runs and finds the real fixture — no exception, world present.
    world = fixture_world.load((fixture_preview_service.fixture_asset_path(),))   # M-WORLD-0: canonical resolver
    assert world.available is True


def test_7_development_startup_path_is_valid_for_every_known_capability(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    for adapter in sorted(environment.KNOWN_BROKER_ADAPTERS):
        for provider in sorted(environment.KNOWN_MARKET_DATA_PROVIDERS):
            monkeypatch.setenv(broker_adapter.VAR_ADAPTER, adapter)
            monkeypatch.setenv(environment.VAR_MARKET_DATA_PROVIDER, provider)
            assert environment.enforce_startup() == environment.ENV_DEVELOPMENT


# ── PRODUCTION policy (8–22) ─────────────────────────────────────────────────
def _production(monkeypatch, adapter=None, provider=None):
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, "production")
    for var, value in ((broker_adapter.VAR_ADAPTER, adapter),
                       (environment.VAR_MARKET_DATA_PROVIDER, provider)):
        if value is None:
            monkeypatch.delenv(var, raising=False)
        else:
            monkeypatch.setenv(var, value)


def test_8_explicit_production_resolves_to_production(monkeypatch):
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, "production")
    assert environment.resolve() == environment.ENV_PRODUCTION
    assert environment.is_production() is True


@pytest.mark.parametrize("adapter,expected", [
    (None, "broker_adapter_unset"),                       # 9
    ("mock", "broker_adapter_inadmissible: mock"),        # 10
    ("mockk", "broker_adapter_unknown: mockk"),           # 11
    ("paper", "broker_adapter_unknown: paper"),           # 11
])
def test_9_10_11_production_rejects_broker_adapter(monkeypatch, adapter, expected):
    _production(monkeypatch, adapter=adapter, provider="mt5")
    with pytest.raises(environment.EnvironmentViolation) as exc:
        environment.enforce_startup()
    assert str(exc.value) == expected


def test_12_explicit_real_broker_adapter_passes_the_policy(monkeypatch):
    # Policy only — no adapter is constructed and no terminal is contacted.
    _production(monkeypatch, adapter="mt5", provider="mt5")
    assert environment.enforce_startup() == environment.ENV_PRODUCTION


@pytest.mark.parametrize("provider,expected", [
    (None, "market_provider_unset"),                              # 13
    ("fixture", "market_provider_inadmissible: fixture"),         # 14
    ("mock_live", "market_provider_inadmissible: mock_live"),     # 15
    ("replay", "market_provider_inadmissible: replay"),           # 16
    ("store", "market_provider_unknown: store"),                  # 17
    ("polygon", "market_provider_unknown: polygon"),              # 17
    ("mt-5", "market_provider_unknown: mt-5"),                    # 17
])
def test_13_to_17_production_rejects_market_provider(monkeypatch, provider, expected):
    _production(monkeypatch, adapter="mt5", provider=provider)
    with pytest.raises(environment.EnvironmentViolation) as exc:
        environment.enforce_startup()
    assert str(exc.value) == expected


def test_18_explicit_admissible_real_provider_passes(monkeypatch):
    _production(monkeypatch, adapter="mt5", provider="mt5")
    pol = environment.policy()
    assert pol.market_data_providers == frozenset({"mt5"})
    assert environment.enforce_startup() == environment.ENV_PRODUCTION


def test_19_fixture_world_activation_is_rejected_in_production(monkeypatch):
    _production(monkeypatch, adapter="mt5", provider="mt5")
    assert environment.policy().world_may_load is False
    with pytest.raises(environment.EnvironmentViolation) as exc:
        environment.require_fixture_activation_allowed()
    assert exc.value.reason == environment.REASON_FIXTURE_FORBIDDEN
    # The loader itself refuses — a real, existing fixture path is used, so this
    # proves the guard fires BEFORE any file is opened, not that the file is gone.
    real_fixture = fixture_preview_service.fixture_asset_path()   # M-WORLD-0
    assert real_fixture.exists(), "test needs the real fixture present to be meaningful"
    with pytest.raises(environment.EnvironmentViolation):
        fixture_world.load((real_fixture,))


@pytest.mark.parametrize("value", ["staging", "prod", "dev", "PRODUCTION_", "test"])
def test_20_unknown_environment_value_is_rejected(monkeypatch, value):
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, value)
    with pytest.raises(environment.EnvironmentViolation) as exc:
        environment.resolve()
    assert str(exc.value) == f"environment_invalid: {value.lower()}"


def test_21_no_rejected_capability_falls_back_to_a_development_source(monkeypatch):
    # (a) an unknown environment never degrades to development
    monkeypatch.setenv(environment.VAR_ENVIRONMENT, "staging")
    with pytest.raises(environment.EnvironmentViolation):
        environment.policy()
    # (b) an unset adapter in production never degrades to the mock default
    _production(monkeypatch, adapter=None, provider="mt5")
    with pytest.raises(environment.EnvironmentViolation) as exc:
        broker_adapter.active_kind()
    assert exc.value.reason == environment.REASON_BROKER_UNSET
    # (c) the production policy contains no development capability at all
    pol = environment.policy()
    assert "mock" not in pol.broker_adapters
    assert pol.market_data_providers.isdisjoint({"fixture", "mock_live", "replay"})
    assert pol.replay_permitted is False


def test_21b_the_mock_adapter_cannot_be_CONSTRUCTED_in_production(monkeypatch):
    """The strongest form of criterion 6: not "mock is not selected", but "the
    single construction path refuses to build it", so an explicit call site
    cannot smuggle the mock broker into a production process."""
    _production(monkeypatch, adapter="mt5", provider="mt5")
    # `_CACHE` is a PROCESS global that other tests legitimately populate by
    # calling `get_adapter("mock")` in development. Asserting the key is absent
    # would therefore test the order tests happened to run in, not this call.
    # The property that actually matters is that THIS call built nothing.
    before = dict(broker_adapter._CACHE)
    with pytest.raises(environment.EnvironmentViolation) as exc:
        broker_adapter.get_adapter("mock")
    assert str(exc.value) == "broker_adapter_inadmissible: mock"
    assert broker_adapter._CACHE == before, "get_adapter constructed an adapter"


def test_21c_explicit_get_adapter_still_works_in_development(monkeypatch):
    monkeypatch.delenv(environment.VAR_ENVIRONMENT, raising=False)
    adapter = broker_adapter.get_adapter("mock")
    assert type(adapter).__name__ == "MockBroker"
    # And an unknown kind still fails with the ORIGINAL error, not the new one.
    with pytest.raises(broker_adapter.UnknownAdapterError):
        broker_adapter.get_adapter("nope")


def test_22_failure_messages_identify_the_exact_capability_and_value(monkeypatch):
    _production(monkeypatch, adapter="mock", provider="fixture")
    with pytest.raises(environment.EnvironmentViolation) as exc:
        environment.enforce_startup()
    # The FIRST violation is reported, and it names both capability and value.
    assert exc.value.reason == environment.REASON_BROKER_INADMISSIBLE
    assert exc.value.value == "mock"
    assert str(exc.value) == "broker_adapter_inadmissible: mock"


# ── REGRESSION (23–28) ───────────────────────────────────────────────────────
def test_23_control_tower_mode_semantics_are_unchanged():
    import security_config
    assert security_config.VAR_MODE == "CONTROL_TOWER_MODE"
    assert security_config.VAR_MODE != environment.VAR_ENVIRONMENT
    # M-ENV-1 must not read, write or reinterpret the existing mode variable.
    src = Path(environment.__file__).read_text()
    code = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    body = code.split('"""')[-1]                    # drop the module docstring
    assert "CONTROL_TOWER_MODE" not in body, \
        "environment.py references CONTROL_TOWER_MODE — the mode variable was repurposed"


def test_24_execution_mode_semantics_are_unchanged():
    import execution_mode
    assert execution_mode.MODE_OBSERVE == "observe"
    assert execution_mode.GOVERNED_MODES == frozenset(
        {execution_mode.MODE_OBSERVE, execution_mode.MODE_MANUAL_LIVE,
         execution_mode.MODE_HALTED})
    # The environment axis shares no value vocabulary with the execution mode.
    assert environment.KNOWN_ENVIRONMENTS.isdisjoint(execution_mode.GOVERNED_MODES)


def test_25_connection_policy_semantics_are_unchanged():
    import connection_policy
    assert connection_policy.VAR_PROFILE == "CONTROL_TOWER_CONNECTION_PROFILE"
    assert connection_policy.APPROVED_PROFILES == frozenset(
        {connection_policy.PROFILE_LOCAL_LOOPBACK})
    assert environment.KNOWN_ENVIRONMENTS.isdisjoint(connection_policy.KNOWN_PROFILES)


def test_26_capability_registries_match_the_canonical_sources():
    """The admissibility matrix must be DERIVED from reality, not asserted.

    If someone registers a new adapter or provider, this fails until the matrix
    is revisited — which is the point: a new capability must be classified
    deliberately, never admitted into production by omission.
    """
    assert environment.KNOWN_BROKER_ADAPTERS == frozenset(broker_adapter.known_kinds())
    registered = {cls.provider_id for cls in (
        market_data.FixtureProvider, market_data.ReplayProvider,
        market_data.MockLiveProvider, market_data.MT5MarketDataProvider)}
    assert environment.KNOWN_MARKET_DATA_PROVIDERS == registered
    # `store` and `polygon` are DataService candle sources, not engine providers.
    assert {"store", "polygon"}.isdisjoint(environment.KNOWN_MARKET_DATA_PROVIDERS)


def test_27_only_one_module_parses_the_environment_variable():
    offenders = []
    for path in sorted(BACKEND.glob("*.py")):
        if path.name == "environment.py":
            continue
        if environment.VAR_ENVIRONMENT in path.read_text():
            offenders.append(path.name)
    assert offenders == [], \
        f"{offenders} parse {environment.VAR_ENVIRONMENT} — it has one canonical authority"


def test_27b_development_tests_need_no_injected_environment_variable():
    # The suite that just ran did not set it, and everything above passed.
    assert environment.VAR_ENVIRONMENT not in os.environ or \
        os.environ[environment.VAR_ENVIRONMENT] in ("", "development")


def test_28_health_reports_the_resolved_environment():
    from fastapi.testclient import TestClient
    import server
    body = TestClient(server.app).get("/api/health").json()
    assert body["environment"] == "development"
    assert server.ENVIRONMENT == "development"
    # Development still serves the fixture world, so the derived fields agree.
    assert body["backendMode"] == "fixture"
    assert body["dataSources"]["world"] == "fixture"


# ── STARTUP BEHAVIOUR: the process genuinely refuses to start ────────────────
def _boot(env_overrides: dict) -> subprocess.CompletedProcess:
    """Import `server` in a child process. Import alone is the whole startup
    path for module-scope validation: if it raises, uvicorn never serves."""
    env = dict(os.environ)
    env.update({k: v for k, v in env_overrides.items() if v is not None})
    for key, value in env_overrides.items():
        if value is None:
            env.pop(key, None)
    env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{BACKEND / 'tests'}"
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import _fake_mt5      # never touch a real terminal
            import server         # module scope IS the startup path
            print("STARTED", server.ENVIRONMENT)
        """)],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180)


@pytest.mark.parametrize("adapter,provider,reason", [
    (None, "mt5", "broker_adapter_unset"),
    ("mock", "mt5", "broker_adapter_inadmissible: mock"),
    ("mt5", None, "market_provider_unset"),
    ("mt5", "fixture", "market_provider_inadmissible: fixture"),
    ("mt5", "replay", "market_provider_inadmissible: replay"),
    ("mt5", "mock_live", "market_provider_inadmissible: mock_live"),
])
def test_production_startup_aborts_before_serving(adapter, provider, reason):
    out = _boot({"CONTROL_TOWER_ENVIRONMENT": "production",
                 "CONTROL_TOWER_BROKER_ADAPTER": adapter,
                 "MARKET_DATA_PROVIDER": provider})
    assert out.returncode != 0, f"process started with {reason!r} configuration"
    assert "STARTED" not in out.stdout
    assert reason in out.stderr, out.stderr[-2000:]
    assert "EnvironmentViolation" in out.stderr


def test_invalid_environment_value_aborts_startup():
    out = _boot({"CONTROL_TOWER_ENVIRONMENT": "staging"})
    assert out.returncode != 0
    assert "environment_invalid: staging" in out.stderr


def test_development_startup_succeeds_with_no_environment_variables():
    out = _boot({"CONTROL_TOWER_ENVIRONMENT": None,
                 "CONTROL_TOWER_BROKER_ADAPTER": None,
                 "MARKET_DATA_PROVIDER": None})
    assert out.returncode == 0, out.stderr[-2000:]
    assert "STARTED development" in out.stdout


def test_startup_logs_the_resolved_environment_exactly_once():
    """A dropped log line is a real failure mode: `logging.basicConfig` runs late
    in `server.py`, so a line emitted at resolution time is silently discarded.
    This asserts the line actually reaches a handler, and appears once."""
    env = dict(os.environ)
    for key in ("CONTROL_TOWER_ENVIRONMENT", "CONTROL_TOWER_BROKER_ADAPTER",
                "MARKET_DATA_PROVIDER"):
        env.pop(key, None)
    env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{BACKEND / 'tests'}"
    out = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import logging
            logging.basicConfig(level=logging.INFO, format="LOG %(message)s")
            import _fake_mt5
            import server
        """)],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180)
    lines = [l for l in out.stderr.splitlines() if "ENVIRONMENT resolved" in l]
    assert len(lines) == 1, out.stderr[-2000:]
    assert "resolved=development" in lines[0]
    assert "fixture_world_permitted=true" in lines[0]
    # Redaction: the line reports counts and booleans, never variable values.
    assert "mock" not in lines[0] and "mt5" not in lines[0]


def test_rejected_production_config_starts_no_loops_and_mounts_no_routes():
    """The abort must happen before any background worker or route exists."""
    env = dict(os.environ)
    env.update({"CONTROL_TOWER_ENVIRONMENT": "production"})
    env.pop("CONTROL_TOWER_BROKER_ADAPTER", None)
    env["PYTHONPATH"] = f"{BACKEND}{os.pathsep}{BACKEND / 'tests'}"
    out = subprocess.run(
        [sys.executable, "-c", textwrap.dedent("""
            import sys, threading
            import _fake_mt5
            before = threading.active_count()
            try:
                import server
            except Exception as exc:
                print("ABORTED", type(exc).__name__, exc)
                print("THREADS_UNCHANGED", threading.active_count() == before)
                print("SERVER_NOT_IMPORTED", "server" not in sys.modules
                      or not hasattr(sys.modules.get("server"), "app"))
                sys.exit(3)
            sys.exit(0)
        """)],
        cwd=str(BACKEND), env=env, capture_output=True, text=True, timeout=180)
    assert out.returncode == 3, out.stderr[-2000:]
    assert "ABORTED EnvironmentViolation broker_adapter_unset" in out.stdout
    assert "THREADS_UNCHANGED True" in out.stdout
    assert "SERVER_NOT_IMPORTED True" in out.stdout
