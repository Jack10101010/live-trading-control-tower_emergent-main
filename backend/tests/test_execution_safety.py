"""UI-18 — the execution-control safety boundary.

A contract slice: the evaluator is pure, deny-by-default and fail-closed, and it acts
on nothing. These tests pin every invariant — default disarmed denial, read-only
allowance, unknown-command denial, identity/confirmation requirements, arming expiry,
the node-health gate, duplicate/expired denial, the emergency class, fail-closed
exceptions, the immutable redaction-safe audit record, and the absence of any
transport / network / execution dependency.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import execution_safety as es                                       # noqa: E402

NOW = datetime(2026, 7, 26, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def req(command_type, **over):
    kw = {"command_id": "cmd_1",
          "expires_at": _iso(NOW + timedelta(minutes=5))}
    kw.update(over)
    return es.CommandRequest(command_type=command_type, **kw)


def armed(minutes_ahead=5):
    return es.ArmingState(armed=True, armed_by="op-7",
                          expires_at=_iso(NOW + timedelta(minutes=minutes_ahead)))


def operator(ref="op-7", confirmed=True):
    return es.OperatorAuthorization(operator_ref=ref, confirmed=confirmed)


def ctx(**over):
    kw = {"mode": es.MODE_ACTIVE, "arming": armed(), "node": es.NodeSafety(es.NODE_HEALTHY),
          "operator": operator()}
    kw.update(over)
    return es.SafetyContext(**kw)


# ── default disarmed denial ────────────────────────────────────────────────────

def test_the_default_context_is_safe_and_denies_everything_execution():
    default = es.SafetyContext()
    assert default.mode == es.MODE_OBSERVE
    assert default.arming.armed is False
    assert default.node.healthy is False
    d = es.evaluate(req("close_position"), default, now=NOW)
    assert d.allowed is False


def test_an_execution_command_with_no_context_denies():
    d = es.evaluate(req("pause_submission"), now=NOW)   # context omitted → all defaults
    assert d.allowed is False
    assert d.reason == es.DENY_MODE_NOT_ACTIVE           # observe mode is the first gate


def test_disarmed_denies_an_execution_affecting_command():
    d = es.evaluate(req("close_position"), ctx(arming=es.ArmingState()), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_DISARMED


# ── valid read-only allowance ──────────────────────────────────────────────────

@pytest.mark.parametrize("ctype", ["noop", "request_health", "request_telemetry"])
def test_read_only_commands_are_allowed_even_in_the_default_safe_context(ctype):
    d = es.evaluate(req(ctype), es.SafetyContext(), now=NOW)   # observe, disarmed, unknown node
    assert d.allowed is True
    assert d.reason == es.ALLOW_READ_ONLY
    assert d.risk_class == es.RISK_READ_ONLY


def test_a_read_only_command_is_allowed_even_when_a_duplicate():
    d = es.evaluate(req("request_health", duplicate=True), es.SafetyContext(), now=NOW)
    assert d.allowed is True                              # idempotent replay is fine (UI-15)


def test_an_expired_read_only_command_is_still_denied():
    d = es.evaluate(req("noop", expires_at=_iso(NOW - timedelta(minutes=1))),
                    es.SafetyContext(), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_EXPIRED


# ── unknown command denial ──────────────────────────────────────────────────────

@pytest.mark.parametrize("bad", ["definitely_not_a_command", "", None, 123, "NOOP"])
def test_unknown_commands_are_denied(bad):
    d = es.evaluate(req(bad), ctx(), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_UNKNOWN_COMMAND
    assert d.risk_class is None


def test_classify_returns_none_for_unknown_and_the_class_otherwise():
    assert es.classify("kill_switch") == es.RISK_EMERGENCY
    assert es.classify("close_position") == es.RISK_EXECUTION_AFFECTING
    assert es.classify("request_reconcile") == es.RISK_OPERATIONAL
    assert es.classify("request_health") == es.RISK_READ_ONLY
    assert es.classify("nope") is None


# ── missing identity / confirmation ────────────────────────────────────────────

def test_missing_operator_identity_denies():
    d = es.evaluate(req("close_position"),
                    ctx(operator=es.OperatorAuthorization(operator_ref=None, confirmed=True)),
                    now=NOW)
    assert d.allowed is False and d.reason == es.DENY_IDENTITY_REQUIRED


def test_blank_operator_identity_denies():
    d = es.evaluate(req("close_position"),
                    ctx(operator=es.OperatorAuthorization(operator_ref="   ", confirmed=True)),
                    now=NOW)
    assert d.allowed is False and d.reason == es.DENY_IDENTITY_REQUIRED


def test_missing_confirmation_denies():
    d = es.evaluate(req("close_position"),
                    ctx(operator=es.OperatorAuthorization(operator_ref="op-7", confirmed=False)),
                    now=NOW)
    assert d.allowed is False and d.reason == es.DENY_CONFIRMATION_REQUIRED


# ── expired arming ──────────────────────────────────────────────────────────────

def test_expired_arming_denies_with_its_own_reason():
    stale_arm = es.ArmingState(armed=True, armed_by="op-7",
                               expires_at=_iso(NOW - timedelta(seconds=1)))
    d = es.evaluate(req("close_position"), ctx(arming=stale_arm), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_ARMING_EXPIRED


def test_arming_without_an_expiry_is_never_active():
    never = es.ArmingState(armed=True, armed_by="op-7", expires_at=None)
    assert never.is_active(NOW) is False
    d = es.evaluate(req("close_position"), ctx(arming=never), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_DISARMED


def test_arming_expires_automatically_between_two_reads():
    arm = armed(minutes_ahead=1)
    assert arm.is_active(NOW) is True
    assert arm.is_active(NOW + timedelta(minutes=2)) is False       # auto-expired


# ── unhealthy / stale node ─────────────────────────────────────────────────────

@pytest.mark.parametrize("state, reason", [
    (es.NODE_STALE, "node_stale"),
    (es.NODE_DEGRADED, "node_degraded"),
    (es.NODE_DISCONNECTED, "node_disconnected"),
    (es.NODE_UNREACHABLE, "node_unreachable"),
    (es.NODE_UNAUTHORIZED, "node_unauthorized"),
    (es.NODE_DISABLED, "node_disabled"),
    (es.NODE_UNKNOWN, "node_unknown"),
])
def test_an_unhealthy_node_denies_execution_with_a_specific_reason(state, reason):
    d = es.evaluate(req("close_position"), ctx(node=es.NodeSafety(state)), now=NOW)
    assert d.allowed is False and d.reason == reason


def test_an_unrecognised_node_state_denies_generically():
    d = es.evaluate(req("close_position"), ctx(node=es.NodeSafety("martian")), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_NODE_NOT_HEALTHY


def test_a_fully_satisfied_execution_command_is_allowed():
    d = es.evaluate(req("close_position"), ctx(), now=NOW)
    assert d.allowed is True and d.reason == es.ALLOW
    assert d.risk_class == es.RISK_EXECUTION_AFFECTING


# ── duplicate / expired command ────────────────────────────────────────────────

def test_a_duplicate_execution_command_denies_safely():
    d = es.evaluate(req("close_position", duplicate=True), ctx(), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_DUPLICATE


def test_an_expired_execution_command_denies():
    d = es.evaluate(req("close_position", expires_at=_iso(NOW - timedelta(minutes=1))),
                    ctx(), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_EXPIRED


# ── emergency classification ────────────────────────────────────────────────────

def test_an_emergency_command_does_not_require_arming():
    # Disarmed, but everything else satisfied: an emergency STOP is still allowed.
    d = es.evaluate(req("kill_switch"), ctx(arming=es.ArmingState()), now=NOW)
    assert d.allowed is True and d.reason == es.ALLOW
    assert d.risk_class == es.RISK_EMERGENCY


def test_an_emergency_command_still_requires_identity_confirmation_and_health():
    assert es.evaluate(req("kill_switch"),
                       ctx(operator=es.OperatorAuthorization(None, True)), now=NOW).reason \
        == es.DENY_IDENTITY_REQUIRED
    assert es.evaluate(req("kill_switch"),
                       ctx(operator=es.OperatorAuthorization("op", False)), now=NOW).reason \
        == es.DENY_CONFIRMATION_REQUIRED
    assert es.evaluate(req("kill_switch"), ctx(node=es.NodeSafety(es.NODE_STALE)), now=NOW).reason \
        == "node_stale"
    assert es.evaluate(req("kill_switch"), ctx(mode=es.MODE_OBSERVE), now=NOW).reason \
        == es.DENY_MODE_NOT_ACTIVE


def test_operational_requires_confirmation_and_active_mode_but_not_arming():
    d = es.evaluate(req("request_reconcile"), ctx(arming=es.ArmingState()), now=NOW)
    assert d.allowed is True                              # no arming needed for operational
    assert es.evaluate(req("request_reconcile"), ctx(mode=es.MODE_OBSERVE), now=NOW).reason \
        == es.DENY_MODE_NOT_ACTIVE


# ── fail-closed exceptions ──────────────────────────────────────────────────────

def test_a_policy_fault_denies_and_never_raises(monkeypatch):
    # Force an internal fault: classify blows up. The evaluator must convert it to a
    # DENY, not raise and not allow.
    def boom(_ct):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(es, "classify", boom)
    d = es.evaluate(req("request_health"), ctx(), now=NOW)
    assert d.allowed is False and d.reason == es.DENY_POLICY_ERROR


def test_a_malformed_context_denies_rather_than_raising():
    # A hostile arming object whose expiry access raises must not crash the evaluator.
    class Hostile:
        armed = True
        armed_by = None
        @property
        def expires_at(self):
            raise ValueError("nope")
    bad_ctx = es.SafetyContext(mode=es.MODE_ACTIVE, node=es.NodeSafety(es.NODE_HEALTHY),
                               operator=operator())
    object.__setattr__(bad_ctx, "arming", Hostile())
    d = es.evaluate(req("close_position"), bad_ctx, now=NOW)
    assert d.allowed is False                             # denied, not raised
    assert d.reason in (es.DENY_POLICY_ERROR, es.DENY_DISARMED, es.DENY_ARMING_EXPIRED)


def test_no_reason_code_other_than_allow_permits_action():
    # Exhaustive sanity: only ALLOW / ALLOW_READ_ONLY ever accompany allowed=True.
    samples = [
        es.evaluate(req("noop"), es.SafetyContext(), now=NOW),
        es.evaluate(req("close_position"), ctx(), now=NOW),
        es.evaluate(req("kill_switch"), ctx(arming=es.ArmingState()), now=NOW),
        es.evaluate(req("close_position"), es.SafetyContext(), now=NOW),
        es.evaluate(req("unknown_x"), ctx(), now=NOW),
    ]
    for d in samples:
        if d.allowed:
            assert d.reason in (es.ALLOW, es.ALLOW_READ_ONLY)
        else:
            assert d.reason not in (es.ALLOW, es.ALLOW_READ_ONLY)


# ── immutable, redaction-safe audit output ────────────────────────────────────

def test_the_decision_is_immutable():
    d = es.evaluate(req("close_position"), ctx(), now=NOW)
    with pytest.raises(Exception):
        d.allowed = False                                 # frozen dataclass


def test_the_safe_view_is_value_free_and_redacts_detail():
    stale_arm = es.ArmingState(armed=True, expires_at=_iso(NOW - timedelta(seconds=1)))
    d = es.evaluate(req("close_position"), ctx(arming=stale_arm), now=NOW)
    view = d.safe_view()
    assert set(view) >= {"allowed", "reason", "commandType", "riskClass", "commandId",
                         "mode", "armed", "nodeState", "evaluatedAt", "detail"}
    assert view["allowed"] is False
    # No operator ref, no token, no arming secret is present anywhere in the view.
    assert "op-7" not in str(view)


def test_a_secret_shaped_detail_is_masked_in_the_view():
    d = es.SafetyDecision(
        allowed=False, reason=es.DENY_POLICY_ERROR, command_type="close_position",
        risk_class=None, command_id="cmd_x", mode=es.MODE_OBSERVE, armed=False,
        node_state=es.NODE_UNKNOWN, evaluated_at=_iso(NOW),
        detail="token=SUPERSECRETVALUE")
    assert "SUPERSECRETVALUE" not in str(d.safe_view())


# ── no transport / network / execution dependency ────────────────────────────

def test_module_imports_nothing_that_transports_networks_or_executes():
    source = (BACKEND_DIR / "execution_safety.py").read_text()
    for forbidden in ("import transport", "import rest_transport", "import node_client",
                      "import command_transport", "import broker", "import execution",
                      "import socket", "urllib", "requests", "httpx",
                      "default_transport", "order_send", "place_order", "requests.post"):
        assert forbidden not in source, f"execution_safety references {forbidden}"


def test_evaluation_opens_no_socket(monkeypatch):
    import socket
    monkeypatch.setattr(socket, "socket", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("execution_safety opened a socket")))
    es.evaluate(req("close_position"), ctx(), now=NOW)
    es.evaluate(req("noop"), es.SafetyContext(), now=NOW)
    es.evaluate(req("kill_switch"), ctx(), now=NOW)


def test_execution_commands_are_classified_but_not_in_the_ui15_allowlist():
    # Classification exists for future use, but execution commands are NOT submit-able:
    # UI-15's ALLOWED_TYPES stays the read-only trio.
    import command_channel as cc
    for execution_cmd in ("close_position", "cancel_order", "kill_switch", "pause_submission"):
        assert es.classify(execution_cmd) is not None      # classified
        assert execution_cmd not in cc.ALLOWED_TYPES        # but not submit-able
    assert cc.ALLOWED_TYPES == frozenset({"noop", "request_health", "request_telemetry"})
