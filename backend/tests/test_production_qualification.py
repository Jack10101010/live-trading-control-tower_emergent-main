"""Production qualification — deterministic reproduction of operational failures.

These are not unit tests of happy paths. Each one injects a realistic fault and
asserts the system's *containment* contract:

  * fail closed (never trade on bad data / bad config),
  * never advance the boundary on a failed cycle (so the work replays),
  * surface an actionable message rather than a raw traceback,
  * keep the trading loop alive for faults that are not about trading.

Every fault here was reproduced against the real code before the assertion was
written; several of them were failing at the time (see PROJECT_STATE).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import main as live_main                                    # noqa: E402
from live.config import LiveConfig                                    # noqa: E402
from live.mt5_bridge import MT5BarBridge, verify_segment_time_base    # noqa: E402
from live.mt5_gateway import MT5Gateway                               # noqa: E402
from live.ops_log import OpsLog                                       # noqa: E402
from live.runner import LiveRunner                                    # noqa: E402
from live.state import RunnerState                                    # noqa: E402


def _cfg(tmp_path, **kw) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    for k, v in kw.items():
        setattr(c, k, v)
    c.ensure_dirs()
    return c


def _epoch(wall: datetime) -> int:
    return int(wall.replace(tzinfo=timezone.utc).timestamp())


def _srv(t: datetime) -> datetime:
    """Broker wall clock for a true-UTC instant (EEST, +3h in July)."""
    return (t + timedelta(hours=3)).replace(tzinfo=None)


class _Tick:
    def __init__(self, e): self.time = e; self.bid = 1.1; self.ask = 1.1


class _TI:
    def __init__(self, c=True): self.connected = c


class FaultySDK:
    """MT5 double with individually injectable faults."""
    TIMEFRAME_M1 = 1

    def __init__(self, tick_wall=None, bars=(), connected=True, init_ok=True,
                 rates_none=False, probe_raises=False):
        self.tick_wall = tick_wall
        self.bars = [{"time": _epoch(w), "open": 1.1, "high": 1.2, "low": 1.0,
                      "close": 1.15, "tick_volume": 1.0} for w in bars]
        self.connected = connected
        self.init_ok = init_ok
        self.rates_none = rates_none
        self.probe_raises = probe_raises

    def initialize(self, **kw): return self.init_ok
    def shutdown(self): pass
    def terminal_info(self):
        if self.probe_raises:
            raise OSError("IPC handle dead")
        return _TI(self.connected)
    def symbol_info_tick(self, s): return _Tick(_epoch(self.tick_wall)) if self.tick_wall else None
    def last_error(self): return (-10003, "IPC initialize failed")
    def copy_rates_range(self, s, tf, a, b): return None if self.rates_none else list(self.bars)


class _OkGW:
    def ensure_connected(self): return True, "connected"


class _BridgeStub:
    def __init__(self, result=None, boom=None): self.result = result or {"appended": 0}; self.boom = boom
    def poll_once(self):
        if self.boom:
            raise self.boom
        return self.result


NOW = datetime.now(timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# BROKER / MT5 FAULTS
# ══════════════════════════════════════════════════════════════════════════════
def test_terminal_crash_is_reported_not_silently_ignored(tmp_path):
    sdk = FaultySDK(tick_wall=_srv(NOW))
    gw = MT5Gateway(_cfg(tmp_path), sdk=sdk)
    assert gw.connect()[0]
    sdk.init_ok = False; sdk.connected = False          # terminal died
    ok, detail = gw.ensure_connected()
    assert not ok and "reconnect failed" in detail


def test_dead_ipc_handle_recovers_when_terminal_returns(tmp_path):
    sdk = FaultySDK(tick_wall=_srv(NOW))
    gw = MT5Gateway(_cfg(tmp_path), sdk=sdk)
    gw.connect()
    sdk.probe_raises = True                              # handle dead, terminal back
    sdk.probe_raises = False; sdk.connected = False
    assert gw.ensure_connected() == (True, "reconnected")


def test_market_data_outage_returns_structured_errors(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path), sdk=FaultySDK(tick_wall=None))
    gw.connect()
    assert gw.server_time_utc() is None
    ok, detail = gw.closed_m1_bars(NOW - timedelta(minutes=5))
    assert not ok and "no server time" in detail
    assert gw.verify_time_base()[0] is False             # fails closed


def test_broker_returns_no_rates(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path), sdk=FaultySDK(tick_wall=_srv(NOW), rates_none=True))
    gw.connect()
    ok, detail = gw.closed_m1_bars(NOW - timedelta(minutes=5))
    assert not ok and "copy_rates_range failed" in detail


def test_empty_broker_history_is_not_an_error(tmp_path):
    cfg = _cfg(tmp_path)
    gw = MT5Gateway(cfg, sdk=FaultySDK(tick_wall=_srv(NOW), bars=()))
    gw.connect()
    assert MT5BarBridge(cfg, gw).poll_once() == {"ok": True, "appended": 0, "last_bar_time": None}


def test_duplicate_and_unordered_broker_bars_are_normalised(tmp_path):
    """Broker replays/reorders must not corrupt the segment or double-append."""
    cfg = _cfg(tmp_path)
    walls = [_srv(NOW - timedelta(minutes=m)) for m in (5, 3, 5, 4)]   # dupe + unordered
    gw = MT5Gateway(cfg, sdk=FaultySDK(tick_wall=_srv(NOW), bars=walls))
    gw.connect()
    bridge = MT5BarBridge(cfg, gw)
    assert bridge.poll_once()["appended"] == 3                      # dupe collapsed
    rows = cfg.live_segment_csv.read_text().splitlines()[1:]
    assert rows == sorted(rows) and len(set(rows)) == len(rows)     # ordered + unique
    assert bridge.poll_once()["appended"] == 0                      # idempotent re-poll


def test_clock_drift_is_diagnosed_separately_from_a_wrong_model(tmp_path):
    """The remedies are opposite, so the message must name the right cause."""
    cfg = _cfg(tmp_path)
    drifted = MT5Gateway(cfg, sdk=FaultySDK(tick_wall=_srv(NOW + timedelta(minutes=17))))
    drifted.connect()
    ok, detail = drifted.verify_time_base()
    assert not ok and "clock looks wrong" in detail                 # NTP, not config
    wrong_model = MT5Gateway(cfg, sdk=FaultySDK(tick_wall=_srv(NOW + timedelta(hours=1))))
    wrong_model.connect()
    ok2, detail2 = wrong_model.verify_time_base()
    assert not ok2 and "clock model" in detail2                     # config, not NTP


def test_startup_while_market_closed_is_permitted(tmp_path):
    """A stale weekend tick must not block startup — only a FUTURE tick does."""
    gw = MT5Gateway(_cfg(tmp_path), sdk=FaultySDK(tick_wall=_srv(NOW - timedelta(days=2))))
    gw.connect()
    ok, detail = gw.verify_time_base()
    assert ok and "unverified" in detail


# ══════════════════════════════════════════════════════════════════════════════
# STATE / FILESYSTEM INTEGRITY
# ══════════════════════════════════════════════════════════════════════════════
def test_corrupt_state_file_fails_closed_with_an_actionable_message(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.state_dir / "runner_state.json").write_text("{ not json")
    with pytest.raises(RuntimeError) as exc:
        RunnerState(cfg.state_dir)
    msg = str(exc.value)
    assert "unreadable" in msg and "Quarantined" in msg              # actionable
    assert (cfg.state_dir / "runner_state.corrupt").exists()         # preserved


def test_truncated_state_file_is_quarantined_not_partially_trusted(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir); st.ledger_set("i1", "simulated"); st.save()
    p = cfg.state_dir / "runner_state.json"
    raw = p.read_text(); p.write_text(raw[: len(raw) // 2])
    with pytest.raises(RuntimeError):
        RunnerState(cfg.state_dir)


def test_missing_state_file_bootstraps_cleanly(tmp_path):
    st = RunnerState(_cfg(tmp_path).state_dir)
    assert st.data["last_boundary"] is None and st.data["ledger"] == {}
    assert st.data["mirror"] == {} and st.data["broker_closed"] == {}


def test_corrupt_provenance_fails_closed(tmp_path):
    cfg = _cfg(tmp_path)
    (cfg.market_data_dir / "provenance.json").write_text("<<<not json")
    ok, detail = verify_segment_time_base(cfg)
    assert not ok and "unreadable" in detail


def test_corrupt_live_segment_gives_an_actionable_error(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.live_segment_csv.write_text("time,open,high,low,close,volume\nGARBAGE,1,1,1,1,1\n")
    bridge = MT5BarBridge(cfg, MT5Gateway(cfg, sdk=FaultySDK(tick_wall=_srv(NOW))))
    with pytest.raises(RuntimeError) as exc:
        bridge.last_stored_time()
    assert "unparseable" in str(exc.value) and "re-backfill" in str(exc.value)


def test_state_writes_are_atomic_under_repeated_saves(tmp_path):
    """tmp+replace: a reader never observes a half-written document."""
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    for i in range(25):
        st.ledger_set(f"i{i}", "simulated")
        st.save()
        assert json.loads((cfg.state_dir / "runner_state.json").read_text())["ledger"]
    assert len(RunnerState(cfg.state_dir).data["ledger"]) == 25


def test_state_survives_restart_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.mirror_set("L_1", 42); st.mark_broker_closed("L_2", 43)
    st.add_realized_r(-1.5, "2026-07-28"); st.ledger_set("i1", "simulated"); st.save()
    back = RunnerState(cfg.state_dir)
    assert back.mirror_ticket("L_1") == 42
    assert back.broker_closed_ticket("L_2") == 43
    assert back.daily_realized_r("2026-07-28") == pytest.approx(-1.5)
    assert back.ledger_status("i1") == "simulated"


def test_legacy_state_without_new_keys_still_loads(tmp_path):
    """Backwards compatibility: a pre-upgrade state file must not crash."""
    cfg = _cfg(tmp_path)
    (cfg.state_dir / "runner_state.json").write_text(json.dumps({
        "last_boundary": "2026-07-17 23:30:00+00:00", "prev_frame_hash": "x",
        "prev_frame_file": None, "ledger": {}, "mirror": {},
        "daily": {"date": None, "realized_r": 0.0}, "updated_at": None}))
    st = RunnerState(cfg.state_dir)
    assert st.broker_closed_ticket("anything") is None       # tolerated, defaulted
    st.mark_broker_closed("L_9", 7); st.save()
    assert RunnerState(cfg.state_dir).broker_closed_ticket("L_9") == 7


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION MISTAKES
# ══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,value,needle", [
    ("fixed_risk_lots", -0.5, "LIVE_FIXED_RISK_LOTS"),
    ("fixed_risk_lots", 0.0, "LIVE_FIXED_RISK_LOTS"),
    ("max_open_positions", -5, "LIVE_MAX_OPEN_POSITIONS"),
    ("daily_loss_limit_r", 0.0, "LIVE_DAILY_LOSS_LIMIT_R"),
    ("magic_number", 0, "LIVE_MT5_MAGIC"),
    ("mode", "sideways", "LIVE_MODE"),
    ("mt5_server_dst_rule", "atlantis", "MT5_SERVER_DST_RULE"),
    ("mt5_server_base_utc_offset_hours", 99, "MT5_SERVER_BASE_UTC_OFFSET_HOURS"),
])
def test_invalid_configuration_fails_closed(tmp_path, field, value, needle):
    """A negative lot size reached order_send as a volume before this check."""
    cfg = _cfg(tmp_path)
    setattr(cfg, field, value)
    with pytest.raises(ValueError) as exc:
        cfg.validate()
    assert needle in str(exc.value)


def test_valid_configuration_passes_validation(tmp_path):
    _cfg(tmp_path).validate()


def test_invalid_dst_rule_rejected_at_gateway_construction(tmp_path):
    with pytest.raises(ValueError):
        MT5Gateway(_cfg(tmp_path, mt5_server_dst_rule="atlantis"))


# ══════════════════════════════════════════════════════════════════════════════
# RUNTIME LOOP CONTAINMENT
# ══════════════════════════════════════════════════════════════════════════════
def _runner_with(cfg, pipeline):
    return LiveRunner(cfg, session=None, pipeline=pipeline,
                      candles_provider=lambda: pd.DataFrame({"time": ["2026-07-28 08:14:00"]}))


def test_lux_exception_is_contained_and_does_not_advance_the_boundary(tmp_path):
    cfg = _cfg(tmp_path)
    def boom(candles, frontier): raise RuntimeError("Lux pipeline exploded")
    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(), _runner_with(cfg, boom), None, None, ops)
    assert rec["status"] == "error" and "Lux pipeline exploded" in rec["error"]
    assert RunnerState(cfg.state_dir).data["last_boundary"] is None      # replays
    hb = json.loads((cfg.state_dir / "ops" / "heartbeat.json").read_text())
    assert "Lux pipeline exploded" in hb["error"]                        # operator-visible


def test_mt5_unavailable_is_contained_as_a_cycle_error(tmp_path):
    cfg = _cfg(tmp_path)
    class DeadGW:
        def ensure_connected(self): return False, "mt5.initialize failed"
    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, DeadGW(), _BridgeStub(), None, None, None, ops)
    assert rec["status"] == "error" and "MT5 gateway unavailable" in rec["error"]


def test_bridge_exception_is_contained(tmp_path):
    cfg = _cfg(tmp_path)
    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(boom=RuntimeError("terminal offline")),
                          None, None, None, ops)
    assert "terminal offline" in rec["error"]


def test_ops_log_failure_does_not_kill_the_trading_loop(tmp_path):
    """Ops logging sits outside the trading try; a full disk used to kill the process."""
    cfg = _cfg(tmp_path)
    class BoomOps(OpsLog):
        def cycle_end(self, *a, **k):
            raise OSError(28, "No space left on device")
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(), None, None, None, BoomOps(cfg.state_dir))
    assert "ops_log_failed" in rec["error"]          # recorded, not raised


def test_failed_commit_leaves_the_boundary_unadvanced(tmp_path):
    """Disk full at commit must replay the cycle, never mark it processed."""
    cfg = _cfg(tmp_path)
    frame = pd.DataFrame([{"trade_id": "L1", "direction": "bullish", "fill_time": "",
                           "outcome": "UNFILLED"}]).astype(str)
    r = _runner_with(cfg, lambda a, b: frame)
    r.run_once(defer_commit=True)
    def boom(): raise OSError(28, "No space left on device")
    r.state.save = boom
    with pytest.raises(OSError):
        r.commit_cycle()
    assert RunnerState(cfg.state_dir).data["last_boundary"] is None


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS
# ══════════════════════════════════════════════════════════════════════════════
def test_status_reports_healthy_on_a_clean_idle_deployment(tmp_path):
    from live import status as live_status
    cfg = _cfg(tmp_path)
    # A clean deployment has a real, verified engine tree. The other fixtures use
    # a synthetic lux_root, which the engine-identity row correctly reports as
    # FAIL — so this test, which asserts zero FAILs, must supply the pinned tree.
    real_lux = Path(__file__).resolve().parents[2].parent / "Lux-OB-Backtester"
    if not real_lux.exists():
        pytest.skip("pinned Lux tree not present")
    cfg.lux_root = real_lux
    ops = OpsLog(cfg.state_dir)
    ops.cycle_end(ops.cycle_start(), boundary="2026-07-28 08:15:00+00:00", status="no_new_bar")
    RunnerState(cfg.state_dir).save()
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    answered = {q for q, _, _ in live_status.ROWS}
    for q in ("bot healthy?", "heartbeat current?", "system stalled?", "time base valid?",
              "reconciliation healthy?", "ledger healthy?", "replay occurring?",
              "engine progressing?", "MT5 healthy?", "engine identity intact?"):
        assert q in answered, f"status does not answer {q!r}"
    assert not [r for r in live_status.ROWS if r[1] == "FAIL"]


def test_status_flags_a_tampered_engine_tree(tmp_path):
    """The governance gate must be visible to the operator, not just at startup."""
    from live import status as live_status
    cfg = _cfg(tmp_path)          # synthetic lux_root == engine tree cannot be certified
    RunnerState(cfg.state_dir).save()
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    identity = [r for r in live_status.ROWS if r[0] == "engine identity intact?"]
    assert identity and identity[0][1] == "FAIL"


def test_status_flags_sent_orders_in_dry_run(tmp_path):
    """The single most important dry-run invariant must be surfaced as FAIL."""
    from live import status as live_status
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir); st.ledger_set("i1", "sent"); st.save()
    ops = OpsLog(cfg.state_dir)
    ops.cycle_end(ops.cycle_start(), boundary="b", status="ok")
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    ledger_row = [r for r in live_status.ROWS if r[0] == "ledger healthy?"][0]
    assert ledger_row[1] == "FAIL" and "MUST be 0" in ledger_row[2]


def test_status_flags_a_stalled_recompute(tmp_path):
    from live import status as live_status
    cfg = _cfg(tmp_path)
    (cfg.state_dir / "ops").mkdir(parents=True, exist_ok=True)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=2000)).isoformat()
    (cfg.state_dir / "ops" / "heartbeat.json").write_text(json.dumps(
        {"at": stale, "phase": "cycle_running", "status": "running", "error": ""}))
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    stalled = [r for r in live_status.ROWS if r[0] == "system stalled?"][0]
    assert stalled[1] == "FAIL"


def test_startup_path_builds_without_attribute_errors(tmp_path, monkeypatch):
    """Regression guard for the P0 that no test caught: main() referenced a
    config field that had been renamed, so the service crashed at startup with
    an AttributeError. The startup path had zero coverage."""
    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MARKET_DATA_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("LIVE_KILL_FILE", str(tmp_path / "state" / "KILL"))
    monkeypatch.setenv("LUX_ROOT", str(tmp_path / "lux"))
    cfg = LiveConfig()
    cfg.validate()
    # every attribute main() interpolates into its startup banner must exist
    for attr in ("mode", "mt5_server_base_utc_offset_hours", "mt5_server_dst_rule",
                 "state_dir", "market_data_dir", "kill_file", "lux_root", "broker_symbol"):
        assert hasattr(cfg, attr), f"main() startup banner needs cfg.{attr}"
    banner = (f"server clock (base{cfg.mt5_server_base_utc_offset_hours:+d}h "
              f"dst={cfg.mt5_server_dst_rule})")
    assert "base+2h" in banner


def test_build_validates_config_before_touching_the_broker(tmp_path, monkeypatch):
    """validate() was defined but never called — an inert fix. Pin the wiring:
    build() must validate BEFORE constructing the gateway or the Lux session."""
    calls = []

    def fake_validate(self):
        calls.append("validated")
        raise ValueError("invalid live configuration: LIVE_MAX_OPEN_POSITIONS must be >= 1")

    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MARKET_DATA_DIR", str(tmp_path / "md"))
    monkeypatch.setenv("LIVE_KILL_FILE", str(tmp_path / "state" / "KILL"))
    monkeypatch.setattr(LiveConfig, "validate", fake_validate)
    with pytest.raises(ValueError) as exc:
        live_main.build()
    assert calls == ["validated"]                       # called, and called first
    assert "LIVE_MAX_OPEN_POSITIONS" in str(exc.value)
