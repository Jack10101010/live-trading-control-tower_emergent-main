"""LIVE-4B — the durable SCENARIO store.

Append-only events are the source of truth; the `scenarios` table holds an
immutable SNAPSHOT of the current state so reads are cheap. Any snapshot can be
discarded and rebuilt deterministically from `scenario_events` — the store never
depends on a snapshot it could not reproduce.

OWNERSHIP
  * This store owns SCENARIOS ONLY. It has its own database file and its own
    schema version, and it never reads or writes execution, reconciliation,
    authorization, mode or lock tables. `ExecutionStore` remains the single
    owner of execution facts; this is a different domain, not a second copy of
    the same facts.
  * It owns no broker and no execution: it holds no adapter, submits nothing,
    and makes no execution decision.

RULES
  * `scenario_events` is APPEND-ONLY — no update or delete surface exists.
  * Writes are atomic: the event and the snapshot commit in one transaction.
  * Writes are IDEMPOTENT on the event key `(scenario_id, sequence)`; a replayed
    write is a no-op rather than a duplicate fact.
  * Nothing is created or updated automatically. Every call is an explicit,
    caller-supplied fact; there is no strategy logic anywhere in this module.
  * Corruption or an unknown schema version FAILS CLOSED (`ScenarioStoreError`).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import scenario_domain as sd

SCHEMA_VERSION = 1


class ScenarioStoreError(RuntimeError):
    """The scenario store is unavailable, corrupt, or from an unsupported schema."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class ScenarioStore:
    """Durable scenario state. One instance per database file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        try:
            with self._conn() as conn:
                self._init_schema(conn)
        except sqlite3.Error as exc:
            raise ScenarioStoreError("store_unavailable", type(exc).__name__) from exc

    # ── connection / schema ──────────────────────────────────────────────────
    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS scenario_meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute(
            "SELECT value FROM scenario_meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO scenario_meta (key, value) VALUES "
                         "('schema_version', ?)", (str(SCHEMA_VERSION),))
        else:
            found = int(row["value"])
            if found > SCHEMA_VERSION:
                raise ScenarioStoreError(
                    "unsupported_schema_version",
                    f"scenario schema {found} > supported {SCHEMA_VERSION}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scenarios (
                scenario_id TEXT PRIMARY KEY,
                instrument TEXT NOT NULL, session TEXT NOT NULL,
                structure TEXT NOT NULL, direction TEXT NOT NULL,
                entry_model TEXT NOT NULL, timeframe TEXT,
                status TEXT NOT NULL, node_id TEXT, account_fingerprint TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL, expires_at TEXT,
                linked_recommendation_id TEXT,
                linked_intent_ids TEXT NOT NULL DEFAULT '[]',
                linked_order_ids TEXT NOT NULL DEFAULT '[]',
                linked_position_ids TEXT NOT NULL DEFAULT '[]',
                tags TEXT NOT NULL DEFAULT '[]',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                provenance TEXT NOT NULL DEFAULT 'operator'
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS scenario_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                scenario_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                at TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                data_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE (scenario_id, sequence)
            )""")
        conn.commit()

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM scenario_meta WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0

    # ── serialization helpers ────────────────────────────────────────────────
    @staticmethod
    def _row_to_scenario(row: Any) -> sd.Scenario:
        try:
            return sd.Scenario(
                scenario_id=row["scenario_id"], instrument=row["instrument"],
                session=row["session"], structure=row["structure"],
                direction=row["direction"], entry_model=row["entry_model"],
                timeframe=row["timeframe"], status=row["status"],
                node_id=row["node_id"], account_fingerprint=row["account_fingerprint"],
                created_at=row["created_at"], updated_at=row["updated_at"],
                expires_at=row["expires_at"],
                linked_recommendation_id=row["linked_recommendation_id"],
                linked_intent_ids=tuple(json.loads(row["linked_intent_ids"])),
                linked_order_ids=tuple(json.loads(row["linked_order_ids"])),
                linked_position_ids=tuple(json.loads(row["linked_position_ids"])),
                tags=tuple(json.loads(row["tags"])),
                metadata=json.loads(row["metadata_json"]),
                provenance=row["provenance"])
        except (ValueError, TypeError, KeyError, sd.ScenarioError) as exc:
            raise ScenarioStoreError("corrupt_scenario_row",
                                     type(exc).__name__) from exc

    @staticmethod
    def _row_to_event(row: Any) -> sd.ScenarioEvent:
        try:
            return sd.ScenarioEvent(
                scenario_id=row["scenario_id"], event_type=row["event_type"],
                at=row["at"], reason=row["reason"],
                data=json.loads(row["data_json"]), sequence=row["sequence"])
        except (ValueError, TypeError, KeyError, sd.ScenarioError) as exc:
            raise ScenarioStoreError("corrupt_scenario_event",
                                     type(exc).__name__) from exc

    def _write(self, conn, scenario: sd.Scenario) -> None:
        conn.execute(
            """INSERT INTO scenarios (scenario_id, instrument, session, structure,
                direction, entry_model, timeframe, status, node_id,
                account_fingerprint, created_at, updated_at, expires_at,
                linked_recommendation_id, linked_intent_ids, linked_order_ids,
                linked_position_ids, tags, metadata_json, provenance)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scenario_id) DO UPDATE SET
                 status=excluded.status, updated_at=excluded.updated_at,
                 expires_at=excluded.expires_at,
                 linked_recommendation_id=excluded.linked_recommendation_id,
                 linked_intent_ids=excluded.linked_intent_ids,
                 linked_order_ids=excluded.linked_order_ids,
                 linked_position_ids=excluded.linked_position_ids,
                 tags=excluded.tags, metadata_json=excluded.metadata_json""",
            (scenario.scenario_id, scenario.instrument, scenario.session,
             scenario.structure, scenario.direction, scenario.entry_model,
             scenario.timeframe, scenario.status, scenario.node_id,
             scenario.account_fingerprint, scenario.created_at, scenario.updated_at,
             scenario.expires_at, scenario.linked_recommendation_id,
             json.dumps(list(scenario.linked_intent_ids)),
             json.dumps(list(scenario.linked_order_ids)),
             json.dumps(list(scenario.linked_position_ids)),
             json.dumps(list(scenario.tags)), json.dumps(scenario.metadata),
             scenario.provenance))

    def _next_sequence(self, conn, scenario_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(sequence) AS s FROM scenario_events WHERE scenario_id=?",
            (scenario_id,)).fetchone()
        return int(row["s"] or 0) + 1

    # ── writes (explicit only — nothing here creates or updates automatically) ─
    def create_scenario(self, scenario: sd.Scenario, *, reason: str = "created") -> sd.Scenario:
        """Persist a NEW scenario plus its `ScenarioCreated` event, atomically.
        Idempotent: re-creating the same scenario id is a no-op returning the
        stored scenario (deterministic ids make replay safe)."""
        existing = self.get_scenario(scenario.scenario_id)
        if existing is not None:
            return existing
        payload = {
            "instrument": scenario.instrument, "session": scenario.session,
            "structure": scenario.structure, "direction": scenario.direction,
            "entry_model": scenario.entry_model, "timeframe": scenario.timeframe,
            "node_id": scenario.node_id,
            "account_fingerprint": scenario.account_fingerprint,
            "expires_at": scenario.expires_at, "tags": list(scenario.tags),
            "metadata": dict(scenario.metadata), "provenance": scenario.provenance,
            "status": sd.ScenarioStatus.CREATED,
        }
        try:
            with self._conn() as conn:
                conn.execute("BEGIN")
                self._write(conn, scenario)
                conn.execute(
                    "INSERT INTO scenario_events (scenario_id, sequence, event_type,"
                    " at, reason, data_json) VALUES (?,?,?,?,?,?)",
                    (scenario.scenario_id, 1, sd.ScenarioEventType.CREATED,
                     scenario.created_at, reason, json.dumps(payload)))
                conn.commit()
        except sqlite3.IntegrityError:
            return self.get_scenario(scenario.scenario_id) or scenario
        except sqlite3.Error as exc:
            raise ScenarioStoreError("store_write_failed", type(exc).__name__) from exc
        return scenario

    def update_scenario(self, scenario_id: str, *, at: str, reason: str,
                        to_status: str | None = None,
                        recommendation_id: str | None = None,
                        intent_id: str | None = None,
                        order_id: str | None = None,
                        position_id: str | None = None) -> sd.Scenario:
        """Apply ONE explicit change (a status transition and/or link) and append
        its event, atomically. Invalid transitions raise and write NOTHING."""
        current = self.get_scenario(scenario_id)
        if current is None:
            raise ScenarioStoreError("scenario_not_found", scenario_id)
        updated = current
        event_type = None
        data: dict = {}
        if any((recommendation_id, intent_id, order_id, position_id)):
            updated = sd.link(updated, at=at, recommendation_id=recommendation_id,
                              intent_id=intent_id, order_id=order_id,
                              position_id=position_id)
            event_type = (sd.ScenarioEventType.RECOMMENDATION_LINKED if recommendation_id
                          else sd.ScenarioEventType.INTENT_LINKED if intent_id
                          else sd.ScenarioEventType.ORDER_LINKED if order_id
                          else sd.ScenarioEventType.POSITION_LINKED)
            data = {k: v for k, v in (("recommendation_id", recommendation_id),
                                      ("intent_id", intent_id),
                                      ("order_id", order_id),
                                      ("position_id", position_id)) if v}
        if to_status is not None and to_status != updated.status:
            updated = sd.transition(updated, to_status, at=at, reason=reason)
            event_type = sd.STATUS_EVENT.get(to_status, event_type)
            data["status"] = to_status
        if event_type is None:
            return current                       # nothing to record
        try:
            with self._conn() as conn:
                seq = self._next_sequence(conn, scenario_id)
                conn.execute("BEGIN")
                self._write(conn, updated)
                conn.execute(
                    "INSERT INTO scenario_events (scenario_id, sequence, event_type,"
                    " at, reason, data_json) VALUES (?,?,?,?,?,?)",
                    (scenario_id, seq, event_type, at, reason, json.dumps(data)))
                conn.commit()
        except sqlite3.IntegrityError:
            return self.get_scenario(scenario_id) or updated    # idempotent replay
        except sqlite3.Error as exc:
            raise ScenarioStoreError("store_write_failed", type(exc).__name__) from exc
        return updated

    # ── reads ────────────────────────────────────────────────────────────────
    def get_scenario(self, scenario_id: str) -> sd.Scenario | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM scenarios WHERE scenario_id=?",
                               (scenario_id,)).fetchone()
            return self._row_to_scenario(row) if row else None

    def list_scenarios(self, *, statuses: tuple | None = None,
                       instrument: str | None = None, session: str | None = None,
                       node_id: str | None = None, limit: int = 500) -> list:
        """Deterministic ordering: newest first, ties broken by id."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        for column, value in (("instrument", instrument), ("session", session),
                              ("node_id", node_id)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(int(limit))
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM scenarios{where} "
                "ORDER BY created_at DESC, scenario_id ASC LIMIT ?", params).fetchall()
            return [self._row_to_scenario(r) for r in rows]

    def list_active_scenarios(self, limit: int = 500) -> list:
        return self.list_scenarios(statuses=tuple(sorted(sd.ACTIVE_STATUSES)),
                                   limit=limit)

    def history(self, scenario_id: str, limit: int = 500) -> list:
        """The append-only event history, in sequence order."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scenario_events WHERE scenario_id=? "
                "ORDER BY sequence ASC LIMIT ?", (scenario_id, int(limit))).fetchall()
            return [self._row_to_event(r) for r in rows]

    def all_events(self, limit: int = 1000) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM scenario_events ORDER BY seq DESC LIMIT ?",
                (int(limit),)).fetchall()
            return [self._row_to_event(r) for r in rows]

    def rebuild_scenario(self, scenario_id: str) -> sd.Scenario | None:
        """Deterministically rebuild from the append-only history alone. The
        snapshot is never consulted — this is what proves the events are the
        source of truth."""
        events = self.history(scenario_id)
        return sd.rebuild(events) if events else None

    def summary(self) -> sd.ScenarioSummary:
        return sd.ScenarioSummary.of(self.list_scenarios(limit=1000))
