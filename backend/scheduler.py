"""
Scheduler (Phase 10).

The Scheduler is the ONLY component that decides WHEN strategy evaluations occur.
It NEVER evaluates strategies itself and NEVER executes commands — it invokes the
injected `evaluate_fn` (the Strategy Engine, which is itself read-only).

No real scheduling infra: no cron, no threads, no worker pools, no message queues.
The "clock" is the existing request-driven tick; the queue is a logical single-
threaded FIFO drained synchronously, one job at a time (a lock serialises drains).
"""
from __future__ import annotations

import time
import threading
import uuid
from dataclasses import dataclass, field, asdict
from typing import Callable


# ---------------------------------------------------------------------------
# Trigger types (Objective 2). Manual + Interval run today; the rest are
# structural placeholders (enumerated, not wired to fire).
# ---------------------------------------------------------------------------

class TriggerType:
    MANUAL = "manual"
    INTERVAL = "interval"
    MARKET_EVENT = "market_event"  # placeholder
    REPLAY_TICK = "replay_tick"    # placeholder
    STARTUP = "startup"            # placeholder


ALL_TRIGGERS = [TriggerType.MANUAL, TriggerType.INTERVAL, TriggerType.MARKET_EVENT,
                TriggerType.REPLAY_TICK, TriggerType.STARTUP]
ACTIVE_TRIGGERS = [TriggerType.MANUAL, TriggerType.INTERVAL]


# ---------------------------------------------------------------------------
# Job (Objective 3) — every scheduled evaluation.
# ---------------------------------------------------------------------------

@dataclass
class Job:
    id: str
    strategy: str | None
    deployment: str | None  # scope; None = all deployments (the engine sweeps all)
    trigger: str
    scheduledAt: str
    startedAt: str | None = None
    finishedAt: str | None = None
    status: str = "queued"  # queued | running | completed | failed
    durationMs: float | None = None
    result: dict | None = None


@dataclass
class Schedule:
    id: str
    trigger: str
    intervalMs: int
    enabled: bool = True
    _last_mono: float = field(default=-1e12, repr=False)


# ---------------------------------------------------------------------------
# Evaluation queue (Objective 4) — logical single-threaded FIFO, no concurrency.
# ---------------------------------------------------------------------------

class EvaluationQueue:
    def __init__(self):
        self._items: list[Job] = []

    def enqueue(self, job: Job) -> None:
        self._items.append(job)

    def dequeue(self) -> Job | None:
        return self._items.pop(0) if self._items else None

    def depth(self) -> int:
        return len(self._items)

    def pending(self) -> list[dict]:
        return [asdict(j) for j in self._items]


# ---------------------------------------------------------------------------
# Scheduler (Objectives 1, 5).
# ---------------------------------------------------------------------------

class Scheduler:
    def __init__(self, evaluate_fn: Callable[[], dict], now_fn: Callable[[], str]):
        # evaluate_fn invokes the Strategy Engine (read-only). The scheduler never
        # executes commands and never evaluates strategies itself.
        self._evaluate = evaluate_fn
        self._now = now_fn
        self._queue = EvaluationQueue()
        self._schedules: list[Schedule] = []
        self._history: list[Job] = []
        self._lock = threading.Lock()  # serialises drains → one evaluation at a time
        self._running = False
        self._completed = 0
        self._failed = 0
        self._total_ms = 0.0
        self._last: Job | None = None
        self._current_trigger: str | None = None

    # --- schedule maintenance ---
    def register_interval(self, interval_ms: int) -> Schedule:
        s = Schedule(id=f"sch_{uuid.uuid4().hex[:8]}", trigger=TriggerType.INTERVAL, intervalMs=interval_ms)
        self._schedules.append(s)
        return s

    def _new_job(self, trigger: str, deployment: str | None = None) -> Job:
        return Job(id=f"job_{uuid.uuid4().hex[:10]}", strategy=None, deployment=deployment,
                   trigger=trigger, scheduledAt=self._now())

    # --- triggers ---
    def tick(self) -> dict:
        """Advance the clock: enqueue any due interval jobs, then drain."""
        mono = time.monotonic()
        for s in self._schedules:
            if s.enabled and (mono - s._last_mono) * 1000.0 >= s.intervalMs:
                s._last_mono = mono
                self._queue.enqueue(self._new_job(s.trigger))
        return self._drain()

    def trigger_manual(self, deployment: str | None = None) -> dict:
        self._queue.enqueue(self._new_job(TriggerType.MANUAL, deployment=deployment))
        return self._drain()

    # --- queue processing (single-threaded, FIFO, one at a time) ---
    def _drain(self) -> dict:
        ran: dict | None = None
        with self._lock:
            if self._running:
                return {"skipped": True, "ran": False, "queueDepth": self._queue.depth(),
                        "strategy": None, "scheduler": self._view()}
            self._running = True
            try:
                while self._queue.depth():
                    job = self._queue.dequeue()
                    self._current_trigger = job.trigger
                    job.startedAt = self._now()
                    job.status = "running"
                    t0 = time.perf_counter()
                    try:
                        result = self._evaluate()  # Strategy Engine only — no execution
                        job.strategy = result.get("strategyName")
                        job.status = "completed"
                        rep = result.get("report", {})
                        job.result = {"evaluated": rep.get("evaluated"), "fired": rep.get("fired"),
                                      "executed": rep.get("executed", 0)}
                        self._completed += 1
                        ran = result
                    except Exception as e:  # pragma: no cover - defensive
                        job.status = "failed"
                        job.result = {"error": str(e)}
                        self._failed += 1
                    job.durationMs = round((time.perf_counter() - t0) * 1000, 3)
                    job.finishedAt = self._now()
                    self._total_ms += job.durationMs or 0.0
                    self._last = job
                    self._history.insert(0, job)
                    self._history = self._history[:25]
            finally:
                self._running = False
                self._current_trigger = None
        return {"skipped": False, "ran": ran is not None, "queueDepth": self._queue.depth(),
                "strategy": ran, "scheduler": self._view()}

    # --- reads ---
    def health(self) -> dict:
        total = self._completed + self._failed
        return {
            "schedulerHealthy": True,
            "queueDepth": self._queue.depth(),
            "running": self._running,
            "completed": self._completed,
            "failed": self._failed,
            "averageDurationMs": round(self._total_ms / total, 3) if total else 0.0,
            "lastExecution": asdict(self._last) if self._last else None,
        }

    def _view(self) -> dict:
        return {
            **self.health(),
            "currentTrigger": self._current_trigger,
            "triggerTypes": ALL_TRIGGERS,
            "activeTriggers": ACTIVE_TRIGGERS,
            "schedules": [{"id": s.id, "trigger": s.trigger, "intervalMs": s.intervalMs, "enabled": s.enabled}
                          for s in self._schedules],
            "recentHistory": [asdict(j) for j in self._history[:5]],
        }

    def status(self) -> dict:
        return self._view()

    def queue(self) -> list[dict]:
        return self._queue.pending()

    def history(self) -> list[dict]:
        return [asdict(j) for j in self._history]
