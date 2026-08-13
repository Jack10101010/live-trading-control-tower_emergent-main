"""LIVE-4D — the durable RECOMMENDATION store.

Append-only events are the source of truth; `recommendations` holds a snapshot
for cheap reads that can be discarded and rebuilt deterministically. Decisions
live in their own append-only table and are never overwritten.

OWNERSHIP
  * Owns RECOMMENDATIONS and DECISIONS only, in its own database file with its
    own schema version.
  * Never touches execution, scenario, ledger or broker tables. It holds no
    adapter, issues no command, and authorizes nothing.

RULES
  * `recommendation_events` and `recommendation_decisions` are APPEND-ONLY:
    no update or delete surface, and no generic mutable CRUD.
  * Writes are atomic. LIVE-4E: a DECISION, its event and the snapshot commit
    in ONE transaction — before LIVE-4E the decision row committed separately,
    so a crash in between left a committed decision that history and the
    rebuild path could not see.
  * Writes are IDEMPOTENT on `(recommendation_id, sequence)`; replaying the
    same decision or event appends nothing. LIVE-4E: a DIFFERENT decision at
    the same sequence is a CONFLICT, not a silent no-op — before LIVE-4E a
    losing concurrent writer was told it had succeeded.
  * LIVE-4E: `version` is the sequence of the last applied event. Operator
    writes take it as an optimistic-concurrency precondition under
    BEGIN IMMEDIATE, so exactly one of two racing decisions can win.
  * Nothing is created automatically — every call records a caller-supplied
    fact. There is no strategy logic anywhere in this module.
  * An unsupported newer schema FAILS CLOSED.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path
from typing import Any

import recommendation_domain as rd

#: How long a writer waits for the write lock before reporting contention,
#: rather than failing instantly under a concurrent decision.
_WRITE_LOCK_TIMEOUT_S = 5.0

SCHEMA_VERSION = 2
#: Schema history — every step is additive and fails closed on anything newer.
#:   1 -> 2 (LIVE-4E): `recommendations.version`; decision `note`,
#:   `against_version`, `correlation_id`, `identity_assurance`; and a UNIQUE
#:   index on (recommendation_id, sequence) for decisions so two racing
#:   operators cannot both write at the same ordinal.


class RecommendationStoreError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class RecommendationStore:
    """Durable recommendation state. One instance per database file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        try:
            with self._conn() as conn:
                self._init_schema(conn)
        except sqlite3.Error as exc:
            raise RecommendationStoreError("store_unavailable",
                                           type(exc).__name__) from exc

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS recommendation_meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM recommendation_meta "
                           "WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO recommendation_meta (key, value) "
                         "VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise RecommendationStoreError(
                "unsupported_schema_version",
                f"recommendation schema {row['value']} > supported {SCHEMA_VERSION}")
        else:
            found = int(row["value"])
            if found not in (1, SCHEMA_VERSION):
                raise RecommendationStoreError(
                    "unsupported_schema_version",
                    f"recommendation schema {found} cannot be migrated")
            if found < SCHEMA_VERSION:
                self._migrate(conn, found)
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recommendations (
                recommendation_id TEXT PRIMARY KEY,
                scenario_id TEXT NOT NULL, instrument TEXT NOT NULL,
                direction TEXT NOT NULL, source TEXT NOT NULL, status TEXT NOT NULL,
                node_id TEXT, account_fingerprint TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                decided_at TEXT, withdrawn_at TEXT, expired_at TEXT, expiry_at TEXT,
                superseded_by TEXT, supersedes TEXT,
                linked_intent_ids TEXT NOT NULL DEFAULT '[]',
                source_reference TEXT, provenance TEXT NOT NULL DEFAULT 'operator',
                terms_json TEXT NOT NULL DEFAULT '{}',
                version INTEGER NOT NULL DEFAULT 0
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                recommendation_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL, recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                provenance TEXT NOT NULL DEFAULT 'operator',
                schema_version TEXT NOT NULL,
                UNIQUE (recommendation_id, sequence)
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recommendation_decisions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id TEXT NOT NULL UNIQUE,
                recommendation_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                decision_type TEXT NOT NULL,
                actor_type TEXT NOT NULL, actor_id TEXT,
                occurred_at TEXT NOT NULL, reason TEXT,
                authorization_reference TEXT, execution_mode TEXT,
                provenance TEXT NOT NULL DEFAULT 'operator',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                note TEXT, against_version INTEGER, correlation_id TEXT,
                identity_assurance TEXT NOT NULL DEFAULT 'unknown'
            )""")
        # LIVE-4E: the write-time guarantee that one ordinal holds one decision.
        # `rd.conflicting_decisions()` remains as a defensive READ-side check for
        # records assembled outside this store.
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS "
                     "idx_recommendation_decision_sequence "
                     "ON recommendation_decisions (recommendation_id, sequence)")
        conn.commit()

    def _migrate(self, conn: sqlite3.Connection, found: int) -> None:
        """Additive, fail-closed migration. Each ALTER is guarded by a probe of
        the live table, so a partially-migrated database converges."""
        existing = {r["name"] for r in conn.execute("PRAGMA table_info(recommendations)")}
        if "version" not in existing:
            conn.execute("ALTER TABLE recommendations ADD COLUMN "
                         "version INTEGER NOT NULL DEFAULT 0")
        # Backfill from the append-only log, which is the real authority. This
        # runs for EVERY v1 database, not only when the ALTER was needed: a
        # partially-migrated file already has the column but no values, and
        # leaving those at 0 would make every optimistic write conflict forever.
        conn.execute(
            """UPDATE recommendations SET version = COALESCE((
                   SELECT MAX(sequence) FROM recommendation_events e
                   WHERE e.recommendation_id = recommendations.recommendation_id
               ), 0)""")
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(recommendation_decisions)")}
        for name, ddl in (("note", "note TEXT"),
                          ("against_version", "against_version INTEGER"),
                          ("correlation_id", "correlation_id TEXT"),
                          ("identity_assurance",
                           "identity_assurance TEXT NOT NULL DEFAULT 'unknown'")):
            if name not in cols:
                conn.execute(f"ALTER TABLE recommendation_decisions ADD COLUMN {ddl}")
        conn.execute("UPDATE recommendation_meta SET value=? WHERE key='schema_version'",
                     (str(SCHEMA_VERSION),))
        conn.commit()

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM recommendation_meta "
                               "WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0

    # ── serialization ────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_recommendation(row: Any) -> rd.Recommendation:
        try:
            terms_raw = json.loads(row["terms_json"])
            return rd.Recommendation(
                recommendation_id=row["recommendation_id"],
                scenario_id=row["scenario_id"], instrument=row["instrument"],
                direction=row["direction"], source=row["source"],
                status=row["status"], node_id=row["node_id"],
                account_fingerprint=row["account_fingerprint"],
                terms=rd._terms_from(terms_raw),
                created_at=row["created_at"], updated_at=row["updated_at"],
                decided_at=row["decided_at"], withdrawn_at=row["withdrawn_at"],
                expired_at=row["expired_at"], expiry_at=row["expiry_at"],
                superseded_by=row["superseded_by"], supersedes=row["supersedes"],
                linked_intent_ids=tuple(json.loads(row["linked_intent_ids"])),
                source_reference=row["source_reference"],
                provenance=row["provenance"],
                version=int(row["version"] or 0))
        except (ValueError, TypeError, KeyError, rd.RecommendationError) as exc:
            raise RecommendationStoreError("corrupt_recommendation_row",
                                           type(exc).__name__) from exc

    @staticmethod
    def _row_to_event(row: Any) -> rd.RecommendationEvent:
        try:
            return rd.RecommendationEvent(
                event_id=row["event_id"], recommendation_id=row["recommendation_id"],
                sequence=row["sequence"], event_type=row["event_type"],
                occurred_at=row["occurred_at"], recorded_at=row["recorded_at"],
                payload=json.loads(row["payload_json"]),
                provenance=row["provenance"], schema_version=row["schema_version"])
        except (ValueError, TypeError, KeyError, rd.RecommendationError) as exc:
            raise RecommendationStoreError("corrupt_recommendation_event",
                                           type(exc).__name__) from exc

    @staticmethod
    def _row_to_decision(row: Any) -> rd.RecommendationDecision:
        try:
            return rd.RecommendationDecision(
                decision_id=row["decision_id"],
                recommendation_id=row["recommendation_id"],
                decision_type=row["decision_type"], actor_type=row["actor_type"],
                actor_id=row["actor_id"], occurred_at=row["occurred_at"],
                sequence=row["sequence"], reason=row["reason"],
                authorization_reference=row["authorization_reference"],
                execution_mode=row["execution_mode"], provenance=row["provenance"],
                metadata=json.loads(row["metadata_json"]),
                note=row["note"], against_version=row["against_version"],
                correlation_id=row["correlation_id"],
                identity_assurance=(row["identity_assurance"]
                                    or rd.IDENTITY_UNKNOWN))
        except (ValueError, TypeError, KeyError, rd.RecommendationError) as exc:
            raise RecommendationStoreError("corrupt_recommendation_decision",
                                           type(exc).__name__) from exc

    def _terms_payload(self, recommendation: rd.Recommendation) -> str:
        t = recommendation.terms
        return json.dumps({**t.execution.as_dict(), **t.risk.as_dict(),
                           "rationale": t.rationale, "confidence": t.confidence,
                           "tags": list(t.tags), "metadata": dict(t.metadata)},
                          sort_keys=True)

    def _write(self, conn, recommendation: rd.Recommendation) -> None:
        conn.execute(
            """INSERT INTO recommendations (recommendation_id, scenario_id,
                instrument, direction, source, status, node_id,
                account_fingerprint, created_at, updated_at, decided_at,
                withdrawn_at, expired_at, expiry_at, superseded_by, supersedes,
                linked_intent_ids, source_reference, provenance, terms_json,
                version)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(recommendation_id) DO UPDATE SET
                 status=excluded.status, updated_at=excluded.updated_at,
                 decided_at=excluded.decided_at, withdrawn_at=excluded.withdrawn_at,
                 expired_at=excluded.expired_at,
                 superseded_by=excluded.superseded_by,
                 supersedes=excluded.supersedes,
                 linked_intent_ids=excluded.linked_intent_ids,
                 version=excluded.version""",
            (recommendation.recommendation_id, recommendation.scenario_id,
             recommendation.instrument, recommendation.direction,
             recommendation.source, recommendation.status, recommendation.node_id,
             recommendation.account_fingerprint, recommendation.created_at,
             recommendation.updated_at, recommendation.decided_at,
             recommendation.withdrawn_at, recommendation.expired_at,
             recommendation.expiry_at, recommendation.superseded_by,
             recommendation.supersedes,
             json.dumps(list(recommendation.linked_intent_ids)),
             recommendation.source_reference, recommendation.provenance,
             self._terms_payload(recommendation), recommendation.version))

    def _write_conn(self) -> sqlite3.Connection:
        """A connection with MANUAL transaction control (`isolation_level=None`).

        LIVE-4E: the operator decision path issues `BEGIN IMMEDIATE` so the
        write lock is taken before the version is read. Without manual control
        pysqlite would manage transactions implicitly and the compare-and-set
        would not be serialized against a concurrent writer."""
        conn = sqlite3.connect(self._path, timeout=_WRITE_LOCK_TIMEOUT_S,
                               isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def current_version(self, recommendation_id: str) -> int:
        """The sequence of the last applied event — the value an operator must
        write against. 0 when the recommendation does not exist."""
        with self._conn() as conn:
            return self._version(conn, recommendation_id)

    @staticmethod
    def _version(conn, recommendation_id: str) -> int:
        row = conn.execute("SELECT MAX(sequence) AS s FROM recommendation_events "
                           "WHERE recommendation_id=?", (recommendation_id,)).fetchone()
        return int(row["s"] or 0)

    def _next_sequence(self, conn, recommendation_id: str) -> int:
        row = conn.execute("SELECT MAX(sequence) AS s FROM recommendation_events "
                           "WHERE recommendation_id=?", (recommendation_id,)).fetchone()
        return int(row["s"] or 0) + 1

    @staticmethod
    def _insert_event(conn, *, recommendation_id: str, sequence: int,
                      event_type: str, occurred_at: str, payload: dict,
                      provenance: str) -> None:
        """Validate then insert ONE event. The caller owns the transaction."""
        event_id = rd.new_event_id(recommendation_id, sequence)
        rd.RecommendationEvent(                               # validate first
            event_id=event_id, recommendation_id=recommendation_id,
            sequence=sequence, event_type=event_type, occurred_at=occurred_at,
            recorded_at=occurred_at, payload=payload, provenance=provenance)
        conn.execute(
            """INSERT INTO recommendation_events (event_id,
                recommendation_id, sequence, event_type, occurred_at,
                recorded_at, payload_json, provenance, schema_version)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (event_id, recommendation_id, sequence, event_type, occurred_at,
             occurred_at, json.dumps(payload), provenance, rd.SCHEMA_VERSION))

    def _append(self, *, recommendation: rd.Recommendation, event_type: str,
                occurred_at: str, payload: dict, provenance: str,
                decision: rd.RecommendationDecision | None = None
                ) -> rd.Recommendation:
        """Append one event (and OPTIONALLY the decision that caused it) plus the
        snapshot, in ONE transaction.

        LIVE-4E: passing `decision` here is what makes a decision atomic. When it
        was inserted by a separate transaction, a failure in between committed a
        decision that no event recorded and no rebuild could see."""
        rid = recommendation.recommendation_id
        try:
            with self._conn() as conn:
                sequence = self._next_sequence(conn, rid)
                versioned = replace(recommendation, version=sequence)
                conn.execute("BEGIN")
                if decision is not None:
                    self._insert_decision(conn, decision)
                self._insert_event(conn, recommendation_id=rid, sequence=sequence,
                                   event_type=event_type, occurred_at=occurred_at,
                                   payload=payload, provenance=provenance)
                self._write(conn, versioned)
                conn.commit()
        except sqlite3.IntegrityError:
            return self.get_recommendation(rid) or recommendation   # idempotent
        except sqlite3.Error as exc:
            raise RecommendationStoreError("store_write_failed",
                                           type(exc).__name__) from exc
        return versioned

    @staticmethod
    def _insert_decision(conn, decision: rd.RecommendationDecision) -> None:
        conn.execute(
            """INSERT INTO recommendation_decisions (decision_id,
                recommendation_id, sequence, decision_type, actor_type,
                actor_id, occurred_at, reason, authorization_reference,
                execution_mode, provenance, metadata_json, note,
                against_version, correlation_id, identity_assurance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (decision.decision_id, decision.recommendation_id, decision.sequence,
             decision.decision_type, decision.actor_type, decision.actor_id,
             decision.occurred_at, decision.reason,
             decision.authorization_reference, decision.execution_mode,
             decision.provenance, json.dumps(decision.metadata), decision.note,
             decision.against_version, decision.correlation_id,
             decision.identity_assurance))

    # ── writes (explicit only) ───────────────────────────────────────────────
    def create_recommendation(self, recommendation: rd.Recommendation, *,
                              reason: str = "created") -> rd.Recommendation:
        """Persist a NEW recommendation plus its CREATED event, atomically.
        Idempotent: re-creating the same id returns the stored recommendation."""
        existing = self.get_recommendation(recommendation.recommendation_id)
        if existing is not None:
            return existing
        snapshot = recommendation.as_dict()
        # The rebuild path needs the RAW fingerprint, not the masked view.
        snapshot["accountFingerprintRaw"] = recommendation.account_fingerprint
        return self._append(recommendation=recommendation,
                            event_type=rd.RecommendationEventType.CREATED,
                            occurred_at=recommendation.created_at,
                            payload={"recommendation": snapshot, "reason": reason},
                            provenance=recommendation.provenance)

    def record_decision(self, recommendation_id: str, *,
                        decision: rd.RecommendationDecision,
                        to_status: str | None, at: str) -> rd.Recommendation:
        """Append ONE immutable decision and apply its lifecycle effect.

        Idempotent on the decision id; an illegal transition writes NOTHING.

        LIVE-4E: the decision, its event and the snapshot now commit in ONE
        transaction (see `_append`), and a DIFFERENT decision replaying at an
        occupied sequence raises `decision_sequence_conflict` instead of being
        swallowed as an idempotent no-op."""
        current = self.get_recommendation(recommendation_id)
        if current is None:
            raise RecommendationStoreError("recommendation_not_found",
                                           recommendation_id)
        existing = self.get_decision(decision.decision_id)
        if existing is not None:
            return current                                    # true replay
        self._assert_sequence_free(recommendation_id, decision)
        updated = current
        if to_status is not None and to_status != current.status:
            updated = rd.transition(current, to_status, at=at,
                                    reason=decision.reason or "decision")
        event_type = (rd.STATUS_EVENT.get(to_status)
                      if to_status else rd.RecommendationEventType.DECISION_RECORDED)
        return self._append(
            recommendation=updated,
            event_type=event_type or rd.RecommendationEventType.DECISION_RECORDED,
            occurred_at=at,
            payload={"decisionId": decision.decision_id,
                     "decisionType": decision.decision_type,
                     "actorType": decision.actor_type,
                     "status": to_status, "reason": decision.reason},
            provenance=decision.provenance,
            decision=decision)

    def _assert_sequence_free(self, recommendation_id: str,
                              decision: rd.RecommendationDecision) -> None:
        """Refuse a CONTRADICTORY decision at an already-occupied ordinal.

        Two operators deciding concurrently is the case this exists for: before
        LIVE-4E the loser's write hit the decision-id unique constraint, was
        treated as a replay, and the loser was told it had succeeded — while its
        decision left no trace at all."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT decision_id, decision_type FROM recommendation_decisions "
                "WHERE recommendation_id=? AND sequence=?",
                (recommendation_id, decision.sequence)).fetchone()
        if row is not None and row["decision_id"] != decision.decision_id:
            raise RecommendationStoreError(
                "decision_sequence_conflict",
                f"sequence {decision.sequence} already holds "
                f"{row['decision_type']}")

    def find_decision_by_idempotency_key(self, recommendation_id: str,
                                         key: str) -> rd.RecommendationDecision | None:
        """The decision a previous request with this key already recorded.

        A retry after a SUCCESSFUL commit must replay that result rather than be
        judged against the new state — by then the proposal is terminal, and a
        naive re-check would tell the caller its own successful decision was
        illegal."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM recommendation_decisions WHERE recommendation_id=? "
                "AND json_extract(metadata_json, '$.idempotencyKey')=?",
                (recommendation_id, key)).fetchone()
        return self._row_to_decision(row) if row else None

    def get_decision(self, decision_id: str) -> rd.RecommendationDecision | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM recommendation_decisions "
                               "WHERE decision_id=?", (decision_id,)).fetchone()
        return self._row_to_decision(row) if row else None

    # ── LIVE-4E: the atomic operator decision path ───────────────────────────
    def record_operator_decision(self, recommendation_id: str, *,
                                 decision_type: str, to_status: str,
                                 actor_type: str, actor_id: str | None,
                                 reason: str, at: str,
                                 expected_version: int | None = None,
                                 note: str | None = None,
                                 correlation_id: str | None = None,
                                 identity_assurance: str = rd.IDENTITY_UNKNOWN,
                                 execution_mode: str | None = None,
                                 idempotency_key: str | None = None,
                                 ) -> tuple[rd.Recommendation, rd.RecommendationDecision]:
        """Record ONE operator decision under optimistic concurrency, atomically.

        The whole read-check-write runs inside `BEGIN IMMEDIATE`, so the write
        lock is held BEFORE the version is read. Two operators racing therefore
        serialize: the first commits, the second re-reads a version that no
        longer matches its precondition and receives `version_conflict`. Exactly
        one decision wins, the state stays valid, and BOTH outcomes are truthful
        — the loser is never told it succeeded.

        `idempotency_key` makes a retried request safe: the same key replaying
        against the same recommendation returns the ORIGINAL decision instead of
        recording a second one.
        """
        conn = self._write_conn()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM recommendations WHERE "
                               "recommendation_id=?", (recommendation_id,)).fetchone()
            if row is None:
                raise RecommendationStoreError("recommendation_not_found",
                                               recommendation_id)
            current = self._row_to_recommendation(row)
            if idempotency_key:
                prior = conn.execute(
                    "SELECT * FROM recommendation_decisions WHERE "
                    "recommendation_id=? AND json_extract(metadata_json, "
                    "'$.idempotencyKey')=?",
                    (recommendation_id, idempotency_key)).fetchone()
                if prior is not None:
                    replayed = self._row_to_decision(prior)
                    if replayed.decision_type != decision_type:
                        raise RecommendationStoreError(
                            "idempotency_key_reused",
                            f"key already recorded {replayed.decision_type}")
                    conn.execute("ROLLBACK")
                    return current, replayed
            version = self._version(conn, recommendation_id)
            if expected_version is not None and int(expected_version) != version:
                raise RecommendationStoreError(
                    "version_conflict",
                    f"expected version {expected_version}, stored {version}")
            if not rd.operator_can_transition(current.status, to_status):
                raise RecommendationStoreError(
                    "invalid_transition", f"{current.status} -> {to_status}")

            sequence = version + 1
            decision = rd.RecommendationDecision(
                decision_id=rd.new_decision_id(recommendation_id, sequence),
                recommendation_id=recommendation_id, decision_type=decision_type,
                actor_type=actor_type, actor_id=actor_id, occurred_at=at,
                sequence=sequence, reason=reason, execution_mode=execution_mode,
                provenance=actor_type.lower(), note=note,
                against_version=version, correlation_id=correlation_id,
                identity_assurance=identity_assurance,
                metadata={"idempotencyKey": idempotency_key} if idempotency_key else {})
            updated = replace(
                rd.transition(current, to_status, at=at, reason=reason),
                version=sequence)
            self._insert_decision(conn, decision)
            self._insert_event(
                conn, recommendation_id=recommendation_id, sequence=sequence,
                event_type=(rd.STATUS_EVENT.get(to_status)
                            or rd.RecommendationEventType.DECISION_RECORDED),
                occurred_at=at,
                payload={"decisionId": decision.decision_id,
                         "decisionType": decision_type, "actorType": actor_type,
                         "status": to_status, "reason": reason, "note": note,
                         "againstVersion": version,
                         "identityAssurance": identity_assurance,
                         "correlationId": correlation_id},
                provenance=decision.provenance)
            self._write(conn, updated)
            conn.execute("COMMIT")
            return updated, decision
        except (RecommendationStoreError, rd.RecommendationError):
            conn.execute("ROLLBACK")
            raise
        except sqlite3.IntegrityError as exc:
            conn.execute("ROLLBACK")
            # The unique index is the backstop if two writers ever reach here.
            raise RecommendationStoreError("decision_sequence_conflict",
                                           type(exc).__name__) from exc
        except sqlite3.Error as exc:
            conn.execute("ROLLBACK")
            raise RecommendationStoreError("store_write_failed",
                                           type(exc).__name__) from exc
        finally:
            conn.close()

    def link_intent(self, recommendation_id: str, intent_id: str, *,
                    at: str, advance: bool = True) -> rd.Recommendation:
        """Record an EXPLICIT intent link. One recommendation may link to many
        intents; the first link may advance ACCEPTED -> INTENT_CREATED."""
        current = self.get_recommendation(recommendation_id)
        if current is None:
            raise RecommendationStoreError("recommendation_not_found",
                                           recommendation_id)
        if intent_id in current.linked_intent_ids:
            return current                                   # idempotent
        updated = rd.link_intent(current, intent_id, at=at)
        if advance and rd.can_transition(updated.status,
                                         rd.RecommendationStatus.INTENT_CREATED):
            updated = rd.transition(updated, rd.RecommendationStatus.INTENT_CREATED,
                                    at=at, reason="intent linked")
        return self._append(recommendation=updated,
                            event_type=rd.RecommendationEventType.INTENT_LINKED,
                            occurred_at=at,
                            payload={"intentId": intent_id,
                                     "status": updated.status},
                            provenance=updated.provenance)

    def observe_execution(self, recommendation_id: str, *, status: str, at: str,
                          reason: str, evidence: dict | None = None) -> rd.Recommendation:
        """Record OBSERVED execution progress. The ledger and execution store
        remain authoritative; this only mirrors the proposal's disposition."""
        current = self.get_recommendation(recommendation_id)
        if current is None:
            raise RecommendationStoreError("recommendation_not_found",
                                           recommendation_id)
        if status == current.status:
            return current
        updated = rd.transition(current, status, at=at, reason=reason)
        event = (rd.RecommendationEventType.FAILED
                 if status == rd.RecommendationStatus.FAILED
                 else rd.RecommendationEventType.EXECUTION_OBSERVED)
        return self._append(recommendation=updated, event_type=event,
                            occurred_at=at,
                            payload={"status": status, "reason": reason,
                                     "evidence": dict(evidence or {})},
                            provenance=updated.provenance)

    def mark_expired(self, recommendation_id: str, *, at: str,
                     reason: str = "expiry reached") -> rd.Recommendation:
        return self._status_write(recommendation_id, rd.RecommendationStatus.EXPIRED,
                                  rd.RecommendationEventType.EXPIRED, at, reason)

    def withdraw_recommendation(self, recommendation_id: str, *, at: str,
                                reason: str) -> rd.Recommendation:
        return self._status_write(recommendation_id, rd.RecommendationStatus.WITHDRAWN,
                                  rd.RecommendationEventType.WITHDRAWN, at, reason)

    def _status_write(self, recommendation_id: str, status: str, event: str,
                      at: str, reason: str) -> rd.Recommendation:
        current = self.get_recommendation(recommendation_id)
        if current is None:
            raise RecommendationStoreError("recommendation_not_found",
                                           recommendation_id)
        if current.status == status:
            return current
        updated = rd.transition(current, status, at=at, reason=reason)
        return self._append(recommendation=updated, event_type=event,
                            occurred_at=at,
                            payload={"status": status, "reason": reason},
                            provenance=updated.provenance)

    def supersede_recommendation(self, previous_id: str,
                                 replacement: rd.Recommendation, *, at: str,
                                 reason: str) -> tuple:
        """Supersede `previous_id` with a NEW recommendation, preserving all
        prior history. Cycles and cross-Scenario supersession are refused."""
        previous = self.get_recommendation(previous_id)
        if previous is None:
            raise RecommendationStoreError("recommendation_not_found", previous_id)
        superseded, linked = rd.supersede(previous, replacement, at=at, reason=reason)
        stored_new = self.create_recommendation(linked)
        stored_old = self._append(
            recommendation=superseded,
            event_type=rd.RecommendationEventType.SUPERSEDED, occurred_at=at,
            payload={"status": rd.RecommendationStatus.SUPERSEDED,
                     "supersededBy": replacement.recommendation_id,
                     "reason": reason},
            provenance=superseded.provenance)
        return stored_old, stored_new

    # ── reads ────────────────────────────────────────────────────────────────
    def get_recommendation(self, recommendation_id: str) -> rd.Recommendation | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM recommendations "
                               "WHERE recommendation_id=?",
                               (recommendation_id,)).fetchone()
            return self._row_to_recommendation(row) if row else None

    def list_recommendations(self, *, statuses: tuple | None = None,
                             scenario_id: str | None = None,
                             instrument: str | None = None,
                             direction: str | None = None,
                             source: str | None = None, node_id: str | None = None,
                             account_fingerprint: str | None = None,
                             created_from: str | None = None,
                             created_to: str | None = None,
                             linked_intent: str | None = None,
                             limit: int = 100, offset: int = 0) -> list:
        """Deterministic ordering: newest first, ties broken by id."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        for column, value in (("scenario_id", scenario_id),
                              ("instrument", instrument), ("direction", direction),
                              ("source", source), ("node_id", node_id),
                              ("account_fingerprint", account_fingerprint)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        for column, value, op in (("created_at", created_from, ">="),
                                  ("created_at", created_to, "<=")):
            if value:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        if linked_intent:
            clauses.append("linked_intent_ids LIKE ?")
            params.append(f'%"{linked_intent}"%')
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM recommendations{where} "
                "ORDER BY created_at DESC, recommendation_id ASC LIMIT ? OFFSET ?",
                params).fetchall()
            return [self._row_to_recommendation(r) for r in rows]

    def count_recommendations(self, **filters) -> int:
        return len(self.list_recommendations(limit=100000, **filters))

    def list_active_recommendations(self, limit: int = 200) -> list:
        return self.list_recommendations(
            statuses=tuple(sorted(rd.ACTIVE_STATUSES)), limit=limit)

    def decisions(self, recommendation_id: str, limit: int = 200) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recommendation_decisions WHERE recommendation_id=? "
                "ORDER BY sequence ASC, seq ASC LIMIT ?",
                (recommendation_id, int(limit))).fetchall()
            return [self._row_to_decision(r) for r in rows]

    def history(self, recommendation_id: str, limit: int = 200) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM recommendation_events WHERE recommendation_id=? "
                "ORDER BY sequence ASC LIMIT ?",
                (recommendation_id, int(limit))).fetchall()
            return [self._row_to_event(r) for r in rows]

    def next_decision_sequence(self, recommendation_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT MAX(sequence) AS s FROM "
                               "recommendation_decisions WHERE recommendation_id=?",
                               (recommendation_id,)).fetchone()
            return int(row["s"] or 0) + 1

    def rebuild_recommendation(self, recommendation_id: str) -> rd.Recommendation | None:
        """Rebuild from the append-only history ALONE — the snapshot is never
        consulted. This proves the events are the source of truth."""
        events = self.history(recommendation_id)
        return rd.rebuild(events) if events else None

    def summary(self) -> rd.RecommendationSummary:
        return rd.RecommendationSummary.of(self.list_recommendations(limit=100000))
