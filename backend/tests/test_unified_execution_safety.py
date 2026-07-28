"""UES — one financial policy for both execution paths.

WHAT THIS DEFENDS
    Two paths reached the same broker submission call:

        CT:     API -> Orchestrator -> execution_safety -> MT5Adapter -> gateway
        runner: Golden -> Executor -> SafetyRails      -> gateway

    Only the runner evaluated `SafetyRails`. The kill switch, daily-loss limit,
    max-open cap and symbol whitelist were therefore enforced on one path and
    not the other — and `FIRST_LIVE_TRADE_RUNBOOK.md` routes the first live
    trade through the unguarded one.

    The property under test is NOT "both paths behave identically" — they must
    not. It is:

        both paths obey ONE financial policy, and every difference between them
        is a deliberate, stated exemption rather than an accident of wiring.

THE EXEMPTION THAT MATTERS MOST
    Reducing risk must never be blocked by a financial limit. A daily-loss
    breach is a reason to stop OPENING; it is never a reason to trap an operator
    in a position they are trying to exit. The runner already encodes this by
    exempting CLOSE_POSITION from the kill switch, and these tests pin the same
    semantics for the manual equivalents. Several of them exist specifically to
    fail if a future change makes the rails "safer" by blocking a close.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import execution_safety as es
import manual_financial_rails as mfr


# ── a controllable stand-in for the live rails ───────────────────────────────

class FakeRails:
    """Mimics `live.safety.SafetyRails` at the surface the adapter uses.

    A fake rather than a real `SafetyRails` because the real one needs a live
    config, a state directory and a kill file on disk. The adapter's contract
    with it is exactly two calls, and those are what is faked; the real rails'
    own behaviour is covered by the live-slice suite and asserted unchanged by
    `test_autonomous_evaluate_is_untouched` below.
    """

    def __init__(self, *, kill=False, blocked_rail=None, detail=""):
        self.kill = kill
        self.blocked_rail = blocked_rail
        self.detail = detail
        self.evaluate_calls = []

    def kill_switch_engaged(self):
        return self.kill

    def evaluate(self, intent, symbol, today, market, health):
        from live.safety import RailVerdict
        self.evaluate_calls.append((intent.action, symbol, today))
        if self.kill and intent.action != "CLOSE_POSITION":
            return RailVerdict(False, "kill_switch", "kill file present")
        if self.blocked_rail:
            return RailVerdict(False, self.blocked_rail, self.detail)
        return RailVerdict(True, "allowed")


def evaluator(rails, *, instrument="EURUSD", today="2026-07-28"):
    return mfr.build_evaluator(rails, today=today, instrument=instrument)


# ── manual OPEN: fully gated ─────────────────────────────────────────────────

@pytest.mark.parametrize("rail,detail", [
    ("kill_switch", "kill file present"),
    ("daily_loss_limit", "realized -5.20R <= -5.0R"),
    ("max_open_positions", "mirror at 6"),
    ("symbol_whitelist", "GBPUSD != EURUSD"),
    ("account_health", "free margin below floor"),
    ("market_condition", "spread too wide"),
    ("duplicate_intent", "already SENT"),
])
def test_manual_open_is_blocked_by_every_financial_rail(rail, detail):
    """Each rail the runner enforces on an OPEN now also refuses a manual OPEN,
    and the operator is told WHICH limit stopped them."""
    decide = evaluator(FakeRails(blocked_rail=rail, detail=detail))
    verdict = decide(mfr.CMD_SUBMIT)
    assert not verdict.allowed
    assert verdict.rail == rail
    assert verdict.detail == detail


def test_manual_open_is_allowed_when_every_rail_passes():
    """The gate must not become a blanket refusal."""
    assert evaluator(FakeRails())(mfr.CMD_SUBMIT).allowed


def test_manual_open_passes_the_operators_instrument_to_the_whitelist():
    """The regression this pins: passing the runner's configured SYMBOL would
    compare that constant against itself, so the whitelist would pass
    unconditionally and silently. The OPERATOR's symbol must be judged."""
    rails = FakeRails()
    evaluator(rails, instrument="GBPUSD")(mfr.CMD_SUBMIT)
    assert rails.evaluate_calls[0][1] == "GBPUSD"


def test_manual_open_with_no_instrument_fails_closed():
    """An order with no stated symbol is not one to route to a broker."""
    rails = FakeRails()
    mfr.build_evaluator(rails, today="2026-07-28", instrument=None)(mfr.CMD_SUBMIT)
    assert rails.evaluate_calls[0][1] == ""      # whitelist will refuse it


# ── manual CLOSE / CANCEL: risk reduction is never blocked ───────────────────

@pytest.mark.parametrize("command", [mfr.CMD_CLOSE, mfr.CMD_CANCEL])
def test_risk_reducing_commands_survive_an_engaged_kill_switch(command):
    """The emergency case. If this ever fails, an operator cannot flatten during
    exactly the incident the kill switch was engaged for."""
    assert evaluator(FakeRails(kill=True))(command).allowed


@pytest.mark.parametrize("command", [mfr.CMD_CLOSE, mfr.CMD_CANCEL])
def test_risk_reducing_commands_survive_a_breached_daily_loss_limit(command):
    """A loss limit stops new risk. Trapping an operator in an open position
    once the limit is hit would make the limit the cause of further loss."""
    assert evaluator(FakeRails(blocked_rail="daily_loss_limit"))(command).allowed


@pytest.mark.parametrize("command", [mfr.CMD_CLOSE, mfr.CMD_CANCEL])
def test_risk_reducing_commands_never_consult_the_rails_at_all(command):
    """Exemption is structural, not a rail that happens to return allow — so a
    future rail cannot accidentally acquire the power to block a close."""
    rails = FakeRails(kill=True, blocked_rail="max_open_positions")
    assert evaluator(rails)(command).allowed
    assert rails.evaluate_calls == []


def test_risk_reducing_classification_comes_from_the_canonical_registry():
    """Not a local list. The registry is the one place a command's risk posture
    is declared, so a new command is classified once and both paths follow."""
    from command_registry import is_risk_reducing
    assert is_risk_reducing(mfr.CMD_CLOSE) and is_risk_reducing(mfr.CMD_CANCEL)
    assert not is_risk_reducing(mfr.CMD_SUBMIT)
    assert not is_risk_reducing(mfr.CMD_MODIFY)


# ── manual MODIFY: kill switch only ──────────────────────────────────────────

def test_manual_modify_is_blocked_by_the_kill_switch():
    """Modify is NOT risk-reducing (the registry says so) and can be
    discretionary, so an engaged kill switch suspends it — matching the runner,
    which does not exempt MODIFY_STOP either."""
    verdict = evaluator(FakeRails(kill=True))(mfr.CMD_MODIFY)
    assert not verdict.allowed
    assert verdict.rail == mfr.RAIL_KILL_SWITCH


def test_legitimate_modify_still_works_when_the_kill_switch_is_clear():
    assert evaluator(FakeRails())(mfr.CMD_MODIFY).allowed


def test_manual_modify_is_not_subject_to_mirror_or_open_count_rails():
    """A manually managed position has no entry in the RUNNER's intent mirror.
    Running a modify through the full rail set would refuse it as
    `unknown_position` — a bookkeeping fact, not a safety one. Over-refusal is
    the mirror-image failure and equally unacceptable."""
    rails = FakeRails(blocked_rail="max_open_positions")
    assert evaluator(rails)(mfr.CMD_MODIFY).allowed
    assert rails.evaluate_calls == []


# ── unavailable rails fail closed ────────────────────────────────────────────

def test_a_live_command_with_no_rails_is_refused_not_waved_through():
    """Unevaluated is not the same as allowed. If the rails cannot be built on a
    host running a live adapter, a risk-increasing command must stop."""
    verdict = evaluator(None)(mfr.CMD_SUBMIT)
    assert not verdict.allowed
    assert verdict.rail == mfr.RAIL_UNAVAILABLE


def test_risk_reduction_still_works_when_the_rails_are_unavailable():
    """Even with no rails, de-risking must remain possible."""
    assert evaluator(None)(mfr.CMD_CLOSE).allowed
    assert evaluator(None)(mfr.CMD_CANCEL).allowed


def test_an_unclassifiable_command_does_not_inherit_the_close_exemption():
    """Fail closed: an unknown command must not be treated as risk-reducing."""
    assert mfr._is_risk_reducing("NotARealCommand") is False


# ── the gate inside execution_safety ─────────────────────────────────────────

def _ctx(**kw):
    base = dict(
        mode=es.MODE_ACTIVE,
        arming=es.ArmingState(armed=True, expires_at="2099-01-01T00:00:00Z"),
        node=es.NodeSafety(es.NODE_HEALTHY),
        operator=es.OperatorAuthorization(operator_ref="op_1", confirmed=True),
        account=es.AccountSafety(es.ACCOUNT_MATCH),
    )
    base.update(kw)
    return es.SafetyContext(**base)


def test_evaluate_denies_when_a_financial_rail_refuses():
    decision = es.evaluate(
        es.CommandRequest(command_type=mfr.CMD_SUBMIT),
        _ctx(financial=evaluator(FakeRails(blocked_rail="daily_loss_limit",
                                           detail="over the limit"))))
    assert not decision.allowed
    assert decision.reason == "financial_rail_daily_loss_limit"


def test_evaluate_allows_when_the_financial_rails_pass():
    decision = es.evaluate(es.CommandRequest(command_type=mfr.CMD_SUBMIT),
                           _ctx(financial=evaluator(FakeRails())))
    assert decision.allowed and decision.reason == es.ALLOW


def test_authorization_reasons_keep_priority_over_financial_ones():
    """A disarmed operator sees `system_disarmed`, not a financial rail. The new
    gate is last, so no existing denial reason changed meaning."""
    decision = es.evaluate(
        es.CommandRequest(command_type=mfr.CMD_SUBMIT),
        _ctx(arming=es.ArmingState(),
             financial=evaluator(FakeRails(blocked_rail="kill_switch"))))
    assert not decision.allowed
    assert decision.reason == es.DENY_DISARMED


def test_absent_financial_evaluator_applies_no_gate():
    """The `not evaluated` convention: pure-engine callers and the mock world
    are unaffected, exactly as with the account gate."""
    assert es.evaluate(es.CommandRequest(command_type=mfr.CMD_SUBMIT),
                       _ctx(financial=None)).allowed


@pytest.mark.parametrize("command", ["noop", "request_health", "request_telemetry"])
def test_read_only_commands_are_never_financially_gated(command):
    """A read must not be refused because a trading limit was reached.

    Uses REAL read-only commands from the registry. An invented name would
    classify as unknown and be denied before the read-only branch, so the test
    would pass while proving nothing about the gate it claims to cover.
    """
    def explode(_command):
        raise AssertionError("a read-only command consulted the financial rails")
    decision = es.evaluate(es.CommandRequest(command_type=command),
                           _ctx(financial=explode))
    assert decision.allowed
    assert decision.reason == es.ALLOW_READ_ONLY


def test_a_faulty_financial_evaluator_fails_closed():
    """A policy fault is a DENY, never a fall-through to allow."""
    def boom(_command):
        raise RuntimeError("rails exploded")
    decision = es.evaluate(es.CommandRequest(command_type=mfr.CMD_SUBMIT),
                           _ctx(financial=boom))
    assert not decision.allowed
    assert decision.reason == es.DENY_POLICY_ERROR


# ── regression: the autonomous path is untouched ─────────────────────────────

def test_autonomous_evaluate_is_untouched():
    """`SafetyRails.evaluate` gained nothing and lost nothing.

    The only change to `live/safety.py` is a public delegator to the existing
    private kill-switch predicate. If `evaluate` itself is ever modified to
    accommodate the Control Tower, that is a fork of the rules and this asserts
    against it.
    """
    import inspect

    from live.safety import SafetyRails
    source = inspect.getsource(SafetyRails.evaluate)
    for rail in ("kill_switch", "symbol_whitelist", "duplicate_intent",
                 "daily_loss_limit", "max_open_positions", "unknown_position"):
        assert rail in source, f"{rail} disappeared from the autonomous evaluator"
    # The delegator must not have grown logic of its own.
    delegator = inspect.getsource(SafetyRails.kill_switch_engaged)
    assert "return self._kill_switch_on()" in delegator


def test_the_close_exemption_matches_the_runner_semantics():
    """Both paths exempt de-risking from the kill switch, for the same reason."""
    import inspect

    from live.safety import SafetyRails
    source = inspect.getsource(SafetyRails.evaluate)
    assert "intent.action != CLOSE_POSITION" in source     # runner's exemption
    assert evaluator(FakeRails(kill=True))(mfr.CMD_CLOSE).allowed   # CT's


def test_no_financial_rule_was_reimplemented_in_the_backend():
    """One source of truth. The adapter may name rails; it must not compute
    them — no thresholds, no comparisons against limits, no arithmetic."""
    from conftest import code_only
    source = code_only("manual_financial_rails.py")
    for banned in ("daily_loss_limit_r", "max_open_positions", "open_mirror_count",
                   "daily_realized_r", "<=", ">="):
        assert banned not in source, f"financial logic leaked into the backend: {banned}"


# ── integration with the REAL SafetyRails (not the fake) ─────────────────────

def _real_rails(tmp_path, **overrides):
    """A genuine `live.safety.SafetyRails` over a temporary state directory.

    Every test above uses a fake, which proves only that the adapter matches my
    ASSUMPTIONS about the rails. These prove it matches the real class — its
    actual method names, signatures and verdict shape. A rename in `live/safety`
    that the fake happily absorbs will fail here, which is the point.
    """
    import os

    from live.config import LiveConfig
    from live.safety import SafetyRails
    from live.state import RunnerState
    env = {"LIVE_STATE_DIR": str(tmp_path / "state"),
           "LIVE_KILL_FILE": str(tmp_path / "KILL")}
    env.update(overrides)
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        config = LiveConfig()
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        return SafetyRails(config, RunnerState(config.state_dir))
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_real_rails_block_a_manual_open_when_the_kill_file_exists(tmp_path):
    rails = _real_rails(tmp_path)
    decide = mfr.build_evaluator(rails, today="2026-07-28", instrument="EURUSD")
    assert decide(mfr.CMD_SUBMIT).allowed              # no kill file yet

    (tmp_path / "KILL").write_text("stop")
    verdict = decide(mfr.CMD_SUBMIT)
    assert not verdict.allowed
    assert verdict.rail == "kill_switch"


def test_real_rails_still_permit_closing_with_the_kill_file_present(tmp_path):
    """The emergency guarantee, proven against the real rails."""
    rails = _real_rails(tmp_path)
    (tmp_path / "KILL").write_text("stop")
    decide = mfr.build_evaluator(rails, today="2026-07-28", instrument="EURUSD")
    assert decide(mfr.CMD_CLOSE).allowed
    assert decide(mfr.CMD_CANCEL).allowed


def test_real_rails_reject_a_manual_open_on_a_non_whitelisted_symbol(tmp_path):
    """Proves the operator's instrument genuinely reaches the whitelist."""
    rails = _real_rails(tmp_path)
    verdict = mfr.build_evaluator(rails, today="2026-07-28",
                                  instrument="GBPUSD")(mfr.CMD_SUBMIT)
    assert not verdict.allowed
    assert verdict.rail == "symbol_whitelist"


def test_real_rails_expose_the_kill_switch_reader_the_adapter_relies_on(tmp_path):
    """The adapter's MODIFY path calls exactly this; a rename must fail loudly."""
    rails = _real_rails(tmp_path)
    assert rails.kill_switch_engaged() is False
    (tmp_path / "KILL").write_text("stop")
    assert rails.kill_switch_engaged() is True
