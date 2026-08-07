"""Regression repairs — each test reproduces a defect the forensic audit proved.

The audit demonstrated six reproducible defects introduced or exposed by the
hardening milestone. Every one of them now has a test that FAILS against the
pre-repair code:

  * broker-side exits (SL/TP/manual) never reached the daily-loss counter;
  * a blocked/frozen annotation overwrote an acted-on ledger entry, so the
    SECOND replay re-executed the intent (duplicate order);
  * the server clock was modelled on Europe/Athens although this broker
    switches on the US DST calendar (~4 weeks/yr wrong by one hour);
  * provenance was written at construction, letting an uncertified segment
    certify itself — and never refreshed on rebuild, latching the deployment
    into permanent refusal;
  * deploy_check acquired a write side effect;
  * the running-cycle heartbeat blanked the last processed boundary.

Offline: the SDK, gateway and strategy pipeline are injected fakes.
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

from live.config import TIME_BASE, LiveConfig                              # noqa: E402
from live.executor import Executor                                          # noqa: E402
from live.intents import MODIFY_STOP, OrderIntent, diff_frontier            # noqa: E402
from live.mt5_bridge import MT5BarBridge, verify_segment_time_base          # noqa: E402
from live.mt5_gateway import MT5Gateway                                     # noqa: E402
from live.ops_log import OpsLog                                             # noqa: E402
from live.runner import LiveRunner                                          # noqa: E402
from live.state import LEDGER_SIMULATED, RunnerState                        # noqa: E402

COLS = ["trade_id", "direction", "fill_time", "outcome", "entry", "stop", "tp", "net_r"]


def _cfg(tmp_path, **kw) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    for k, v in kw.items():
        setattr(c, k, v)
    c.ensure_dirs()
    return c


def _armed(cfg):
    """LR-1/crash tests assert REPLAY semantics in live mode; the arm rail is
    covered by test_arming_and_stale_open. Give them a valid operator arm so
    they exercise what they are for."""
    from live.arming import ArmRuntime
    return ArmRuntime.create(cfg.state_dir, login=9000000001, server="FTMO-Demo",
                             mode="live", ttl_minutes=60, max_opens=9)


def _frame(rows):
    from conftest import with_identity_anchors
    cols = COLS + ["ob_id", "detection_time"]
    return pd.DataFrame([{c: r.get(c, "") for c in cols}
                         for r in with_identity_anchors(rows)]).astype(str)


def _server_epoch(wall: datetime) -> int:
    return int(wall.replace(tzinfo=timezone.utc).timestamp())


class _Tick:
    def __init__(self, epoch): self.time = epoch; self.bid = 1.1; self.ask = 1.10002


class _TermInfo:
    def __init__(self, connected=True): self.connected = connected


class FakeMT5:
    TIMEFRAME_M1 = 1

    def __init__(self, tick_wall, bars_wall=(), connected=True):
        self.tick_epoch = _server_epoch(tick_wall)
        self.bars = [{"time": _server_epoch(w), "open": 1.1, "high": 1.2, "low": 1.0,
                      "close": 1.15, "tick_volume": 7.0} for w in bars_wall]
        self.connected = connected
        self.init_calls = 0

    def initialize(self, **kw): self.init_calls += 1; return True
    def shutdown(self): pass
    def terminal_info(self): return _TermInfo(self.connected)
    def symbol_info_tick(self, symbol): return _Tick(self.tick_epoch)
    def last_error(self): return (0, "ok")
    def copy_rates_range(self, symbol, tf, start, end): return list(self.bars)


class BrokerGW:
    """Fake broker. `positions` is what reconcile will see."""
    def __init__(self, positions=None):
        self.positions = positions or []
        self.closes = 0

    def snapshot(self):
        return True, {"account": {"login": 1}, "positions": self.positions, "orders": []}

    def close_position(self, ticket):
        self.closes += 1
        return True, {"ticket": ticket, "closed": True}

    def open_position(self, *a, **k): return True, {"ticket": 555, "price": 1.1, "volume": 0.01}
    def modify_position_sl(self, *a, **k): return True, {}


def _close_intents(net_r="-1.05", tid="L_1", bar="b1"):
    prev = _frame([{"trade_id": tid, "direction": "bullish", "fill_time": "t", "outcome": "OPEN"}])
    cur = _frame([{"trade_id": tid, "direction": "bullish", "fill_time": "t",
                   "outcome": "LOSS", "net_r": net_r}])
    return diff_frontier(prev, cur, bar)


# ══════════════════════════════════════════════════════════════════════════════
# 1. DAILY-LOSS: broker-side exits must always contribute realised R
# ══════════════════════════════════════════════════════════════════════════════
def test_broker_side_exit_still_accrues_realized_r(tmp_path):
    """The dominant loss path: SL fires server-side, reconcile drops the mirror.

    Pre-repair the engine's CLOSE was rejected as `unknown_position` and the
    loss never reached the counter — the rail could not cap what it exists for.
    """
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    gw = BrokerGW(positions=[])                      # position gone at the broker
    ex = Executor(cfg, st, gw)
    st.mirror_set("L_1", 555)
    res = ex.apply(_close_intents(), today="2026-07-28")
    assert [f["code"] for f in res["reconcile"]["findings"]] == ["missing_position"]
    assert len(res["applied"]) == 1
    assert gw.closes == 0                            # no redundant broker call
    assert st.daily_realized_r("2026-07-28") == pytest.approx(-1.05)
    assert st.broker_closed_ticket("L_1") is None    # marker consumed


def test_engine_close_sends_exactly_one_close_and_counts_once(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    gw = BrokerGW(positions=[{"ticket": 555, "magic": cfg.magic_number, "symbol": "EURUSD"}])
    ex = Executor(cfg, st, gw)
    st.mirror_set("L_1", 555)
    ex.apply(_close_intents(), today="2026-07-28")
    assert gw.closes == 1
    assert st.daily_realized_r("2026-07-28") == pytest.approx(-1.05)


def test_broker_exit_replayed_many_times_counts_once(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, BrokerGW(positions=[]))
    st.mirror_set("L_1", 555)
    ints = _close_intents()
    for _ in range(5):
        ex.apply(ints, today="2026-07-28")
    assert st.daily_realized_r("2026-07-28") == pytest.approx(-1.05)


def test_modify_stop_after_broker_exit_stays_blocked(tmp_path):
    """Only CLOSE is legitimate after a broker exit; nothing remains to modify."""
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    st.mark_broker_closed("L_1", 555)
    ex = Executor(cfg, st, BrokerGW(positions=[]))
    intent = OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="L_1",
                         side="long", frontier_bar="b1", stop=1.05)
    res = ex.apply([intent], today="2026-07-28")
    assert res["blocked"][0]["rail"] == "unknown_position"


def test_partial_close_residual_freezes_and_never_double_counts(tmp_path):
    """A residual position after a close surfaces as FREEZE, not as extra R."""
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    gw = BrokerGW(positions=[{"ticket": 555, "magic": cfg.magic_number, "symbol": "EURUSD"}])
    ex = Executor(cfg, st, gw)
    st.mirror_set("L_1", 555)
    ex.apply(_close_intents(), today="2026-07-28")
    assert st.daily_realized_r("2026-07-28") == pytest.approx(-1.05)
    res2 = ex.apply(_close_intents(bar="b2"), today="2026-07-28")   # residual still there
    assert res2["frozen"] is True
    assert st.daily_realized_r("2026-07-28") == pytest.approx(-1.05)


def test_daily_counter_resets_by_utc_day_and_survives_restart(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, BrokerGW(positions=[]))
    st.mirror_set("L_1", 555)
    ex.apply(_close_intents(net_r="-2.5"), today="2026-07-28")
    assert RunnerState(cfg.state_dir).daily_realized_r("2026-07-28") == pytest.approx(-2.5)
    assert RunnerState(cfg.state_dir).daily_realized_r("2026-07-29") == 0.0


def test_dry_run_and_live_share_one_accounting_path(tmp_path):
    """Same helper, same result — only the broker call differs."""
    dry = _cfg(tmp_path / "d", mode="dry_run")
    st_d = RunnerState(dry.state_dir); st_d.mirror_set("L_1", -1)
    Executor(dry, st_d, gateway=None).apply(_close_intents(), today="2026-07-28")
    live = _cfg(tmp_path / "l", mode="live")
    st_l = RunnerState(live.state_dir); st_l.mirror_set("L_1", 555)
    Executor(live, st_l, BrokerGW(positions=[])).apply(_close_intents(), today="2026-07-28")
    assert st_d.daily_realized_r("2026-07-28") == st_l.daily_realized_r("2026-07-28")


# ══════════════════════════════════════════════════════════════════════════════
# 2. LR-1: unlimited replay safety
# ══════════════════════════════════════════════════════════════════════════════
def test_annotation_never_overwrites_an_acted_on_ledger_entry(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.ledger_set("i1", LEDGER_SIMULATED)
    st.ledger_set("i1", "blocked", {"rail": "duplicate_intent"})
    st.ledger_set("i1", "frozen", {"reason": "reconcile_freeze"})
    assert st.ledger_status("i1") == LEDGER_SIMULATED           # terminal preserved
    assert st.data["ledger"]["i1"]["suppressed_annotations"] == 2
    st.ledger_set("i2", "sent")
    st.ledger_set("i2", "confirmed")
    assert st.ledger_status("i2") == "confirmed"                # real transitions still flow


def test_unlimited_replays_never_duplicate_execution(tmp_path):
    """Pre-repair the 3rd pass re-executed: blocked overwrote simulated."""
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, gateway=None, arm_runtime=_armed(cfg), observed_account={"login": 9000000001, "server": "FTMO-Demo"})
    prev = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t",
                   "outcome": "OPEN", "entry": "1.1", "stop": "1.0", "tp": "1.3"}])
    ints = diff_frontier(prev, cur, "b1")
    total = sum(len(ex.apply(ints, today="2026-07-28")["applied"]) for _ in range(10))
    assert total == 1
    assert st.ledger_status(ints[0].intent_id) == LEDGER_SIMULATED


UNFILLED = [{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}]
OPENED = [{"trade_id": "L_1", "direction": "bullish", "fill_time": "t1", "outcome": "OPEN",
           "entry": "1.1", "stop": "1.0", "tp": "1.3"}]
BAR1, BAR2 = "2026-07-28 08:14:00", "2026-07-28 08:29:00"


def _candles(last_minute):
    idx = pd.date_range(end=pd.Timestamp(last_minute), periods=3, freq="1min")
    return pd.DataFrame({"time": idx.astype(str)})


def _runner(cfg, frame, last_minute):
    return LiveRunner(cfg, session=None, pipeline=lambda c, d: frame,
                      candles_provider=lambda: _candles(last_minute))


def test_repeated_crash_recovery_across_restarts_executes_once(tmp_path):
    """Five consecutive crash-before-commit recoveries: identical ids, one apply."""
    cfg = _cfg(tmp_path)
    r0 = _runner(cfg, _frame(UNFILLED), BAR1)
    r0.run_once(defer_commit=True); r0.commit_cycle()
    ids, total = None, 0
    for _ in range(5):
        r = _runner(cfg, _frame(OPENED), BAR2)
        res = r.run_once(defer_commit=True)
        assert res["status"] == "ok"
        ids = ids or [i.intent_id for i in res["intents"]]
        assert [i.intent_id for i in res["intents"]] == ids       # replay determinism
        total += len(Executor(cfg, r.state, gateway=None, arm_runtime=_armed(cfg), observed_account={"login": 9000000001, "server": "FTMO-Demo"})
                     .apply(res["intents"], today="2026-07-28")["applied"])
    assert total == 1
    r = _runner(cfg, _frame(OPENED), BAR2)
    r.run_once(defer_commit=True); r.commit_cycle()
    assert _runner(cfg, _frame(OPENED), BAR2).run_once(defer_commit=True)["status"] == "no_new_bar"


# ══════════════════════════════════════════════════════════════════════════════
# 3. TIMEZONE: broker follows the US DST calendar (measured)
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("day,expected", [
    ("2026-03-01", 2.0),   # both calendars standard
    ("2026-03-10", 3.0),   # US switched 8 Mar, EU not -> Europe/Athens said +2 (WRONG)
    ("2026-03-25", 3.0),   # still inside the shoulder
    ("2026-04-01", 3.0),   # both on DST
    ("2026-10-20", 3.0),   # both on DST
    ("2026-10-27", 3.0),   # EU switched back 25 Oct, US not -> Athens said +2 (WRONG)
    ("2026-11-03", 2.0),   # both standard
])
def test_broker_offset_follows_us_dst_calendar(tmp_path, day, expected):
    gw = MT5Gateway(_cfg(tmp_path))
    assert gw.declared_offset_hours(datetime.fromisoformat(day).replace(tzinfo=timezone.utc)) == expected


def test_dst_shoulder_is_surfaced(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path))
    assert gw.dst_disagreement(datetime(2026, 3, 15, tzinfo=timezone.utc))
    assert not gw.dst_disagreement(datetime(2026, 7, 15, tzinfo=timezone.utc))


def test_other_dst_rules_remain_available(tmp_path):
    eu = MT5Gateway(_cfg(tmp_path, mt5_server_dst_rule="eu"))
    assert eu.declared_offset_hours(datetime(2026, 3, 10, tzinfo=timezone.utc)) == 2.0
    none = MT5Gateway(_cfg(tmp_path, mt5_server_dst_rule="none"))
    assert none.declared_offset_hours(datetime(2026, 7, 10, tzinfo=timezone.utc)) == 2.0


def test_invalid_dst_rule_fails_closed(tmp_path):
    with pytest.raises(ValueError):
        MT5Gateway(_cfg(tmp_path, mt5_server_dst_rule="atlantis"))


def test_bars_leaving_the_gateway_are_utc_across_the_dst_boundary(tmp_path):
    """The invariant every layer above the gateway depends on."""
    for wall, expect in ((datetime(2026, 1, 15, 12, 0), "2026-01-15 10:00:00+00:00"),
                         (datetime(2026, 7, 15, 12, 0), "2026-07-15 09:00:00+00:00")):
        sdk = FakeMT5(wall + timedelta(minutes=10), bars_wall=[wall])
        gw = MT5Gateway(_cfg(tmp_path), sdk=sdk)
        gw.connect()
        ok, bars = gw.closed_m1_bars(datetime(2020, 1, 1, tzinfo=timezone.utc))
        assert ok and bars[0]["time"] == expect


def test_epoch_round_trip_holds_on_both_sides_of_the_transition(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path))
    for utc in (datetime(2026, 3, 7, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 3, 9, 12, 0, tzinfo=timezone.utc),
                datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)):
        assert gw.server_epoch_to_utc(gw.utc_to_server_arg(utc).timestamp()) == utc


# ══════════════════════════════════════════════════════════════════════════════
# 4. PROVENANCE lifecycle
# ══════════════════════════════════════════════════════════════════════════════
def test_construction_writes_no_provenance(tmp_path):
    cfg = _cfg(tmp_path)
    MT5BarBridge(cfg, MT5Gateway(cfg, sdk=FakeMT5(datetime(2026, 7, 28, 11, 0))))
    assert not (cfg.market_data_dir / "provenance.json").exists()


def test_segment_without_provenance_is_refused(tmp_path):
    """Pre-repair this self-certified: the ctor stamped provenance, guard passed."""
    cfg = _cfg(tmp_path)
    cfg.live_segment_csv.write_text("time,open,high,low,close,volume\n"
                                    "2026-07-17 23:53:00+00:00,1.1,1.1,1.1,1.1,1\n")
    ok, detail = verify_segment_time_base(cfg)
    assert not ok and "NO provenance" in detail


def test_rebuild_refreshes_provenance_and_does_not_latch(tmp_path):
    """Pre-repair a legitimate rebuild latched into permanent refusal."""
    cfg = _cfg(tmp_path)
    (cfg.market_data_dir / "provenance.json").write_text(
        json.dumps({"time_base": "server-wallclock-v1"}))
    assert verify_segment_time_base(cfg)[0]                     # rebuild permitted
    bridge = MT5BarBridge(cfg, MT5Gateway(cfg, sdk=FakeMT5(datetime(2026, 7, 28, 11, 0))))
    bridge.append_bars([{"time": "2026-07-28 08:05:00+00:00", "open": 1.1, "high": 1.2,
                         "low": 1.0, "close": 1.15, "volume": 3.0}])
    stored = json.loads((cfg.market_data_dir / "provenance.json").read_text())
    assert stored["time_base"] == TIME_BASE
    assert stored["superseded_time_base"] == "server-wallclock-v1"
    assert verify_segment_time_base(cfg)[0]                     # no latch


def test_mixed_time_base_still_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.market_data_dir / "provenance.json").write_text(
        json.dumps({"time_base": "server-wallclock-v1"}))
    cfg.live_segment_csv.write_text("time,open,high,low,close,volume\n")
    ok, detail = verify_segment_time_base(cfg)
    assert not ok and "not comparable" in detail


def test_segment_gate_is_pure(tmp_path):
    cfg = _cfg(tmp_path)
    before = sorted(p.name for p in cfg.market_data_dir.iterdir())
    verify_segment_time_base(cfg)
    assert sorted(p.name for p in cfg.market_data_dir.iterdir()) == before


# ══════════════════════════════════════════════════════════════════════════════
# 5. RUNTIME ROBUSTNESS
# ══════════════════════════════════════════════════════════════════════════════
def test_reconnect_revalidates_the_time_base(tmp_path):
    """A reconnect can land on a different broker; the clock must be re-checked."""
    now = datetime.now(timezone.utc)
    ref = MT5Gateway(_cfg(tmp_path))
    good = (now + ref.server_utc_offset(now)).replace(tzinfo=None)
    sdk = FakeMT5(good)
    gw = MT5Gateway(_cfg(tmp_path), sdk=sdk)
    assert gw.connect()[0]
    sdk.connected = False
    assert gw.ensure_connected() == (True, "reconnected")
    sdk.connected = False
    sdk.tick_epoch = _server_epoch(good + timedelta(hours=5))    # different broker zone
    ok, detail = gw.ensure_connected()
    assert not ok and "time base rejected" in detail


def test_heartbeat_keeps_last_boundary_while_a_cycle_runs(tmp_path):
    ops = OpsLog(tmp_path)
    ops.cycle_end(ops.cycle_start(), boundary="2026-07-28 08:15:00+00:00", status="ok")
    ops.cycle_start()
    hb = json.loads((tmp_path / "ops" / "heartbeat.json").read_text())
    assert hb["phase"] == "cycle_running"
    assert hb["boundary"] == "2026-07-28 08:15:00+00:00"


def test_heartbeat_boundary_survives_process_restart(tmp_path):
    ops = OpsLog(tmp_path)
    ops.cycle_end(ops.cycle_start(), boundary="2026-07-28 08:15:00+00:00", status="ok")
    OpsLog(tmp_path).cycle_start()                       # fresh process
    hb = json.loads((tmp_path / "ops" / "heartbeat.json").read_text())
    assert hb["boundary"] == "2026-07-28 08:15:00+00:00"
