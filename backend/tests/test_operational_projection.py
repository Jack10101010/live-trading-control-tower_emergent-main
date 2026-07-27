"""LIVE-4A — the canonical operational projection.

Proves: one projection owner, deterministic/idempotent derivation, immutable
serializable models, explicit freshness (fresh / stale / unavailable kept
distinct), provenance on every model, nothing invented when a source is absent,
orders and positions as separate concepts, the unified read-only API, and that
the projection owns no writes and no execution capability.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import operational_projection as op                                 # noqa: E402
import scenario_domain as sd                                        # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

NOW = "2026-07-27T12:00:00Z"
OLD = "2026-07-27T10:00:00Z"          # 2h earlier -> stale at a 120s threshold


@pytest.fixture(autouse=True)
def isolated_stores(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")


# ── source fixtures ───────────────────────────────────────────────────────────

def _snapshot(at=NOW, positions=None, orders=None, accounts=None):
    return {
        "positions": positions if positions is not None else [
            {"positionId": "P1", "canonicalSymbol": "EURUSD", "side": "long",
             "size": 0.10, "entry": 1.1000, "sl": 1.0950, "tp": 1.1100,
             "state": "open", "unrealizedPnl": 12.5}],
        "orders": orders if orders is not None else [],
        "accounts": accounts if accounts is not None else [
            {"accountId": "acct_1", "brokerId": "brk_x", "type": "demo",
             "baseCurrency": "USD", "balance": 100000.0, "equity": 100412.0}],
        "connection": "Connected", "accountIdentity": "acctfp_live1",
        "at": at, "provenance": "mock-fixture",
    }


def _intent(intent_id="intent_a", command="ClosePosition", state="acknowledged",
            ref="P1", instrument="EURUSD", created=NOW):
    return {
        "intent_id": intent_id, "command_name": command, "kind": "close",
        "state": state, "broker_ref": ref, "instrument": instrument,
        "side": "long", "quantity": 0.1, "order_type": "market",
        "created_at": created, "updated_at": created,
        "metadata_json": json.dumps({"payload": {"positionRef": ref,
                                                 "scenarioKey": "EURUSD:london"}}),
    }


def _scenario(**over):
    kw = dict(instrument="EURUSD", session="london", structure="BOS",
              direction="long", entry_model="BullExpand", created_at=NOW,
              node_id="node-1", account_fingerprint="acctfp_live1")
    kw.update(over)
    return sd.new_scenario(**kw)


def _sources(**over):
    base = dict(
        broker_snapshot=lambda: _snapshot(),
        account_snapshot=lambda: None,
        adapter_kind=lambda: "mock",
        connection_state=lambda: "Connected",
        node_entries=lambda: [{"instance_id": "node-1", "published_at": NOW,
                               "age_seconds": 3.0, "stale": False,
                               "snapshot": {"engine": {"symbol": "EURUSD"},
                                            "account": {"identity": {"fingerprint": "acctfp_live1"}},
                                            "runtime": {"mode": "dry_run"}}}],
        intents=lambda: [_intent()],
        transitions=lambda i: [{"to_state": "acknowledged", "at": NOW,
                                "reason": "broker_acknowledged", "broker_ref": "P1"}],
        reconciliation_posture=lambda: {"criticalUnresolved": False, "stale": False,
                                        "lastRunId": "recon_1"},
        latest_reconciliation=lambda: None,
        execution_mode=lambda: "observe",
        authorization=lambda: None,
        entity_locks=lambda: [],
    )
    base.update(over)
    return op.ProjectionSources(**base)


# ── determinism, immutability, serialization ─────────────────────────────────

def test_projection_is_deterministic_and_idempotent():
    s = _sources()
    a = json.dumps(op.build_summary(s, now=NOW).as_dict(), sort_keys=True)
    b = json.dumps(op.build_summary(s, now=NOW).as_dict(), sort_keys=True)
    assert a == b                                   # same sources + now -> identical
    # Idempotent: building repeatedly holds no state that changes the answer.
    c = json.dumps(op.build_summary(s, now=NOW).as_dict(), sort_keys=True)
    assert a == c


def test_every_model_is_immutable():
    s = _sources()
    summary = op.build_summary(s, now=NOW)
    for obj in (summary, summary.nodes[0], summary.accounts[0],
                summary.open_positions[0], summary.freshness):
        with pytest.raises(Exception):
            setattr(obj, "provenance", "tampered")
    assert isinstance(summary.nodes, tuple)         # collections are tuples, not lists


def test_models_serialize_deterministically_and_are_json_safe():
    summary = op.build_summary(_sources(), now=NOW)
    d = summary.as_dict()
    assert list(d) == sorted(d)                     # sorted keys at every level
    assert list(d["nodes"][0]) == sorted(d["nodes"][0])
    json.dumps(d)                                   # fully JSON-serializable


def test_schema_version_is_pinned():
    assert op.build_summary(_sources(), now=NOW).as_dict()["schemaVersion"] == \
        "ct.operational-projection.v1"


# ── freshness: fresh / stale / unavailable stay DISTINCT ─────────────────────

def test_fresh_source_is_fresh():
    f = op.freshness(now=NOW, source_at=NOW)
    assert f.available and not f.stale and f.status == op.AVAILABILITY_OK
    assert f.age_seconds == 0.0


def test_old_source_is_stale_not_unavailable():
    f = op.freshness(now=NOW, source_at=OLD)
    assert f.available is True                      # it WAS readable
    assert f.stale is True and f.status == op.AVAILABILITY_STALE
    assert f.age_seconds == 7200.0


def test_unreadable_source_is_unavailable_not_stale_zero():
    f = op.freshness(now=NOW, source_at=None, available=False)
    assert f.available is False and f.status == op.AVAILABILITY_UNAVAILABLE
    assert f.age_seconds is None                    # NOT zero — absence is absence


def test_unparseable_timestamp_is_stale_never_fresh():
    f = op.freshness(now=NOW, source_at="not-a-timestamp")
    assert f.available is True and f.stale is True
    assert "unparseable" in (f.detail or "")


def test_stale_broker_snapshot_propagates_to_the_summary():
    summary = op.build_summary(_sources(broker_snapshot=lambda: _snapshot(at=OLD)),
                               now=NOW)
    assert summary.freshness.stale is True
    assert summary.freshness.status == op.AVAILABILITY_STALE


def test_unavailable_broker_snapshot_is_explicit_and_invents_nothing():
    summary = op.build_summary(_sources(broker_snapshot=lambda: None), now=NOW)
    assert summary.freshness.status == op.AVAILABILITY_UNAVAILABLE
    assert summary.open_positions == ()             # no invented positions
    acct = summary.accounts[0]
    assert acct.freshness.available is False
    assert acct.balance is None and acct.equity is None      # not zeroed
    assert "broker snapshot unavailable" in summary.warnings


# ── provenance on every model ────────────────────────────────────────────────

def test_every_model_carries_provenance():
    # LIVE-4B: scenarios now come from the canonical scenario STORE source.
    summary = op.build_summary(_sources(scenarios=lambda: [_scenario()]), now=NOW)
    assert summary.nodes[0].provenance == op.PROV_NODE_TELEMETRY
    assert summary.accounts[0].provenance == op.PROV_MOCK_FIXTURE
    assert summary.open_positions[0].provenance == op.PROV_MOCK_FIXTURE
    assert summary.active_scenarios[0].provenance == op.PROV_DURABLE_STORE


def test_live_adapter_changes_provenance_to_live():
    summary = op.build_summary(_sources(adapter_kind=lambda: "mt5"), now=NOW)
    assert summary.accounts[0].provenance == op.PROV_LIVE_MT5
    assert summary.open_positions[0].provenance == op.PROV_LIVE_MT5


def test_absent_node_telemetry_is_explicit_not_empty():
    summary = op.build_summary(_sources(node_entries=list), now=NOW)
    assert len(summary.nodes) == 1                  # one EXPLICIT unavailable view
    node = summary.nodes[0]
    assert node.provenance == op.PROV_ABSENT
    assert node.freshness.available is False
    assert "node telemetry unavailable" in node.warnings


# ── orders and positions remain separate concepts ────────────────────────────

def test_orders_and_positions_are_never_merged():
    summary = op.build_summary(_sources(
        broker_snapshot=lambda: _snapshot(
            orders=[{"orderId": "O1", "canonicalSymbol": "EURUSD", "side": "long",
                     "size": 0.2, "state": "pending"}]),
        intents=lambda: [_intent(intent_id="intent_o", command="CancelPendingOrder",
                                 state="acknowledged", ref="O1")]), now=NOW)
    assert len(summary.active_orders) == 1
    assert len(summary.open_positions) == 1
    order = summary.active_orders[0]
    position = summary.open_positions[0]
    assert order.broker_order_reference == "O1"
    assert position.broker_position_reference == "P1"
    # Distinct model types with distinct field vocabularies.
    assert "brokerOrderReference" in order.as_dict()
    assert "brokerPositionReference" in position.as_dict()
    assert "brokerPositionReference" not in order.as_dict()


def test_only_active_orders_are_projected():
    summary = op.build_summary(_sources(
        intents=lambda: [_intent(intent_id="intent_done", state="closed"),
                         _intent(intent_id="intent_live", state="acknowledged")]),
        now=NOW)
    ids = [o.intent_id for o in summary.active_orders]
    assert ids == ["intent_live"]                   # terminal states are not "active"


def test_order_without_broker_report_is_flagged_not_hidden():
    summary = op.build_summary(_sources(
        broker_snapshot=lambda: _snapshot(orders=[]),
        intents=lambda: [_intent(intent_id="intent_x", command="CancelPendingOrder",
                                 state="acknowledged", ref="MISSING")]), now=NOW)
    assert summary.active_orders[0].broker_status == "not_reported_by_broker"


# ── positions: derived truth, protection state, lifecycle, locks ─────────────

def test_position_projects_broker_truth_and_protection_state():
    p = op.build_summary(_sources(), now=NOW).open_positions[0]
    assert p.instrument == "EURUSD" and p.side == "long" and p.quantity == 0.10
    assert p.entry_price == 1.1000 and p.stop_loss == 1.0950 and p.take_profit == 1.1100
    assert p.unrealized_pnl == 12.5
    assert p.protection_state == "protected"
    assert p.current_price is None                  # no feed -> never invented
    assert p.lifecycle and p.lifecycle[0]["reason"] == "broker_acknowledged"


@pytest.mark.parametrize("sl,tp,expected", [
    (1.09, 1.11, "protected"), (1.09, 0, "stop_only"),
    (0, 1.11, "target_only"), (0, 0, "unprotected"),
])
def test_protection_state_is_derived_from_observed_levels(sl, tp, expected):
    snap = _snapshot(positions=[{"positionId": "P1", "canonicalSymbol": "EURUSD",
                                 "side": "long", "size": 0.1, "entry": 1.1,
                                 "sl": sl, "tp": tp}])
    p = op.build_summary(_sources(broker_snapshot=lambda: snap), now=NOW).open_positions[0]
    assert p.protection_state == expected


def test_entity_lock_surfaces_on_the_position_and_in_operations():
    locks = [{"entity_ref": "P1", "intent_id": "intent_a", "operation": "ClosePosition",
              "acquired_at": NOW}]
    summary = op.build_summary(_sources(entity_locks=lambda: locks), now=NOW)
    assert summary.open_positions[0].reconciliation["locked"] is True
    assert summary.open_positions[0].reconciliation["lockOperation"] == "ClosePosition"
    assert summary.active_operations[0]["entityRef"] == "P1"


def test_reconciliation_required_is_surfaced_on_orders_and_positions():
    summary = op.build_summary(_sources(
        intents=lambda: [_intent(state="reconciliation_required")]), now=NOW)
    assert summary.active_orders[0].reconciliation["required"] is True
    assert summary.open_positions[0].reconciliation["required"] is True


def test_unresolved_reconciliation_items_become_summary_issues():
    latest = {"items": [{"class": "status_mismatch", "entity_id": "P1", "critical": 0,
                         "detail": "not observed", "resolved": 0},
                        {"class": "stale_snapshot", "entity_id": None, "critical": 0,
                         "detail": "old", "resolved": 1}]}
    summary = op.build_summary(_sources(latest_reconciliation=lambda: latest), now=NOW)
    assert len(summary.reconciliation_issues) == 1        # resolved items excluded
    assert summary.reconciliation_issues[0]["class"] == "status_mismatch"


def test_critical_reconciliation_becomes_a_node_warning():
    summary = op.build_summary(_sources(
        reconciliation_posture=lambda: {"criticalUnresolved": True, "stale": False}),
        now=NOW)
    assert "unresolved critical reconciliation" in summary.nodes[0].warnings


# ── scenarios: placeholder projection of existing evidence only ──────────────

def test_scenarios_project_the_canonical_store_entity():
    """LIVE-4B (deliberate replacement of the LIVE-4A placeholder): scenarios are
    projected from the canonical scenario STORE, not scraped from a payload."""
    summary = op.build_summary(_sources(scenarios=lambda: [_scenario()]), now=NOW)
    assert len(summary.active_scenarios) == 1
    s = summary.active_scenarios[0]
    assert s.scenario_id.startswith("scn_")
    assert s.instrument == "EURUSD" and s.direction == "long"
    assert s.session == "london" and s.structure == "BOS"
    assert s.entry_model == "BullExpand"
    assert s.status == sd.ScenarioStatus.CREATED and s.active is True
    assert s.provenance == op.PROV_DURABLE_STORE
    assert s.freshness is not None


def test_no_scenario_store_yields_no_scenarios_and_is_reported_unavailable():
    """An absent store yields an empty projection AND an explicit unavailable
    signal — an empty list never claims 'no scenarios exist'."""
    sources = _sources()                             # default scenarios source -> None
    summary = op.build_summary(sources, now=NOW)
    assert summary.active_scenarios == ()            # never fabricated
    assert op.scenarios_available(sources) is False
    assert op.scenarios_available(_sources(scenarios=lambda: [])) is True


# ── account masking + non-derivable fields ───────────────────────────────────

def test_node_account_fingerprint_is_masked():
    node = op.build_summary(_sources(), now=NOW).nodes[0]
    assert node.account_fingerprint_masked
    assert node.account_fingerprint_masked != "acctfp_live1"
    assert "…" in node.account_fingerprint_masked


def test_non_derivable_account_fields_are_absent_not_zero():
    acct = op.build_summary(_sources(), now=NOW).accounts[0]
    assert acct.realized_pnl_today is None           # not derivable from evidence
    assert acct.open_risk is None


# ── ledger: interfaces only ──────────────────────────────────────────────────

def test_trade_ledger_is_a_real_read_model_with_honest_unavailability():
    """LIVE-4C (deliberate replacement of the LIVE-4A/4B interface-only pin):
    the ledger is now a REAL read model. An unreadable store is still reported
    explicitly rather than as an empty, healthy-looking ledger."""
    unavailable = op.build_trade_ledger(None, now=NOW)
    assert unavailable.available is False
    assert unavailable.code == "ledger_store_unavailable"
    assert unavailable.trades == ()
    assert unavailable.summary.availability == op.AVAILABILITY_UNAVAILABLE
    json.dumps(unavailable.as_dict())
    # Readable-but-empty is a DIFFERENT state from unavailable.
    empty = op.build_trade_ledger([], now=NOW)
    assert empty.available is True and empty.trades == ()
    for cls in (op.ClosedTradeOperationalView, op.LedgerOperationalSummary,
                op.TradeLedgerOperationalView):
        assert hasattr(cls, "as_dict")


# ── the projection owns no writes and no execution ───────────────────────────

def test_projection_module_performs_no_writes_and_no_execution():
    code = code_only("operational_projection.py")
    for forbidden in ("get_broker", "order_send", "submit_market_order",
                      "record_transition", "acquire_entity_lock", "save_",
                      "execute(", "evaluate(", "sqlite3", "requests.", "socket"):
        assert forbidden not in code, f"projection references {forbidden}"


def test_projection_imports_no_runtime_owner():
    code = code_only("operational_projection.py")
    for forbidden in ("import server", "import broker", "import execution_store",
                      "import execution_safety", "import reconciliation"):
        assert forbidden not in code


# ── the unified read-only API ────────────────────────────────────────────────

@pytest.mark.parametrize("path,key", [
    ("/api/operations/summary", "projectionTimestamp"),
    ("/api/operations/nodes", "nodes"),
    ("/api/operations/accounts", "accounts"),
    ("/api/operations/orders", "orders"),
    ("/api/operations/positions", "positions"),
])
def test_operations_endpoints_are_read_only_and_no_store(path, key):
    r = client.get(path)
    assert r.status_code == 200, r.text
    assert r.headers.get("Cache-Control") == "no-store"
    assert key in r.json()
    # Read-only: the same endpoint must reject mutation verbs.
    assert client.post(path).status_code in (404, 405)


def test_summary_endpoint_returns_the_canonical_projection():
    body = client.get("/api/operations/summary").json()
    for key in ("schemaVersion", "projectionTimestamp", "nodes", "accounts",
                "activeOrders", "openPositions", "activeScenarios",
                "reconciliationIssues", "activeOperations", "warnings",
                "counts", "freshness"):
        assert key in body
    assert body["schemaVersion"] == "ct.operational-projection.v1"


def test_entity_endpoints_resolve_and_404_honestly():
    positions = client.get("/api/operations/positions").json()["positions"]
    if positions:
        ref = positions[0]["brokerPositionReference"]
        got = client.get(f"/api/operations/position/{ref}")
        assert got.status_code == 200
        assert got.json()["brokerPositionReference"] == ref
    for path, code in (("/api/operations/node/missing", "node_not_projected"),
                       ("/api/operations/order/missing", "order_not_projected"),
                       ("/api/operations/position/missing", "position_not_projected")):
        r = client.get(path)
        assert r.status_code == 404 and r.json()["code"] == code
        assert r.headers.get("Cache-Control") == "no-store"


def test_route_handlers_contain_no_aggregation_logic():
    """Every /api/operations handler must delegate to the projection owner —
    no handler may join, filter or derive operational truth itself."""
    code = code_only("server.py")
    start = code.index("def operations_summary")
    # Scope to the /api/operations handlers ONLY. (LIVE-4C added the ledger
    # ingestion SERVICE after them; gathering reconstruction inputs there is
    # correct and is guarded separately in test_trade_ledger.py.)
    end = code.index("def _broker_history_snapshot")
    handlers = code[start:end]
    for forbidden in ("reconcile_snapshot(", "intents_by_state(", "transitions_of(",
                      "safety_posture(", "active_entity_locks("):
        assert forbidden not in handlers, f"handler aggregates via {forbidden}"
    assert handlers.count("projection_layer.build_") >= 5


def test_projection_adds_no_execution_capability():
    """LIVE-4A is read-model only: the execution surface is unchanged."""
    import command_registry as reg
    assert reg.execution_command_names() == frozenset({
        "SubmitMarketOrder", "ModifyPositionProtection",
        "CancelPendingOrder", "ClosePosition"})
