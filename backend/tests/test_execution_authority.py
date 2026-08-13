"""ARCH-1 — the single, canonical execution authority.

Proves the unification: one command registry, one policy engine, one execution
pipeline, no fail-open, no duplicate ownership, broker still mock-only. Everything
here runs in-process against the fixture/mock world; nothing connects.
"""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import broker as broker_layer                                        # noqa: E402
import command_channel as cc                                        # noqa: E402
import command_registry as reg                                      # noqa: E402
import execution as ex                                              # noqa: E402
import execution_safety as es                                       # noqa: E402
import server                                                       # noqa: E402
from conftest import code_only                                      # noqa: E402

client = TestClient(server.app)

FIXTURE_DEPLOYMENT = "dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ"
NOW = "2026-07-27T00:00:00Z"


@pytest.fixture(autouse=True)
def isolated_event_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())


# ── orchestrator test harness (no server, no broker I/O) ──────────────────────

def _env(**over):
    base = dict(
        deployment=lambda i: {"status": "Armed"},
        trade=lambda i: {"state": "open"},
        order=lambda i: {"id": i},
        active_package_version=lambda: 1,
        broker_connection=lambda: "Connected",
        broker_capabilities=lambda: {"supportsModify": True},
    )
    base.update(over)
    return ex.ExecutionEnv(**base)


def _permissive_context():
    return es.SafetyContext(
        mode=es.MODE_ACTIVE,
        arming=es.ArmingState(armed=True, armed_by="test",
                              expires_at="2099-01-01T00:00:00Z"),
        node=es.NodeSafety(es.NODE_HEALTHY),
        operator=es.OperatorAuthorization(operator_ref="op-test", confirmed=True),
    )


def _orchestrator(dispatch, *, context_factory):
    return ex.ExecutionOrchestrator(
        dispatch=dispatch, env_factory=_env,
        metrics=ex.ExecutionMetrics(), safety_context_factory=context_factory)


# ── 1. one command registry — uniqueness + no duplicate ownership ─────────────

def test_registry_has_no_duplicate_canonical_names_or_aliases():
    # The live registry built without raising; re-run the builder to confirm the
    # guard is real, then prove it fires on a duplicate and on an alias collision.
    reg._build_index()  # does not raise

    dup = list(reg._ALL_SPECS) + [reg.CommandSpec("CloseTrade", es.RISK_EMERGENCY,
                                                   reg.SURFACE_FIXTURE, category="risk")]
    with pytest.raises(ValueError, match="duplicate canonical"):
        _rebuild_with(dup)

    collide = list(reg._ALL_SPECS) + [
        reg.CommandSpec("BrandNewCmd", es.RISK_OPERATIONAL, reg.SURFACE_ABSTRACT,
                        aliases=frozenset({"CloseTrade"}))]
    with pytest.raises(ValueError, match="collides"):
        _rebuild_with(collide)


def _rebuild_with(specs, monkeypatch=None):
    saved = reg._ALL_SPECS
    reg._ALL_SPECS = tuple(specs)
    try:
        reg._build_index()
    finally:
        reg._ALL_SPECS = saved


def test_every_backend_vocabulary_derives_from_the_one_registry():
    # KNOWN_COMMANDS, BROKER_COMMANDS, categories, and the read-only trio all come
    # from the registry — there is no second, independent command list to drift.
    assert server.KNOWN_COMMANDS == reg.fixture_command_names()
    assert broker_layer.BROKER_COMMANDS == reg.broker_dispatched_names()
    assert server._COMMAND_CATEGORY == reg.fixture_categories()
    assert cc.ALLOWED_TYPES == reg.operator_command_names()


def test_aliases_resolve_to_their_canonical_command():
    assert reg.resolve("close_position") == "CloseTrade"
    assert reg.resolve("kill_switch") == "GlobalKill"
    assert reg.resolve("pause_submission") == "PauseDeployment"
    assert reg.resolve("cancel_order") == "CancelOrder"
    # classification follows the alias to the canonical entry's class
    assert es.classify("close_position") == es.RISK_EXECUTION_AFFECTING
    assert es.classify("kill_switch") == es.RISK_EMERGENCY
    # a canonical name resolves to itself; an unknown resolves to nothing
    assert reg.resolve("CloseTrade") == "CloseTrade"
    assert reg.resolve("not_a_command") is None


# ── 2. one policy engine — no fail-open, every path reaches evaluate() ────────

def test_the_orchestrator_calls_execution_safety_evaluate_on_every_command(monkeypatch):
    seen: list[str] = []
    real = es.evaluate

    def spy(request, context=None, *, now=None):
        seen.append(request.command_type)
        return real(request, context, now=now)

    monkeypatch.setattr(ex.safety, "evaluate", spy)
    orch = _orchestrator(lambda *a: ({}, {}), context_factory=_permissive_context)
    orch.execute("PauseDeployment", {"deploymentId": "d"}, NOW)
    assert seen == ["PauseDeployment"], "safety gate was not consulted exactly once"


def test_broker_dispatch_is_unreachable_without_a_safety_allow():
    """Deny context → the command is denied at the SAFETY stage and dispatch is never
    invoked. This is the core anti-bypass property."""
    dispatched: list = []

    def dispatch(name, payload, now, dry):
        dispatched.append(name)
        return ({}, {})

    # Default SafetyContext is deny-by-default (observe mode, disarmed, unknown node).
    orch = _orchestrator(dispatch, context_factory=es.SafetyContext)
    result = orch.execute("CloseTrade", {"tradeId": "t"}, NOW)

    assert result.status == "denied"
    assert result.stage == ex.STAGE_SAFETY
    assert result.reason == es.DENY_MODE_NOT_ACTIVE      # observe mode denies first
    assert dispatched == [], "dispatch ran despite a safety DENY"


def test_unknown_commands_deny_and_never_dispatch():
    dispatched: list = []
    orch = _orchestrator(lambda n, p, w, d: dispatched.append(n) or ({}, {}),
                         context_factory=_permissive_context)
    result = orch.execute("TotallyUnknownCommand", {}, NOW)
    assert result.status == "rejected"
    assert result.stage == ex.STAGE_VALIDATED
    assert result.code == "unknown_command"
    assert dispatched == []


def test_there_is_no_fail_open_allow_in_the_orchestrator():
    """The Audit A fail-open was `PolicyResult(True, "none")`. It must be gone; a
    command with no feasibility rule now resolves to an EXPLICIT named constant, and
    only after an explicit safety allow."""
    source = code_only("execution.py")
    assert 'PolicyResult(True, "none")' not in source
    assert "_NO_FEASIBILITY_CONSTRAINT" in source
    # and a no-feasibility command still passes the safety gate first
    orch = _orchestrator(lambda *a: ({}, {}), context_factory=_permissive_context)
    r = orch.execute("LockDeployment", {"deploymentId": "d"}, NOW)
    assert r.status == "completed"
    assert any(s["stage"] == ex.STAGE_SAFETY and s["ok"] for s in r.stages)


def test_missing_registry_entry_denies_rather_than_allows():
    """A command absent from the registry cannot reach dispatch — it is unknown."""
    assert reg.risk_class_of("ghost_command") is None
    d = es.evaluate(es.CommandRequest(command_type="ghost_command"))
    assert d.allowed is False and d.reason == es.DENY_UNKNOWN_COMMAND


# ── 3. explicit pipeline stages, in order ─────────────────────────────────────

def test_the_pipeline_stages_are_explicit_and_ordered():
    orch = _orchestrator(lambda *a: ({}, {}), context_factory=_permissive_context)
    r = orch.execute("PauseDeployment", {"deploymentId": "d"}, NOW)
    order = [s["stage"] for s in r.stages]
    assert order == [
        ex.STAGE_RECEIVED, ex.STAGE_VALIDATED, ex.STAGE_SAFETY, ex.STAGE_RESOLVED,
        ex.STAGE_BROKER_DISPATCH, ex.STAGE_BROKER_RESULT, ex.STAGE_RUNTIME_UPDATE,
        ex.STAGE_AUDIT_EVENT, ex.STAGE_COMPLETED,
    ]
    # Safety strictly precedes broker dispatch.
    assert order.index(ex.STAGE_SAFETY) < order.index(ex.STAGE_BROKER_DISPATCH)


# ── 4. broker isolation — mock only, MT5 inert, _ACTIVE unchanged ─────────────

def test_active_broker_is_still_mock_and_pipeline_terminates_in_it():
    assert broker_layer._ACTIVE == "mock"
    assert broker_layer.active_kind() == "mock"
    r = client.post(f"/api/commands/PauseDeployment", json={"deploymentId": FIXTURE_DEPLOYMENT})
    assert r.status_code == 200
    assert r.json()["mode"] == "mock"
    assert broker_layer.get_broker().kind == "mock"


def test_the_safety_context_is_permissive_only_for_the_mock_broker(monkeypatch):
    """The permissive context that lets the fixture world work is gated on the mock.
    Flip the active broker and the pipeline denies by default — no real broker can be
    dispatched to on the strength of the mock context."""
    # mock → permissive (execution-affecting allowed)
    ctx = server._safety_context()
    assert ctx.mode == es.MODE_ACTIVE and ctx.arming.armed is True

    monkeypatch.setattr(broker_layer, "active_kind", lambda: "mt5")
    ctx = server._safety_context()
    assert ctx.mode == es.MODE_OBSERVE and ctx.arming.armed is False   # deny-by-default
    d = es.evaluate(es.CommandRequest(command_type="CloseTrade"), ctx,
                    now=__import__("datetime").datetime(2026, 7, 27,
                        tzinfo=__import__("datetime").timezone.utc))
    assert d.allowed is False


# ── 5. read-only operator behaviour preserved ────────────────────────────────

def test_read_only_operator_vocabulary_is_unchanged_and_never_broker_dispatched():
    assert cc.ALLOWED_TYPES == frozenset({"noop", "request_health", "request_telemetry"})
    for name in cc.ALLOWED_TYPES:
        assert reg.risk_class_of(name) == es.RISK_READ_ONLY
        assert not reg.is_broker_dispatched(name)
        assert reg.spec_of(name).surface == reg.SURFACE_OPERATOR


def test_operator_command_validation_still_accepts_only_the_read_only_trio():
    now = __import__("datetime").datetime(2026, 7, 27, tzinfo=__import__("datetime").timezone.utc)
    good = {"schema_version": cc.SCHEMA_VERSION, "command_type": "request_health",
            "idempotency_key": "k", "requested_at": "2026-07-27T00:00:00Z",
            "expires_at": "2026-07-27T00:05:00Z", "payload": {}}
    env = cc.validate_envelope(good, now=now)
    assert env.command_type == "request_health"
    bad = dict(good, command_type="CloseTrade", idempotency_key="k2")
    with pytest.raises(cc.CommandError) as exc:
        cc.validate_envelope(bad, now=now)
    assert exc.value.reason == cc.REASON_UNKNOWN_TYPE
