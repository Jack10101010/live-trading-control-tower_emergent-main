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

# The complete authorized vocabulary. No lifecycle/bookkeeping codes exist.
# Sequence-derived (L2-A): edges in canonical cycle records.
# Level-derived (L2-B): confirmed edges in canonical L1A classifications,
# consumed verbatim via the injected level provider — thresholds are NEVER
# re-derived here, and attention.intervention_required is deliberately NOT
# narrated (it is a roll-up whose underlying facts are narrated individually).
CODES = (
    "CYCLE_ERROR", "CYCLE_ERROR_CLEARED",
    "EXECUTION_FROZEN", "EXECUTION_UNFROZEN",
    "PUBLISH_DELIVERY_LOST", "PUBLISH_DELIVERY_RESTORED",
    "NODE_UNAVAILABLE", "NODE_RECOVERED",
    "CYCLE_STALLED", "CYCLE_RECOVERED",
    "FEED_UNHEALTHY", "FEED_RESTORED",
    "KILL_FILE_ENGAGED", "KILL_FILE_CLEARED",
    "SOURCES_DEGRADED", "SOURCES_RESTORED",
)

# Level kinds: checkpoint detector field, (degrade code, recover code), and
# whether confirmation is debounced (two consecutive observations) or
# immediate (kill file — a source-grounded existence fact, not NOW-derived).
# ORDER MATTERS and is the deterministic within-tick emission order:
# sources_degraded is evaluated FIRST because, while a source outage is
# confirmed, no per-field classification is trusted — the other kinds neither
# seed nor debounce until SOURCES_RESTORED reseeds them (silence rule §9).
_LEVEL_KINDS = {
    "sources_degraded": ("SOURCES_DEGRADED", "SOURCES_RESTORED", True),
    "node_unavailable": ("NODE_UNAVAILABLE", "NODE_RECOVERED", True),
    "cycle_stalled": ("CYCLE_STALLED", "CYCLE_RECOVERED", True),
    "feed_unhealthy": ("FEED_UNHEALTHY", "FEED_RESTORED", True),
    "kill_file_present": ("KILL_FILE_ENGAGED", "KILL_FILE_CLEARED", False),
}
_DEBOUNCE_CONFIRMATIONS = 2  # consecutive observations required for NOW-derived kinds

# Kinds whose classifications depend on the degraded-able sources: suppressed
# (no seed, no debounce) while a source outage is confirmed, and reseeded
# silently after SOURCES_RESTORED. kill_file_present is deliberately EXCLUDED:
# it derives solely from the S7 existence check, which is always performed and
# stays canonical during other-source outages — operator kill actions must
# narrate even mid-outage, and are therefore never reseeded either.
_OUTAGE_GUARDED = ("node_unavailable", "cycle_stalled", "feed_unhealthy")

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
                 logger: logging.Logger | None = None,
                 level_provider: Callable[[Any], dict] | None = None):
        self.cycles_path = Path(cycles_path)
        self.checkpoint_path = Path(checkpoint_path)
        self._append = append_event
        self._package_hash = package_hash
        self.interval_s = interval_s
        self.log = logger or logging.getLogger(__name__)
        # L2-B: injected canonical-status provider (now_utc -> the frozen L1A
        # model, built via collect_sources + build_operational_status by the
        # caller). None disables the level pass entirely (L2-A behaviour).
        self._level_provider = level_provider
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
            # L2-B additive keys (schema stays v1: the loader auto-defaults
            # missing keys, so L2-A checkpoints upgrade by silent seeding and
            # an L2-A rollback simply ignores them):
            "level": {k: None for k in _LEVEL_KINDS},   # last CONFIRMED state per kind
            "debounce": {},                 # kind -> {"state": bool, "count": int}
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
            if not isinstance(state.get("level"), dict) or not isinstance(state.get("debounce"), dict):
                raise ValueError("level/debounce state is not an object")
            for k in _LEVEL_KINDS:
                state["level"].setdefault(k, None)
            return state
        except Exception as exc:
            self.log.warning("ops-journal checkpoint malformed (%s); re-baselining", exc)
            return self._baseline("malformed checkpoint")

    def _baseline(self, reason: str) -> dict:
        """Silent baseline: cursor at the current safe end (end of the last
        COMPLETE line), detector state unseeded, no pending, ZERO events.
        Used on cold start and on checkpoint loss/corruption, where there is
        no trustworthy prior state to carry forward. The projector never
        back-narrates and never emits a lifecycle event."""
        state = self._fresh_state()
        state["cursor"] = self._safe_end()
        self.log.info("ops-journal baselined at byte %s (%s)", state["cursor"], reason)
        return state

    def _rebaseline_source(self, state: dict, reason: str) -> dict:
        """Re-baseline ONLY the cycles-source cursor and the sequence detector
        on a source discontinuity (shrink/replacement). Level narration
        (pending / level / debounce) is INDEPENDENT of cycles.jsonl, so it is
        PRESERVED — a confirmed level pending must never be silently discarded
        just because the cycles file was rotated (audit D-L2B-1). Only the
        cursor, span, identity and sequence detector legitimately reset here."""
        fresh = self._fresh_state()
        fresh["cursor"] = self._safe_end()
        fresh["pending"] = state.get("pending", [])
        fresh["level"] = {k: state.get("level", {}).get(k) for k in _LEVEL_KINDS}
        fresh["debounce"] = state.get("debounce", {})
        self.log.warning("ops-journal source re-baselined at byte %s (%s); "
                         "level narration preserved", fresh["cursor"], reason)
        return fresh

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
        On discontinuity: log, SILENTLY re-baseline the cycles cursor + sequence
        detector (zero events), PRESERVING level narration (audit D-L2B-1).
        Never back-narrate a replacement file."""
        try:
            size = self.cycles_path.stat().st_size
        except OSError:
            size = 0
        if size < state["cursor"]:
            self.log.warning("ops-journal source shrank below cursor (%s < %s); re-baselining",
                             size, state["cursor"])
            return self._rebaseline_source(state, "source shrank")
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
                return self._rebaseline_source(state, "continuity mismatch")
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

    # ── level-derived narration (L2-B) ───────────────────────────────────────

    @staticmethod
    def _observe_levels(model: dict) -> dict:
        """Map the canonical L1A model onto per-kind boolean conditions,
        consuming ONLY canonical classifications — no thresholds, no
        timestamps, no direct file inspection. None = not comparable this
        tick (unknown, or a deliberately silent intermediate): the kind is
        skipped without touching detector or debounce state.

          node_unavailable : aliveness UNAVAILABLE->True, ALIVE->False;
                             STALE is a silent intermediate -> None
          cycle_stalled    : freshness DEGRADED/UNAVAILABLE->True,
                             FRESH/COMPUTING (both healthy)->False
          feed_unhealthy   : polling_healthy False->True, True->False
          kill_file_present: canonical bool, immediate (not NOW-derived)
          sources_degraded : any expected source unavailable
        """
        obs: dict[str, Any] = {}
        aliveness = (model.get("process") or {}).get("aliveness")
        obs["node_unavailable"] = {"UNAVAILABLE": True, "ALIVE": False}.get(aliveness)
        freshness = (model.get("cycle") or {}).get("cycle_freshness")
        obs["cycle_stalled"] = {"DEGRADED": True, "UNAVAILABLE": True,
                                "FRESH": False, "COMPUTING": False}.get(freshness)
        polling = (model.get("data_feed") or {}).get("polling_healthy")
        obs["feed_unhealthy"] = (not polling) if isinstance(polling, bool) else None
        kill = (model.get("attention") or {}).get("kill_file_present")
        obs["kill_file_present"] = kill if isinstance(kill, bool) else None
        available = (model.get("meta") or {}).get("sources_available")
        if isinstance(available, dict) and available:
            obs["sources_degraded"] = not all(v is True for v in available.values())
            obs["_unavailable_sources"] = sorted(
                k for k, v in available.items() if v is not True)
        else:
            obs["sources_degraded"] = None
            obs["_unavailable_sources"] = []
        return obs

    def _level_event(self, kind: str, code: str, old: bool, new: bool,
                     confirmed_at: str, observed: dict) -> tuple[dict, str]:
        """Freeze a complete level EventEntry + deterministic key. Level
        transitions have no replayable source identity, so the confirmation
        identity (the injected NOW at confirmation, frozen here) IS the
        identity — it is persisted into pending BEFORE append and never
        regenerated after a restart."""
        explanations = {
            "NODE_UNAVAILABLE": "Live node liveness beacon UNAVAILABLE — process presumed down.",
            "NODE_RECOVERED": "Live node liveness recovered — beacon ALIVE again.",
            "CYCLE_STALLED": "Live node cycle heartbeat degraded/unavailable — evaluation stalled.",
            "CYCLE_RECOVERED": "Live node cycle heartbeat recovered.",
            "FEED_UNHEALTHY": "Market-data feed polling unhealthy.",
            "FEED_RESTORED": "Market-data feed polling restored.",
            "KILL_FILE_ENGAGED": "Operator kill file ENGAGED — new entries blocked.",
            "KILL_FILE_CLEARED": "Operator kill file cleared.",
            "SOURCES_DEGRADED": "Operational sources degraded: "
                                + (", ".join(observed.get("_unavailable_sources", [])) or "unknown") + ".",
            "SOURCES_RESTORED": "All operational sources available again.",
        }
        event = {
            "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}",
            "seq": 0,
            "category": CATEGORY,
            "code": code,
            "humanExplanation": explanations[code],
            "scenarioKey": None,
            "packageHash": self._package_hash(),
            "who": "system",
            "causedBy": f"l2_projector:level:{kind}",
            "before": {kind: old},
            "after": {kind: new, "confirmed_at": confirmed_at,
                      **({"unavailable_sources": observed.get("_unavailable_sources", [])}
                         if kind == "sources_degraded" and new else {})},
            "at": confirmed_at,
        }
        key = f"l2|{code}|{old}|{new}|{confirmed_at}"
        return event, key

    def _level_pass(self, state: dict, now_utc, summary: dict) -> None:
        """Confirmed-edge narration over canonical classifications. Contained
        independently: a provider failure is logged and skipped (zero events)
        without disturbing the sequence pass or existing state."""
        if self._level_provider is None:
            return
        try:
            model = self._level_provider(now_utc)
            obs = self._observe_levels(model)
        except Exception:
            self.log.warning("ops-journal level provider failed; level pass skipped",
                             exc_info=True)
            return

        confirmed_at = now_utc.isoformat()
        level, debounce = state["level"], state["debounce"]
        transitions: list[tuple[dict, str, str]] = []
        for kind, (code_on, code_off, debounced) in _LEVEL_KINDS.items():
            if kind in _OUTAGE_GUARDED and level.get("sources_degraded") is True:
                # Confirmed source outage: source-dependent classifications are
                # not trusted — they neither seed nor debounce (any open window
                # dies) until SOURCES_RESTORED confirms and reseeds them.
                # kill_file_present is exempt (see _OUTAGE_GUARDED).
                debounce.pop(kind, None)
                continue
            value = obs.get(kind)
            if value is None:
                continue                     # unknown/silent-intermediate: untouched
            if level[kind] is None:
                level[kind] = value          # SILENT first-observation seed
                debounce.pop(kind, None)
                continue
            if value == level[kind]:
                debounce.pop(kind, None)     # steady: any pending window dies
                continue
            # changed vs confirmed state
            if debounced:
                cand = debounce.get(kind)
                if cand and cand.get("state") == value:
                    cand["count"] += 1
                else:
                    debounce[kind] = cand = {"state": value, "count": 1}
                if cand["count"] < _DEBOUNCE_CONFIRMATIONS:
                    continue                 # window open — not yet confirmed
            debounce.pop(kind, None)
            code = code_on if value else code_off
            ev, key = self._level_event(kind, code, level[kind], value,
                                        confirmed_at, obs)
            transitions.append((ev, key, f"level:{kind}:{confirmed_at}"))
            level[kind] = value
            if kind == "sources_degraded" and not value:
                # SOURCES_RESTORED: source-dependent detector state reseeds
                # silently — post-outage classifications must seed fresh, never
                # narrate against the pre-outage world. kill_file_present is
                # not reseeded: it stayed trusted throughout the outage.
                for other in _OUTAGE_GUARDED:
                    level[other] = None
                    debounce.pop(other, None)

        if transitions:
            # Same crash-safe lifecycle as sequence events: freeze -> persist
            # pending -> append -> remove -> (final checkpoint save follows in
            # tick_once). Frozen payloads/keys are never regenerated.
            state["pending"].extend(
                {"event": ev, "idempotency_key": key, "source_identity": ident}
                for ev, key, ident in transitions)
            self._save_checkpoint(state)
            while state["pending"]:
                item = state["pending"][0]
                stored, dedup = self._append(item["event"], item["idempotency_key"])
                summary["deduplicated" if dedup else "appended"] += 1
                state["pending"].pop(0)

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

            # 2) Read new complete lines from the cursor. (No idle early-return:
            #    the L2-B level pass below runs on every tick.)
            lines, new_cursor = self._read_new_lines(state["cursor"])

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

            # 3) Level-derived narration (L2-B) — always AFTER the sequence
            #    pass, preserving deterministic within-tick ordering.
            self._level_pass(state, now_utc, summary)

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
