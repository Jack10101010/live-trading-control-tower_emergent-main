"""P-2 — BotEvent retention and pagination tests.

Verifies: fixed-row-count retention pruned atomically inside _append_event;
sequence pagination (since_seq/limit) on GET /api/events with the default
response preserved; indexed seq-filtered reads for the realtime delta path;
malformed-payload tolerance on ordinary reads with once-per-seq warning
suppression; strict integrity behaviour for malformed idempotency matches
(dedup never degrades into replay); SQL COUNT for the event count; fixture
interleaving correctness; and the producer/regression boundaries.

Fast, in-process; temp events DB + isolated module state per test — safe under
`pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import server                                                          # noqa: E402

client = TestClient(server.app, raise_server_exceptions=False)

FIXTURE_SEQS = sorted(e["seq"] for e in server.WORLD.get("events", []))  # 3 frozen events


def mk(i: int) -> dict:
    return {"eventId": f"ev_TEST{i:05d}", "seq": 0, "category": "system",
            "code": "TEST_EVENT", "humanExplanation": f"test event {i}",
            "scenarioKey": None, "packageHash": "h", "who": "system",
            "causedBy": "p2-test", "before": None, "after": None,
            "at": "2026-07-22T00:00:00Z"}


def raw_insert(db: Path, seq: int, payload: str, idem: str | None = None,
               event_id: str | None = None) -> None:
    conn = sqlite3.connect(db)
    try:
        conn.execute("""CREATE TABLE IF NOT EXISTS bot_events (
            seq INTEGER PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT UNIQUE, payload TEXT NOT NULL)""")
        conn.execute("INSERT INTO bot_events VALUES (?, ?, ?, ?)",
                     (seq, event_id or f"ev_RAW{seq}", idem, payload))
        conn.commit()
    finally:
        conn.close()


def raw_event(seq: int, **over) -> str:
    ev = mk(seq)
    ev["seq"] = seq
    ev["eventId"] = f"ev_RAW{seq}"
    ev.update(over)
    return json.dumps(ev)


def stored_seqs(db: Path) -> list[int]:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [r[0] for r in conn.execute("SELECT seq FROM bot_events ORDER BY seq")]
    finally:
        conn.close()


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    db = tmp_path / "events.db"
    monkeypatch.setattr(server, "EVENTS_DB_PATH", db)
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())
    return SimpleNamespace(db=db)


@pytest.fixture()
def small_cap(monkeypatch):
    monkeypatch.setattr(server, "EVENTS_RETENTION_MAX", 5)
    return 5


@pytest.fixture()
def sql_log(monkeypatch):
    """Record every (sql, params) executed through _events_db connections."""
    log: list[tuple[str, tuple]] = []
    real = server._events_db

    class Wrap:
        def __init__(self, c): self._c = c
        def execute(self, sql, *a):
            log.append((" ".join(sql.split()), a[0] if a else ()))
            return self._c.execute(sql, *a)
        def commit(self): return self._c.commit()
        def rollback(self): return self._c.rollback()
        def close(self): return self._c.close()

    monkeypatch.setattr(server, "_events_db", lambda: Wrap(real()))
    return log


# ── 1. Retention cap ─────────────────────────────────────────────────────────

def test_production_retention_constant_is_10000():
    assert server.EVENTS_RETENTION_MAX == 10000
    assert server.EVENTS_PAGE_LIMIT_MAX == 1000


def test_retention_keeps_only_newest_capped_rows(iso, small_cap):
    for i in range(8):
        server._append_event(mk(i), None)
    seqs = stored_seqs(iso.db)
    base = server._FIXTURE_MAX_SEQ
    assert len(seqs) == 5                                    # cap enforced
    assert seqs == [base + 4, base + 5, base + 6, base + 7, base + 8]  # newest, unrenumbered
    assert server._max_seq() == base + 8                     # MAX(seq) monotonic
    assert len(server.WORLD.get("events", [])) == 3          # fixtures untouched


# ── 2. Atomic insert and prune ───────────────────────────────────────────────

def test_insert_and_prune_atomic_rollback(iso, small_cap, monkeypatch):
    for i in range(5):
        server._append_event(mk(i), None)
    before = stored_seqs(iso.db)

    real = server._events_db

    class FailPrune:
        def __init__(self, c): self._c = c
        def execute(self, sql, *a):
            if sql.lstrip().startswith("DELETE"):
                raise sqlite3.OperationalError("injected prune failure")
            return self._c.execute(sql, *a)
        def commit(self): return self._c.commit()
        def rollback(self): return self._c.rollback()
        def close(self): return self._c.close()

    monkeypatch.setattr(server, "_events_db", lambda: FailPrune(real()))
    with pytest.raises(sqlite3.OperationalError):
        server._append_event(mk(99), None)
    monkeypatch.setattr(server, "_events_db", real)
    assert stored_seqs(iso.db) == before                     # insert rolled back too

    ev, dedup = server._append_event(mk(100), None)          # retry succeeds
    assert dedup is False
    assert stored_seqs(iso.db)[-1] == ev["seq"]
    assert len(stored_seqs(iso.db)) == 5                     # cap re-enforced


# ── 3. Deduplicated replay ───────────────────────────────────────────────────

def test_dedup_replay_no_insert_no_prune(iso, small_cap, sql_log):
    for i in range(5):
        server._append_event(mk(i), f"key-{i}")
    before = stored_seqs(iso.db)
    sql_log.clear()
    ev, dedup = server._append_event(mk(0), "key-0")
    assert dedup is True and ev["eventId"] == f"ev_TEST{0:05d}"
    assert stored_seqs(iso.db) == before                     # no extra row, seqs unchanged
    assert not any(s.startswith("DELETE") for s, _ in sql_log)   # no unnecessary pruning
    assert not any(s.startswith("INSERT") for s, _ in sql_log)


# ── 4. Pruned idempotency key ────────────────────────────────────────────────

def test_pruned_idempotency_key_no_longer_deduplicates(iso, small_cap):
    """Accepted P-2 consequence: retention removes the row AND its idempotency
    key, so a key older than the retained window deduplicates nothing."""
    for i in range(8):
        server._append_event(mk(i), f"key-{i}")
    assert server._find_event_by_idempotency("key-0") is None    # pruned with its row
    ev, dedup = server._append_event(mk(50), "key-0")            # re-append: new row
    assert dedup is False and ev["seq"] == server._FIXTURE_MAX_SEQ + 9


# ── 5. Default snapshot compatibility ────────────────────────────────────────

def test_default_snapshot_shape_order_and_pair_filter(iso):
    a, _ = server._append_event(mk(1), None)
    b, _ = server._append_event(mk(2), None)
    r = client.get("/api/events")
    assert r.status_code == 200
    evs = r.json()
    assert [e["seq"] for e in evs] == FIXTURE_SEQS + [a["seq"], b["seq"]]  # exact merge
    assert all(isinstance(e, dict) and "eventId" in e for e in evs)
    # pair filtering unchanged: scenarioKey None passes; mismatched prefix filtered
    scoped, _ = server._append_event({**mk(3), "scenarioKey": "GBPUSD:x"}, None)
    r2 = client.get("/api/events", params={"pair": "EURUSD"})
    assert scoped["seq"] not in [e["seq"] for e in r2.json()]
    assert a["seq"] in [e["seq"] for e in r2.json()]


# ── 6. since_seq pagination ──────────────────────────────────────────────────

def test_since_seq_semantics(iso):
    a, _ = server._append_event(mk(1), None)
    b, _ = server._append_event(mk(2), None)
    head = b["seq"]

    def seqs(**params):
        r = client.get("/api/events", params=params)
        assert r.status_code == 200
        return [e["seq"] for e in r.json()]

    assert seqs(since_seq=0) == FIXTURE_SEQS + [a["seq"], b["seq"]]
    assert seqs(since_seq=FIXTURE_SEQS[0] - 10) == FIXTURE_SEQS + [a["seq"], b["seq"]]
    assert seqs(since_seq=a["seq"]) == [b["seq"]]            # strictly greater
    assert seqs(since_seq=FIXTURE_SEQS[1]) == [FIXTURE_SEQS[2], a["seq"], b["seq"]]
    assert seqs(since_seq=head) == []                        # equal to head
    assert seqs(since_seq=head + 1000) == []                 # beyond head, no stale field
    assert client.get("/api/events", params={"since_seq": -1}).status_code == 422


# ── 7. limit validation and boundaries ───────────────────────────────────────

def test_limit_semantics_and_validation(iso):
    for i in range(4):
        server._append_event(mk(i), None)
    full = [e["seq"] for e in client.get("/api/events").json()]

    assert [e["seq"] for e in client.get("/api/events", params={"limit": 1}).json()] == full[:1]
    n = len(full)
    assert [e["seq"] for e in client.get("/api/events", params={"limit": n}).json()] == full
    assert [e["seq"] for e in client.get("/api/events", params={"limit": 1000}).json()] == full
    assert client.get("/api/events", params={"limit": 0}).status_code == 422
    assert client.get("/api/events", params={"limit": -5}).status_code == 422
    assert client.get("/api/events", params={"limit": 1001}).status_code == 422


# ── 7b. D1: pair + limit rejected; each alone still works ────────────────────

def test_pair_alone_and_limit_alone_succeed_but_combination_rejected(iso):
    a, _ = server._append_event(mk(1), None)
    scoped, _ = server._append_event({**mk(2), "scenarioKey": "GBPUSD:x"}, None)

    r_pair = client.get("/api/events", params={"pair": "EURUSD"})
    assert r_pair.status_code == 200
    seqs = [e["seq"] for e in r_pair.json()]
    assert a["seq"] in seqs and scoped["seq"] not in seqs    # pair alone: filters

    r_limit = client.get("/api/events", params={"limit": 2})
    assert r_limit.status_code == 200
    assert [e["seq"] for e in r_limit.json()] == FIXTURE_SEQS[:2]  # limit alone: pages

    r_both = client.get("/api/events", params={"pair": "EURUSD", "limit": 2})
    assert r_both.status_code == 422                         # combination rejected (D1)
    assert "pair" in json.dumps(r_both.json())


# ── 8. Fixture and stored interleaving (mandatory) ───────────────────────────

def test_fixture_stored_interleaving_pages_exactly(iso):
    lo, mid, hi = FIXTURE_SEQS
    below, between, above = lo - 50, mid + 3, hi + 500
    for s in (below, between, above):
        raw_insert(iso.db, s, raw_event(s))
    expected = sorted(FIXTURE_SEQS + [below, between, above])

    def seqs(**params):
        return [e["seq"] for e in client.get("/api/events", params=params).json()]

    assert seqs() == expected                                # globally ascending
    assert seqs(since_seq=mid) == [s for s in expected if s > mid]  # both kinds filtered
    assert seqs(limit=2) == expected[:2]                     # first N of MERGED stream
    assert seqs(limit=4) == expected[:4]
    # multi-page walk reconstructs the exact stream, no dupes, no omissions
    walked, cursor = [], 0
    while True:
        page = seqs(since_seq=cursor, limit=2)
        if not page:
            break
        walked += page
        cursor = page[-1]
    assert walked == expected


# ── 9. Live endpoint indexed delta behaviour ─────────────────────────────────

def test_live_endpoint_contract_and_indexed_reads(iso, sql_log):
    a, _ = server._append_event(mk(1), None)
    b, _ = server._append_event(mk(2), None)
    sql_log.clear()
    r = client.get("/api/events/live", params={"since": a["seq"], "timeout": 1})
    body = r.json()
    assert set(body) == {"events", "seq", "head", "stale"}   # shape unchanged
    assert [e["seq"] for e in body["events"]] == [b["seq"]]  # strictly > cursor
    assert body["seq"] == b["seq"] and body["stale"] is False
    stored_reads = [(s, p) for s, p in sql_log if "WHERE seq > ?" in s]
    assert stored_reads and all(p[0] == a["seq"] for p in [p for _, p in stored_reads])
    assert not any("ORDER BY seq ASC" in s and "WHERE" not in s for s, _ in sql_log)
    # stale semantics unchanged: cursor beyond head returns immediately
    r2 = client.get("/api/events/live", params={"since": b["seq"] + 999, "timeout": 5})
    assert r2.json()["stale"] is True and r2.json()["events"] == []


# ── 10. Malformed ordinary row ───────────────────────────────────────────────

def test_malformed_row_skipped_logged_once(iso, caplog):
    a, _ = server._append_event(mk(1), None)
    bad_seq = a["seq"] + 1
    raw_insert(iso.db, bad_seq, "{not json at all")
    raw_insert(iso.db, bad_seq + 1, raw_event(bad_seq + 1))

    with caplog.at_level(logging.WARNING, logger="server"):
        for _ in range(3):                                   # repeated polling
            r = client.get("/api/events")
            assert r.status_code == 200
    seqs = [e["seq"] for e in r.json()]
    assert a["seq"] in seqs and bad_seq + 1 in seqs and bad_seq not in seqs
    assert seqs == sorted(seqs)
    warnings = [rec for rec in caplog.records if str(bad_seq) in rec.getMessage()]
    assert len(warnings) == 1                                # once-per-seq suppression
    # live endpoint also survives the malformed row
    r = client.get("/api/events/live", params={"since": a["seq"], "timeout": 1})
    assert r.status_code == 200
    assert [e["seq"] for e in r.json()["events"]] == [bad_seq + 1]


# ── 10b. D2: invalid seq values are malformed, never 500 ─────────────────────

def test_invalid_seq_payloads_skipped_never_500(iso, caplog):
    a, _ = server._append_event(mk(1), None)
    base = a["seq"]
    bad_rows = {
        base + 1: raw_event(base + 1).replace(f'"seq": {base + 1}', '"seq": "not-an-int"'),
        base + 2: raw_event(base + 2).replace(f'"seq": {base + 2}', '"seq": 5.5'),
        base + 3: raw_event(base + 3).replace(f'"seq": {base + 3}', '"seq": true'),
        base + 4: raw_event(base + 4).replace(f'"seq": {base + 4}', f'"seq": {base + 999}'),
    }
    for s, payload in bad_rows.items():
        raw_insert(iso.db, s, payload)                       # string / float / bool / mismatch
    tail, _ = server._append_event(mk(2), None)              # valid row after the bad block

    with caplog.at_level(logging.WARNING, logger="server"):
        r = client.get("/api/events")
    assert r.status_code == 200                              # never 500 (D2)
    seqs = [e["seq"] for e in r.json()]
    assert a["seq"] in seqs and tail["seq"] in seqs          # valid neighbours survive
    assert not any(s in seqs for s in bad_rows)              # none reach the API output
    assert seqs == sorted(seqs)                              # ordering intact
    assert all(isinstance(s, int) and not isinstance(s, bool) for s in seqs)
    for s in bad_rows:                                       # one warning per bad seq
        assert sum(1 for rec in caplog.records if f"seq={s}" in rec.getMessage()) == 1
    # live endpoint also survives and skips them
    r2 = client.get("/api/events/live", params={"since": a["seq"], "timeout": 1})
    assert r2.status_code == 200
    assert [e["seq"] for e in r2.json()["events"]] == [tail["seq"]]


# ── 11. Malformed matching idempotency row (mandatory) ───────────────────────

def test_malformed_idempotency_match_raises_not_replays(iso, caplog):
    raw_insert(iso.db, 200001, "garbage-payload", idem="idem-hurt")
    before = stored_seqs(iso.db)
    with caplog.at_level(logging.ERROR, logger="server"):
        with pytest.raises(server.EventStoreIntegrityError):
            server._find_event_by_idempotency("idem-hurt")   # never "not found"
        with pytest.raises(server.EventStoreIntegrityError):
            server._append_event(mk(9), "idem-hurt")         # action NOT re-executed
    assert stored_seqs(iso.db) == before                     # no new row
    assert any("200001" in rec.getMessage() for rec in caplog.records)
    # absent keys still behave normally
    assert server._find_event_by_idempotency("never-seen") is None


# ── 12. Count path ───────────────────────────────────────────────────────────

def test_event_count_sql_and_includes_malformed(iso, sql_log):
    server._append_event(mk(1), None)
    raw_insert(iso.db, 300000, "malformed!")
    sql_log.clear()
    assert server._event_count() == 3 + 2                    # fixtures + stored (incl. bad row)
    assert any(s.startswith("SELECT COUNT(*)") for s, _ in sql_log)
    assert not any("SELECT seq, payload" in s for s, _ in sql_log)   # no payload decode


# ── 13. Empty store ──────────────────────────────────────────────────────────

def test_empty_store_fixture_backed_model(iso):
    r = client.get("/api/events")
    assert [e["seq"] for e in r.json()] == FIXTURE_SEQS
    assert server._event_count() == 3
    assert [e["seq"] for e in client.get(
        "/api/events", params={"since_seq": FIXTURE_SEQS[1]}).json()] == [FIXTURE_SEQS[2]]
    assert client.get("/api/events", params={"limit": 2}).json()[-1]["seq"] == FIXTURE_SEQS[1]
    r2 = client.get("/api/events/live", params={"since": FIXTURE_SEQS[2] + 1, "timeout": 5})
    assert r2.json()["stale"] is True and r2.json()["head"] == FIXTURE_SEQS[2]


# ── 14. Existing oversized store ─────────────────────────────────────────────

def test_oversized_store_pruned_only_on_append(iso, small_cap):
    base = server._FIXTURE_MAX_SEQ
    for i in range(1, 11):                                   # 10 rows > cap of 5
        raw_insert(iso.db, base + i, raw_event(base + i))
    client.get("/api/events")                                # reads do not prune
    server._event_count()
    assert len(stored_seqs(iso.db)) == 10
    ev, _ = server._append_event(mk(999), None)              # one append prunes the excess
    seqs = stored_seqs(iso.db)
    assert len(seqs) == 5
    assert seqs[-1] == ev["seq"] and seqs == sorted(seqs)


# ── 15. Regression boundaries ────────────────────────────────────────────────

def test_exactly_four_append_event_production_call_sites():
    src = (BACKEND_DIR / "server.py").read_text()
    calls = [m for m in re.finditer(r"_append_event\(", src)
             if not src[max(0, m.start() - 4):m.start()].endswith("def ")]
    assert len(calls) == 4  # command, broker-sync, LIVE_STATUS, L2 projector wiring


def test_no_stray_events_db_in_repo():
    assert not (BACKEND_DIR / "events.db").exists()


# ── 16. Performance sanity (structural, not timing) ──────────────────────────

def test_reads_are_seq_bound_and_prune_is_indexed(iso, small_cap, sql_log):
    for i in range(5):
        server._append_event(mk(i), None)
    # pruning SQL is indexed on seq, no payload load
    prunes = [s for s, _ in sql_log if s.startswith("DELETE")]
    assert prunes and all("seq <=" in s and "payload" not in s for s in prunes)
    # small delta decodes only the delta rows, not the whole retained store
    decoded = []
    real_decode = server._decode_event_row
    server._decode_event_row = lambda seq, p: decoded.append(seq) or real_decode(seq, p)
    try:
        head = server._max_seq()
        client.get("/api/events/live", params={"since": head - 1, "timeout": 1})
    finally:
        server._decode_event_row = real_decode
    assert len(decoded) <= 2                                 # delta-sized, not store-sized
    # paged snapshot read uses SQL LIMIT
    sql_log.clear()
    client.get("/api/events", params={"limit": 1})
    assert any("LIMIT ?" in s for s, _ in sql_log)
