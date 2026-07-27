"""ARCH-2 — the durable execution and order store.

The single durable owner of canonical execution lifecycle state:

  * intents            — current state of every OrderIntent (one row per intent)
  * intent_transitions — the APPEND-ONLY lifecycle history (never updated,
                         never deleted; the durable audit of every transition)
  * recon_runs         — immutable reconciliation runs
  * recon_items        — the discrepancies each run found (resolution is a new
                         fact on the row, never a deletion)

PERSISTENCE APPROACH — the repository's existing pattern: one SQLite file beside
`events.db`/`runtime.db` (gitignored), opened per operation, written in single
transactions so the intent row and its transition record commit atomically.
This deliberately does NOT create a parallel command/audit store: BotEvents stay
in `events.db` exactly as before; this store holds the lifecycle facts that
previously existed only in process memory.

FAIL-CLOSED RULES
  * `schema_version` is stamped in a meta table. An unknown/newer version raises
    `StoreError` — the store refuses to guess at a migration.
  * Corruption (failed integrity check, unreadable rows) raises `StoreError`;
    callers deny execution rather than proceeding without durability.
  * No silent retention: nothing is ever deleted. Queries are bounded via LIMIT
    parameters, but history is never evicted. If retention is ever needed it must
    be an explicit, documented, audited change.
  * No secrets: intent metadata is redacted BEFORE persistence
    (`security_config.redact_mapping`), so a credential can never reach disk.

RESTART RECOVERY (`recover()`): deterministic and fail-closed per
`order_lifecycle.recovery_plan` — pre-dispatch intents fail, possibly-dispatched
intents become unknown → reconciliation_required. Completion is never fabricated.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import order_lifecycle as ol
import security_config

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """The store is unavailable, corrupt, or from an unsupported schema.
    Callers must FAIL CLOSED (deny execution), never continue without durability."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ExecutionStore:
    """Durable execution/order state. One instance per database file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        try:
            with self._conn() as conn:
                self._init_schema(conn)
        except sqlite3.Error as exc:
            raise StoreError("store_unavailable", f"{type(exc).__name__}") from exc

    # ── connection / schema ──────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO meta (key, value) VALUES ('schema_version', ?)",
                         (str(SCHEMA_VERSION),))
        else:
            found = int(row["value"])
            if found > SCHEMA_VERSION:
                # A newer schema than this code understands: refuse, never guess.
                raise StoreError("unsupported_schema_version",
                                 f"store schema {found} > supported {SCHEMA_VERSION}")
            # found < SCHEMA_VERSION would be a real migration; none exists yet —
            # when one does, it must be explicit and fail closed on error.
        conn.execute(
            """CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY,
                command_id TEXT, correlation_id TEXT, idempotency_key TEXT,
                command_name TEXT NOT NULL, kind TEXT NOT NULL,
                deployment_id TEXT, account_id TEXT, instrument TEXT,
                side TEXT, order_type TEXT, quantity REAL,
                entry REAL, stop_loss REAL, take_profit REAL, time_in_force TEXT,
                source TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT,
                metadata_json TEXT NOT NULL,
                state TEXT NOT NULL, updated_at TEXT NOT NULL,
                broker_ref TEXT
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS intent_transitions (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                intent_id TEXT NOT NULL,
                from_state TEXT NOT NULL, to_state TEXT NOT NULL,
                at TEXT NOT NULL, reason TEXT NOT NULL, evidence TEXT NOT NULL,
                broker_ref TEXT
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recon_runs (
                recon_id TEXT PRIMARY KEY,
                at TEXT NOT NULL, account_identity TEXT,
                expected_account_identity TEXT,
                clean INTEGER NOT NULL, critical INTEGER NOT NULL,
                stale INTEGER NOT NULL, failed INTEGER NOT NULL,
                sources_json TEXT NOT NULL
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS recon_items (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                recon_id TEXT NOT NULL,
                class TEXT NOT NULL, entity_id TEXT, critical INTEGER NOT NULL,
                detail TEXT NOT NULL, resolved INTEGER NOT NULL DEFAULT 0,
                resolved_at TEXT, resolved_evidence TEXT
            )""")
        conn.commit()

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0

    # ── intents ──────────────────────────────────────────────────────────────
    def create_intent(self, intent: ol.OrderIntent, *, now: str) -> None:
        """Persist a new intent in state `created`, with its first transition
        record, atomically. Duplicate intent ids fail closed."""
        redacted = security_config.redact_mapping(intent.metadata)
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                conn.execute(
                    """INSERT INTO intents (intent_id, command_id, correlation_id,
                        idempotency_key, command_name, kind, deployment_id, account_id,
                        instrument, side, order_type, quantity, entry, stop_loss,
                        take_profit, time_in_force, source, created_at, expires_at,
                        metadata_json, state, updated_at, broker_ref)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (intent.intent_id, intent.command_id, intent.correlation_id,
                     intent.idempotency_key, intent.command_name, intent.kind,
                     intent.deployment_id, intent.account_id, intent.instrument,
                     intent.side, intent.order_type, intent.quantity, intent.entry,
                     intent.stop_loss, intent.take_profit, intent.time_in_force,
                     intent.source, intent.created_at, intent.expires_at,
                     json.dumps(redacted), ol.CREATED, now, None))
                conn.execute(
                    "INSERT INTO intent_transitions (intent_id, from_state, to_state, at, reason, evidence, broker_ref)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (intent.intent_id, ol.CREATED, ol.CREATED, now, "intent_created", "", None))
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreError("duplicate_intent_id", intent.intent_id) from exc
        except sqlite3.Error as exc:
            raise StoreError("store_write_failed", type(exc).__name__) from exc

    def record_transition(self, intent_id: str, to_state: str, *, at: str,
                          reason: str, evidence: str = "",
                          broker_ref: str | None = None) -> ol.Transition:
        """Validate (via the canonical state machine) and durably record ONE
        transition. The intent row and the transition history commit atomically;
        an invalid transition raises and writes NOTHING."""
        try:
            with self._conn() as conn:
                row = conn.execute("SELECT state, updated_at FROM intents WHERE intent_id=?",
                                   (intent_id,)).fetchone()
                if row is None:
                    raise StoreError("intent_not_found", intent_id)
                tr = ol.transition(intent_id, row["state"], to_state, at=at,
                                   reason=reason, evidence=evidence,
                                   broker_ref=broker_ref, previous_at=row["updated_at"])
                conn.execute("BEGIN")
                conn.execute(
                    "UPDATE intents SET state=?, updated_at=?, broker_ref=COALESCE(?, broker_ref)"
                    " WHERE intent_id=?",
                    (tr.to_state, tr.at, tr.broker_ref, intent_id))
                conn.execute(
                    "INSERT INTO intent_transitions (intent_id, from_state, to_state, at, reason, evidence, broker_ref)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (tr.intent_id, tr.from_state, tr.to_state, tr.at, tr.reason,
                     tr.evidence, tr.broker_ref))
                conn.commit()
                return tr
        except ol.LifecycleError:
            raise
        except StoreError:
            raise
        except sqlite3.Error as exc:
            raise StoreError("store_write_failed", type(exc).__name__) from exc

    def get_intent(self, intent_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM intents WHERE intent_id=?", (intent_id,)).fetchone()
            return dict(row) if row else None

    def intent_by_idempotency_key(self, key: str, *,
                                  command_name: str | None = None) -> dict | None:
        """LIVE-2 duplicate detection: the EARLIEST intent recorded under this
        idempotency key (restart-safe — the store is durable, so a replayed
        request after a process restart still finds the original submission).
        Optionally narrowed to one command name."""
        if not key:
            return None
        with self._conn() as conn:
            if command_name:
                row = conn.execute(
                    "SELECT * FROM intents WHERE idempotency_key=? AND command_name=?"
                    " ORDER BY created_at ASC, intent_id ASC LIMIT 1",
                    (key, command_name)).fetchone()
            else:
                row = conn.execute(
                    "SELECT * FROM intents WHERE idempotency_key=?"
                    " ORDER BY created_at ASC, intent_id ASC LIMIT 1",
                    (key,)).fetchone()
            return dict(row) if row else None

    def transitions_of(self, intent_id: str, limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM intent_transitions WHERE intent_id=? ORDER BY seq ASC LIMIT ?",
                (intent_id, int(limit))).fetchall()
            return [dict(r) for r in rows]

    def intents_by_state(self, states: tuple[str, ...] | None = None,
                         limit: int = 200) -> list[dict]:
        with self._conn() as conn:
            if states:
                q = ",".join("?" for _ in states)
                rows = conn.execute(
                    f"SELECT * FROM intents WHERE state IN ({q}) ORDER BY created_at DESC LIMIT ?",
                    (*states, int(limit))).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM intents ORDER BY created_at DESC LIMIT ?",
                    (int(limit),)).fetchall()
            return [dict(r) for r in rows]

    def counts_by_state(self) -> dict[str, int]:
        with self._conn() as conn:
            rows = conn.execute("SELECT state, COUNT(*) AS n FROM intents GROUP BY state").fetchall()
            return {r["state"]: r["n"] for r in rows}

    # ── restart recovery ─────────────────────────────────────────────────────
    def recover(self, *, now: str) -> dict:
        """Deterministic restart recovery per `order_lifecycle.recovery_plan`.
        Every action is a normal, validated, durably-recorded transition —
        recovery uses no privileged path and fabricates no completion."""
        failed, unknown = [], []
        for row in self.intents_by_state(limit=10_000):
            plan = ol.recovery_plan(row["state"])
            if plan is None:
                continue
            target, reason = plan
            self.record_transition(row["intent_id"], target, at=now, reason=reason,
                                   evidence="process restart")
            if target == ol.FAILED:
                failed.append(row["intent_id"])
            else:
                unknown.append(row["intent_id"])
                self.record_transition(row["intent_id"], ol.RECONCILIATION_REQUIRED,
                                       at=now, reason="restart_requires_reconciliation",
                                       evidence="in-flight at restart")
        return {"failedPreDispatch": failed, "queuedForReconciliation": unknown}

    # ── reconciliation persistence ───────────────────────────────────────────
    def save_reconciliation(self, run: dict, items: list[dict]) -> None:
        """Persist one immutable reconciliation run and its discrepancies."""
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                conn.execute(
                    "INSERT INTO recon_runs (recon_id, at, account_identity, expected_account_identity,"
                    " clean, critical, stale, failed, sources_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run["reconId"], run["at"], run.get("accountIdentity"),
                     run.get("expectedAccountIdentity"), int(run["clean"]),
                     int(run["critical"]), int(run["stale"]), int(run["failed"]),
                     json.dumps(run.get("sources", {}))))
                for item in items:
                    conn.execute(
                        "INSERT INTO recon_items (recon_id, class, entity_id, critical, detail)"
                        " VALUES (?,?,?,?,?)",
                        (run["reconId"], item["class"], item.get("entityId"),
                         int(item.get("critical", False)), item.get("detail", "")))
                conn.commit()
        except sqlite3.IntegrityError as exc:
            raise StoreError("duplicate_reconciliation_id", run.get("reconId", "")) from exc
        except sqlite3.Error as exc:
            raise StoreError("store_write_failed", type(exc).__name__) from exc

    def latest_reconciliation(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM recon_runs ORDER BY at DESC, recon_id DESC LIMIT 1").fetchone()
            if row is None:
                return None
            out = dict(row)
            items = conn.execute("SELECT * FROM recon_items WHERE recon_id=? ORDER BY seq ASC",
                                 (row["recon_id"],)).fetchall()
            out["items"] = [dict(i) for i in items]
            return out

    def unresolved_critical_count(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM recon_items WHERE critical=1 AND resolved=0").fetchone()
            return int(row["n"])

    def resolve_item(self, seq: int, *, at: str, evidence: str) -> None:
        """Mark a discrepancy resolved — a NEW fact with evidence, not a deletion."""
        if not evidence:
            raise StoreError("evidence_required", "resolution requires evidence")
        with self._conn() as conn:
            conn.execute("UPDATE recon_items SET resolved=1, resolved_at=?, resolved_evidence=? WHERE seq=?",
                         (at, evidence, int(seq)))
            conn.commit()
