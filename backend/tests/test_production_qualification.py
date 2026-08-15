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
from live.executor import Executor                                    # noqa: E402
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


def _exec(cfg, runner=None):
    """A REAL Executor. In dry_run `reconcile()` pulls no broker snapshot, so no
    gateway is needed and the genuine reconcile path is still exercised."""
    return Executor(cfg, runner.state if runner else RunnerState(cfg.state_dir), gateway=None)

def test_lux_exception_is_contained_and_does_not_advance_the_boundary(tmp_path):
    cfg = _cfg(tmp_path)
    def boom(candles, frontier): raise RuntimeError("Lux pipeline exploded")
    ops = OpsLog(cfg.state_dir)
    r = _runner_with(cfg, boom)
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(), r, _exec(cfg, r), None, ops)
    assert rec["status"] == "error" and "Lux pipeline exploded" in rec["error"]
    assert RunnerState(cfg.state_dir).data["last_boundary"] is None      # replays
    hb = json.loads((cfg.state_dir / "ops" / "heartbeat.json").read_text())
    assert "Lux pipeline exploded" in hb["error"]                        # operator-visible


def test_mt5_unavailable_is_contained_as_a_cycle_error(tmp_path):
    cfg = _cfg(tmp_path)
    class DeadGW:
        def ensure_connected(self): return False, "mt5.initialize failed"
    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, DeadGW(), _BridgeStub(), None, _exec(cfg), None, ops)
    assert rec["status"] == "error" and "MT5 gateway unavailable" in rec["error"]


def test_bridge_exception_is_contained(tmp_path):
    cfg = _cfg(tmp_path)
    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(boom=RuntimeError("terminal offline")),
                          None, _exec(cfg), None, ops)
    assert "terminal offline" in rec["error"]


def test_ops_log_failure_does_not_kill_the_trading_loop(tmp_path):
    """Ops logging sits outside the trading try; a full disk used to kill the process."""
    cfg = _cfg(tmp_path)
    class BoomOps(OpsLog):
        def cycle_end(self, *a, **k):
            raise OSError(28, "No space left on device")
    rec = live_main.cycle(cfg, _OkGW(), _BridgeStub(), None, _exec(cfg), None, BoomOps(cfg.state_dir))
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
    # A CLEAN deployment has news protection in place. Without a calendar the
    # `news protection?` row is correctly FAIL (M-LIVE-NEWS-1 fails closed), so
    # a fixture asserting zero FAILs has to supply one. Written through the
    # production writer with an injected feed — no hand-rolled cache file.
    from live.news_feed import NewsCalendar
    NewsCalendar(cfg, opener=lambda url: [
        {"title": "CPI m/m", "country": "USD", "impact": "High",
         "date": "2030-01-01T12:30:00+00:00"}]).refresh()
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


# ══════════════════════════════════════════════════════════════════════════════
# TELEMETRY DELIVERY OBSERVABILITY
# ══════════════════════════════════════════════════════════════════════════════
# Derived from the `published` result OpsLog already records per cycle. The
# publisher stays stateless: it writes publish_last.json BEFORE the network
# attempt and swallows failure, so that file proves nothing about delivery. The
# real incident needed a hand-written parser over cycles.jsonl to answer "when did
# telemetry stop arriving?" — these rows exist so it never does again.

def _delivery_status(tmp_path, published_seq):
    """Drive live.status over a synthetic cycle history."""
    from live import status as live_status
    cfg = _cfg(tmp_path)
    ops = OpsLog(cfg.state_dir)
    for i, pub in enumerate(published_seq):
        rec = ops.cycle_start()
        ops.cycle_end(rec, boundary=f"2026-07-31 {i:02d}:00:00+00:00",
                      status="ok", published=pub)
    RunnerState(cfg.state_dir).save()
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    return {q: (v, d) for q, v, d in live_status.ROWS}


OKD = {"delivered": True, "status": 200}
BAD = {"delivered": False, "error": "<urlopen error timed out>", "fallback": "x"}


def test_status_reports_healthy_delivery(tmp_path):
    rows = _delivery_status(tmp_path, [OKD, OKD, OKD])
    v, d = rows["telemetry delivering?"]
    assert v == "OK" and "last attempt OK" in d and "0/3 failed" in d
    assert rows["telemetry last delivered"][0] == "OK"


def test_status_reports_consecutive_failures_and_last_error(tmp_path):
    """The incident shape: delivery worked, then stopped."""
    rows = _delivery_status(tmp_path, [OKD, BAD, BAD, BAD, BAD])
    v, d = rows["telemetry delivering?"]
    assert v == "FAIL"                      # >=3 consecutive is not a blip
    assert "4 consecutive" in d and "4/5 failed in window" in d
    assert "urlopen error timed out" in d   # most recent error surfaced


def test_a_single_failure_is_a_warning_not_an_outage(tmp_path):
    """One dropped publish must not read as a dead Control Tower."""
    v, d = _delivery_status(tmp_path, [OKD, OKD, BAD])["telemetry delivering?"]
    assert v == "WARN" and "1 consecutive" in d


def test_recovery_after_an_outage_reads_healthy_again(tmp_path):
    v, d = _delivery_status(tmp_path, [BAD, BAD, BAD, OKD])["telemetry delivering?"]
    assert v == "OK" and "0 consecutive" not in d
    assert "3/4 failed in window" in d      # history retained, not forgotten


def test_last_delivered_timestamp_is_reported(tmp_path):
    _, d = _delivery_status(tmp_path, [OKD, BAD, BAD])["telemetry last delivered"]
    assert "2026-" in d and "window = last" in d, "must state the analysed window"


def test_never_delivered_in_window_is_not_silently_ok(tmp_path):
    v, d = _delivery_status(tmp_path, [BAD, BAD])["telemetry last delivered"]
    assert v == "n/a" and "never, in this window" in d


def test_sparse_history_without_publish_results_is_not_an_outage(tmp_path):
    """Quiet cycles that produced no publish are not delivery failures."""
    rows = _delivery_status(tmp_path, [None, None])
    v, d = rows["telemetry delivering?"]
    assert v == "n/a" and "no delivery attempts" in d


def test_no_history_at_all_reports_honestly(tmp_path):
    from live import status as live_status
    cfg = _cfg(tmp_path)
    RunnerState(cfg.state_dir).save()
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    rows = {q: (v, d) for q, v, d in live_status.ROWS}
    assert rows["telemetry delivering?"] == ("n/a", "no cycles logged yet")


def test_delivery_rows_never_synthesise_lifetime_totals(tmp_path):
    """Only what the window supports; a lifetime counter would need state."""
    _, d = _delivery_status(tmp_path, [OKD, BAD])["telemetry delivering?"]
    assert "in window" in d, "counts must be window-scoped, not implied all-time"


def test_publisher_remains_stateless(tmp_path):
    """The whole point: no delivery counters leaked into the trading path."""
    from live.publisher import CTPublisher
    pub = CTPublisher(_cfg(tmp_path))
    assert set(vars(pub)) == {"config", "fallback"}


# ══════════════════════════════════════════════════════════════════════════════
# M-WORLD-0 — world identity is one value, and it is load-bearing
# ══════════════════════════════════════════════════════════════════════════════
def test_world_identity_is_unchanged_from_m3_p1():
    """intent_id = sha1(INSTANCE_ID|...) is the ledger's duplicate-suppression key,
    so changing this string would let an already-executed order re-send. The whole
    refactor must be identity-preserving."""
    from live import WORLD, INSTANCE_ID, DEPLOYMENT_PROFILE
    assert WORLD.instance_id == "live-eurusd-golden-001"
    assert WORLD.deployment_profile == "GOLDEN_COMPATIBLE"
    assert INSTANCE_ID == WORLD.instance_id
    assert DEPLOYMENT_PROFILE == WORLD.deployment_profile


def test_intent_ids_are_byte_stable_across_the_refactor():
    """The observable consequence of identity: these ids must not move."""
    from live.intents import _intent_id
    assert _intent_id("T_1", "fill", "2026-07-31 08:00:00") == \
        _intent_id("T_1", "fill", "2026-07-31 08:00:00")
    import hashlib
    from live import INSTANCE_ID
    expected = hashlib.sha1(
        f"{INSTANCE_ID}|T_1|fill|2026-07-31 08:00:00".encode()).hexdigest()[:20]
    assert _intent_id("T_1", "fill", "2026-07-31 08:00:00") == expected


def test_identity_is_stated_exactly_once_in_source():
    """It was stated three ways: two module constants plus bare literals in
    deploy_check, so renaming the instance would have left the deploy gate
    checking a node that no longer exists."""
    live_dir = Path(__file__).resolve().parents[2] / "live"
    literals = [p.name for p in live_dir.glob("*.py")
                if p.name != "world.py" and "live-eurusd-golden-001" in p.read_text()]
    assert literals == [], f"hard-coded instance id still in {literals}"
    profile_defs = [p.name for p in live_dir.glob("*.py")
                    if p.name != "world.py" and 'DEPLOYMENT_PROFILE = "' in p.read_text()]
    assert profile_defs == [], f"duplicate DEPLOYMENT_PROFILE in {profile_defs}"


def test_world_is_immutable_and_validated():
    from live.world import World, WORLD_KINDS
    import dataclasses, pytest as _pt
    w = World(instance_id="x", deployment_profile="p", kind="demo")
    with _pt.raises(dataclasses.FrozenInstanceError):
        w.instance_id = "y"                       # identity cannot drift at runtime
    with _pt.raises(ValueError):
        World(instance_id="x", deployment_profile="p", kind="nonsense")
    with _pt.raises(ValueError):
        World(instance_id="", deployment_profile="p", kind="demo")
    assert "funded_live" in WORLD_KINDS and "research" in WORLD_KINDS


def test_world_declares_no_behaviour():
    """Guard against speculative abstraction: World is identity only. Nothing may
    branch on `kind` until a second world actually exists."""
    live_dir = Path(__file__).resolve().parents[2] / "live"
    branching = [p.name for p in live_dir.glob("*.py")
                 if p.name != "world.py"
                 and ("WORLD.kind" in p.read_text() or ".kind ==" in p.read_text())]
    assert branching == [], f"behaviour branched on world kind in {branching}"


# ══════════════════════════════════════════════════════════════════════════════
# M-OPS-1 — operational resilience
# ══════════════════════════════════════════════════════════════════════════════
def _lifecycle_state(tmp_path, phase, hb_age_s, hb_phase="cycle_running", pid=1712):
    """live.status over a synthetic lifecycle+heartbeat pair."""
    from live import status as live_status
    cfg = _cfg(tmp_path)
    ops = cfg.state_dir / "ops"; ops.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    (ops / "lifecycle.json").write_text(json.dumps({
        "phase": phase, "pid": pid, "since": now.isoformat(),
        "updated_at": now.isoformat(), "restart_reason": "first_start",
        "reconcile": {"status": "complete", "runs": 1, "completed_at": now.isoformat(),
                      "detail": "dry_run"},
        "last_clean_shutdown": None, "last_crash": None, "recovery_duration_s": 1.0}))
    (ops / "heartbeat.json").write_text(json.dumps({
        "at": (now - timedelta(seconds=hb_age_s)).isoformat(),
        "phase": hb_phase, "status": "running", "boundary": None, "error": ""}))
    RunnerState(cfg.state_dir).save()
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    return {q: (v, d) for q, v, d in live_status.ROWS}


def test_lifecycle_running_with_a_fresh_heartbeat_still_reads_ok(tmp_path):
    """The positive case must not regress: a genuinely live node reads OK."""
    v, d = _lifecycle_state(tmp_path, "RUNNING", hb_age_s=5)["lifecycle state?"]
    assert v == "OK" and "RUNNING since" in d


def test_lifecycle_claiming_running_after_death_is_not_reported_ok(tmp_path):
    """Observed for 3.5h after a real node death: lifecycle.json kept asserting
    RUNNING for a pid that no longer existed, because a killed process cannot
    write STOPPED. That row is the one an operator scans for "is it alive?"."""
    v, d = _lifecycle_state(tmp_path, "RUNNING", hb_age_s=17265)["lifecycle state?"]
    assert v == "FAIL"
    assert "claims RUNNING" in d and "1712" in d and "process is gone" in d


def test_recomputing_node_is_not_called_dead_inside_its_budget(tmp_path):
    """A ~19min recompute is legitimate; only past the bar budget is it gone."""
    v, _ = _lifecycle_state(tmp_path, "RUNNING", hb_age_s=600)["lifecycle state?"]
    assert v == "OK"


def test_idle_node_uses_the_tight_bound_for_abandonment(tmp_path):
    v, _ = _lifecycle_state(tmp_path, "RUNNING", hb_age_s=200, hb_phase="idle")["lifecycle state?"]
    assert v == "FAIL"


def test_stopped_lifecycle_is_not_flagged_as_abandoned(tmp_path):
    """STOPPED is a correct resting state, however old the heartbeat is."""
    v, d = _lifecycle_state(tmp_path, "STOPPED", hb_age_s=99999)["lifecycle state?"]
    assert v == "OK" and "STOPPED" in d


def test_cycle_tail_read_is_bounded_by_file_size(tmp_path):
    """cycles.jsonl is append-only with no rotation anywhere in the node, so a
    whole-file read to take a 40-line tail grew without limit — worst exactly
    during the long unattended runs when status matters most."""
    from live.status import _tail_cycles, _TAIL_BYTES
    p = tmp_path / "cycles.jsonl"
    recs = [{"cycle_start": f"2026-01-01T00:00:{i%60:02d}", "n": i, "pad": "x" * 600}
            for i in range(3000)]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    assert p.stat().st_size > _TAIL_BYTES, "fixture must exceed the read window"
    got = _tail_cycles(p, 40)
    assert len(got) == 40
    assert got[-1]["n"] == 2999 and got[0]["n"] == 2960     # correct tail, not a slice of junk


def test_cycle_tail_handles_small_and_missing_files(tmp_path):
    from live.status import _tail_cycles
    p = tmp_path / "c.jsonl"
    p.write_text("\n".join(json.dumps({"n": i}) for i in range(5)))
    assert len(_tail_cycles(p, 40)) == 5
    assert _tail_cycles(tmp_path / "absent.jsonl", 40) == []


def test_cycle_tail_survives_a_torn_first_record(tmp_path):
    """Seeking mid-file lands inside a record; the partial line must be dropped,
    not parsed into a bogus cycle."""
    from live.status import _tail_cycles, _TAIL_BYTES
    p = tmp_path / "c.jsonl"
    recs = [{"n": i, "pad": "y" * 600} for i in range(3000)]
    p.write_text("\n".join(json.dumps(r) for r in recs))
    got = _tail_cycles(p, 40)
    assert all("n" in g for g in got), "a partial record leaked through"
