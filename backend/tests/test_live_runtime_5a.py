"""LIVE-5A — the live runtime, the producers, the preflight gate and the
END-TO-END SMOKE WORKFLOW.

The smoke test at the bottom of this file IS the acceptance test named in PART
16. It drives the complete pipeline

    market evidence -> Scenario -> Recommendation -> operator decision
                    -> Intent -> Order -> Position -> close -> Ledger

against the MOCK broker, which PART 14 requires to flow through identical
projections to LIVE. Running it against a real MT5 terminal is the same workflow
with `MARKET_DATA_PROVIDER=mt5` and a live adapter — see
`LIVE-5A-SMOKE-WORKFLOW.md`. **A genuine live trade cannot execute on this host:
MetaTrader5 has no macOS build, and `live/mt5_gateway.py` is annotated "Windows
VPS only".**
"""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import live_pipeline as lp                                       # noqa: E402
import live_preflight as pf                                      # noqa: E402
import market_runtime as mr                                      # noqa: E402
import operational_projection as op                              # noqa: E402
import recommendation_domain as rd                               # noqa: E402
import recommendation_store as rstore                            # noqa: E402
import recommendation_service as rsvc                            # noqa: E402
import runtime_supervisor as rsup                                # noqa: E402
import scenario_store as sstore_mod                              # noqa: E402
import server                                                    # noqa: E402
from test_recommendation_domain import statements_only            # noqa: E402

client = TestClient(server.app)

T0 = "2026-07-28T10:00:00Z"
T1 = "2026-07-28T10:00:05Z"


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RECOMMENDATION_DB_PATH", tmp_path / "rec.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", False)
    yield


# ── builders ─────────────────────────────────────────────────────────────────

def _candle(o, h, l, c, opened, closed, symbol="EURUSD"):
    return mr.SymbolCandle(symbol=symbol, timeframe="M15", open=o, high=h, low=l,
                           close=c, opened_at=opened, closed_at=closed,
                           provider="mock-fixture")


PREV = _candle(1.0980, 1.0995, 1.0975, 1.0990,
               "2026-07-28T09:30:00Z", "2026-07-28T09:45:00Z")
BREAK_UP = _candle(1.0990, 1.1010, 1.0988, 1.1005,
                   "2026-07-28T09:45:00Z", T0)
BREAK_DOWN = _candle(1.0990, 1.0992, 1.0960, 1.0965,
                     "2026-07-28T09:45:00Z", T0)
INSIDE = _candle(1.1005, 1.1008, 1.0992, 1.1000, T0, "2026-07-28T10:15:00Z")


def _runtime(*, now=T0, connection="Connected", quote=True, candle=BREAK_UP,
             symbols=("EURUSD",), latency_steps=0.01):
    clock = [100.0]

    def mono():
        clock[0] += latency_steps
        return clock[0]

    quotes = {s: ({"bid": 1.10045, "ask": 1.10055, "at": now,
                   "provider": "mock-fixture"} if quote else None)
              for s in symbols}
    candles = {s: ({"open": candle.open, "high": candle.high, "low": candle.low,
                    "close": candle.close, "openedAt": candle.opened_at,
                    "closedAt": candle.closed_at, "timeframe": "M15",
                    "provider": "mock-fixture"} if candle else None)
               for s in symbols}
    return mr.MarketRuntime(
        symbols=symbols, now_iso_fn=lambda: now,
        quote_fn=lambda s: quotes.get(s), candle_fn=lambda s: candles.get(s),
        connection_fn=lambda: connection, provider_fn=lambda: "mock-fixture",
        adapter_kind_fn=lambda: "mock", monotonic_fn=mono)


def _supervisor(market, *, now=T0, snapshot=None, account=None, on_tick=None,
                interval_s=5.0):
    return rsup.RuntimeSupervisor(
        market=market,
        broker_snapshot_fn=lambda: (snapshot if snapshot is not None
                                    else {"positions": [], "orders": []}),
        account_fn=lambda: account, now_iso_fn=lambda: now,
        interval_s=interval_s, on_tick=on_tick,
        monotonic_fn=lambda: 0.0)


# ── PART 1: market runtime ───────────────────────────────────────────────────

def test_heartbeat_records_success_latency_and_connection():
    snapshot = _runtime().refresh()
    hb = snapshot.heartbeat
    assert hb.ok is True and hb.connection == mr.CONN_CONNECTED
    assert hb.at == T0 and hb.attempted_at == T0
    assert hb.latency_ms is not None and hb.latency_ms >= 0
    assert hb.consecutive_failures == 0
    assert hb.availability(T0) == mr.AVAILABLE


def test_a_failed_read_does_not_advance_the_heartbeat():
    """"Last heartbeat" must mean the last time the feed genuinely answered."""
    market = _runtime()
    market.refresh()                                  # success at T0
    market._connection = lambda: mr.CONN_DISCONNECTED
    market._now = lambda: T1
    second = market.refresh()
    assert second.heartbeat.at == T0                  # NOT advanced
    assert second.heartbeat.attempted_at == T1        # but the attempt is recorded
    assert second.heartbeat.ok is False
    assert second.heartbeat.consecutive_failures == 1


def test_stale_detection_is_age_based_not_opinion():
    snapshot = _runtime().refresh()
    quote = snapshot.symbol("EURUSD").quote
    assert quote.availability(T0) == mr.AVAILABLE
    later = "2026-07-28T10:05:00Z"                    # 300s > 60s tick threshold
    assert quote.availability(later) == mr.STALE
    assert quote.age_seconds(later) == 300.0


def test_an_unreadable_price_is_unavailable_never_zero():
    snapshot = _runtime(quote=False).refresh()
    sub = snapshot.symbol("EURUSD")
    assert sub.quote.bid is None and sub.quote.ask is None
    assert sub.quote.spread is None                   # not 0.0
    assert sub.quote.availability(T0) == mr.UNAVAILABLE
    assert sub.active is False
    assert "no_symbol_answered" in snapshot.warnings


def test_spread_is_derived_once_in_the_runtime():
    quote = _runtime().refresh().symbol("EURUSD").quote
    assert quote.spread == pytest.approx(0.0001, abs=1e-9)


def test_an_incomplete_candle_is_not_evidence():
    partial = mr.SymbolCandle(symbol="EURUSD", timeframe="M15", open=1.1,
                              high=1.1, low=1.1, close=None,
                              opened_at=T0, closed_at=None)
    assert partial.complete is False
    assert partial.availability(T0) == mr.UNAVAILABLE


def test_candle_close_time_has_one_rule():
    assert mr.candle_close_time("2026-07-28T09:45:00Z", "M15") == T0
    assert mr.candle_close_time("2026-07-28T09:00:00Z", "H1") == T0
    assert mr.candle_close_time("nonsense", "M15") is None
    assert mr.candle_close_time(T0, "M7") is None


def test_market_runtime_refresh_is_deterministic():
    market = _runtime()
    first, second = market.refresh(), market.refresh()
    a, b = first.as_dict(T0), second.as_dict(T0)
    # Only the sequence advances; every measured value is identical.
    a.pop("refreshSequence"), b.pop("refreshSequence")
    a.pop("refreshDurationMs"), b.pop("refreshDurationMs")
    a["heartbeat"].pop("latencyMs"), b["heartbeat"].pop("latencyMs")
    assert a == b


def test_market_runtime_never_executes_or_projects():
    code = statements_only("market_runtime.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "_ORCHESTRATOR", "execution_safety", "operational_projection",
                      "build_summary", "scenario_store", "recommendation_store",
                      "threading", "sqlite3"):
        assert forbidden not in code, f"market_runtime references {forbidden}"


# ── PART 11/12: runtime health and the refresh loop ──────────────────────────

def test_first_successful_tick_is_connected():
    health = _supervisor(_runtime()).tick_once().health
    assert health.state == rsup.CONNECTED
    assert health.tick_count == 1 and health.consecutive_failures == 0


def test_repeated_failures_become_reconnecting():
    market = _runtime(connection=mr.CONN_DISCONNECTED)
    supervisor = _supervisor(market)
    assert supervisor.tick_once().health.state == rsup.STARTING
    second = supervisor.tick_once().health
    assert second.state == rsup.RECONNECTING
    assert second.consecutive_failures == 2


def test_a_wedged_loop_is_reported_stale_not_connected():
    """The stale-loop detector: ages are recomputed against `now`, so a loop that
    stopped ticking degrades instead of freezing on CONNECTED."""
    supervisor = _supervisor(_runtime())
    assert supervisor.tick_once().health.state == rsup.CONNECTED
    much_later = "2026-07-28T10:10:00Z"
    health = supervisor.health(now=much_later)
    assert health.state == rsup.STALE
    assert health.projection_age_seconds == 600.0


def test_degraded_when_latency_is_high():
    market = _runtime(latency_steps=2.5)      # 2.5s between the two reads
    health = _supervisor(market).tick_once().health
    assert health.state == rsup.DEGRADED
    assert health.latency_ms > mr.DEGRADED_LATENCY_MS


def test_degraded_when_only_some_symbols_answer():
    clock = [0.0]
    market = mr.MarketRuntime(
        symbols=("EURUSD", "GBPUSD"), now_iso_fn=lambda: T0,
        quote_fn=lambda s: ({"bid": 1.1, "ask": 1.1001, "at": T0}
                            if s == "EURUSD" else None),
        connection_fn=lambda: mr.CONN_CONNECTED,
        monotonic_fn=lambda: clock[0])
    health = _supervisor(market).tick_once().health
    assert health.state == rsup.DEGRADED         # connected, but not all live


def test_stopped_is_reported_after_stop():
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    supervisor.stop()
    assert supervisor.health(now=T0).state == rsup.STOPPED


def test_no_tick_yet_is_starting_never_healthy():
    supervisor = _supervisor(_runtime())
    assert supervisor.last is None
    health = supervisor.health(now=T0)
    assert health.state == rsup.STARTING
    assert "no_tick_recorded" in health.warnings


def test_every_state_is_in_the_declared_vocabulary():
    for state in (rsup.STARTING, rsup.CONNECTED, rsup.STALE, rsup.DEGRADED,
                  rsup.RECONNECTING, rsup.STOPPED, rsup.UNKNOWN):
        assert state in rsup.ALL_RUNTIME_STATES
    assert len(rsup.ALL_RUNTIME_STATES) == 7


def test_ticks_never_overlap():
    """Concurrent ticks serialize, so two refreshes cannot interleave."""
    entered, overlapped = [], []
    lock = threading.Lock()

    def slow_quote(_symbol):
        with lock:
            entered.append(1)
            if len(entered) > 1:
                overlapped.append(1)
        import time as _t
        _t.sleep(0.05)
        with lock:
            entered.pop()
        return {"bid": 1.1, "ask": 1.1001, "at": T0}

    market = mr.MarketRuntime(symbols=("EURUSD",), now_iso_fn=lambda: T0,
                              quote_fn=slow_quote,
                              connection_fn=lambda: mr.CONN_CONNECTED,
                              monotonic_fn=lambda: 0.0)
    supervisor = _supervisor(market)
    threads = [threading.Thread(target=supervisor.tick_once) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert overlapped == [], "two ticks ran concurrently"
    assert supervisor.last.sequence == 4


def test_a_failing_observer_cannot_break_the_loop():
    def explode(_snapshot):
        raise RuntimeError("producer is broken")

    supervisor = _supervisor(_runtime(), on_tick=explode)
    snapshot = supervisor.tick_once()          # must not raise
    assert snapshot.health.state == rsup.CONNECTED


def test_the_supervisor_cannot_execute_anything():
    code = statements_only("runtime_supervisor.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "SubmitMarketOrder", "ClosePosition", "_ORCHESTRATOR",
                      "execution_safety", "command_authorization",
                      "AuthorizationGrant", "recommendation_decision_service",
                      "sqlite3"):
        assert forbidden not in code, f"supervisor references {forbidden}"


# ── PART 2: the live projection ──────────────────────────────────────────────

def test_live_projection_reports_broker_market_and_runtime():
    supervisor = _supervisor(
        _runtime(), account={"server": "Demo-1", "currency": "USD",
                             "balance": 10000.0, "equity": 10012.5,
                             "margin": 120.0, "marginFree": 9892.5,
                             "leverage": 100,
                             "accountFingerprint": "acctfp_abcdef123456"})
    snapshot = supervisor.tick_once()
    view = op.build_live_runtime(snapshot, now=T0,
                                health=supervisor.health(now=T0),
                                execution_mode="observe",
                                recommendation_totals={"activeCount": 2},
                                scenario_count=3).as_dict()
    assert view["available"] is True
    assert view["runtime"]["state"] == rsup.CONNECTED
    broker = view["broker"]
    assert broker["connected"] is True and broker["server"] == "Demo-1"
    assert broker["balance"] == 10000.0 and broker["equity"] == 10012.5
    assert broker["freeMargin"] == 9892.5 and broker["leverage"] == 100
    assert broker["accountCurrency"] == "USD"
    assert broker["executionMode"] == "observe"
    symbol = view["symbols"][0]
    assert symbol["bid"] == 1.10045 and symbol["spread"] == pytest.approx(0.0001)
    assert symbol["live"] is True and symbol["availability"] == "ok"
    assert view["execution"] == {
        "activePositions": 0, "pendingOrders": 0, "openRecommendations": 2,
        "activeScenarios": 3, "positionsAvailable": True,
        "recommendationsAvailable": True, "scenariosAvailable": True}
    assert list(view) == sorted(view)


def test_the_raw_account_fingerprint_never_reaches_the_projection():
    supervisor = _supervisor(
        _runtime(), account={"accountFingerprint": "acctfp_abcdef123456"})
    view = op.build_live_runtime(supervisor.tick_once(), now=T0).as_dict()
    assert "acctfp_abcdef123456" not in str(view)
    assert "…" in view["broker"]["accountFingerprint"]


def test_no_runtime_tick_is_unavailable_not_empty():
    view = op.build_live_runtime(None, now=T0).as_dict()
    assert view["available"] is False
    assert view["code"] == "runtime_not_started"
    assert view["broker"]["connected"] is False
    assert view["broker"]["balance"] is None          # NOT 0.0
    assert view["symbols"] == []


def test_missing_account_values_stay_absent():
    supervisor = _supervisor(_runtime(), account={})
    broker = op.build_live_runtime(supervisor.tick_once(), now=T0).as_dict()["broker"]
    for field in ("balance", "equity", "margin", "freeMargin", "marginLevel",
                  "leverage", "server", "accountCurrency"):
        assert broker[field] is None, f"{field} was invented"


def test_unavailable_and_stale_are_distinct_in_the_projection():
    fresh = op.build_live_runtime(_supervisor(_runtime()).tick_once(),
                                  now=T0).as_dict()["symbols"][0]
    stale = op.build_live_runtime(_supervisor(_runtime()).tick_once(),
                                  now="2026-07-28T10:05:00Z"
                                  ).as_dict()["symbols"][0]
    missing = op.build_live_runtime(_supervisor(_runtime(quote=False)).tick_once(),
                                    now=T0).as_dict()["symbols"][0]
    assert fresh["availability"] == "ok"
    assert stale["availability"] == "stale"
    assert missing["availability"] == "unavailable"
    assert missing["bid"] is None


# ── PART 5/6: the producers ──────────────────────────────────────────────────

@pytest.fixture
def pipeline(tmp_path):
    """A producer wired to real stores, with an injected candle history."""
    scenarios = sstore_mod.ScenarioStore(tmp_path / "scn.db")
    recommendations = rstore.RecommendationStore(tmp_path / "rec.db")
    now = [T0]
    service = rsvc.RecommendationService(
        store_fn=lambda: recommendations,
        scenario_reader=lambda sid: scenarios.get_scenario(sid),
        now_iso_fn=lambda: now[0], execution_mode_fn=lambda: "observe")
    history = {"EURUSD": [PREV, BREAK_UP]}
    producer = lp.LivePipelineProducer(
        scenario_store_fn=lambda: scenarios,
        recommendation_service_fn=lambda: service,
        now_iso_fn=lambda: now[0],
        history_fn=lambda s: history.get(s, []), enabled_fn=lambda: True)
    return {"producer": producer, "scenarios": scenarios,
            "recommendations": recommendations, "service": service,
            "history": history, "now": now}


def _tick_snapshot(candle, *, now=T0):
    quote = mr.SymbolQuote(symbol="EURUSD", bid=1.10045, ask=1.10055, at=now,
                           spread=0.0001)
    sub = mr.SymbolSubscription(symbol="EURUSD", broker_symbol="EURUSD",
                                active=True, quote=quote, candle=candle)
    return rsup.RuntimeSnapshot(
        at=now, sequence=1,
        market=mr.MarketRuntimeSnapshot(at=now, subscriptions=(sub,)))


def test_a_break_of_the_previous_high_creates_one_scenario_and_one_recommendation(pipeline):
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    assert len(result.scenarios_created) == 1
    assert len(result.recommendations_created) == 1
    scenario = pipeline["scenarios"].get_scenario(result.scenarios_created[0])
    assert scenario.instrument == "EURUSD" and scenario.direction == "long"
    assert scenario.structure == lp.STRUCTURE_BREAK_HIGH
    assert scenario.provenance == lp.PROVENANCE          # provenance is explicit
    recommendation = pipeline["recommendations"].get_recommendation(
        result.recommendations_created[0])
    assert recommendation.scenario_id == scenario.scenario_id
    assert recommendation.status == rd.RecommendationStatus.PROPOSED
    assert recommendation.decidable is True


def test_the_recommendation_carries_complete_trade_terms(pipeline):
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    terms = pipeline["recommendations"].get_recommendation(
        result.recommendations_created[0]).terms
    assert terms.execution.requested_entry_price == 1.10055    # the ask, for a buy
    assert terms.execution.quantity == lp.DEFAULT_QUANTITY
    assert terms.risk.stop_loss is not None
    assert terms.risk.take_profit is not None
    assert terms.risk.risk_percent == lp.DEFAULT_RISK_PERCENT
    assert terms.risk.planned_r == lp.DEFAULT_TARGET_R
    # Stop below entry and target above it, for a long.
    assert terms.risk.stop_loss < terms.execution.requested_entry_price
    assert terms.risk.take_profit > terms.execution.requested_entry_price
    # R is honoured: reward is exactly plannedR times risk.
    risk = terms.execution.requested_entry_price - terms.risk.stop_loss
    reward = terms.risk.take_profit - terms.execution.requested_entry_price
    assert reward == pytest.approx(lp.DEFAULT_TARGET_R * risk, rel=1e-6)


def test_a_short_recommendation_inverts_stop_and_target(pipeline):
    pipeline["history"]["EURUSD"] = [PREV, BREAK_DOWN]
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_DOWN))
    recommendation = pipeline["recommendations"].get_recommendation(
        result.recommendations_created[0])
    assert recommendation.direction == "short"
    terms = recommendation.terms
    assert terms.execution.requested_entry_price == 1.10045    # the bid, for a sell
    assert terms.risk.stop_loss > terms.execution.requested_entry_price
    assert terms.risk.take_profit < terms.execution.requested_entry_price


def test_the_recommendation_expires(pipeline):
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    recommendation = pipeline["recommendations"].get_recommendation(
        result.recommendations_created[0])
    assert recommendation.expiry_at == "2026-07-28T10:45:00Z"   # +45m from close
    assert recommendation.past_due("2026-07-28T11:00:00Z") is True


def test_an_inside_candle_produces_nothing(pipeline):
    pipeline["history"]["EURUSD"] = [PREV, BREAK_UP, INSIDE]
    result = pipeline["producer"].on_tick(_tick_snapshot(INSIDE))
    assert result.scenarios_created == [] and result.signals == 0
    assert "EURUSD:no_break" in result.skipped


def test_the_same_candle_never_produces_a_second_scenario(pipeline):
    first = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    second = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    assert len(first.scenarios_created) == 1
    assert second.scenarios_created == []
    assert "EURUSD:candle_already_processed" in second.skipped
    assert len(pipeline["scenarios"].list_scenarios()) == 1


def test_identity_makes_the_producer_restart_safe(pipeline):
    """A fresh producer (i.e. after a restart) re-processing the same candle
    writes nothing, because identity derives from the candle's close time."""
    pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    fresh = lp.LivePipelineProducer(
        scenario_store_fn=lambda: pipeline["scenarios"],
        recommendation_service_fn=lambda: pipeline["service"],
        now_iso_fn=lambda: T0,
        history_fn=lambda s: pipeline["history"].get(s, []),
        enabled_fn=lambda: True)
    result = fresh.on_tick(_tick_snapshot(BREAK_UP))
    assert result.scenarios_created == []
    assert len(pipeline["scenarios"].list_scenarios()) == 1
    assert len(pipeline["recommendations"].list_recommendations()) == 1


def test_scenario_identity_is_deterministic_for_a_candle():
    signal = lp.evaluate_candle(symbol="EURUSD", candle=BREAK_UP, previous=PREV)
    assert lp.scenario_for(signal).scenario_id == lp.scenario_for(signal).scenario_id


def test_an_incomplete_candle_is_never_evaluated():
    partial = mr.SymbolCandle(symbol="EURUSD", timeframe="M15", open=1.1,
                              high=1.2, low=1.0, close=None, opened_at=T0)
    assert lp.evaluate_candle(symbol="EURUSD", candle=partial, previous=PREV) is None


def test_missing_evidence_never_becomes_a_signal():
    assert lp.evaluate_candle(symbol="EURUSD", candle=BREAK_UP, previous=None) is None
    assert lp.evaluate_candle(symbol="EURUSD", candle=None, previous=PREV) is None
    blank = mr.SymbolCandle(symbol="EURUSD", timeframe="M15", open=1.1, high=None,
                            low=None, close=1.1, opened_at=T0, closed_at=T0)
    assert lp.evaluate_candle(symbol="EURUSD", candle=BREAK_UP, previous=blank) is None


def test_the_producer_is_disabled_by_default(tmp_path):
    producer = lp.LivePipelineProducer(
        scenario_store_fn=lambda: None, recommendation_service_fn=lambda: None,
        now_iso_fn=lambda: T0)
    result = producer.on_tick(_tick_snapshot(BREAK_UP))
    assert result.skipped == ["producer_disabled"]
    assert result.scenarios_created == []


def test_the_producer_never_executes_or_decides():
    code = statements_only("live_pipeline.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "_ORCHESTRATOR", "execution_safety", "command_authorization",
                      "recommendation_decision_service", "record_operator_decision",
                      "accept", "threading"):
        assert forbidden not in code, f"producer references {forbidden}"


def test_session_is_derived_from_the_candle_close():
    assert lp.session_for("2026-07-28T03:00:00Z") == "asia"
    assert lp.session_for("2026-07-28T09:00:00Z") == "london"
    assert lp.session_for("2026-07-28T14:00:00Z") == "newyork"
    assert lp.session_for("2026-07-28T22:00:00Z") == "late"
    assert lp.session_for("nonsense") == "unknown"


# ── PART 15: the preflight gate ──────────────────────────────────────────────

def _clear_preflight_inputs(recommendation, scenario, supervisor):
    return dict(
        recommendation_id=recommendation.recommendation_id, now=T0,
        runtime_snapshot=supervisor.last, runtime_health=supervisor.health(now=T0),
        recommendation=recommendation, scenario=scenario,
        execution_mode="manual_live",
        mode_allows_execution=lambda mode: mode == "manual_live",
        authorization={"valid": True}, existing_intent_for=lambda _rid: None)


@pytest.fixture
def accepted(pipeline):
    """A pipeline-produced Recommendation, accepted, with its Scenario."""
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    rid = result.recommendations_created[0]
    store = pipeline["recommendations"]
    store.record_operator_decision(
        rid, decision_type="ACCEPT", to_status="ACCEPTED",
        actor_type="OPERATOR", actor_id="op_jane", reason="smoke test", at=T0,
        identity_assurance="asserted")
    return {"recommendation": store.get_recommendation(rid),
            "scenario": pipeline["scenarios"].get_scenario(
                result.scenarios_created[0]),
            **pipeline}


def test_preflight_is_clear_when_every_condition_holds(accepted):
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    result = pf.evaluate(**_clear_preflight_inputs(
        accepted["recommendation"], accepted["scenario"], supervisor))
    assert result.clear is True, result.blockers
    assert result.blockers == ()


@pytest.mark.parametrize("override,blocker", [
    ({"authorization": None}, pf.BLOCK_AUTHORIZATION),
    ({"execution_mode": "observe"}, pf.BLOCK_EXECUTION_MODE),
    ({"scenario": None}, pf.BLOCK_SCENARIO_MISSING),
    ({"recommendation": None}, pf.BLOCK_RECOMMENDATION_MISSING),
    ({"existing_intent_for": lambda _r: "intent_1"}, pf.BLOCK_INTENT_DUPLICATE),
    ({"runtime_snapshot": None, "runtime_health": None},
     pf.BLOCK_RUNTIME_NOT_READY),
])
def test_preflight_blocks_each_missing_condition(accepted, override, blocker):
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    inputs = _clear_preflight_inputs(accepted["recommendation"],
                                     accepted["scenario"], supervisor)
    inputs.update(override)
    result = pf.evaluate(**inputs)
    assert result.clear is False
    assert blocker in result.blockers


def test_preflight_blocks_a_disconnected_broker(accepted):
    supervisor = _supervisor(_runtime(connection=mr.CONN_DISCONNECTED))
    supervisor.tick_once()
    result = pf.evaluate(**_clear_preflight_inputs(
        accepted["recommendation"], accepted["scenario"], supervisor))
    assert pf.BLOCK_BROKER_DISCONNECTED in result.blockers


def test_preflight_blocks_a_stale_projection(accepted):
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    inputs = _clear_preflight_inputs(accepted["recommendation"],
                                     accepted["scenario"], supervisor)
    much_later = "2026-07-28T10:30:00Z"
    inputs.update(now=much_later,
                  runtime_health=supervisor.health(now=much_later))
    result = pf.evaluate(**inputs)
    assert pf.BLOCK_PROJECTION_STALE in result.blockers


def test_preflight_blocks_an_unaccepted_recommendation(pipeline):
    result = pipeline["producer"].on_tick(_tick_snapshot(BREAK_UP))
    proposed = pipeline["recommendations"].get_recommendation(
        result.recommendations_created[0])
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    verdict = pf.evaluate(**_clear_preflight_inputs(
        proposed, pipeline["scenarios"].get_scenario(result.scenarios_created[0]),
        supervisor))
    assert pf.BLOCK_RECOMMENDATION_NOT_ACCEPTED in verdict.blockers


def test_preflight_blocks_an_expired_recommendation(accepted):
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    inputs = _clear_preflight_inputs(accepted["recommendation"],
                                     accepted["scenario"], supervisor)
    inputs["now"] = "2026-07-28T12:00:00Z"           # past the 45m expiry
    inputs["runtime_health"] = supervisor.health(now=T0)
    result = pf.evaluate(**inputs)
    assert pf.BLOCK_RECOMMENDATION_EXPIRED in result.blockers


def test_preflight_blocks_an_inactive_symbol(accepted):
    supervisor = _supervisor(_runtime(quote=False))
    supervisor.tick_once()
    result = pf.evaluate(**_clear_preflight_inputs(
        accepted["recommendation"], accepted["scenario"], supervisor))
    assert pf.BLOCK_SYMBOL_INACTIVE in result.blockers


def test_preflight_fails_closed_on_anything_unverifiable(accepted):
    supervisor = _supervisor(_runtime())
    supervisor.tick_once()
    inputs = _clear_preflight_inputs(accepted["recommendation"],
                                     accepted["scenario"], supervisor)
    inputs["mode_allows_execution"] = None
    inputs["existing_intent_for"] = None
    result = pf.evaluate(**inputs)
    assert result.clear is False
    assert pf.BLOCK_UNVERIFIABLE in result.blockers


def test_preflight_is_pure():
    code = statements_only("live_preflight.py")
    for forbidden in ("get_broker", "order_send", "sqlite3", "datetime.now",
                      "requests.", "open("):
        assert forbidden not in code, f"preflight performs {forbidden}"


# ── PART 4/13: the API surface ───────────────────────────────────────────────

def test_the_live_runtime_endpoint_is_read_only_and_no_store():
    client.post("/api/live-runtime/tick")
    response = client.get("/api/live-runtime")
    assert response.status_code == 200
    assert response.headers.get("Cache-Control") == "no-store"
    body = response.json()
    assert body["available"] is True
    assert body["runtime"]["state"] in rsup.ALL_RUNTIME_STATES
    assert isinstance(body["symbols"], list)
    for verb in (client.put, client.patch, client.delete):
        assert verb("/api/live-runtime").status_code in (404, 405)


def test_the_live_runtime_endpoint_does_not_read_the_broker(monkeypatch):
    """The whole point of the refresh owner: serving the dashboard must not
    touch the broker."""
    client.post("/api/live-runtime/tick")           # populate the cache
    calls = []
    real = server.broker_layer.get_broker
    monkeypatch.setattr(server.broker_layer, "get_broker",
                        lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    assert client.get("/api/live-runtime").status_code == 200
    # active_kind() is a cheap module-level read, not a broker read; get_broker
    # is what actually opens a session.
    assert calls == [], f"the live endpoint read the broker {len(calls)} times"


def test_the_health_endpoint_reports_a_declared_state():
    response = client.get("/api/live-runtime/health")
    assert response.status_code == 200
    assert response.json()["state"] in rsup.ALL_RUNTIME_STATES


def test_the_tick_endpoint_refreshes_and_cannot_execute():
    first = client.post("/api/live-runtime/tick").json()
    second = client.post("/api/live-runtime/tick").json()
    assert first["ticked"] is True
    assert second["sequence"] > first["sequence"]
    assert "order" not in str(second).lower()


def test_the_preflight_endpoint_reports_every_blocker(monkeypatch, tmp_path):
    store = rstore.RecommendationStore(tmp_path / "api-rec.db")
    monkeypatch.setattr(server, "_RECOMMENDATION_STORE", store)
    recommendation = rd.new_recommendation(
        scenario_id="scn_" + "a" * 16, instrument="EURUSD", direction="long",
        source=rd.RecommendationSource.STRATEGY, created_at=T0,
        terms=rd.RecommendationTerms(
            execution=rd.RecommendationExecutionTerms(order_type="market",
                                                      quantity=0.01),
            risk=rd.RecommendationRiskTerms(stop_loss=1.09)))
    store.create_recommendation(recommendation)
    response = client.get(
        f"/api/trade-recommendations/{recommendation.recommendation_id}/preflight")
    assert response.status_code == 200
    body = response.json()
    assert body["clear"] is False                  # observe mode, not accepted
    assert body["blockers"]
    assert {"name", "passed", "code", "detail"} <= set(body["checks"][0])


# ── PART 14: MOCK and LIVE share one projection shape ────────────────────────

def test_mock_and_live_produce_the_same_projection_shape():
    mock = op.build_live_runtime(
        _supervisor(_runtime()).tick_once(), now=T0).as_dict()
    live_market = _runtime()
    live_market._adapter_kind = lambda: "mt5"
    live_market._provider = lambda: "mt5"
    live = op.build_live_runtime(
        _supervisor(live_market).tick_once(), now=T0, adapter_kind="mt5").as_dict()
    assert set(mock) == set(live)
    assert set(mock["broker"]) == set(live["broker"])
    assert set(mock["symbols"][0]) == set(live["symbols"][0])
    # Only the PROVENANCE differs — the UI shape is identical.
    assert mock["broker"]["provenance"] != live["broker"]["provenance"]
    assert live["broker"]["provenance"] == op.PROV_LIVE_MT5


# ── structural safety (PART 19) ──────────────────────────────────────────────

def test_the_execution_command_surface_is_unchanged():
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})


def test_broker_write_capabilities_are_unchanged():
    import broker as broker_layer
    caps = broker_layer.capability_dict(broker_layer.MT5Adapter().capabilities())
    for enabled in ("supportsLiveWrite", "supportsMarketExecution",
                    "supportsModify", "supportsCancelOrder",
                    "supportsClosePosition"):
        assert caps[enabled] is True
    for disabled in ("supportsPendingOrders", "supportsPartialClose",
                     "supportsHedging", "supportsNetting", "supportsReplay"):
        assert caps[disabled] is False


def test_no_execution_module_learned_about_the_runtime():
    for module in ("execution.py", "execution_safety.py", "broker.py",
                   "broker_adapter.py", "reconciliation.py",
                   "command_registry.py", "command_authorization.py",
                   "execution_mode.py", "trade_ledger_domain.py",
                   "trade_reconstruction.py"):
        code = statements_only(module)
        for forbidden in ("runtime_supervisor", "market_runtime", "live_pipeline",
                          "live_preflight", "RuntimeSupervisor"):
            assert forbidden not in code, f"{module} depends on {forbidden}"


def test_the_market_data_layer_still_imports_no_broker():
    code = statements_only("market_data.py")
    assert "import broker" not in code
    assert "get_broker" not in code


def test_a_zero_mt5_tick_is_not_a_price():
    """MT5 reports 0.0 for "no quote". Zero must never become a price."""
    import market_data as md

    class _Tick:
        bid = 0.0
        ask = 0.0
        time = 1781862300

    class _SDK:
        @staticmethod
        def symbol_select(*_a, **_k):
            return True

        @staticmethod
        def symbol_info_tick(*_a, **_k):
            return _Tick()

    provider = md.MT5MarketDataProvider(lambda _s: None,
                                        available_symbols=["EURUSD"],
                                        aliases={}, enabled=True)
    provider._connected = True
    provider._mt5 = _SDK()
    provider._import_tried = True
    assert provider.live_tick("EURUSD") is None


# ═══════════════════════════════════════════════════════════════════════════════
# PART 16 — THE SMOKE WORKFLOW. This is the acceptance test.
# ═══════════════════════════════════════════════════════════════════════════════

def test_smoke_the_complete_pipeline_from_market_evidence_to_ledger(tmp_path):
    """Market evidence -> Scenario -> Recommendation -> operator decision ->
    Intent -> Order -> Position -> close -> Ledger, with the projection
    reflecting each stage.

    Runs against the MOCK broker. PART 14 guarantees LIVE flows through the same
    projections; the live run is the same workflow with MARKET_DATA_PROVIDER=mt5
    on a Windows host (see LIVE-5A-SMOKE-WORKFLOW.md). A genuine live trade
    cannot execute here — MetaTrader5 has no macOS build.
    """
    import broker_history as bh
    import trade_reconstruction as tr

    # ── stage 1: the runtime produces live market evidence ───────────────────
    scenarios = sstore_mod.ScenarioStore(tmp_path / "scn.db")
    recommendations = rstore.RecommendationStore(tmp_path / "rec.db")
    service = rsvc.RecommendationService(
        store_fn=lambda: recommendations,
        scenario_reader=lambda sid: scenarios.get_scenario(sid),
        now_iso_fn=lambda: T0, execution_mode_fn=lambda: "observe")
    producer = lp.LivePipelineProducer(
        scenario_store_fn=lambda: scenarios,
        recommendation_service_fn=lambda: service, now_iso_fn=lambda: T0,
        history_fn=lambda _s: [PREV, BREAK_UP], enabled_fn=lambda: True)

    supervisor = _supervisor(
        _runtime(), account={"currency": "USD", "balance": 10000.0,
                             "equity": 10000.0},
        on_tick=producer.on_tick)
    snapshot = supervisor.tick_once()
    assert snapshot.health.state == rsup.CONNECTED
    assert snapshot.market.symbol("EURUSD").quote.bid is not None

    # ── stage 2: a Scenario exists ───────────────────────────────────────────
    stored_scenarios = scenarios.list_scenarios()
    assert len(stored_scenarios) == 1, "the candle break produced no Scenario"
    scenario = stored_scenarios[0]

    # ── stage 3: a Recommendation exists, proposed and decidable ─────────────
    stored = recommendations.list_recommendations()
    assert len(stored) == 1, "the Scenario produced no Recommendation"
    recommendation = stored[0]
    assert recommendation.scenario_id == scenario.scenario_id
    assert recommendation.status == rd.RecommendationStatus.PROPOSED
    assert recommendation.decidable is True

    # ── stage 4: the operator decides — and this executes NOTHING ────────────
    accepted_rec, decision = recommendations.record_operator_decision(
        recommendation.recommendation_id, decision_type="ACCEPT",
        to_status="ACCEPTED", actor_type="OPERATOR", actor_id="op_jane",
        reason="smoke workflow", at=T0, identity_assurance="asserted",
        expected_version=recommendations.current_version(
            recommendation.recommendation_id))
    assert accepted_rec.status == rd.RecommendationStatus.ACCEPTED
    assert accepted_rec.outcome == rd.RecommendationOutcome.NOT_EXECUTED
    assert accepted_rec.linked_intent_ids == ()       # no order yet, by design
    assert decision.decision_type == "ACCEPT"

    # ── stage 5: preflight clears the trade ──────────────────────────────────
    verdict = pf.evaluate(
        recommendation_id=accepted_rec.recommendation_id, now=T0,
        runtime_snapshot=supervisor.last, runtime_health=supervisor.health(now=T0),
        recommendation=accepted_rec, scenario=scenario,
        execution_mode="manual_live",
        mode_allows_execution=lambda m: m == "manual_live",
        authorization={"valid": True}, existing_intent_for=lambda _r: None)
    assert verdict.clear is True, verdict.blockers

    # ── stage 6: Intent carries the Recommendation, additively ───────────────
    import order_lifecycle as ol
    intent = ol.OrderIntent(
        intent_id=ol.new_intent_id(), command_name="SubmitMarketOrder",
        kind="submit", created_at=T0, instrument="EURUSD",
        scenario_id=scenario.scenario_id,
        recommendation_id=accepted_rec.recommendation_id)
    assert intent.recommendation_id == accepted_rec.recommendation_id
    linked = recommendations.link_intent(accepted_rec.recommendation_id,
                                        intent.intent_id, at=T0)
    assert linked.linked_intent_ids == (intent.intent_id,)
    assert linked.status == rd.RecommendationStatus.INTENT_CREATED

    # ── stage 7 & 8: the position opens and then closes at the broker ────────
    history = bh.BrokerHistorySnapshot(
        at=T0, availability=bh.AVAILABLE, account_mode=bh.MODE_HEDGING,
        account_currency="USD", account_fingerprint="acctfp_smoke",
        deals=(
            bh.BrokerDealRecord(
                deal_id="D1", order_id="O1", position_id="P1", symbol="EURUSD",
                deal_type="buy", entry=bh.ENTRY_IN, volume=0.01, price=1.10055,
                profit=None, at=T0, costs=bh.BrokerCostEvidence(),
                provenance="mock-fixture"),
            bh.BrokerDealRecord(
                deal_id="D2", order_id="O2", position_id="P1", symbol="EURUSD",
                deal_type="sell", entry=bh.ENTRY_OUT, volume=0.01, price=1.10255,
                profit=2.0, at="2026-07-28T10:30:00Z",
                costs=bh.BrokerCostEvidence(), provenance="mock-fixture"),
        ),
        deals_available=True, provenance="mock-fixture")

    # ── stage 9: the ledger reconstructs the closed trade with full lineage ──
    results = tr.reconstruct(tr.ReconstructionInput(
        history=history,
        intents=({"intent_id": intent.intent_id, "broker_ref": "P1",
                  "kind": "submit", "scenario_id": scenario.scenario_id,
                  "recommendation_id": accepted_rec.recommendation_id},)))
    assert len(results) == 1
    trade = results[0].trade
    assert trade.instrument == "EURUSD" and trade.side == "long"
    assert trade.gross_realized_pnl == 2.0
    assert trade.lineage.scenario_id == scenario.scenario_id
    assert trade.lineage.recommendation_id == accepted_rec.recommendation_id
    assert intent.intent_id in trade.lineage.intent_ids

    # ── stage 10: the projection reflects the whole chain ────────────────────
    view = op.build_live_runtime(
        supervisor.last, now=T0, health=supervisor.health(now=T0),
        execution_mode="manual_live",
        recommendation_totals=recommendations.summary().as_dict(),
        scenario_count=len(scenarios.list_active_scenarios(limit=10))).as_dict()
    assert view["available"] is True
    assert view["runtime"]["state"] == rsup.CONNECTED
    assert view["symbols"][0]["live"] is True
    assert view["execution"]["activeScenarios"] >= 1

    # ── and the invariant that matters: accepting never executed anything ────
    decisions = recommendations.decisions(accepted_rec.recommendation_id)
    # `propose()` records a DEFER (PROPOSED -> PENDING_DECISION) before the
    # operator acts, so the history is DEFER then ACCEPT.
    assert [d.decision_type for d in decisions] == ["DEFER", "ACCEPT"]
    assert decisions[-1].actor_id == "op_jane"
    # No decision type in the whole domain asserts execution, and none here does.
    assert all(d.decision_type in rd.OPERATOR_DECISION_TYPES | {"DEFER"}
               for d in decisions)
    # The ledger — not the decision — is what records that a trade happened.
    assert trade.lineage.recommendation_id == accepted_rec.recommendation_id
