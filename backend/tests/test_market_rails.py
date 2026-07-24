"""LX-1 Slice 5 — pre-trade market-condition rails (spread_ceiling, stale_feed).

Pure rail matrix (SafetyRails._market_verdict / evaluate over constructed
MarketConditions), the gateway read accessor, and executor integration proving
OPEN-only gating, once-per-cycle sampling, fail-closed behaviour, CLOSE/MODIFY
exemption, reconcile-freeze precedence, and zero broker submissions.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
from live.safety import (SafetyRails, MarketCondition, MARKET_NOT_EVALUATED,      # noqa: E402
                         ALLOWED)
from live.state import RunnerState, LEDGER_BLOCKED, LEDGER_CONFIRMED             # noqa: E402
import _fake_mt5 as F                                      # noqa: E402

_SI = F.make_symbol_info(volume_step=0.01, filling_mode=F.SYMBOL_FILLING_IOC)
_TICK = (1.10101, 1.10123)          # 2.2-pip spread, below the 5-pip default ceiling
NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _cfg(tmp_path, mode="live"):
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "st",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "st" / "KILL")
    c.mode = mode
    c.ensure_dirs()
    return c


def _rails(tmp_path):
    cfg = _cfg(tmp_path)
    return cfg, SafetyRails(cfg, RunnerState(cfg.state_dir))


def _mc(bid=1.10001, ask=1.10021, *, age_s=1.0, tick_time=None, server_time=NOW):
    tt = tick_time if tick_time is not None else server_time - timedelta(seconds=age_s)
    return MarketCondition("EURUSD", bid, ask, tt, server_time)


def _open(iid="o1", tid="T1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id=tid, side="long",
                       frontier_bar="B1", stop=1.09, target=1.11)


# ── pure rail matrix ────────────────────────────────────────────────────────────

def test_fresh_below_ceiling_allowed(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc())
    assert v.allowed and v.rail == ALLOWED


def test_spread_exactly_at_ceiling_allowed(tmp_path):
    cfg, r = _rails(tmp_path)                              # ceiling 0.0005
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(bid=1.10000, ask=1.10050))
    assert v.allowed is True                               # equality allowed (block > only)


def test_spread_above_ceiling_blocks(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(bid=1.10000, ask=1.10051))
    assert not v.allowed and v.rail == "spread_ceiling"


def test_tick_exactly_at_max_age_allowed(tmp_path):
    cfg, r = _rails(tmp_path)                              # max_feed_age_s 90
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(age_s=cfg.max_feed_age_s))
    assert v.allowed is True                               # age == max allowed (block > only)


def test_tick_older_than_max_blocks(tmp_path):
    cfg, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(age_s=cfg.max_feed_age_s + 1))
    assert not v.allowed and v.rail == "stale_feed"


def test_missing_market_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", None)
    assert not v.allowed and v.rail == "stale_feed"


@pytest.mark.parametrize("bid,ask", [
    (float("nan"), 1.1002), (1.1000, float("nan")), (float("inf"), 1.1002),
    (1.1000, float("inf")), (0.0, 1.1002), (1.1000, 0.0), (-1.0, 1.1002),
])
def test_malformed_prices_block_stale(tmp_path, bid, ask):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(bid=bid, ask=ask))
    assert not v.allowed and v.rail == "stale_feed"


def test_ask_below_bid_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(bid=1.10050, ask=1.10000))
    assert not v.allowed and v.rail == "stale_feed"


def test_malformed_timestamps_block_stale(tmp_path):
    _, r = _rails(tmp_path)
    bad = MarketCondition("EURUSD", 1.1000, 1.1002, "not-a-time", NOW)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", bad)
    assert not v.allowed and v.rail == "stale_feed"


def test_tick_ahead_of_server_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    ahead = _mc(tick_time=NOW + timedelta(seconds=30))     # 30s in the future
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", ahead)
    assert not v.allowed and v.rail == "stale_feed"


def test_tick_within_clock_skew_allowed(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", _mc(tick_time=NOW + timedelta(seconds=1)))
    assert v.allowed is True                               # 1s ahead within skew tolerance


def test_close_exempt_from_market_rails(tmp_path):
    cfg, r = _rails(tmp_path)
    r.state.mirror_set("T1", 555)                          # CLOSE needs a mirrored ticket
    close = OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="T1", side="long",
                        frontier_bar="B1")
    v = r.evaluate(close, "EURUSD", "2026-07-24", None)    # bad market present
    assert v.allowed and v.rail == ALLOWED                 # never stale_feed


def test_modify_exempt_from_market_rails(tmp_path):
    cfg, r = _rails(tmp_path)
    r.state.mirror_set("T1", 555)
    mod = OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="T1", side="long",
                      frontier_bar="B1", stop=1.095)
    v = r.evaluate(mod, "EURUSD", "2026-07-24", None)
    assert v.allowed and v.rail == ALLOWED


def test_not_evaluated_sentinel_skips_market_rails(tmp_path):
    _, r = _rails(tmp_path)
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", MARKET_NOT_EVALUATED)
    assert v.allowed and v.rail == ALLOWED                 # no sampling -> rails skipped


# ── gateway read accessor ───────────────────────────────────────────────────────

def _gw(tmp_path, tick, *, connect=True):
    cfg = _cfg(tmp_path)
    fake = F.FakeMT5(tick=tick, symbol_info=_SI, account=F.make_account())
    gw = MT5Gateway(cfg, sdk=fake)
    if connect:
        ok, _ = gw.connect(); assert ok
    return fake, gw


def test_gateway_market_condition_valid(tmp_path):
    _, gw = _gw(tmp_path, F.fresh_tick(*_TICK))
    mc = gw.market_condition()
    assert isinstance(mc, MarketCondition) and mc.bid == _TICK[0] and mc.ask == _TICK[1]
    assert mc.server_time_utc.tzinfo is not None and mc.tick_time_utc.tzinfo is not None


def test_gateway_market_condition_not_connected_none(tmp_path):
    _, gw = _gw(tmp_path, F.fresh_tick(*_TICK), connect=False)
    assert gw.market_condition() is None


def test_gateway_market_condition_no_tick_none(tmp_path):
    _, gw = _gw(tmp_path, None)
    assert gw.market_condition() is None


@pytest.mark.parametrize("bid,ask,t", [
    (float("nan"), 1.1002, 1_800_000_000), (1.1000, float("inf"), 1_800_000_000),
    (0.0, 1.1002, 1_800_000_000), (1.1002, 1.1000, 1_800_000_000),  # ask<bid
    (True, 1.1002, 1_800_000_000), (1.1000, 1.1002, 0), (1.1000, 1.1002, float("nan")),
])
def test_gateway_market_condition_malformed_returns_none(tmp_path, bid, ask, t):
    from types import SimpleNamespace
    _, gw = _gw(tmp_path, SimpleNamespace(bid=bid, ask=ask, time=t))
    assert gw.market_condition() is None


# ── executor integration ────────────────────────────────────────────────────────

def _exec(tmp_path, tick, *, mode="live", positions=None, order_result=...):
    cfg = _cfg(tmp_path, mode)
    fake = F.FakeMT5(tick=tick, symbol_info=_SI, account=F.make_account(),
                     order_result=order_result, positions=positions or [])
    gw = MT5Gateway(cfg, sdk=fake)
    ok, _ = gw.connect(); assert ok                        # connected in both modes for these tests
    st = RunnerState(cfg.state_dir)
    return fake, st, gw, Executor(cfg, st, gw)


def test_sampled_once_for_multiple_opens(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.01))
    calls = {"n": 0}
    orig = gw.market_condition
    def counting():
        calls["n"] += 1
        return orig()
    gw.market_condition = counting
    ex.apply([_open("o1", "T1"), _open("o2", "T2")])
    assert calls["n"] == 1                                 # once per cycle, not per intent


def test_wide_spread_blocks_open_zero_send(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(1.1000, 1.1010))   # 10-pip spread
    res = ex.apply([_open()])
    assert res["blocked"] and res["blocked"][0]["rail"] == "spread_ceiling"
    assert st.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0


def test_stale_feed_blocks_open_zero_send(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK, age_s=3600))
    res = ex.apply([_open()])
    assert res["blocked"] and res["blocked"][0]["rail"] == "stale_feed"
    assert st.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0


def test_unavailable_tick_blocks_open_zero_send(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, None)               # symbol_info_tick -> None
    res = ex.apply([_open()])
    assert res["blocked"] and res["blocked"][0]["rail"] == "stale_feed"
    assert len(fake.order_send_calls) == 0


def test_normal_open_unchanged(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=555, volume=0.01))
    res = ex.apply([_open()])
    assert res["frozen"] is False and st.ledger_status("o1") == LEDGER_CONFIRMED
    assert st.mirror_ticket("T1") == 555 and len(fake.order_send_calls) == 1


def test_blocked_ledger_detail_json_safe(tmp_path):
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(1.1000, 1.1010))
    ex.apply([_open()])
    json.dumps(st.data["ledger"]["o1"]["detail"])          # must not raise


def test_reconcile_freeze_precedes_market_sampling(tmp_path):
    # An orphan magic-tagged position freezes reconcile; market must never be
    # sampled and no stale_feed finding must appear.
    orphan = F.make_position(999, 0, 0.01, comment="x", magic=77001, symbol="EURUSD")
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK), positions=[orphan])
    calls = {"n": 0}
    orig = gw.market_condition
    gw.market_condition = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    res = ex.apply([_open()])
    assert res["frozen"] is True and calls["n"] == 0        # sampling never reached
    assert len(fake.order_send_calls) == 0


def test_dry_run_connected_evaluates_and_records_without_submitting(tmp_path):
    # dry-run + connected: rails evaluate; a stale feed BLOCKS and records, and
    # nothing is submitted (dry-run never calls order_send anyway).
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK, age_s=3600), mode="dry_run")
    res = ex.apply([_open()])
    assert res["blocked"] and res["blocked"][0]["rail"] == "stale_feed"
    assert st.ledger_status("o1") == LEDGER_BLOCKED and len(fake.order_send_calls) == 0


# ── F1: non-throwing gateway accessor + executor fail-closed guard ──────────────

class _RaiseOn:
    """A tick whose chosen attribute raises on access (property) — models a
    hostile / broken SDK tick object."""
    def __init__(self, which):
        self._which = which
    def _val(self, name, ok):
        if self._which == name:
            raise RuntimeError(f"{name} access boom")
        return ok
    @property
    def bid(self):
        return self._val("bid", 1.10001)
    @property
    def ask(self):
        return self._val("ask", 1.10021)
    @property
    def time(self):
        return self._val("time", 1_800_000_000)


class _BadFloat:
    """A price object whose float() conversion raises (passes isinstance? no —
    it's not int/float, so it's rejected earlier; kept to prove no escape)."""
    def __float__(self):
        raise RuntimeError("float boom")


def _gw_raise_tick(tmp_path, tick_or_sdkfn):
    cfg = _cfg(tmp_path)
    fake = F.FakeMT5(tick=None, symbol_info=_SI, account=F.make_account())
    gw = MT5Gateway(cfg, sdk=fake)
    ok, _ = gw.connect(); assert ok
    tick_or_sdkfn(fake)
    return fake, gw


def test_gateway_symbol_info_tick_raises_returns_none(tmp_path):
    def setup(fake):
        def boom(symbol):
            raise RuntimeError("symbol_info_tick boom")
        fake.symbol_info_tick = boom
    fake, gw = _gw_raise_tick(tmp_path, setup)
    assert gw.market_condition() is None                    # no escape


@pytest.mark.parametrize("which", ["bid", "ask", "time"])
def test_gateway_tick_property_raises_returns_none(tmp_path, which):
    def setup(fake):
        fake._tick = _RaiseOn(which)
    fake, gw = _gw_raise_tick(tmp_path, setup)
    assert gw.market_condition() is None                    # no escape from a raising property


def test_gateway_bid_ask_conversion_raises_returns_none(tmp_path):
    # a non-numeric object for bid/ask -> rejected (None), no float() escape
    from types import SimpleNamespace
    def setup(fake):
        fake._tick = SimpleNamespace(bid=_BadFloat(), ask=1.10021, time=1_800_000_000)
    fake, gw = _gw_raise_tick(tmp_path, setup)
    assert gw.market_condition() is None


def test_gateway_timestamp_conversion_raises_returns_none(tmp_path):
    from types import SimpleNamespace
    def setup(fake):
        fake._tick = SimpleNamespace(bid=1.10001, ask=1.10021, time=10 ** 30)  # fromtimestamp overflows
    fake, gw = _gw_raise_tick(tmp_path, setup)
    assert gw.market_condition() is None


def test_gateway_accessor_never_mutates_broker(tmp_path):
    def setup(fake):
        fake._tick = _RaiseOn("bid")
    fake, gw = _gw_raise_tick(tmp_path, setup)
    gw.market_condition()
    assert fake.order_send_calls == []                      # read-only, no broker mutation


def test_executor_market_condition_raises_blocks_open_zero_send(tmp_path):
    # Even if the accessor itself raised (it won't, but defence-in-depth), the
    # executor converts it to unavailable -> OPEN blocks stale_feed, no submit.
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK),
                             order_result=F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.01))
    def boom():
        raise RuntimeError("mc boom")
    gw.market_condition = boom
    res = ex.apply([_open()])                                # must NOT raise
    assert res["blocked"] and res["blocked"][0]["rail"] == "stale_feed"
    assert st.ledger_status("o1") == LEDGER_BLOCKED
    json.dumps(st.data["ledger"]["o1"]["detail"])           # JSON-safe evidence
    assert len(fake.order_send_calls) == 0                  # no submit, no fall-through


# ── F2: freshness rail requires timezone-aware UTC evidence ─────────────────────

def test_naive_tick_time_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    m = MarketCondition("EURUSD", 1.10001, 1.10021,
                        datetime(2026, 7, 24, 11, 59, 59), NOW)   # naive tick, aware server
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", m)
    assert not v.allowed and v.rail == "stale_feed"


def test_naive_reference_time_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    m = MarketCondition("EURUSD", 1.10001, 1.10021,
                        NOW - timedelta(seconds=1), datetime(2026, 7, 24, 12, 0, 0))  # aware tick, naive server
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", m)
    assert not v.allowed and v.rail == "stale_feed"


def test_both_naive_blocks_stale(tmp_path):
    _, r = _rails(tmp_path)
    m = MarketCondition("EURUSD", 1.10001, 1.10021,
                        datetime(2026, 7, 24, 11, 59, 59), datetime(2026, 7, 24, 12, 0, 0))
    v = r.evaluate(_open(), "EURUSD", "2026-07-24", m)
    assert not v.allowed and v.rail == "stale_feed"


def test_close_only_cycle_never_samples(tmp_path):
    # No OPEN in the cycle -> market never sampled (CLOSE unaffected by new rails).
    fake, st, gw, ex = _exec(tmp_path, F.fresh_tick(*_TICK))
    st.mirror_set("T1", 555); st.save()
    calls = {"n": 0}
    orig = gw.market_condition
    gw.market_condition = lambda: (calls.__setitem__("n", calls["n"] + 1), orig())[1]
    close = OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="T1", side="long",
                        frontier_bar="B1")
    ex.apply([close])
    assert calls["n"] == 0
