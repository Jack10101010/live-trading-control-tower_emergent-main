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
from datetime import datetime, timedelta, timezone
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

#: B3 — deal ids already accounted, mapped to the UTC day they were posted to.
#: A MAP rather than a list: the day is what B4's retention rule needs to bound
#: this structure, and storing it now avoids a migration later for no extra cost.
ACCOUNTED_KEY = "accounted_deal_ids"

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

#: B4 — ledger statuses that may EVER be pruned. `pending` and `sent` are in
#: flight; `partial` may still have an open position. Being terminal is only the
#: first of five conditions.
_PRUNABLE_STATUSES = frozenset({LEDGER_CONFIRMED, LEDGER_FAILED, LEDGER_BLOCKED,
                                LEDGER_SIMULATED})


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


def _tickets_of(detail: dict) -> set:
    """Broker tickets recorded in one ledger detail, via the canonical keys.

    Imported from the executor so there is exactly one definition of which
    detail keys carry a ticket — the same single source the accounting engine
    resolves through.
    """
    from live.executor import Executor
    return {str(detail[k]) for k in Executor._TICKET_KEYS
            if detail.get(k) not in (None, "", 0)}


def _accounted_entry(value) -> dict:
    """Normalize one accounted-map value to `{"at": iso, "position": id|None}`.

    B3 stored the posted DAY as a bare string. B4 needs the deal's EXECUTION
    timestamp (to bound retention by the lookback window) and its position (to
    prove every close for a ledger entry was accounted). A legacy string is read
    as the LAST instant of that day — deliberately the most conservative
    interpretation, so a migrated record is retained longer rather than pruned
    early. Retention may never cost correctness.
    """
    if isinstance(value, dict):
        return {"at": value.get("at"), "position": value.get("position")}
    if isinstance(value, str) and value:
        return {"at": f"{value}T23:59:59+00:00", "position": None}
    return {"at": None, "position": None}


def _parse_instant(value):
    """A timezone-aware datetime, or None. Never raises on malformed input —
    an unreadable timestamp means "do not prune this", not "prune it"."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


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
    accounted = data.get(ACCOUNTED_KEY)
    data[ACCOUNTED_KEY] = accounted if isinstance(accounted, dict) else {}
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
                ACCOUNTED_KEY: {}, "updated_at": None}

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

    # ── B3: confirmed-close accounting ───────────────────────────────────────

    def _accounted(self) -> dict:
        return self.data.setdefault(ACCOUNTED_KEY, {})

    def is_accounted(self, deal_id: str) -> bool:
        """Whether this broker deal has already been accounted.

        Deal id is THE idempotency identity — a stable broker-assigned fact, not
        a timestamp and not a locally generated key. There is deliberately no
        second mechanism: overlapping history windows, a replayed cycle and a
        restart all deduplicate through this one check.
        """
        return str(deal_id) in self._accounted()

    def accounted_deal_ids(self) -> dict:
        """A copy of the accounted map, for tests and future retention work."""
        return dict(self._accounted())

    def prune(self, *, now=None, lookback_hours: float = 48.0,
              open_tickets=(), pending_positions=()) -> dict:
        """B4 — bounded retention. Housekeeping ONLY; correctness never depends
        on it, and when in doubt data is retained.

        Runs AFTER a successful accounting commit, never before. The ordering is
        the safety property: the accounted map read here IS the durable record of
        committed accounting, so an entry can only be judged prunable on evidence
        that has already survived a save. Pruning before committing could discard
        a ledger entry whose accounting was then lost.

        Idempotent: it removes only what is already ineligible, so running it
        repeatedly produces identical state. Returns counts; performs ONE save
        and only when something actually changed.
        """
        clock = now or datetime.now(timezone.utc)
        cutoff = clock - timedelta(hours=float(lookback_hours))
        removed_deals = self._prune_accounted(cutoff)
        removed_ledger = self._prune_ledger(
            clock=clock, lookback_hours=float(lookback_hours),
            open_tickets={str(t) for t in open_tickets},
            pending_positions={str(p) for p in pending_positions})
        if removed_deals or removed_ledger:
            self.save()
        return {"accounted_removed": removed_deals,
                "ledger_removed": removed_ledger}

    def _prune_accounted(self, cutoff) -> int:
        """Drop idempotency records for deals the reader can no longer return.

        THE PROOF, not a heuristic: the history reader never requests deals older
        than LOOKBACK, so a deal executed before `now - LOOKBACK` can never be
        returned again; it can therefore never be re-accounted, and its
        idempotency record is no longer required. A record whose timestamp is
        missing or unreadable is KEPT — an unknown age is not an expired one.
        """
        accounted = self._accounted()
        expired = []
        for deal_id, raw in accounted.items():
            executed_at = _parse_instant(_accounted_entry(raw)["at"])
            if executed_at is not None and executed_at < cutoff:
                expired.append(deal_id)
        for deal_id in expired:
            accounted.pop(deal_id, None)
        return len(expired)

    def _prune_ledger(self, *, clock, lookback_hours: float,
                      open_tickets: set, pending_positions: set) -> int:
        """Drop ledger entries that can no longer participate in accounting.

        All five conditions must hold. Any doubt retains the entry.
        """
        age_cutoff = clock - timedelta(hours=2.0 * lookback_hours)
        accounted_positions = {
            entry["position"]
            for entry in (_accounted_entry(v) for v in self._accounted().values())
            if entry["position"]}
        ledger = self.data["ledger"]
        removable = []
        for intent_id, record in ledger.items():
            if not isinstance(record, dict):
                continue
            # 1) terminal only — pending/sent are in flight, partial may be open.
            if record.get("status") not in _PRUNABLE_STATUSES:
                continue
            detail = record.get("detail") or {}
            tickets = _tickets_of(detail)
            if not tickets:
                continue                    # no lineage to reason about: keep
            # 2) the position must not still be open.
            if tickets & open_tickets:
                continue
            # 3+4) at least one of its closes is in the accounted map — which is
            #      itself the durable record of a COMMITTED transaction, so
            #      "accounted" and "committed" are the same evidence here.
            if not (tickets & accounted_positions):
                continue
            # 3b) nothing observed-but-unaccounted may remain for this position.
            if tickets & pending_positions:
                continue
            # 5) older than 2x LOOKBACK, so a late close deal has had ample time
            #    to arrive and be accounted before its lineage is discarded.
            written_at = _parse_instant(record.get("at"))
            if written_at is None or written_at >= age_cutoff:
                continue
            removable.append(intent_id)
        for intent_id in removable:
            ledger.pop(intent_id, None)
        return len(removable)

    def commit_accounting(self, postings) -> int:
        """THE accounting transaction. One event, one durable state replacement.

        `postings` is an iterable of
        `(deal_id, utc_date, realised_r, executed_at_iso, position_id)`.
        Already accounted deals are skipped. Returns the number newly committed.

        B4 note: the stored value gained the deal's EXECUTION instant and its
        position id. Atomicity, ordering and rollback are untouched — only the
        recorded facts widened, because retention needs to know when a deal can
        no longer be returned by a bounded read and which ledger entry it closes.

        WHY BOTH MUTATIONS MUST SHARE ONE SAVE
            If the bucket and the accounted id were persisted separately, a crash
            between them would either double-count a loss (bucket saved, id not —
            the deal is re-read next cycle and posted again) or lose a confirmed
            loss permanently (id saved, bucket not — the deal is skipped forever).
            Both are unacceptable, and RunnerState's whole-file replace makes the
            combined write free, so there is no reason to split them.

        WHY IN-MEMORY STATE IS ROLLED BACK ON FAILURE
            `self.data` is shared with every other writer in the process. If a
            save failed and the mutation were left in memory, the NEXT unrelated
            `save()` would silently persist this accounting anyway — outside any
            transaction, and possibly after the caller had already retried it.
            Restoring the prior values keeps "the save failed" and "nothing was
            committed" the same fact.
        """
        buckets_before = dict(self._buckets())
        accounted_before = dict(self._accounted())
        committed = 0
        try:
            for deal_id, on_date, realised_r, executed_at, position_id in postings:
                key = str(deal_id)
                if key in self._accounted():
                    continue                    # idempotent: already counted
                self.add_realized_r(realised_r, on_date)
                self._accounted()[key] = {
                    "at": executed_at,
                    "position": (str(position_id) if position_id else None)}
                committed += 1
            if committed:
                self.save()
            return committed
        except Exception:
            self.data[REALIZED_R_KEY] = buckets_before
            self.data[ACCOUNTED_KEY] = accounted_before
            raise
