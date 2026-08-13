"""M-DEMO-PERSISTENT-ARM-1 — persistent DEMO authorization, 24/5.

The 12-hour TTL was a commissioning safety mechanism, not a strategy rule, and
its lifetime budget of 3 OPENs was measured against reality afterwards: over
11.5 years the deployed strategy filled 659 trades, mean 0.156/day, busiest DAY
ever 3, busiest WEEK ever 5. A lifetime budget of 3 was therefore equal to the
busiest single day on record — a hidden ceiling that could silently halt a legal
24/5 strategy mid-week.

What replaces it is NOT "no limit". Authorization becomes persistent, and the
bounded budget becomes a DAILY circuit breaker that resets at 00:00 UTC. The
distinction these tests defend: persistent authorization means "execution may
reach the normal rails", never "execution is permitted regardless of rails".
Every OPEN still re-proves account, server, DEMO status, engine identity, news
health and every downstream rail.

tmp_path only; no MT5, no network, no production live_state.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import arming, news_feed as nf  # noqa: E402
from live.arming import (DEFAULT_DAILY_OPEN_CAP, R_ACCOUNT, R_DAILY_CAP,  # noqa: E402
                         R_DISARMED, R_EXPIRED, R_NOT_DEMO, R_SERVER,
                         TYPE_PERSISTENT, ArmRuntime)
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,  # noqa: E402
                          OrderIntent)
from live.safety import SafetyRails  # noqa: E402

LOGIN, SERVER = 9000000001, "FTMO-Demo"
DEMO, LIVE_ACCT = 0, 2


def authorize(root, *, login=LOGIN, server=SERVER, cap=DEFAULT_DAILY_OPEN_CAP, now=None):
    return ArmRuntime.create_persistent(root, login=login, server=server,
                                        mode="live", daily_open_cap=cap, now=now)


def ok(arm, *, login=LOGIN, server=SERVER, trade_mode=DEMO, now=None):
    return arm.authorize_open(login=login, server=server, mode="live",
                              trade_mode=trade_mode, now=now)


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
    def __init__(self, mirror=None, realized=0.0, ledger=None):
        self.data = {"ledger": dict(ledger or {}), "mirror": dict(mirror or {}),
                     "broker_closed": {}}
        self._realized = realized
    def ledger_status(self, i): return (self.data["ledger"].get(i) or {}).get("status")
    def mirror_ticket(self, t): return self.data["mirror"].get(t)
    def broker_closed_ticket(self, t): return None
    def daily_realized_r(self, d): return self._realized
    def open_mirror_count(self): return len(self.data["mirror"])
    def record_block(self, *a, **k): pass


class Gate:
    """News gate stub. `allowed=False` mimics a stale/blackout refusal."""
    def __init__(self, allowed=True, reason=None, detail=""):
        self._v = (allowed, reason, detail)
    def verdict(self, now=None): return self._v


def rails(root, arm=None, *, state=None, observed=None, gate=Gate(), mode="live"):
    return SafetyRails(Cfg(root, mode), state or State(), arm_runtime=arm,
                       observed_account=observed if observed is not None else
                       {"login": LOGIN, "server": SERVER, "trade_mode": DEMO},
                       news_gate=gate)


def open_intent(tid="L_1", iid="i1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id=tid,
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00",
                       entry=1.1, stop=1.0, target=1.3)


def close_intent():
    return OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="L_1",
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00")


def modify_intent():
    return OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="L_1",
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00", stop=1.05)


# ── 1. time no longer expires authorization ─────────────────────────────────

@pytest.mark.parametrize("days", [1, 7, 30, 365, 3650])
def test_persistent_authorization_never_expires_with_time(tmp_path, days):
    """The headline property. A commissioning token would refuse at +12h."""
    t0 = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    arm = authorize(tmp_path, now=t0)
    allowed, reason = ok(arm, now=t0 + timedelta(days=days))
    assert allowed, reason


def test_a_commissioning_token_still_expires(tmp_path):
    """The old model must keep working exactly as before."""
    t0 = datetime(2026, 8, 11, 10, 0, tzinfo=timezone.utc)
    arm = ArmRuntime.create(tmp_path, login=LOGIN, server=SERVER, mode="live",
                            ttl_minutes=720, max_opens=3, now=t0)
    assert ok(arm, now=t0 + timedelta(hours=11))[0]
    assert ok(arm, now=t0 + timedelta(hours=13)) == (False, R_EXPIRED)


def test_no_expiry_field_is_written_and_none_is_deliberate(tmp_path):
    d = json.loads((tmp_path / "arm_token.json").read_text()) if False else None
    authorize(tmp_path)
    d = json.loads((tmp_path / "arm_token.json").read_text())
    assert d["expires_at"] is None and "expires_at" in d, "null must be explicit"
    assert d["type"] == TYPE_PERSISTENT and d["demo_only"] is True


def test_no_renewal_or_expiry_logic_remains_for_the_persistent_type():
    """Guards against a future edit re-introducing a hidden clock."""
    src = (REPO_ROOT / "live" / "arming.py").read_text(encoding="utf-8")
    body = src[src.index("def authorize_open"):src.index("def consume_open_attempt")]
    assert "R_EXPIRED" in body, "commissioning expiry must still exist"
    # the expiry branch must be guarded by a non-persistent check
    assert "if self.type != TYPE_PERSISTENT:" in body
    # No renewal PATH may exist: nothing may extend an authorization in place.
    for banned in ("def renew", "def extend", "def rearm", "def unrevoke"):
        assert banned not in src, f"{banned} would let a token extend itself"
    # ...and nothing outside create/create_persistent may write an expiry.
    writers = [ln for ln in src.splitlines()
               if '"expires_at"' in ln and "=" in ln and "data[" in ln]
    assert not writers, f"expires_at written outside construction: {writers}"


# ── 2/4. revocation ─────────────────────────────────────────────────────────

def test_revoke_blocks_new_opens_immediately(tmp_path):
    arm = authorize(tmp_path)
    assert ok(arm)[0]
    arm.revoke("operator")
    assert ok(arm) == (False, R_DISARMED)


def test_revoke_survives_restart(tmp_path):
    authorize(tmp_path).revoke("operator")
    assert ok(ArmRuntime.load(tmp_path)) == (False, R_DISARMED)


def test_revoke_leaves_close_and_modify_available(tmp_path):
    arm = authorize(tmp_path); arm.revoke("operator")
    r = rails(tmp_path, arm, state=State(mirror={"L_1": 555}))
    assert not r.evaluate(open_intent(), "EURUSD", "2026-08-12").allowed
    assert r.evaluate(close_intent(), "EURUSD", "2026-08-12").allowed
    assert r.evaluate(modify_intent(), "EURUSD", "2026-08-12").allowed


def test_revocation_requires_a_new_explicit_authorization(tmp_path):
    """There is no un-revoke: only a fresh operator action restores capability."""
    arm = authorize(tmp_path); arm.revoke("operator")
    assert not hasattr(arm, "unrevoke") and not hasattr(arm, "rearm")
    assert ok(ArmRuntime.load(tmp_path))[0] is False
    authorize(tmp_path)                                  # explicit new action
    assert ok(ArmRuntime.load(tmp_path))[0] is True


# ── 3/8. bindings are re-proved on EVERY open ───────────────────────────────

def test_wrong_fingerprint_blocks(tmp_path):
    assert ok(authorize(tmp_path), login=9000000002) == (False, R_ACCOUNT)


def test_wrong_server_blocks(tmp_path):
    assert ok(authorize(tmp_path), server="Other-Demo") == (False, R_SERVER)


def test_a_replacement_account_cannot_inherit_authorization_after_restart(tmp_path):
    """The exact scenario Part 8 names: token created on one demo account must
    not authorize a different account merely because the process restarted."""
    authorize(tmp_path)
    reloaded = ArmRuntime.load(tmp_path)
    assert ok(reloaded, login=9000000999) == (False, R_ACCOUNT)
    assert ok(reloaded)[0] is True          # the original still validates


@pytest.mark.parametrize("tm", [LIVE_ACCT, 1, None, "", "0 "])
def test_non_demo_or_unknown_trade_mode_blocks(tmp_path, tm):
    """Creation-time proof is not runtime proof. Unknown is not permission."""
    assert ok(authorize(tmp_path), trade_mode=tm) == (False, R_NOT_DEMO)


def test_demo_account_passes(tmp_path):
    assert ok(authorize(tmp_path), trade_mode=0)[0] is True


def test_the_rail_passes_observed_trade_mode_through(tmp_path):
    """A wiring test: the rail must actually forward what it observed."""
    arm = authorize(tmp_path)
    r = rails(tmp_path, arm, observed={"login": LOGIN, "server": SERVER,
                                       "trade_mode": LIVE_ACCT})
    v = r.evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == R_NOT_DEMO


def test_a_rail_with_no_observed_account_refuses(tmp_path):
    r = rails(tmp_path, authorize(tmp_path), observed={})
    assert not r.evaluate(open_intent(), "EURUSD", "2026-08-12").allowed


# ── daily cap: circuit breaker, not a lifetime ceiling ──────────────────────

def test_daily_cap_blocks_only_after_the_cap_and_resets_next_day(tmp_path):
    day1 = datetime(2026, 8, 11, 23, 0, tzinfo=timezone.utc)
    arm = authorize(tmp_path, cap=3, now=day1)
    for _ in range(3):
        assert ok(arm, now=day1)[0]
        arm.consume_open_attempt(day1)
    assert ok(arm, now=day1) == (False, R_DAILY_CAP)
    # ...and the very next UTC day it is available again, with no operator action
    assert ok(arm, now=day1 + timedelta(hours=2))[0] is True


def test_the_cap_is_far_above_the_strategys_measured_worst_day():
    """659 fills / 4,216 days; busiest day 3, busiest week 5. The default must
    not be able to throttle legitimate operation."""
    assert DEFAULT_DAILY_OPEN_CAP >= 4 * 3, "must clear the historical daily max"


def test_daily_counter_survives_restart_within_the_same_day(tmp_path):
    day = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)
    arm = authorize(tmp_path, cap=2, now=day)
    arm.consume_open_attempt(day)
    assert ArmRuntime.load(tmp_path)._opens_today(day) == 1


def test_a_stale_day_counter_reads_as_zero_not_as_spent(tmp_path):
    day = datetime(2026, 8, 11, 9, 0, tzinfo=timezone.utc)
    arm = authorize(tmp_path, cap=2, now=day)
    arm.consume_open_attempt(day); arm.consume_open_attempt(day)
    assert ok(arm, now=day) == (False, R_DAILY_CAP)
    assert ArmRuntime.load(tmp_path)._opens_today(day + timedelta(days=1)) == 0


def test_consumption_is_persisted_before_it_is_reported(tmp_path):
    arm = authorize(tmp_path, cap=5)
    arm.consume_open_attempt()
    on_disk = json.loads((tmp_path / "arm_token.json").read_text())
    assert on_disk["opens_today"] == 1, "budget must be written through"


# ── 6. authorization is not a master override ───────────────────────────────

def test_kill_switch_outranks_authorization(tmp_path):
    (tmp_path / "KILL").write_text("stop")
    v = rails(tmp_path, authorize(tmp_path)).evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == "kill_switch"


def test_stale_or_unhealthy_news_outranks_authorization(tmp_path):
    gate = Gate(False, nf.REFUSE_STALE, "calendar 9h old")
    v = rails(tmp_path, authorize(tmp_path), gate=gate).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == nf.REFUSE_STALE


def test_an_active_blackout_outranks_authorization(tmp_path):
    gate = Gate(False, nf.REFUSE_BLACKOUT, "HIGH USD CPI")
    v = rails(tmp_path, authorize(tmp_path), gate=gate).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == nf.REFUSE_BLACKOUT


def test_daily_loss_limit_outranks_authorization(tmp_path):
    st = State(realized=-6.0)
    v = rails(tmp_path, authorize(tmp_path), state=st).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == "daily_loss_limit"


def test_max_open_positions_outranks_authorization(tmp_path):
    st = State(mirror={f"L_{i}": i for i in range(6)})
    v = rails(tmp_path, authorize(tmp_path), state=st).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == "max_open_positions"


def test_duplicate_intent_still_suppresses_under_persistent_authorization(tmp_path):
    st = State(ledger={"i1": {"status": "sent"}})
    v = rails(tmp_path, authorize(tmp_path), state=st).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == "duplicate_intent"


def test_symbol_whitelist_outranks_authorization(tmp_path):
    cfg = Cfg(tmp_path); cfg.broker_symbol = "GBPUSD"
    r = SafetyRails(cfg, State(), arm_runtime=authorize(tmp_path),
                    observed_account={"login": LOGIN, "server": SERVER, "trade_mode": DEMO})
    v = r.evaluate(open_intent(), "GBPUSD", "2026-08-12")
    assert not v.allowed and v.rail == "symbol_whitelist"


def test_a_clean_open_is_still_allowed_end_to_end(tmp_path):
    """The positive control: with every rail quiet, authorization DOES permit."""
    v = rails(tmp_path, authorize(tmp_path)).evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert v.allowed, f"{v.rail}: {v.detail}"


# ── 5. launcher posture unchanged ───────────────────────────────────────────

def test_launcher_stays_dry_run_and_authorization_elevates(tmp_path, monkeypatch):
    from live.main import resolve_execution_posture
    cfg = Cfg(tmp_path, mode="dry_run")
    authorize(tmp_path)
    note = resolve_execution_posture(cfg)
    assert cfg.mode == "live" and "AUTHORIZED" in note and "persistent" in note


def test_revoked_authorization_does_not_elevate(tmp_path):
    from live.main import resolve_execution_posture
    cfg = Cfg(tmp_path, mode="dry_run")
    authorize(tmp_path).revoke("operator")
    note = resolve_execution_posture(cfg)
    assert cfg.mode == "dry_run" and "REVOKED" in note


def test_no_arm_token_leaves_dry_run(tmp_path):
    from live.main import resolve_execution_posture
    cfg = Cfg(tmp_path, mode="dry_run")
    assert resolve_execution_posture(cfg) == "no arm token" and cfg.mode == "dry_run"


def test_main_no_longer_duplicates_expiry_logic():
    """The drift that caused this bug class: two copies of the rules."""
    src = (REPO_ROOT / "live" / "main.py").read_text(encoding="utf-8")
    fn = src[src.index("def resolve_execution_posture"):src.index("def build")]
    assert "elevation_verdict" in fn
    for leaked in ("_parse(", "R_EXPIRED", "remaining_attempts"):
        assert leaked not in fn, f"expiry logic leaked back into main.py: {leaked}"


# ── 10. telemetry ───────────────────────────────────────────────────────────

def test_telemetry_publishes_no_expiry_countdown_for_persistent(tmp_path):
    from live.telemetry import safe_arming
    a = safe_arming(authorize(tmp_path), mode="live")
    assert a["expires_at"] is None and a["expires"] is False
    assert a["authorization_type"] == TYPE_PERSISTENT and a["demo_only"] is True
    assert a["status"] == "armed" and a["armed"] is True
    assert a["daily_open_cap"] == DEFAULT_DAILY_OPEN_CAP and a["opens_today"] == 0


def test_telemetry_still_reports_expiry_for_a_commissioning_token(tmp_path):
    from live.telemetry import safe_arming
    t0 = datetime.now(timezone.utc) - timedelta(hours=13)
    ArmRuntime.create(tmp_path, login=LOGIN, server=SERVER, mode="live",
                      ttl_minutes=720, max_opens=3, now=t0)
    a = safe_arming(ArmRuntime.load(tmp_path), mode="live")
    assert a["expires"] is True and a["expires_at"] is not None
    assert a["status"] == "expired"


def test_telemetry_reports_revocation_provenance(tmp_path):
    from live.telemetry import safe_arming
    arm = authorize(tmp_path); arm.revoke("operator")
    a = safe_arming(ArmRuntime.load(tmp_path), mode="live")
    assert a["status"] == "disarmed" and a["armed"] is False
    assert a["revoked_at"] and a["revocation_reason"] == "operator"


def test_telemetry_distinguishes_authorized_from_blocked(tmp_path):
    """AUTHORIZED BUT CURRENTLY BLOCKED is a different state from NOT
    AUTHORIZED, and the payload must let the UI tell them apart: `armed` stays
    true while a downstream rail supplies the refusal."""
    from live.telemetry import safe_arming
    a = safe_arming(authorize(tmp_path), mode="live")
    assert a["armed"] is True and a["reason"] is None
    gate = Gate(False, nf.REFUSE_BLACKOUT, "HIGH USD CPI")
    v = rails(tmp_path, authorize(tmp_path), gate=gate).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert v.rail == nf.REFUSE_BLACKOUT       # blocked by a rail, still authorized


def test_no_login_is_published(tmp_path):
    from live.telemetry import safe_arming
    blob = json.dumps(safe_arming(authorize(tmp_path), mode="live"))
    assert str(LOGIN) not in blob and "login" not in blob


# ── 20. backwards compatibility ─────────────────────────────────────────────

def test_v1_tokens_remain_readable(tmp_path):
    t0 = datetime.now(timezone.utc)
    ArmRuntime.create(tmp_path, login=LOGIN, server=SERVER, mode="live",
                      ttl_minutes=60, max_opens=3, now=t0)
    a = ArmRuntime.load(tmp_path)
    assert not a.malformed and a.type == "commissioning" and a.remaining_attempts == 3
    assert a.demo_only is False, "v1 never carried a demo binding; do not invent one"
    assert ok(a, trade_mode=None)[0] is True, "v1 semantics must not change"


def test_an_unknown_schema_is_malformed_not_trusted(tmp_path):
    (tmp_path / "arm_token.json").write_text(json.dumps({"schema": "arm-token-v9"}))
    assert ArmRuntime.load(tmp_path).malformed is True


def test_a_persistent_token_without_a_cap_is_malformed(tmp_path):
    (tmp_path / "arm_token.json").write_text(json.dumps(
        {"schema": "arm-token-v2", "type": TYPE_PERSISTENT, "disarmed": False}))
    a = ArmRuntime.load(tmp_path)
    assert a.malformed and ok(a)[0] is False
