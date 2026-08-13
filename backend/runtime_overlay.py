"""HARDEN-3 — the runtime overlay store, given an owner.

WHAT THIS IS
    The durable store behind `runtime.db`: per-entity mutable state that the
    fixture world cannot express (a deployment paused by an operator, a trade's
    current status, the pre-pause status remembered for Resume, operator
    preferences) plus the latest node-telemetry snapshot.

WHY IT NOW HAS A MODULE
    It was the only durable store in the repository with no owner: its schema,
    its connection handling, its lock and its read/write pair lived as loose
    helpers inside `server.py`, which meant the 5,167-line HTTP module owned a
    database. That is also why it was the store the test-isolation guard missed
    for the longest — nothing named it, so nothing thought about it.

OWNERSHIP RULES
    * This module owns the schema, the connection and the process lock.
    * It performs NO business logic: applying an overlay to a domain entity is
      the caller's job, because only the caller knows the entity's shape.
    * Keys prefixed with `_` are internal bookkeeping and are stripped by the
      caller before an entity is returned.

CONCURRENCY
    One process-wide lock serialises writes, and every connection is opened and
    closed per operation. Python's `sqlite3` supplies a 5-second busy timeout by
    default, so a concurrent reader waits rather than failing — verified. There
    is no WAL: a writer therefore blocks readers for the duration of its
    transaction, which is acceptable for a store whose writes are operator
    actions rather than a hot path.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

#: Kind used for the latest node-telemetry snapshot. Deliberately a distinct
#: kind rather than a general overlay: telemetry is OBSERVED fact and must not
#: be suppressed by the dry-run guard that (correctly) suppresses simulated
#: broker effects.
LIVE_SNAPSHOT_KIND = "live_snapshot"

_SCHEMA = """
    CREATE TABLE IF NOT EXISTS runtime_overlay (
        kind TEXT NOT NULL,
        entity_id TEXT NOT NULL,
        overlay TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (kind, entity_id)
    )
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class RuntimeOverlayStore:
    """Durable per-entity overlay state. One instance per database file.

    `path_fn` is a callable rather than a path so the owner can repoint the
    database (tests do exactly this) without reconstructing the store.
    """

    def __init__(self, path_fn: Callable[[], Path]):
        self._path_fn = path_fn
        self._lock = threading.Lock()

    @property
    def path(self) -> Path:
        return Path(self._path_fn())

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.execute(_SCHEMA)
        return conn

    def exists(self) -> bool:
        """Whether the database has been created yet.

        Checked before reads so a fresh install answers "no overlays" without
        creating an empty file as a side effect of a GET.
        """
        return self.path.exists()

    # ── reads ────────────────────────────────────────────────────────────────
    def load(self, kind: str) -> dict[str, dict]:
        """Every overlay for a kind, keyed by entity id. Empty before any write."""
        if not self.exists():
            return {}
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT entity_id, overlay FROM runtime_overlay WHERE kind = ?",
                    (kind,)).fetchall()
            finally:
                conn.close()
        return {entity_id: json.loads(payload) for entity_id, payload in rows}

    def get(self, kind: str, entity_id: str) -> dict:
        """One entity's overlay, or `{}` when none has been recorded."""
        if not self.exists():
            return {}
        with self._lock:
            conn = self._connect()
            try:
                row = conn.execute(
                    "SELECT overlay FROM runtime_overlay "
                    "WHERE kind = ? AND entity_id = ?", (kind, entity_id)).fetchone()
            finally:
                conn.close()
        return json.loads(row[0]) if row else {}

    def count(self, kind: str) -> int:
        return len(self.load(kind))

    # ── writes ───────────────────────────────────────────────────────────────
    def put(self, kind: str, entity_id: str, overlay: dict, *,
            at: str | None = None) -> None:
        """Upsert one overlay. The caller decides whether a write is permitted
        (e.g. the dry-run guard) — this store does not second-guess it."""
        stamp = at or _now_iso()
        with self._lock:
            conn = self._connect()
            try:
                conn.execute(
                    "INSERT INTO runtime_overlay (kind, entity_id, overlay, "
                    "updated_at) VALUES (?, ?, ?, ?) "
                    "ON CONFLICT(kind, entity_id) DO UPDATE SET "
                    "overlay = excluded.overlay, updated_at = excluded.updated_at",
                    (kind, entity_id, json.dumps(overlay), stamp))
                conn.commit()
            finally:
                conn.close()

    def clear(self, kind: str | None = None) -> int:
        """Delete overlays, returning the number removed.

        Used by the runtime-reset surface. Scoped by kind when given, because
        resetting one domain must not silently discard another's state.
        """
        if not self.exists():
            return 0
        with self._lock:
            conn = self._connect()
            try:
                if kind is None:
                    cursor = conn.execute("DELETE FROM runtime_overlay")
                else:
                    cursor = conn.execute(
                        "DELETE FROM runtime_overlay WHERE kind = ?", (kind,))
                conn.commit()
                return cursor.rowcount or 0
            finally:
                conn.close()

    def status(self) -> dict:
        """Redaction-safe description for diagnostics: the file NAME only, never
        the absolute path (which would disclose host layout)."""
        if not self.exists():
            return {"available": False, "source": None, "kinds": []}
        with self._lock:
            conn = self._connect()
            try:
                rows = conn.execute(
                    "SELECT kind, COUNT(*) FROM runtime_overlay GROUP BY kind"
                ).fetchall()
            finally:
                conn.close()
        return {"available": True, "source": self.path.name,
                "kinds": {kind: count for kind, count in rows}}
