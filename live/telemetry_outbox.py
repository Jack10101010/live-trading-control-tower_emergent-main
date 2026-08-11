"""M-CT-TRANSPORT-DURABILITY-1 — the Mac is a laptop, and laptops sleep.

MEASURED CAUSE, not a guess. Over 6,328 recorded cycle-end publications the
delivery rate splits almost perfectly by the operator's local clock:

    00:00-06:59  Europe/London    45/2059   =  2.2%
    09:00-11:59  Europe/London   664/696    = 95.4%

and 4,498 of 4,501 failures were timeouts — not rejections, not 5xx, not DNS.
Meanwhile the link itself is healthy: Tailscale DIRECT at 22ms RTT, TCP connect
median 40ms, a full `GET /api/live/status` in 0.17s, a rejected POST in 0.10s.

So nothing here is a network or backend performance problem, and RAISING THE
TIMEOUT WOULD NOT HELP: a sleeping Mac never answers, and a longer synchronous
wait would only lengthen the trading cycle while achieving nothing. The real
defect is architectural in two ways:

  1. DELIVERY WAS COUPLED TO THE TRADING CYCLE. `publisher.publish()` ran
     inline, so an unreachable Mac charged the cycle up to 5s twice over. Small
     against a ~1200s cycle, but a trading loop must not depend on a laptop.

  2. THE LAST COMPLETE SNAPSHOT COULD BE LOST. There was ONE local slot,
     `publish_last.json`, overwritten by every publication — including the
     mid-recompute TRANSITION payload, which deliberately carries no `news` and
     no `decisions`. So the sequence "cycle-end fails -> transition succeeds"
     left the Control Tower holding a payload with the strategy context missing
     and no way to recover it. Observed longest gap with no fresh complete
     snapshot: 28.43 hours.

THE DESIGN: LATEST STATE WINS, IN TWO SLOTS.

    telemetry_outbox/latest_runtime.json    newest snapshot of any kind
    telemetry_outbox/latest_complete.json   newest CYCLE-END snapshot only
    telemetry_outbox/delivery_health.json   what the operator needs to see

The trading cycle only writes these files (atomically, locally, in microseconds)
and returns. A daemon worker delivers them on its own schedule with bounded
backoff. Two slots exist because the two facts have different lifetimes: "what
is the node doing right now" goes stale in minutes, "what did the strategy last
decide" must survive a night of failures.

WHY THIS NEEDS NO SEQUENCE NEGOTIATION WITH THE MAC. A retry never resends an
old payload — it re-reads the slot and sends whatever is CURRENT. Superseded
snapshots are overwritten in place and never transmitted at all, so a delayed
retry cannot land after, and overwrite, newer state. The ordering hazard is
removed by construction rather than by asking the Mac to reject stale arrivals.
A monotonic `sequence` is still stamped so the Mac CAN detect staleness, but
correctness does not depend on it acting.

BOUNDED BY CONSTRUCTION. Two files, fixed size, overwritten in place. A Mac
asleep overnight produces exactly two files and one health file — never a queue,
never unbounded growth, and nothing to flood the Control Tower with on wake.

EXECUTION IS NEVER AFFECTED. This module reads no broker state, holds no lock
the trading loop wants, and touches no authorization, ledger, mirror or
boundary. Every failure path ends in a recorded error.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from live.state import atomic_write_text

OUTBOX_DIRNAME = "telemetry_outbox"
RUNTIME_SLOT = "latest_runtime.json"
COMPLETE_SLOT = "latest_complete.json"
HEALTH_FILE = "delivery_health.json"

#: Short on purpose. When the Mac is awake it answers in ~0.2s, so 5s already
#: had 25x headroom and the failures were never slow responses — they were an
#: absent peer. A worker retry costs nothing, so the timeout only needs to
#: cover a healthy request.
DELIVER_TIMEOUT_S = 5.0
#: Backoff between attempts while unreachable: start fast, settle at one
#: attempt per minute. An overnight sleep is then ~480 cheap attempts, and a
#: waking Mac is picked up within a minute.
BACKOFF_START_S = 5.0
BACKOFF_MAX_S = 60.0
#: How long a delivered runtime snapshot stays "fresh enough" not to resend.
IDLE_POLL_S = 5.0


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class TelemetryOutbox:
    """Durable two-slot spool. Written by the trading cycle, read by the worker.

    Writes are atomic (`atomic_write_text` = temp + fsync + rename), so a crash
    mid-write leaves the previous slot intact rather than a truncated payload.
    """

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / OUTBOX_DIRNAME
        self.dir.mkdir(parents=True, exist_ok=True)
        self.runtime_path = self.dir / RUNTIME_SLOT
        self.complete_path = self.dir / COMPLETE_SLOT
        self.health_path = self.dir / HEALTH_FILE
        self._seq_lock = threading.Lock()
        self._seq = self._recover_sequence()

    def _recover_sequence(self) -> int:
        """Continue the counter across restarts so it stays monotonic."""
        best = 0
        for p in (self.runtime_path, self.complete_path):
            try:
                v = json.loads(p.read_text(encoding="utf-8")).get("sequence")
                if isinstance(v, int):
                    best = max(best, v)
            except (OSError, ValueError, AttributeError):
                pass
        return best

    def next_sequence(self) -> int:
        with self._seq_lock:
            self._seq += 1
            return self._seq

    @staticmethod
    def is_complete(payload: dict) -> bool:
        """A CYCLE-END snapshot, distinguished by carrying strategy context.

        The transition payload published at recompute start deliberately has
        neither block, which is exactly why it must never be allowed to
        overwrite the last complete one.
        """
        if not isinstance(payload, dict):
            return False
        cycle = payload.get("cycle") or {}
        if str(cycle.get("status", "")) == "recomputing":
            return False
        return bool(payload.get("news")) or bool(payload.get("decisions"))

    def stage(self, payload: dict) -> dict:
        """Persist a snapshot. Always the runtime slot; the complete slot too
        when it carries cycle-end context. Returns a small staging record."""
        if not isinstance(payload, dict):
            return {"staged": False, "reason": "payload is not a dict"}
        payload = dict(payload)
        payload["sequence"] = self.next_sequence()
        payload.setdefault("published_at", _utcnow())
        blob = json.dumps(payload, indent=1, default=str)
        complete = self.is_complete(payload)
        atomic_write_text(self.runtime_path, blob)
        if complete:
            atomic_write_text(self.complete_path, blob)
        return {"staged": True, "sequence": payload["sequence"], "complete": complete}

    # ── health ───────────────────────────────────────────────────────────────
    def read_health(self) -> dict:
        try:
            return json.loads(self.health_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def write_health(self, health: dict) -> None:
        try:
            atomic_write_text(self.health_path, json.dumps(health, indent=1, default=str))
        except OSError:
            pass


class DeliveryWorker:
    """Delivers the outbox to the Control Tower, independently of trading.

    A daemon thread: if the process exits, this dies with it and never holds
    shutdown open. It owns no state the trading loop reads, so it cannot stall,
    corrupt or deadlock a cycle. Every exception is contained and recorded —
    a telemetry worker that can crash a trading node would be worse than no
    telemetry at all.
    """

    def __init__(self, outbox: TelemetryOutbox, url: str, *,
                 timeout: float = DELIVER_TIMEOUT_S, sender=None):
        self.outbox = outbox
        self.url = url
        self.timeout = timeout
        #: Injection seam for tests. Production uses urllib.
        self._sender = sender or self._http_post
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.consecutive_failures = 0
        self.last_error: str | None = None
        self.last_success_at: str | None = None
        #: sequence last ACCEPTED by the Mac, per slot — so a slot that has not
        #: changed is not re-sent every few seconds while the Mac is awake.
        self._delivered: dict[str, int] = {}

    # ── transport ────────────────────────────────────────────────────────────
    def _http_post(self, blob: bytes) -> tuple[bool, str]:
        req = urllib.request.Request(
            self.url, data=blob, headers={"Content-Type": "application/json"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = getattr(resp, "status", 0)
                if 200 <= status < 300:
                    return True, str(status)
                return False, f"HTTP {status}"
        except urllib.error.HTTPError as exc:
            # 4xx is a CONTRACT problem, not a transport one. Retrying an
            # identical rejected payload forever would be a hot loop against a
            # server that has already given its answer, so it is recorded and
            # the slot is marked delivered-as-rejected: the next DIFFERENT
            # snapshot will be attempted normally.
            return False, f"HTTP {exc.code}"
        except Exception as exc:
            return False, f"{type(exc).__name__}: {str(exc)[:120]}"

    def _deliver_slot(self, path: Path, name: str) -> bool | None:
        """True delivered, False failed, None nothing to do."""
        try:
            blob = path.read_bytes()
            seq = json.loads(blob).get("sequence")
        except (OSError, ValueError):
            return None
        if not isinstance(seq, int) or self._delivered.get(name) == seq:
            return None
        try:
            ok, detail = self._sender(blob)
        except Exception as exc:
            # `deliver_once` is public and is also called directly by the
            # operator CLI, so containment cannot live only in the thread loop.
            # A transport that raises is a failed delivery, never an exception
            # escaping into a caller.
            self.last_error = f"{name}: {type(exc).__name__}: {str(exc)[:120]}"
            return False
        if ok:
            self._delivered[name] = seq
            self.last_success_at = _utcnow()
            self.last_error = None
            return True
        if detail.startswith("HTTP 4"):
            # Poisoned payload: do not retry this exact one forever.
            self._delivered[name] = seq
            self.last_error = f"{name}: {detail} (rejected; will not retry this snapshot)"
            return False
        self.last_error = f"{name}: {detail}"
        return False

    def deliver_once(self) -> dict:
        """One pass over both slots. Complete first: it is the payload whose
        loss actually costs information."""
        results = {}
        any_attempt = any_ok = False
        for name, path in (("complete", self.outbox.complete_path),
                           ("runtime", self.outbox.runtime_path)):
            r = self._deliver_slot(path, name)
            results[name] = r
            if r is not None:
                any_attempt = True
                any_ok = any_ok or bool(r)
        if any_attempt:
            if any_ok:
                self.consecutive_failures = 0
            else:
                self.consecutive_failures += 1
        self._write_health(results)
        return results

    def _write_health(self, results: dict) -> None:
        h = self.health_snapshot()
        h["last_pass"] = results
        self.outbox.write_health(h)

    def health_snapshot(self) -> dict:
        """What an operator needs, and nothing that could be mistaken for an
        execution-health signal."""
        now = datetime.now(timezone.utc)
        age = None
        if self.last_success_at:
            try:
                age = round((now - datetime.fromisoformat(self.last_success_at)).total_seconds(), 1)
            except ValueError:
                age = None
        pend = {}
        for name, path in (("complete", self.outbox.complete_path),
                           ("runtime", self.outbox.runtime_path)):
            try:
                seq = json.loads(path.read_text(encoding="utf-8")).get("sequence")
            except (OSError, ValueError):
                seq = None
            pend[name] = (isinstance(seq, int) and self._delivered.get(name) != seq)
        status = ("healthy" if self.consecutive_failures == 0
                  else "degraded" if self.consecutive_failures < 10 else "offline")
        return {
            "at": now.isoformat(),
            # DELIVERY health only. Execution health is a separate fact and a
            # separate line in `live.status`: a sleeping Mac must never make a
            # correctly-trading node look unhealthy.
            "ct_delivery": status,
            "target": self.url,
            "last_success_at": self.last_success_at,
            "last_success_age_s": age,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "pending_runtime": pend["runtime"],
            "pending_complete": pend["complete"],
            "delivered_sequences": dict(self._delivered),
            "retry_backoff_s": self._backoff(),
        }

    def _backoff(self) -> float:
        if self.consecutive_failures <= 0:
            return IDLE_POLL_S
        return min(BACKOFF_MAX_S, BACKOFF_START_S * (2 ** min(self.consecutive_failures - 1, 6)))

    # ── thread ───────────────────────────────────────────────────────────────
    def _run(self) -> None:  # pragma: no cover - exercised via start/stop
        while not self._stop.is_set():
            try:
                self.deliver_once()
            except Exception as exc:
                # Containment: a telemetry fault must never escape this thread.
                self.consecutive_failures += 1
                self.last_error = f"worker: {type(exc).__name__}: {str(exc)[:120]}"
                try:
                    self.outbox.write_health(self.health_snapshot())
                except Exception:
                    pass
            self._stop.wait(self._backoff())

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="ct-telemetry-delivery",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
