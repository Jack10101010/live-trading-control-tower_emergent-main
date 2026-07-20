"""M3 Phase 1 — live vertical slice tests (fakes only; no MT5, no network,
no full Golden pipeline). Heavy Golden-fidelity is proven by the rehearsal
gate (design P0), not unit tests.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.config import LiveConfig                       # noqa: E402
from live.executor import Executor                        # noqa: E402
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,          # noqa: E402
                          SKIP_INTRA_WINDOW, diff_frontier)
from live.mt5_bridge import MT5BarBridge                  # noqa: E402
from live.mt5_gateway import MT5Gateway                   # noqa: E402
from live.publisher import CTPublisher                    # noqa: E402
from live.runner import LiveRunner, assemble_candles, latest_closed_boundary   # noqa: E402
from live.state import LEDGER_SIMULATED, RunnerState      # noqa: E402


def _cfg(tmp_path, **kw) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    for k, v in kw.items():
        object.__setattr__(c, k, v) if hasattr(c, "__frozen__") else setattr(c, k, v)
    c.ensure_dirs()
    return c


def _frame(rows):
    cols = ["trade_id", "direction", "fill_time", "outcome", "entry", "stop", "tp"]
    return pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows]).astype(str)


# ── intents ─────────────────────────────────────────────────────────────────────
def test_diff_deterministic_and_idempotent_ids():
    prev = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "2026-07-17 09:47:00+00:00",
                   "outcome": "OPEN", "entry": "1.1", "stop": "1.09", "tp": "1.12"}])
    a = diff_frontier(prev, cur, "2026-07-17 09:45:00+00:00")
    b = diff_frontier(prev, cur, "2026-07-17 09:45:00+00:00")
    assert [i.to_dict() for i in a] == [i.to_dict() for i in b]      # deterministic
    assert len(a) == 1 and a[0].action == OPEN_POSITION
    assert a[0].intent_id == b[0].intent_id                          # idempotent identity
    assert a[0].entry == 1.1 and a[0].stop == 1.09 and a[0].target == 1.12


def test_diff_transitions_exit_stopmove_and_intra_window():
    prev = _frame([
        {"trade_id": "L_1", "direction": "bullish", "fill_time": "t1", "outcome": "OPEN",
         "entry": "1.1", "stop": "1.09", "tp": "1.12"},
        {"trade_id": "S_2", "direction": "bearish", "fill_time": "t1", "outcome": "OPEN",
         "entry": "1.2", "stop": "1.21", "tp": "1.18"},
    ])
    cur = _frame([
        {"trade_id": "L_1", "direction": "bullish", "fill_time": "t1", "outcome": "WIN",
         "entry": "1.1", "stop": "1.09", "tp": "1.12"},                      # exit
        {"trade_id": "S_2", "direction": "bearish", "fill_time": "t1", "outcome": "OPEN",
         "entry": "1.2", "stop": "1.2", "tp": "1.18"},                       # stop moved (BE)
        {"trade_id": "S_3", "direction": "bearish", "fill_time": "t2", "outcome": "LOSS"},  # intra-window
    ])
    intents = diff_frontier(prev, cur, "B")
    actions = {i.trade_id: i.action for i in intents}
    assert actions == {"L_1": CLOSE_POSITION, "S_2": MODIFY_STOP, "S_3": SKIP_INTRA_WINDOW}
    assert [i.trade_id for i in intents] == ["L_1", "S_2", "S_3"]    # engine row order preserved


def test_diff_no_changes_produces_no_intents():
    prev = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t1",
                    "outcome": "OPEN", "stop": "1.09"}])
    assert diff_frontier(prev, prev.copy(), "B") == []


# ── boundary / seam ─────────────────────────────────────────────────────────────
def test_latest_closed_boundary():
    t = pd.Timestamp
    assert latest_closed_boundary(t("2026-07-17 10:14:00")) == t("2026-07-17 10:00:00")
    assert latest_closed_boundary(t("2026-07-17 10:15:00")) == t("2026-07-17 10:00:00")  # 10:15 bar still open
    assert latest_closed_boundary(t("2026-07-17 10:29:00")) == t("2026-07-17 10:15:00")
    assert latest_closed_boundary(t("2026-07-17 10:06:00")) == t("2026-07-17 09:45:00")


def test_assemble_candles_seam_frozen_wins(tmp_path):
    frozen = tmp_path / "frozen.csv"
    live = tmp_path / "live.csv"
    frozen.write_text("time,open,high,low,close,volume\n"
                      "2026-06-19 09:52:00+00:00,1,1,1,1,0\n"
                      "2026-06-19 09:53:00+00:00,1,1,1,1,0\n")
    live.write_text("time,open,high,low,close,volume\n"
                    "2026-06-19 09:53:00+00:00,9,9,9,9,0\n"      # overlaps frozen -> dropped
                    "2026-06-19 09:54:00+00:00,2,2,2,2,0\n")
    out = assemble_candles(frozen, live)
    assert len(out) == 3
    assert float(out.iloc[1]["open"]) == 1.0                      # frozen row won the overlap
    assert out.iloc[-1]["time"] == "2026-06-19 09:54:00+00:00"


# ── bridge ──────────────────────────────────────────────────────────────────────
class FakeSDKBars:
    """Minimal fake for closed_m1_bars via gateway injection."""
    def __init__(self, bars):
        self.bars = bars
    def closed_m1_bars(self, since, limit=5000):
        return True, self.bars


def test_bridge_appends_dedupes_and_heartbeats(tmp_path):
    cfg = _cfg(tmp_path)
    bars = [
        {"time": "2026-06-19 10:00:00+00:00", "open": 1, "high": 1, "low": 1, "close": 1, "volume": 1},
        {"time": "2026-06-19 10:00:00+00:00", "open": 9, "high": 9, "low": 9, "close": 9, "volume": 9},  # dup
        {"time": "2026-06-19 10:01:00+00:00", "open": 2, "high": 2, "low": 2, "close": 2, "volume": 2},
    ]
    bridge = MT5BarBridge(cfg, FakeSDKBars(bars))
    r1 = bridge.poll_once()
    assert r1["ok"] and r1["appended"] == 2                        # dup dropped, first wins
    r2 = bridge.poll_once()
    assert r2["appended"] == 0                                     # idempotent re-poll
    hb = json.loads((cfg.market_data_dir / "heartbeat.json").read_text())
    assert hb["last_bar_time"] == "2026-06-19 10:01:00+00:00"
    prov = json.loads((cfg.market_data_dir / "provenance.json").read_text())
    assert "dukascopy" in prov["data_seam"].lower()


# ── state ───────────────────────────────────────────────────────────────────────
def test_state_atomic_roundtrip_and_daily_loss(tmp_path):
    s = RunnerState(tmp_path)
    s.ledger_set("i1", "sent")
    s.mirror_set("L_1", 42)
    s.add_realized_r(-2.0, "2026-07-17")
    s.save()
    s2 = RunnerState(tmp_path)                                     # crash-safe reload
    assert s2.ledger_status("i1") == "sent"
    assert s2.mirror_ticket("L_1") == 42
    assert s2.daily_realized_r("2026-07-17") == -2.0
    assert s2.daily_realized_r("2026-07-18") == 0.0                # day rollover


# ── safety rails + executor (dry_run) ───────────────────────────────────────────
def _intent(action=OPEN_POSITION, tid="L_1", iid="abc123"):
    from live.intents import OrderIntent
    return OrderIntent(intent_id=iid, action=action, trade_id=tid, side="long",
                       frontier_bar="B", entry=1.1, stop=1.09, target=1.12)


def test_executor_dry_run_applies_and_dedupes(tmp_path):
    cfg = _cfg(tmp_path)
    runner_state = RunnerState(cfg.state_dir)
    ex = Executor(cfg, runner_state, gateway=None if False else MT5Gateway(cfg, sdk=None))
    r1 = ex.apply([_intent()], today="2026-07-17")
    assert not r1["frozen"] and len(r1["applied"]) == 1
    assert runner_state.ledger_status("abc123") == LEDGER_SIMULATED
    assert runner_state.mirror_ticket("L_1") == -1                 # simulated mirror
    r2 = ex.apply([_intent()], today="2026-07-17")                 # duplicate replay
    assert len(r2["applied"]) == 0 and r2["blocked"][0]["rail"] == "duplicate_intent"


def test_kill_switch_blocks_opens_but_allows_closes(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.mirror_set("L_2", -1)
    cfg.kill_file.write_text("STOP")
    ex = Executor(cfg, st, MT5Gateway(cfg, sdk=None))
    res = ex.apply([_intent(iid="k1"), _intent(action=CLOSE_POSITION, tid="L_2", iid="k2")],
                   today="2026-07-17")
    rails = {b["intent_id"]: b["rail"] for b in res["blocked"]}
    assert rails.get("k1") == "kill_switch"
    assert len(res["applied"]) == 1                                # the close went through


def test_daily_loss_and_position_cap_rails(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.add_realized_r(-cfg.daily_loss_limit_r, "2026-07-17")
    ex = Executor(cfg, st, MT5Gateway(cfg, sdk=None))
    res = ex.apply([_intent(iid="d1")], today="2026-07-17")
    assert res["blocked"][0]["rail"] == "daily_loss_limit"

    st2 = RunnerState(cfg.state_dir / "s2")
    for i in range(cfg.max_open_positions):
        st2.mirror_set(f"T{i}", i + 1)
    ex2 = Executor(cfg, st2, MT5Gateway(cfg, sdk=None))
    res2 = ex2.apply([_intent(iid="c1", tid="T_new")], today="2026-07-17")
    assert res2["blocked"][0]["rail"] == "max_open_positions"


def test_modify_without_mirror_blocked(tmp_path):
    cfg = _cfg(tmp_path)
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    res = ex.apply([_intent(action=MODIFY_STOP, tid="GHOST", iid="m1")], today="2026-07-17")
    assert res["blocked"][0]["rail"] == "unknown_position"


# ── runner with injected pipeline (no Golden run) ───────────────────────────────
def test_runner_bootstrap_then_diff_then_noop(tmp_path):
    cfg = _cfg(tmp_path)
    lux_candles = cfg.lux_root / "data" / "candles"
    lux_candles.mkdir(parents=True)
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=75, freq="1min")
    pd.DataFrame({"time": times.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(lux_candles / "EURUSD_1m_extended_2015_2026.csv", index=False)

    frames = [
        _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}]),
        _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t",
                 "outcome": "OPEN", "entry": "1.1", "stop": "1.09", "tp": "1.12"}]),
    ]
    calls = {"n": 0}

    def fake_pipeline(candles, frontier_date):
        frame = frames[min(calls["n"], 1)]
        calls["n"] += 1
        return frame

    runner = LiveRunner(cfg, session=None, pipeline=fake_pipeline)
    r1 = runner.run_once()
    assert r1["status"] == "bootstrap" and r1["intents"] == []     # baseline frame only

    # new 15m bar arrives -> boundary advances -> diff emits the fill
    more = pd.date_range("2026-07-17 10:15:00+00:00", periods=16, freq="1min")
    pd.DataFrame({"time": more.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(cfg.live_segment_csv, index=False)
    r2 = runner.run_once()
    assert r2["status"] == "ok" and len(r2["intents"]) == 1
    assert r2["intents"][0].action == OPEN_POSITION

    r3 = runner.run_once()                                         # same boundary -> no-op
    assert r3["status"] == "no_new_bar"


# ── P0 rehearsal edge cases ─────────────────────────────────────────────────────
def test_dry_run_executor_never_touches_order_ops(tmp_path):
    from live.rehearsal import CountingGateway
    cfg = _cfg(tmp_path)
    gw = CountingGateway(cfg)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gw)
    res = ex.apply([_intent(iid="z1"), _intent(action=MODIFY_STOP, tid="L_1", iid="z2")],
                   today="2026-07-17")
    assert gw.order_ops == 0                                   # dry_run: zero broker calls
    assert len(res["applied"]) >= 1


def test_fresh_runner_instance_resumes_without_new_intents(tmp_path):
    cfg = _cfg(tmp_path)
    lux_candles = cfg.lux_root / "data" / "candles"
    lux_candles.mkdir(parents=True)
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=40, freq="1min")
    pd.DataFrame({"time": times.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(lux_candles / "EURUSD_1m_extended_2015_2026.csv", index=False)
    frame = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    r1 = LiveRunner(cfg, session=None, pipeline=lambda c, d: frame)
    assert r1.run_once()["status"] == "bootstrap"
    # crash + restart: a brand-new instance over the same state dir
    r2 = LiveRunner(cfg, session=None, pipeline=lambda c, d: frame)
    out = r2.run_once()
    assert out["status"] == "no_new_bar" and not out.get("intents")


def test_first_divergence_locates_field_and_ordering(tmp_path):
    from live.rehearsal import first_divergence
    ref, cand = tmp_path / "ref.csv", tmp_path / "cand.csv"
    ref.write_text("trade_id,tp\nL_1,1.5\nL_2,2.5\n")
    cand.write_text("trade_id,tp\nL_1,1.5\nL_2,2.6\n")
    d = first_divergence(ref, cand, "trade_id")
    assert d["kind"] == "value" and d["key"] == "L_2" and d["field"] == "tp"
    cand.write_text("trade_id,tp\nL_2,2.5\nL_1,1.5\n")
    assert first_divergence(ref, cand, "trade_id")["kind"] == "ordering"
    cand.write_text("trade_id,tp\nL_1,1.5\n")
    assert first_divergence(ref, cand, "trade_id")["kind"] in ("population", "ordering")


def test_skip_intra_window_is_deterministic_and_never_applied(tmp_path):
    prev = _frame([{"trade_id": "S_9", "direction": "bearish", "fill_time": "", "outcome": "UNFILLED"}])
    cur = _frame([{"trade_id": "S_9", "direction": "bearish", "fill_time": "t", "outcome": "LOSS"}])
    a = diff_frontier(prev, cur, "B")
    b = diff_frontier(prev, cur, "B")
    assert [i.to_dict() for i in a] == [i.to_dict() for i in b]
    assert a[0].action == SKIP_INTRA_WINDOW
    cfg = _cfg(tmp_path)
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    res = ex.apply(a, today="2026-07-17")
    assert res["applied"] == [] and len(res["skipped"]) == 1   # recorded, never sent


# ── publisher ───────────────────────────────────────────────────────────────────
def test_publisher_payload_and_fallback(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.ct_base_url = "http://127.0.0.1:1/api"                     # unreachable by design
    pub = CTPublisher(cfg)
    payload = pub.build_payload({"status": "ok", "boundary": "B", "intents": [_intent()],
                                 "trades_rows": 3}, {"frozen": False, "reconcile": {"findings": []}},
                                engine_version="5bb6372c", mode="dry_run")
    assert payload["instance_id"] and payload["deployment_profile"] == "GOLDEN_COMPATIBLE"
    assert payload["intents"][0]["action"] == OPEN_POSITION
    result = pub.publish(payload, timeout=0.5)
    assert result["delivered"] is False                            # network down ≠ crash
    assert json.loads(Path(result["fallback"]).read_text())["symbol"] == "EURUSD"


# ── P1 shadow: ops log + shadow report ──────────────────────────────────────────
def _cycle_line(start, end, boundary, status="ok", duration=240.0, findings=None,
                error="", frozen=False):
    return {"cycle_start": start, "cycle_end": end, "duration_s": duration,
            "boundary": boundary, "status": status, "intents": 0, "applied": 0,
            "blocked": 0, "skipped": 0, "reconcile_findings": findings or [],
            "frozen": frozen, "error": error, "bars_appended": 1}


def test_ops_log_roundtrip_and_heartbeat(tmp_path):
    from live.ops_log import OpsLog
    ops = OpsLog(tmp_path)
    rec = ops.cycle_start()
    out = ops.cycle_end(rec, boundary="B1", status="ok", intents=2)
    assert out["duration_s"] >= 0 and out["intents"] == 2
    assert ops.read_cycles()[0]["boundary"] == "B1"
    hb = json.loads((tmp_path / "ops" / "heartbeat.json").read_text())
    assert hb["boundary"] == "B1" and hb["status"] == "ok"


def _gate(report, name):
    return next(g for g in report["gates"] if g["gate"] == name)


def test_shadow_report_gates_pass_and_min_bars_configurable(tmp_path):
    import pandas as pd
    from live.ops_log import OpsLog
    from live.shadow_report import build_report, gates_from_env
    ops = OpsLog(tmp_path)
    t0 = pd.Timestamp("2026-07-13 00:00:00+00:00")  # Monday
    with ops.cycles_path.open("w") as fh:
        for i in range(120):
            b = t0 + pd.Timedelta(minutes=15 * i)
            s = (b + pd.Timedelta(minutes=15)).isoformat()
            fh.write(json.dumps(_cycle_line(s, s, str(b))) + "\n")
    # evidence gate, not calendar: 120 clean bars pass with min_bars=100
    r = build_report(tmp_path, {"min_observed_bars": 100, "max_cycle_time_s": 600.0})
    assert r["verdict"] == "ALL_GATES_PASS"
    assert r["processed_bars"] == 120 and not r["missed_bars"]
    assert all(g["result"] == "PASS" for g in r["gates"])
    assert r["recommendation"].startswith("PROMOTE")
    assert "days" not in r["recommendation"]                     # no calendar language
    # same data fails only the min_observed_bars gate at a higher threshold
    r2 = build_report(tmp_path, {"min_observed_bars": 480, "max_cycle_time_s": 600.0})
    assert r2["verdict"] == "GATES_FAILED"
    assert _gate(r2, "min_observed_bars")["result"] == "FAIL"
    assert [g["gate"] for g in r2["gates"] if g["result"] == "FAIL"] == ["min_observed_bars"]
    # env-driven configuration
    os.environ["SHADOW_MIN_OBSERVED_BARS"] = "77"
    os.environ["SHADOW_MAX_CYCLE_TIME_S"] = "333"
    try:
        cfg = gates_from_env()
        assert cfg == {"min_observed_bars": 77, "max_cycle_time_s": 333.0}
        assert gates_from_env(min_bars=5)["min_observed_bars"] == 5   # CLI beats env
    finally:
        del os.environ["SHADOW_MIN_OBSERVED_BARS"], os.environ["SHADOW_MAX_CYCLE_TIME_S"]


def test_shadow_report_cycle_time_and_overrun_gates(tmp_path):
    from live.ops_log import OpsLog
    from live.shadow_report import build_report
    ops = OpsLog(tmp_path)
    with ops.cycles_path.open("w") as fh:
        fh.write(json.dumps(_cycle_line("2026-07-14 10:15:00+00:00", "2026-07-14 10:15:00+00:00",
                                        "2026-07-14 10:00:00+00:00", duration=950.0)) + "\n")
    r = build_report(tmp_path, {"min_observed_bars": 1, "max_cycle_time_s": 600.0})
    assert _gate(r, "max_cycle_time")["result"] == "FAIL"
    assert _gate(r, "no_boundary_overrun")["result"] == "FAIL"   # 950s > 900s bar interval
    assert r["verdict"] == "GATES_FAILED"


def test_shadow_report_flags_missed_bars_but_not_weekends(tmp_path):
    import pandas as pd
    from live.ops_log import OpsLog
    from live.shadow_report import build_report, classify_gap
    # weekend gap: Fri 19:45 -> Sun 22:00 == market_closed
    assert classify_gap(pd.Timestamp("2026-07-10 20:00:00+00:00").to_pydatetime(),
                        pd.Timestamp("2026-07-12 22:00:00+00:00").to_pydatetime()) == "market_closed"
    # mid-week hole == missed
    assert classify_gap(pd.Timestamp("2026-07-14 10:15:00+00:00").to_pydatetime(),
                        pd.Timestamp("2026-07-14 11:00:00+00:00").to_pydatetime()) == "missed"
    ops = OpsLog(tmp_path)
    rows = ["2026-07-14 10:00:00+00:00", "2026-07-14 10:15:00+00:00",
            "2026-07-14 11:15:00+00:00"]          # 3 windows missing between 10:15 and 11:15
    with ops.cycles_path.open("w") as fh:
        for b in rows:
            fh.write(json.dumps(_cycle_line(b, b, b)) + "\n")
    r = build_report(tmp_path, {"min_observed_bars": 1, "max_cycle_time_s": 600.0})
    assert len(r["missed_bars"]) == 1 and r["missed_bars"][0]["windows"] == 3
    assert _gate(r, "missed_bars")["result"] == "FAIL"
    assert r["verdict"] == "GATES_FAILED" and r["recommendation"].startswith("remain P1")


def test_shadow_report_flags_reconcile_and_errors(tmp_path):
    from live.ops_log import OpsLog
    from live.shadow_report import build_report
    ops = OpsLog(tmp_path)
    with ops.cycles_path.open("w") as fh:
        fh.write(json.dumps(_cycle_line("2026-07-14 10:15:00+00:00", "2026-07-14 10:15:00+00:00",
                                        "2026-07-14 10:00:00+00:00",
                                        findings=[{"severity": "critical", "code": "unknown_position"}],
                                        error="RuntimeError: boom")) + "\n")
    r = build_report(tmp_path, {"min_observed_bars": 1, "max_cycle_time_s": 600.0})
    assert r["reconciliation"]["critical"] == 1
    assert r["errors"]["count"] == 1
    assert _gate(r, "critical_reconciliation")["result"] == "FAIL"
    assert _gate(r, "runner_errors")["result"] == "FAIL"
    assert r["verdict"] == "GATES_FAILED"


def test_main_cycle_logs_and_survives_component_failure(tmp_path):
    from live import main as live_main
    from live.ops_log import OpsLog
    cfg = _cfg(tmp_path)

    class BoomBridge:
        def poll_once(self):
            raise RuntimeError("terminal offline")

    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, None, BoomBridge(), None, None, None, ops)
    assert "terminal offline" in rec["error"]                 # logged, not raised
    assert ops.read_cycles()[0]["status"] == "error"
    hb = json.loads((cfg.state_dir / "ops" / "heartbeat.json").read_text())
    assert "terminal offline" in hb["error"]


# ── gateway degradation + CT adapter ───────────────────────────────────────────
def test_gateway_degrades_without_sdk(tmp_path):
    g = MT5Gateway(_cfg(tmp_path), sdk=None)
    assert g.available is False
    ok, detail = g.connect()
    assert ok is False and "not available" in detail


def test_ct_mt5_adapter_reports_disconnected_off_vps():
    backend = REPO_ROOT / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    import broker as broker_layer
    adapter = broker_layer.MT5Adapter()
    h = adapter.health()
    assert h.connection == broker_layer.ConnectionState.DISCONNECTED
    assert adapter.positions(None) == [] and adapter.orders(None) == []
    assert broker_layer.active_kind() == "mock"                    # CT default untouched


# ── Phase 1: LR-1 durable intent transaction ────────────────────────────────────
from live.state import LEDGER_SENT                                  # noqa: E402
from live.intents import OrderIntent                                # noqa: E402


def _lr1_runner(tmp_path):
    """Runner with an injected pipeline: bootstrap frame, then an OPEN fill frame."""
    cfg = _cfg(tmp_path)
    lux_candles = cfg.lux_root / "data" / "candles"
    lux_candles.mkdir(parents=True)
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=75, freq="1min")
    pd.DataFrame({"time": times.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(lux_candles / "EURUSD_1m_extended_2015_2026.csv", index=False)
    frames = [
        _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}]),
        _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t",
                 "outcome": "OPEN", "entry": "1.1", "stop": "1.09", "tp": "1.12"}]),
    ]
    calls = {"n": 0}

    def fake_pipeline(candles, frontier_date):
        frame = frames[min(calls["n"], 1)]
        calls["n"] += 1
        return frame

    runner = LiveRunner(cfg, session=None, pipeline=fake_pipeline)
    assert runner.run_once()["status"] == "bootstrap"
    more = pd.date_range("2026-07-17 10:15:00+00:00", periods=16, freq="1min")
    pd.DataFrame({"time": more.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(cfg.live_segment_csv, index=False)
    r2 = runner.run_once()          # commits boundary + reserves PENDING intent, NO executor.apply
    assert r2["status"] == "ok" and len(r2["intents"]) == 1
    return cfg, r2["intents"][0].intent_id, fake_pipeline


# A. LR-1 core crash: intent survives a crash before executor.apply, applied exactly once.
def test_lr1_crash_before_apply_recovers_intent_exactly_once(tmp_path):
    cfg, iid, _ = _lr1_runner(tmp_path)
    # crash simulated: process dies before executor.apply. State is durable.
    st = RunnerState(cfg.state_dir)
    assert st.ledger_status(iid) == "pending"          # intent durably reserved
    assert st.data["last_boundary"] is not None         # boundary durable too
    # restart: fresh state + executor, drain
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d = ex.drain_pending(today="2026-07-17")
    assert d["drained"] == [iid] and len(d["applied"]) == 1
    assert RunnerState(cfg.state_dir).ledger_status(iid) == LEDGER_SIMULATED   # applied once
    # repeated drain is a no-op (idempotent)
    ex2 = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d2 = ex2.drain_pending(today="2026-07-17")
    assert d2["drained"] == [] and d2["applied"] == []


# B. Atomic boundary invariant: no committed boundary without its generated intents.
def test_lr1_boundary_and_intents_committed_together(tmp_path):
    cfg, iid, _ = _lr1_runner(tmp_path)
    st = RunnerState(cfg.state_dir)                      # reloaded from disk
    assert st.data["last_boundary"] is not None
    assert iid in dict(st.pending_intents())            # intent durable alongside the boundary


# C. No-new-bar restart still recovers the intent independently of the gate.
def test_lr1_restart_no_new_bar_but_drain_recovers(tmp_path):
    cfg, iid, pipeline = _lr1_runner(tmp_path)
    runner2 = LiveRunner(cfg, session=None, pipeline=pipeline)
    assert runner2.run_once()["status"] == "no_new_bar"     # boundary already committed
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d = ex.drain_pending(today="2026-07-17")
    assert len(d["applied"]) == 1 and d["drained"] == [iid]


# D. Multiple pending intents: deterministic order, each once, repeat no-op.
def test_lr1_multiple_pending_deterministic_once(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    ids = []
    for k in range(3):
        it = OrderIntent(intent_id=f"p{k}", action=OPEN_POSITION, trade_id=f"T{k}",
                         side="long", frontier_bar="B", entry=1.1, stop=1.09, target=1.12)
        st.reserve_pending(it)
        ids.append(it.intent_id)
    st.save()
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d = ex.drain_pending(today="2026-07-17")
    assert d["drained"] == ids                          # stable insertion order
    assert len(d["applied"]) == 3
    ex2 = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d2 = ex2.drain_pending(today="2026-07-17")
    assert d2["drained"] == [] and d2["applied"] == []


# E. Malformed persisted payload: not silently ignored -> freeze with diagnostic.
def test_lr1_malformed_pending_freezes_with_diagnostic(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.ledger_set("bad1", "pending", {"intent": {"action": "OPEN_POSITION"}})  # missing fields
    st.save()
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d = ex.drain_pending(today="2026-07-17")
    assert d["frozen"] is True and d["applied"] == []
    assert any("bad1" in f["detail"] for f in d["reconcile"]["findings"])   # id surfaced
    assert RunnerState(cfg.state_dir).ledger_status("bad1") == "frozen"     # not dropped


# F. Existing terminal records are never resubmitted.
def test_lr1_drain_skips_terminal_records(tmp_path):
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.ledger_set("t1", LEDGER_SIMULATED, {"mode": "dry_run"})
    st.ledger_set("t2", "confirmed")
    st.ledger_set("t3", "failed")
    st.ledger_set("t4", "blocked")
    st.save()
    ex = Executor(cfg, RunnerState(cfg.state_dir), MT5Gateway(cfg, sdk=None))
    d = ex.drain_pending(today="2026-07-17")
    assert d["drained"] == [] and d["applied"] == [] and not d["frozen"]


class _SnapshotGateway(MT5Gateway):
    """Fake gateway exposing a fixed broker snapshot; counts any order op."""
    def __init__(self, cfg, positions):
        super().__init__(cfg, sdk=None)
        self._positions = positions
        self.order_ops = 0

    def snapshot(self):
        return True, {"account": None, "positions": self._positions, "orders": []}

    def open_position(self, *a, **k):
        self.order_ops += 1
        return True, {"ticket": 999}


class _RaisingOpenGateway(MT5Gateway):
    """Live gateway whose open_position raises AFTER the SENT record is durable —
    simulates a crash during order_send. snapshot() later reveals the position."""
    def __init__(self, cfg, positions_after=None):
        super().__init__(cfg, sdk=None)
        self._positions_after = positions_after or []
        self.order_ops = 0

    def snapshot(self):
        return True, {"account": None, "positions": self._positions_after, "orders": []}

    def open_position(self, *a, **k):
        self.order_ops += 1
        raise RuntimeError("connection dropped mid-order_send")


# G. SENT adoption on restart, via the PRODUCTION record shape (reserve_pending +
#    the exact _execute SENT write). Adopt unique match; restore mirror; never resubmit.
def test_lr1_sent_adopts_unique_match_and_never_resubmits(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mode = "live"
    it = OrderIntent(intent_id="sent_open_xyz9876", action=OPEN_POSITION, trade_id="L_9",
                     side="long", frontier_bar="B", entry=1.1, stop=1.09, target=1.12)
    st = RunnerState(cfg.state_dir)
    st.reserve_pending(it)                                          # PENDING with payload (prod helper)
    st.ledger_set(it.intent_id, LEDGER_SENT, {"intent": it.to_dict()})  # exact _execute SENT write
    st.save()
    # persisted SENT detail retains the full reconstructable payload
    reloaded = RunnerState(cfg.state_dir)
    assert reloaded.data["ledger"][it.intent_id]["status"] == "sent"
    assert reloaded.data["ledger"][it.intent_id]["detail"]["intent"]["trade_id"] == "L_9"
    # restart: one uniquely matching broker position (comment == intent_id[:26])
    tag = it.intent_id[:26]
    gw = _SnapshotGateway(cfg, [{"ticket": 555, "magic": cfg.magic_number, "comment": tag}])
    d = Executor(cfg, RunnerState(cfg.state_dir), gw).drain_pending(today="2026-07-17")
    assert d["frozen"] is False
    st2 = RunnerState(cfg.state_dir)
    assert st2.ledger_status(it.intent_id) == "confirmed"          # adopted, not resubmitted
    assert st2.mirror_ticket("L_9") == 555                         # mirror restored
    assert gw.order_ops == 0
    # subsequent reconcile is healthy: adopted position is in the mirror -> no unknown freeze
    rep = Executor(cfg, RunnerState(cfg.state_dir), gw).reconcile()
    assert rep.frozen is False
    assert all(f["code"] != "unknown_position" for f in rep.findings)
    # idempotent: a repeat drain is a no-op
    d2 = Executor(cfg, RunnerState(cfg.state_dir), gw).drain_pending(today="2026-07-17")
    assert d2["drained"] == [] and not d2["frozen"]


# G2. Focused regression for the exact defect: drive the REAL _execute path, crash
#     during order_send, then prove restart restores BOTH ledger and mirror. Fails
#     against the pre-fix code (SENT written without payload -> mirror not restorable).
def test_lr1_sent_crash_during_order_send_restores_ledger_and_mirror(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mode = "live"
    it = OrderIntent(intent_id="live_open_abc12345", action=OPEN_POSITION, trade_id="L_7",
                     side="long", frontier_bar="B", entry=1.1, stop=1.09, target=1.12)
    st = RunnerState(cfg.state_dir)
    st.reserve_pending(it)                                          # PENDING with payload
    st.save()
    # drive the real production _execute; crash (raise) during order_send
    crash_gw = _RaisingOpenGateway(cfg)
    with pytest.raises(RuntimeError):
        Executor(cfg, RunnerState(cfg.state_dir), crash_gw).apply([it], today="2026-07-17")
    assert crash_gw.order_ops == 1                                  # order_send was attempted
    # the fix: SENT is durable WITH the payload, so the trade_id survives the crash
    after = RunnerState(cfg.state_dir)
    assert after.ledger_status(it.intent_id) == LEDGER_SENT
    assert after.data["ledger"][it.intent_id]["detail"]["intent"]["trade_id"] == "L_7"
    # restart: broker actually holds the position
    tag = it.intent_id[:26]
    recover_gw = _SnapshotGateway(cfg, [{"ticket": 771, "magic": cfg.magic_number, "comment": tag}])
    d = Executor(cfg, RunnerState(cfg.state_dir), recover_gw).drain_pending(today="2026-07-17")
    assert d["frozen"] is False
    st2 = RunnerState(cfg.state_dir)
    assert st2.ledger_status(it.intent_id) == "confirmed"          # ledger restored
    assert st2.mirror_ticket("L_7") == 771                         # mirror restored (defect fix)
    assert recover_gw.order_ops == 0                               # never resubmitted
    # next reconcile stays healthy (no unknown_position freeze)
    rep = Executor(cfg, RunnerState(cfg.state_dir), recover_gw).reconcile()
    assert rep.frozen is False
    assert all(f["code"] != "unknown_position" for f in rep.findings)


def test_lr1_sent_freezes_when_ambiguous_never_resubmits(tmp_path):
    cfg = _cfg(tmp_path)
    cfg.mode = "live"
    iid = "sent_zzz111222333444555666"
    st = RunnerState(cfg.state_dir)
    st.ledger_set(iid, LEDGER_SENT, {"intent": {
        "intent_id": iid, "action": OPEN_POSITION, "trade_id": "L_8", "side": "long",
        "frontier_bar": "B"}})
    st.save()
    gw = _SnapshotGateway(cfg, [])                    # no matching position -> ambiguous
    ex = Executor(cfg, RunnerState(cfg.state_dir), gw)
    d = ex.drain_pending(today="2026-07-17")
    assert d["frozen"] is True
    assert any(iid in f["detail"] for f in d["reconcile"]["findings"])
    assert gw.order_ops == 0                           # never blindly resubmitted


# H. Dry-run drain never submits.
def test_lr1_drain_dry_run_never_submits(tmp_path):
    from live.rehearsal import CountingGateway
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    it = OrderIntent(intent_id="dr1", action=OPEN_POSITION, trade_id="L_1", side="long",
                     frontier_bar="B", entry=1.1, stop=1.09, target=1.12)
    st.reserve_pending(it)
    st.save()
    gw = CountingGateway(cfg)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gw)
    d = ex.drain_pending(today="2026-07-17")
    assert len(d["applied"]) == 1 and gw.order_ops == 0
    assert RunnerState(cfg.state_dir).ledger_status("dr1") == LEDGER_SIMULATED


# ── C1-A instrumentation: driver-side timing (cycle + golden_pipeline) ───────────
import types  # noqa: E402


def _fake_session(trades_df):
    """Minimal stub of LuxSession/rb/core so golden_pipeline runs fast and
    deterministically without the real 963s Golden pipeline."""
    ns = types.SimpleNamespace
    cfg = ns(news_flatten_minutes_before_blackout=0, detection_timeframe="15min",
             swing_length=5, ob_filter=None, pip_size=0.0001, min_ob_size_pips=0,
             max_ob_size_pips=999, structure_filter=None,
             allowed_structure_directions=None, execution_modes=["triggered_edge"])
    rb = ns(
        filter_date_range=lambda c, cf: c,
        load_news_calendar_events=lambda cf: [],
        load_news_events=lambda cf: [],
        resample_candles=lambda c, tf: c,
        tag_order_blocks_with_news=lambda ob, ce: ob,
        filter_order_blocks_by_structure=lambda ob, sf: ob,
        filter_order_blocks_by_structure_direction=lambda ob, ad: (ob, None),
        entry_scenarios=lambda cf: [{"mode": "triggered_edge", "key": "k"}],
        execute_scenario_job=lambda job, cf, c, sob, ob, nc: {
            "results": [{"trades": trades_df, "summary": {"net_r": "0"}}]},
    )
    core = ns(
        prepare_candles_for_simulation=lambda c: c,
        detect_order_blocks=lambda d, **kw: [],
        prepare_order_blocks_for_simulation=lambda ob: [],
    )
    return ns(rb=rb, core=core, engine_version="fake",
              golden_config=lambda path, end_date: cfg,
              prepare_news_cache=lambda ne, c, m: {})


def test_phase_profiler_and_timed_noop():
    from live.runner import PhaseProfiler, _timed
    p = PhaseProfiler()
    with _timed(p, "a"):
        pass
    with _timed(None, "b"):          # None profiler must be a safe no-op
        pass
    p.increment("cnt", 2)
    p.increment("cnt")
    snap = p.snapshot()
    assert "a" in snap["phase_timings"] and snap["phase_timings"]["a"] >= 0.0
    assert "b" not in snap["phase_timings"]
    assert snap["counters"]["cnt"] == 3


def test_golden_pipeline_profiler_is_parity_safe(tmp_path):
    from live.runner import PhaseProfiler
    cfg = _cfg(tmp_path)
    trades = pd.DataFrame({"trade_id": ["L_1"], "direction": ["bullish"], "fill_time": [""],
                           "outcome": ["OPEN"], "entry": ["1.1"], "stop": ["1.09"], "tp": ["1.12"]})
    runner = LiveRunner(cfg, session=_fake_session(trades))
    candles = pd.DataFrame({"time": pd.date_range("2026-07-17 09:00:00+00:00", periods=3,
                            freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")})
    art_off, art_on = {}, {}
    out_off = runner.golden_pipeline(candles, "2026-07-17", artifacts=art_off)
    prof = PhaseProfiler()
    out_on = runner.golden_pipeline(candles, "2026-07-17", artifacts=art_on, profiler=prof)
    pd.testing.assert_frame_equal(out_off, out_on)            # timing never changes results
    # timing NEVER leaks into the deterministic artifacts channel
    assert "phase_timings" not in art_off and "phase_timings" not in art_on
    assert set(art_on) == {"config", "candles", "order_blocks",
                           "simulation_obs", "news_cache", "summary"}
    assert "execute_scenario_job" in prof.snapshot()["phase_timings"]


def test_run_once_emits_phase_timings_real_path(tmp_path):
    cfg = _cfg(tmp_path)
    trades = pd.DataFrame({"trade_id": ["L_1"], "direction": ["bullish"], "fill_time": [""],
                           "outcome": ["UNFILLED"], "entry": [""], "stop": [""], "tp": [""]})
    candles = pd.DataFrame({"time": pd.date_range("2026-07-17 09:00:00+00:00", periods=40,
                            freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")})
    runner = LiveRunner(cfg, session=_fake_session(trades), candles_provider=lambda: candles)
    out = runner.run_once()
    assert out["status"] in ("bootstrap", "ok")
    assert out["phase_timings"] and "execute_scenario_job" in out["phase_timings"]
    assert isinstance(out["pipeline_s"], float) and out["pipeline_s"] >= 0.0


def test_run_once_injected_pipeline_has_empty_phase_timings(tmp_path):
    cfg = _cfg(tmp_path)
    lux_candles = cfg.lux_root / "data" / "candles"
    lux_candles.mkdir(parents=True)
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=40, freq="1min")
    pd.DataFrame({"time": times.strftime("%Y-%m-%d %H:%M:%S+00:00"),
                  "open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "volume": 0}
                 ).to_csv(lux_candles / "EURUSD_1m_extended_2015_2026.csv", index=False)
    frame = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "", "outcome": "UNFILLED"}])
    runner = LiveRunner(cfg, session=None, pipeline=lambda c, d: frame)
    out = runner.run_once()
    assert out["phase_timings"] == {} and out["pipeline_s"] >= 0.0


def test_ops_log_records_stage_and_phase_timings(tmp_path):
    from live.ops_log import OpsLog
    ops = OpsLog(tmp_path)
    rec = ops.cycle_start()
    ops.cycle_end(rec, boundary="B", status="ok",
                  stage_timings={"runner_s": 1.5}, phase_timings={"resample": 0.2})
    got = ops.read_cycles()
    assert got[-1]["stage_timings"] == {"runner_s": 1.5}
    assert got[-1]["phase_timings"] == {"resample": 0.2}
    # a legacy record (pre-C1, no timing keys) still parses
    with (tmp_path / "ops" / "cycles.jsonl").open("a") as fh:
        fh.write(json.dumps({"cycle_start": "x", "status": "ok"}) + "\n")
    assert ops.read_cycles()[-1].get("stage_timings", {}) == {}


# ── C1-B mid-cycle liveness (daemon beacon; operational telemetry only) ──────────
import time as _time  # noqa: E402


def _wait_until(pred, timeout=3.0, interval=0.01):
    """Poll pred() until true or the deadline; robust to thread timing."""
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        try:
            if pred():
                return True
        except Exception:
            pass
        _time.sleep(interval)
    try:
        return bool(pred())
    except Exception:
        return False


def _read_liveness(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return None


def test_liveness_tick_advances_while_caller_blocked(tmp_path):
    from live.liveness import Liveness
    path = tmp_path / "ops" / "liveness.json"
    lv = Liveness(path, interval_s=0.02)
    lv.start()
    try:
        lv.begin_cycle()
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("tick", 0) >= 1)
        t1 = _read_liveness(path)["tick"]
        _time.sleep(0.2)                              # caller thread blocked in "compute"
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("tick", 0) > t1)
    finally:
        lv.stop()


def test_liveness_phase_update_reflected_promptly(tmp_path):
    from live.liveness import Liveness
    path = tmp_path / "ops" / "liveness.json"
    lv = Liveness(path, interval_s=5.0)               # long tick: only the wake Event is prompt
    lv.start()
    try:
        lv.begin_cycle()
        lv.set_phase("runner")
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("phase") == "runner")
        seq1 = _read_liveness(path)["cycle_seq"]
        lv.begin_cycle()
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("cycle_seq") == seq1 + 1)
    finally:
        lv.stop()


def test_liveness_schema_and_atomic_replacement(tmp_path):
    from live.liveness import Liveness
    path = tmp_path / "ops" / "liveness.json"
    lv = Liveness(path, interval_s=0.02)
    lv.start()
    try:
        lv.begin_cycle()
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("tick", 0) >= 2)
        d = _read_liveness(path)
        for k in ("at", "started_at", "cycle_seq", "phase", "tick",
                  "elapsed_s", "state", "interval_s", "pid"):
            assert k in d, k
        assert isinstance(d["tick"], int) and isinstance(d["elapsed_s"], float)
        assert isinstance(d["cycle_seq"], int) and isinstance(d["pid"], int)
    finally:
        lv.stop()
    assert not path.with_name(path.name + ".tmp").exists()   # no stale temp after clean stop


def test_liveness_write_failure_is_inert(tmp_path):
    from live.liveness import Liveness
    blocker = tmp_path / "blocker"
    blocker.write_text("x")                           # a FILE where a dir is needed
    path = blocker / "liveness.json"                  # parent is a file -> mkdir/write fail
    lv = Liveness(path, interval_s=0.02)
    lv.start()
    try:
        lv.begin_cycle()                              # none of these may raise
        lv.set_phase("runner")
        _time.sleep(0.1)                              # let the worker attempt writes
        assert not path.exists()                      # write never succeeded, but no crash
    finally:
        lv.stop()                                     # must not raise


def test_liveness_cycle_exception_propagates_and_ends_idle(tmp_path):
    from live.liveness import Liveness
    from live.main import cycle
    cfg = _cfg(tmp_path)

    class Ops:
        def cycle_start(self):
            return {"cycle_start": datetime.now(timezone.utc).isoformat()}

        def cycle_end(self, *a, **k):
            raise RuntimeError("boom-logging")        # a logging failure that must propagate

    class Bridge:
        def poll_once(self):
            return {}

    class Runner:
        session = None

        def run_once(self):
            return {"status": "no_new_bar", "boundary": "B"}

    class Executor:
        def drain_pending(self):
            return None

    class Pub:
        def build_payload(self, *a, **k):
            return {}

        def publish(self, payload):
            return None

    path = tmp_path / "ops" / "liveness.json"
    lv = Liveness(path, interval_s=5.0)
    lv.start()
    try:
        with pytest.raises(RuntimeError, match="boom-logging"):   # original exception unchanged
            cycle(cfg, None, Bridge(), Runner(), Executor(), Pub(), Ops(), lv)
        assert _wait_until(lambda: (_read_liveness(path) or {}).get("state") == "idle")
    finally:
        lv.stop()


def test_liveness_bounded_idempotent_shutdown(tmp_path):
    from live.liveness import Liveness
    lv = Liveness(tmp_path / "ops" / "liveness.json", interval_s=0.05)
    lv.start()
    lv.begin_cycle()
    lv.stop(timeout=2.0)
    assert _wait_until(lambda: not (lv._thread and lv._thread.is_alive()))
    lv.stop()                                          # idempotent — must not raise


def test_cycle_without_liveness_writes_no_beacon(tmp_path):
    from live import main as live_main
    from live.ops_log import OpsLog
    cfg = _cfg(tmp_path)

    class Bridge:
        def poll_once(self):
            return {"last_bar_time": None, "appended": 0}

    class Runner:
        session = None

        def run_once(self):
            return {"status": "no_new_bar", "boundary": "B"}

    class Pub:
        def build_payload(self, *a, **k):
            return {}

        def publish(self, payload):
            return None

    ops = OpsLog(cfg.state_dir)
    rec = live_main.cycle(cfg, None, Bridge(), Runner(), None, Pub(), ops)   # 7 args, no liveness
    assert rec["status"] == "no_new_bar" and not rec.get("error")
    assert not (cfg.state_dir / "ops" / "liveness.json").exists()            # no beacon written
