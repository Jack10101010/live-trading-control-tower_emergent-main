"""L2-A — Operational Transition Log projector core (Programme 2).

A backend-owned, additive NARRATION subsystem. It incrementally reads the
canonical append-only cycle stream (`<state_dir>/ops/cycles.jsonl`), detects
approved sequence transitions, and appends complete EventEntry records through
the injected shared BotEvent append path. It owns no canonical runtime state
(OP-1): canonical truth stays with the original runtime sources; the projector
persists only narration progress in its own checkpoint, and deleting that
checkpoint changes narration only — never operational truth.

SILENCE RULES (frozen architecture): cold start, checkpoint recovery,
unsupported checkpoint versions, and source discontinuities all re-baseline
SILENTLY — the projector's own lifecycle never produces a BotEvent, and no
historical event is generated merely because the first visible record is
currently errored, frozen or undelivered (first observation seeds state).

AUTHORIZED VOCABULARY (L2-A, sequence class only), category "operational":
  CYCLE_ERROR / CYCLE_ERROR_CLEARED            edge on record error presence
  EXECUTION_FROZEN / EXECUTION_UNFROZEN        edge on record frozen flag
  PUBLISH_DELIVERY_LOST / PUBLISH_DELIVERY_RESTORED
                                               edge on published.delivered

RECORD IDENTITY (deterministic, wall-clock-free): each processed record is
identified by "{start_byte_offset}|{cycle_start}|{cycle_end}" — source
position plus source-native timestamps. Stable across restart, checkpoint
replay and append retry; distinct for genuine recurrences (different records
occupy different offsets). Idempotency keys are "l2|{code}|{identity}", so a
crash-replayed transition reproduces the identical key and deduplicates at
the shared store, while a later genuine recurrence mints a new key.

CHECKPOINT (schema_version 1, atomic tmp+fsync+replace, backend-owned):
narration progress only — byte cursor, last-record span+identity (continuity
verification), per-kind last-observed detector state, and the pending-emission
list (each entry freezes the complete event payload + key + source identity so
a retry re-emits the exact same event; nothing is regenerated from the wall
clock). The pending list is part of the schema now for L2-B compatibility
even though L2-A drains sequence emissions within the same tick.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

CHECKPOINT_SCHEMA_VERSION = 1
CATEGORY = "operational"

# The complete authorized L2-A vocabulary. No lifecycle/bookkeeping codes exist.
CODES = (
    "CYCLE_ERROR", "CYCLE_ERROR_CLEARED",
    "EXECUTION_FROZEN", "EXECUTION_UNFROZEN",
    "PUBLISH_DELIVERY_LOST", "PUBLISH_DELIVERY_RESTORED",
)

_MALFORMED_LOG_PER_TICK = 3  # individually logged bad lines per tick; rest summarized


def env_flag(value: str | None) -> bool:
    """Explicit-truthy parser for OPS_JOURNAL_ENABLED. Default disabled."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class OpsJournalProjector:
    """Single-instance sequence-transition projector (one per backend process).

    Dependency-injected: paths, append callable, package-hash provider,
    interval and logger all come from the caller, so tests run entirely
    against temporary directories. The constructor performs NO I/O.
    """

    def __init__(self, cycles_path: Path, checkpoint_path: Path,
                 append_event: Callable[[dict, str], tuple],
                 package_hash: Callable[[], str] = lambda: "",
                 interval_s: float = 10.0,
                 logger: logging.Logger | None = None):
        self.cycles_path = Path(cycles_path)
        self.checkpoint_path = Path(checkpoint_path)
        self._append = append_event
        self._package_hash = package_hash
        self.interval_s = interval_s
        self.log = logger or logging.getLogger(__name__)
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._state: dict | None = None  # loaded checkpoint (in-memory working copy)

    # ── lifecycle ────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Idempotent: at most one daemon worker thread; never blocks requests."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run, name="ops-journal-projector", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Idempotent, bounded: signal, join with timeout, never wait forever."""
        self._stop_evt.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout_s)
        self._thread = None

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick_once(datetime.now(timezone.utc))
            except Exception:  # backstop — tick_once already contains its own
                self.log.exception("ops-journal tick failed; will retry")
            self._stop_evt.wait(self.interval_s)

    # ── checkpoint ───────────────────────────────────────────────────────────

    def _fresh_state(self) -> dict:
        return {
            "schema_version": CHECKPOINT_SCHEMA_VERSION,
            "cursor": 0,
            "last_record_span": None,       # [start, end] bytes of last consumed line
            "last_record_identity": None,   # identity string for continuity check
            "detector": {"error_present": None, "frozen": None, "delivered": None},
            "pending": [],                  # [{event, idempotency_key, source_identity}]
        }

    def _load_checkpoint(self) -> dict:
        """Missing/malformed/unsupported checkpoints re-baseline SILENTLY
        (log only; zero events; narration restarts from the safe end)."""
        try:
            raw = self.checkpoint_path.read_text()
        except FileNotFoundError:
            return self._baseline("no checkpoint (cold start)")
        except OSError as exc:
            self.log.warning("ops-journal checkpoint unreadable (%s); re-baselining", exc)
            return self._baseline("unreadable checkpoint")
        try:
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("checkpoint is not an object")
            if state.get("schema_version") != CHECKPOINT_SCHEMA_VERSION:
                self.log.warning(
                    "ops-journal checkpoint schema_version %r unsupported; re-baselining",
                    state.get("schema_version"))
                return self._baseline("unsupported schema version")
            for key, default in self._fresh_state().items():
                state.setdefault(key, default)
            if not isinstance(state.get("pending"), list):
                raise ValueError("pending is not a list")
            return state
        except Exception as exc:
            self.log.warning("ops-journal checkpoint malformed (%s); re-baselining", exc)
            return self._baseline("malformed checkpoint")

    def _baseline(self, reason: str) -> dict:
        """Silent baseline: cursor at the current safe end (end of the last
        COMPLETE line), detector state unseeded, no pending, ZERO events.
        The projector never back-narrates and never emits a lifecycle event."""
        state = self._fresh_state()
        state["cursor"] = self._safe_end()
        self.log.info("ops-journal baselined at byte %s (%s)", state["cursor"], reason)
        return state

    def _safe_end(self) -> int:
        """End of the last complete (newline-terminated) line, or 0."""
        try:
            data = self.cycles_path.read_bytes()
        except OSError:
            return 0
        end = data.rfind(b"\n")
        return end + 1 if end >= 0 else 0

    def _save_checkpoint(self, state: dict) -> None:
        """Atomic: full snapshot -> same-dir temp -> flush+fsync -> os.replace.
        A failed write never corrupts the last complete checkpoint."""
        tmp = self.checkpoint_path.with_name(self.checkpoint_path.name + ".tmp")
        try:
            with tmp.open("w") as fh:
                json.dump(state, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.checkpoint_path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    # ── source reading ───────────────────────────────────────────────────────

    def _verify_continuity(self, state: dict) -> dict:
        """Detect source discontinuity: file shrink below the cursor, or the
        saved last-record identity no longer matching the bytes at its span.
        On discontinuity: log, SILENTLY re-baseline (zero events), reset the
        detector from the new baseline. Never back-narrate a replacement file."""
        try:
            size = self.cycles_path.stat().st_size
        except OSError:
            size = 0
        if size < state["cursor"]:
            self.log.warning("ops-journal source shrank below cursor (%s < %s); re-baselining",
                             size, state["cursor"])
            return self._baseline("source shrank")
        span = state.get("last_record_span")
        if span and state.get("last_record_identity"):
            start, end = span
            try:
                with self.cycles_path.open("rb") as fh:
                    fh.seek(start)
                    line = fh.read(end - start)
                record = json.loads(line.decode("utf-8"))
                identity = self._record_identity(start, record)
            except Exception:
                identity = None
            if identity != state["last_record_identity"]:
                self.log.warning("ops-journal source continuity mismatch at span %s; re-baselining", span)
                return self._baseline("continuity mismatch")
        return state

    def _read_new_lines(self, cursor: int) -> tuple[list[tuple[int, int, bytes]], int]:
        """Complete lines after `cursor` as (start, end, raw) plus the new safe
        end. A trailing line without a terminating newline is HELD BACK: not
        parsed, not emitted from, and the cursor never advances past its start —
        it is retried on a later tick and consumed once complete."""
        try:
            with self.cycles_path.open("rb") as fh:
                fh.seek(cursor)
                data = fh.read()
        except OSError:
            return [], cursor
        lines: list[tuple[int, int, bytes]] = []
        offset = cursor
        while True:
            nl = data.find(b"\n", offset - cursor)
            if nl < 0:
                break  # remainder (if any) is a partial trailing line — held back
            start = offset
            end = cursor + nl + 1
            lines.append((start, end, data[start - cursor:nl]))
            offset = end
        return lines, offset

    @staticmethod
    def _record_identity(start_offset: int, record: dict) -> str:
        """Deterministic canonical record identity: source byte position plus
        source-native timestamps. No wall clock; stable across restart, replay
        and retry; distinct for genuine recurrences (distinct offsets)."""
        return f"{start_offset}|{record.get('cycle_start')}|{record.get('cycle_end')}"

    # ── detection ────────────────────────────────────────────────────────────

    @staticmethod
    def _eligible(record: Any) -> bool:
        """Eligibility is explicit: a parsed dict carrying a cycle_end (the
        canonical per-record timestamp every emitted event's `at` derives
        from). Eligibility lives here, not in the generic line reader."""
        return isinstance(record, dict) and isinstance(record.get("cycle_end"), str)

    def _detect(self, state: dict, start: int, record: dict) -> list[tuple[dict, str, str]]:
        """Edge-triggered detection for one eligible record, in fixed kind
        order (error, frozen, publish). First observation of each kind SEEDS
        silently; steady states emit nothing; every edge emits exactly once."""
        det = state["detector"]
        out: list[tuple[dict, str, str]] = []
        identity = self._record_identity(start, record)
        boundary = record.get("boundary")
        at = record["cycle_end"]  # source-record time; never polling/append time

        def emit(code: str, before: dict, after: dict, text: str):
            event = {
                "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}",
                "seq": 0,  # assigned inside the shared append path
                "category": CATEGORY,
                "code": code,
                "humanExplanation": text,
                "scenarioKey": None,
                "packageHash": self._package_hash(),
                "who": "system",
                "causedBy": f"l2_projector:{identity}",
                "before": before,
                "after": {**after, "boundary": boundary, "cycle_end": at,
                          "source_identity": identity},
                "at": at,
            }
            key = f"l2|{code}|{identity}"
            out.append((event, key, identity))

        # error presence — from the canonical record field only
        err = record.get("error", "") or ""
        present = bool(err)
        if det["error_present"] is None:
            det["error_present"] = present            # silent seed
        elif present and not det["error_present"]:
            emit("CYCLE_ERROR", {"error_present": False},
                 {"error_present": True, "error": str(err)[:200]},
                 f"Live node cycle errored at boundary {boundary}: {str(err)[:160]}")
            det["error_present"] = True
        elif not present and det["error_present"]:
            emit("CYCLE_ERROR_CLEARED", {"error_present": True}, {"error_present": False},
                 f"Live node cycle error cleared at boundary {boundary}.")
            det["error_present"] = False

        # execution frozen
        frozen = bool(record.get("frozen", False))
        if det["frozen"] is None:
            det["frozen"] = frozen                     # silent seed
        elif frozen and not det["frozen"]:
            emit("EXECUTION_FROZEN", {"frozen": False}, {"frozen": True},
                 f"Live node execution FROZEN at boundary {boundary}.")
            det["frozen"] = True
        elif not frozen and det["frozen"]:
            emit("EXECUTION_UNFROZEN", {"frozen": True}, {"frozen": False},
                 f"Live node execution unfrozen at boundary {boundary}.")
            det["frozen"] = False

        # publish delivery — only when meaningfully comparable; no transition
        # is invented from an absent/unknown delivery state
        published = record.get("published")
        if isinstance(published, dict) and isinstance(published.get("delivered"), bool):
            delivered = published["delivered"]
            if det["delivered"] is None:
                det["delivered"] = delivered           # silent seed
            elif not delivered and det["delivered"]:
                emit("PUBLISH_DELIVERY_LOST", {"delivered": True}, {"delivered": False},
                     f"Control Tower publish delivery lost at boundary {boundary}.")
                det["delivered"] = False
            elif delivered and not det["delivered"]:
                emit("PUBLISH_DELIVERY_RESTORED", {"delivered": False}, {"delivered": True},
                     f"Control Tower publish delivery restored at boundary {boundary}.")
                det["delivered"] = True
        return out

    # ── tick ─────────────────────────────────────────────────────────────────

    def tick_once(self, now_utc) -> dict:
        """One projection pass. `now_utc` is injected and MUST NOT influence
        any emitted event (sequence events derive entirely from source
        records). Contains its own failures: a failed append retains pending
        state and fails the tick safely; the API is never affected. Returns a
        small summary for tests/telemetry."""
        summary = {"appended": 0, "deduplicated": 0, "pending": 0, "skipped_lines": 0,
                   "error": None}
        try:
            if self._state is None:
                self._state = self._load_checkpoint()
            state = self._verify_continuity(self._state)
            self._state = state

            # 1) Drain saved pending emissions FIRST, with their exact frozen
            #    payloads and keys (never recomputed).
            while state["pending"]:
                item = state["pending"][0]
                stored, dedup = self._append(item["event"], item["idempotency_key"])
                summary["deduplicated" if dedup else "appended"] += 1
                state["pending"].pop(0)

            # 2) Read new complete lines from the cursor.
            lines, new_cursor = self._read_new_lines(state["cursor"])
            if not lines and new_cursor == state["cursor"]:
                self._save_checkpoint(state)
                return summary

            malformed_logged = 0
            for start, end, raw in lines:
                try:
                    record = json.loads(raw.decode("utf-8"))
                except Exception:
                    # complete-but-undecodable line: skip, log (bounded),
                    # advance past it — later records must not stall
                    summary["skipped_lines"] += 1
                    if malformed_logged < _MALFORMED_LOG_PER_TICK:
                        malformed_logged += 1
                        self.log.warning("ops-journal skipping malformed cycles line at bytes %s-%s", start, end)
                    state["cursor"] = end
                    state["last_record_span"] = None
                    state["last_record_identity"] = None
                    continue
                if not self._eligible(record):
                    state["cursor"] = end
                    state["last_record_span"] = None
                    state["last_record_identity"] = None
                    continue

                transitions = self._detect(state, start, record)
                if transitions:
                    # Crash-safe order: freeze payload+key into pending and
                    # PERSIST before appending; a crash at any point replays
                    # the identical event (from pending, or by re-detection
                    # from the un-advanced cursor with identical keys) and
                    # deduplicates at the shared store.
                    state["pending"].extend(
                        {"event": ev, "idempotency_key": key, "source_identity": ident}
                        for ev, key, ident in transitions)
                    self._save_checkpoint(state)
                    while state["pending"]:
                        item = state["pending"][0]
                        stored, dedup = self._append(item["event"], item["idempotency_key"])
                        summary["deduplicated" if dedup else "appended"] += 1
                        state["pending"].pop(0)
                # advance the durable cursor only after the record's events
                # (if any) were safely append-attempted
                state["cursor"] = end
                state["last_record_span"] = [start, end]
                state["last_record_identity"] = self._record_identity(start, record)

            if summary["skipped_lines"] > _MALFORMED_LOG_PER_TICK:
                self.log.warning("ops-journal skipped %s malformed cycles lines this tick",
                                 summary["skipped_lines"])
            self._save_checkpoint(state)
        except Exception as exc:
            # Containment: log, keep pending/cursor state for retry, never
            # propagate into the API process, never emit a failure BotEvent.
            summary["error"] = str(exc)
            self.log.exception("ops-journal tick failed safely; state retained for retry")
            try:
                if self._state is not None:
                    self._save_checkpoint(self._state)
            except Exception:
                self.log.exception("ops-journal checkpoint save also failed; prior checkpoint intact")
        summary["pending"] = len(self._state["pending"]) if self._state else 0
        return summary
