"""LIVE-5B — integration diagnostics, recovery, logging and the smoke checklist.

Proves: every failure names what failed / why / the likely fix; a gateway
misconfiguration is no longer reported as "package unavailable"; reconnect is
attempted, counted and safe; state logging is change-only; the twelve-stage
checklist never passes by omission; and no credential VALUE is ever disclosed.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker as broker_layer                                  # noqa: E402
import integration_smoke as smoke                               # noqa: E402
import market_runtime as mr                                     # noqa: E402
import mt5_diagnostics as dx                                    # noqa: E402
import runtime_supervisor as rsup                               # noqa: E402
import server                                                   # noqa: E402
from test_recommendation_domain import statements_only           # noqa: E402

client = TestClient(server.app)
T0 = "2026-07-28T10:00:00Z"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RECOMMENDATION_DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", False)
    yield


# ── PART 2: every failure explains itself ────────────────────────────────────

def test_every_failing_check_states_what_why_and_the_fix():
    """The contract of this whole module: never a generic failure."""
    diagnosis = dx.diagnose(now=T0, execution_mode="observe")
    failures = [c for c in diagnosis.checks if c.status == dx.FAIL]
    assert failures, "expected failures on a host without MetaTrader5"
    for check in failures:
        assert check.detail, f"{check.name} has no detail"
        assert check.why, f"{check.name} does not say WHY"
        assert check.fix, f"{check.name} offers no fix"
        assert len(check.fix) > 20, f"{check.name} fix is not actionable"


def test_the_platform_check_names_the_real_blocker():
    check = dx.check_platform()
    import platform as _p
    if _p.system() == "Windows":
        assert check.status == dx.PASS
    else:
        assert check.status == dx.FAIL
        assert "Windows wheel" in check.why
        assert "VPS" in check.fix


def test_the_root_cause_is_the_first_failure_in_dependency_order():
    diagnosis = dx.diagnose(now=T0, execution_mode="observe")
    blocker = diagnosis.first_blocker()
    assert blocker is not None
    assert blocker.name == diagnosis.blocking[0]


def test_dependent_checks_are_skipped_not_failed_again():
    """One root cause, not eight cascading errors."""
    diagnosis = dx.diagnose(now=T0, execution_mode="observe")
    by_name = {c.name: c for c in diagnosis.checks}
    if by_name["mt5_package"].status == dx.FAIL:
        assert by_name["terminal"].status == dx.SKIPPED
        assert by_name["symbol"].status == dx.SKIPPED
        assert "prerequisite" in by_name["terminal"].why


def test_a_ready_chain_reports_ready():
    """With every dependency satisfied the diagnosis is ready — proving `ready`
    is not hard-wired to False off-VPS."""
    checks = (dx.Check("platform", dx.PASS), dx.Check("mt5_package", dx.PASS),
              dx.Check("market_provider", dx.WARN))
    assert dx.GatewayDiagnosis(at=T0, checks=checks).ready is True
    assert dx.GatewayDiagnosis(at=T0, checks=checks).blocking == ()


def test_an_empty_diagnosis_is_never_ready():
    assert dx.GatewayDiagnosis(at=T0).ready is False


@pytest.mark.parametrize("detail,expected", [
    ("mt5.initialize failed: (-6, 'Terminal: Authorization failed')", "password"),
    ("mt5.initialize failed: (-10005, 'IPC timeout')", "terminal"),
    ("MetaTrader5 package not available on this host", "install"),
])
def test_terminal_failures_map_to_actionable_fixes(detail, expected):
    check = dx.check_terminal(lambda: (False, detail))
    assert check.status == dx.FAIL
    assert expected in check.fix.lower()


def test_a_terminal_exception_is_reported_not_swallowed():
    def explode():
        raise OSError("pipe closed")
    check = dx.check_terminal(explode)
    assert check.status == dx.FAIL and check.detail == "OSError"
    assert check.fix


def test_a_zero_quote_is_reported_as_no_quote():
    check = dx.check_symbol("EURUSD", lambda _s: {"bid": None, "ask": None})
    assert check.status == dx.FAIL
    assert "0.0" in check.why or "Market Watch" in check.fix


def test_a_good_symbol_passes_with_latency():
    check = dx.check_symbol("EURUSD", lambda _s: {"bid": 1.1, "ask": 1.1001})
    assert check.status == dx.PASS
    assert check.latency_ms is not None


# ── the silent-failure defect ────────────────────────────────────────────────

def test_the_gateway_loader_names_the_failing_step(monkeypatch):
    """Before LIVE-5B a config failure was reported as "package unavailable"."""
    gateway, reason = broker_layer.load_live_gateway_diagnostic()
    assert gateway is None                     # no MetaTrader5 on this host
    assert reason is not None
    assert "Windows wheel" in reason or "not importable" in reason


def test_the_adapter_reports_the_specific_reason_not_a_generic_one():
    adapter = broker_layer.MT5Adapter()
    detail = adapter.connection().detail
    assert detail
    # It must be the specific loader reason, not the old blanket sentence with
    # no explanation of WHY.
    assert "not importable" in detail or "unavailable" in detail
    assert adapter._unavailable_detail() == detail


def test_the_loader_no_longer_swallows_every_exception():
    code = statements_only("broker.py")
    start = code.index("def load_live_gateway_diagnostic")
    end = code.index("def _load_live_gateway")
    body = code[start:end]
    # Each step is guarded SEPARATELY and returns a reason; there is no single
    # blanket handler that loses the cause.
    assert body.count("except Exception") >= 4
    # `statements_only` normalises via ast.unparse, so `return None, reason`
    # appears as `return (None, reason)`.
    assert "return (None, " in body


# ── PART 9: recovery ─────────────────────────────────────────────────────────

def _market(connection="Connected", quote=True, now=T0):
    quotes = {"EURUSD": ({"bid": 1.1, "ask": 1.1001, "at": now} if quote else None)}
    return mr.MarketRuntime(
        symbols=("EURUSD",), now_iso_fn=lambda: now,
        quote_fn=lambda s: quotes.get(s), connection_fn=lambda: connection,
        monotonic_fn=lambda: 0.0), quotes


def test_a_down_link_triggers_one_reconnect_attempt():
    market, _q = _market(connection=mr.CONN_DISCONNECTED)
    attempts = []
    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0,
        reconnect_fn=lambda: (attempts.append(1), (False, "still down"))[1])
    health = supervisor.tick_once().health
    assert len(attempts) == 1
    assert health.reconnect_attempts == 1
    assert health.reconnect_successes == 0
    assert health.last_failure_detail


def test_a_successful_reconnect_re_reads_immediately():
    """The operator should not wait a whole interval to see the repair."""
    state = {"connection": mr.CONN_DISCONNECTED}
    quotes = {"EURUSD": {"bid": 1.1, "ask": 1.1001, "at": T0}}
    market = mr.MarketRuntime(
        symbols=("EURUSD",), now_iso_fn=lambda: T0,
        quote_fn=lambda s: quotes.get(s),
        connection_fn=lambda: state["connection"], monotonic_fn=lambda: 0.0)

    def reconnect():
        state["connection"] = mr.CONN_CONNECTED
        return True, "connected"

    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0,
        reconnect_fn=reconnect)
    health = supervisor.tick_once().health
    assert health.state == rsup.CONNECTED       # recovered within ONE tick
    assert health.reconnect_attempts == 1 and health.reconnect_successes == 1
    assert health.last_reconnect_at == T0


def test_a_raising_reconnect_cannot_break_the_tick():
    market, _q = _market(connection=mr.CONN_DISCONNECTED)

    def explode():
        raise RuntimeError("gateway gone")

    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0, reconnect_fn=explode)
    health = supervisor.tick_once().health     # must not raise
    assert health.reconnect_attempts == 1
    assert any("reconnect_failed" in w for w in health.warnings)


def test_no_reconnect_is_attempted_while_healthy():
    market, _q = _market()
    attempts = []
    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0,
        reconnect_fn=lambda: (attempts.append(1), (True, "x"))[1])
    supervisor.tick_once()
    assert attempts == [], "reconnected while the link was healthy"


def test_recovery_never_touches_a_position():
    """Only the READ path is auto-recovered. Anything that could move a position
    fails closed and waits for a human."""
    code = statements_only("runtime_supervisor.py")
    for forbidden in ("close_position", "submit_market_order", "order_send",
                      "flatten", "modify_position"):
        assert forbidden not in code, f"supervisor references {forbidden}"


# ── PART 8: logging is structured and change-only ────────────────────────────

def test_state_logging_fires_on_transition_only(caplog):
    market, _q = _market()
    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0,
        logger=logging.getLogger("live5b.test"))
    with caplog.at_level(logging.INFO, logger="live5b.test"):
        for _ in range(4):
            supervisor.tick_once()
    lines = [r for r in caplog.records if "runtime.state" in r.getMessage()]
    assert len(lines) == 1, "a per-tick log line would be pure noise"
    message = lines[0].getMessage()
    for field in ("at=", "from=", "to=", "connection=", "tick=", "failures="):
        assert field in message


def test_a_transition_is_logged_once_each_way(caplog):
    state = {"connection": mr.CONN_CONNECTED}
    quotes = {"EURUSD": {"bid": 1.1, "ask": 1.1001, "at": T0}}
    market = mr.MarketRuntime(
        symbols=("EURUSD",), now_iso_fn=lambda: T0,
        quote_fn=lambda s: quotes.get(s),
        connection_fn=lambda: state["connection"], monotonic_fn=lambda: 0.0)
    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0,
        logger=logging.getLogger("live5b.test2"))
    with caplog.at_level(logging.INFO, logger="live5b.test2"):
        supervisor.tick_once()
        state["connection"] = mr.CONN_DISCONNECTED
        supervisor.tick_once()
        supervisor.tick_once()
    lines = [r for r in caplog.records if "runtime.state" in r.getMessage()]
    assert len(lines) == 2


def test_a_broken_logger_cannot_break_a_tick():
    class Boom:
        def info(self, *_a, **_k):
            raise RuntimeError("log sink down")

        def warning(self, *_a, **_k):
            raise RuntimeError("log sink down")

        def exception(self, *_a, **_k):
            raise RuntimeError("log sink down")

    market, _q = _market()
    supervisor = rsup.RuntimeSupervisor(
        market=market, broker_snapshot_fn=lambda: {"positions": []},
        now_iso_fn=lambda: T0, monotonic_fn=lambda: 0.0, logger=Boom())
    assert supervisor.tick_once().health.state == rsup.CONNECTED


# ── PART 7: the diagnostics endpoint ─────────────────────────────────────────

def test_the_diagnostics_endpoint_covers_every_subsystem():
    client.post("/api/live-runtime/tick")
    response = client.get("/api/integration/diagnostics")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"
    body = response.json()
    names = {row["name"] for row in body["subsystems"]}
    assert names == {"gateway", "runtime", "broker", "projection", "execution",
                     "recommendation", "scenario", "ledger"}
    for row in body["subsystems"]:
        assert set(row) >= {"name", "status", "detail", "lastSuccess",
                            "lastFailure", "latencyMs", "freshness", "warnings"}
        assert row["status"] in (dx.PASS, dx.WARN, dx.FAIL, dx.UNKNOWN)


def test_the_diagnostics_endpoint_is_read_only():
    for verb in (client.post, client.put, client.patch, client.delete):
        assert verb("/api/integration/diagnostics").status_code in (404, 405)


def test_diagnostics_never_disclose_a_credential_value(monkeypatch):
    """A variable NAME in remediation advice is fine; a VALUE never is."""
    monkeypatch.setenv("MT5_PASSWORD", "sup3r-s3cret-value")
    monkeypatch.setenv("MT5_LOGIN", "80412345")
    monkeypatch.setenv("MT5_SERVER", "BrokerDemo-Server-7")
    body = client.get("/api/integration/diagnostics").text
    assert "sup3r-s3cret-value" not in body
    assert "80412345" not in body               # masked, never whole
    assert "BrokerDemo-Server-7" not in body


def test_login_and_server_are_masked_recognisably():
    assert dx.mask("80412345") == "80…45"
    assert dx.mask("abc") == "…"
    assert dx.mask(None) is None
    assert dx.mask("") is None


def test_the_password_variable_is_never_read_for_its_value():
    code = statements_only("mt5_diagnostics.py")
    assert 'os.environ.get("MT5_PASSWORD")' not in code
    assert '_env("MT5_PASSWORD")' not in code
    assert "MT5_PASSWORD" in code               # named in advice only


def test_the_diagnostics_module_places_no_order():
    code = statements_only("mt5_diagnostics.py")
    for forbidden in ("order_send", "submit_market_order", "open_position",
                      "close_position", "modify_position"):
        assert forbidden not in code


# ── PART 4: the new dashboard fields ─────────────────────────────────────────

def test_the_live_projection_exposes_the_operational_fields():
    client.post("/api/live-runtime/tick")
    body = client.get("/api/live-runtime").json()
    broker = body["broker"]
    for field in ("login", "accountType", "brokerCompany", "gatewayLatencyMs",
                  "reconnectCount", "connected", "server", "leverage",
                  "balance", "equity", "margin", "freeMargin"):
        assert field in broker, f"broker card is missing {field}"
    runtime = body["runtime"]
    for field in ("reconnectAttempts", "reconnectSuccesses", "lastFailureAt",
                  "lastFailureDetail", "projectionAgeSeconds", "state"):
        assert field in runtime, f"runtime card is missing {field}"
    for symbol in body["symbols"]:
        for field in ("bid", "ask", "spread", "quoteAt", "candleClosedAt",
                      "availability"):
            assert field in symbol


def test_account_type_is_absent_rather_than_guessed():
    """`trade_mode` is None unless the terminal reported a mode it could
    classify. Demo-vs-real must never be inferred."""
    import broker_adapter as ba
    info = ba.BrokerAccountInfo(
        login_masked="***1234", fingerprint=None, broker_company=None,
        server=None, currency=None, balance=None, equity=None, margin=None,
        margin_free=None, margin_level=None, leverage=None)
    assert info.as_dict()["trade_mode"] is None


# ── PART 5: the smoke checklist ──────────────────────────────────────────────

def _live(**over):
    base = {
        "projectionTimestamp": T0,
        "broker": {"connected": True, "adapterKind": "mt5",
                   "connectionState": "Connected", "pingMs": 12.0},
        "runtime": {"state": "CONNECTED", "tickCount": 5,
                    "projectionAgeSeconds": 2},
        "symbols": [{"symbol": "EURUSD", "live": True, "availability": "ok",
                     "bid": 1.1, "ask": 1.1001, "spread": 0.0001,
                     "quoteAgeSeconds": 1, "candleAvailability": "ok",
                     "candleTimeframe": "M15", "candleClosedAt": T0,
                     "candleAgeSeconds": 30}],
        "execution": {"activePositions": 1, "positionsAvailable": True,
                      "openRecommendations": 1},
    }
    base.update(over)
    return base


class _Rec:
    def __init__(self, status, intents=()):
        self.status = status
        self.linked_intent_ids = tuple(intents)


class _Lineage:
    def __init__(self, scenario, recommendation):
        self.scenario_id = scenario
        self.recommendation_id = recommendation


class _Trade:
    def __init__(self, scenario="scn_1", recommendation="rcm_1"):
        self.lineage = _Lineage(scenario, recommendation)


def test_the_checklist_has_exactly_twelve_ordered_stages():
    report = smoke.run(live_runtime=_live(), now=T0)
    assert [r.stage for r in report.results] == list(smoke.STAGES)
    assert len(smoke.STAGES) == 12


def test_a_complete_pipeline_passes_every_stage():
    report = smoke.run(
        live_runtime=_live(), scenarios=["scn_1"],
        recommendations=[_Rec("EXECUTED", intents=("intent_1",))],
        ledger_trades=[_Trade()], now=T0)
    assert report.complete is True, report.render()
    assert report.passed == 12
    assert report.first_failure is None
    assert "ALL STAGES PASS" in report.render()


def test_the_checklist_never_passes_by_omission():
    """Missing evidence is FAIL with a fix, never a silent skip."""
    report = smoke.run(live_runtime=_live(), now=T0)
    assert report.complete is False
    assert report.first_failure.stage == "scenario_created"
    for result in report.results:
        if result.status == smoke.FAIL:
            assert result.fix, f"{result.stage} fails without a fix"


def test_a_disconnected_broker_fails_the_first_stage():
    report = smoke.run(
        live_runtime=_live(broker={"connected": False,
                                   "connectionState": "Disconnected"}), now=T0)
    assert report.results[0].status == smoke.FAIL
    assert "diagnostics" in report.results[0].fix


def test_an_unaccepted_recommendation_fails_the_acceptance_stage():
    report = smoke.run(live_runtime=_live(), scenarios=["scn_1"],
                       recommendations=[_Rec("PROPOSED")], now=T0)
    by_stage = {r.stage: r for r in report.results}
    assert by_stage["recommendation_created"].status == smoke.PASS
    assert by_stage["operator_acceptance"].status == smoke.FAIL
    assert "never accepts on your behalf" in by_stage["operator_acceptance"].fix


def test_a_trade_without_lineage_fails_the_ledger_stage():
    report = smoke.run(
        live_runtime=_live(), scenarios=["scn_1"],
        recommendations=[_Rec("EXECUTED", intents=("i1",))],
        ledger_trades=[_Trade(scenario="scn_1", recommendation=None)], now=T0)
    by_stage = {r.stage: r for r in report.results}
    assert by_stage["position_closes"].status == smoke.PASS
    assert by_stage["ledger_updated"].status == smoke.FAIL


def test_the_checklist_performs_no_write():
    code = statements_only("integration_smoke.py")
    for forbidden in ("submit_market_order", "order_send", "record_operator_decision",
                      "create_scenario", "close_position", "accept("):
        assert forbidden not in code, f"the checklist calls {forbidden}"


def test_the_report_renders_the_first_failure_prominently():
    rendered = smoke.run(live_runtime=_live(), now=T0).render()
    assert "FIRST FAILURE: scenario_created" in rendered
    assert "fix:" in rendered


# ── structural: nothing else changed ─────────────────────────────────────────

def test_the_execution_command_surface_is_unchanged():
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})


def test_broker_write_capabilities_are_unchanged():
    caps = broker_layer.capability_dict(broker_layer.MT5Adapter().capabilities())
    for enabled in ("supportsLiveWrite", "supportsMarketExecution",
                    "supportsModify", "supportsCancelOrder",
                    "supportsClosePosition"):
        assert caps[enabled] is True
    for disabled in ("supportsPendingOrders", "supportsPartialClose",
                     "supportsHedging", "supportsNetting", "supportsReplay"):
        assert caps[disabled] is False


def test_switching_adapter_needs_no_code_change(monkeypatch):
    """PART 10: MOCK -> DEMO -> LIVE is environment only."""
    import broker_adapter as ba
    monkeypatch.delenv(ba.VAR_ADAPTER, raising=False)
    assert broker_layer.active_kind() == "mock"
    monkeypatch.setenv(ba.VAR_ADAPTER, "mt5")
    assert broker_layer.active_kind() == "mt5"
    assert broker_layer.get_broker().kind == "mt5"
    monkeypatch.setenv(ba.VAR_ADAPTER, "nonsense")
    assert broker_layer.active_kind() == "nonsense"   # returned verbatim
    with pytest.raises(Exception):
        broker_layer.get_broker()                     # and fails closed


def test_the_default_adapter_is_still_mock(monkeypatch):
    """PART 10: never require a live account."""
    import broker_adapter as ba
    monkeypatch.delenv(ba.VAR_ADAPTER, raising=False)
    assert ba.ACTIVE_KIND == "mock"
    assert broker_layer.active_kind() == "mock"


def test_no_execution_module_learned_about_diagnostics():
    for module in ("execution.py", "execution_safety.py", "reconciliation.py",
                   "command_registry.py", "command_authorization.py",
                   "execution_mode.py", "trade_ledger_domain.py",
                   "trade_reconstruction.py", "scenario_domain.py",
                   "recommendation_domain.py"):
        code = statements_only(module)
        for forbidden in ("mt5_diagnostics", "integration_smoke",
                          "runtime_supervisor"):
            assert forbidden not in code, f"{module} depends on {forbidden}"


def test_the_browser_still_never_contacts_mt5():
    frontend = REPO_ROOT / "frontend" / "src"
    hits = []
    for path in frontend.rglob("*.ts*"):
        text = path.read_text(errors="ignore")
        for token in ("MetaTrader5", "symbol_info_tick", "copy_rates",
                      "mt5_gateway", "terminal64"):
            if token in text:
                hits.append(f"{path.name}:{token}")
    assert hits == [], f"frontend references MT5 directly: {hits}"
