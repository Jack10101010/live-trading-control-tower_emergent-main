"""LX-1 Slice 7 — account-health & capital rail.

Covers the pure evaluator, strict config policy, the non-throwing gateway
accessor, and executor integration (once-per-cycle sampling, OPEN-only,
fail-closed, CLOSE/MODIFY exempt, health-before-market precedence, zero
submissions). Runtime health only — no identity reverification, live stays refused.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest                                              # noqa: E402
from live.config import LiveConfig                         # noqa: E402
from live.executor import Executor                         # noqa: E402
from live.mt5_gateway import MT5Gateway                    # noqa: E402
from live.intents import OrderIntent, OPEN_POSITION, CLOSE_POSITION, MODIFY_STOP  # noqa: E402
from live.account_health import (                          # noqa: E402
    AccountHealth, HealthPolicy, HealthConfigError, evaluate_health, parse_min_equity)
from live.state import RunnerState, LEDGER_BLOCKED, LEDGER_CONFIRMED   # noqa: E402
import _fake_mt5 as F                                      # noqa: E402

_SI = F.make_symbol_info(volume_step=0.01, filling_mode=F.SYMBOL_FILLING_IOC)
POLICY = HealthPolicy(min_equity=5_000.0, expected_currency="EUR")


def _h(**kw):
    base = dict(currency="EUR", balance=10_000.0, equity=10_000.0, free_margin=10_000.0,
                trade_allowed=True, trade_expert=True)
    base.update(kw)
    return AccountHealth(**base)


# ── pure evaluator ──────────────────────────────────────────────────────────────

def test_valid_health_allowed():
    v = evaluate_health(_h(), POLICY)
    assert v.allowed and v.reasons == () and v.evidence["equity"] == 10_000.0


def test_unavailable_health():
    v = evaluate_health(None, POLICY)
    assert not v.allowed and v.reasons == ("account_health_unavailable",) and v.evidence == {}


def test_wrong_object_type():
    assert evaluate_health(SimpleNamespace(equity=9e9), POLICY).reasons == ("account_health_unavailable",)


def test_trading_disabled():
    assert evaluate_health(_h(trade_allowed=False), POLICY).reasons == ("trading_not_allowed",)


def test_expert_disabled():
    assert evaluate_health(_h(trade_expert=False), POLICY).reasons == ("expert_trading_not_allowed",)


def test_currency_mismatch():
    assert evaluate_health(_h(currency="USD"), POLICY).reasons == ("currency_mismatch",)


def test_equity_below_floor():
    assert evaluate_health(_h(equity=4_999.99), POLICY).reasons == ("equity_floor",)


def test_equity_exactly_at_floor_allowed():
    assert evaluate_health(_h(equity=5_000.0), POLICY).allowed


def test_equity_above_floor_allowed():
    assert evaluate_health(_h(equity=5_000.01), POLICY).allowed


def test_multiple_failures_deterministic_order():
    v = evaluate_health(_h(trade_allowed=False, trade_expert=False, currency="USD", equity=1.0), POLICY)
    assert v.reasons == ("trading_not_allowed", "expert_trading_not_allowed",
                         "currency_mismatch", "equity_floor")


def test_verdict_json_safe_bounded_no_credentials():
    blob = json.dumps(evaluate_health(_h(), POLICY).to_dict())
    assert "password" not in blob and "server" not in blob and "login" not in blob
    assert json.loads(blob)["allowed"] is True


# ── config policy ───────────────────────────────────────────────────────────────

def test_parse_min_equity_valid():
    assert parse_min_equity("5000") == 5_000.0 and parse_min_equity("0.01") == 0.01


@pytest.mark.parametrize("raw", [None, "", "   ", "0", "-1", "nan", "inf", "-inf", "abc", True])
def test_parse_min_equity_malformed_fails(raw):
    with pytest.raises(HealthConfigError):
        parse_min_equity(raw)


def test_config_health_policy_end_to_end(monkeypatch):
    monkeypatch.setenv("LIVE_MIN_EQUITY", "5000")
    pol = LiveConfig().health_policy()
    assert pol.min_equity == 5_000.0 and pol.expected_currency == "EUR"


def test_config_health_policy_equality_semantics():
    pol = LiveConfig(min_equity_raw="5000").health_policy()
    assert evaluate_health(_h(equity=5_000.0), pol).allowed          # equality passes
    assert not evaluate_health(_h(equity=4_999.99), pol).allowed


def test_config_missing_min_equity_fails_closed():
    with pytest.raises(HealthConfigError):
        LiveConfig(min_equity_raw="").health_policy()


def test_config_malformed_expected_currency_fails():
    from live.account_identity import IdentityConfigError
    with pytest.raises((HealthConfigError, IdentityConfigError)):
        LiveConfig(min_equity_raw="5000", expected_currency="E UR!").health_policy()


def test_config_construction_stays_cheap():
    r = subprocess.run([sys.executable, "-c", "from live.config import LiveConfig; LiveConfig()"],
                       cwd=str(REPO_ROOT), env={**os.environ}, capture_output=True, text=True)
    assert r.returncode == 0                                          # no LIVE_MIN_EQUITY -> still constructs


# ── gateway accessor ────────────────────────────────────────────────────────────

def _gw(tmp_path, account, *, connect=True):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K",
                     min_equity_raw="5000")
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.make_tick(1.1, 1.1002), symbol_info=_SI, account=account)
    gw = MT5Gateway(cfg, sdk=fake)
    if connect:
        ok, _ = gw.connect(); assert ok
    return fake, gw


def test_accessor_valid_healthy_account(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    h = gw.account_health()
    assert h is not None and h.currency == "EUR" and h.equity == 10_000.0
    assert h.trade_allowed is True and h.trade_expert is True and h.free_margin == 10_000.0


def test_accessor_disconnected_none(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account(), connect=False)
    assert gw.account_health() is None


def test_accessor_account_info_none(tmp_path):
    fake, gw = _gw(tmp_path, None)
    assert gw.account_health() is None


def test_accessor_account_info_raises_none(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    def boom():
        raise RuntimeError("boom")
    fake.account_info = boom
    assert gw.account_health() is None


def test_accessor_lowercase_currency_normalized(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account(currency="eur"))
    assert gw.account_health().currency == "EUR"


@pytest.mark.parametrize("field,val", [
    ("currency", None), ("currency", ""), ("currency", "  "), ("currency", "TOOLONGCUR"), ("currency", 5),
    ("balance", None), ("balance", True), ("balance", -1.0), ("balance", float("nan")), ("balance", float("inf")),
    ("equity", None), ("equity", True), ("equity", -0.01), ("equity", float("nan")), ("equity", float("inf")),
    ("margin_free", None), ("margin_free", True), ("margin_free", -1.0), ("margin_free", float("nan")),
    ("trade_allowed", None), ("trade_allowed", 1), ("trade_allowed", "yes"),
    ("trade_expert", None), ("trade_expert", 0), ("trade_expert", "no"),
])
def test_accessor_malformed_field_returns_none(tmp_path, field, val):
    acct = F.make_account()
    setattr(acct, field, val)
    fake, gw = _gw(tmp_path, acct)
    assert gw.account_health() is None
    assert fake.order_send_calls == []                               # no broker mutation


class _PropRaise:
    def __init__(self, which):
        self._w = which
    def _v(self, n, ok):
        if self._w == n:
            raise RuntimeError(n + " boom")
        return ok
    @property
    def currency(self):
        return self._v("currency", "EUR")
    @property
    def balance(self):
        return self._v("balance", 10_000.0)
    @property
    def equity(self):
        return self._v("equity", 10_000.0)
    @property
    def margin_free(self):
        return self._v("margin_free", 10_000.0)
    @property
    def trade_allowed(self):
        return self._v("trade_allowed", True)
    @property
    def trade_expert(self):
        return self._v("trade_expert", True)


@pytest.mark.parametrize("which", ["currency", "balance", "equity", "margin_free",
                                   "trade_allowed", "trade_expert"])
def test_accessor_property_raises_returns_none(tmp_path, which):
    fake, gw = _gw(tmp_path, _PropRaise(which))
    assert gw.account_health() is None


def test_accessor_conversion_raises_none(tmp_path):
    class BadFloat:
        def __float__(self):
            raise RuntimeError("float boom")
    acct = F.make_account()
    acct.equity = BadFloat()
    fake, gw = _gw(tmp_path, acct)
    assert gw.account_health() is None


def test_accessor_no_sdk_object_or_credentials(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    blob = json.dumps(gw.account_health().to_dict())
    assert "SimpleNamespace" not in blob and "login" not in blob and "server" not in blob


def test_accessor_single_account_info_call(tmp_path):
    fake, gw = _gw(tmp_path, F.make_account())
    gw.account_health()
    assert fake.account_info_calls == 1                              # exactly one call


# ── executor integration ────────────────────────────────────────────────────────

def _exec(tmp_path, account, *, order_result=..., mode="live", positions=None):
    cfg = LiveConfig(lux_root=tmp_path / "l", state_dir=tmp_path / "s",
                     market_data_dir=tmp_path / "m", kill_file=tmp_path / "s" / "K",
                     min_equity_raw="5000")
    cfg.mode = mode
    cfg.ensure_dirs()
    fake = F.FakeMT5(tick=F.fresh_tick(1.10101, 1.10123), symbol_info=_SI, account=account,
                     order_result=order_result, positions=positions or [])
    gw = MT5Gateway(cfg, sdk=fake); ok, _ = gw.connect(); assert ok
    st = RunnerState(cfg.state_dir)
    arm = F.make_arm_runtime() if mode == "live" else None
    return fake, st, gw, Executor(cfg, st, gw, arm_runtime=arm)


def _open(iid="o1", tid="T1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id=tid, side="long",
                       frontier_bar="B1", stop=1.09, target=1.11)


def test_healthy_open_proceeds(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=555, volume=0.01))
    res = ex.apply([_open()])
    assert res["frozen"] is False and st.ledger_status("o1") == LEDGER_CONFIRMED
    assert len(fake.order_send_calls) == 1


def test_below_floor_blocks(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(equity=4_000.0))
    res = ex.apply([_open()])
    assert res["blocked"] and res["blocked"][0]["rail"] == "equity_floor"
    assert st.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0


def test_trade_disabled_blocks(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(trade_allowed=False))
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "trading_not_allowed" and len(fake.order_send_calls) == 0


def test_expert_disabled_blocks(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(trade_expert=False))
    assert ex.apply([_open()])["blocked"][0]["rail"] == "expert_trading_not_allowed"
    assert len(fake.order_send_calls) == 0


def test_currency_mismatch_blocks(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(currency="USD"))
    assert ex.apply([_open()])["blocked"][0]["rail"] == "currency_mismatch"
    assert len(fake.order_send_calls) == 0


def test_unavailable_health_blocks(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, None)                         # account_info None -> health None
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "account_health_unavailable"
    assert len(fake.order_send_calls) == 0


def test_accessor_raises_blocks_no_escape(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account())
    def boom():
        raise RuntimeError("hostile")
    gw.account_health = boom
    res = ex.apply([_open()])                                        # must not raise
    assert res["blocked"][0]["rail"] == "account_health_unavailable"
    assert len(fake.order_send_calls) == 0


def test_blocked_ledger_detail_json_safe(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(equity=100.0))
    ex.apply([_open()])
    json.dumps(st.data["ledger"]["o1"]["detail"])


def test_health_sampled_once_for_multiple_opens(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.01))
    hc = {"n": 0}; mc = {"n": 0}
    oh, om = gw.account_health, gw.market_condition
    gw.account_health = lambda: (hc.__setitem__("n", hc["n"] + 1), oh())[1]
    gw.market_condition = lambda: (mc.__setitem__("n", mc["n"] + 1), om())[1]
    ex.apply([_open("o1", "T1"), _open("o2", "T2")])
    assert hc["n"] == 1 and mc["n"] == 1                            # each sampled once


def test_reconcile_freeze_prevents_both_samples(tmp_path):
    orphan = F.make_position(999, 0, 0.01, comment="x", magic=77001, symbol="EURUSD")
    fake, st, gw, ex = _exec(tmp_path, F.make_account(), positions=[orphan])
    hc = {"n": 0}; mc = {"n": 0}
    oh, om = gw.account_health, gw.market_condition
    gw.account_health = lambda: (hc.__setitem__("n", hc["n"] + 1), oh())[1]
    gw.market_condition = lambda: (mc.__setitem__("n", mc["n"] + 1), om())[1]
    res = ex.apply([_open()])
    assert res["frozen"] is True and hc["n"] == 0 and mc["n"] == 0
    assert len(fake.order_send_calls) == 0


def _count_samples(gw):
    hc = {"n": 0}; mc = {"n": 0}
    oh, om = gw.account_health, gw.market_condition
    gw.account_health = lambda: (hc.__setitem__("n", hc["n"] + 1), oh())[1]
    gw.market_condition = lambda: (mc.__setitem__("n", mc["n"] + 1), om())[1]
    return hc, mc


def test_close_only_cycle_no_samples(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account())
    st.mirror_set("T1", 555); st.save()
    hc, mc = _count_samples(gw)
    ex.apply([OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="T1", side="long", frontier_bar="B")])
    assert hc["n"] == 0 and mc["n"] == 0


def test_modify_only_cycle_no_samples(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account())
    st.mirror_set("T1", 555); st.save()
    hc, mc = _count_samples(gw)
    ex.apply([OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="T1", side="long",
                          frontier_bar="B", stop=1.095)])
    assert hc["n"] == 0 and mc["n"] == 0


def test_mixed_open_close_samples_once(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.01))
    st.mirror_set("T2", 555); st.save()
    hc, mc = _count_samples(gw)
    ex.apply([_open("o1", "T1"),
              OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="T2", side="long", frontier_bar="B")])
    assert hc["n"] == 1 and mc["n"] == 1                            # sampled once despite the CLOSE


def test_health_precedes_market(tmp_path):
    # bad health AND wide spread -> health reason wins (evaluated first)
    fake, st, gw, ex = _exec(tmp_path, F.make_account(equity=1.0))
    gw.market_condition = lambda: __import__("live.safety", fromlist=["MarketCondition"]).MarketCondition(
        "EURUSD", 1.1000, 1.1010, *(2 * (__import__("datetime").datetime.now(__import__("datetime").timezone.utc),)))
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "equity_floor"              # health precedes stale/spread


def test_dry_run_connected_evaluates_without_submitting(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.make_account(equity=100.0), mode="dry_run")
    res = ex.apply([_open()])
    assert res["blocked"][0]["rail"] == "equity_floor"
    assert st.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0
