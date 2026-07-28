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

    class OkGateway:
        """cycle() now re-establishes the MT5 link first (reconnect hardening)."""
        def ensure_connected(self):
            return True, "connected"

    ops = OpsLog(cfg.state_dir)
    # cycle() now reconciles FIRST every cycle, so a real Executor is required;
    # in dry_run it pulls no broker snapshot, so no gateway is needed.
    from live.executor import Executor
    from live.state import RunnerState
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=None)
    rec = live_main.cycle(cfg, OkGateway(), BoomBridge(), None, ex, None, ops)
    assert "terminal offline" in rec["error"]                 # logged, not raised
    assert ops.read_cycles()[0]["status"] == "error"
    hb = json.loads((cfg.state_dir / "ops" / "heartbeat.json").read_text())
    assert "terminal offline" in hb["error"]


# ── gateway degradation + CT adapter ───────────────────────────────────────────
def test_gateway_degrades_without_sdk(tmp_path):
    # `sdk=None` means "use the default", which resolves to the real package on a
    # host that HAS MetaTrader5 installed (i.e. the VPS) — so the absent-package
    # state is asserted explicitly instead of depending on the host.
    g = MT5Gateway(_cfg(tmp_path))
    g.sdk = None
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
