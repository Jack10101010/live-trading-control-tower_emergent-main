"""LIVE-4C — the durable TRADE LEDGER store.

Append-only events are the source of truth; `ledger_entries` holds an immutable
current-version snapshot for cheap reads. Any snapshot can be discarded and
rebuilt deterministically from `ledger_events`.

OWNERSHIP
  * Owns the LEDGER only, in its own database file with its own schema version.
  * Never touches execution tables, scenario tables, or a broker. It holds no
    adapter and issues no command — the ledger is never an execution authority.

RULES
  * `ledger_events` is APPEND-ONLY: there is no update or delete surface, and
    no generic mutable CRUD. Financial facts can only be superseded by a NEW
    amendment event.
  * A FINALIZED entry is never rewritten in place. `amend_trade` appends a
    `TradeAmended` event and bumps the version; every prior version remains
    replayable from history.
  * Ingestion is IDEMPOTENT on `(trade_id, sequence)` and on deterministic
    event ids, so re-reading the same broker history appends nothing.
  * Writes are atomic (event + snapshot in one transaction).
  * An unsupported newer schema FAILS CLOSED.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import trade_ledger_domain as tld

SCHEMA_VERSION = 1


#: Which event records arrival at each working status.
_EVENT_FOR_STATUS = {
    tld.TradeLedgerStatus.RECONSTRUCTING: tld.LedgerEventType.RECONSTRUCTION_STARTED,
    tld.TradeLedgerStatus.INCOMPLETE: tld.LedgerEventType.MARKED_INCOMPLETE,
    tld.TradeLedgerStatus.READY_TO_FINALIZE: tld.LedgerEventType.READY_TO_FINALIZE,
    tld.TradeLedgerStatus.CONFLICTED: tld.LedgerEventType.CONFLICT_DETECTED,
}


def _legal_path(src: str, dst: str) -> tuple:
    """The shortest legal status path from `src` to `dst`, or () when none
    exists. Only working statuses are traversed — finalization and amendment
    are explicit operator/service actions, never reached by a refresh."""
    if src == dst:
        return ()
    if tld.can_transition(src, dst):
        return (dst,)
    via = tld.TradeLedgerStatus.RECONSTRUCTING
    if tld.can_transition(src, via) and tld.can_transition(via, dst):
        return (via, dst)
    return ()


class LedgerStoreError(RuntimeError):
    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


class TradeLedgerStore:
    """Durable ledger state. One instance per database file."""

    def __init__(self, path: Path | str):
        self._path = Path(path)
        try:
            with self._conn() as conn:
                self._init_schema(conn)
        except sqlite3.Error as exc:
            raise LedgerStoreError("store_unavailable", type(exc).__name__) from exc

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.execute("CREATE TABLE IF NOT EXISTS ledger_meta "
                     "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        row = conn.execute(
            "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
        if row is None:
            conn.execute("INSERT INTO ledger_meta (key, value) "
                         "VALUES ('schema_version', ?)", (str(SCHEMA_VERSION),))
        elif int(row["value"]) > SCHEMA_VERSION:
            raise LedgerStoreError(
                "unsupported_schema_version",
                f"ledger schema {row['value']} > supported {SCHEMA_VERSION}")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ledger_entries (
                trade_id TEXT PRIMARY KEY,
                status TEXT NOT NULL, version INTEGER NOT NULL,
                instrument TEXT, side TEXT, scenario_id TEXT, node_id TEXT,
                account_fingerprint TEXT, origin TEXT,
                opened_at TEXT, closed_at TEXT,
                gross_realized_pnl REAL, net_realized_pnl REAL, total_costs REAL,
                realized_r REAL, exit_classification TEXT,
                entry_json TEXT NOT NULL,
                observed_at TEXT, finalized_at TEXT, amended_at TEXT,
                updated_at TEXT NOT NULL
            )""")
        conn.execute(
            """CREATE TABLE IF NOT EXISTS ledger_events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id TEXT NOT NULL UNIQUE,
                trade_id TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_type TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                recorded_at TEXT NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                provenance TEXT NOT NULL DEFAULT 'absent',
                schema_version TEXT NOT NULL,
                UNIQUE (trade_id, sequence)
            )""")
        conn.commit()

    def schema_version(self) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT value FROM ledger_meta WHERE key='schema_version'").fetchone()
            return int(row["value"]) if row else 0

    # ── serialization ────────────────────────────────────────────────────────
    @staticmethod
    def _row_to_event(row: Any) -> tld.LedgerEvent:
        try:
            return tld.LedgerEvent(
                event_id=row["event_id"], trade_id=row["trade_id"],
                sequence=row["sequence"], event_type=row["event_type"],
                occurred_at=row["occurred_at"], recorded_at=row["recorded_at"],
                payload=json.loads(row["payload_json"]),
                provenance=row["provenance"], schema_version=row["schema_version"])
        except (ValueError, TypeError, KeyError, tld.TradeLedgerError) as exc:
            raise LedgerStoreError("corrupt_ledger_event", type(exc).__name__) from exc

    @staticmethod
    def _row_to_entry(row: Any) -> dict:
        try:
            return json.loads(row["entry_json"])
        except (ValueError, TypeError) as exc:
            raise LedgerStoreError("corrupt_ledger_entry", type(exc).__name__) from exc

    def _next_sequence(self, conn, trade_id: str) -> int:
        row = conn.execute(
            "SELECT MAX(sequence) AS s FROM ledger_events WHERE trade_id=?",
            (trade_id,)).fetchone()
        return int(row["s"] or 0) + 1

    def _write_snapshot(self, conn, entry: tld.TradeLedgerEntry) -> None:
        trade = entry.trade
        lineage = trade.lineage if trade else None
        conn.execute(
            """INSERT INTO ledger_entries (trade_id, status, version, instrument,
                side, scenario_id, node_id, account_fingerprint, origin,
                opened_at, closed_at, gross_realized_pnl, net_realized_pnl,
                total_costs, realized_r, exit_classification, entry_json,
                observed_at, finalized_at, amended_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(trade_id) DO UPDATE SET
                 status=excluded.status, version=excluded.version,
                 instrument=excluded.instrument, side=excluded.side,
                 scenario_id=excluded.scenario_id, node_id=excluded.node_id,
                 account_fingerprint=excluded.account_fingerprint,
                 origin=excluded.origin, opened_at=excluded.opened_at,
                 closed_at=excluded.closed_at,
                 gross_realized_pnl=excluded.gross_realized_pnl,
                 net_realized_pnl=excluded.net_realized_pnl,
                 total_costs=excluded.total_costs, realized_r=excluded.realized_r,
                 exit_classification=excluded.exit_classification,
                 entry_json=excluded.entry_json, finalized_at=excluded.finalized_at,
                 amended_at=excluded.amended_at, updated_at=excluded.updated_at""",
            (entry.trade_id, entry.status, entry.version,
             trade.instrument if trade else None,
             trade.side if trade else None,
             lineage.scenario_id if lineage else None,
             lineage.node_id if lineage else None,
             lineage.account_fingerprint if lineage else None,
             lineage.origin if lineage else None,
             trade.timing.opened_at if trade else None,
             trade.timing.closed_at if trade else None,
             trade.gross_realized_pnl if trade else None,
             trade.net_realized_pnl if trade else None,
             trade.costs.total if trade else None,
             trade.risk.realized_r if trade else None,
             trade.exit_classification if trade else None,
             json.dumps(entry.as_dict()),
             entry.observed_at, entry.finalized_at, entry.amended_at,
             entry.updated_at))

    # ── append (the ONLY write path) ─────────────────────────────────────────
    def _append(self, *, trade_id: str, event_type: str, occurred_at: str,
                recorded_at: str, payload: dict, provenance: str,
                entry: tld.TradeLedgerEntry) -> tld.TradeLedgerEntry:
        try:
            with self._conn() as conn:
                sequence = self._next_sequence(conn, trade_id)
                event_id = tld.new_event_id(trade_id, sequence)
                # Validate before writing — an invalid event writes NOTHING.
                tld.LedgerEvent(event_id=event_id, trade_id=trade_id,
                                sequence=sequence, event_type=event_type,
                                occurred_at=occurred_at, recorded_at=recorded_at,
                                payload=payload, provenance=provenance)
                conn.execute("BEGIN")
                conn.execute(
                    """INSERT INTO ledger_events (event_id, trade_id, sequence,
                        event_type, occurred_at, recorded_at, payload_json,
                        provenance, schema_version) VALUES (?,?,?,?,?,?,?,?,?)""",
                    (event_id, trade_id, sequence, event_type, occurred_at,
                     recorded_at, json.dumps(payload), provenance,
                     tld.SCHEMA_VERSION))
                self._write_snapshot(conn, entry)
                conn.commit()
        except sqlite3.IntegrityError:
            return self.get_trade(trade_id) or entry      # idempotent replay
        except sqlite3.Error as exc:
            raise LedgerStoreError("store_write_failed", type(exc).__name__) from exc
        return entry

    # ── public interfaces ────────────────────────────────────────────────────
    def ingest_broker_history(self, results, *, now: str,
                              provenance: str) -> list:
        """Record observation + reconstruction for each reconstructed trade.

        IDEMPOTENT: a trade whose recorded state already matches is left
        untouched, so repeated broker-history reads append no events."""
        out: list = []
        for result in results:
            entry = self.reconstruct_trade(result, now=now, provenance=provenance)
            out.append(entry)
        return out

    def reconstruct_trade(self, result, *, now: str,
                          provenance: str) -> tld.TradeLedgerEntry:
        """Record (or refresh) a reconstruction. Never finalizes on its own."""
        trade_id = result.trade.trade_id
        existing = self.get_entry(trade_id)
        target_status = result.status
        payload = {
            "trade": {"_closed_trade": None, "snapshot": result.trade.as_dict()},
            "warnings": list(result.warnings),
            "conflicts": list(result.conflicts),
            "reconciliationFindings": [dict(f) for f in result.reconciliation_findings],
            "managementHistory": [dict(m) for m in result.management_history],
            "blocking": list(result.blocking),
        }
        if existing is None:
            entry = tld.TradeLedgerEntry(
                trade_id=trade_id, status=tld.TradeLedgerStatus.OBSERVED,
                trade=result.trade, warnings=result.warnings,
                conflicts=result.conflicts,
                reconciliation_findings=result.reconciliation_findings,
                management_history=result.management_history,
                observed_at=now, updated_at=now)
            entry = self._append(trade_id=trade_id,
                                 event_type=tld.LedgerEventType.BROKER_HISTORY_OBSERVED,
                                 occurred_at=now, recorded_at=now, payload=payload,
                                 provenance=provenance, entry=entry)
            existing = entry
        # A FINALIZED/AMENDED entry is settled truth: refresh never rewrites it.
        if existing.finalized:
            return existing
        if existing.status == target_status and existing.trade is not None:
            return existing                       # nothing changed -> no event
        from dataclasses import replace as _replace
        # Walk the LEGAL lifecycle path. A freshly OBSERVED entry must pass
        # through RECONSTRUCTING before it can be assessed, so each step is a
        # real recorded event rather than a silent jump.
        path = _legal_path(existing.status, target_status)
        if not path:
            return existing
        entry = existing
        for step in path:
            entry = _replace(entry, status=step, trade=result.trade,
                             warnings=result.warnings, conflicts=result.conflicts,
                             reconciliation_findings=result.reconciliation_findings,
                             management_history=result.management_history,
                             updated_at=now)
            entry = self._append(trade_id=trade_id, event_type=_EVENT_FOR_STATUS[step],
                                 occurred_at=now, recorded_at=now, payload=payload,
                                 provenance=provenance, entry=entry)
        return entry

    def finalize_trade(self, trade_id: str, *, now: str, provenance: str,
                       reason: str = "sufficient closing evidence") -> tld.TradeLedgerEntry:
        """Finalize an entry that is READY_TO_FINALIZE. Anything else raises —
        finalization requires sufficient closing evidence."""
        entry = self.get_entry(trade_id)
        if entry is None:
            raise LedgerStoreError("trade_not_found", trade_id)
        if entry.status != tld.TradeLedgerStatus.READY_TO_FINALIZE:
            raise LedgerStoreError("not_ready_to_finalize", entry.status)
        from dataclasses import replace as _replace
        updated = _replace(entry, status=tld.TradeLedgerStatus.FINALIZED,
                           finalized_at=now, updated_at=now)
        return self._append(trade_id=trade_id, event_type=tld.LedgerEventType.FINALIZED,
                            occurred_at=now, recorded_at=now,
                            payload={"reason": reason}, provenance=provenance,
                            entry=updated)

    def amend_trade(self, trade_id: str, *, trade, now: str, provenance: str,
                    reason: str) -> tld.TradeLedgerEntry:
        """Supersede a finalized entry with LATE evidence (e.g. settled costs).
        The prior version is never rewritten — it stays in the event history."""
        entry = self.get_entry(trade_id)
        if entry is None:
            raise LedgerStoreError("trade_not_found", trade_id)
        if not entry.finalized:
            raise LedgerStoreError("not_finalized", entry.status)
        if not reason:
            raise LedgerStoreError("reason_required")
        from dataclasses import replace as _replace
        updated = _replace(entry, status=tld.TradeLedgerStatus.AMENDED, trade=trade,
                           version=entry.version + 1, amended_at=now, updated_at=now)
        return self._append(
            trade_id=trade_id, event_type=tld.LedgerEventType.AMENDED,
            occurred_at=now, recorded_at=now,
            payload={"reason": reason, "previousVersion": entry.version,
                     "trade": {"_closed_trade": None, "snapshot": trade.as_dict()}},
            provenance=provenance, entry=updated)

    def record_cost_evidence(self, trade_id: str, *, trade, now: str,
                             provenance: str) -> tld.TradeLedgerEntry:
        """Late-arriving cost evidence. On a finalized entry this becomes an
        amendment; otherwise it refreshes the working record."""
        entry = self.get_entry(trade_id)
        if entry is None:
            raise LedgerStoreError("trade_not_found", trade_id)
        if entry.finalized:
            return self.amend_trade(trade_id, trade=trade, now=now,
                                    provenance=provenance,
                                    reason="late cost evidence received")
        from dataclasses import replace as _replace
        updated = _replace(entry, trade=trade, updated_at=now)
        return self._append(trade_id=trade_id,
                            event_type=tld.LedgerEventType.COST_EVIDENCE_RECEIVED,
                            occurred_at=now, recorded_at=now,
                            payload={"trade": {"_closed_trade": None,
                                               "snapshot": trade.as_dict()}},
                            provenance=provenance, entry=updated)

    # ── reads ────────────────────────────────────────────────────────────────
    def get_entry(self, trade_id: str) -> tld.TradeLedgerEntry | None:
        """The current entry, rebuilt from its append-only history (the events
        are the truth; the snapshot is a cache)."""
        events = self.history(trade_id)
        if not events:
            return None
        entry = tld.rebuild_entry(events)
        if entry is None:
            return None
        snapshot = self.get_trade(trade_id)
        if snapshot is not None and snapshot.trade is not None:
            from dataclasses import replace as _replace
            entry = _replace(entry, trade=snapshot.trade,
                             warnings=snapshot.warnings, conflicts=snapshot.conflicts,
                             reconciliation_findings=snapshot.reconciliation_findings,
                             management_history=snapshot.management_history)
        return entry

    def get_trade(self, trade_id: str) -> tld.TradeLedgerEntry | None:
        """The stored snapshot (already-projected dict rehydrated minimally)."""
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM ledger_entries WHERE trade_id=?",
                               (trade_id,)).fetchone()
        if row is None:
            return None
        return _entry_from_snapshot(self._row_to_entry(row))

    def list_trades(self, *, statuses: tuple | None = None,
                    instrument: str | None = None, scenario_id: str | None = None,
                    node_id: str | None = None, account_fingerprint: str | None = None,
                    opened_from: str | None = None, opened_to: str | None = None,
                    closed_from: str | None = None, closed_to: str | None = None,
                    limit: int = 100, offset: int = 0) -> list:
        """Deterministic ordering: newest close first, ties broken by trade id."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        for column, value in (("instrument", instrument), ("scenario_id", scenario_id),
                              ("node_id", node_id),
                              ("account_fingerprint", account_fingerprint)):
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        for column, value, op in (("opened_at", opened_from, ">="),
                                  ("opened_at", opened_to, "<="),
                                  ("closed_at", closed_from, ">="),
                                  ("closed_at", closed_to, "<=")):
            if value:
                clauses.append(f"{column} {op} ?")
                params.append(value)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        params.extend([int(limit), int(offset)])
        with self._conn() as conn:
            rows = conn.execute(
                f"SELECT * FROM ledger_entries{where} "
                "ORDER BY closed_at DESC, trade_id ASC LIMIT ? OFFSET ?",
                params).fetchall()
        return [_entry_from_snapshot(self._row_to_entry(r)) for r in rows]

    def count_trades(self, **filters) -> int:
        return len(self.list_trades(limit=100000, **filters))

    def list_open_reconstructions(self, limit: int = 200) -> list:
        return self.list_trades(statuses=(tld.TradeLedgerStatus.OBSERVED,
                                          tld.TradeLedgerStatus.RECONSTRUCTING,
                                          tld.TradeLedgerStatus.INCOMPLETE,
                                          tld.TradeLedgerStatus.READY_TO_FINALIZE),
                                limit=limit)

    def list_conflicts(self, limit: int = 200) -> list:
        return self.list_trades(statuses=(tld.TradeLedgerStatus.CONFLICTED,),
                                limit=limit)

    def history(self, trade_id: str, limit: int = 500) -> list:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM ledger_events WHERE trade_id=? "
                "ORDER BY sequence ASC LIMIT ?", (trade_id, int(limit))).fetchall()
        return [self._row_to_event(r) for r in rows]

    def rebuild_trade(self, trade_id: str) -> tld.TradeLedgerEntry | None:
        """Rebuild from the append-only history ALONE — the snapshot is never
        consulted. This is what proves the events are the source of truth."""
        events = self.history(trade_id)
        return tld.rebuild_entry(events) if events else None

    def latest_version(self, trade_id: str) -> int | None:
        entry = self.get_trade(trade_id)
        return entry.version if entry else None

    def summary(self) -> tld.LedgerSummaryTotals:
        return tld.LedgerSummaryTotals.of(self.list_trades(limit=100000))


def _entry_from_snapshot(payload: dict) -> tld.TradeLedgerEntry:
    """Rehydrate the stored projection into a `TradeLedgerEntry`. The trade is
    carried as an already-projected dict wrapper so no financial value is
    recomputed on read — the ledger reports what it recorded."""
    trade_payload = payload.get("trade")
    trade = _StoredTrade(trade_payload) if trade_payload else None
    return tld.TradeLedgerEntry(
        trade_id=payload["tradeId"], status=payload["status"],
        version=int(payload.get("version") or 1), trade=trade,
        warnings=tuple(payload.get("warnings") or ()),
        conflicts=tuple(payload.get("conflicts") or ()),
        reconciliation_findings=tuple(payload.get("reconciliationFindings") or ()),
        management_history=tuple(payload.get("managementHistory") or ()),
        observed_at=payload.get("observedAt"),
        finalized_at=payload.get("finalizedAt"),
        amended_at=payload.get("amendedAt"),
        updated_at=payload.get("updatedAt"))


class _StoredTrade:
    """A read-only view over a stored trade projection. It exposes the handful
    of attributes the store and summary need, and returns the recorded dict
    verbatim from `as_dict()` — nothing is recomputed."""

    __slots__ = ("_data",)

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", dict(data))

    def __setattr__(self, *_args):
        raise AttributeError("stored trade projections are immutable")

    def as_dict(self) -> dict:
        return dict(self._data)

    def _get(self, key):
        return self._data.get(key)

    instrument = property(lambda self: self._get("instrument"))
    side = property(lambda self: self._get("side"))
    gross_realized_pnl = property(lambda self: self._get("grossRealizedPnL"))
    net_realized_pnl = property(lambda self: self._get("netRealizedPnL"))
    exit_classification = property(lambda self: self._get("exitClassification"))
    account_currency = property(lambda self: self._get("accountCurrency"))
    outcome = property(lambda self: self._get("outcome"))

    @property
    def costs(self):
        return _Group({"total": self._get("totalCosts"),
                       "completeness": self._get("costCompleteness")})

    @property
    def risk(self):
        return _Group({"realized_r": self._get("realizedR"),
                       "completeness": self._get("riskCompleteness")})

    @property
    def timing(self):
        return _Group({"opened_at": self._get("openedAt"),
                       "closed_at": self._get("closedAt")})

    @property
    def lineage(self):
        return _Group(_lineage_fields(self._get("lineage") or {}))


def _lineage_fields(raw: dict) -> dict:
    return {"scenario_id": raw.get("scenarioId"), "node_id": raw.get("nodeId"),
            "account_fingerprint": raw.get("accountFingerprint"),
            "origin": raw.get("origin")}


class _Group:
    __slots__ = ("_data",)

    def __init__(self, data: dict):
        object.__setattr__(self, "_data", dict(data))

    def __getattr__(self, name):
        try:
            return object.__getattribute__(self, "_data")[name]
        except KeyError as exc:
            raise AttributeError(name) from exc

    def __setattr__(self, *_args):
        raise AttributeError("stored projections are immutable")
