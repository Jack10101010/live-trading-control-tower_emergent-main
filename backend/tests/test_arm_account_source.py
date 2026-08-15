"""M-ARM-ACCOUNT-SOURCE-FIX-1 — one account authority, and OPEN can actually pass.

THE INCIDENT. On 2026-08-12 the engine produced a legal fill for L_2106 at
18:11Z (session Outside, Bear/Chop, cohort EURUSD|outside|bos_long, TRADE, final
RR 1.25). An OPEN intent was generated at boundary 18:00 and refused
`arm_server_mismatch`. Nothing reached FTMO.

Root cause: `build()` read the account BEFORE `gateway.connect()` (main() calls
build() then connect()), so the read returned "not connected", `observed_account`
stayed empty, and `SafetyRails` compared `server=None` against `FTMO-Demo` — for
the entire life of the process. Meanwhile telemetry re-read the account through
the CONNECTED gateway and published `fingerprint_matches: true`. Two authorities
for one fact; the reassuring one was the one on screen.

WHY THE SUITE MISSED IT. Every existing arm test proved a REFUSAL. Not one
proved that a correctly-authorized OPEN actually PASSES. A rail that refuses
everything passes a refusal-only suite perfectly. That gap is closed here.

tmp_path only; no MT5, no network, no production live_state, and no order is
ever submitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import news_feed as nf  # noqa: E402
from live.account_observation import (STATUS_OK, STATUS_UNAVAILABLE,  # noqa: E402
                                      AccountObservation, ObservedIdentity)
from live.arming import ArmRuntime  # noqa: E402
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,  # noqa: E402
                          OrderIntent)
from live.safety import SafetyRails  # noqa: E402

LOGIN, SERVER = 9000000001, "FTMO-Demo"
DEMO = 0


# ── fixtures ────────────────────────────────────────────────────────────────

# ── M-LIVE-STALE-OPEN-GUARDS-1 test wiring ──────────────────────────────────
# Two OPEN rails were added after these tests were written: wall-clock freshness
# of the modelled fill, and executable-price divergence. Production always
# supplies both a fill_time (diff_frontier sets it on every OPEN) and a quote
# provider (the Executor wires the gateway), so these fixtures now do the same.
# The fixed clock keeps them deterministic and the quote sits exactly on the
# canonical entry, so both guards abstain and each test still proves what it
# was written to prove rather than tripping on the new rails.
import datetime as _dt
_GUARD_NOW = _dt.datetime.fromisoformat("2026-08-12T18:05:00+00:00")


def _guard_clock():
    return _GUARD_NOW


def _guard_quote():
    return True, {"bid": 1.15234, "ask": 1.15234, "at": "2026-08-12T18:05:00+00:00"}


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
    def ledger_status(self, i): return None
    def mirror_ticket(self, t): return self.data["mirror"].get(t)
    def broker_closed_ticket(self, t): return None
    def daily_realized_r(self, d): return 0.0
    def open_mirror_count(self): return 0
    def record_block(self, *a, **k): pass


class HealthyNews:
    def verdict(self, now=None): return (True, None, "6 relevant events; fresh")


def observation(*, connected=True, server=SERVER, login=LOGIN, trade_mode=DEMO):
    """What AccountObserver returns. `connected=False` models the pre-connect
    read that caused the incident."""
    if not connected:
        return AccountObservation(sampled_at="2026-08-12T18:00:00Z",
                                  status=STATUS_UNAVAILABLE, identity=None,
                                  error="not connected")
    return AccountObservation(
        sampled_at="2026-08-12T18:00:00Z", status=STATUS_OK,
        identity=ObservedIdentity(login=login, server=server,
                                  currency="USD", trade_mode=trade_mode),
        identity_observed_at="2026-08-12T18:00:00Z")


def authorize(root):
    return ArmRuntime.create_persistent(root, login=LOGIN, server=SERVER,
                                        mode="live", daily_open_cap=12)


def rails(root, *, binding, arm=None, news=None, mode="live"):
    return SafetyRails(Cfg(root, mode), State(), arm_runtime=arm or authorize(root),
                       observed_account=binding, news_gate=news or HealthyNews(),
                       quote_provider=_guard_quote, clock=_guard_clock)


def open_intent():
    return OrderIntent(intent_id="i1", action=OPEN_POSITION, trade_id="L_2106",
                       side="long", frontier_bar="2026-08-12 18:00:00+00:00",
                       entry=1.15234, stop=1.15166, target=1.15319,
                       fill_time="2026-08-12 18:02:00+00:00")


# ── 5. POSITIVE AUTHORIZATION — the test whose absence hid the incident ─────

def test_a_correctly_authorized_open_PASSES_the_arm_rail(tmp_path):
    """Connected DEMO account, matching server and fingerprint, persistent_demo
    authorization, cap available, no kill, healthy news, no duplicate, valid
    symbol -> the OPEN must be ALLOWED. No broker order is created."""
    r = rails(tmp_path, binding=observation().as_arm_binding())
    v = r.evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert v.allowed, f"correctly-authorized OPEN was refused by {v.rail}: {v.detail}"
    assert v.rail == "allowed"


def test_the_arm_rail_itself_abstains_on_a_valid_binding(tmp_path):
    """`_arm_verdict` returning None is what 'this rail permits' looks like."""
    r = rails(tmp_path, binding=observation().as_arm_binding())
    assert r._arm_verdict(open_intent()) is None


def test_the_exact_L_2106_intent_would_now_be_authorized(tmp_path):
    """The real refused intent, replayed against the fixed source."""
    r = rails(tmp_path, binding=observation().as_arm_binding())
    v = r.evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert v.allowed and v.rail != "arm_server_mismatch"


def test_close_and_modify_remain_allowed_alongside_a_passing_open(tmp_path):
    r = rails(tmp_path, binding=observation().as_arm_binding())
    r.state.data["mirror"]["L_2106"] = 555
    for intent in (OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="L_2106",
                               side="long", frontier_bar="2026-08-12 18:00:00+00:00"),
                   OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="L_2106",
                               side="long", frontier_bar="2026-08-12 18:00:00+00:00",
                               stop=1.15200)):
        assert r.evaluate(intent, "EURUSD", "2026-08-12").allowed


# ── 6. INCIDENT REGRESSION ──────────────────────────────────────────────────

def test_the_old_architecture_would_have_refused_arm_server_mismatch(tmp_path):
    """NOT VACUOUS: proves the pre-connect binding really does produce the exact
    refusal recorded in the production ledger on 2026-08-12T18:36:09Z."""
    boot = observation(connected=False).as_arm_binding()
    assert boot == {}, "an unconnected read must yield no binding"
    v = rails(tmp_path, binding=boot).evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed
    assert v.rail == "arm_server_mismatch", f"incident signature not reproduced: {v.rail}"


def test_the_fresh_connected_observation_replaces_the_empty_boot_value(tmp_path):
    """A -> G: unconnected at boot, connected later, OPEN must use the FRESH
    observation and must not retain the empty one."""
    from live.executor import Executor

    class Gw:
        pass
    ex = Executor(Cfg(tmp_path), State(), Gw(), arm_runtime=authorize(tmp_path),
                  observed_account=observation(connected=False).as_arm_binding(),
                  news_gate=HealthyNews())
    ex.rails.quote_provider, ex.rails._clock = _guard_quote, _guard_clock
    # boot state: empty, refuses
    assert ex.rails.observed_account == {}
    assert ex.rails.evaluate(open_intent(), "EURUSD", "2026-08-12").rail == "arm_server_mismatch"
    # gateway connects; the cycle installs the canonical observation
    ex.set_observed_account(observation().as_arm_binding())
    assert ex.rails.observed_account["server"] == SERVER
    v = ex.rails.evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert v.allowed, f"stale boot binding was retained: {v.rail}"


def test_losing_the_observation_refuses_rather_than_reusing_the_last_good_one(tmp_path):
    """A binding must never outlive the observation that produced it."""
    from live.executor import Executor

    class Gw:
        pass
    ex = Executor(Cfg(tmp_path), State(), Gw(), arm_runtime=authorize(tmp_path),
                  observed_account=None, news_gate=HealthyNews())
    ex.rails.quote_provider, ex.rails._clock = _guard_quote, _guard_clock
    ex.set_observed_account(observation().as_arm_binding())
    assert ex.rails.evaluate(open_intent(), "EURUSD", "2026-08-12").allowed
    ex.set_observed_account({})                      # observation lost this cycle
    assert not ex.rails.evaluate(open_intent(), "EURUSD", "2026-08-12").allowed


def _code_lines(path):
    """(lineno, code) for executable lines only.

    Raw substring scans match comments and docstrings — including prose that
    states the OPPOSITE of what the check appears to assert. Both structural
    tests below were briefly vacuous for exactly that reason, so every one of
    them now reads code and never commentary.
    """
    out = []
    triple = '"' * 3
    for n, ln in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        body = ln.split("#", 1)[0].rstrip()
        stripped = body.strip()
        if stripped and not stripped.startswith(triple) and not stripped.startswith("'''"):
            out.append((n, body))
    return out


def test_build_no_longer_reads_the_account_before_connect(tmp_path):
    """The structural defect, pinned in source: build() runs before connect()."""
    lines = _code_lines(REPO_ROOT / "live" / "main.py")
    a = next(n for n, l in lines if l.startswith("def build("))
    b = next(n for n, l in lines if n > a and l.startswith("def _news_block("))
    build = [l for n, l in lines if a <= n < b]
    assert not [l for l in build if "read_account_state" in l], \
        "build() reads the account again -- it runs BEFORE gateway.connect()"
    c0 = next(n for n, l in lines if l.startswith("def cycle("))
    cyc = [(n, l) for n, l in lines if n >= c0]
    obs = [n for n, l in cyc if "_observe_account(observer)" in l]
    setb = [n for n, l in cyc if "set_observed_account" in l]
    applied = [n for n, l in cyc if "executor.apply(" in l]
    assert obs and setb and applied, f"missing wiring: {obs} {setb} {applied}"
    # the canonical observation must be installed BEFORE any intent can apply
    assert min(setb) < min(applied), f"binding installed after apply: {setb} vs {applied}"
    assert min(obs) <= min(setb)


def test_telemetry_and_the_rail_project_from_ONE_observation(tmp_path):
    """G: the fingerprint telemetry shows and the account the rail compares
    must come from the same object, not two reads."""
    from live.telemetry import account_fingerprint
    obs = observation()
    binding = obs.as_arm_binding()
    published = obs.as_observed_mapping()["identity"]
    assert binding["server"] == published.server
    assert binding["login"] == published.login
    assert binding["trade_mode"] == published.trade_mode
    # and the fingerprint the Control Tower renders is derived from that same pair
    fp_from_obs = account_fingerprint(published.login, published.server)
    fp_from_rail = account_fingerprint(binding["login"], binding["server"])
    assert fp_from_obs == fp_from_rail and fp_from_obs.startswith("acctfp_")


def test_an_unavailable_observation_publishes_nothing_and_authorizes_nothing(tmp_path):
    obs = observation(connected=False)
    assert obs.as_arm_binding() == {}
    assert obs.as_observed_mapping()["identity"] is None
    assert not rails(tmp_path, binding=obs.as_arm_binding()).evaluate(
        open_intent(), "EURUSD", "2026-08-12").allowed


# ── 7. DEMO SAFETY PRESERVED ────────────────────────────────────────────────

@pytest.mark.parametrize("kw,expect", [
    ({"server": "Other-Demo"}, "arm_server_mismatch"),
    ({"login": 9000000999}, "arm_account_mismatch"),
    ({"trade_mode": 2}, "arm_account_not_demo"),
    ({"trade_mode": None}, "arm_account_not_demo"),
])
def test_bindings_still_refuse_on_mismatch(tmp_path, kw, expect):
    """The fix must not have widened anything."""
    v = rails(tmp_path, binding=observation(**kw).as_arm_binding()).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert not v.allowed and v.rail == expect


def test_kill_still_outranks_a_valid_binding(tmp_path):
    (tmp_path / "KILL").write_text("stop")
    v = rails(tmp_path, binding=observation().as_arm_binding()).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert v.rail == "kill_switch"


def test_news_still_outranks_a_valid_binding(tmp_path):
    class Stale:
        def verdict(self, now=None): return (False, nf.REFUSE_STALE, "calendar 9h old")
    v = rails(tmp_path, binding=observation().as_arm_binding(), news=Stale()).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert v.rail == nf.REFUSE_STALE


def test_dry_run_still_bypasses_the_arm_rail_only(tmp_path):
    r = rails(tmp_path, binding=observation().as_arm_binding(), mode="dry_run")
    assert r._arm_verdict(open_intent()) is None


def test_no_account_value_is_ever_defaulted_from_config_or_token(tmp_path):
    """The binding must come from the observation alone."""
    lines = _code_lines(REPO_ROOT / "live" / "account_observation.py")
    start = next(n for n, l in lines if l.strip().startswith("def as_arm_binding"))
    body = [l for n, l in lines if n >= start][:10]
    for leak in ("config", "token", "LiveConfig", "arm_token"):
        assert not [l for l in body if leak in l], \
            f"as_arm_binding fell back to {leak}: {body}"


def test_no_login_is_published_by_the_observation_mapping(tmp_path):
    """The binding carries the login (the rail needs it); the PUBLISHED
    telemetry must still reduce it to a fingerprint elsewhere."""
    from live.telemetry import safe_identity
    ident = safe_identity(observation().as_observed_mapping()["identity"])
    assert "login" not in json.dumps(ident)
    assert str(LOGIN) not in json.dumps(ident)
