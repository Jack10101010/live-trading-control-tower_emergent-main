"""EXTERNAL smoke tests for the Control Tower API.

Every test here talks to a DEPLOYED backend over real HTTP at
`REACT_APP_BACKEND_URL`, exercising ingress end-to-end against the frozen
world.v1.json fixture. It is therefore an environment-dependent smoke suite, not a
unit suite.

WHY THIS FILE SKIPS INSTEAD OF ASSERTING AT IMPORT
    It previously ran `assert BASE_URL` at module scope. With the variable unset —
    the normal local and CI case — that assertion fired during COLLECTION, which
    aborted the whole run (`pytest -n 0` reported "Interrupted: 1 error during
    collection" and executed zero tests; `-n 2` reported two worker errors and a
    non-zero exit). The failure was silent in practice because the headline count
    still read "N passed". A module-level `skipif` reports the situation honestly
    and lets the rest of the suite run.

    The safety-critical assertions this file used to carry — MT5 never connects, and
    the fixture command route cannot reach a real broker — did NOT depend on a
    deployed backend. They now live in `test_safety_invariants.py`, where they run
    unconditionally against the in-process app.
"""
import os
import pytest
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL")
if not BASE_URL:
    # Fall back to reading the frontend .env file when pytest is invoked
    # outside the shell where the var is exported.
    env_path = "/app/frontend/.env"
    if os.path.exists(env_path):
        with open(env_path) as fh:
            for line in fh:
                if line.startswith("REACT_APP_BACKEND_URL="):
                    BASE_URL = line.split("=", 1)[1].strip()
                    break
BASE_URL = (BASE_URL or "").rstrip("/")

#: Skip the whole module — clearly, and at collection time — when no deployed
#: backend is configured. Never abort collection for the rest of the suite.
pytestmark = pytest.mark.skipif(
    not BASE_URL,
    reason="REACT_APP_BACKEND_URL is not set — external smoke tests need a deployed "
           "backend. Local unit and safety coverage runs unconditionally; the "
           "MT5-isolation and fixture-command guards live in test_safety_invariants.py.",
)


@pytest.fixture(scope="session")
def api():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Meta / Health ----------

class TestMeta:
    def test_root(self, api):
        r = api.get(f"{BASE_URL}/api/")
        assert r.status_code == 200
        d = r.json()
        assert d.get("service") == "Control Tower API"
        assert d.get("fixtureVersion") == "world.v1"

    def test_health(self, api):
        r = api.get(f"{BASE_URL}/api/health")
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ok"
        assert "asOf" in d and d["asOf"]


# ---------- World fixture ----------

class TestWorld:
    def test_world_contract(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-world")
        assert r.status_code == 200
        d = r.json()
        assert d["meta"]["fixtureVersion"] == "world.v1"
        # Sanity: top-level buckets exist
        for key in ("deployments", "brokers", "accounts", "packages",
                    "liveTrades", "ghostTrades", "blockedIntents",
                    "edgeMonitor", "systemConfidence", "recommendations",
                    "events", "decisionChains"):
            assert key in d, f"missing top-level key: {key}"


# ---------- Fleet ----------

class TestFleet:
    def test_fleet_shape(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-fleet")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["deployments"], list)
        assert isinstance(d["brokers"], list)
        assert isinstance(d["accounts"], list)
        # Spec: 3 deployments, 2 brokers, 2 accounts
        assert len(d["deployments"]) == 3, f"expected 3 deployments, got {len(d['deployments'])}"
        assert len(d["brokers"]) == 2, f"expected 2 brokers, got {len(d['brokers'])}"
        assert len(d["accounts"]) == 2, f"expected 2 accounts, got {len(d['accounts'])}"

    def test_deployments_list(self, api):
        r = api.get(f"{BASE_URL}/api/deployments")
        assert r.status_code == 200
        arr = r.json()
        assert isinstance(arr, list) and len(arr) == 3
        for dep in arr:
            assert "deploymentId" in dep

    def test_deployment_by_id_ok(self, api):
        # Use the first real deployment id from the list
        arr = api.get(f"{BASE_URL}/api/deployments").json()
        dep_id = arr[0]["deploymentId"]
        r = api.get(f"{BASE_URL}/api/deployments/{dep_id}")
        assert r.status_code == 200
        assert r.json()["deploymentId"] == dep_id

    def test_deployment_by_id_404(self, api):
        r = api.get(f"{BASE_URL}/api/deployments/does-not-exist-xyz")
        assert r.status_code == 404


# ---------- Policy Matrix ----------

class TestPolicy:
    def test_matrix_eurusd(self, api):
        r = api.get(f"{BASE_URL}/api/policy/EURUSD/matrix")
        assert r.status_code == 200
        d = r.json()
        # Matrix should have a `cells` object per contract
        assert "cells" in d, f"missing cells key. keys={list(d.keys())}"
        assert isinstance(d["cells"], dict)
        assert len(d["cells"]) >= 1  # representative seed cells

    def test_matrix_unknown_instrument(self, api):
        r = api.get(f"{BASE_URL}/api/policy/ZZZZZZ/matrix")
        assert r.status_code == 404


# ---------- Trades ----------

class TestTrades:
    def test_trades_eurusd(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-trades", params={"pair": "EURUSD"})
        assert r.status_code == 200
        d = r.json()
        assert set(d.keys()) >= {"live", "ghost", "blocked"}
        # Spec: 2 live, 3 ghost, 5 blocked for EURUSD
        assert len(d["live"]) == 2, f"expected 2 live, got {len(d['live'])}"
        assert len(d["ghost"]) == 3, f"expected 3 ghost, got {len(d['ghost'])}"
        assert len(d["blocked"]) == 5, f"expected 5 blocked, got {len(d['blocked'])}"

    def test_trades_no_filter(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-trades")
        assert r.status_code == 200
        d = r.json()
        assert isinstance(d["live"], list)
        assert isinstance(d["ghost"], list)
        assert isinstance(d["blocked"], list)


# ---------- Edge monitor & confidence ----------

class TestSignals:
    def test_edge_monitor(self, api):
        r = api.get(f"{BASE_URL}/api/edge-monitor")
        assert r.status_code == 200
        d = r.json()
        # Must contain something about EURUSD with expectancyR
        blob = str(d)
        assert "EURUSD" in blob
        assert "expectancyR" in blob

    def test_broker_health(self, api):
        r = api.get(f"{BASE_URL}/api/broker-health")
        assert r.status_code == 200
        d = r.json()
        assert "brokers" in d and "health" in d
        assert isinstance(d["brokers"], list)
        assert isinstance(d["health"], list)

    def test_system_confidence(self, api):
        r = api.get(f"{BASE_URL}/api/system-confidence")
        assert r.status_code == 200
        d = r.json()
        # Spec says gauge shows 82 CAUTION on the FE
        assert isinstance(d, dict)
        # Must at least contain a score-like field or a state
        assert d, "empty system-confidence payload"

    def test_recommendations(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-recommendations")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_events(self, api):
        r = api.get(f"{BASE_URL}/api/events")
        assert r.status_code == 200
        assert isinstance(r.json(), list)

    def test_feature_flags(self, api):
        """M-FLAGS-1: capabilities report STATE and SOURCE, never a bare bool.

        A boolean cannot distinguish "switched off" from "never built" from
        "should exist but nothing is reporting", and the old dict asserted
        availability for surfaces that report their own absence."""
        r = api.get(f"{BASE_URL}/api/feature-flags")
        assert r.status_code == 200
        d = r.json()
        assert d["schemaVersion"] == 1
        caps = d["capabilities"]
        VALID = {"available", "disabled", "unsupported", "unavailable"}
        for k in ("ghostTrading", "replay", "edgeMonitor", "commandPalette"):
            assert k in caps, k
            assert not isinstance(caps[k], bool), f"{k} is still a bare boolean"
            assert caps[k]["state"] in VALID, caps[k]
            assert caps[k]["source"] in {"runtime", "configuration", "none"}
        # A capability whose domain reports its own absence must not claim to be
        # available — that disagreement was the defect this milestone removed.
        for retired in ("edgeMonitor", "versionHistory", "packageComparison"):
            assert caps[retired]["state"] == "unavailable", retired

    def test_active_package(self, api):
        r = api.get(f"{BASE_URL}/api/dev/fixture-packages/active")
        assert r.status_code == 200
        d = r.json()
        assert d.get("status") == "active"
        assert "version" in d


# ---------- Decisions ----------

class TestDecisions:
    def test_decision_404(self, api):
        r = api.get(f"{BASE_URL}/api/decisions/no-such-id-xyz")
        assert r.status_code == 404


# ---------- Broker sync & reconciliation (Phase 7) ----------

class TestBrokerSync:
    """Read-only reconciliation surface. Each test resets the runtime (restores
    the open position) and clears faults first, so cases are order-independent."""

    def _reset(self, api):
        api.post(f"{BASE_URL}/api/runtime/reset")
        api.post(f"{BASE_URL}/api/broker/faults", json={"clear": True})

    def test_sync_shape_and_healthy(self, api):
        self._reset(api)
        r = api.post(f"{BASE_URL}/api/broker/sync")
        assert r.status_code == 200
        d = r.json()
        # snapshot + reconciliation structure
        assert "brokerSnapshot" in d and "runtimeSnapshot" in d and "reconciliation" in d
        for key in ("deployments", "positions", "orders", "accounts", "activePackage",
                    "runtimeHealth", "connectionState", "eventSeq", "timestamp"):
            assert key in d["runtimeSnapshot"], f"runtimeSnapshot missing {key}"
        summ = d["reconciliation"]["summary"]
        assert d["status"] == "ok"
        assert summ["healthy"] is True and summ["warnings"] == 0 and summ["errors"] == 0
        # baseline: broker mirrors runtime
        assert summ["brokerPositions"] == summ["runtimePositions"]

    def test_reconciliation_endpoint(self, api):
        self._reset(api)
        api.post(f"{BASE_URL}/api/broker/sync")
        r = api.get(f"{BASE_URL}/api/broker/reconciliation")
        assert r.status_code == 200
        d = r.json()
        assert "reconciliation" in d and "summary" in d["reconciliation"]

    def test_faults_get_is_dict(self, api):
        r = api.get(f"{BASE_URL}/api/broker/faults")
        assert r.status_code == 200
        assert isinstance(r.json(), dict)

    def test_warning_reconciliation(self, api):
        # connectionDegraded always yields a WARNING regardless of open-position
        # count → race-safe under the shared parallel backend.
        self._reset(api)
        api.post(f"{BASE_URL}/api/broker/faults", json={"connectionDegraded": True})
        d = api.post(f"{BASE_URL}/api/broker/sync").json()
        assert d["status"] == "warning"
        assert d["reconciliation"]["summary"]["warnings"] >= 1
        assert "connection_degraded" in {f["type"] for f in d["reconciliation"]["findings"]}
        api.post(f"{BASE_URL}/api/broker/faults", json={"clear": True})

    def test_error_reconciliation(self, api):
        # unexpectedPosition always injects a broker-only position → ERROR,
        # independent of runtime open-position count (race-safe).
        self._reset(api)
        api.post(f"{BASE_URL}/api/broker/faults", json={"unexpectedPosition": True})
        d = api.post(f"{BASE_URL}/api/broker/sync").json()
        assert d["status"] == "error"
        assert d["reconciliation"]["summary"]["errors"] >= 1
        assert "unexpected_position" in {f["type"] for f in d["reconciliation"]["findings"]}
        api.post(f"{BASE_URL}/api/broker/faults", json={"clear": True})

    def test_fault_injection_does_not_mutate_runtime(self, api):
        # Runtime stays authoritative: positionDisappears removes from the BROKER
        # snapshot only, never from runtime → runtime count >= broker count
        # (race-safe: holds whether or not a concurrent test has closed the trade).
        self._reset(api)
        api.post(f"{BASE_URL}/api/broker/faults", json={"positionDisappears": True})
        d = api.post(f"{BASE_URL}/api/broker/sync").json()
        assert d["runtimeSnapshot"]["counts"]["positions"] >= d["brokerSnapshot"]["counts"]["positions"]
        api.post(f"{BASE_URL}/api/broker/faults", json={"clear": True})

    def test_clear_restores_healthy(self, api):
        self._reset(api)
        d = api.post(f"{BASE_URL}/api/broker/sync").json()
        assert d["status"] == "ok" and len(d["reconciliation"]["findings"]) == 0


# ---------- Execution Orchestrator (Phase 8) ----------

DEP = "dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ"
TRADE = "tr_01J8ZC4H2N8M6K1E3R5T7V9W0XZ"          # fixture: managing (open)
CLOSED_TRADE = "tr_01J8ZC7K4Q0T3W6Z9C2F5J8L1N4"   # fixture: closed


class TestExecutionOrchestrator:
    def _reset(self, api):
        api.post(f"{BASE_URL}/api/runtime/reset")
        api.post(f"{BASE_URL}/api/execution/dry-run", json={"enabled": False})

    def test_command_runs_through_orchestrator(self, api):
        self._reset(api)
        r = api.post(f"{BASE_URL}/api/commands/PauseDeployment", json={"deploymentId": DEP})
        assert r.status_code == 200
        d = r.json()
        assert "execution" in d
        ex = d["execution"]
        assert ex["status"] == "completed"
        # full observable pipeline
        stages = [s["stage"] for s in ex["stages"]]
        for s in ("received", "validated", "policy_checked", "broker_dispatch",
                  "broker_result", "runtime_update", "audit_event", "completed"):
            assert s in stages, f"missing stage {s}"
        for k in ("validationMs", "policyMs", "brokerMs", "totalMs"):
            assert k in ex["timeline"]

    def test_dry_run_generates_event_but_does_not_persist(self, api):
        self._reset(api)
        r = api.post(f"{BASE_URL}/api/commands/CloseTrade", json={"tradeId": TRADE, "dryRun": True})
        assert r.status_code == 200
        d = r.json()
        assert d["dryRun"] is True
        assert d["events"][0]["after"]["state"] == "closed"  # simulated
        # ...but the runtime overlay is unchanged (still open)
        live = api.get(f"{BASE_URL}/api/dev/fixture-trades").json()["live"]
        t = next(x for x in live if x["tradeId"] == TRADE)
        assert t["state"] == "managing"

    def test_validation_rejects_unknown_trade(self, api):
        self._reset(api)
        r = api.post(f"{BASE_URL}/api/commands/CloseTrade", json={"tradeId": "tr_nope"})
        assert r.status_code == 422
        assert r.json()["detail"]["stage"] == "validated"

    def test_policy_denies_closing_closed_trade(self, api):
        # Uses the fixture's already-closed trade → policy denies without mutating
        # shared state or depending on reset ordering (race-safe).
        self._reset(api)
        r = api.post(f"{BASE_URL}/api/commands/CloseTrade", json={"tradeId": CLOSED_TRADE})
        assert r.status_code == 422
        assert r.json()["detail"]["stage"] == "policy_checked"

    def test_dry_run_toggle_and_health(self, api):
        assert api.post(f"{BASE_URL}/api/execution/dry-run", json={"enabled": True}).json()["enabled"] is True
        h = api.get(f"{BASE_URL}/api/runtime/health").json()["orchestrator"]
        for k in ("orchestratorHealthy", "averageExecutionMs", "brokerLatencyMs",
                  "dryRunEnabled", "validationFailures", "policyFailures", "lastExecution"):
            assert k in h
        assert h["dryRunEnabled"] is True
        api.post(f"{BASE_URL}/api/execution/dry-run", json={"enabled": False})


# ---------- Strategy Engine (Phase 9) — read-only, executes nothing ----------

class TestStrategyEngine:
    def test_registry(self, api):
        r = api.get(f"{BASE_URL}/api/strategy/registry")
        assert r.status_code == 200
        ids = {s["strategyId"] for s in r.json()}
        assert "policy_alignment" in ids

    def test_evaluate_produces_decisions(self, api):
        r = api.post(f"{BASE_URL}/api/strategy/evaluate")
        assert r.status_code == 200
        d = r.json()
        assert d["decisions"], "expected at least one decision"
        dec = d["decisions"][0]
        for k in ("decisionId", "deploymentId", "pair", "policyCell", "marketState",
                  "score", "confidence", "signal", "reasoning", "policyFailures",
                  "recommendations", "candidateCommands", "evaluatedAt"):
            assert k in dec, f"decision missing {k}"
        # observable pipeline present
        assert d["report"]["pipeline"][0] == "market_snapshot"
        assert "execution_request" in d["report"]["pipeline"]

    def test_engine_never_executes(self, api):
        # Structural invariant (race-safe): the evaluation reports zero executions
        # and returns no events — the Strategy Engine only proposes candidates.
        d = api.post(f"{BASE_URL}/api/strategy/evaluate").json()
        assert d["report"]["executed"] == 0
        assert "events" not in d
        # candidate commands, when present, are proposals only
        for dec in d["decisions"]:
            for c in dec["candidateCommands"]:
                assert "name" in c and "rationale" in c

    def test_decisions_endpoint(self, api):
        api.post(f"{BASE_URL}/api/strategy/evaluate")
        r = api.get(f"{BASE_URL}/api/strategy/decisions")
        assert r.status_code == 200
        assert "decisions" in r.json() and "report" in r.json()

    def test_strategy_health(self, api):
        api.post(f"{BASE_URL}/api/strategy/evaluate")
        h = api.get(f"{BASE_URL}/api/runtime/health").json()["strategy"]
        for k in ("strategyHealthy", "evaluations", "averageEvaluationMs",
                  "lastEvaluationMs", "lastEvaluationAt", "lastStrategy"):
            assert k in h
        assert h["strategyHealthy"] is True


# ---------- Scheduler (Phase 10) — decides WHEN; invokes Strategy Engine only ----------

class TestScheduler:
    def test_status(self, api):
        r = api.get(f"{BASE_URL}/api/scheduler/status")
        assert r.status_code == 200
        d = r.json()
        assert d["triggerTypes"] == ["manual", "interval", "market_event", "replay_tick", "startup"]
        assert d["activeTriggers"] == ["manual", "interval"]
        assert any(s["trigger"] == "interval" for s in d["schedules"])

    def test_manual_trigger_runs_job(self, api):
        r = api.post(f"{BASE_URL}/api/scheduler/trigger")
        assert r.status_code == 200
        d = r.json()
        assert d["ran"] is True
        job = d["scheduler"]["lastExecution"]
        for k in ("id", "strategy", "deployment", "trigger", "scheduledAt", "startedAt",
                  "finishedAt", "status", "durationMs", "result"):
            assert k in job, f"job missing {k}"
        assert job["trigger"] == "manual" and job["status"] == "completed"

    def test_scheduler_never_executes(self, api):
        # Structural invariant (race-safe): the scheduler only invokes the read-only
        # Strategy Engine → every job reports executed == 0.
        d = api.post(f"{BASE_URL}/api/scheduler/trigger").json()
        assert d["scheduler"]["lastExecution"]["result"]["executed"] == 0
        assert d["strategy"]["report"]["executed"] == 0

    def test_queue_and_history(self, api):
        api.post(f"{BASE_URL}/api/scheduler/trigger")
        assert isinstance(api.get(f"{BASE_URL}/api/scheduler/queue").json(), list)
        hist = api.get(f"{BASE_URL}/api/scheduler/history").json()
        assert isinstance(hist, list) and hist
        assert hist[0]["trigger"] in ("manual", "interval")

    def test_tick_fires_interval(self, api):
        r = api.post(f"{BASE_URL}/api/scheduler/tick")
        assert r.status_code == 200
        assert "scheduler" in r.json()

    def test_scheduler_health(self, api):
        api.post(f"{BASE_URL}/api/scheduler/trigger")
        h = api.get(f"{BASE_URL}/api/runtime/health").json()["scheduler"]
        for k in ("schedulerHealthy", "queueDepth", "running", "completed",
                  "failed", "averageDurationMs", "lastExecution"):
            assert k in h
        assert h["schedulerHealthy"] is True


# ---------- Market Data Engine (Phase 11) ----------

class TestMarketData:
    def test_snapshot_contract(self, api):
        r = api.get(f"{BASE_URL}/api/market-data/snapshot?symbol=EURUSD")
        assert r.status_code == 200
        d = r.json()
        for k in ("snapshotId", "timestamp", "symbol", "timeframe", "ohlc", "spread",
                  "session", "marketState", "volatility", "trend", "liquidity",
                  "integrity", "provider", "source"):
            assert k in d, f"snapshot missing {k}"
        assert d["symbol"] == "EURUSD" and d["timeframe"] == "M15"
        for k in ("open", "high", "low", "close"):
            assert k in d["ohlc"]

    def test_ohlc_ordered_and_integrity_ok(self, api):
        # Structural invariant (race-safe): fixture-derived OHLC is well-formed.
        d = api.get(f"{BASE_URL}/api/market-data/snapshot?symbol=EURUSD").json()
        o = d["ohlc"]
        assert o["high"] >= max(o["open"], o["close"])
        assert o["low"] <= min(o["open"], o["close"])
        assert d["integrity"]["ok"] is True
        assert d["integrity"]["issues"] == []

    def test_providers(self, api):
        d = api.get(f"{BASE_URL}/api/market-data/providers").json()
        assert d["active"] == "fixture"
        ids = {p["providerId"] for p in d["providers"]}
        assert {"fixture", "replay", "mock_live", "mt5"} <= ids
        mt5 = next(p for p in d["providers"] if p["providerId"] == "mt5")
        assert mt5["available"] is False  # placeholder only — never connects

    def test_history(self, api):
        # Ensure at least one snapshot exists, then history is a non-empty list.
        api.post(f"{BASE_URL}/api/scheduler/tick")
        hist = api.get(f"{BASE_URL}/api/market-data/history").json()
        assert isinstance(hist, list) and hist
        assert hist[0]["symbol"] and hist[0]["snapshotId"]

    def test_runtime_health_market_data(self, api):
        api.post(f"{BASE_URL}/api/scheduler/tick")
        md = api.get(f"{BASE_URL}/api/runtime/health").json()["marketData"]
        for k in ("marketDataHealthy", "provider", "snapshotsProduced", "snapshotAgeMs",
                  "updateLatencyMs", "lastSnapshotAt", "dataIntegrity",
                  "currentSymbol", "currentTimeframe"):
            assert k in md, f"marketData health missing {k}"
        assert md["marketDataHealthy"] is True
        assert md["provider"] == "fixture"

    def test_engine_owns_snapshot_and_strategy_consumes_it(self, api):
        # The scheduler tick produces snapshots via the engine, then the strategy
        # consumes them. Market state on the Decision must match the snapshot's
        # marketState (both sourced from the engine) — and nothing executes.
        snap = api.get(f"{BASE_URL}/api/market-data/snapshot?symbol=EURUSD").json()
        tick = api.post(f"{BASE_URL}/api/scheduler/tick").json()
        strat = tick.get("strategy") or api.get(f"{BASE_URL}/api/strategy/decisions").json()
        assert strat["report"]["executed"] == 0  # engine + strategy execute nothing
        eur = [d for d in strat["decisions"] if d["pair"] == "EURUSD"]
        assert eur, "expected an EURUSD decision"
        assert eur[0]["marketState"] == snap["marketState"]["state"]


# ---------- Risk Engine (Phase 12) ----------

class TestRisk:
    CHECKS = {"max_open_trades", "max_exposure", "max_daily_loss", "max_floating_loss",
              "market_state_compat", "package_compat", "runtime_health", "broker_health"}

    def test_limits(self, api):
        d = api.get(f"{BASE_URL}/api/risk/limits").json()
        for k in ("maxOpenTrades", "maxExposureLots", "maxDailyLoss",
                  "maxFloatingLoss", "minMarketConfidence"):
            assert k in d
        # Account-scoped limits reuse the fixture fundedRules (not invented).
        acct = api.get(f"{BASE_URL}/api/risk/limits?account=acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F").json()
        assert acct["maxDailyLoss"] == 5000.0 and acct["maxExposureLots"] == 20.0

    def test_health_invariant(self, api):
        h = api.get(f"{BASE_URL}/api/risk/health").json()
        for k in ("riskHealthy", "assessments", "allowed", "warnings", "denials",
                  "averageAssessmentMs", "lastAssessmentMs", "lastAssessmentAt", "lastSeverity"):
            assert k in h
        assert h["riskHealthy"] is True
        # Invariant (race-safe): every assessment lands in exactly one bucket.
        assert h["allowed"] + h["warnings"] + h["denials"] == h["assessments"]

    def test_assessment_contract(self, api):
        dep_id = api.get(f"{BASE_URL}/api/deployments").json()[0]["deploymentId"]
        a = api.get(f"{BASE_URL}/api/risk/assessment?deployment={dep_id}").json()
        for k in ("timestamp", "deployment", "account", "symbol", "severity", "allowed",
                  "reason", "checks", "limits", "metrics"):
            assert k in a, f"assessment missing {k}"
        assert self.CHECKS <= {c["name"] for c in a["checks"]}
        assert a["severity"] in ("allow", "warn", "deny")
        assert a["allowed"] == (a["severity"] != "deny")

    def test_assesses_every_deployment(self, api):
        deps = api.get(f"{BASE_URL}/api/deployments").json()
        d = api.get(f"{BASE_URL}/api/risk/assessment").json()
        assert len(d["assessments"]) == len(deps)  # the engine assesses every deployment

    def test_allowed_iff_no_deny(self, api):
        # Race-safe structural invariant: a deployment is allowed exactly when no
        # check denied it (e.g. a non-active package pin denies package_compat).
        d = api.get(f"{BASE_URL}/api/risk/assessment").json()
        for a in d["assessments"]:
            has_deny = any(c["severity"] == "deny" for c in a["checks"])
            assert a["allowed"] == (not has_deny)
            assert (a["severity"] == "deny") == has_deny

    def test_pipeline_assesses_and_never_executes(self, api):
        # Risk sits between Decision and Execution: the scheduler-driven pipeline
        # attaches one assessment per Decision and executes nothing.
        r = api.post(f"{BASE_URL}/api/scheduler/tick").json()
        strat = r.get("strategy") or api.get(f"{BASE_URL}/api/strategy/decisions").json()
        risk = strat.get("risk", {})
        assert risk.get("assessments") and len(risk["assessments"]) == strat["report"]["evaluated"]
        assert strat["report"]["executed"] == 0  # risk + strategy execute nothing


# ---------- MT5 Market Data Provider (Phase 13) ----------

class TestMT5MarketData:
    def test_disabled_by_default(self, api):
        # MT5 is registered but NOT active; Fixture stays the active provider.
        d = api.get(f"{BASE_URL}/api/market-data/providers").json()
        assert d["active"] == "fixture"  # selection is config-only; default = fixture
        mt5 = next(p for p in d["providers"] if p["providerId"] == "mt5")
        assert mt5["available"] is False and mt5["connection"] == "Disconnected"
        assert mt5["symbolCount"] == 3

    def test_available_symbols(self, api):
        d = api.get(f"{BASE_URL}/api/market-data/symbols?provider=mt5").json()
        assert d["provider"] == "mt5"
        syms = {s["symbol"]: s["brokerSymbol"] for s in d["symbols"]}
        # Broker-style aliases reused (single source of alias truth).
        assert syms.get("EURUSD") == "EURUSD.r" and syms.get("XAUUSD") == "XAUUSD.a"

    def test_quote_bid_ask_spread(self, api):
        q = api.get(f"{BASE_URL}/api/market-data/quote?symbol=EURUSD&provider=mt5").json()
        for k in ("symbol", "brokerSymbol", "bid", "ask", "spread", "timeframe", "timestamp", "connection"):
            assert k in q, f"quote missing {k}"
        assert q["bid"] < q["ask"] and q["spread"] > 0
        assert q["brokerSymbol"] == "EURUSD.r"

    def test_preview_snapshot_contract(self, api):
        # Read-only preview from MT5 — full snapshot contract, provider tagged mt5,
        # and it does NOT change the active provider.
        s = api.get(f"{BASE_URL}/api/market-data/snapshot?symbol=EURUSD&provider=mt5").json()
        for k in ("snapshotId", "timestamp", "symbol", "timeframe", "ohlc", "spread",
                  "session", "marketState", "volatility", "trend", "liquidity",
                  "integrity", "provider", "source"):
            assert k in s, f"snapshot missing {k}"
        assert s["provider"] == "mt5" and s["source"] == "mt5"
        assert s["integrity"]["ok"] is True
        # Active provider unchanged by a read-only preview.
        assert api.get(f"{BASE_URL}/api/market-data/providers").json()["active"] == "fixture"

    def test_unknown_provider_404(self, api):
        assert api.get(f"{BASE_URL}/api/market-data/snapshot?symbol=EURUSD&provider=nope").status_code == 404

    def test_health_feed_fields(self, api):
        # Objective 4 — marketData health carries the provider feed fields.
        api.post(f"{BASE_URL}/api/scheduler/tick")
        md = api.get(f"{BASE_URL}/api/runtime/health").json()["marketData"]
        for k in ("provider", "connection", "symbolCount", "feedLatencyMs", "lastUpdate"):
            assert k in md, f"marketData health missing {k}"
        # Fixture is active by default → a local (non-MT5) feed.
        assert md["provider"] == "fixture" and md["connection"] == "local"


# ---------- Genuine MT5 provider (Phase 21) ----------

class TestMT5RealProvider:
    """The MT5 provider is now a real MetaTrader5 integration. These tests exercise its
    behaviour WITHOUT a terminal (the CI/dev host has no MetaTrader5): it must degrade to
    disconnected + empty while keeping the provider interface intact."""

    def _mt5(self, api):
        prov = api.get(f"{BASE_URL}/api/market-data/providers").json()["providers"]
        return next(p for p in prov if p["providerId"] == "mt5")

    def test_disconnected_when_terminal_absent(self, api):
        # Default (fixture-active) host has no MetaTrader5 → mt5 is Disconnected/unavailable.
        mt5 = self._mt5(api)
        assert mt5["connection"] == "Disconnected"
        assert mt5["available"] is False

    def test_status_exposes_feed_fields(self, api):
        mt5 = self._mt5(api)
        for k in ("connection", "symbolCount", "cacheHits", "historyFetches", "timeframes"):
            assert k in mt5, f"mt5 status missing {k}"
        # Advertises the full timeframe set even while disconnected (config, not a session).
        assert set(mt5["timeframes"]) >= {"M1", "M5", "M15", "H1", "H4", "D1"}

    def test_returns_no_bars_when_disconnected(self, api):
        d = api.get(f"{BASE_URL}/api/market-data/candles?symbol=EURUSD&count=50&provider=mt5").json()
        assert d["provider"] == "mt5" and d["count"] == 0 and d["candles"] == []

    def test_all_timeframes_handled(self, api):
        # Every required timeframe is accepted (200 + empty here) — same code serves real
        # bars on a connected host with no further change.
        for tf in ("M1", "M5", "M15", "H1", "H4", "D1"):
            r = api.get(f"{BASE_URL}/api/market-data/candles?symbol=EURUSD&count=20&timeframe={tf}&provider=mt5")
            assert r.status_code == 200 and r.json()["count"] == 0

    def test_arbitrary_count_accepted(self, api):
        for n in (100, 250, 500):
            r = api.get(f"{BASE_URL}/api/market-data/candles?symbol=EURUSD&count={n}&provider=mt5")
            assert r.status_code == 200  # accepted; empty here, real bars on a live host

    def test_switching_to_mt5_keeps_engine_and_interface(self, api):
        # Provider abstraction intact: mt5 still lists symbols with broker aliases and the
        # engine/providers endpoint keeps working (switching is config-only, no code path).
        syms = api.get(f"{BASE_URL}/api/market-data/symbols?provider=mt5").json()
        by = {s["symbol"]: s["brokerSymbol"] for s in syms["symbols"]}
        assert by.get("EURUSD") == "EURUSD.r" and by.get("XAUUSD") == "XAUUSD.a"


# ---------- Market Data candle series (Phase 16) ----------

class TestMarketCandles:
    def _candles(self, api, **params):
        q = "&".join(f"{k}={v}" for k, v in params.items())
        return api.get(f"{BASE_URL}/api/market-data/candles?{q}").json()

    def test_candles_contract(self, api):
        d = self._candles(api, symbol="EURUSD", count=50)
        for k in ("symbol", "timeframe", "provider", "end", "count", "candles"):
            assert k in d, f"candles response missing {k}"
        assert d["count"] == 50 and len(d["candles"]) == 50
        for c in d["candles"]:
            for k in ("time", "open", "high", "low", "close"):
                assert k in c
            assert c["high"] >= max(c["open"], c["close"])
            assert c["low"] <= min(c["open"], c["close"])
        # Strictly increasing time axis (one clean series).
        times = [c["time"] for c in d["candles"]]
        assert times == sorted(times) and len(set(times)) == len(times)

    def test_active_provider_default(self, api):
        # Live candles come from the active provider (fixture by default).
        d = self._candles(api, symbol="EURUSD", count=10)
        assert d["provider"] == "fixture"

    def test_every_provider_serves_candles(self, api):
        # The synthetic providers serve the deterministic series through the one generator.
        for provider in ("fixture", "replay", "mock_live"):
            d = self._candles(api, symbol="EURUSD", count=8, provider=provider)
            assert d["provider"] == provider and d["count"] == 8
        # MT5 is a GENUINE provider (Phase 21): it serves bars only when a terminal is
        # connected; where MetaTrader5 is unavailable it reports disconnected + no bars.
        prov = api.get(f"{BASE_URL}/api/market-data/providers").json()["providers"]
        mt5 = next(p for p in prov if p["providerId"] == "mt5")
        d = self._candles(api, symbol="EURUSD", count=8, provider="mt5")
        assert d["provider"] == "mt5"
        if mt5["available"]:
            assert d["count"] == 8  # real terminal present
        else:
            assert d["count"] == 0 and mt5["connection"] == "Disconnected"

    def test_replay_honours_end(self, api):
        from datetime import datetime
        end_iso = "2026-07-01T09:14:00Z"
        d = self._candles(api, symbol="EURUSD", count=5, provider="replay", end=end_iso)
        assert d["end"] == end_iso
        end_unix = int(datetime.fromisoformat(end_iso.replace("Z", "+00:00")).timestamp())
        # Series ends at (just before) the requested replay-window end.
        assert d["candles"][-1]["time"] <= end_unix

    def test_candles_deterministic(self, api):
        a = self._candles(api, symbol="EURUSD", count=20, end="2026-07-01T09:14:22Z")
        b = self._candles(api, symbol="EURUSD", count=20, end="2026-07-01T09:14:22Z")
        assert a["candles"] == b["candles"]  # deterministic — same key, same series

    def test_unknown_provider_empty(self, api):
        d = self._candles(api, symbol="EURUSD", count=5, provider="nope")
        assert d["count"] == 0 and d["candles"] == []


# ---------- Portfolio Engine (Phase 14) ----------

class TestPortfolio:
    FIELDS = ("timestamp", "deployment", "account", "symbol", "allocation", "priority",
              "score", "reason", "capitalRequired", "capitalAvailable")

    def test_status(self, api):
        d = api.get(f"{BASE_URL}/api/portfolio/status").json()
        assert d["portfolioHealthy"] is True
        # Total capital reuses account equity (sum) — not recomputed from trades.
        accts = api.get(f"{BASE_URL}/api/dev/fixture-world").json()["accounts"]
        expected = round(sum((a.get("equity") or a.get("balance") or 0) for a in accts), 2)
        assert d["totalCapital"] == expected
        assert d["limits"]["maxAllocationsPerPair"] >= 1

    def test_metrics_invariant(self, api):
        api.post(f"{BASE_URL}/api/scheduler/tick")
        h = api.get(f"{BASE_URL}/api/portfolio/metrics").json()
        for k in ("portfolioHealthy", "cycles", "allocations", "deferred", "rejected",
                  "totalCapital", "allocatedCapital", "availableCapital", "utilisationPct"):
            assert k in h
        assert h["portfolioHealthy"] is True
        # Invariant (race-safe): allocated + available == total (last cycle, computed together).
        assert round(h["allocatedCapital"] + h["availableCapital"], 2) == round(h["totalCapital"], 2)

    def test_allocations_contract(self, api):
        api.post(f"{BASE_URL}/api/scheduler/tick")
        decs = api.get(f"{BASE_URL}/api/portfolio/allocations").json()
        assert isinstance(decs, list) and decs
        for a in decs:
            for k in self.FIELDS:
                assert k in a, f"allocation missing {k}"
            assert a["allocation"] in ("allocate", "defer", "reject")
            assert a["capitalRequired"] >= 0 and a["capitalAvailable"] >= 0

    def test_pipeline_allocates_every_opportunity(self, api):
        # Portfolio sits between Risk and Execution: one AllocationDecision per
        # opportunity; the verdict counts partition them; nothing is executed.
        r = api.post(f"{BASE_URL}/api/scheduler/tick").json()
        strat = r.get("strategy") or api.get(f"{BASE_URL}/api/strategy/decisions").json()
        pf = strat.get("portfolio", {})
        summ = pf.get("summary", {})
        assert len(pf.get("decisions", [])) == strat["report"]["evaluated"]
        assert summ["allocations"] + summ["deferred"] + summ["rejected"] == len(pf["decisions"])
        assert strat["report"]["executed"] == 0  # portfolio + strategy execute nothing

    def test_risk_denied_forces_reject(self, api):
        # Invariant (race-safe): any risk-denied opportunity is rejected by portfolio.
        r = api.post(f"{BASE_URL}/api/scheduler/tick").json()
        strat = r.get("strategy") or api.get(f"{BASE_URL}/api/strategy/decisions").json()
        risk = {a["deployment"]: a for a in strat["risk"]["assessments"]}
        for a in strat["portfolio"]["decisions"]:
            asr = risk.get(a["deployment"])
            if asr and asr["allowed"] is False:
                assert a["allocation"] == "reject"

    def test_allocated_capital_matches_decisions(self, api):
        r = api.post(f"{BASE_URL}/api/scheduler/tick").json()
        strat = r.get("strategy") or api.get(f"{BASE_URL}/api/strategy/decisions").json()
        pf = strat["portfolio"]
        allocated = round(sum(d["capitalRequired"] for d in pf["decisions"] if d["allocation"] == "allocate"), 2)
        assert allocated == pf["summary"]["allocatedCapital"]
