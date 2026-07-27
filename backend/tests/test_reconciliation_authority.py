"""ARCH-2 — the canonical reconciliation authority + the execution context."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import command_authorization as ca                                  # noqa: E402
import execution_context as xc                                      # noqa: E402
import execution_safety as es                                       # noqa: E402
import execution_store as xs                                        # noqa: E402
import order_lifecycle as ol                                        # noqa: E402
import reconciliation as rc                                         # noqa: E402

NOW = "2026-07-27T00:01:00Z"
FRESH = "2026-07-27T00:00:30Z"
STALE = "2026-07-26T00:00:00Z"


def _run(**over):
    kw = dict(tower_intents=[], broker_orders=[], broker_positions=[],
              broker_account_identity="acc_mock", expected_account_identity="acc_mock",
              broker_snapshot_at=FRESH, now=NOW)
    kw.update(over)
    return rc.run_reconciliation(**kw)


def _inflight(state=ol.SUBMITTED, ref="ord_1", intent="intent_a", qty=None, entry=None):
    return {"intent_id": intent, "state": state, "broker_ref": ref,
            "quantity": qty, "entry": entry}


# ── clean and per-class classification ───────────────────────────────────────

def test_clean_match_is_clean_and_immutable():
    run = _run()
    assert run.clean and not run.critical and not run.discrepancies
    assert run.recon_id.startswith("recon_")
    with pytest.raises(Exception):
        run.stale = True                              # frozen


def test_tower_only_order():
    run = _run(tower_intents=[_inflight(ref="ord_ghost")])
    assert any(d.cls == rc.TOWER_ONLY_ORDER for d in run.discrepancies)


def test_broker_only_order_is_critical():
    run = _run(broker_orders=[{"id": "ord_alien", "state": "pending"}])
    d = next(d for d in run.discrepancies if d.cls == rc.BROKER_ONLY_ORDER)
    assert d.critical and run.critical


def test_broker_only_position_is_critical():
    run = _run(broker_positions=[{"id": "pos_alien", "_fixture_lineage": False}])
    assert any(d.cls == rc.BROKER_ONLY_POSITION and d.critical for d in run.discrepancies)


def test_status_quantity_and_price_mismatches():
    run = _run(
        tower_intents=[_inflight(state=ol.SUBMITTED, ref="ord_1", qty=1.0, entry=1.1000)],
        broker_orders=[{"id": "ord_1", "state": "rejected", "size": 2.0, "entry": 1.2000}])
    classes = {d.cls for d in run.discrepancies}
    assert {rc.STATUS_MISMATCH, rc.QUANTITY_MISMATCH, rc.PRICE_MISMATCH} <= classes


def test_missing_and_duplicate_broker_reference():
    run = _run(tower_intents=[
        _inflight(state=ol.SUBMITTED, ref=None, intent="intent_a"),
        _inflight(state=ol.ACKNOWLEDGED, ref="ord_x", intent="intent_b"),
        _inflight(state=ol.ACKNOWLEDGED, ref="ord_x", intent="intent_c"),
    ], broker_orders=[{"id": "ord_x", "state": "pending"}])
    classes = [d.cls for d in run.discrepancies]
    assert rc.MISSING_BROKER_REFERENCE in classes
    assert rc.DUPLICATE_BROKER_REFERENCE in classes


def test_account_identity_mismatch_is_a_hard_failure():
    run = _run(broker_account_identity="acc_other")
    assert run.failed and run.critical and not run.clean
    assert any(d.cls == rc.ACCOUNT_IDENTITY_MISMATCH for d in run.discrepancies)


def test_stale_broker_snapshot_can_never_reconcile_clean():
    run = _run(broker_snapshot_at=STALE)
    assert run.stale and not run.clean
    assert any(d.cls == rc.STALE_SNAPSHOT for d in run.discrepancies)
    missing = _run(broker_snapshot_at=None)
    assert missing.stale and not missing.clean


def test_stale_node_snapshot_taints_the_run():
    run = _run(node_snapshot={"positions": []}, node_snapshot_at=STALE)
    assert run.stale and not run.clean


def test_node_position_count_cross_check():
    run = _run(node_snapshot={"positions": [{}, {}]}, node_snapshot_at=FRESH,
               broker_positions=[])
    assert any(d.cls == rc.STATUS_MISMATCH for d in run.discrepancies)


def test_every_discrepancy_class_is_in_the_declared_vocabulary():
    assert rc.CRITICAL_CLASSES <= rc.ALL_CLASSES
    for cls in (rc.TOWER_ONLY_ORDER, rc.BROKER_ONLY_ORDER, rc.STATUS_MISMATCH,
                rc.QUANTITY_MISMATCH, rc.PRICE_MISMATCH, rc.STALE_SNAPSHOT,
                rc.ACCOUNT_IDENTITY_MISMATCH):
        assert cls in rc.ALL_CLASSES


def test_run_records_sources_and_account_identity():
    run = _run()
    rec = run.as_record()
    assert rec["sources"]["brokerSnapshotAt"] == FRESH
    assert rec["accountIdentity"] == "acc_mock"
    assert rec["expectedAccountIdentity"] == "acc_mock"


# ── read-only + persistence + safety feed ────────────────────────────────────

def test_reconciliation_module_is_read_only_toward_the_broker():
    from conftest import code_only
    code = code_only("reconciliation.py")
    for forbidden in ("submit_command", "cancel_order(", "modify_order(",
                      "flatten(", "submit_order", "close_position(", "connect("):
        assert forbidden not in code, forbidden
    # And it imports no adapter/broker module at all — inputs are injected.
    assert "import broker" not in code


def test_result_persists_and_feeds_safety(tmp_path):
    store = xs.ExecutionStore(tmp_path / "x.db")
    run = _run(broker_orders=[{"id": "ord_alien", "state": "pending"}])
    rc.persist(store, run)
    posture = rc.safety_posture(store)
    assert posture["criticalUnresolved"] is True
    assert posture["lastRunId"] == run.recon_id

    # The safety gate consumes the posture: risk-increasing denied, close allowed.
    ctx = es.SafetyContext(
        mode=es.MODE_ACTIVE,
        arming=es.ArmingState(True, "op", "2099-01-01T00:00:00Z"),
        node=es.NodeSafety(es.NODE_HEALTHY),
        operator=es.OperatorAuthorization("op", True),
        reconciliation=es.ReconciliationSafety(
            critical_unresolved=posture["criticalUnresolved"], stale=posture["stale"]))
    assert es.evaluate(es.CommandRequest(command_type="MoveTradeSL"), ctx).reason \
        == es.DENY_RECONCILIATION_REQUIRED
    assert es.evaluate(es.CommandRequest(command_type="CloseTrade"), ctx).allowed
    assert es.evaluate(es.CommandRequest(command_type="CancelOrder"), ctx).allowed
    assert es.evaluate(es.CommandRequest(command_type="GlobalKill"), ctx).allowed


def test_safety_posture_fails_closed_when_the_store_is_broken():
    class Broken:
        def latest_reconciliation(self):
            raise RuntimeError("io error")
        def unresolved_critical_count(self):
            raise RuntimeError("io error")
    posture = rc.safety_posture(Broken())
    assert posture["criticalUnresolved"] is True and posture["stale"] is True


# ── execution context ────────────────────────────────────────────────────────

def test_context_is_immutable_and_default_denies():
    ctx = xc.ExecutionContext()
    with pytest.raises(Exception):
        ctx.execution_mode = es.MODE_ACTIVE
    decision = es.evaluate(es.CommandRequest(command_type="CloseTrade"),
                           ctx.to_safety_context())
    assert decision.allowed is False


def _grant(classes=("execution_affecting",), scopes=(ca.SCOPE_ANY,), **over):
    kw = dict(authorization_id=ca.new_authorization_id(), operator_ref="op-7",
              issued_at="2026-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
              allowed_risk_classes=frozenset(classes),
              allowed_scopes=frozenset(scopes), confirmed=True, provider="test")
    kw.update(over)
    return ca.AuthorizationGrant(**kw)


def test_context_missing_node_health_denies_execution():
    ctx = xc.ExecutionContext(
        execution_mode=es.MODE_ACTIVE,
        operator=es.OperatorAuthorization("op", True),
        authorization=_grant(),
        account_identity_state=es.ACCOUNT_MATCH,
        node=xc.NodeFacts(health=""),                  # missing → unknown → deny
    )
    d = es.evaluate(es.CommandRequest(command_type="CloseTrade"),
                    ctx.to_safety_context("CloseTrade"))
    assert d.allowed is False and d.reason == "node_unknown"


def test_node_authority_and_tower_authority_cannot_be_confused():
    # The tower-side fact is an operator COMMAND AUTHORIZATION grant; node facts
    # live in the observed node group. No generic "armed" boolean exists on the
    # tower side, and no top-level field is called "arming".
    ctx = xc.ExecutionContext(authorization=_grant())
    view = ctx.safe_view()
    assert view["authorization"]["provider"] == "test"
    assert "node" in view and "armingStatus" in view["node"]   # node arming = observed fact
    fields = {f.name for f in __import__("dataclasses").fields(xc.ExecutionContext)}
    assert "arming" not in fields
    assert "authorization" in fields and "node" in fields
    # A tower grant NEVER populates the node arming facts, and vice versa.
    assert ctx.node.arming_armed is None
    # And node arming cannot substitute for tower authorization: armed node facts
    # with NO grant still derive a disarmed safety window.
    armed_node = xc.ExecutionContext(
        node=xc.NodeFacts(arming_status="armed", arming_armed=True,
                          provenance=xc.PROV_NODE_TELEMETRY))
    assert armed_node.to_safety_context("CloseTrade").arming.armed is False


def test_context_safe_view_is_redaction_safe():
    ctx = xc.ExecutionContext(
        node=xc.NodeFacts(account_fingerprint="login:1234567 password=P@SS"))
    view = ctx.safe_view()
    assert "P@SS" not in str(view)


def test_context_capabilities_freeze_and_fail_closed():
    assert xc.capabilities_from_mapping({"supportsModify": True, "x": False}) == \
        frozenset({"supportsModify"})
    assert xc.capabilities_from_mapping("not-a-dict") == frozenset()
    assert xc.capabilities_from_mapping(None) == frozenset()


def test_reconciliation_facts_flow_from_context_into_the_safety_gate():
    ctx = xc.ExecutionContext(
        execution_mode=es.MODE_ACTIVE,
        operator=es.OperatorAuthorization("op", True),
        authorization=_grant(),
        account_identity_state=es.ACCOUNT_MATCH,
        node=xc.NodeFacts(health=es.NODE_HEALTHY, stale=False),
        reconciliation=xc.ReconciliationFacts(critical_unresolved=True))
    d = es.evaluate(es.CommandRequest(command_type="MoveTradeSL"),
                    ctx.to_safety_context("MoveTradeSL"))
    assert d.reason == es.DENY_RECONCILIATION_REQUIRED
