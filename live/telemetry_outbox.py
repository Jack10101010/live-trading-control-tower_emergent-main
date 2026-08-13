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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from live.state import atomic_write_text

OUTBOX_DIRNAME = "telemetry_outbox"
RUNTIME_SLOT = "latest_runtime.json"
COMPLETE_SLOT = "latest_complete.json"
HEALTH_FILE = "delivery_health.json"
#: M-CT-FLEET-AUTHORITY-1. A THIRD slot, and it needs justifying: the heartbeat
#: is written every ~20s while the strategy slots change every ~20 MINUTES.
#: Sharing a slot would mean rapid writes overwriting the runtime/complete
#: snapshots, destroying exactly the two-slot guarantee the transport milestone
#: established. It is also never classified `complete`, so it can never displace
#: the last cycle-end payload carrying news/decisions.
HEARTBEAT_SLOT = "latest_heartbeat.json"

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
#: How often the node proves it is ALIVE. Fast enough that a ~20 minute
#: recompute is visibly healthy rather than indistinguishable from a dead
#: process, slow enough to be free next to a 67KB strategy snapshot.
HEARTBEAT_INTERVAL_S = 20.0


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
        self.heartbeat_path = self.dir / HEARTBEAT_SLOT
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

    def stage_heartbeat(self, payload: dict) -> dict:
        """Persist a liveness beat. Writes the HEARTBEAT SLOT ONLY.

        Deliberately not routed through `stage`: a heartbeat must never touch
        the runtime or complete slots, and must never be classified complete.
        `news`/`decisions` are stripped defensively even though the builder does
        not add them, because the cost of that assumption being wrong later is
        the Control Tower losing its strategy context to a liveness ping.
        """
        if not isinstance(payload, dict):
            return {"staged": False, "reason": "payload is not a dict"}
        payload = {k: v for k, v in payload.items() if k not in ("news", "decisions")}
        payload["sequence"] = self.next_sequence()
        atomic_write_text(self.heartbeat_path, json.dumps(payload, indent=1, default=str))
        return {"staged": True, "sequence": payload["sequence"], "heartbeat": True}

    def slot_published_at(self, path) -> str | None:
        try:
            return json.loads(Path(path).read_text(encoding="utf-8")).get("published_at")
        except (OSError, ValueError):
            return None

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


class NodeHeartbeat:
    """Proof the PROCESS is alive, independent of the strategy computation.

    THE DEFECT. The node publishes a transition payload at recompute start then
    goes silent for ~20-25 minutes until cycle end. Telemetry age was therefore
    not a liveness signal at all: a healthy long recompute and a process that
    died mid-recompute look identical from the Control Tower.

    WHAT IT DOES NOT MEAN. A beat says "this process is running" and nothing
    else. It deliberately carries the runtime and last-complete timestamps
    UNCHANGED from their own slots, so a fresh beat can never make stale
    strategy data look current. Three ages, three independent facts:

        heartbeat.emitted_at         -> is the node alive
        runtime_snapshot_at          -> how old is the node's runtime view
        last_complete_at             -> how old is the last full strategy cycle

    The cycle updates `phase`/`boundary` on this object; the delivery worker
    reads them. That is the whole coupling — no locks, no strategy work, and a
    beat costs one small file write.
    """

    def __init__(self, instance_id: str, *, pid: int | None = None,
                 started_at: str | None = None):
        self.instance_id = instance_id
        self.pid = pid
        self.started_at = started_at or _utcnow()
        self.phase: str = "starting"
        self.boundary: str | None = None
        self.last_complete_boundary: str | None = None

    def update(self, *, phase: str | None = None, boundary: str | None = None,
               last_complete_boundary: str | None = None) -> None:
        if phase is not None:
            self.phase = str(phase)
        if boundary is not None:
            self.boundary = str(boundary)
        if last_complete_boundary is not None:
            self.last_complete_boundary = str(last_complete_boundary)

    def build(self, outbox: "TelemetryOutbox") -> dict:
        """A MINIMAL canonical envelope carrying the beat.

        It must be a canonical envelope because the Control Tower rejects
        anything else (a bare heartbeat payload returns HTTP 400 — measured).
        It carries no `news` and no `decisions`, so it can never be mistaken for
        a cycle-end snapshot.
        """
        now = datetime.now(timezone.utc)
        try:
            up = round((now - datetime.fromisoformat(self.started_at)).total_seconds(), 1)
        except ValueError:
            up = None
        return {
            "schema_version": "ct.node-telemetry.v1",
            "instance_id": self.instance_id,
            # This is the BEAT's time. It is not the snapshot's time, and it is
            # deliberately not written into `published_at`.
            "published_at": outbox.slot_published_at(outbox.runtime_path),
            "cycle": {"status": self.phase, "last_boundary": self.boundary},
            "heartbeat": {
                "schema_version": "ct.node-heartbeat.v1",
                "emitted_at": _utcnow(),
                "process_started_at": self.started_at,
                "uptime_s": up,
                "pid": self.pid,
                "instance_id": self.instance_id,
                "phase": self.phase,
                "boundary": self.boundary,
                # The two strategy ages, carried through untouched so the Mac
                # renders three distinct facts and never infers one from another.
                "runtime_snapshot_at": outbox.slot_published_at(outbox.runtime_path),
                "last_complete_at": outbox.slot_published_at(outbox.complete_path),
                "last_complete_boundary": self.last_complete_boundary,
            },
        }


class DeliveryWorker:
    """Delivers the outbox to the Control Tower, independently of trading.

    A daemon thread: if the process exits, this dies with it and never holds
    shutdown open. It owns no state the trading loop reads, so it cannot stall,
    corrupt or deadlock a cycle. Every exception is contained and recorded —
    a telemetry worker that can crash a trading node would be worse than no
    telemetry at all.
    """

    def __init__(self, outbox: TelemetryOutbox, url: str, *,
                 timeout: float = DELIVER_TIMEOUT_S, sender=None, heartbeat=None):
        self.outbox = outbox
        #: `NodeHeartbeat` or None. When present the worker emits a beat on its
        #: own cadence, so liveness keeps flowing through a long recompute.
        self.heartbeat = heartbeat
        self._last_beat: float | None = None
        self.last_failure_at: str | None = None
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
        #: M-CT-DELIVERY-RECOVERY-1. Last ATTEMPT outcome per slot:
        #: "ok" | "failed" | "rejected". Status is derived from these CURRENT
        #: facts plus pending state, never from a sticky counter — see
        #: `health_snapshot`.
        self._outcome: dict[str, str] = {}

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
            self._outcome[name] = "ok"
            self.last_success_at = _utcnow()
            self.last_error = None
            return True
        if detail.startswith("HTTP 4"):
            self._outcome[name] = "rejected"
            # Poisoned payload: do not retry this exact one forever.
            self._delivered[name] = seq
            self.last_error = f"{name}: {detail} (rejected; will not retry this snapshot)"
            return False
        self._outcome[name] = "failed"
        self.last_error = f"{name}: {detail}"
        self.last_failure_at = _utcnow()
        return False

    def beat_if_due(self, force: bool = False) -> bool:
        """Stage a heartbeat when its interval has elapsed. Never raises."""
        if self.heartbeat is None:
            return False
        now = time.monotonic()
        if not force and self._last_beat is not None and                 (now - self._last_beat) < HEARTBEAT_INTERVAL_S:
            return False
        try:
            self.outbox.stage_heartbeat(self.heartbeat.build(self.outbox))
            self._last_beat = now
            return True
        except Exception as exc:
            self.last_error = f"heartbeat: {type(exc).__name__}: {str(exc)[:80]}"
            return False

    def deliver_once(self) -> dict:
        """One pass over the slots. Complete first: it is the payload whose
        loss actually costs information. Heartbeat last: it is the cheapest and
        the most replaceable."""
        self.beat_if_due()
        results = {}
        any_attempt = any_ok = False
        for name, path in (("complete", self.outbox.complete_path),
                           ("runtime", self.outbox.runtime_path),
                           ("heartbeat", self.outbox.heartbeat_path)):
            r = self._deliver_slot(path, name)
            results[name] = r
            if r is not None:
                any_attempt = True
                any_ok = any_ok or bool(r)
        # The streak describes work that is STILL outstanding. Resetting it on
        # "any slot succeeded" was the false-healthy bug: a succeeding heartbeat
        # cleared a streak owed to a complete snapshot that had not landed.
        if any_attempt and not any_ok:
            self.consecutive_failures += 1
        elif self._all_settled():
            self.consecutive_failures = 0
        self._write_health(results)
        return results

    def _all_settled(self) -> bool:
        """Every slot's CURRENT content is delivered and its last attempt was ok.

        This is the whole recovery condition. A historical `last_failure_at` or
        a retained `last_error` says what once happened; neither may imply the
        node is unhealthy NOW.
        """
        for name, path in (("complete", self.outbox.complete_path),
                           ("runtime", self.outbox.runtime_path),
                           ("heartbeat", self.outbox.heartbeat_path)):
            try:
                seq = json.loads(path.read_text(encoding="utf-8")).get("sequence")
            except (OSError, ValueError):
                continue                      # a slot with no content owes nothing
            if not isinstance(seq, int):
                continue
            if self._delivered.get(name) != seq:
                return False                  # current content still undelivered
            if self._outcome.get(name) in ("failed", "rejected"):
                return False                  # last thing we know of it was a failure
        return True

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
        pend, seqs = {}, {}
        for name, path in (("complete", self.outbox.complete_path),
                           ("runtime", self.outbox.runtime_path),
                           ("heartbeat", self.outbox.heartbeat_path)):
            try:
                seq = json.loads(path.read_text(encoding="utf-8")).get("sequence")
            except (OSError, ValueError):
                seq = None
            seqs[name] = seq
            pend[name] = (isinstance(seq, int) and self._delivered.get(name) != seq)
        # DERIVED FROM CURRENT FACTS, not from a sticky counter. Two defects
        # this replaces, in opposite directions:
        #   * healthy while `pending_complete` was true, because a succeeding
        #     heartbeat reset the streak the complete slot had earned;
        #   * degraded forever after one HTTP 400 when no new work arrived to
        #     re-evaluate the streak.
        settled = self._all_settled()
        status = ("offline" if self.consecutive_failures >= 10
                  else "healthy" if settled else "degraded")
        return {
            "at": now.isoformat(),
            # DELIVERY health only. Execution health is a separate fact and a
            # separate line in `live.status`: a sleeping Mac must never make a
            # correctly-trading node look unhealthy.
            "ct_delivery": status,
            "target": self.url,
            "last_success_at": self.last_success_at,
            "last_success_age_s": age,
            "last_failure_at": self.last_failure_at,
            "consecutive_failures": self.consecutive_failures,
            "last_error": self.last_error,
            "pending_runtime": pend["runtime"],
            "pending_complete": pend["complete"],
            "pending_heartbeat": pend["heartbeat"],
            "runtime_sequence": seqs["runtime"],
            "complete_sequence": seqs["complete"],
            "heartbeat_sequence": seqs["heartbeat"],
            "delivered_sequences": dict(self._delivered),
            "slot_outcomes": dict(self._outcome),
            "retry_backoff_s": self._backoff(),
            "next_retry_at": (now + timedelta(seconds=self._backoff())).isoformat(),
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
