"""LIVE-2 — the first market-order execution slice.

Proves: canonical request/acknowledgement models, the single execution surface,
deterministic mock execution, the real MT5 write mapping (fake SDK — no broker
object escapes), the full canonical pipeline (validate -> safety -> resolve ->
durability -> dispatch -> lifecycle -> audit), every safety gate independently
denying, idempotency (duplicate/restart/duplicate-ack), reconciliation and
telemetry integration, and graceful failure handling. Every other broker
mutation remains structurally unavailable.
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
import command_registry as reg                                      # noqa: E402
import execution_safety as es                                       # noqa: E402
import order_lifecycle as ol                                        # noqa: E402
import reconciliation as rc                                         # noqa: E402
import server                                                       # noqa: E402
from live.mt5_gateway import MT5Gateway                             # noqa: E402

client = TestClient(server.app)


@pytest.fixture(autouse=True)
def isolated_event_store(tmp_path, monkeypatch):
    """Audit events from market-order submissions must never touch the repo's
    events.db (same isolation pattern as every other event-appending suite)."""
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")


NOW = "2026-07-27T12:00:00Z"
ORDER = {"instrument": "EURUSD", "side": "long", "quantity": 0.01}


class _Cfg:
    mt5_login = None; mt5_password = None; mt5_server = None
    broker_symbol = "EURUSD.r"; magic_number = 777001


def _bctx():
    return SimpleNamespace(now=NOW, payload={}, reason=None)


def _request(intent="intent_live2test0001"):
    return ba.MarketOrderRequest(intent_id=intent, instrument="EURUSD",
                                 side="long", quantity=0.01)


def _connected_mt5(*, order_result=None, order_exc=None, with_symbol=True):
    """MT5Adapter over a connected FAKE SDK scripted for order_send."""
    kwargs = {}
    if order_result is not None:
        kwargs["order_result"] = order_result
    if order_exc is not None:
        kwargs["order_exc"] = order_exc
    fake = _fake_mt5.FakeMT5(
        tick=_fake_mt5.make_tick(1.10000, 1.10012),
        symbol_info=_fake_mt5.make_symbol_info(name="EURUSD.r") if with_symbol else None,
        **kwargs)
    fake._account = _fake_mt5.make_account()
    gw = MT5Gateway(_Cfg(), sdk=fake)
    ok, _ = gw.connect()
    assert ok
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = gw
    adapter._gateway_loaded = True
    adapter._policy_denied_reason = None
    return adapter, fake


# ── canonical models ──────────────────────────────────────────────────────────

def test_market_order_request_is_validated_and_immutable():
    r = _request()
    with pytest.raises(Exception):
        r.side = "short"                                  # frozen
    assert list(r.as_dict()) == sorted(r.as_dict())       # deterministic
    for bad in (dict(side="buy"), dict(quantity=0), dict(quantity=-1),
                dict(quantity=float("nan")), dict(quantity=True),
                dict(stop_loss=-5.0), dict(take_profit=0.0),
                dict(intent="not_an_intent_id"), dict(comment="x" * 65)):
        kw = dict(intent_id="intent_x1", instrument="EURUSD", side="long",
                  quantity=0.01)
        kw.update({("intent_id" if k == "intent" else k): v for k, v in bad.items()})
        with pytest.raises(ValueError):
            ba.MarketOrderRequest(**kw)


def test_market_order_ack_is_canonical_and_immutable():
    ack = ba.MarketOrderAck(intent_id="intent_x", status=ba.ACK_FILLED,
                            broker_order_ticket="123")
    with pytest.raises(Exception):
        ack.status = ba.ACK_REJECTED
    assert list(ack.as_dict()) == sorted(ack.as_dict())
    with pytest.raises(ValueError):
        ba.MarketOrderAck(intent_id="intent_x", status="what")
    assert ba.MarketOrderAck(intent_id="i", status=ba.ACK_TIMEOUT).ambiguous


# ── exactly one executable operation, one surface ─────────────────────────────

def test_exactly_one_execution_surface_command():
    assert reg.execution_command_names() == frozenset({"SubmitMarketOrder"})
    spec = reg.spec_of("SubmitMarketOrder")
    assert spec.broker_dispatched and spec.intent_kind == "submit"
    assert spec.risk_class == es.RISK_EXECUTION_AFFECTING
    assert spec.broker_capability == "supportsMarketExecution"
    # NOT part of the fixture control-plane vocabulary:
    assert "SubmitMarketOrder" not in reg.fixture_command_names()


def test_fixture_route_rejects_the_execution_command():
    r = client.post("/api/commands/SubmitMarketOrder", json=ORDER)
    assert r.status_code == 400                           # unknown to that surface


def test_every_other_broker_mutation_remains_unavailable():
    mt5 = broker_layer.MT5Adapter()
    mt5._gateway_cache = None; mt5._gateway_loaded = True
    mt5._policy_denied_reason = None
    assert mt5.submit_command("CloseTrade", _bctx()) == (None, None)
    assert mt5.cancel_order("1", _bctx()) == (None, None)
    assert mt5.modify_order("1", {}, _bctx()) == (None, None)
    assert mt5.flatten("d", _bctx()) == []
    assert mt5.submit_order(None, _bctx()).code == ba.RESULT_UNAVAILABLE
    assert mt5.close_position("1", _bctx()).code == ba.RESULT_UNAVAILABLE
    caps = broker_layer.capability_dict(mt5.capabilities())
    # Exactly one write capability pair; every other mutation capability absent.
    assert caps["supportsLiveWrite"] is True and caps["supportsMarketExecution"] is True
    for w in ("supportsPendingOrders", "supportsModify", "supportsPartialClose",
              "supportsHedging", "supportsNetting"):
        assert caps[w] is False


# ── mock adapter: deterministic fixture execution ─────────────────────────────

def test_mock_market_order_is_deterministic():
    mock = broker_layer.MockBroker()
    r1 = mock.submit_market_order(_request(), _bctx())
    r2 = mock.submit_market_order(_request(), _bctx())
    assert r1.ok and r1.code == "ok"
    assert r1.broker_ref == r2.broker_ref                 # same request -> same ticket
    assert r1.broker_ref.startswith("mockord_")
    ack = r1.data
    assert ack["status"] == ba.ACK_FILLED
    assert ack["provenance"] == "mock-fixture"
    assert ack["price"] is None                           # no invented market price
    assert list(ack) == sorted(ack)


# ── MT5 adapter: the ONE live write, canonically mapped ───────────────────────

def test_mt5_successful_market_order_maps_canonically():
    adapter, fake = _connected_mt5(order_result=_fake_mt5.make_result(
        _fake_mt5.TRADE_RETCODE_DONE, order=444001, deal=555001,
        price=1.10012, volume=0.01))
    r = adapter.submit_market_order(_request(), _bctx())
    assert len(fake.order_send_calls) == 1                # exactly one order_send
    assert r.ok and r.code == "ok"
    assert r.broker_ref == "444001"                       # ticket mapped, stringified
    ack = r.data
    assert ack["status"] == ba.ACK_FILLED
    assert ack["broker_order_ticket"] == "444001"
    assert ack["broker_deal_ticket"] == "555001"
    assert ack["provenance"] == "live_mt5"
    assert list(ack) == sorted(ack)
    # No MT5/SDK object escapes: plain scalars only.
    assert all(isinstance(v, (str, int, float, bool, type(None)))
               for v in ack.values())
    # The request threaded the intent id to the broker comment (traceability).
    assert fake.order_send_calls[0]["comment"].startswith("intent_")


def test_mt5_broker_rejection_is_canonical():
    adapter, fake = _connected_mt5(order_result=_fake_mt5.make_result(
        _fake_mt5.TRADE_RETCODE_NO_MONEY))
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.ok is False and r.code == "rejected"
    assert r.data["status"] == ba.ACK_REJECTED
    assert r.data["reason"] == "broker_rejected"


def test_mt5_timeout_is_ambiguous_never_guessed():
    adapter, fake = _connected_mt5(order_result=_fake_mt5.make_result(
        _fake_mt5.TRADE_RETCODE_TIMEOUT))
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.ok is False and r.code == "timeout"
    assert r.data["status"] == ba.ACK_TIMEOUT


def test_mt5_order_send_exception_is_communication_failed():
    adapter, fake = _connected_mt5(order_exc=RuntimeError("wire cut"))
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.ok is False and r.code == "communication_failed"
    assert r.data["status"] == ba.ACK_COMMUNICATION_FAILED
    assert "wire cut" not in (r.detail or "")             # no message leakage


def test_mt5_disconnected_terminal_never_calls_order_send():
    fake = _fake_mt5.FakeMT5()
    gw = MT5Gateway(_Cfg(), sdk=fake)                     # never connect()
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = gw; adapter._gateway_loaded = True
    adapter._policy_denied_reason = None
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.code == ba.RESULT_NOT_CONNECTED
    assert fake.order_send_calls == []


def test_mt5_policy_denied_makes_zero_calls():
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = None; adapter._gateway_loaded = True
    adapter._policy_denied_reason = "profile_not_approved"
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.code == "connection_denied" and r.detail == "profile_not_approved"


def test_mt5_normalization_reject_is_not_submitted():
    adapter, fake = _connected_mt5(with_symbol=False)     # no symbol metadata
    r = adapter.submit_market_order(_request(), _bctx())
    assert r.ok is False and r.code == "not_submitted"
    assert r.data["status"] == ba.ACK_NOT_SUBMITTED
    assert fake.order_send_calls == []                    # order_send never ran


# ── the canonical pipeline end-to-end (mock world) ────────────────────────────

def _post_order(key="live2-key-001", body=None):
    return client.post("/api/execution/market-order", json=body or ORDER,
                       headers={"Idempotency-Key": key})


def test_successful_market_order_through_the_pipeline():
    r = _post_order()
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["lifecycleState"] == ol.OPEN
    assert body["brokerTicket"].startswith("mockord_")
    assert body["acknowledgement"]["status"] == ba.ACK_FILLED
    assert body["latencyMs"] is not None and body["latencyMs"] >= 0
    assert body["deduplicated"] is False


def test_lifecycle_transitions_are_the_required_flow():
    r = _post_order(key="live2-flow")
    intent_id = r.json()["intentId"]
    store = server._execution_store()
    transitions = store.transitions_of(intent_id)
    flow = [(t["to_state"], t["reason"]) for t in transitions]
    assert flow == [
        (ol.CREATED, "intent_created"),
        (ol.VALIDATED, "validation_passed"),
        (ol.READY, "safety_allowed"),
        (ol.SUBMITTING, "dispatching_to_adapter"),
        (ol.SUBMITTED, "order_send_accepted"),
        (ol.ACKNOWLEDGED, "broker_acknowledged"),
        (ol.OPEN, "position_open"),
    ]
    # Broker ticket persisted; timestamps monotonic; evidence immutable JSON.
    row = store.get_intent(intent_id)
    assert row["broker_ref"].startswith("mockord_")
    ats = [t["at"] for t in transitions]
    assert ats == sorted(ats)
    final = transitions[-1]
    assert json.loads(final["evidence"])["ack"]["status"] == ba.ACK_FILLED
    assert json.loads(final["evidence"])["brokerLatencyMs"] >= 0


def test_missing_idempotency_key_is_refused():
    r = client.post("/api/execution/market-order", json=ORDER)
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "idempotency_key_required"


def test_invalid_payloads_are_rejected_at_validation():
    for body, code in ((dict(ORDER, side="buy"), "invalid_side"),
                       (dict(ORDER, quantity=0), "invalid_quantity"),
                       (dict(ORDER, instrument=""), "invalid_instrument"),
                       (dict(ORDER, stopLoss=-1), "invalid_protective_level")):
        r = _post_order(key=f"live2-bad-{code}", body=body)
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == code
        assert r.json()["detail"]["stage"] == "validated"


# ── idempotency: a duplicate can never create two broker orders ───────────────

def test_duplicate_submission_replays_the_original_outcome(monkeypatch):
    calls = []
    original = broker_layer.MockBroker.submit_market_order

    def counting(self, request, ctx):
        calls.append(request.intent_id)
        return original(self, request, ctx)
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order", counting)

    first = _post_order(key="live2-dup")
    second = _post_order(key="live2-dup")
    assert first.status_code == 200 and second.status_code == 200
    assert len(calls) == 1                                 # ONE broker order, ever
    assert second.json()["deduplicated"] is True
    assert second.json()["intentId"] == first.json()["intentId"]
    # Exactly one durable intent exists under the key.
    store = server._execution_store()
    rows = [x for x in store.intents_by_state(limit=100)
            if x.get("idempotency_key") == "live2-dup"]
    assert len(rows) == 1


def test_replay_is_restart_safe():
    first = _post_order(key="live2-restart")
    intent_id = first.json()["intentId"]
    # Simulate a process restart: drop the lazy store cache; the durable file
    # is re-opened and recovery runs — the OPEN intent is terminal and untouched.
    server._EXECUTION_STORE = None
    replay = _post_order(key="live2-restart")
    assert replay.json()["deduplicated"] is True
    assert replay.json()["intentId"] == intent_id
    store = server._execution_store()
    assert store.get_intent(intent_id)["state"] == ol.OPEN


def test_duplicate_broker_acknowledgement_is_a_reconciliation_fault():
    run = rc.run_reconciliation(
        tower_intents=[
            {"intent_id": "intent_a", "state": ol.OPEN, "broker_ref": "T1"},
            {"intent_id": "intent_b", "state": ol.OPEN, "broker_ref": "T1"},
        ],
        broker_orders=[], broker_positions=[],
        broker_account_identity="acct", expected_account_identity="acct",
        broker_snapshot_at=NOW, now=NOW)
    assert any(d.cls == rc.DUPLICATE_BROKER_REFERENCE for d in run.discrepancies)


# ── failure handling through the pipeline ─────────────────────────────────────

def test_broker_rejection_through_the_pipeline(monkeypatch):
    def rejecting(self, request, ctx):
        ack = ba.MarketOrderAck(intent_id=request.intent_id, status=ba.ACK_REJECTED,
                                reason="broker_rejected", provenance="mock-fixture")
        return ba.BrokerResult(ok=False, code="rejected", detail="NO_MONEY",
                               data=ack.as_dict())
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order", rejecting)
    r = _post_order(key="live2-reject")
    assert r.status_code == 200                            # pipeline completed; outcome recorded
    body = r.json()
    assert body["ok"] is False and body["lifecycleState"] == ol.REJECTED
    store = server._execution_store()
    reasons = [t["reason"] for t in store.transitions_of(body["intentId"])]
    assert "broker_rejected" in reasons


def test_timeout_through_the_pipeline_queues_reconciliation(monkeypatch):
    def timing_out(self, request, ctx):
        ack = ba.MarketOrderAck(intent_id=request.intent_id, status=ba.ACK_TIMEOUT,
                                reason="timeout", provenance="mock-fixture")
        return ba.BrokerResult(ok=False, code="timeout", data=ack.as_dict())
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order", timing_out)
    r = _post_order(key="live2-timeout")
    body = r.json()
    assert body["lifecycleState"] == ol.RECONCILIATION_REQUIRED
    store = server._execution_store()
    states = [t["to_state"] for t in store.transitions_of(body["intentId"])]
    assert ol.UNKNOWN in states                            # never guessed


def test_store_failure_denies_before_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order",
                        lambda self, req, ctx: calls.append(1))
    monkeypatch.setattr(server, "_EXECUTION_STORE", None)
    monkeypatch.setattr(server, "_EXECUTION_STORE_FAILED", True)
    r = _post_order(key="live2-nostore")
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "execution_store_unavailable"
    assert calls == []                                     # broker untouched


def test_denied_submission_appends_an_audit_event(monkeypatch):
    monkeypatch.setattr(server, "_EXECUTION_STORE", None)
    monkeypatch.setattr(server, "_EXECUTION_STORE_FAILED", True)
    before = len(server._stored_events())
    _post_order(key="live2-audit-deny")
    events = server._stored_events()
    assert len(events) == before + 1
    assert events[-1]["code"] == "SUBMIT_MARKET_ORDER_DENIED"


# ── safety: every gate independently denies ───────────────────────────────────

def _allow_ctx():
    """A context in which SubmitMarketOrder is ALLOWED — each gate test breaks
    exactly one fact and must flip the decision to a specific deny."""
    return es.SafetyContext(
        mode=es.MODE_ACTIVE,
        arming=es.ArmingState(armed=True, armed_by="op",
                              expires_at="2099-01-01T00:00:00Z"),
        node=es.NodeSafety(es.NODE_HEALTHY),
        operator=es.OperatorAuthorization(operator_ref="op_1", confirmed=True),
        reconciliation=es.ReconciliationSafety(critical_unresolved=False, stale=False),
        account=es.AccountSafety(es.ACCOUNT_MATCH),
    )


def _eval(ctx, **req):
    request = es.CommandRequest(command_type="SubmitMarketOrder", **req)
    return es.evaluate(request, ctx, now=datetime(2026, 7, 27, tzinfo=timezone.utc))


def test_the_allow_context_allows():
    assert _eval(_allow_ctx()).allowed is True


@pytest.mark.parametrize("mutate,expected", [
    (lambda c: es.SafetyContext(mode=es.MODE_OBSERVE, arming=c.arming, node=c.node,
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=c.account), es.DENY_MODE_NOT_ACTIVE),
    (lambda c: es.SafetyContext(mode=es.MODE_SUSPENDED, arming=c.arming, node=c.node,
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=c.account), es.DENY_MODE_NOT_ACTIVE),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming, node=c.node,
                                operator=es.OperatorAuthorization(),
                                reconciliation=c.reconciliation, account=c.account),
     es.DENY_IDENTITY_REQUIRED),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming, node=c.node,
                                operator=es.OperatorAuthorization(operator_ref="op_1",
                                                                  confirmed=False),
                                reconciliation=c.reconciliation, account=c.account),
     es.DENY_CONFIRMATION_REQUIRED),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming,
                                node=es.NodeSafety(es.NODE_STALE),
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=c.account), "node_stale"),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming,
                                node=es.NodeSafety(es.NODE_UNKNOWN),
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=c.account), "node_unknown"),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming, node=c.node,
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=es.AccountSafety(es.ACCOUNT_MISMATCH)),
     es.DENY_ACCOUNT_MISMATCH),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming, node=c.node,
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=es.AccountSafety(es.ACCOUNT_UNKNOWN)),
     es.DENY_ACCOUNT_UNKNOWN),
    (lambda c: es.SafetyContext(mode=c.mode, arming=c.arming, node=c.node,
                                operator=c.operator,
                                reconciliation=es.ReconciliationSafety(
                                    critical_unresolved=True),
                                account=c.account), es.DENY_RECONCILIATION_REQUIRED),
    (lambda c: es.SafetyContext(mode=c.mode, arming=es.ArmingState(), node=c.node,
                                operator=c.operator, reconciliation=c.reconciliation,
                                account=c.account), es.DENY_DISARMED),
    (lambda c: es.SafetyContext(mode=c.mode,
                                arming=es.ArmingState(armed=True, armed_by="op",
                                                      expires_at="2020-01-01T00:00:00Z"),
                                node=c.node, operator=c.operator,
                                reconciliation=c.reconciliation, account=c.account),
     es.DENY_ARMING_EXPIRED),
])
def test_each_gate_independently_denies_the_market_order(mutate, expected):
    decision = _eval(mutate(_allow_ctx()))
    assert decision.allowed is False and decision.reason == expected


def test_duplicate_and_expired_commands_deny():
    assert _eval(_allow_ctx(), duplicate=True).reason == es.DENY_DUPLICATE
    assert _eval(_allow_ctx(),
                 expires_at="2020-01-01T00:00:00Z").reason == es.DENY_EXPIRED


def test_unknown_and_missing_state_deny():
    # An entirely default context (unknown node, observe, no operator) denies.
    assert _eval(es.SafetyContext()).allowed is False
    # An unknown command has no class and denies.
    d = es.evaluate(es.CommandRequest(command_type="SubmitMarketOrderV2"),
                    _allow_ctx())
    assert d.allowed is False and d.reason == es.DENY_UNKNOWN_COMMAND


def test_mock_authorization_cannot_reach_a_live_adapter(monkeypatch):
    """The permissive mock grant structurally cannot authorize execution against
    the MT5 adapter: the non-mock context carries NO grant, observe mode and an
    unevaluated-deny account state — the market order denies at safety."""
    monkeypatch.setenv(ba.VAR_ADAPTER, "mt5")
    monkeypatch.setattr(ba, "_CACHE", {})
    ctx = server._execution_context()
    assert ctx.authorization is None                       # no grant, structurally
    d = es.evaluate(es.CommandRequest(command_type="SubmitMarketOrder"),
                    ctx.to_safety_context("SubmitMarketOrder"))
    assert d.allowed is False


def test_write_capability_and_profile_and_connection_are_required(monkeypatch):
    import connection_policy as cpol
    import execution as ex
    env = SimpleNamespace(
        deployment=lambda i: None, trade=lambda i: None, order=lambda i: None,
        active_package_version=lambda: "1", broker_connection=lambda: "Connected",
        broker_capabilities=lambda: {"supportsMarketExecution": False})
    # capability absent -> missing_capability
    v = ex.v_capability("supportsMarketExecution")(ORDER, env)
    assert not v.ok and v.code == "missing_capability"
    # broker disconnected -> broker_disconnected
    env2 = SimpleNamespace(**{**env.__dict__, "broker_connection": lambda: "Disconnected"})
    v = ex.v_broker_connected(ORDER, env2)
    assert not v.ok and v.code == "broker_disconnected"
    # unapproved profile -> connection_profile_not_approved
    monkeypatch.setattr(cpol, "active_profile", lambda env=None: "remote_live")
    v = ex.v_approved_connection_profile(ORDER, env)
    assert not v.ok and v.code == "connection_profile_not_approved"


def test_no_dispatch_happens_on_a_safety_deny(monkeypatch):
    import execution_context as ec
    calls = []
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order",
                        lambda self, req, ctx: calls.append(1))
    # Force the safety stage to deny: the orchestrator holds the context factory
    # captured at construction, so patch it there (observe-mode default denies).
    monkeypatch.setattr(server._ORCHESTRATOR, "_safety_context_factory",
                        lambda: ec.ExecutionContext())
    r = _post_order(key="live2-deny-dispatch")
    assert r.status_code == 422
    assert r.json()["detail"]["stage"] == "safety"
    assert calls == []                                     # broker never touched


# ── full simulated-live integration: every gate satisfied -> ONE order_send ──

def test_fully_authorized_live_simulation_submits_exactly_once(monkeypatch):
    import command_authorization as ca
    import execution_context as ec
    adapter, fake = _connected_mt5(order_result=_fake_mt5.make_result(
        _fake_mt5.TRADE_RETCODE_DONE, order=990001, deal=990002,
        price=1.10012, volume=0.01))
    monkeypatch.setattr(broker_layer, "get_broker", lambda kind=None: adapter)
    now = datetime.now(timezone.utc)
    grant = ca.AuthorizationGrant(
        authorization_id=ca.new_authorization_id(), operator_ref="op_live",
        issued_at="2026-01-01T00:00:00Z", expires_at="2099-01-01T00:00:00Z",
        allowed_risk_classes=frozenset({es.RISK_EXECUTION_AFFECTING}),
        allowed_scopes=frozenset({ca.SCOPE_ANY}), confirmed=True,
        provider="test-simulated-live")
    ctx = ec.ExecutionContext(
        execution_mode=es.MODE_ACTIVE,
        operator=es.OperatorAuthorization(operator_ref="op_live", confirmed=True),
        authorization=grant,
        reconciliation=ec.ReconciliationFacts(critical_unresolved=False, stale=False),
        account_identity_state=es.ACCOUNT_MATCH,
        node=ec.NodeFacts(health=es.NODE_HEALTHY, stale=False,
                          provenance=ec.PROV_MOCK),
        broker_kind="mt5", broker_connection="Connected",
        broker_capabilities=frozenset({"supportsLiveWrite", "supportsMarketExecution"}),
        observed_at=NOW)
    # The orchestrator holds the factory captured at construction — patch THERE,
    # so the simulated live context is genuinely what authorizes the dispatch.
    monkeypatch.setattr(server._ORCHESTRATOR, "_safety_context_factory", lambda: ctx)
    first = _post_order(key="live2-sim-live")
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["lifecycleState"] == ol.OPEN
    assert body["brokerTicket"] == "990001"
    assert body["acknowledgement"]["provenance"] == "live_mt5"
    assert len(fake.order_send_calls) == 1
    # Duplicate against the SAME live environment: still exactly one broker order.
    second = _post_order(key="live2-sim-live")
    assert second.json()["deduplicated"] is True
    assert len(fake.order_send_calls) == 1


# ── reconciliation integration ────────────────────────────────────────────────

def test_open_market_order_with_matching_position_reconciles_clean():
    run = rc.run_reconciliation(
        tower_intents=[{"intent_id": "intent_a", "state": ol.OPEN,
                        "broker_ref": "990001"}],
        broker_orders=[],
        broker_positions=[{"positionId": "990001", "_fixture_lineage": False}],
        broker_account_identity="acct", expected_account_identity="acct",
        broker_snapshot_at=NOW, now=NOW)
    assert run.clean, [d.as_record() for d in run.discrepancies]


def test_acknowledgement_missing_is_detected():
    run = rc.run_reconciliation(
        tower_intents=[{"intent_id": "intent_a", "state": ol.OPEN,
                        "broker_ref": None}],
        broker_orders=[], broker_positions=[],
        broker_account_identity="acct", expected_account_identity="acct",
        broker_snapshot_at=NOW, now=NOW)
    assert any(d.cls == rc.MISSING_BROKER_REFERENCE for d in run.discrepancies)


def test_broker_only_position_is_detected():
    run = rc.run_reconciliation(
        tower_intents=[], broker_orders=[],
        broker_positions=[{"positionId": "777", "_fixture_lineage": False}],
        broker_account_identity="acct", expected_account_identity="acct",
        broker_snapshot_at=NOW, now=NOW)
    assert any(d.cls == rc.BROKER_ONLY_POSITION for d in run.discrepancies)


def test_tower_only_order_is_detected():
    run = rc.run_reconciliation(
        tower_intents=[{"intent_id": "intent_a", "state": ol.SUBMITTED,
                        "broker_ref": "R1"}],
        broker_orders=[], broker_positions=[],
        broker_account_identity="acct", expected_account_identity="acct",
        broker_snapshot_at=NOW, now=NOW)
    assert any(d.cls == rc.TOWER_ONLY_ORDER for d in run.discrepancies)


def test_reconciliation_takes_no_corrective_action():
    from conftest import code_only
    code = code_only("reconciliation.py")
    for forbidden in ("submit_market_order", "submit_command", "order_send",
                      "close_position(", "cancel_order("):
        assert forbidden not in code


# ── telemetry integration ─────────────────────────────────────────────────────

def test_market_order_telemetry_derives_from_the_store():
    r = _post_order(key="live2-telemetry")
    intent_id = r.json()["intentId"]
    state = client.get("/api/execution/state").json()
    mo = state["marketOrder"]
    assert mo["available"] is True and mo["provenance"] == "durable-store"
    assert mo["activeMarketOrders"] >= 1
    assert mo["lastSubmission"]["intentId"] == intent_id
    assert mo["lastSubmission"]["state"] == ol.OPEN
    assert mo["lastSubmission"]["latencyMs"] is not None
    assert mo["lastBrokerTicket"] == r.json()["brokerTicket"]


def test_market_order_telemetry_reports_unavailable_store_explicitly(monkeypatch):
    monkeypatch.setattr(server, "_EXECUTION_STORE", None)
    monkeypatch.setattr(server, "_EXECUTION_STORE_FAILED", True)
    state = client.get("/api/execution/state").json()
    assert state["marketOrder"]["available"] is False
    assert state["marketOrder"]["code"] == "execution_store_unavailable"


def test_rejection_reason_is_exposed_in_telemetry(monkeypatch):
    def rejecting(self, request, ctx):
        ack = ba.MarketOrderAck(intent_id=request.intent_id, status=ba.ACK_REJECTED,
                                reason="broker_rejected", provenance="mock-fixture")
        return ba.BrokerResult(ok=False, code="rejected", data=ack.as_dict())
    monkeypatch.setattr(broker_layer.MockBroker, "submit_market_order", rejecting)
    _post_order(key="live2-telemetry-reject")
    mo = client.get("/api/execution/state").json()["marketOrder"]
    assert mo["lastSubmission"]["state"] == ol.REJECTED
    assert mo["lastSubmission"]["finalReason"] == "broker_rejected"
    assert mo["submissionFailures"] >= 1
