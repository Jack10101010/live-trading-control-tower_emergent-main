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
import math
import os
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
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

# ── L3-B delivery constants (tunable module policy, not frozen architecture) ─
_DELIVERY_BUDGET_PER_TICK = 20     # max webhook attempts per tick (latency bound)
_WEBHOOK_TIMEOUT_S = 5.0           # per-request HTTP timeout
_MAX_ATTEMPTS = 6                  # bounded retries before dead_letter
_BACKOFF_BASE_S = 30.0             # exponential backoff base
_BACKOFF_CAP_S = 3600.0            # backoff ceiling
_DELIVERED_RETENTION = 5000        # keep newest N delivered records
_DEAD_LETTER_RETENTION = 5000      # keep newest N dead-lettered records (L3-C inspects)
# HTTP 4xx that are transient and therefore retryable (per the frozen contract).
_RETRYABLE_4XX = frozenset({408, 425, 429})


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
                 logger: logging.Logger | None = None,
                 webhook_url: str | None = None,
                 clock: Callable[[], Any] | None = None):
        self._provider = l1a_provider
        self.state_path = Path(state_path)
        self.outbox_path = Path(outbox_path)
        self.interval_s = interval_s
        self.log = logger or logging.getLogger(__name__)
        # L3-C: completion-clock seam. Used ONLY to stamp last_tick.at at
        # PUBLICATION time (D-L3C-1) — never for episode identity, policy time,
        # retry math, or delivery timestamps, which all keep the injected tick
        # clock. Defaults to UTC-now; tests inject a deterministic sequence.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # L3-B: env-only webhook destination (single operator URL). None/blank
        # or malformed -> delivery is skipped, records stay pending (config
        # error, never a page loss). Read once (env config; restart to change).
        self._webhook_url = (webhook_url or "").strip()
        self._no_url_warned = False           # once-per-process missing-URL log
        # A non-redirect-following opener: a webhook must not redirect; a 3xx is
        # a misconfiguration (permanent), never an SSRF-surprise follow.
        self._opener = urllib.request.build_opener(_NoRedirect)
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._lock = threading.Lock()
        self._state: dict | None = None
        # L3-C read-only observability. `_last_tick` is a process-lifetime
        # activity snapshot published by the worker after each tick (sole
        # publisher; the status route only reads the reference). None until the
        # first tick_once completes and after every restart (never persisted).
        self._last_tick: dict | None = None
        # Once-per-process guard so repeated polling of a corrupt outbox by the
        # read-only status route cannot flood the log; reset on a clean read.
        self._inspect_corrupt_logged = False

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
                records = _prune_outbox(records)
                self._save_outbox(records)
                summary["decisions"] = len(appended)
            self._save_state(state)

            # ── L3-B delivery phase (single writer; strictly AFTER policy) ────
            self._deliver_phase(now_utc, summary)
        except Exception as exc:
            summary["error"] = str(exc)
            self.log.exception("ops-notifier tick failed safely; state retained")
        finally:
            # L3-C: publish this tick's activity snapshot on EVERY exit (normal,
            # provider-skip, malformed-skip, outer-exception). Purely
            # observational — it changes no decision/delivery/persistence.
            self._publish_last_tick(summary)
        return summary

    def _publish_last_tick(self, summary: dict) -> None:
        """L3-C: publish a completed-tick activity snapshot. Sole publisher is
        the tick thread; the status route is a reader only. A brand-new dict of
        copied SCALARS (no reference to the mutable summary/outbox/records/
        episodes/payload) is assigned to `self._last_tick` in one reference
        swap and never mutated afterward — so a reader sees the prior-complete
        or the new-complete object, never a partial one (no lock needed; the
        start lock is unrelated). `at` is read from the completion clock at
        PUBLICATION time (D-L3C-1) — the tick-start clock is NOT reused, so a
        slow delivery phase cannot backdate the snapshot. The delivery
        sub-counts are ATTEMPTED outcomes for this tick, not a persisted ledger
        (persisted truth is the fresh outbox aggregate); this preserves the
        existing summary semantics verbatim."""
        self._last_tick = {
            "at": self._clock().isoformat(),
            "error": summary.get("error"),
            "decisions": int(summary.get("decisions", 0)),
            "initial": int(summary.get("initial", 0)),
            "remind": int(summary.get("remind", 0)),
            "resolved": int(summary.get("resolved", 0)),
            "delivered": int(summary.get("delivered", 0)),
            "retried": int(summary.get("retried", 0)),
            "dead_lettered": int(summary.get("dead_lettered", 0)),
        }

    # ── L3-B delivery ────────────────────────────────────────────────────────

    def _deliver_phase(self, now_utc, summary: dict) -> None:
        """Drain due outbox records to the webhook, up to the per-tick budget.
        Runs in the single tick thread (no separate writer), strictly after the
        policy phase, so a slow webhook can never delay condition detection.
        Contained: any failure here is logged and never propagates to the API."""
        summary["delivered"] = 0
        summary["retried"] = 0
        summary["dead_lettered"] = 0
        if not _valid_webhook_url(self._webhook_url):
            # Enabled but no/invalid URL: records stay pending, NO attempt, no
            # attempts++/retry/dead_letter — a config error must not lose pages.
            if not self._no_url_warned:
                self._no_url_warned = True
                self.log.warning("ops-notifier enabled but OPS_NOTIFIER_WEBHOOK_URL "
                                 "is absent/invalid; notifications remain pending")
            return
        try:
            records = self._load_outbox()
        except Exception:
            self.log.exception("ops-notifier delivery: outbox load failed; skipping")
            return
        due = self._select_due(records, now_utc)
        for rec in due:
            self._attempt(rec, now_utc, summary)
            # Persist EACH outcome atomically (crash bounds duplicate resends to
            # the single in-flight record). Then re-prune terminals.
            try:
                self._save_outbox(_prune_outbox(records))
            except Exception:
                self.log.exception("ops-notifier delivery: outbox save failed; retrying next tick")
                return

    def _select_due(self, records: list[dict], now_utc) -> list[dict]:
        """Currently-due delivery records: status in {pending, retry_wait} and
        (next_attempt_at is None or reached). Oldest created_at first, tie-broken
        by notification_id (both existing, unique). A backing-off record is not
        due, so it never blocks later due records. Capped at the tick budget."""
        due = [r for r in records
               if r.get("status") in ("pending", "retry_wait") and self._is_due(r, now_utc)]
        due.sort(key=lambda r: (r.get("created_at", ""), r.get("notification_id", "")))
        return due[:_DELIVERY_BUDGET_PER_TICK]

    @staticmethod
    def _is_due(rec: dict, now_utc) -> bool:
        na = rec.get("next_attempt_at")
        if na is None:
            return True
        return _age_s(now_utc, na) >= 0.0        # now >= next_attempt_at

    def _attempt(self, rec: dict, now_utc, summary: dict) -> None:
        """One delivery attempt; classify; mutate the record in place. attempts
        is incremented HERE (on the completed, about-to-be-persisted attempt) —
        never before the request — so a crash mid-request under-counts (extra
        retries) rather than premature dead-lettering."""
        envelope = {
            "notification_id": rec["notification_id"], "code": rec["code"],
            "decision": rec["decision"], "episode_key": rec["episode_key"],
            "payload": rec["payload"], "created_at": rec["created_at"],
        }
        result = self._deliver(envelope, rec["notification_id"])
        rec["attempts"] = rec.get("attempts", 0) + 1
        now_iso = now_utc.isoformat()
        if result["outcome"] == "delivered":
            rec["status"] = "delivered"
            rec["delivered_at"] = now_iso
            rec["next_attempt_at"] = None
            rec["last_error"] = None
            summary["delivered"] += 1
        elif result["outcome"] == "permanent" or rec["attempts"] >= _MAX_ATTEMPTS:
            rec["status"] = "dead_letter"
            rec["last_error"] = result.get("error")
            summary["dead_lettered"] += 1
        else:  # retry
            delay = result.get("retry_after") or _backoff_s(rec["attempts"])
            rec["next_attempt_at"] = (now_utc + timedelta(seconds=delay)).isoformat()
            rec["status"] = "retry_wait"
            rec["last_error"] = result.get("error")
            summary["retried"] += 1

    def _deliver(self, envelope: dict, notification_id: str) -> dict:
        """POST the envelope to the webhook. Returns {outcome, status?, error?,
        retry_after?}. 2xx=delivered; {408,425,429,5xx}/timeout/URLError/OSError
        =retry; other 4xx / 3xx=permanent. Response body is never read/parsed/
        logged; only the status line matters. URL is never logged."""
        # The caller has already validated the URL shape (missing/malformed ->
        # records stay pending, no attempt), so this path assumes a well-formed
        # http(s) URL; any exotic construction failure is caught below as retry
        # (bounded by _MAX_ATTEMPTS), never a silent page loss.
        try:
            data = json.dumps(envelope).encode("utf-8")
            req = urllib.request.Request(
                self._webhook_url, data=data, method="POST",
                headers={"Content-Type": "application/json",
                         "Idempotency-Key": notification_id})
            with self._opener.open(req, timeout=_WEBHOOK_TIMEOUT_S) as resp:
                status = getattr(resp, "status", None) or resp.getcode()
            if 200 <= int(status) < 300:
                return {"outcome": "delivered", "status": int(status)}
            return {"outcome": "permanent", "status": int(status),
                    "error": f"HTTP {status}"}
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            if status in _RETRYABLE_4XX or 500 <= status < 600:
                ra = _parse_retry_after(exc.headers.get("Retry-After")) if status == 429 else None
                return {"outcome": "retry", "status": status,
                        "error": f"HTTP {status}", "retry_after": ra}
            return {"outcome": "permanent", "status": status, "error": f"HTTP {status}"}
        except urllib.error.URLError as exc:
            return {"outcome": "retry", "error": type(exc.reason).__name__
                    if getattr(exc, "reason", None) else "URLError"}
        except (OSError, TimeoutError, ValueError) as exc:
            return {"outcome": "retry", "error": type(exc).__name__}

    # ── L3-C read-only operational surface (NO writes; never _load_outbox) ────

    def inspect(self, enabled: bool, limit: int) -> dict:
        """Assemble the READ-ONLY notifier status model (L3-C). Pure read: it
        reports live config/liveness + the last-tick snapshot, reads the outbox
        via the NON-MUTATING reader (never `_load_outbox`, which quarantines by
        rename), and computes fresh full-file aggregates plus a bounded
        newest-first allowlisted dead-letter projection. It writes nothing,
        renames nothing, prunes nothing, delivers nothing, and starts/stops no
        thread. A corrupt/unreadable outbox is DATA (outbox=null + generic
        outbox_error, still returned to a 200 route); only an unexpected fault
        propagates to the route's generic 500. `enabled` is the server-level
        OPS_NOTIFIER_ENABLED flag (the notifier instance does not own it)."""
        t = self._thread
        model = {
            "schema_version": 1,
            "enabled": bool(enabled),
            "running": bool(enabled) and t is not None and t.is_alive(),
            "webhook_configured": _valid_webhook_url(self._webhook_url),
            "interval_s": self.interval_s,
            # N2 hardening: hand callers a shallow SCALAR copy, never the live
            # published object, so no reachable caller can mutate self._last_tick.
            "last_tick": (dict(self._last_tick) if self._last_tick is not None else None),
            "outbox": None,
            "outbox_error": None,
            "dead_letters": [],
        }
        try:
            records = self._read_outbox_records()
        except (ValueError, OSError) as exc:
            # Known integrity / I/O degradation -> DATA (200). Never quarantine,
            # never leak path/exception text; log once per process, reset later.
            model["outbox_error"] = "outbox unreadable"
            if not self._inspect_corrupt_logged:
                self._inspect_corrupt_logged = True
                self.log.error("ops-notifier inspect: outbox unreadable (%s)",
                               type(exc).__name__)
            return model
        # Clean read (absent or valid): re-arm the once-per-process log so a
        # FUTURE corruption is reported again.
        self._inspect_corrupt_logged = False
        model["outbox"] = self._aggregate(records)
        model["dead_letters"] = self._dead_letter_projection(records, limit)
        return model

    def _read_outbox_records(self) -> list[dict]:
        """NON-MUTATING outbox read for L3-C inspection ONLY. Unlike
        `_load_outbox`, it NEVER renames/quarantines/writes: an absent file
        returns [] (absence is not corruption); corrupt JSON/shape raises
        ValueError; an I/O/permission failure raises OSError. The caller turns
        those into 200 degradation. Read-only by construction."""
        try:
            raw = self.outbox_path.read_text()
        except FileNotFoundError:
            return []
        doc = json.loads(raw)  # JSONDecodeError <: ValueError
        if not isinstance(doc, dict) or doc.get("schema_version") != OUTBOX_SCHEMA_VERSION \
                or not isinstance(doc.get("records"), list):
            raise ValueError("outbox shape/schema invalid")
        return doc["records"]

    @staticmethod
    def _aggregate(records: list) -> dict:
        """Full-file status aggregate — NEVER bounded by the listing limit.
        Every element contributes exactly one to `total`. A non-dict element,
        or a dict whose status is missing/unknown, contributes one to `unknown`
        (D-L3C-3: a single malformed-but-parseable record is DATA, never a 500).
        So counts never mislead and a corrupt record cannot crash the read."""
        counts = {"pending": 0, "retry_wait": 0, "delivered": 0, "dead_letter": 0}
        unknown = 0
        for r in records:
            st = r.get("status") if isinstance(r, dict) else None
            if st in counts:
                counts[st] += 1
            else:
                unknown += 1
        counts["unknown"] = unknown
        counts["total"] = len(records)
        return counts

    def _dead_letter_projection(self, records: list, limit: int) -> list[dict]:
        """Newest-first by PARSED chronological instant (D-L3C-2), tie-broken by
        notification_id descending (deterministic). Only dict records whose
        status is exactly 'dead_letter' enter (D-L3C-3). Malformed/missing/
        non-string created_at parses to a fixed minimum UTC instant (sorts
        behind all valid timestamps) and never raises; stored values are not
        mutated while sorting, and the projected created_at is the stored value
        unchanged (never the parsed datetime)."""
        dead = [r for r in records
                if isinstance(r, dict) and r.get("status") == "dead_letter"]
        dead = sorted(dead,
                      key=lambda r: (_parse_created_at_utc(r.get("created_at")),
                                     _sort_key_str(r.get("notification_id"))),
                      reverse=True)
        return [self._project_dead_letter(r) for r in dead[:limit]]

    @staticmethod
    def _project_dead_letter(rec: dict) -> dict:
        """Explicit allowlist — NEVER the verbatim record, NEVER verbatim
        payload, so a future record/payload field cannot leak. Every field is
        scalar-sanitized (D-L3C-3): a stored dict/list/other object in an
        allowlisted field is returned as null, never leaked by reference.
        `attempts` returns the stored value only if it is a real int (not bool),
        else 0. `payload` missing/null/non-dict -> a fully-null allowlisted
        payload. `last_error` is bounded by L3-B to 'HTTP <n>' / an exception
        TYPE name (never a URL or body); the webhook URL is never in a record."""
        payload = rec.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        att = rec.get("attempts")
        attempts = att if isinstance(att, int) and not isinstance(att, bool) else 0
        return {
            "notification_id": _scalar_or_none(rec.get("notification_id")),
            "code": _scalar_or_none(rec.get("code")),
            "episode_key": _scalar_or_none(rec.get("episode_key")),
            "decision": _scalar_or_none(rec.get("decision")),
            "created_at": _scalar_or_none(rec.get("created_at")),
            "attempts": attempts,
            "delivered_at": _scalar_or_none(rec.get("delivered_at")),
            "next_attempt_at": _scalar_or_none(rec.get("next_attempt_at")),
            "last_error": _scalar_or_none(rec.get("last_error")),
            "source_generated_at": _scalar_or_none(rec.get("source_generated_at")),
            "payload": {
                "code": _scalar_or_none(payload.get("code")),
                "humanExplanation": _scalar_or_none(payload.get("humanExplanation")),
                "first_generated_at": _scalar_or_none(payload.get("first_generated_at")),
            },
        }

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


def _prune_outbox(records: list[dict]) -> list[dict]:
    """Delivery-STATUS-aware pruning (L3-B). NEVER prunes on decision alone:
    `pending` and `retry_wait` records are always retained (including a
    RESOLVED that is still undelivered), so no page is dropped before delivery.
    Only terminal records are compacted, by count (oldest-first, list order):
    `delivered` beyond _DELIVERED_RETENTION and `dead_letter` beyond
    _DEAD_LETTER_RETENTION (dead-letters are retained for the future L3-C
    operator surface — terminal is not dispensable)."""
    delivered = [r for r in records if r.get("status") == "delivered"]
    dead = [r for r in records if r.get("status") == "dead_letter"]
    drop: set[int] = set()
    if len(delivered) > _DELIVERED_RETENTION:
        drop.update(id(r) for r in delivered[:len(delivered) - _DELIVERED_RETENTION])
    if len(dead) > _DEAD_LETTER_RETENTION:
        drop.update(id(r) for r in dead[:len(dead) - _DEAD_LETTER_RETENTION])
    if not drop:
        return records
    return [r for r in records if id(r) not in drop]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Disable automatic redirect following: a webhook 3xx surfaces as an
    HTTPError (classified permanent), never a silent cross-host follow."""
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _parse_retry_after(value: Any) -> float | None:
    """HTTP 429 Retry-After: integer delay-seconds only (smallest reliable
    form), CLAMPED to _BACKOFF_CAP_S so an untrusted header can never postpone
    delivery beyond the retry cap or overflow timedelta (D-L3B-1). Negative,
    malformed, or HTTP-date values -> None (fall back to normal backoff)."""
    try:
        secs = int(str(value).strip())
    except Exception:
        return None
    if secs < 0:
        return None
    return float(min(secs, _BACKOFF_CAP_S))


def _sort_key_str(value: Any) -> str:
    """Deterministic, crash-proof sort key: a str passes through; anything else
    (None, int, malformed) coerces to '' so a malformed notification_id never
    raises on comparison and sorts last under a descending (newest-first)
    order."""
    return value if isinstance(value, str) else ""


# A fixed minimum UTC instant for malformed/missing created_at values, so they
# sort behind every valid timestamp under descending (newest-first) order.
_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)


def _parse_created_at_utc(value: Any) -> datetime:
    """Parse an ISO-8601 created_at into a UTC-normalized aware datetime for
    CHRONOLOGICAL ordering (D-L3C-2), never lexical. Supports datetime.isoformat()
    output, a trailing 'Z' (normalized to +00:00), and explicit offsets. A
    successfully-parsed NAIVE datetime is treated as UTC. Malformed, missing,
    or non-string values return a fixed minimum UTC instant so they sort behind
    all valid timestamps. Never raises; never mutates the stored value."""
    if not isinstance(value, str):
        return _MIN_UTC
    text = value.strip()
    if text.endswith("Z") or text.endswith("z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except Exception:
        return _MIN_UTC
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _scalar_or_none(value: Any) -> Any:
    """Response-safety boundary (D-L3C-3): allow only JSON scalar leaves
    (str / int / bool / None, and FINITE float) into the projection. A stored
    dict, list, or any other object in an allowlisted field is returned as null
    rather than leaked by reference — so parseable corruption cannot smuggle
    arbitrary nested structures into the response. (bool is a subclass of int,
    so it is naturally accepted as a boolean.) A NON-finite float (NaN / +Inf /
    -Inf) — which Python's json.loads accepts from a malformed-but-parseable
    outbox but which strict JSON serialization (allow_nan=False) rejects — is
    degraded to null (D-L3C-4), so the read-only status endpoint stays
    serializable and returns HTTP 200 rather than crashing to 500."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return None


def _backoff_s(attempts: int) -> float:
    """Exponential backoff: base * 2^(attempts-1), capped."""
    n = max(1, attempts)
    return min(_BACKOFF_CAP_S, _BACKOFF_BASE_S * (2 ** (n - 1)))


def _valid_webhook_url(url: str) -> bool:
    """A usable webhook destination: non-blank and http(s). A blank or
    malformed value means delivery is skipped and records stay pending (a
    config error must never lose a page)."""
    u = (url or "").strip()
    return u.startswith("http://") or u.startswith("https://")
