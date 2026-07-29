"""Crash-safe runner state: atomic JSON writes (tmp + rename).

Holds: last processed 15m boundary, the exact input revision that boundary was
computed from (C3), hash of the previous trades frame, the intent ledger
(intent_id -> status/ticket), the trade->ticket mirror map and the daily
realised-R counter for the loss kill switch. Restart = reload state, re-run
pipeline, re-diff; idempotent intent ids make replays harmless.

Pre-C3 state files load unchanged: `last_recomputed_input_revision` is simply
absent, which readers treat as unknown (and therefore recompute). There is no
migration and no schema rewrite on load.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PENDING = "pending"
LEDGER_SENT = "sent"
LEDGER_CONFIRMED = "confirmed"
LEDGER_PARTIAL = "partial"       # OPEN filled below requested volume (LX-1 Slice 3)
LEDGER_FAILED = "failed"
LEDGER_BLOCKED = "blocked"
LEDGER_SIMULATED = "simulated"   # dry_run terminal state

# Statuses that reserve_pending must never overwrite: terminal outcomes plus the
# unresolved-but-in-flight SENT state (downgrading SENT -> PENDING could create a
# future resubmission path). PARTIAL is included: a partially-filled position
# exists at the broker and must never be re-reserved/resubmitted. This set
# governs re-reservation only; it is NOT a terminal-vs-unresolved classification.
_NON_RERESERVABLE_STATUSES = frozenset({LEDGER_SENT, LEDGER_CONFIRMED, LEDGER_PARTIAL,
                                        LEDGER_FAILED, LEDGER_BLOCKED, LEDGER_SIMULATED,
                                        "frozen"})


def frame_hash(frame) -> str:
    return hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest() if frame is not None else ""


#: Canonical durable key for per-date realised R (MS-A). Replaces the legacy
#: single `daily` bucket, which could hold exactly one day and reset itself.
REALIZED_R_KEY = "realized_r_by_date"

#: Detail keys carried across every ledger lifecycle transition. Only the
#: ORIGINATING INTENT qualifies: it is the one fact a later state cannot
#: reconstruct and must never fabricate. Broker execution facts are lifecycle
#: detail and are supplied fresh by each writer; diagnostics are deliberately
#: absent so a stale error cannot survive into a successful state.
_LINEAGE_KEYS = ("intent",)

#: Retention for the realised-R map: the most recent N day buckets.
#:
#: 40 covers a calendar month of trading plus slack for weekends, holidays and a
#: late history read, which is far more than the rail needs — it only ever reads
#: the current day. The surplus exists for operator telemetry and post-incident
#: review, not recordkeeping: this map is bounded state hygiene, and the trade
#: ledger remains the durable financial record.
_RETAIN_DAYS = 40


def _utc_today() -> str:
    """The R day. UTC calendar date, matching the executor's own definition
    (`datetime.now(timezone.utc).strftime("%Y-%m-%d")`, `executor.py:248`).

    MS-A deliberately does NOT introduce an account timezone or a configured
    reset time; those belong to the future funded-rules milestone, and inventing
    one here would silently change which day a trade is counted in.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _date_key(value) -> str:
    """Validate an explicit ISO `YYYY-MM-DD` key, defaulting to the UTC day.

    Strict on purpose: a locale-formatted or malformed date would create a
    bucket that no reader could ever address, so the loss would be silent.
    """
    if value is None:
        return _utc_today()
    if not isinstance(value, str):
        raise ValueError(f"realised-R date must be an ISO string, got {type(value).__name__}")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"realised-R date must be ISO YYYY-MM-DD: {value!r}") from exc
    return parsed.strftime("%Y-%m-%d")


def _prune(buckets: dict, *, keep: tuple = ()) -> None:
    """Keep the most recent `_RETAIN_DAYS` buckets. Deterministic: ISO keys sort
    chronologically, so the newest are unambiguous with no clock involved.

    `keep` is always retained regardless of age — the current day and any bucket
    just written, so pruning can never discard the post that triggered it."""
    if len(buckets) <= _RETAIN_DAYS:
        return
    ordered = sorted(buckets, reverse=True)
    for stale in ordered[_RETAIN_DAYS:]:
        if stale not in keep:
            buckets.pop(stale, None)


def _migrate(data: dict) -> dict:
    """Bring a state file forward to the canonical shape, in memory only.

    The new shape is written on the next NORMAL save, so merely reading an old
    file never rewrites the operator's state.

    Legacy: `{"daily": {"date": "...", "realized_r": -1.0}}` becomes one map
    entry. Nothing is invented — a legacy bucket with no date, or a malformed
    amount, is dropped rather than guessed into the current day, because
    misdating a realised loss is worse than not having it.
    """
    if not isinstance(data, dict):
        raise ValueError("runner state must be a JSON object")
    buckets = data.get(REALIZED_R_KEY)
    if not isinstance(buckets, dict):
        buckets = {}
    legacy = data.pop("daily", None)
    if isinstance(legacy, dict):
        date, amount = legacy.get("date"), legacy.get("realized_r")
        if isinstance(date, str) and isinstance(amount, (int, float)) \
                and not isinstance(amount, bool):
            try:
                key = _date_key(date)
            except ValueError:
                key = None
            # Never overwrite an existing canonical bucket: a partially migrated
            # file keeps the newer value rather than silently reverting it.
            if key is not None and key not in buckets:
                buckets[key] = float(amount)
    data[REALIZED_R_KEY] = buckets
    return data


class RunnerState:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "runner_state.json"
        self.frames_dir = Path(state_dir) / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return _migrate(json.loads(self.path.read_text()))
        return {"last_boundary": None, "last_recomputed_input_revision": None,
                "prev_frame_hash": "", "prev_frame_file": None,
                "ledger": {}, "mirror": {}, REALIZED_R_KEY: {},
                "updated_at": None}

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    # ── frames ───────────────────────────────────────────────────────────────
    def store_frame(self, frame, boundary: str, input_revision: str) -> None:
        """Record the evaluated frame, its boundary AND the exact input revision
        that produced it — all in memory; the caller's single atomic ``save()``
        commits them together with any reserved PENDING intents.

        ``input_revision`` is REQUIRED (C3): a boundary can never advance without
        the revision it was computed from, so durable state can never hold a new
        boundary paired with a stale revision."""
        f = self.frames_dir / "prev_trades.csv"
        frame.to_csv(f, index=False)
        self.data["prev_frame_file"] = str(f)
        self.data["prev_frame_hash"] = frame_hash(frame)
        self.data["last_boundary"] = boundary
        self.data["last_recomputed_input_revision"] = input_revision

    def load_prev_frame(self):
        import pandas as pd
        f = self.data.get("prev_frame_file")
        if not f or not Path(f).exists():
            return None
        return pd.read_csv(f, dtype=str, keep_default_na=False)

    # ── ledger / mirror ──────────────────────────────────────────────────────
    def ledger_status(self, intent_id: str) -> str | None:
        entry = self.data["ledger"].get(intent_id)
        return entry["status"] if entry else None

    def ledger_set(self, intent_id: str, status: str, detail: dict | None = None) -> None:
        """Record a lifecycle state, CARRYING IMMUTABLE LINEAGE FORWARD.

        MS-A defect this repairs: this used to replace the detail dict outright.
        `_execute` writes the originating intent at SENT; `_record_open_result`
        then overwrote it with broker execution facts, so the ORIGINAL PLANNED
        STOP — the denominator of realised R — was destroyed at the moment the
        trade was confirmed. It is unrecoverable afterwards: the engine frame no
        longer holds a closed trade, and the broker's stop is gone once the
        position closes and may anyway have been moved to breakeven, which the
        canonical formula deliberately refuses to use.

        Three field classes, applied here rather than scattered across call
        sites:
          * IMMUTABLE LINEAGE (`_LINEAGE_KEYS`) — carried forward from the prior
            record when the new detail does not supply it. Never invented.
          * lifecycle-specific detail — everything the caller passes, which wins
            over a carried value of the same name.
          * replaceable telemetry (diagnostics, error text) — deliberately NOT
            carried, so a stale failure reason cannot survive into a later
            successful state.
        """
        prior = (self.data["ledger"].get(intent_id) or {}).get("detail") or {}
        carried = {k: prior[k] for k in _LINEAGE_KEYS if k in prior}
        merged = {**carried, **(detail or {})}
        self.data["ledger"][intent_id] = {"status": status, "detail": merged,
                                          "at": datetime.now(timezone.utc).isoformat()}

    def planned_risk_facts(self, intent_id: str) -> dict | None:
        """The originating intent for an intent id, or None when unavailable.

        Legacy records written before MS-A lost their intent at confirmation.
        They return None — explicitly incomplete — rather than a fabricated
        stop. Milestone B must treat None as "cannot compute R", never as zero.
        """
        detail = (self.data["ledger"].get(intent_id) or {}).get("detail") or {}
        intent = detail.get("intent")
        return dict(intent) if isinstance(intent, dict) else None

    # ── durable pending-intent reservation (LR-1) ────────────────────────────
    def reserve_pending(self, intent) -> None:
        """Record a generated intent as PENDING with a fully reconstructable
        payload in the existing ledger ``detail``. In-memory only; the caller's
        single atomic ``save()`` commits it together with the boundary. Never
        overwrites a terminal record (idempotent re-reservation)."""
        existing = self.data["ledger"].get(intent.intent_id)
        if existing and existing.get("status") in _NON_RERESERVABLE_STATUSES:
            return
        self.ledger_set(intent.intent_id, LEDGER_PENDING, {"intent": intent.to_dict()})

    def pending_intents(self) -> list:
        """(intent_id, detail) for each PENDING ledger record, in stable insertion
        order (JSON preserves object order across reload)."""
        return [(iid, entry.get("detail", {}))
                for iid, entry in self.data["ledger"].items()
                if entry.get("status") == LEDGER_PENDING]

    def sent_intents(self) -> list:
        """(intent_id, detail) for each unresolved SENT ledger record (restart
        reconciliation of the live submission window)."""
        return [(iid, entry.get("detail", {}))
                for iid, entry in self.data["ledger"].items()
                if entry.get("status") == LEDGER_SENT]

    def mirror_ticket(self, trade_id: str) -> int | None:
        return self.data["mirror"].get(trade_id)

    def mirror_set(self, trade_id: str, ticket: int | None) -> None:
        if ticket is None:
            self.data["mirror"].pop(trade_id, None)
        else:
            self.data["mirror"][trade_id] = ticket

    def open_mirror_count(self) -> int:
        return len(self.data["mirror"])

    # ── daily loss tracking ──────────────────────────────────────────────────
    def add_realized_r(self, r: float, on_date: str | None = None) -> float:
        """Post a realised-R delta to an EXPLICIT day and return that day's total.

        MS-A defect this repairs: the old single-bucket accumulator reset itself
        whenever the date key differed, so posting a delayed prior-day close
        wiped the current day's accumulation AND re-dated the bucket — after
        which the rail read 0.0 for today and silently re-disarmed. Any history
        read spanning midnight triggered it.

        `on_date` defaults to the current UTC day. It must be an ISO `YYYY-MM-DD`
        key; a locale-formatted or malformed date is rejected rather than
        creating a bucket nothing can ever read back.
        """
        key = _date_key(on_date)
        amount = float(r)
        if amount != amount or amount in (float("inf"), float("-inf")):
            raise ValueError("realised R must be a finite number")
        buckets = self._buckets()
        buckets[key] = round(float(buckets.get(key, 0.0)) + amount, 8)
        # Prune AFTER posting and never the day just written, so a historical
        # post can never be discarded by the same call that recorded it.
        _prune(buckets, keep=(key, _utc_today()))
        return buckets[key]

    def daily_realized_r(self, on_date: str | None = None) -> float:
        """Realised R for one day. Unknown days read 0.0, exactly as before."""
        return float(self._buckets().get(_date_key(on_date), 0.0))

    def realized_r_by_date(self) -> dict:
        """A copy of every retained bucket, for telemetry. State hygiene only —
        this is a bounded operational window, NOT a financial record of account
        history."""
        return dict(self._buckets())

    def _buckets(self) -> dict:
        return self.data.setdefault(REALIZED_R_KEY, {})
