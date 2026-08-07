"""M-PRODUCTION-BASE — monitoring honesty: budget and liveness-vs-freshness.

The defect these tests pin: live.status inferred "process is gone" purely from
heartbeat staleness, with a 900s budget sitting BELOW the measured normal cycle
(median 1017.9s, p95 1123.5s, max 1170.3s over the 48-cycle candidate shadow).
Every healthy long cycle therefore raised the exact alarm that should mean a
dead node. Process existence and heartbeat freshness are different facts, and
each combination has a distinct operator action.

All fixtures run against tmp_path state dirs; no MT5, no production live_state.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import status as st  # noqa: E402
from live.config import CYCLE_BUDGET_S, IDLE_HEARTBEAT_BUDGET_S  # noqa: E402


# ── the budget itself ────────────────────────────────────────────────────────

def test_budget_covers_the_entire_measured_candidate_distribution():
    """Measured max was 1170.3s. A budget below that alarms on healthy cycles;
    this pins the budget above measured-max with real headroom, and still low
    enough to catch a wedge inside ~25 minutes."""
    MEASURED_MAX = 1170.3
    assert CYCLE_BUDGET_S >= MEASURED_MAX * 1.15
    assert CYCLE_BUDGET_S <= 1800, "a budget past 30min stops detecting wedges"
    assert IDLE_HEARTBEAT_BUDGET_S == 120


def test_status_no_longer_hardcodes_the_old_900s_budget():
    src = (REPO_ROOT / "live" / "status.py").read_text(encoding="utf-8")
    import re
    for m in re.finditer(r"\b900\b", src):
        line = src[:m.start()].count("\n") + 1
        pytest.fail(f"status.py:{line} still hardcodes 900")


# ── row-level behavior via fixtures ──────────────────────────────────────────

def _utc(offset_s: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=offset_s)).isoformat()


@pytest.fixture
def state_dir(tmp_path):
    (tmp_path / "ops").mkdir()
    (tmp_path / "market_data").mkdir()
    return tmp_path


def _write(state_dir, *, hb_age_s, hb_phase, lc_phase="RUNNING", pid=4242):
    (state_dir / "ops" / "heartbeat.json").write_text(json.dumps(
        {"at": _utc(hb_age_s), "phase": hb_phase, "status": "running",
         "boundary": "2026-08-07 08:00:00+00:00", "error": ""}))
    (state_dir / "ops" / "lifecycle.json").write_text(json.dumps(
        {"phase": lc_phase, "pid": pid, "since": _utc(hb_age_s + 60),
         "reconcile": {"status": "complete", "runs": 1}}))


class _Cfg:
    def __init__(self, root):
        self.state_dir = root
        self.market_data_dir = root / "market_data"
        self.mt5_server = ""
        self.mode = "dry_run"
        self.live_segment_csv = root / "market_data" / "absent.csv"
        self.kill_file = root / "KILL"


def _rows(state_dir, monkeypatch, *, alive):
    monkeypatch.setattr(st, "_pid_alive", lambda pid: alive)
    st.ROWS.clear()
    try:
        st.collect(_Cfg(state_dir), probe_mt5=False)
    except Exception:
        pass  # later sections may lack fixtures; the rows we assert on exist
    out = {q: (v, d) for q, v, d in st.ROWS}
    st.ROWS.clear()
    return out


def test_long_but_healthy_cycle_is_not_an_alarm(state_dir, monkeypatch):
    """1300s into a cycle, pid alive: inside budget, everything OK."""
    _write(state_dir, hb_age_s=1300, hb_phase="cycle_running")
    out = _rows(state_dir, monkeypatch, alive=True)
    assert out["heartbeat current?"][0] == st.OK
    assert out["system stalled?"][0] == st.OK
    assert out["lifecycle state?"][0] == st.OK
    assert "process is gone" not in out["lifecycle state?"][1]


def test_genuinely_dead_process_is_the_real_alarm(state_dir, monkeypatch):
    """RUNNING claim + pid absent -> crashed, regardless of heartbeat age."""
    _write(state_dir, hb_age_s=30, hb_phase="cycle_running")
    out = _rows(state_dir, monkeypatch, alive=False)
    verdict, detail = out["lifecycle state?"]
    assert verdict == st.FAIL
    assert "does not exist" in detail
    assert "died without a clean stop" in detail


def test_genuinely_stalled_cycle_names_stalled_not_dead(state_dir, monkeypatch):
    """pid alive + heartbeat past budget -> stalled: a different fault with a
    different fix, and the wording must not claim death."""
    _write(state_dir, hb_age_s=CYCLE_BUDGET_S + 200, hb_phase="cycle_running")
    out = _rows(state_dir, monkeypatch, alive=True)
    assert out["heartbeat current?"][0] == st.FAIL
    assert out["system stalled?"][0] == st.FAIL
    verdict, detail = out["lifecycle state?"]
    assert verdict == st.FAIL
    assert "stalled, not dead" in detail
    assert "ALIVE" in detail
    assert "process is gone" not in detail


def test_normal_idle_state_is_quiet(state_dir, monkeypatch):
    _write(state_dir, hb_age_s=40, hb_phase="idle")
    out = _rows(state_dir, monkeypatch, alive=True)
    assert out["heartbeat current?"][0] == st.OK
    assert out["system stalled?"][0] == st.OK
    assert out["lifecycle state?"][0] == st.OK


def test_idle_uses_the_tight_idle_budget_not_the_cycle_budget(state_dir, monkeypatch):
    """An idle node writing no heartbeat for 5 minutes IS anomalous."""
    _write(state_dir, hb_age_s=300, hb_phase="idle")
    out = _rows(state_dir, monkeypatch, alive=True)
    assert out["heartbeat current?"][0] == st.FAIL


def test_undeterminable_liveness_says_so_and_falls_back(state_dir, monkeypatch):
    _write(state_dir, hb_age_s=CYCLE_BUDGET_S + 100, hb_phase="cycle_running")
    out = _rows(state_dir, monkeypatch, alive=None)
    verdict, detail = out["lifecycle state?"]
    assert verdict == st.FAIL
    assert "undeterminable" in detail
    assert "process is gone" not in detail, "must not assert what it cannot know"


def test_stopped_is_a_resting_state_not_a_fault(state_dir, monkeypatch):
    _write(state_dir, hb_age_s=5000, hb_phase="idle", lc_phase="STOPPED")
    out = _rows(state_dir, monkeypatch, alive=False)
    assert out["lifecycle state?"][0] == st.OK


# ── the real _pid_alive against real processes ───────────────────────────────

def test_pid_alive_detects_this_very_process():
    import os
    assert st._pid_alive(os.getpid()) is True


def test_pid_alive_detects_a_freshly_dead_process():
    import subprocess, sys as _sys
    p = subprocess.Popen([_sys.executable, "-c", "pass"])
    p.wait()
    time.sleep(0.2)
    assert st._pid_alive(p.pid) is False


def test_pid_alive_handles_garbage_pids():
    assert st._pid_alive(None) is None
    assert st._pid_alive(-5) is None
    assert st._pid_alive("4242") is None


# ── kill-switch path derivation (M-LIVE-FIRE-1 defect) ──────────────────────

def test_kill_file_defaults_beside_the_state_dir_it_protects(tmp_path, monkeypatch):
    """The drill defect: a fixed './live_state/KILL' default meant a node with a
    custom LIVE_STATE_DIR watched a path nobody wrote to, so creating KILL did
    nothing and every rail passed. It failed in the only direction that matters."""
    from live.config import LiveConfig
    monkeypatch.delenv("LIVE_KILL_FILE", raising=False)
    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path / "somewhere_else"))
    monkeypatch.setenv("MARKET_DATA_DIR", str(tmp_path / "md"))
    cfg = LiveConfig()
    assert cfg.kill_file == cfg.state_dir / "KILL"


def test_explicit_kill_file_override_still_wins(tmp_path, monkeypatch):
    from live.config import LiveConfig
    monkeypatch.setenv("LIVE_KILL_FILE", str(tmp_path / "custom.KILL"))
    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MARKET_DATA_DIR", str(tmp_path / "md"))
    assert LiveConfig().kill_file == (tmp_path / "custom.KILL").resolve()
