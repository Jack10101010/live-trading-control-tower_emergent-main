"""L3-A — Notifier policy engine (Programme 3).

A backend-owned, single-instance policy engine over the FROZEN L1A operational
status model. It owns NO operational health: L1A already owns the canonical
health assessment (`attention.attention_reasons`), and this engine consumes
those condition codes VERBATIM (exactly as the L2-B level pass consumes L1A) —
it never derives, re-classifies, or re-thresholds health.

Each tick it calls an injected L1A provider (now -> the frozen model), diffs
the set of present `attention_reasons` codes against durable per-code EPISODE
state, and generates notification DECISIONS (INITIAL / REMIND / RESOLVED) which
are atomically appended to a durable OUTBOX. L3-A ENDS at durable outbox
persistence — nothing is delivered (delivery is L3-B).

FROZEN PIPELINE (canonical, five invariants):
  L1A snapshot -> policy evaluation -> decision objects (memory)
     -> atomic append to durable outbox -> STOP.
  (1) only persisted decisions are deliverable; (2) no transport observes an
  unpersisted decision; (3) no retry regenerates an identity; (4) delivery
  workers consume persisted notifications only; (5) policy generation and
  transport are completely separated. L3-A implements (1)-(3),(5); the append
  boundary is the whole slice.

IDENTITY (frozen at decision generation, never regenerated): the episode key
is `{code}@{first_generated_at}`, where `first_generated_at` is the L1A
`meta.generated_at` of the snapshot in which the code first entered
attention after being absent — frozen into episode state and reused verbatim.
Notification ids: `l3|{code}|{episode_key}|INITIAL`,
`l3|{code}|{episode_key}|REMIND|{n}`, `l3|{code}|{episode_key}|RESOLVED`.
No episode ordinal appears in identity; the reminder ordinal is a within-
episode counter. This mirrors the L2-B level-event key (frozen-timestamp
anchor for a transition with no source-record identity) and is collision-safe
across state-file loss (a new episode always carries a current timestamp).

STATE (schema_version 1, atomic tmp+fsync+replace, backend-owned, gitignored):
per-code episode state only — derived notifier bookkeeping, never canonical
operational truth. Deleting it changes narration/reminders, never truth.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

STATE_SCHEMA_VERSION = 1
OUTBOX_SCHEMA_VERSION = 1

# Policy-as-code (module constants, matching the L1A/L2 convention — not
# operator-runtime config). Confirmation: a NOW-derived condition must be
# present for this many consecutive ticks before an INITIAL is emitted;
# source-grounded conditions (kill file, frozen) confirm on the first tick.
_CONFIRM_TICKS = 2
_IMMEDIATE_CODES = frozenset({"kill_file_present", "frozen"})
_REMINDER_CADENCE_S = 1800.0  # sustained-degradation reminder interval
# Bound the delivered/resolved outbox tail (retention pattern; L3-B/L3-C add
# delivery-aware pruning — here we only cap terminal records to avoid unbounded
# growth while nothing drains the outbox yet).
_OUTBOX_MAX_TERMINAL = 5000


def env_flag(value: str | None) -> bool:
    """Explicit-truthy parser for OPS_NOTIFIER_ENABLED. Default disabled.
    Identical convention to ops_journal.env_flag."""
    return (value or "").strip().lower() in ("1", "true", "yes", "on")


class OutboxIntegrityError(RuntimeError):
    """The outbox holds undelivered pages; unlike the re-baseline-safe state
    file, it cannot be silently discarded. Raised so the caller quarantines it
    loudly rather than losing notifications silently."""


class OpsNotifier:
    """Single-instance notifier policy engine (one per backend process, D5).

    Dependency-injected: the L1A provider, state/outbox paths, interval and
    logger all come from the caller, so tests run entirely against temporary
    directories. The constructor performs NO I/O.
    """

    def __init__(self, l1a_provider: Callable[[Any], dict],
                 state_path: Path, outbox_path: Path,
                 interval_s: float = 10.0,
                 logger: logging.Logger | None = None):
        self._provider = l1a_provider
        self.state_path = Path(state_path)
        self.outbox_path = Path(outbox_path)
        self.interval_s = interval_s
        self.log = logger or logging.getLogger(__name__)
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._state: dict | None = None

    # ── lifecycle (mirrors the projector) ────────────────────────────────────

    def start(self) -> None:
        """Idempotent: at most one daemon worker thread; never blocks requests."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(
                target=self._run, name="ops-notifier", daemon=True)
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
            except Exception:  # backstop — tick_once contains its own
                self.log.exception("ops-notifier tick failed; will retry")
            self._stop_evt.wait(self.interval_s)

    # ── durable state (schema v1; atomic; re-baseline on loss/corruption) ────

    def _fresh_state(self) -> dict:
        # episodes: code -> {present, confirm_count, episode_key,
        #                    first_generated_at, acknowledged,
        #                    reminder_ordinal, last_reminder_at}
        return {"schema_version": STATE_SCHEMA_VERSION, "episodes": {}}

    def _load_state(self) -> dict:
        """Missing / unreadable / malformed / unsupported-version state files
        SILENTLY re-baseline (fresh state; log only) — the state holds only
        derived reminders, never truth, so its loss is narration-only."""
        try:
            raw = self.state_path.read_text()
        except FileNotFoundError:
            return self._fresh_state()
        except OSError as exc:
            self.log.warning("ops-notifier state unreadable (%s); re-baselining", exc)
            return self._fresh_state()
        try:
            state = json.loads(raw)
            if not isinstance(state, dict) or "episodes" not in state:
                raise ValueError("state is not an object with episodes")
            if state.get("schema_version") != STATE_SCHEMA_VERSION:
                self.log.warning("ops-notifier state schema_version %r unsupported; re-baselining",
                                 state.get("schema_version"))
                return self._fresh_state()
            if not isinstance(state["episodes"], dict):
                raise ValueError("episodes is not an object")
            return state
        except Exception as exc:
            self.log.warning("ops-notifier state malformed (%s); re-baselining", exc)
            return self._fresh_state()

    def _save_state(self, state: dict) -> None:
        _atomic_write_json(self.state_path, state)

    # ── durable outbox (schema v1; atomic; quarantine on corruption) ─────────

    def _load_outbox(self) -> list[dict]:
        """The outbox holds undelivered pages: on corruption it is QUARANTINED
        (renamed .corrupt) with an ERROR and a fresh outbox started — it is
        never silently discarded and never silently reused when unreadable."""
        try:
            raw = self.outbox_path.read_text()
        except FileNotFoundError:
            return []
        try:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or doc.get("schema_version") != OUTBOX_SCHEMA_VERSION \
                    or not isinstance(doc.get("records"), list):
                raise ValueError("outbox shape/schema invalid")
            return doc["records"]
        except Exception as exc:
            corrupt = self.outbox_path.with_name(self.outbox_path.name + ".corrupt")
            try:
                os.replace(self.outbox_path, corrupt)
            except OSError:
                pass
            self.log.error("ops-notifier outbox corrupt (%s); quarantined to %s; starting fresh",
                           exc, corrupt.name)
            return []

    def _save_outbox(self, records: list[dict]) -> None:
        _atomic_write_json(self.outbox_path, {"schema_version": OUTBOX_SCHEMA_VERSION,
                                              "records": records})

    # ── policy ───────────────────────────────────────────────────────────────

    @staticmethod
    def _present_codes(model: dict) -> tuple[list[str] | None, str | None]:
        """The attention reason codes present this snapshot, consumed VERBATIM
        from L1A's canonical `attention.attention_reasons` list (the notifier
        does NOT separately read `intervention_required` — L1A defines it as
        bool(attention_reasons), so the reason list is the complete signal).

        Returns (codes, generated_at). A WELL-FORMED L1A snapshot has attention
        as a dict whose attention_reasons is a list; only then are codes real
        (an empty list is a genuine 'no conditions'). A MALFORMED / shapeless
        snapshot returns codes=None so the caller SKIPS the tick (like a
        provider exception) rather than treating absence-of-shape as a
        recovery — a malformed snapshot is not evidence that conditions
        cleared, and must never resolve an active episode."""
        attention = model.get("attention") if isinstance(model, dict) else None
        reasons = attention.get("attention_reasons") if isinstance(attention, dict) else None
        if not isinstance(reasons, list):
            return None, None            # malformed / unusable -> caller skips
        codes = [c for c in reasons if isinstance(c, str)]
        meta = model.get("meta") if isinstance(model, dict) else None
        generated_at = meta.get("generated_at") if isinstance(meta, dict) else None
        return codes, (generated_at if isinstance(generated_at, str) else None)

    def _decision(self, code: str, ep: dict, decision: str, now_iso: str,
                  ordinal: int | None = None) -> dict:
        episode_key = ep["episode_key"]
        if decision == "REMIND":
            nid = f"l3|{code}|{episode_key}|REMIND|{ordinal}"
            human = f"Operational condition still active: {code} (reminder {ordinal})."
        elif decision == "RESOLVED":
            nid = f"l3|{code}|{episode_key}|RESOLVED"
            human = f"Operational condition resolved: {code}."
        else:  # INITIAL
            nid = f"l3|{code}|{episode_key}|INITIAL"
            human = f"Operational attention required: {code}."
        return {
            "notification_id": nid,
            "code": code,
            "episode_key": episode_key,
            "decision": decision,
            "payload": {"code": code, "humanExplanation": human,
                        "first_generated_at": ep["first_generated_at"]},
            "status": "pending",
            "created_at": now_iso,
            "attempts": 0,
            "next_attempt_at": None,
            "delivered_at": None,
            "last_error": None,
            "source_generated_at": now_iso,
        }

    def tick_once(self, now_utc) -> dict:
        """One policy pass. now_utc is injected; it never participates in any
        notification identity (episode keys anchor to the frozen L1A
        first_generated_at). Contains its own failures — a provider error skips
        the tick safely, the API is never affected. Returns a small summary."""
        summary = {"decisions": 0, "initial": 0, "remind": 0, "resolved": 0, "error": None}
        try:
            if self._state is None:
                self._state = self._load_state()
            state = self._state
            episodes: dict = state["episodes"]
            now_iso = now_utc.isoformat()

            try:
                model = self._provider(now_utc)
                present, generated_at = self._present_codes(model)
            except Exception:
                self.log.warning("ops-notifier L1A provider failed; tick skipped", exc_info=True)
                return summary
            if present is None:
                # Malformed / unusable snapshot: skip like a provider failure —
                # NEVER resolve active episodes on absence-of-shape (Defect 2).
                self.log.warning("ops-notifier snapshot malformed; tick skipped (no resolution)")
                return summary
            present_set = set(present)

            # Phase 1 — reconcile presence: open new episodes, freezing each
            # episode_key. PERSIST the opened keys BEFORE any outbox record can
            # embed them, so a crash between the outbox write and the final
            # state write reloads the SAME key and the append dedup is
            # authoritative (Defect 1 / frozen invariant #3). This mirrors L2's
            # "persist the frozen identity before the observable effect" rule.
            opened = False
            for code in present:
                ep = episodes.get(code)
                if ep is None or not ep.get("present"):
                    fga = generated_at or now_iso
                    episodes[code] = {
                        "present": True, "confirm_count": 0,
                        "episode_key": f"{code}@{fga}", "first_generated_at": fga,
                        "acknowledged": False, "reminder_ordinal": 0,
                        "last_reminder_at": None, "initial_emitted": False,
                        "seeded": False}
                    opened = True
            if opened:
                self._save_state(state)

            new_records: list[dict] = []

            # Phase 2 — decisions. The entering tick counts as observation #1
            # (immediate codes page on it; others need _CONFIRM_TICKS).
            for code in present:
                ep = episodes[code]
                if not ep.get("initial_emitted"):
                    ep["confirm_count"] = ep.get("confirm_count", 0) + 1
                    need = 1 if code in _IMMEDIATE_CODES else _CONFIRM_TICKS
                    # present-at-baseline (seeded) never back-pages; page-eligible
                    # only after it clears and recurs (seeded=False).
                    if not ep.get("seeded") and ep["confirm_count"] >= need:
                        new_records.append(self._decision(code, ep, "INITIAL", now_iso))
                        ep["initial_emitted"] = True
                        ep["last_reminder_at"] = now_iso
                        summary["initial"] += 1
                elif not ep.get("acknowledged"):
                    last = ep.get("last_reminder_at")
                    if last is not None and _age_s(now_utc, last) >= _REMINDER_CADENCE_S:
                        ep["reminder_ordinal"] = ep.get("reminder_ordinal", 0) + 1
                        new_records.append(self._decision(code, ep, "REMIND", now_iso,
                                                          ordinal=ep["reminder_ordinal"]))
                        ep["last_reminder_at"] = now_iso
                        summary["remind"] += 1

            # departed codes: RESOLVED (only if we had paged it). Their keys are
            # durable from a prior tick, so RESOLVED identity is always stable.
            for code, ep in list(episodes.items()):
                if code in present_set:
                    continue
                if ep.get("present") and ep.get("initial_emitted"):
                    new_records.append(self._decision(code, ep, "RESOLVED", now_iso))
                    summary["resolved"] += 1
                episodes.pop(code, None)

            # append to the durable outbox (dedup by frozen id), then persist the
            # advanced episode state.
            if new_records:
                records = self._load_outbox()
                have = {r.get("notification_id") for r in records}
                appended = [r for r in new_records if r["notification_id"] not in have]
                records.extend(appended)
                records = _prune_terminal(records, _OUTBOX_MAX_TERMINAL)
                self._save_outbox(records)
                summary["decisions"] = len(appended)
            self._save_state(state)
        except Exception as exc:
            summary["error"] = str(exc)
            self.log.exception("ops-notifier tick failed safely; state retained")
        return summary

    def baseline(self, now_utc) -> dict:
        """Silent baseline: seed episode state from the current snapshot WITHOUT
        paging. A condition already present at startup/first-run is recorded as
        present+seeded so it never back-pages; it becomes page-eligible only
        after it clears and recurs. Emits zero decisions."""
        summary = {"seeded": 0, "error": None}
        try:
            if self._state is None:
                self._state = self._load_state()
            state = self._state
            model = self._provider(now_utc)
            present, generated_at = self._present_codes(model)
            now_iso = now_utc.isoformat()
            for code in present:
                if code not in state["episodes"]:
                    fga = generated_at or now_iso
                    state["episodes"][code] = {
                        "present": True, "confirm_count": _CONFIRM_TICKS,
                        "episode_key": f"{code}@{fga}", "first_generated_at": fga,
                        "acknowledged": False, "reminder_ordinal": 0,
                        "last_reminder_at": None, "initial_emitted": False,
                        "seeded": True}
                    summary["seeded"] += 1
            self._save_state(state)
        except Exception as exc:
            summary["error"] = str(exc)
            self.log.exception("ops-notifier baseline failed safely")
        return summary


# ── shared helpers ───────────────────────────────────────────────────────────

def _atomic_write_json(path: Path, doc: Any) -> None:
    """Atomic: full snapshot -> same-dir temp -> flush+fsync -> os.replace.
    Mirrors ops_journal._save_checkpoint; a failed write never corrupts the
    last complete file."""
    tmp = path.with_name(path.name + ".tmp")
    try:
        with tmp.open("w") as fh:
            json.dump(doc, fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _age_s(now_utc, iso: str) -> float:
    """Seconds between an ISO timestamp and now; tolerant of skew (never raises
    into the caller — a malformed/absent stamp yields 0.0, deferring the action)."""
    try:
        then = datetime.fromisoformat(iso)
        if then.tzinfo is None:
            then = then.replace(tzinfo=timezone.utc)
        return (now_utc - then).total_seconds()
    except Exception:
        return 0.0


def _prune_terminal(records: list[dict], cap: int) -> list[dict]:
    """Bound growth by capping RESOLVED records (terminal in L3-A, since nothing
    delivers yet); pending INITIAL/REMIND are always retained."""
    terminal = [r for r in records if r.get("decision") == "RESOLVED"]
    if len(terminal) <= cap:
        return records
    drop = set(id(r) for r in terminal[:len(terminal) - cap])
    return [r for r in records if id(r) not in drop]
