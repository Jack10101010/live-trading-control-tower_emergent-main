"""LIVE-3 — controlled position and order management.

Proves: the durable authorization provider (issuance requirements, exact
scope/adapter/account matching, expiry, revocation, restart persistence),
execution-mode governance (observe default, manual_live gate matrix, halted
semantics, restart reset), the three canonical operations end-to-end through
the canonical pipeline (acknowledgement DISTINCT from reconciliation-confirmed
completion), non-risk-increasing protection rules, entity locking and
concurrency, idempotency (replay / payload conflict), MT5 typed write mapping,
reconciliation confirmation + discrepancies, and the governed API surface.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _fake_mt5                                                    # noqa: E402
import broker as broker_layer                                       # noqa: E402
import broker_adapter as ba                                         # noqa: E402
import command_authorization as ca                                  # noqa: E402
import command_registry as reg                                      # noqa: E402
import execution_mode as em                                         # noqa: E402
import execution_safety as es                                       # noqa: E402
import execution_store as est                                       # noqa: E402
import order_lifecycle as ol                                        # noqa: E402
import reconciliation as rc                                         # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402
from live.mt5_gateway import MT5Gateway                             # noqa: E402

client = TestClient(server.app)

NOW = datetime(2026, 7, 27, 12, 0, tzinfo=timezone.utc)
NOW_ISO = "2026-07-27T12:00:00Z"


@pytest.fixture(autouse=True)
def isolated_event_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    # Overlay isolation: every test sees the PRISTINE fixture world (earlier
    # tests close/cancel fixture entities through the runtime overlay).
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")


class _Cfg:
    mt5_login = None; mt5_password = None; mt5_server = None
    broker_symbol = "EURUSD.r"; magic_number = 777001


def _bctx():
    return SimpleNamespace(now=NOW_ISO, payload={}, reason=None)


# ── helpers ───────────────────────────────────────────────────────────────────

def _mk_provider(tmp_path, *, account_state="match", kind="mt5", profile_ok=True):
    store = est.ExecutionStore(tmp_path / "s.db")
    provider = ca.DurableOperatorAuthorizationProvider(
        store_fn=lambda: store,
        active_adapter_kind_fn=lambda: kind,
        account_state_fn=lambda: account_state,
        profile_ok_fn=lambda: profile_ok,
        now_iso_fn=lambda: NOW_ISO)
    return provider, store


def _issue(provider, **over):
    kw = dict(operator_ref="op_1", ttl_seconds=3600,
              scopes={"dpl_A"}, account_scope="acctfp_live1",
              risk_classes={es.RISK_EXECUTION_AFFECTING}, commands=None,
              max_quantity=1.0, reason="test grant", confirmed=True, now=NOW)
    kw.update(over)
    return provider.issue(**kw)


def _first_position():
    snap = server._fresh_broker_snapshot()
    assert snap and snap.get("positions"), "fixture world must expose positions"
    p = snap["positions"][0]
    return {"id": p.get("positionId"), "side": p.get("side"),
            "sl": p.get("sl"), "tp": p.get("tp"), "symbol": p.get("canonicalSymbol")}


def _first_order():
    snap = server._fresh_broker_snapshot()
    orders = snap.get("orders") or []
    return ({"id": o.get("orderId")} for o in orders[:1]).__next__() if orders else None


def _tightened_sl(pos):
    """A strictly-tighter (or first) stop for the observed position."""
    if pos["sl"]:
        return pos["sl"] + 0.0001 if pos["side"] == "long" else pos["sl"] - 0.0001
    return 0.0001 if pos["side"] == "long" else 999.0


def _op(path, body, key):
    return client.post(path, json={**body, "confirm": True},
                       headers={"Idempotency-Key": key})


# ── durable authorization provider ────────────────────────────────────────────

def test_grant_issuance_is_durable_and_restart_safe(tmp_path):
    provider, store = _mk_provider(tmp_path)
    g = _issue(provider)
    assert g.provider == "local-operator" and g.adapter_kind == "mt5"
    # Restart: a NEW store handle over the same file still holds the grant + audit.
    store2 = est.ExecutionStore(tmp_path / "s.db")
    rows = store2.grants()
    assert len(rows) == 1 and rows[0]["authorization_id"] == g.authorization_id
    events = store2.grant_events(g.authorization_id)
    assert [e["event"] for e in events] == ["issued"]
    # No secret-looking values in the persisted record.
    assert "password" not in json.dumps(rows[0]).lower()


@pytest.mark.parametrize("override,expected", [
    (dict(confirmed=False), "confirmation_required"),
    (dict(account_scope=""), "account_scope_required"),
    (dict(account_scope="*"), "account_scope_required"),      # no wildcard live scope
    (dict(scopes=set()), "deployment_scope_required"),
    (dict(scopes={"*"}), "deployment_scope_required"),
    (dict(risk_classes=set()), "risk_class_required"),
    (dict(ttl_seconds=0), "invalid_ttl"),
    (dict(ttl_seconds=10 ** 9), "invalid_ttl"),
    (dict(reason=""), "reason_required"),
    (dict(operator_ref=""), "operator_identity_required"),
])
def test_grant_issuance_requirements_each_deny(tmp_path, override, expected):
    provider, _ = _mk_provider(tmp_path)
    with pytest.raises(ca.GrantIssuanceError) as exc:
        _issue(provider, **override)
    assert exc.value.reason == expected


def test_grant_issuance_denies_wrong_adapter_profile_account(tmp_path):
    for kw, expected in ((dict(kind="mock"), "adapter_not_grantable"),
                         (dict(profile_ok=False), "connection_profile_not_approved"),
                         (dict(account_state="unknown"), "account_identity_not_verified"),
                         (dict(account_state="mismatch"), "account_identity_not_verified")):
        sub = tmp_path / str(expected + kw.get("kind", ""))
        sub.mkdir(exist_ok=True)
        provider, _ = _mk_provider(sub, **kw)
        with pytest.raises(ca.GrantIssuanceError) as exc:
            _issue(provider)
        assert exc.value.reason == expected


def test_grant_selection_is_exact_and_deterministic(tmp_path):
    provider, _ = _mk_provider(tmp_path)
    g1 = _issue(provider)
    g2 = _issue(provider, ttl_seconds=1800)      # expires earlier -> selected first
    pick = provider.applicable_grant(now=NOW, command="ClosePosition",
                                     scope="dpl_A", account_fingerprint="acctfp_live1")
    assert pick.authorization_id == g2.authorization_id     # deterministic ordering
    # Exact matching, deny by default:
    assert provider.applicable_grant(now=NOW, scope="dpl_OTHER",
                                     account_fingerprint="acctfp_live1") is None
    assert provider.applicable_grant(now=NOW, scope="dpl_A",
                                     account_fingerprint="acctfp_OTHER") is None
    assert provider.applicable_grant(now=NOW, scope="dpl_A",
                                     account_fingerprint="acctfp_live1",
                                     adapter_kind="mock") is None
    # Revocation and expiry deny:
    provider.revoke(g2.authorization_id, operator_ref="op_1")
    provider.revoke(g1.authorization_id, operator_ref="op_1")
    assert provider.applicable_grant(now=NOW, scope="dpl_A",
                                     account_fingerprint="acctfp_live1") is None


def test_grant_command_and_expiry_bounds(tmp_path):
    provider, _ = _mk_provider(tmp_path)
    g = _issue(provider, commands={"ClosePosition"})
    assert provider.applicable_grant(now=NOW, command="ClosePosition",
                                     scope="dpl_A",
                                     account_fingerprint="acctfp_live1") is not None
    assert provider.applicable_grant(now=NOW, command="SubmitMarketOrder",
                                     scope="dpl_A",
                                     account_fingerprint="acctfp_live1") is None
    late = datetime(2026, 7, 28, tzinfo=timezone.utc)     # past expiry
    assert provider.applicable_grant(now=late, command="ClosePosition",
                                     scope="dpl_A",
                                     account_fingerprint="acctfp_live1") is None


def test_mock_provider_still_cannot_authorize_live(monkeypatch):
    monkeypatch.setenv(ba.VAR_ADAPTER, "mt5")
    monkeypatch.setattr(ba, "_CACHE", {})
    assert server._MOCK_AUTHORIZATION.current_grant(NOW) is None


# ── execution-mode governance ─────────────────────────────────────────────────

def _mk_mode(tmp_path, gates=None):
    store = est.ExecutionStore(tmp_path / "m.db")
    owner = em.ExecutionModeOwner(store_fn=lambda: store,
                                  now_iso_fn=lambda: NOW_ISO,
                                  gates_fn=lambda: gates if gates is not None else {})
    return owner, store


def test_observe_is_the_default_mode(tmp_path):
    owner, _ = _mk_mode(tmp_path)
    assert owner.current_mode() == em.MODE_OBSERVE
    no_store = em.ExecutionModeOwner(store_fn=lambda: None,
                                     now_iso_fn=lambda: NOW_ISO, gates_fn=dict)
    assert no_store.current_mode() == em.MODE_OBSERVE


def test_manual_live_requires_every_gate(tmp_path):
    gates = {"a": True, "b": True, "c": True}
    owner, _ = _mk_mode(tmp_path, gates=gates)
    for broken in ("a", "b", "c"):
        bad = {**gates, broken: False}
        owner2 = em.ExecutionModeOwner(store_fn=owner._store_fn,
                                       now_iso_fn=lambda: NOW_ISO,
                                       gates_fn=lambda: bad)
        with pytest.raises(em.ModeTransitionError) as exc:
            owner2.transition(em.MODE_MANUAL_LIVE, operator_ref="op",
                              reason="go live", confirmed=True)
        assert exc.value.reason == em.DENY_GATE and broken in exc.value.detail
    out = owner.transition(em.MODE_MANUAL_LIVE, operator_ref="op",
                           reason="go live", confirmed=True)
    assert out["toMode"] == em.MODE_MANUAL_LIVE
    assert owner.current_mode() == em.MODE_MANUAL_LIVE


def test_mode_transitions_require_reason_confirmation_operator(tmp_path):
    owner, _ = _mk_mode(tmp_path)
    for kw, reason in ((dict(operator_ref="", reason="r", confirmed=True),
                        em.DENY_OPERATOR_REQUIRED),
                       (dict(operator_ref="op", reason="", confirmed=True),
                        em.DENY_REASON_REQUIRED),
                       (dict(operator_ref="op", reason="r", confirmed=False),
                        em.DENY_CONFIRMATION_REQUIRED)):
        with pytest.raises(em.ModeTransitionError) as exc:
            owner.transition(em.MODE_HALTED, **kw)
        assert exc.value.reason == reason
    with pytest.raises(em.ModeTransitionError):
        owner.transition("turbo", operator_ref="op", reason="r", confirmed=True)


def test_observe_and_halted_are_always_reachable(tmp_path):
    owner, _ = _mk_mode(tmp_path, gates={"g": True})
    owner.transition(em.MODE_MANUAL_LIVE, operator_ref="op", reason="up", confirmed=True)
    owner.transition(em.MODE_HALTED, operator_ref="op", reason="halt", confirmed=True)
    assert owner.current_mode() == em.MODE_HALTED
    owner.transition(em.MODE_OBSERVE, operator_ref="op", reason="down", confirmed=True)
    assert owner.current_mode() == em.MODE_OBSERVE


def test_restart_never_fabricates_manual_live(tmp_path):
    owner, store = _mk_mode(tmp_path, gates={"g": True})
    owner.transition(em.MODE_MANUAL_LIVE, operator_ref="op", reason="up", confirmed=True)
    # A NEW owner (process restart) must downgrade to observe with an audit row.
    owner2 = em.ExecutionModeOwner(store_fn=lambda: store,
                                   now_iso_fn=lambda: NOW_ISO, gates_fn=dict)
    assert owner2.current_mode() == em.MODE_OBSERVE
    last = store.current_mode_row()
    assert last["to_mode"] == em.MODE_OBSERVE
    assert last["reason"] == em.RESTART_RESET_REASON
    # halted DOES survive restart (staying halted is safe).
    owner2.transition(em.MODE_HALTED, operator_ref="op", reason="halt", confirmed=True)
    owner3 = em.ExecutionModeOwner(store_fn=lambda: store,
                                   now_iso_fn=lambda: NOW_ISO, gates_fn=dict)
    assert owner3.current_mode() == em.MODE_HALTED


def test_no_strategy_or_scheduler_can_change_mode():
    for module in ("strategy.py", "scheduler.py"):
        code = code_only(module)
        assert "execution_mode" not in code
        assert "mode_transition" not in code


def test_halted_semantics_in_the_safety_engine():
    ctx = es.SafetyContext(
        mode=es.MODE_HALTED,
        arming=es.ArmingState(armed=True, armed_by="op",
                              expires_at="2099-01-01T00:00:00Z"),
        node=es.NodeSafety(es.NODE_HEALTHY),
        operator=es.OperatorAuthorization(operator_ref="op", confirmed=True),
        account=es.AccountSafety(es.ACCOUNT_MATCH))
    def d(cmd):
        return es.evaluate(es.CommandRequest(command_type=cmd), ctx, now=NOW)
    # Only the two flagged emergency de-risking operations may run in halted:
    assert d("CancelPendingOrder").allowed is True
    assert d("ClosePosition").allowed is True
    # Market orders and protection modification DENY in halted:
    assert d("SubmitMarketOrder").reason == es.DENY_MODE_HALTED
    assert d("ModifyPositionProtection").reason == es.DENY_MODE_HALTED
    # And halted operations still require every other gate (e.g. confirmation):
    unconfirmed = es.SafetyContext(
        mode=es.MODE_HALTED, arming=ctx.arming, node=ctx.node,
        operator=es.OperatorAuthorization(operator_ref="op", confirmed=False),
        account=ctx.account)
    assert es.evaluate(es.CommandRequest(command_type="ClosePosition"),
                       unconfirmed, now=NOW).reason == es.DENY_CONFIRMATION_REQUIRED


def test_manual_live_mode_permits_and_observe_denies():
    base = dict(arming=es.ArmingState(armed=True, armed_by="op",
                                      expires_at="2099-01-01T00:00:00Z"),
                node=es.NodeSafety(es.NODE_HEALTHY),
                operator=es.OperatorAuthorization(operator_ref="op", confirmed=True),
                account=es.AccountSafety(es.ACCOUNT_MATCH))
    allow = es.evaluate(es.CommandRequest(command_type="ModifyPositionProtection"),
                        es.SafetyContext(mode=es.MODE_MANUAL_LIVE, **base), now=NOW)
    assert allow.allowed is True
    deny = es.evaluate(es.CommandRequest(command_type="ModifyPositionProtection"),
                       es.SafetyContext(mode=es.MODE_OBSERVE, **base), now=NOW)
    assert deny.reason == es.DENY_MODE_NOT_ACTIVE


# ── canonical request models ──────────────────────────────────────────────────

def test_operation_requests_are_validated_and_immutable():
    m = ba.ModifyPositionProtectionRequest(intent_id="intent_a", position_ref="1",
                                           instrument="EURUSD", stop_loss=1.1)
    with pytest.raises(Exception):
        m.stop_loss = 2.0
    assert list(m.as_dict()) == sorted(m.as_dict())
    with pytest.raises(ValueError):        # at least one level required
        ba.ModifyPositionProtectionRequest(intent_id="intent_a", position_ref="1",
                                           instrument="EURUSD")
    with pytest.raises(ValueError):        # partial close denies at construction
        ba.ClosePositionRequest(intent_id="intent_a", position_ref="1",
                                instrument="EURUSD", quantity=0.005)
    with pytest.raises(ValueError):
        ba.CancelPendingOrderRequest(intent_id="intent_a", order_ref="",
                                     instrument="EURUSD")
    with pytest.raises(ValueError):
        ba.OperationAck(intent_id="intent_a", operation="close_position",
                        status="nonsense")


# ── the pipeline end-to-end (mock world) ──────────────────────────────────────

def test_modify_protection_acknowledged_then_reconciliation_confirmed():
    pos = _first_position()
    new_sl = _tightened_sl(pos)
    r = _op(f"/api/execution/positions/{pos['id']}/protection",
            {"stopLoss": new_sl}, key="l3-mod-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycleState"] == ol.ACKNOWLEDGED       # ack is NOT final
    assert body["reconciliationConfirmed"] is False
    assert body["acknowledgement"]["status"] == "acknowledged"
    # The entity is locked while awaiting confirmation.
    store = server._execution_store()
    assert any(l["entity_ref"] == pos["id"] for l in store.active_entity_locks())
    # A sync cycle observes the applied change and CONFIRMS the operation.
    client.post("/api/broker/sync", json={})
    row = store.get_intent(body["intentId"])
    assert row["state"] == ol.MODIFIED
    assert not any(l["entity_ref"] == pos["id"] for l in store.active_entity_locks())
    reasons = [t["reason"] for t in store.transitions_of(body["intentId"])]
    assert "broker_acknowledged" in reasons and "reconciliation_confirmed" in reasons


def test_worsening_stop_denies():
    pos = _first_position()
    if not pos["sl"]:
        pytest.skip("fixture position carries no stop to worsen")
    worse = pos["sl"] - 0.01 if pos["side"] == "long" else pos["sl"] + 0.01
    r = _op(f"/api/execution/positions/{pos['id']}/protection",
            {"stopLoss": worse}, key="l3-worse")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "risk_increasing_stop"


def test_protection_requires_a_level_and_cannot_remove():
    pos = _first_position()
    r = _op(f"/api/execution/positions/{pos['id']}/protection", {}, key="l3-nolevel")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "protection_level_required"
    # Removal is structurally impossible: None means UNCHANGED, and zero/negative
    # levels are invalid prices.
    r = _op(f"/api/execution/positions/{pos['id']}/protection",
            {"stopLoss": 0}, key="l3-zero")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "invalid_protective_level"


def test_close_position_acknowledged_then_confirmed_and_partial_denies():
    pos = _first_position()
    r = _op(f"/api/execution/positions/{pos['id']}/close",
            {"quantity": 0.01}, key="l3-partial")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "partial_close_unsupported"
    r = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-close-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycleState"] == ol.ACKNOWLEDGED
    client.post("/api/broker/sync", json={})
    store = server._execution_store()
    assert store.get_intent(body["intentId"])["state"] == ol.CLOSED


def test_cancel_pending_order_flow_or_absence_denies():
    order = _first_order()
    if order is None:
        # No pending order in the fixture: a cancel against a fresh snapshot
        # without the order must deny order_not_found.
        r = _op("/api/execution/orders/ord_missing/cancel", {}, key="l3-cancel-x")
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "order_not_found"
        return
    r = _op(f"/api/execution/orders/{order['id']}/cancel", {}, key="l3-cancel-1")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["lifecycleState"] == ol.ACKNOWLEDGED
    client.post("/api/broker/sync", json={})
    store = server._execution_store()
    assert store.get_intent(body["intentId"])["state"] == ol.CANCELLED


def test_stale_snapshot_denies(monkeypatch):
    pos = _first_position()
    monkeypatch.setattr(server, "_fresh_broker_snapshot", lambda: None)
    r = _op(f"/api/execution/positions/{pos['id']}/protection",
            {"stopLoss": _tightened_sl(pos)}, key="l3-stale")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "stale_broker_snapshot"


def test_confirmation_and_idempotency_key_required():
    pos = _first_position()
    r = client.post(f"/api/execution/positions/{pos['id']}/close", json={"confirm": True})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "idempotency_key_required"
    r = client.post(f"/api/execution/positions/{pos['id']}/close", json={},
                    headers={"Idempotency-Key": "l3-noconfirm"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "confirmation_required"


# ── concurrency and entity locking ────────────────────────────────────────────

def test_same_entity_operations_serialize_close_vs_modify(monkeypatch):
    pos = _first_position()
    close_calls = []
    original_close = broker_layer.MockBroker.close_position
    def counting(self, request, ctx):
        close_calls.append(1)
        return original_close(self, request, ctx)
    monkeypatch.setattr(broker_layer.MockBroker, "close_position", counting)
    # A modify acquires the entity lock and awaits reconciliation confirmation
    # (the entity remains observable, so contention is exercised cleanly).
    r1 = _op(f"/api/execution/positions/{pos['id']}/protection",
             {"stopLoss": _tightened_sl(pos)}, key="l3-race-mod")
    assert r1.status_code == 200
    assert r1.json()["lifecycleState"] == ol.ACKNOWLEDGED   # lock held
    # A close on the SAME entity while the modify is in flight denies — the
    # close-vs-modify race cannot double-dispatch.
    r2 = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-race-close")
    assert r2.status_code == 422
    assert r2.json()["detail"]["code"] == "entity_locked"
    # A second modify under a NEW key also denies.
    r3 = _op(f"/api/execution/positions/{pos['id']}/protection",
             {"stopLoss": _tightened_sl(pos)}, key="l3-race-mod2")
    assert r3.status_code == 422 and r3.json()["detail"]["code"] == "entity_locked"
    assert close_calls == []                                # close NEVER dispatched


def test_duplicate_request_replays_without_contention(monkeypatch):
    pos = _first_position()
    calls = []
    original = broker_layer.MockBroker.close_position
    def counting(self, request, ctx):
        calls.append(1)
        return original(self, request, ctx)
    monkeypatch.setattr(broker_layer.MockBroker, "close_position", counting)
    first = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-dup")
    replay = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-dup")
    assert first.status_code == 200 and replay.status_code == 200
    assert replay.json()["deduplicated"] is True
    assert replay.json()["intentId"] == first.json()["intentId"]
    assert len(calls) == 1


def test_conflicting_payload_under_same_key_denies():
    pos = _first_position()
    sl = _tightened_sl(pos)
    first = _op(f"/api/execution/positions/{pos['id']}/protection",
                {"stopLoss": sl}, key="l3-conflict")
    assert first.status_code == 200
    conflict = _op(f"/api/execution/positions/{pos['id']}/protection",
                   {"stopLoss": sl + 0.0001}, key="l3-conflict")
    assert conflict.status_code == 422
    assert conflict.json()["detail"]["code"] == "idempotency_conflict"


def test_different_entities_are_independent(monkeypatch):
    # Deterministic two-position fixture: the pristine world exposes exactly ONE
    # open ("managing") live trade, so this path previously skipped. Clone that
    # trade under a fresh tradeId in a DEEP-COPIED world and rebind server.WORLD
    # for this test only — every consumer (_fresh_broker_snapshot projection,
    # _trade_current, close overlays) reads server.WORLD at call time, and the
    # runtime overlay DB is already isolated per-test by `isolated_event_store`.
    import copy
    world = copy.deepcopy(server.WORLD)
    open_trades = [t for t in world.get("liveTrades", []) if t.get("state") == "managing"]
    assert open_trades, "pristine fixture must expose one open live trade"
    clone = copy.deepcopy(open_trades[0])
    clone["tradeId"] = "tr_TESTINDEPENDENT0000000000A"
    clone["clientOrderId"] = "cli_TESTINDEPENDENT0000000A"
    clone["brokerOrderId"] = "brk_TESTINDEPENDENT0000000A"
    world["liveTrades"].append(clone)
    monkeypatch.setattr(server, "WORLD", world)

    snap = server._fresh_broker_snapshot()
    positions = snap.get("positions") or []
    assert len(positions) >= 2, f"expected 2 open positions, got {len(positions)}"
    a, b = positions[0]["positionId"], positions[1]["positionId"]
    assert a != b
    r1 = _op(f"/api/execution/positions/{a}/close", {}, key="l3-ind-a")
    r2 = _op(f"/api/execution/positions/{b}/close", {}, key="l3-ind-b")
    assert r1.status_code == 200 and r2.status_code == 200
    # Independence is the point: closing A must not have locked or closed B —
    # both closes succeeded against distinct entities, and each is now closed
    # independently (a further close of each denies for ITS OWN reason).
    r1b = _op(f"/api/execution/positions/{a}/close", {}, key="l3-ind-a2")
    r2b = _op(f"/api/execution/positions/{b}/close", {}, key="l3-ind-b2")
    assert r1b.status_code == 422 and r2b.status_code == 422


def test_ambiguous_outcome_freezes_entity_until_reconciliation(monkeypatch):
    pos = _first_position()
    original = broker_layer.MockBroker.close_position
    def timing_out(self, request, ctx):
        ack = ba.OperationAck(intent_id=request.intent_id, operation="close_position",
                              status=ba.ACK_TIMEOUT, entity_ref=request.position_ref,
                              reason="timeout", provenance="mock-fixture")
        return ba.BrokerResult(ok=False, code="timeout", data=ack.as_dict())
    monkeypatch.setattr(broker_layer.MockBroker, "close_position", timing_out)
    r = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-freeze")
    assert r.status_code == 200
    body = r.json()
    store = server._execution_store()
    assert store.get_intent(body["intentId"])["state"] == ol.RECONCILIATION_REQUIRED
    # The entity stays LOCKED (frozen) — a further mutation denies. (Restore the
    # real close via setattr — never monkeypatch.undo(), which would also undo
    # the shared isolation fixtures.)
    monkeypatch.setattr(broker_layer.MockBroker, "close_position", original)
    r2 = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-freeze2")
    assert r2.status_code == 422 and r2.json()["detail"]["code"] == "entity_locked"
    # Reconciliation resolves it with evidence (position still open -> the close
    # did NOT happen -> failed) and releases the lock.
    client.post("/api/broker/sync", json={})
    assert store.get_intent(body["intentId"])["state"] == ol.FAILED
    assert not any(l["entity_ref"] == pos["id"] for l in store.active_entity_locks())


def test_broker_rejection_releases_the_lock(monkeypatch):
    pos = _first_position()
    def rejecting(self, request, ctx):
        ack = ba.OperationAck(intent_id=request.intent_id, operation="close_position",
                              status=ba.ACK_REJECTED, entity_ref=request.position_ref,
                              reason="broker_rejected", provenance="mock-fixture")
        return ba.BrokerResult(ok=False, code="rejected", data=ack.as_dict())
    monkeypatch.setattr(broker_layer.MockBroker, "close_position", rejecting)
    r = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-rej")
    body = r.json()
    store = server._execution_store()
    assert store.get_intent(body["intentId"])["state"] == ol.REJECTED
    assert not any(l["entity_ref"] == pos["id"] for l in store.active_entity_locks())


def test_same_entity_unresolved_discrepancy_blocks_mutation():
    pos = _first_position()
    store = server._execution_store()
    run = rc.ReconciliationRun(
        recon_id=ol.new_reconciliation_id(), at=NOW_ISO,
        account_identity="acc_mock", expected_account_identity="acc_mock",
        sources={}, discrepancies=(
            rc.Discrepancy(rc.STATUS_MISMATCH, pos["id"], "test discrepancy"),))
    rc.persist(store, run)
    r = _op(f"/api/execution/positions/{pos['id']}/close", {}, key="l3-blocked")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "entity_discrepancy_unresolved"


# ── MT5 typed write mapping (fake SDK) ────────────────────────────────────────

def _connected_mt5(**fake_kwargs):
    fake = _fake_mt5.FakeMT5(tick=_fake_mt5.make_tick(1.10000, 1.10012),
                             symbol_info=_fake_mt5.make_symbol_info(name="EURUSD.r"),
                             **fake_kwargs)
    fake._account = _fake_mt5.make_account()
    gw = MT5Gateway(_Cfg(), sdk=fake)
    ok, _ = gw.connect()
    assert ok
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = gw; adapter._gateway_loaded = True
    adapter._policy_denied_reason = None
    return adapter, fake


def test_mt5_modify_protection_maps_done(monkeypatch):
    adapter, fake = _connected_mt5(
        positions=[_fake_mt5.make_position(9001, 0, 0.1, symbol="EURUSD.r")],
        order_result=_fake_mt5.make_result(_fake_mt5.TRADE_RETCODE_DONE, order=9001))
    req = ba.ModifyPositionProtectionRequest(intent_id="intent_m", position_ref="9001",
                                             instrument="EURUSD", stop_loss=1.09)
    r = adapter.modify_position_protection(req, _bctx())
    assert r.ok and r.code == "ok"
    assert r.data["status"] == "acknowledged" and r.data["provenance"] == "live_mt5"
    assert len(fake.order_send_calls) == 1
    assert fake.order_send_calls[0]["action"] == _fake_mt5.TRADE_ACTION_SLTP
    assert all(isinstance(v, (str, int, float, bool, type(None)))
               for v in r.data.values())                   # no SDK object escapes


def test_mt5_cancel_pending_order_maps_remove(monkeypatch):
    adapter, fake = _connected_mt5(
        order_result=_fake_mt5.make_result(_fake_mt5.TRADE_RETCODE_DONE, order=5005))
    fake._orders = [_fake_mt5.make_order(5005, symbol="EURUSD.r")]
    req = ba.CancelPendingOrderRequest(intent_id="intent_c", order_ref="5005",
                                       instrument="EURUSD")
    r = adapter.cancel_pending_order(req, _bctx())
    assert r.ok and len(fake.order_send_calls) == 1
    assert fake.order_send_calls[0]["action"] == _fake_mt5.TRADE_ACTION_REMOVE


def test_mt5_close_position_full_close_only(monkeypatch):
    adapter, fake = _connected_mt5(
        positions=[_fake_mt5.make_position(9002, 0, 0.2, symbol="EURUSD.r")],
        order_result=_fake_mt5.make_result(_fake_mt5.TRADE_RETCODE_DONE, deal=7007))
    req = ba.ClosePositionRequest(intent_id="intent_cl", position_ref="9002",
                                  instrument="EURUSD")
    r = adapter.close_position(req, _bctx())
    assert r.ok and len(fake.order_send_calls) == 1
    sent = fake.order_send_calls[0]
    assert sent["volume"] == 0.2                            # the OBSERVED volume
    assert sent["position"] == 9002


def test_mt5_action_failure_modes(monkeypatch):
    # rejection
    adapter, fake = _connected_mt5(
        positions=[_fake_mt5.make_position(1, 0, 0.1, symbol="EURUSD.r")],
        order_result=_fake_mt5.make_result(_fake_mt5.TRADE_RETCODE_NO_MONEY))
    req = ba.ClosePositionRequest(intent_id="intent_x", position_ref="1",
                                  instrument="EURUSD")
    r = adapter.close_position(req, _bctx())
    assert r.code == "rejected" and r.data["status"] == "rejected"
    # timeout/ambiguous
    adapter, fake = _connected_mt5(
        positions=[_fake_mt5.make_position(1, 0, 0.1, symbol="EURUSD.r")],
        order_result=_fake_mt5.make_result(_fake_mt5.TRADE_RETCODE_TIMEOUT))
    r = adapter.close_position(req, _bctx())
    assert r.code == "timeout" and r.data["status"] == "timeout"
    # exception -> sanitized communication_failed
    adapter, fake = _connected_mt5(
        positions=[_fake_mt5.make_position(1, 0, 0.1, symbol="EURUSD.r")],
        order_exc=RuntimeError("secret wire detail"))
    r = adapter.close_position(req, _bctx())
    assert r.code == "communication_failed"
    assert "secret wire detail" not in json.dumps(r.data)
    # missing entity -> not_submitted, zero order_send calls
    adapter, fake = _connected_mt5()
    r = adapter.close_position(req, _bctx())
    assert r.code == "not_submitted" and fake.order_send_calls == []
    # disconnected -> not_connected, zero calls
    fake2 = _fake_mt5.FakeMT5()
    gw = MT5Gateway(_Cfg(), sdk=fake2)
    adapter2 = broker_layer.MT5Adapter()
    adapter2._gateway_cache = gw; adapter2._gateway_loaded = True
    adapter2._policy_denied_reason = None
    r = adapter2.close_position(req, _bctx())
    assert r.code == ba.RESULT_NOT_CONNECTED and fake2.order_send_calls == []


# ── reconciliation confirmation unit behaviour ───────────────────────────────

def _ack_intent(store, command, payload, entity_ref):
    intent = ol.OrderIntent(intent_id=ol.new_intent_id(), command_name=command,
                            kind=reg.intent_kind_of(command), created_at=NOW_ISO,
                            metadata={"payload": payload})
    store.create_intent(intent, now=NOW_ISO)
    for state, reason in ((ol.VALIDATED, "validation_passed"), (ol.READY, "safety_allowed"),
                          (ol.PENDING_STATE_BY_KIND[intent.kind], "dispatching_to_adapter"),
                          (ol.ACKNOWLEDGED, "broker_acknowledged")):
        store.record_transition(intent.intent_id, state, at=NOW_ISO, reason=reason)
    store.acquire_entity_lock(entity_ref, intent_id=intent.intent_id,
                              operation=command, now=NOW_ISO)
    return intent


def test_ack_without_observed_change_is_a_discrepancy(tmp_path):
    store = est.ExecutionStore(tmp_path / "c.db")
    intent = _ack_intent(store, "ModifyPositionProtection",
                         {"positionRef": "p1", "stopLoss": 1.2}, "p1")
    snapshot = {"at": NOW_ISO, "positions": [
        {"positionId": "p1", "sl": 1.0, "tp": 2.0}], "orders": []}
    out = rc.confirm_operations(store, snapshot, now=NOW_ISO)
    assert out["confirmed"] == []
    assert any(d["class"] == rc.STATUS_MISMATCH for d in out["discrepancies"])
    assert store.get_intent(intent.intent_id)["state"] == ol.ACKNOWLEDGED
    assert store.entity_lock("p1")["released"] == 0        # still locked


def test_confirmed_modification_and_close_and_cancel(tmp_path):
    store = est.ExecutionStore(tmp_path / "c2.db")
    m = _ack_intent(store, "ModifyPositionProtection",
                    {"positionRef": "p1", "stopLoss": 1.2}, "p1")
    c = _ack_intent(store, "ClosePosition", {"positionRef": "p2"}, "p2")
    x = _ack_intent(store, "CancelPendingOrder", {"orderRef": "o1"}, "o1")
    snapshot = {"at": NOW_ISO,
                "positions": [{"positionId": "p1", "sl": 1.2, "tp": None}],
                "orders": []}                              # p2 closed, o1 gone
    out = rc.confirm_operations(store, snapshot, now=NOW_ISO)
    assert set(out["confirmed"]) == {m.intent_id, c.intent_id, x.intent_id}
    assert store.get_intent(m.intent_id)["state"] == ol.MODIFIED
    assert store.get_intent(c.intent_id)["state"] == ol.CLOSED
    assert store.get_intent(x.intent_id)["state"] == ol.CANCELLED
    assert store.active_entity_locks() == []


def test_stale_snapshot_confirms_nothing(tmp_path):
    store = est.ExecutionStore(tmp_path / "c3.db")
    intent = _ack_intent(store, "ClosePosition", {"positionRef": "p9"}, "p9")
    out = rc.confirm_operations(store, {"at": "2026-07-27T10:00:00Z",
                                        "positions": [], "orders": []}, now=NOW_ISO)
    assert out["stale"] is True and out["confirmed"] == []
    assert store.get_intent(intent.intent_id)["state"] == ol.ACKNOWLEDGED


def test_reconciliation_still_issues_no_corrective_operation():
    code = code_only("reconciliation.py")
    for forbidden in ("submit_market_order", "order_send", "modify_position_protection(",
                      "cancel_pending_order(", "close_position_full"):
        assert forbidden not in code


# ── governed API surface ──────────────────────────────────────────────────────

def test_mode_routes_are_no_store_and_governed():
    r = client.get("/api/execution/mode")
    assert r.status_code == 200
    assert r.headers.get("Cache-Control") == "no-store"
    assert r.json()["mode"] == "observe"                   # default, durable
    deny = client.post("/api/execution/mode",
                       json={"mode": "manual_live", "reason": "x", "confirm": True})
    assert deny.status_code == 422
    assert deny.json()["code"] == "mode_transition_refused" or \
           deny.json().get("error") == "mode_transition_refused"


def test_grant_routes_refuse_in_mock_world_and_are_no_store():
    r = client.get("/api/authorization/grants")
    assert r.status_code == 200 and r.headers.get("Cache-Control") == "no-store"
    issue = client.post("/api/authorization/grants",
                        json={"accountScope": "acctfp_x", "scopes": ["dpl_A"],
                              "reason": "r", "confirm": True})
    assert issue.status_code == 422                        # mock adapter not grantable
    assert issue.json()["code"] == "adapter_not_grantable"


def test_governance_telemetry_is_derived_and_distinct():
    state = client.get("/api/execution/state").json()
    gov = state["governance"]
    assert gov["executionMode"] == "observe"
    assert gov["modeProvenance"] == "durable-store"
    assert "awaitingAcknowledgementConfirmation" in gov["operations"]
    assert "entityLocks" in gov["operations"]


def test_fixture_route_cannot_invoke_live3_operations():
    for name in ("ModifyPositionProtection", "CancelPendingOrder", "ClosePosition"):
        r = client.post(f"/api/commands/{name}", json={})
        assert r.status_code == 400
