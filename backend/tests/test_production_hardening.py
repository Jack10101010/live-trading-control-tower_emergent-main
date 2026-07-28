"""Production-hardening tests: MT5 time base, safety rails, LR-1, resilience.

Every test here pins a behaviour that was previously wrong or unprovable:
  * MT5 encodes times as broker wall clock — the gateway must convert (DST-aware).
  * The daily-loss rail had no data source and could never fire.
  * The instrument rail compared a constant with itself.
  * LR-1: committing the boundary before execution lost orders permanently.
  * The MT5 link was never re-established after a terminal restart.

No MetaTrader5 package, network or Lux pipeline is required — the SDK and the
strategy pipeline are injected fakes, so these run identically off-VPS.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.config import SYMBOL, TIME_BASE, LiveConfig          # noqa: E402
from live.executor import Executor                              # noqa: E402
from live.intents import CLOSE_POSITION, OPEN_POSITION, diff_frontier  # noqa: E402
from live.mt5_bridge import MT5BarBridge                        # noqa: E402
from live.mt5_gateway import MT5Gateway                         # noqa: E402
from live.ops_log import OpsLog                                 # noqa: E402
from live.runner import LiveRunner                              # noqa: E402
from live.safety import SafetyRails                             # noqa: E402
from live.state import LEDGER_SIMULATED, RunnerState            # noqa: E402

TRADE_COLS = ["trade_id", "direction", "fill_time", "outcome",
              "entry", "stop", "tp", "net_r"]


def _cfg(tmp_path, **kw) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    for k, v in kw.items():
        setattr(c, k, v)
    c.ensure_dirs()
    return c


def _frame(rows):
    return pd.DataFrame([{c: r.get(c, "") for c in TRADE_COLS} for r in rows]).astype(str)


def _server_epoch(wall: datetime) -> int:
    """Encode a broker wall clock the way MT5 does: as if it were UTC."""
    return int(wall.replace(tzinfo=timezone.utc).timestamp())


# ── fake MT5 SDK ────────────────────────────────────────────────────────────────
class _Tick:
    def __init__(self, epoch): self.time = epoch; self.bid = 1.1; self.ask = 1.10002


class _TerminalInfo:
    def __init__(self, connected=True): self.connected = connected


class FakeMT5:
    TIMEFRAME_M1 = 1

    def __init__(self, tick_wall: datetime, bars_wall=(), connected=True):
        self.tick_epoch = _server_epoch(tick_wall)
        self.bars = [{"time": _server_epoch(w), "open": 1.1, "high": 1.2,
                      "low": 1.0, "close": 1.15, "tick_volume": 7.0} for w in bars_wall]
        self.connected = connected
        self.range_args = None
        self.init_calls = 0
        self.shutdown_calls = 0

    def initialize(self, **kw): self.init_calls += 1; return True
    def shutdown(self): self.shutdown_calls += 1
    def terminal_info(self): return _TerminalInfo(self.connected)
    def symbol_info_tick(self, symbol): return _Tick(self.tick_epoch)
    def last_error(self): return (0, "ok")

    def copy_rates_range(self, symbol, tf, start, end):
        self.range_args = (start, end)
        return list(self.bars)


# ══════════════════════════════════════════════════════════════════════════════
# 1. MT5 TIME BASE
# ══════════════════════════════════════════════════════════════════════════════
def test_server_epoch_to_utc_applies_summer_offset(tmp_path):
    """EEST (+3): a bar stamped 11:05 server is 08:05 UTC."""
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="Europe/Athens"))
    assert gw.server_epoch_to_utc(_server_epoch(datetime(2026, 7, 28, 11, 5))) == \
        datetime(2026, 7, 28, 8, 5, tzinfo=timezone.utc)


def test_server_epoch_to_utc_is_dst_aware_not_hardcoded(tmp_path):
    """The SAME code must yield +2 in winter and +3 in summer — no fixed offset."""
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="Europe/Athens"))
    winter = gw.server_epoch_to_utc(_server_epoch(datetime(2026, 1, 15, 12, 0)))
    summer = gw.server_epoch_to_utc(_server_epoch(datetime(2026, 7, 15, 12, 0)))
    assert winter == datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)   # +2 EET
    assert summer == datetime(2026, 7, 15, 9, 0, tzinfo=timezone.utc)    # +3 EEST
    assert gw.declared_offset_hours(datetime(2026, 1, 15, tzinfo=timezone.utc)) == 2.0
    assert gw.declared_offset_hours(datetime(2026, 7, 15, tzinfo=timezone.utc)) == 3.0


def test_utc_to_server_arg_round_trips(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="Europe/Athens"))
    for utc in (datetime(2026, 7, 28, 8, 5, tzinfo=timezone.utc),
                datetime(2026, 1, 15, 10, 0, tzinfo=timezone.utc)):
        assert gw.server_epoch_to_utc(gw.utc_to_server_arg(utc).timestamp()) == utc


def test_closed_m1_bars_converts_bounds_and_labels(tmp_path):
    """Bounds go out in server encoding; bar labels come back canonical UTC."""
    now_wall = datetime(2026, 7, 28, 11, 10)              # 08:10 UTC
    sdk = FakeMT5(now_wall, bars_wall=[datetime(2026, 7, 28, 11, 5)])
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="Europe/Athens"), sdk=sdk)
    gw.connect()
    ok, bars = gw.closed_m1_bars(datetime(2026, 7, 28, 8, 0, tzinfo=timezone.utc))
    assert ok
    # outbound bound carries the SERVER wall clock (11:00), not raw UTC (08:00);
    # passing raw UTC is what silently under-fetched 3h of bars.
    assert sdk.range_args[0].replace(tzinfo=None) == datetime(2026, 7, 28, 11, 0)
    # inbound label is canonical UTC
    assert bars[0]["time"] == "2026-07-28 08:05:00+00:00"


def test_verify_time_base_rejects_future_tick(tmp_path):
    """A converted tick in the future is the unmistakable wrong-zone signature."""
    sdk = FakeMT5(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=3))
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="UTC"), sdk=sdk)   # deliberately wrong zone
    gw.connect()
    ok, detail = gw.verify_time_base()
    assert not ok and "FUTURE" in detail


def test_verify_time_base_accepts_correctly_converted_tick(tmp_path):
    now_utc = datetime.now(timezone.utc)
    athens_wall = now_utc.astimezone(MT5Gateway(_cfg(tmp_path))._zone).replace(tzinfo=None)
    sdk = FakeMT5(athens_wall)
    gw = MT5Gateway(_cfg(tmp_path, mt5_server_tz="Europe/Athens"), sdk=sdk)
    gw.connect()
    ok, detail = gw.verify_time_base()
    assert ok and "verified" in detail


def test_bridge_refuses_segment_from_older_time_base(tmp_path):
    cfg = _cfg(tmp_path)
    bridge = MT5BarBridge(cfg, MT5Gateway(cfg, sdk=FakeMT5(datetime(2026, 7, 28, 11, 0))))
    prov = cfg.market_data_dir / "provenance.json"
    prov.write_text(json.dumps({"time_base": "server-wallclock-v1"}))
    cfg.live_segment_csv.write_text("time,open,high,low,close,volume\n")
    ok, detail = bridge.verify_time_base()
    assert not ok and "not comparable" in detail
    # fresh provenance written by this build is accepted
    prov.write_text(json.dumps({"time_base": TIME_BASE}))
    assert bridge.verify_time_base()[0]


# ══════════════════════════════════════════════════════════════════════════════
# 2. DAILY-LOSS RAIL  (previously dead: no production writer)
# ══════════════════════════════════════════════════════════════════════════════
def test_close_intent_carries_realized_r():
    prev = _frame([{"trade_id": "L_1", "direction": "bullish",
                    "fill_time": "2026-07-28 08:00:00+00:00", "outcome": "OPEN"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish",
                   "fill_time": "2026-07-28 08:00:00+00:00", "outcome": "LOSS",
                   "net_r": "-1.0483870967741884"}])
    (intent,) = diff_frontier(prev, cur, "2026-07-28 08:15:00+00:00")
    assert intent.action == CLOSE_POSITION
    assert intent.realized_r == pytest.approx(-1.0483870967741884)


def test_dry_run_close_accrues_realized_r_and_arms_the_rail(tmp_path):
    cfg = _cfg(tmp_path)
    state = RunnerState(cfg.state_dir)
    ex = Executor(cfg, state, gateway=None)
    state.mirror_set("L_1", -1)
    prev = _frame([{"trade_id": "L_1", "direction": "bullish",
                    "fill_time": "t0", "outcome": "OPEN"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t0",
                   "outcome": "LOSS", "net_r": "-3.0"}])
    ex.apply(diff_frontier(prev, cur, "b1"), today="2026-07-28")
    assert state.daily_realized_r("2026-07-28") == pytest.approx(-3.0)
    # survives a restart (atomic state write)
    assert RunnerState(cfg.state_dir).daily_realized_r("2026-07-28") == pytest.approx(-3.0)


def test_daily_loss_rail_blocks_opens_once_breached(tmp_path):
    cfg = _cfg(tmp_path, daily_loss_limit_r=5.0)
    state = RunnerState(cfg.state_dir)
    rails = SafetyRails(cfg, state)
    prev = _frame([{"trade_id": "L_9", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "L_9", "direction": "bullish", "fill_time": "t",
                   "outcome": "OPEN", "entry": "1.1", "stop": "1.0", "tp": "1.3"}])
    (open_intent,) = diff_frontier(prev, cur, "b")
    assert open_intent.action == OPEN_POSITION
    assert rails.evaluate(open_intent, SYMBOL, "2026-07-28").allowed
    state.add_realized_r(-5.0, "2026-07-28")
    verdict = rails.evaluate(open_intent, SYMBOL, "2026-07-28")
    assert not verdict.allowed and verdict.rail == "daily_loss_limit"


def test_realized_r_not_double_counted_on_replay(tmp_path):
    cfg = _cfg(tmp_path)
    state = RunnerState(cfg.state_dir)
    ex = Executor(cfg, state, gateway=None)
    state.mirror_set("L_1", -1)
    prev = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t0", "outcome": "OPEN"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t0",
                   "outcome": "LOSS", "net_r": "-2.0"}])
    intents = diff_frontier(prev, cur, "b1")
    ex.apply(intents, today="2026-07-28")
    ex.apply(intents, today="2026-07-28")            # replay identical intents
    assert state.daily_realized_r("2026-07-28") == pytest.approx(-2.0)


def test_intra_window_skip_does_not_consume_loss_budget(tmp_path):
    """SKIP_INTRA_WINDOW never reaches the broker, so it must not accrue R."""
    cfg = _cfg(tmp_path)
    state = RunnerState(cfg.state_dir)
    ex = Executor(cfg, state, gateway=None)
    prev = _frame([{"trade_id": "S_2", "direction": "bearish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "S_2", "direction": "bearish", "fill_time": "t",
                   "outcome": "LOSS", "net_r": "-1.0"}])
    res = ex.apply(diff_frontier(prev, cur, "b1"), today="2026-07-28")
    assert len(res["skipped"]) == 1
    assert state.daily_realized_r("2026-07-28") == 0.0


# ══════════════════════════════════════════════════════════════════════════════
# 3. INSTRUMENT RAIL  (previously vacuous: constant compared with itself)
# ══════════════════════════════════════════════════════════════════════════════
def test_instrument_rail_allows_whitelisted_symbol_with_suffix(tmp_path):
    cfg = _cfg(tmp_path, mt5_symbol_suffix=".r")
    rails = SafetyRails(cfg, RunnerState(cfg.state_dir))
    assert rails._instrument_allowed()


def test_instrument_rail_blocks_foreign_symbol(tmp_path):
    """Proves the rail can now actually fire.

    The old test was `symbol != SYMBOL` with SYMBOL supplied by the caller — a
    value compared with itself. The rail now validates the RESOLVED broker
    symbol. With the stock LiveConfig that value is `SYMBOL + suffix` by
    construction, so this is a contract check on the config object: a config
    whose resolved symbol is not the whitelisted instrument is rejected.
    """
    cfg = _cfg(tmp_path)
    stub = SimpleNamespace(kill_file=cfg.kill_file, broker_symbol="GBPUSD",
                           daily_loss_limit_r=5.0, max_open_positions=6)
    rails = SafetyRails(stub, RunnerState(cfg.state_dir))
    prev = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t",
                   "outcome": "OPEN", "entry": "1.1", "stop": "1.0", "tp": "1.3"}])
    (intent,) = diff_frontier(prev, cur, "b")
    verdict = rails.evaluate(intent, SYMBOL, "2026-07-28")
    assert not verdict.allowed and verdict.rail == "symbol_whitelist"


# ══════════════════════════════════════════════════════════════════════════════
# 4. LR-1 — reproduce the loss, then prove the fix
# ══════════════════════════════════════════════════════════════════════════════
def _candles(last_minute: str):
    idx = pd.date_range(end=pd.Timestamp(last_minute), periods=3, freq="1min")
    return pd.DataFrame({"time": idx.astype(str)})


def _runner(cfg, frame, last_minute):
    """Runner whose pipeline always returns `frame`; `last_minute` sets the boundary."""
    return LiveRunner(cfg, session=None,
                      pipeline=lambda candles, frontier_date: frame,
                      candles_provider=lambda: _candles(last_minute))


BAR1 = "2026-07-28 08:14:00"     # -> boundary 08:00
BAR2 = "2026-07-28 08:29:00"     # -> boundary 08:15


UNFILLED = [{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}]
OPENED = [{"trade_id": "L_1", "direction": "bullish", "fill_time": "t1",
           "outcome": "OPEN", "entry": "1.1", "stop": "1.0", "tp": "1.3"}]


def test_lr1_reproduction_eager_commit_loses_the_order(tmp_path):
    """CONTROLLED CRASH (pre-fix behaviour): commit-then-execute loses the OPEN."""
    cfg = _cfg(tmp_path)
    _runner(cfg, _frame(UNFILLED), BAR1).run_once()            # baseline @08:00
    res = _runner(cfg, _frame(OPENED), BAR2).run_once()        # OLD PATH: eager commit
    assert [i.action for i in res["intents"]] == [OPEN_POSITION]
    # ---- crash here: the executor never ran, yet the boundary is committed ----
    replay = _runner(cfg, _frame(OPENED), BAR2).run_once()
    assert replay["status"] == "no_new_bar"        # boundary already marked processed
    assert "intents" not in replay                 # ORDER LOST — the LR-1 defect
    assert RunnerState(cfg.state_dir).data["ledger"] == {}


def test_lr1_fixed_deferred_commit_replays_and_executes_exactly_once(tmp_path):
    """Same crash under the fix: the cycle replays and the order applies once."""
    cfg = _cfg(tmp_path)
    r1 = _runner(cfg, _frame(UNFILLED), BAR1)
    r1.run_once(defer_commit=True); r1.commit_cycle()          # baseline @08:00
    r2 = _runner(cfg, _frame(OPENED), BAR2)
    res = r2.run_once(defer_commit=True)
    first_ids = [i.intent_id for i in res["intents"]]
    assert [i.action for i in res["intents"]] == [OPEN_POSITION]
    # ---- crash BEFORE commit_cycle(): nothing was promoted ----
    assert RunnerState(cfg.state_dir).data["last_boundary"] != res["boundary"]
    r3 = _runner(cfg, _frame(OPENED), BAR2)
    replay = r3.run_once(defer_commit=True)
    assert replay["status"] == "ok"
    assert [i.intent_id for i in replay["intents"]] == first_ids   # identical ids
    applied = Executor(cfg, r3.state, gateway=None).apply(replay["intents"], today="2026-07-28")
    assert len(applied["applied"]) == 1
    r3.commit_cycle()
    # a further cycle at the same boundary is a no-op — no duplicate execution
    assert _runner(cfg, _frame(OPENED), BAR2).run_once(defer_commit=True)["status"] == "no_new_bar"
    ledger = RunnerState(cfg.state_dir).data["ledger"]
    assert [v["status"] for v in ledger.values()] == [LEDGER_SIMULATED]


def test_commit_cycle_is_noop_without_staged_work(tmp_path):
    cfg = _cfg(tmp_path)
    assert _runner(cfg, _frame(UNFILLED), BAR1).commit_cycle() is None


# ══════════════════════════════════════════════════════════════════════════════
# 5. RESILIENCE
# ══════════════════════════════════════════════════════════════════════════════
def test_ensure_connected_reinitialises_after_terminal_drop(tmp_path):
    sdk = FakeMT5(datetime(2026, 7, 28, 11, 0))
    gw = MT5Gateway(_cfg(tmp_path), sdk=sdk)
    assert gw.connect()[0] and sdk.init_calls == 1
    assert gw.ensure_connected() == (True, "connected")
    assert sdk.init_calls == 1                     # healthy link: no churn
    sdk.connected = False                          # terminal restarted
    ok, detail = gw.ensure_connected()
    assert ok and detail == "reconnected" and sdk.init_calls == 2


def test_heartbeat_marks_running_then_idle(tmp_path):
    ops = OpsLog(tmp_path)
    rec = ops.cycle_start()
    hb = json.loads((tmp_path / "ops" / "heartbeat.json").read_text())
    assert hb["phase"] == "cycle_running" and hb["error"] == ""
    ops.cycle_end(rec, boundary="b1", status="ok")
    hb = json.loads((tmp_path / "ops" / "heartbeat.json").read_text())
    assert hb["phase"] == "idle" and hb["boundary"] == "b1"
