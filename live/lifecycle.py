"""Explicit runtime lifecycle, crash detection and single-process ownership.

WHY THIS EXISTS
---------------
Three measured gaps in the pre-existing runtime:

1. **No lifecycle.** `main()` connected, verified the clock, then entered the
   cycle loop. There was no state in which the process was "up but not yet
   allowed to trade", so nothing could gate the scheduler on reconciliation
   having completed. `heartbeat.json` carries a `phase`, but that is a per-cycle
   indicator (`cycle_running`/`idle`), not a runtime lifecycle.

2. **No crash/clean-shutdown distinction.** The process had no signal handlers
   and wrote no shutdown marker, so an operator could not tell a graceful stop
   from a power loss, and neither could the code — every restart was treated as
   routine.

3. **No ownership.** Two `live.main` processes could open the same state
   directory. Measured: with two `RunnerState` holders, the second `save()`
   silently discards the first's ledger entries. A lost ledger entry re-arms an
   already-executed intent, which is a duplicate order at the broker.

The lock is OS-enforced (`msvcrt.locking` / `fcntl.flock`) rather than a PID
file, because a PID file leaks on a hard kill and needs liveness probing —
`os.kill(pid, 0)` is not a safe liveness probe on Windows. An OS lock is
released by the kernel when the holder dies, so it is correct across power loss
with no stale-lock heuristics.
"""

from __future__ import annotations

import json
import os
import socket
from datetime import datetime, timezone
from pathlib import Path

from live.state import atomic_write_text

# ── phases ─────────────────────────────────────────────────────────────────────
INITIALIZING = "INITIALIZING"
VALIDATING = "VALIDATING"
CONNECTING = "CONNECTING"
RECONCILING = "RECONCILING"
READY = "READY"
RUNNING = "RUNNING"
DEGRADED = "DEGRADED"
RECOVERING = "RECOVERING"
STOPPING = "STOPPING"
STOPPED = "STOPPED"

#: Only phases from which the scheduler may execute intents. Everything else —
#: including DEGRADED and RECOVERING — must not trade.
TRADING_PHASES = (RUNNING,)

LEGAL: dict[str, tuple[str, ...]] = {
    INITIALIZING: (VALIDATING, STOPPING),
    VALIDATING: (CONNECTING, DEGRADED, STOPPING),
    CONNECTING: (RECONCILING, DEGRADED, STOPPING),
    RECONCILING: (READY, DEGRADED, STOPPING),
    READY: (RUNNING, DEGRADED, STOPPING),
    RUNNING: (DEGRADED, STOPPING),
    DEGRADED: (RECOVERING, STOPPING),
    RECOVERING: (CONNECTING, RECONCILING, DEGRADED, STOPPING),
    STOPPING: (STOPPED,),
    STOPPED: (INITIALIZING,),
}

RECONCILE_PENDING = "pending"
RECONCILE_RUNNING = "running"
RECONCILE_COMPLETE = "complete"
RECONCILE_FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class IllegalTransition(RuntimeError):
    """A transition not in LEGAL. Raised, never logged-and-continued."""


class ProcessLock:
    """Exclusive ownership of a state directory, enforced by the OS.

    Held for the process lifetime. Released by the kernel on crash or power
    loss, so there is no stale-lock recovery path to get wrong.
    """

    #: Locked byte lives far past EOF so the PID banner at offset 0 stays
    #: freely writable — locking a region you also truncate fails on Windows.
    _LOCK_OFFSET = 1 << 30

    def __init__(self, path: Path):
        self.path = Path(path)
        self._fh = None

    def acquire(self) -> tuple[bool, str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+")
        try:
            fh.seek(self._LOCK_OFFSET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.seek(0)
            holder = fh.read(200).strip() or "unknown holder"
            fh.close()
            return False, f"state dir already owned by a live process: {holder}"
        fh.seek(0)
        fh.truncate(0)
        fh.write(f"pid={os.getpid()} host={socket.gethostname()} at={_now()}\n")
        fh.flush()
        os.fsync(fh.fileno())
        self._fh = fh
        return True, f"acquired by pid {os.getpid()}"

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            self._fh.seek(self._LOCK_OFFSET)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass                      # dying anyway; the kernel releases it
        finally:
            try:
                self._fh.close()
            finally:
                self._fh = None

    def __enter__(self):
        ok, detail = self.acquire()
        if not ok:
            raise RuntimeError(detail)
        return self

    def __exit__(self, *exc):
        self.release()
        return False


class Lifecycle:
    """Persisted runtime lifecycle with fail-closed transitions.

    Also answers the questions an operator asks after an unexpected restart:
    was the last shutdown clean, when did we crash, in which phase, and how long
    did recovery take.
    """

    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "ops" / "lifecycle.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._read()
        self.data = {
            "phase": INITIALIZING,
            "since": _now(),
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "started_at": _now(),
            "reconcile": {"status": RECONCILE_PENDING, "at": None, "detail": "",
                          "completed_at": None, "runs": 0},
            "last_clean_shutdown": (previous or {}).get("last_clean_shutdown"),
            "last_crash": (previous or {}).get("last_crash"),
            "restart_reason": "first_start",
            "recovery_duration_s": None,
            "updated_at": _now(),
        }
        # A previous record that never reached STOPPED means the process died
        # without running its shutdown path: crash, kill, or power loss.
        if previous:
            prior_phase = previous.get("phase")
            if prior_phase == STOPPED:
                self.data["restart_reason"] = "clean_restart"
            else:
                self.data["restart_reason"] = f"crash_recovery (died in {prior_phase})"
                self.data["last_crash"] = {
                    "at": previous.get("updated_at"), "phase": prior_phase,
                    "pid": previous.get("pid"),
                }
        self._persist()

    # ── persistence ──────────────────────────────────────────────────────────
    def _read(self) -> dict | None:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            # A corrupt lifecycle file must never block startup — it is
            # diagnostics, not trading state. Treated as "no prior record",
            # which conservatively reports the restart as a crash recovery.
            return None

    def _persist(self) -> None:
        self.data["updated_at"] = _now()
        try:
            atomic_write_text(self.path, json.dumps(self.data, indent=1))
        except OSError as exc:            # diagnostics must never kill the loop
            print(f"lifecycle write failed (continuing): {exc}")

    # ── transitions ──────────────────────────────────────────────────────────
    @property
    def phase(self) -> str:
        return self.data["phase"]

    def transition(self, to: str, detail: str = "") -> None:
        current = self.data["phase"]
        if to == current:
            return
        if to not in LEGAL.get(current, ()):
            raise IllegalTransition(
                f"illegal lifecycle transition {current} -> {to} "
                f"(legal: {', '.join(LEGAL.get(current, ())) or 'none'})")
        self.data["phase"] = to
        self.data["since"] = _now()
        if detail:
            self.data["phase_detail"] = detail
        if to == RUNNING and self.data.get("recovery_duration_s") is None:
            started = datetime.fromisoformat(self.data["started_at"])
            self.data["recovery_duration_s"] = round(
                (datetime.now(timezone.utc) - started).total_seconds(), 1)
        if to == STOPPED:
            self.data["last_clean_shutdown"] = _now()
        self._persist()

    def may_trade(self) -> bool:
        return self.data["phase"] in TRADING_PHASES

    # ── reconciliation status ────────────────────────────────────────────────
    def reconcile_started(self) -> None:
        rec = self.data["reconcile"]
        rec["status"] = RECONCILE_RUNNING
        rec["at"] = _now()
        rec["runs"] = rec.get("runs", 0) + 1
        self._persist()

    def reconcile_finished(self, ok: bool, detail: str = "") -> None:
        rec = self.data["reconcile"]
        rec["status"] = RECONCILE_COMPLETE if ok else RECONCILE_FAILED
        rec["detail"] = detail[:200]
        if ok:
            rec["completed_at"] = _now()
        self._persist()

    @property
    def reconciled(self) -> bool:
        return self.data["reconcile"]["status"] == RECONCILE_COMPLETE

    # ── shutdown ─────────────────────────────────────────────────────────────
    def stopping(self, reason: str) -> None:
        if self.data["phase"] not in (STOPPING, STOPPED):
            self.data["phase"] = STOPPING          # reachable from ANY phase
            self.data["since"] = _now()
            self.data["stop_reason"] = reason
            self._persist()

    def stopped(self) -> None:
        self.data["phase"] = STOPPED
        self.data["since"] = _now()
        self.data["last_clean_shutdown"] = _now()
        self._persist()


def read_lifecycle(state_dir: Path) -> dict | None:
    """Read-only accessor for diagnostics (live.status, deploy_check)."""
    try:
        return json.loads((Path(state_dir) / "ops" / "lifecycle.json").read_text())
    except (OSError, ValueError):
        return None
