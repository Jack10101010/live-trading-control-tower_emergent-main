"""Runtime recovery, crash survivability and convergence to broker truth.

Every test names the production defect it prevents. The four headline defects
were reproduced against the real code before these assertions were written:

  D1  `store_frame` wrote the diff baseline non-atomically and `load_prev_frame`
      never checked the hash it recorded. A crash mid-write left a truncated CSV
      that parsed cleanly, and the next diff emitted a spurious OPEN_POSITION
      for an already-open trade.
  D2  `reconcile()` ran ONLY inside `executor.apply()`, i.e. only on a cycle that
      produced intents — 1 of 6 realistic cycle outcomes, and never at startup.
  D3  No signal handling and no shutdown marker: every restart looked like a
      crash, and terminations landed mid-write.
  D4  No ownership. Two processes on one state dir silently lose ledger entries
      on save, and a lost entry re-arms an already-executed intent.
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

from live import lifecycle as lc                                      # noqa: E402
from live import main as live_main                                   # noqa: E402
from live import status as live_status                               # noqa: E402
from live.config import LiveConfig                                   # noqa: E402
from live.executor import Executor                                   # noqa: E402
from live.lifecycle import IllegalTransition, Lifecycle, ProcessLock  # noqa: E402
from live.ops_log import OpsLog                                      # noqa: E402
from live.runner import LiveRunner                                   # noqa: E402
from live.state import RunnerState, frame_hash                       # noqa: E402

BAR1 = datetime(2026, 7, 27, 8, 14, tzinfo=timezone.utc)
BAR2 = datetime(2026, 7, 27, 8, 29, tzinfo=timezone.utc)

# `diff_frontier` reads `entry`/`stop`/`tp` (live/intents.py:108); these
# fixtures only ever carried `entry_price`/`stop_price`/`target_price`, so every
# OPEN intent they produced had None geometry. Harmless until an execution rail
# needed it — the divergence guard cannot verify a price against an absent
# entry, and correctly refuses. The canonical names are added so the fixture
# matches the production frame; the *_price columns stay for existing readers.
UNFILLED = [{"trade_id": "T_1", "status": "unfilled", "fill_time": "", "exit_time": "",
             "direction": "long", "entry_price": "1.1", "stop_price": "1.09",
             "target_price": "1.12", "exit_price": "", "net_r": "",
             "entry": "1.1", "stop": "1.09", "tp": "1.12"}]
OPENED = [{"trade_id": "T_1", "status": "open", "fill_time": "2026-07-27 08:20:00",
           "exit_time": "", "direction": "long", "entry_price": "1.1",
           "stop_price": "1.09", "target_price": "1.12", "exit_price": "", "net_r": "",
           "entry": "1.1", "stop": "1.09", "tp": "1.12"}]


def _cfg(tmp_path, **kw) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    for k, v in kw.items():
        setattr(c, k, v)
    c.ensure_dirs()
    return c


def _candles(last):
    idx = pd.date_range(last - timedelta(minutes=200), last, freq="1min")
    return pd.DataFrame({"time": [str(t) for t in idx], "open": 1.0, "high": 1.0,
                         "low": 1.0, "close": 1.0, "volume": 1})


def _frame(rows):
    from conftest import with_identity_anchors
    return pd.DataFrame(with_identity_anchors(rows)).astype(str)


def _runner(cfg, rows, last):
    return LiveRunner(cfg, session=None, pipeline=lambda c, d: _frame(rows),
                      candles_provider=lambda: _candles(last))


class _OkGW:
    def ensure_connected(self): return True, "connected"


class _BridgeStub:
    def __init__(self, boom=None): self.boom = boom
    def poll_once(self):
        if self.boom:
            raise self.boom
        return {"appended": 0}


class _SnapGW:
    """Live-mode gateway double: returns a controllable broker snapshot."""
    def __init__(self, positions=(), ok=True):
        self.positions = list(positions)
        self.ok = ok
        self.calls = 0
    def ensure_connected(self): return True, "connected"
    def snapshot(self):
        self.calls += 1
        if not self.ok:
            return False, "broker unreachable"
        return True, {"positions": list(self.positions)}


# ══════════════════════════════════════════════════════════════════════════════
# D1 — DIFF BASELINE INTEGRITY  (the defect that invents orders)
# ══════════════════════════════════════════════════════════════════════════════
def test_torn_frame_write_cannot_emit_a_spurious_open(tmp_path):
    """D1. Measured before the fix: a half-written baseline produced an
    OPEN_POSITION for a trade that was already open — a duplicate at the broker."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, OPENED, BAR1)
    r.run_once(defer_commit=True); r.commit_cycle()
    fpath = Path(r.state.data["prev_frame_file"])
    fpath.write_bytes(fpath.read_bytes()[: len(fpath.read_bytes()) // 2])   # crash mid-write

    r2 = _runner(cfg, OPENED, BAR2)
    with pytest.raises(RuntimeError, match="corrupt"):
        r2.run_once(defer_commit=True)


def test_corrupt_frame_is_quarantined_and_the_next_start_rebootstraps(tmp_path):
    """D1. Recovery must converge, not wedge: after quarantine the next cycle
    re-establishes the baseline and emits NO intents."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, OPENED, BAR1)
    r.run_once(defer_commit=True); r.commit_cycle()
    fpath = Path(r.state.data["prev_frame_file"])
    fpath.write_bytes(b"trade_id,status\ngarbage")

    with pytest.raises(RuntimeError):
        _runner(cfg, OPENED, BAR2).run_once(defer_commit=True)
    assert fpath.with_suffix(".corrupt.csv").exists()          # preserved for forensics

    res = _runner(cfg, OPENED, BAR2).run_once(defer_commit=True)
    assert res["status"] == "bootstrap"
    assert res["intents"] == []                                # invents nothing


def test_intact_frame_still_loads(tmp_path):
    """Guard against the integrity check rejecting VALID state (a self-inflicted
    outage would be worse than the bug it prevents)."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, UNFILLED, BAR1)
    r.run_once(defer_commit=True); r.commit_cycle()
    r2 = _runner(cfg, OPENED, BAR2)
    res = r2.run_once(defer_commit=True)
    assert res["status"] == "ok"
    assert frame_hash(r2.state.load_prev_frame()) == r2.state.data["prev_frame_hash"]


def test_frame_write_is_atomic(tmp_path):
    """D1. A direct to_csv leaves a torn file; tmp+rename never does."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, OPENED, BAR1)
    r.run_once(defer_commit=True); r.commit_cycle()
    frames = cfg.state_dir / "frames"
    assert not list(frames.glob("*.tmp"))                      # no residue
    assert frame_hash(r.state.load_prev_frame()) == r.state.data["prev_frame_hash"]


def test_state_writes_are_fsynced(tmp_path):
    """D1/power loss. os.replace makes the RENAME atomic; without fsync the
    DATA can still be lost, leaving a correctly-named empty file."""
    import inspect
    from live.state import atomic_write_text
    assert "fsync" in inspect.getsource(atomic_write_text)
    cfg = _cfg(tmp_path)
    st = RunnerState(cfg.state_dir)
    st.ledger_set("i1", "simulated"); st.save()
    assert json.loads((cfg.state_dir / "runner_state.json").read_text())["ledger"]["i1"]
    assert not list(cfg.state_dir.glob("*.tmp"))


# ══════════════════════════════════════════════════════════════════════════════
# D2 — RECONCILIATION LEADS EVERY CYCLE
# ══════════════════════════════════════════════════════════════════════════════
def test_reconcile_runs_on_a_cycle_that_produces_no_intents(tmp_path):
    """D2. Reconciliation used to run only when the engine emitted intents, so a
    server-side stop-out stayed invisible for as long as the engine was quiet."""
    cfg = _cfg(tmp_path)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=None)
    calls = {"n": 0}
    inner = ex._reconcile_inner
    ex._reconcile_inner = lambda: (calls.__setitem__("n", calls["n"] + 1), inner())[1]
    live_main.cycle(cfg, _OkGW(), _BridgeStub(), None, ex, None, OpsLog(cfg.state_dir))
    assert calls["n"] == 1


def test_reconcile_findings_are_logged_on_a_quiet_cycle(tmp_path):
    """D2. Findings existed only when the executor ran, so quiet cycles reported
    nothing — the operator could not see a stop-out until an intent appeared."""
    cfg = _cfg(tmp_path)
    ops = OpsLog(cfg.state_dir)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=None)
    live_main.cycle(cfg, _OkGW(), _BridgeStub(), None, ex, None, ops)
    rec = ops.read_cycles()[0]
    assert rec["reconcile_findings"], "a quiet cycle recorded no reconciliation"


def test_reconciliation_is_idempotent(tmp_path):
    """Repeated reconciliation must converge, not accumulate. A restart loop
    reconciles many times; each pass must reach the same mirror."""
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    st.mirror_set("T_1", 555)                       # we think a position is open
    gw = _SnapGW(positions=[])                      # broker says it is gone (SL hit)
    ex = Executor(cfg, st, gateway=gw)

    first = ex.reconcile()
    after_first = json.loads(json.dumps({"mirror": st.data["mirror"],
                                         "broker_closed": st.data["broker_closed"]}))
    second = ex.reconcile()
    after_second = {"mirror": st.data["mirror"], "broker_closed": st.data["broker_closed"]}

    assert [f["code"] for f in first.findings] == ["missing_position"]
    assert [f["code"] for f in second.findings] == []          # converged
    assert after_first == after_second                          # no drift
    assert not first.frozen and not second.frozen


def test_duplicate_reconciliation_cannot_duplicate_an_intent(tmp_path):
    """Reconciling twice around an apply must not let an executed intent re-fire."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, UNFILLED, BAR1)
    r.run_once(defer_commit=True); r.commit_cycle()
    r2 = _runner(cfg, OPENED, BAR2)
    res = r2.run_once(defer_commit=True)
    ex = Executor(cfg, r2.state, gateway=None)
    # M-LIVE-STALE-OPEN-GUARDS-1. Two OPEN rails now require the modelled fill
    # to still be executable: wall-clock freshness and price divergence. This
    # test is about the LEDGER, so the OPEN must be legitimately applicable —
    # otherwise `first` is empty and the suppression it asserts is untestable.
    # The clock and quote come from the fixture's OWN data (fills 08:20 @ 1.1),
    # so nothing is invented; real wall-clock would make a months-old fixture
    # permanently stale, which is the guards working, not a defect.
    ex.rails._clock = lambda: datetime(2026, 7, 27, 8, 22, tzinfo=timezone.utc)
    ex.rails.quote_provider = lambda: (True, {"bid": 1.1, "ask": 1.1,
                                              "at": "2026-07-27T08:22:00+00:00"})
    ex.reconcile()
    first = ex.apply(res["intents"], today="2026-07-27")
    ex.reconcile()
    second = ex.apply(res["intents"], today="2026-07-27")
    assert len(first["applied"]) == 1
    assert len(second["applied"]) == 0                          # ledger suppressed it


def test_reconcile_is_single_flight(tmp_path):
    """Concurrent reconciliation would read a mirror the other pass is midway
    through rewriting — a torn view of broker truth."""
    cfg = _cfg(tmp_path, mode="live")
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=_SnapGW())
    nested = {}
    inner = ex._reconcile_inner
    def reenter():
        try:
            ex.reconcile()
        except RuntimeError as exc:
            nested["err"] = str(exc)
        return inner()
    ex._reconcile_inner = reenter
    ex.reconcile()
    assert "re-entered" in nested["err"]


def test_single_flight_guard_is_released_after_a_failure(tmp_path):
    """A raising reconcile must not wedge the guard shut forever."""
    cfg = _cfg(tmp_path, mode="live")
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=_SnapGW())
    ex._reconcile_inner = lambda: (_ for _ in ()).throw(RuntimeError("snapshot blew up"))
    with pytest.raises(RuntimeError, match="snapshot blew up"):
        ex.reconcile()
    assert ex._reconciling is False
    ex._reconcile_inner = lambda: __import__("live.executor", fromlist=["x"]).ReconcileReport()
    ex.reconcile()                                              # recovers


def test_broker_unreachable_freezes_rather_than_assuming(tmp_path):
    """Broker truth unavailable must never be read as 'no positions'."""
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    st.mirror_set("T_1", 555)
    ex = Executor(cfg, st, gateway=_SnapGW(ok=False))
    report = ex.reconcile()
    assert report.frozen
    assert [f["code"] for f in report.findings] == ["broker_unreachable"]
    assert st.data["mirror"] == {"T_1": 555}                    # mirror NOT invented away


def test_broker_reconnect_converges_after_an_outage(tmp_path):
    """A transient outage must self-heal on the next reconcile."""
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    st.mirror_set("T_1", 555)
    gw = _SnapGW(ok=False)
    ex = Executor(cfg, st, gateway=gw)
    assert ex.reconcile().frozen
    gw.ok = True                                                # broker returns
    report = ex.reconcile()
    assert not report.frozen
    assert st.data["mirror"] == {}                              # converged to truth


def test_unknown_position_freezes_and_never_auto_closes(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, gateway=_SnapGW(
        positions=[{"ticket": 999, "magic": cfg.magic_number}]))
    report = ex.reconcile()
    assert report.frozen
    assert "unknown_position" in [f["code"] for f in report.findings]


def test_apply_reuses_the_cycle_reconcile_instead_of_running_a_second(tmp_path):
    """Two broker snapshots per cycle is both wasteful and racy."""
    cfg = _cfg(tmp_path, mode="live")
    gw = _SnapGW()
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=gw)
    report = ex.reconcile()
    assert gw.calls == 1
    ex.apply([], today="2026-07-27", report=report)
    assert gw.calls == 1                                        # not re-pulled


# ══════════════════════════════════════════════════════════════════════════════
# D3 — LIFECYCLE, CRASH DETECTION, SHUTDOWN
# ══════════════════════════════════════════════════════════════════════════════
def test_clean_startup_reaches_running(tmp_path):
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    assert life.phase == lc.INITIALIZING and not life.may_trade()
    for phase in (lc.VALIDATING, lc.CONNECTING, lc.RECONCILING, lc.READY, lc.RUNNING):
        life.transition(phase)
    assert life.may_trade()
    assert life.data["recovery_duration_s"] is not None


def test_scheduler_may_not_trade_before_ready(tmp_path):
    """THE gate: no trading in any pre-READY, DEGRADED or RECOVERING phase."""
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    for phase in (lc.INITIALIZING, lc.VALIDATING, lc.CONNECTING, lc.RECONCILING, lc.READY):
        life.data["phase"] = phase
        assert not life.may_trade(), f"{phase} must not trade"
    for phase in (lc.DEGRADED, lc.RECOVERING, lc.STOPPING, lc.STOPPED):
        life.data["phase"] = phase
        assert not life.may_trade(), f"{phase} must not trade"
    life.data["phase"] = lc.RUNNING
    assert life.may_trade()


def test_illegal_transitions_fail_closed(tmp_path):
    """Skipping reconciliation straight to RUNNING must be impossible."""
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    with pytest.raises(IllegalTransition, match="INITIALIZING -> RUNNING"):
        life.transition(lc.RUNNING)
    life.transition(lc.VALIDATING)
    with pytest.raises(IllegalTransition):
        life.transition(lc.READY)                               # skips CONNECTING/RECONCILING


def test_graceful_shutdown_records_a_clean_marker(tmp_path):
    """D3. Without a marker every restart looked like a crash."""
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    life.transition(lc.VALIDATING); life.transition(lc.CONNECTING)
    life.stopping("SIGTERM"); life.stopped()
    assert life.phase == lc.STOPPED
    assert life.data["last_clean_shutdown"]

    restarted = Lifecycle(cfg.state_dir)
    assert restarted.data["restart_reason"] == "clean_restart"
    assert restarted.data["last_crash"] is None


def test_forced_termination_is_detected_as_a_crash_on_next_start(tmp_path):
    """D3. Simulates TerminateProcess / power loss: no shutdown path runs."""
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    life.transition(lc.VALIDATING); life.transition(lc.CONNECTING)
    life.transition(lc.RECONCILING)                              # died mid-reconciliation

    restarted = Lifecycle(cfg.state_dir)
    assert restarted.data["restart_reason"].startswith("crash_recovery")
    assert "RECONCILING" in restarted.data["restart_reason"]
    assert restarted.data["last_crash"]["phase"] == lc.RECONCILING


def test_stopping_is_reachable_from_every_phase(tmp_path):
    """Shutdown must never be blocked by an illegal-transition check."""
    for phase in (lc.INITIALIZING, lc.VALIDATING, lc.CONNECTING, lc.RECONCILING,
                  lc.READY, lc.RUNNING, lc.DEGRADED, lc.RECOVERING):
        d = tmp_path / phase
        d.mkdir()
        life = Lifecycle(d)
        life.data["phase"] = phase
        life.stopping("test"); life.stopped()
        assert life.phase == lc.STOPPED


def test_repeated_crash_restarts_keep_converging(tmp_path):
    """Five crash/restart rounds must not accumulate state or lose the marker."""
    cfg = _cfg(tmp_path)
    for i in range(5):
        life = Lifecycle(cfg.state_dir)
        assert life.data["restart_reason"] != "first_start" or i == 0
        life.transition(lc.VALIDATING)
        life.transition(lc.CONNECTING)                            # then "crash"
    final = Lifecycle(cfg.state_dir)
    assert final.data["last_crash"]["phase"] == lc.CONNECTING
    assert final.data["restart_reason"].startswith("crash_recovery")


def test_corrupt_lifecycle_file_does_not_block_startup(tmp_path):
    """Diagnostics must never wedge the runtime; conservatively reports a crash."""
    cfg = _cfg(tmp_path)
    (cfg.state_dir / "ops").mkdir(parents=True, exist_ok=True)
    (cfg.state_dir / "ops" / "lifecycle.json").write_text("{ not json")
    life = Lifecycle(cfg.state_dir)
    assert life.phase == lc.INITIALIZING


def test_reconcile_status_is_tracked_through_the_lifecycle(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    life = Lifecycle(cfg.state_dir)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=_SnapGW(), lifecycle=life)
    assert not life.reconciled
    ex.reconcile()
    assert life.reconciled
    assert life.data["reconcile"]["runs"] == 1
    assert life.data["reconcile"]["completed_at"]


def test_frozen_reconciliation_is_not_recorded_as_complete(tmp_path):
    """A freeze means broker truth was NOT established — the scheduler must not
    be released on the strength of it."""
    cfg = _cfg(tmp_path, mode="live")
    life = Lifecycle(cfg.state_dir)
    ex = Executor(cfg, RunnerState(cfg.state_dir), gateway=_SnapGW(ok=False), lifecycle=life)
    ex.reconcile()
    assert not life.reconciled
    assert life.data["reconcile"]["status"] == lc.RECONCILE_FAILED


# ══════════════════════════════════════════════════════════════════════════════
# D4 — SINGLE-PROCESS OWNERSHIP
# ══════════════════════════════════════════════════════════════════════════════
def test_second_process_cannot_own_the_same_state_dir(tmp_path):
    """D4. Measured: two RunnerState holders silently lose ledger entries on
    save, and a lost entry re-arms an already-executed intent."""
    cfg = _cfg(tmp_path)
    path = cfg.state_dir / "ops" / "runtime.lock"
    first = ProcessLock(path)
    ok, _ = first.acquire()
    assert ok
    try:
        blocked, detail = ProcessLock(path).acquire()
        assert not blocked
        assert "already owned" in detail
    finally:
        first.release()


def test_lock_is_reacquirable_after_release(tmp_path):
    """A clean stop must not leave the next start locked out."""
    cfg = _cfg(tmp_path)
    path = cfg.state_dir / "ops" / "runtime.lock"
    a = ProcessLock(path); assert a.acquire()[0]; a.release()
    b = ProcessLock(path); ok, _ = b.acquire()
    assert ok
    b.release()


def test_lock_records_its_holder_for_diagnostics(tmp_path):
    import os
    cfg = _cfg(tmp_path)
    path = cfg.state_dir / "ops" / "runtime.lock"
    lock = ProcessLock(path)
    lock.acquire()
    try:
        assert f"pid={os.getpid()}" in path.read_text()
    finally:
        lock.release()


def test_lock_context_manager_releases_on_exception(tmp_path):
    cfg = _cfg(tmp_path)
    path = cfg.state_dir / "ops" / "runtime.lock"
    with pytest.raises(ValueError):
        with ProcessLock(path):
            raise ValueError("boom")
    after = ProcessLock(path)
    assert after.acquire()[0]
    after.release()


# ══════════════════════════════════════════════════════════════════════════════
# DIAGNOSTICS — integrated into live.status, not a parallel surface
# ══════════════════════════════════════════════════════════════════════════════
def _rows(cfg):
    live_status.ROWS.clear()
    live_status.collect(cfg, probe_mt5=False)
    return {q: (v, d) for q, v, d in live_status.ROWS}


def test_status_answers_every_recovery_question(tmp_path):
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    for phase in (lc.VALIDATING, lc.CONNECTING, lc.RECONCILING, lc.READY, lc.RUNNING):
        life.transition(phase)
    RunnerState(cfg.state_dir).save()
    rows = _rows(cfg)
    for q in ("lifecycle state?", "reconciliation complete?", "last shutdown clean?",
              "recovery duration"):
        assert q in rows, f"status does not answer {q!r}"
    assert rows["lifecycle state?"][0] == "OK"


def test_status_flags_a_degraded_runtime(tmp_path):
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    life.transition(lc.VALIDATING)
    life.transition(lc.DEGRADED, "startup reconciliation froze")
    assert _rows(cfg)["lifecycle state?"][0] == "FAIL"


def test_status_reports_a_crash_restart(tmp_path):
    cfg = _cfg(tmp_path)
    life = Lifecycle(cfg.state_dir)
    life.transition(lc.VALIDATING)                # die here
    Lifecycle(cfg.state_dir)                      # restart detects it
    verdict, detail = _rows(cfg)["last shutdown clean?"]
    assert verdict == "WARN" and "crash_recovery" in detail


def test_status_reports_a_never_started_runtime(tmp_path):
    cfg = _cfg(tmp_path)
    rows = _rows(cfg)
    assert rows["lifecycle state?"][0] == "n/a"
    assert "never started" in rows["lifecycle state?"][1]


def test_status_flags_failed_reconciliation(tmp_path):
    cfg = _cfg(tmp_path, mode="live")
    life = Lifecycle(cfg.state_dir)
    Executor(cfg, RunnerState(cfg.state_dir), gateway=_SnapGW(ok=False),
             lifecycle=life).reconcile()
    assert _rows(cfg)["reconciliation complete?"][0] == "FAIL"


# ══════════════════════════════════════════════════════════════════════════════
# SHUTDOWN CONTRACT — including its measured limit
# ══════════════════════════════════════════════════════════════════════════════
def test_signal_handler_requests_a_stop_rather_than_dying_mid_cycle():
    """D3. The handler must set a flag, never terminate in place: dying inside a
    cycle is what lands a process mid-write.

    MEASURED LIMIT (Windows): this works only while the loop is between cycles.
    A CTRL_BREAK delivered during a long C-bound Lux recompute terminates the
    process with STATUS_CONTROL_C_EXIT (0xC000013A) before Python can run the
    handler — verified directly: an idle loop exits rc=0 with reason=SIGBREAK,
    a loop inside a single long numpy call is killed outright. Making the
    recompute interruptible is checkpointing work and out of scope, so the
    mid-cycle case is covered by atomic writes plus crash detection instead of
    by a graceful stop. Do NOT weaken those two on the assumption that a clean
    shutdown always runs.
    """
    stop = {"requested": False, "reason": ""}
    live_main._install_signal_handlers(stop)
    import signal
    handler = signal.getsignal(getattr(signal, "SIGBREAK", signal.SIGINT))
    assert callable(handler)
    handler(None, None)
    assert stop["requested"] is True and stop["reason"]


def test_crash_mid_cycle_leaves_the_boundary_unadvanced(tmp_path):
    """D3 + LR-1 together: a hard kill mid-cycle must replay, never skip."""
    cfg = _cfg(tmp_path)
    r = _runner(cfg, UNFILLED, BAR1)
    r.run_once(defer_commit=True)                  # staged, NOT committed
    assert RunnerState(cfg.state_dir).data["last_boundary"] is None
    r2 = _runner(cfg, UNFILLED, BAR1)              # restart
    assert r2.run_once(defer_commit=True)["status"] in ("bootstrap", "ok")
