"""M-DEMO-ARM-1 — arm token, stale-OPEN guard, policy governance.

Three gates that together make unattended DEMO execution safe:
  * an OPEN requires LIVE_MODE=live AND a durable operator arm bound to the
    OBSERVED account;
  * an OPEN whose engine fill predates this cycle's window is refused rather
    than market-entered at a price the engine never saw;
  * a change to the executable portfolio policy freezes the cycle before any
    intent exists.

Everything runs against tmp_path; no MT5, no production state.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import arming  # noqa: E402
from live.arming import ArmRuntime  # noqa: E402
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,  # noqa: E402
                          OrderIntent)
from live.safety import SafetyRails  # noqa: E402

# Synthetic login. The real account number is deliberately absent from every
# committed file (see test_telemetry_scope_guards); the arm binds whatever the
# terminal reports, so the value here only has to be internally consistent.
LOGIN, SERVER = 9000000001, "FTMO-Demo"


class Cfg:
    def __init__(self, root, mode="live"):
        self.state_dir = root
        self.kill_file = root / "KILL"
        self.mode = mode
        self.broker_symbol = "EURUSD"
        self.daily_loss_limit_r = 5.0
        self.max_open_positions = 6
        self.fixed_risk_lots = 0.01


class State:
    def __init__(self):
        self.data = {"ledger": {}, "mirror": {}, "broker_closed": {}}
    def ledger_status(self, i): return (self.data["ledger"].get(i) or {}).get("status")
    def mirror_ticket(self, t): return self.data["mirror"].get(t)
    def broker_closed_ticket(self, t): return (self.data["broker_closed"].get(t) or {}).get("ticket")
    def daily_realized_r(self, d): return 0.0
    def open_mirror_count(self): return len(self.data["mirror"])
    def record_block(self, *a, **k): pass


def arm_now(root, *, ttl=60, opens=3, login=LOGIN, server=SERVER, mode="live"):
    return ArmRuntime.create(root, login=login, server=server, mode=mode,
                             ttl_minutes=ttl, max_opens=opens)


def rails(root, arm=None, mode="live", observed=None):
    return SafetyRails(Cfg(root, mode), State(), arm_runtime=arm,
                       observed_account=observed or {"login": LOGIN, "server": SERVER})


def open_intent(frontier="2026-08-07 09:30:00+00:00", fill=None):
    return OrderIntent(intent_id="i1", action=OPEN_POSITION, trade_id="L_1",
                       side="long", frontier_bar=frontier, entry=1.1, stop=1.0,
                       target=1.3, fill_time=fill)


def close_intent():
    return OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="L_1",
                       side="long", frontier_bar="2026-08-07 09:30:00+00:00")


TODAY = "2026-08-07"


# ── PART 1: arm token ────────────────────────────────────────────────────────

def test_absent_arm_refuses_open_in_live(tmp_path):
    v = rails(tmp_path, arm=None).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == "not_armed"


def test_valid_arm_allows_open(tmp_path):
    v = rails(tmp_path, arm=arm_now(tmp_path)).evaluate(open_intent(), "EURUSD", TODAY)
    assert v.allowed


def test_dry_run_never_consults_the_arm(tmp_path):
    """dry_run simulates; arming is a LIVE concern and must not change it."""
    v = rails(tmp_path, arm=None, mode="dry_run").evaluate(open_intent(), "EURUSD", TODAY)
    assert v.allowed


def test_expired_arm_refuses(tmp_path):
    arm = arm_now(tmp_path, ttl=-1)
    v = rails(tmp_path, arm=arm).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_EXPIRED


def test_wrong_account_refuses(tmp_path):
    arm = arm_now(tmp_path, login=999999)
    v = rails(tmp_path, arm=arm).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_ACCOUNT


def test_wrong_server_refuses(tmp_path):
    arm = arm_now(tmp_path, server="OtherBroker-Demo")
    v = rails(tmp_path, arm=arm).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_SERVER


def test_arm_binds_to_observed_account_not_config(tmp_path):
    """The terminal is ground truth: an arm for the configured account must not
    authorise a terminal that is logged into something else."""
    arm = arm_now(tmp_path)
    r = rails(tmp_path, arm=arm, observed={"login": 777, "server": SERVER})
    v = r.evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_ACCOUNT


def test_restart_preserves_deadline_and_budget(tmp_path):
    arm = arm_now(tmp_path, ttl=60, opens=3)
    arm.consume_open_attempt()
    expiry, remaining = arm.context.request_expires_at, arm.remaining_attempts
    reloaded = ArmRuntime.load(tmp_path)                # simulates a restart
    assert reloaded.context.request_expires_at == expiry, "restart extended expiry"
    assert reloaded.remaining_attempts == remaining == 2, "restart reset the budget"


def test_attempt_exhaustion_refuses_further_opens(tmp_path):
    arm = arm_now(tmp_path, opens=1)
    assert rails(tmp_path, arm=arm).evaluate(open_intent(), "EURUSD", TODAY).allowed
    arm.consume_open_attempt()
    v = rails(tmp_path, arm=arm).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_EXHAUSTED


def test_consumption_is_persisted_immediately(tmp_path):
    """Pre-submission accounting is only safe if it survives a crash between
    the decrement and the broker call."""
    arm = arm_now(tmp_path, opens=2)
    arm.consume_open_attempt()
    on_disk = json.loads((tmp_path / "arm_token.json").read_text())
    assert on_disk["remaining_open_attempts"] == 1


def test_malformed_token_fails_closed(tmp_path):
    arm_now(tmp_path)
    (tmp_path / "arm_token.json").write_text("{not json")
    v = rails(tmp_path, arm=ArmRuntime.load(tmp_path)).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_MALFORMED


def test_disarm_is_immediate_and_durable(tmp_path):
    arm = arm_now(tmp_path)
    arm.disarm("operator")
    v = rails(tmp_path, arm=ArmRuntime.load(tmp_path)).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == arming.R_DISARMED


def test_arm_never_renews_itself_during_operation(tmp_path):
    """`create` legitimately sets the initial deadline — that IS the operator
    action. What must not exist is any RUNTIME path that moves it: the methods
    the node calls every cycle may only read expiry, never write it."""
    import ast
    tree = ast.parse((REPO_ROOT / "live" / "arming.py").read_text(encoding="utf-8"))
    runtime_methods = {"authorize_open", "consume_open_attempt", "load", "__init__"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in runtime_methods:
            seen.add(node.name)
            for sub in ast.walk(node):
                if isinstance(sub, ast.Call):
                    fn = sub.func
                    name = fn.id if isinstance(fn, ast.Name) else getattr(fn, "attr", "")
                    assert name != "timedelta", f"{node.name} constructs a duration"
                # writing expires_at anywhere outside create is the failure
                if isinstance(sub, ast.Constant) and sub.value == "expires_at":
                    assert node.name != "consume_open_attempt", \
                        "attempt consumption touches the deadline"
    assert runtime_methods <= seen, f"methods not found: {runtime_methods - seen}"


def test_consume_does_not_alter_expiry_in_practice(tmp_path):
    """The behavioural counterpart to the structural check above."""
    arm = arm_now(tmp_path, ttl=60, opens=3)
    before = json.loads((tmp_path / "arm_token.json").read_text())["expires_at"]
    arm.consume_open_attempt()
    after = json.loads((tmp_path / "arm_token.json").read_text())["expires_at"]
    assert before == after


# ── superior rails still win ─────────────────────────────────────────────────

def test_kill_beats_a_valid_arm(tmp_path):
    (tmp_path / "KILL").write_text("stop")
    v = rails(tmp_path, arm=arm_now(tmp_path)).evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == "kill_switch"


def test_duplicate_ledger_beats_a_valid_arm(tmp_path):
    r = rails(tmp_path, arm=arm_now(tmp_path))
    r.state.data["ledger"]["i1"] = {"status": "confirmed"}
    v = r.evaluate(open_intent(), "EURUSD", TODAY)
    assert not v.allowed and v.rail == "duplicate_intent"


def test_identity_freeze_is_upstream_of_arming_entirely():
    """A frozen cycle produces no intents, so no arm check ever runs."""
    src = (REPO_ROOT / "live" / "runner.py").read_text(encoding="utf-8")
    body = src[src.index("def run_once"):src.index("def _engine_time_string")]
    assert body.index("verify_and_extend") < body.index("diff_frontier")


# ── CLOSE is never gated on the OPEN budget ──────────────────────────────────

def test_close_allowed_when_open_budget_exhausted(tmp_path):
    arm = arm_now(tmp_path, opens=1)
    arm.consume_open_attempt()
    r = rails(tmp_path, arm=arm)
    r.state.data["mirror"]["L_1"] = 12345
    v = r.evaluate(close_intent(), "EURUSD", TODAY)
    assert v.allowed, "an exhausted OPEN budget must never trap an open position"


def test_close_allowed_with_no_arm_at_all(tmp_path):
    r = rails(tmp_path, arm=None)
    r.state.data["mirror"]["L_1"] = 12345
    assert r.evaluate(close_intent(), "EURUSD", TODAY).allowed


def test_modify_not_gated_on_arm(tmp_path):
    r = rails(tmp_path, arm=None)
    r.state.data["mirror"]["L_1"] = 12345
    i = OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="L_1", side="long",
                    frontier_bar="2026-08-07 09:30:00+00:00", stop=1.05)
    assert r.evaluate(i, "EURUSD", TODAY).allowed


# ── PART 2: stale-OPEN guard ─────────────────────────────────────────────────

FRONTIER = "2026-08-07 09:30:00+00:00"


def test_fill_inside_the_frontier_window_is_accepted(tmp_path):
    """Normal case: the fill happened on an M1 candle inside [B, B+15)."""
    i = open_intent(FRONTIER, fill="2026-08-07 09:37:00+00:00")
    assert rails(tmp_path, arm=arm_now(tmp_path)).evaluate(i, "EURUSD", TODAY).allowed


def test_fill_exactly_at_the_boundary_is_accepted(tmp_path):
    i = open_intent(FRONTIER, fill=FRONTIER)
    assert rails(tmp_path, arm=arm_now(tmp_path)).evaluate(i, "EURUSD", TODAY).allowed


def test_fill_from_a_skipped_earlier_window_is_refused(tmp_path):
    """The audit finding: one skipped boundary => a fill up to 30 min stale."""
    i = open_intent(FRONTIER, fill="2026-08-07 09:20:00+00:00")
    v = rails(tmp_path, arm=arm_now(tmp_path)).evaluate(i, "EURUSD", TODAY)
    assert not v.allowed and v.rail == "stale_open"
    assert "skipped boundary" in v.detail


def test_several_skipped_boundaries_are_refused(tmp_path):
    """An hour of skipped boundaries is still squarely within the window this
    rail judges, and is exactly the case it exists for."""
    i = open_intent(FRONTIER, fill="2026-08-07 08:30:00+00:00")
    v = rails(tmp_path, arm=arm_now(tmp_path)).evaluate(i, "EURUSD", TODAY)
    assert not v.allowed and v.rail == "stale_open"


def test_implausibly_old_fill_abstains_rather_than_refusing(tmp_path):
    """Days-old input is not a late entry, it is uninterpretable data — and
    pandas turns short tokens into year 1 without raising. The rail must not
    manufacture a refusal from that; the other rails still govern."""
    i = open_intent(FRONTIER, fill="2020-01-01 00:00:00+00:00")
    assert rails(tmp_path, arm=arm_now(tmp_path)).evaluate(i, "EURUSD", TODAY).allowed


def test_close_is_never_stale_gated(tmp_path):
    """Exits must always be actionable, however late."""
    r = rails(tmp_path, arm=arm_now(tmp_path))
    r.state.data["mirror"]["L_1"] = 999
    i = OrderIntent(intent_id="c9", action=CLOSE_POSITION, trade_id="L_1",
                    side="long", frontier_bar=FRONTIER,
                    fill_time="2026-08-01 00:00:00+00:00")
    assert r.evaluate(i, "EURUSD", TODAY).allowed


def test_missing_fill_time_does_not_silently_refuse(tmp_path):
    """Other rails still apply; absence of the field is not evidence of staleness."""
    assert rails(tmp_path, arm=arm_now(tmp_path)).evaluate(
        open_intent(FRONTIER, fill=None), "EURUSD", TODAY).allowed


def test_diff_frontier_carries_fill_time_onto_open_intents():
    from live.intents import diff_frontier
    prev = pd.DataFrame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "",
                          "outcome": "UNFILLED", "entry": "1.1", "stop": "1.0", "tp": "1.3"}]).astype(str)
    cur = pd.DataFrame([{"trade_id": "L_1", "direction": "bullish",
                         "fill_time": "2026-08-07 09:37:00+00:00", "outcome": "OPEN",
                         "entry": "1.1", "stop": "1.0", "tp": "1.3"}]).astype(str)
    intents = diff_frontier(prev, cur, FRONTIER)
    assert [i.action for i in intents] == [OPEN_POSITION]
    assert intents[0].fill_time == "2026-08-07 09:37:00+00:00"


def test_intra_window_fill_and_exit_still_never_reaches_the_broker():
    """Unchanged behaviour: SKIP_INTRA_WINDOW, not an OPEN."""
    from live.intents import SKIP_INTRA_WINDOW, diff_frontier
    prev = pd.DataFrame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "",
                          "outcome": "UNFILLED", "entry": "1.1", "stop": "1.0", "tp": "1.3"}]).astype(str)
    cur = pd.DataFrame([{"trade_id": "L_1", "direction": "bullish",
                         "fill_time": "2026-08-07 09:31:00+00:00", "outcome": "WIN",
                         "entry": "1.1", "stop": "1.0", "tp": "1.3", "net_r": "2.0"}]).astype(str)
    assert [i.action for i in diff_frontier(prev, cur, FRONTIER)] == [SKIP_INTRA_WINDOW]


def test_stale_refusal_reaches_no_broker_open_call(tmp_path):
    """End-to-end at the executor: a refused OPEN must not touch the gateway."""
    from live.executor import Executor

    class Gateway:
        def snapshot(self): return True, {"positions": []}
        def open_position(self, *a, **k):
            raise AssertionError("broker OPEN reached despite a stale refusal")

    class St(State):
        def ledger_set(self, i, s, d=None):
            self.data["ledger"][i] = {"status": s, "detail": d or {}}
        def mirror_set(self, t, v):
            self.data["mirror"][t] = v if v is not None else None
        def save(self): pass
        def clear_broker_closed(self, t): pass

    cfg = Cfg(tmp_path, "live")
    ex = Executor(cfg, St(), Gateway(), arm_runtime=arm_now(tmp_path),
                  observed_account={"login": LOGIN, "server": SERVER})
    out = ex.apply([open_intent(FRONTIER, fill="2026-08-07 09:00:00+00:00")],
                   today=TODAY)
    assert out["applied"] == []
    assert out["blocked"][0]["rail"] == "stale_open"


# ── PART 3: policy governance ────────────────────────────────────────────────

def _lux(tmp_path, body=b'{"cohorts": []}'):
    p = tmp_path / "lux" / "configs" / "policy"
    p.mkdir(parents=True, exist_ok=True)
    (p / "deployed_policy.v1.json").write_bytes(body)
    return tmp_path / "lux"


def _frame():
    return pd.DataFrame([{"trade_id": "S_1", "ob_id": "7", "direction": "bearish",
                          "detection_time": "2024-01-02 10:00:00+00:00"}]).astype(str)


def _guard(state_dir, lux):
    from live.identity_guard import IdentityGuard, policy_digest
    return IdentityGuard(state_dir, config_digest="cfg", engine_version="v1",
                         policy_digest=policy_digest(lux))


def test_unchanged_policy_is_accepted(tmp_path):
    lux = _lux(tmp_path)
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]


def test_one_byte_policy_change_is_refused(tmp_path):
    lux = _lux(tmp_path)
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]
    (lux / "configs" / "policy" / "deployed_policy.v1.json").write_bytes(b'{"cohorts": [] }')
    ok, detail = _guard(tmp_path, lux).verify_and_extend(_frame(), {})
    assert not ok and "policy_drift" in detail


def test_missing_policy_is_refused(tmp_path):
    lux = _lux(tmp_path)
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]
    (lux / "configs" / "policy" / "deployed_policy.v1.json").unlink()
    ok, detail = _guard(tmp_path, lux).verify_and_extend(_frame(), {})
    assert not ok and "policy_drift" in detail


def test_restart_with_same_policy_is_accepted(tmp_path):
    lux = _lux(tmp_path)
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]
    assert _guard(tmp_path, lux).verify_and_extend(_frame(), {})[0]   # fresh instance


def test_policy_digest_is_recorded_in_the_witness(tmp_path):
    lux = _lux(tmp_path)
    _guard(tmp_path, lux).verify_and_extend(_frame(), {})
    w = json.loads((tmp_path / "identity_witness.json").read_text())
    assert w["policy_digest"] and len(w["policy_digest"]) == 64


def test_unparseable_fill_time_abstains_rather_than_refusing(tmp_path):
    """Regression: pandas parses a short token like "t1" as YEAR 1 under
    errors="coerce", which made the rail refuse a legitimate OPEN on a parse
    artefact. Garbage must make the rail abstain, not block."""
    for junk in ("t1", "n/a", "?", "not-a-time"):
        v = rails(tmp_path, arm=arm_now(tmp_path)).evaluate(
            open_intent(FRONTIER, fill=junk), "EURUSD", TODAY)
        assert v.allowed, f"{junk!r} caused a bogus stale_open refusal"


# ── PART 4: arm-based elevation (launcher stays dry_run) ─────────────────────

def _posture(tmp_path, monkeypatch, launcher_mode="dry_run"):
    """Run main()'s posture block in isolation and report the resolved mode."""
    from live.arming import ArmRuntime, _now, _parse

    class C:
        mode = launcher_mode
        state_dir = tmp_path
    cfg = C()
    arm = ArmRuntime.load(cfg.state_dir)
    if arm is not None and not arm.malformed and not arm.disarmed:
        exp = _parse(arm.context.request_expires_at)
        if (exp is not None and _now() < exp and str(arm.context.mode) == "live"
                and isinstance(arm.remaining_attempts, int)
                and arm.remaining_attempts > 0):
            cfg.mode = "live"
    return cfg.mode


def test_launcher_default_stays_dry_run_without_an_arm(tmp_path, monkeypatch):
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_valid_arm_elevates_to_live(tmp_path, monkeypatch):
    arm_now(tmp_path, ttl=60, opens=3)
    assert _posture(tmp_path, monkeypatch) == "live"


def test_expired_arm_does_not_elevate(tmp_path, monkeypatch):
    arm_now(tmp_path, ttl=-1)
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_exhausted_arm_does_not_elevate(tmp_path, monkeypatch):
    arm = arm_now(tmp_path, opens=1)
    arm.consume_open_attempt()
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_disarmed_arm_does_not_elevate(tmp_path, monkeypatch):
    arm_now(tmp_path).disarm("operator")
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_malformed_arm_does_not_elevate(tmp_path, monkeypatch):
    arm_now(tmp_path)
    (tmp_path / "arm_token.json").write_text("{not json")
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_capability_disappears_with_no_launcher_edit(tmp_path, monkeypatch):
    """The whole point of elevation: the same launcher config yields live while
    armed and dry_run once the arm lapses."""
    arm = arm_now(tmp_path, opens=1)
    assert _posture(tmp_path, monkeypatch) == "live"
    arm.consume_open_attempt()
    assert _posture(tmp_path, monkeypatch) == "dry_run"


def test_main_resolves_exactly_one_effective_mode():
    """A partial migration (executor elevated, reconciliation not) would send
    orders with no broker reconciliation. Elevation must be a single assignment
    to config.mode, not a second parallel concept."""
    src = (REPO_ROOT / "live" / "main.py").read_text(encoding="utf-8")
    assert 'config.mode = "live"' in src
    assert "effective_mode" not in src, "a second mode concept has appeared"


def test_build_resolves_posture_on_the_config_components_actually_hold(tmp_path, monkeypatch):
    """The defect this pins: elevation originally ran in main() against a
    SEPARATE LiveConfig instance, so build()'s config stayed dry_run. The node
    published mode=dry_run while the operator believed it was armed — a silent
    disagreement between the arm and the executor."""
    import inspect
    from live import main as m
    src = inspect.getsource(m.build)
    assert "resolve_execution_posture(config)" in src, \
        "build() must resolve posture on its own config"
    # and the function must mutate the object it is handed
    arm_now(tmp_path, ttl=60, opens=2)

    class C:
        mode = "dry_run"
        state_dir = tmp_path
    cfg = C()
    m.resolve_execution_posture(cfg)
    assert cfg.mode == "live"


def test_posture_resolution_leaves_dry_run_untouched_without_an_arm(tmp_path):
    from live import main as m

    class C:
        mode = "dry_run"
        state_dir = tmp_path
    cfg = C()
    note = m.resolve_execution_posture(cfg)
    assert cfg.mode == "dry_run" and "no arm token" in note
