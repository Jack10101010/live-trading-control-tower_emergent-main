"""C1-B — mid-cycle liveness beacon (operational telemetry only).

A single daemon worker thread writes ``<state_dir>/ops/liveness.json`` on a
wall-clock tick so liveness keeps ADVANCING while the main cycle thread is
blocked inside a long compute stage (e.g. the ~900s Golden pipeline call). The
main cycle thread never performs filesystem I/O here: ``begin_cycle`` /
``set_phase`` / ``end_cycle`` only mutate in-memory fields under a lock and wake
the worker via an Event. The worker is the SOLE writer — it alone creates the
directory, writes the temp file, and calls ``os.replace``.

This is reader-safe operational telemetry — NOT a durable state transaction and
NOT the Architecture V1 Event Journal (no envelope, no event_id/seq/
schema_version). Every public method is fail-open: a telemetry failure must
never fail startup, fail a trading cycle, alter cycle results, or replace the
original trading exception. All public methods catch ``Exception`` (never
``BaseException``) and return normally.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Liveness:
    def __init__(self, path, interval_s: float = 5.0) -> None:
        # No filesystem access in __init__ — directory creation belongs inside
        # the worker's protected write path so a bad path can never fail startup.
        self._path = Path(path)
        self._tmp = self._path.with_name(self._path.name + ".tmp")
        self._interval_s = max(0.01, float(interval_s))
        self._lock = threading.Lock()
        self._wake = threading.Event()      # signals a prompt write on transition
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # in-memory beacon state (guarded by _lock)
        self._cycle_seq = 0
        self._phase = "init"
        self._tick = 0
        self._state = "idle"
        self._started_at = _now()
        self._mono_start = time.monotonic()

    # ── public API — fail-open, non-throwing, no filesystem I/O ───────────────
    def start(self) -> None:
        try:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            t = threading.Thread(target=self._run, name="liveness", daemon=True)
            self._thread = t
            t.start()
        except Exception:
            return

    def begin_cycle(self) -> None:
        try:
            with self._lock:
                self._cycle_seq += 1
                self._tick = 0
                self._state = "running"
                self._phase = "cycle_start"
                self._started_at = _now()
                self._mono_start = time.monotonic()
            self._wake.set()
        except Exception:
            return

    def set_phase(self, label: str) -> None:
        try:
            with self._lock:
                self._phase = str(label)
            self._wake.set()
        except Exception:
            return

    def end_cycle(self) -> None:
        try:
            with self._lock:
                self._state = "idle"
            self._wake.set()
        except Exception:
            return

    def stop(self, timeout: float | None = None) -> None:
        try:
            self._stop.set()
            self._wake.set()
            t = self._thread
            if t is not None:
                t.join(timeout if timeout is not None else self._interval_s + 1.0)
        except Exception:
            return

    # ── worker thread — the SOLE filesystem writer ────────────────────────────
    def _snapshot(self) -> dict:
        with self._lock:
            elapsed = time.monotonic() - self._mono_start
            return {
                "at": _now(),
                "started_at": self._started_at,
                "cycle_seq": self._cycle_seq,
                "phase": self._phase,
                "tick": self._tick,
                "elapsed_s": round(elapsed, 3),
                "state": self._state,
                "interval_s": self._interval_s,
                "pid": os.getpid(),
            }

    def _write_atomic(self, data: dict) -> None:
        # Best-effort throughout: a beacon write failure just skips one update.
        # Directory creation lives here (the protected write path), never __init__.
        payload = json.dumps(data, default=str)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            # Single writer thread ⇒ no concurrent-writer race. A stale tmp from a
            # previously crashed write is simply overwritten by opening "w".
            with open(self._tmp, "w") as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(self._tmp, self._path)   # atomic; readers see old or new, never partial
        except Exception:
            try:                                # best-effort cleanup of a failed tmp
                self._tmp.unlink()
            except Exception:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                with self._lock:
                    self._tick += 1
                self._write_atomic(self._snapshot())
            except Exception:
                pass
            # Wake early on a phase/state transition, else fall through on interval.
            self._wake.wait(self._interval_s)
            self._wake.clear()
        # Final best-effort idle beacon on shutdown.
        try:
            self._write_atomic(self._snapshot())
        except Exception:
            pass
