"""L2-A — Operational Transition Log projector tests.

Verifies: incremental byte-cursor reading of cycles.jsonl (resume, >500-record
recovery, partial-trailing-line hold-back, malformed-line skip, silent
discontinuity re-baselining), edge-triggered sequence detection with silent
first-observation seeding, complete EventEntry envelopes with source-record
timestamps and deterministic replay-stable idempotency keys, the crash-safe
pending-emission checkpoint (schema 1, atomic writes, silent recovery),
the disabled-by-default enable gate, lifecycle containment, and integration
with the real shared BotEvent store (P-2 retention included).

All projector instances run against temporary directories with injected
dependencies; tick_once() is driven directly with an injected clock. Safe
under `pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import ops_journal                                                     # noqa: E402
import server                                                          # noqa: E402

NOW = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
ENVELOPE_FIELDS = {
    "eventId", "seq", "category", "code", "humanExplanation", "scenarioKey",
    "packageHash", "who", "causedBy", "before", "after", "at",
}
CODES = set(ops_journal.CODES)


def rec(i: int, error: str = "", frozen: bool = False,
        delivered: bool | None = True, **over) -> dict:
    r = {"cycle_start": f"2026-07-22T10:{i:02d}:00+00:00",
         "cycle_end": f"2026-07-22T10:{i:02d}:05+00:00",
         "duration_s": 5.0, "boundary": f"2026-07-22T09:{45 + (i % 10)}:00+00:00",
         "last_bar": None, "bars_appended": 1, "status": "ok", "intents": 0,
         "applied": 0, "blocked": 0, "skipped": 0, "reconcile_findings": [],
         "frozen": frozen, "published": ({"delivered": delivered}
                                         if delivered is not None else None),
         "error": error}
    r.update(over)
    return r


class Store:
    """Collects appended events; optionally raising; store-level key dedup."""

    def __init__(self):
        self.events: list[dict] = []
        self.keys: list[str] = []
        self.fail = False

    def append(self, event: dict, key: str):
        if self.fail:
            raise RuntimeError("injected append failure")
        if key in self.keys:
            return next(e for e, k in zip(self.events, self.keys) if k == key), True
        event = dict(event)
        event["seq"] = len(self.events) + 1
        self.events.append(event)
        self.keys.append(key)
        return event, False


@pytest.fixture()
def pj(tmp_path):
    """A projector on temp paths with a capturing store."""
    store = Store()
    src = tmp_path / "cycles.jsonl"
    proj = ops_journal.OpsJournalProjector(
        cycles_path=src, checkpoint_path=tmp_path / "ckpt.json",
        append_event=store.append, package_hash=lambda: "hash",
        interval_s=0.05, logger=logging.getLogger("ops_journal_test"))
    return SimpleNamespace(p=proj, store=store, src=src,
                           ckpt=tmp_path / "ckpt.json", tmp=tmp_path)


def write(src: Path, *records, partial: str | None = None, raw_lines: list[str] = ()):
    with src.open("a") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
        for line in raw_lines:
            fh.write(line + "\n")
        if partial is not None:
            fh.write(partial)                       # no newline — incomplete


# ── Reader and cursor ────────────────────────────────────────────────────────

def test_cold_baseline_emits_zero_events(pj):
    write(pj.src, rec(1, error="boom", frozen=True, delivered=False))  # pre-existing bad state
    s = pj.p.tick_once(NOW)
    assert pj.store.events == []                    # never back-narrates
    assert s["appended"] == 0 and s["error"] is None
    assert pj.ckpt.exists()                         # checkpoint persisted


def test_cursor_resume_exact_and_transitions(pj):
    pj.p.tick_once(NOW)                             # baseline on empty source
    write(pj.src, rec(1))                           # healthy — seeds silently
    pj.p.tick_once(NOW)
    assert pj.store.events == []
    write(pj.src, rec(2, error="E1"))               # edge -> CYCLE_ERROR
    pj.p.tick_once(NOW)
    assert [e["code"] for e in pj.store.events] == ["CYCLE_ERROR"]
    pj.p.tick_once(NOW)                             # nothing new
    assert len(pj.store.events) == 1


def test_more_than_500_unread_records(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(0))                           # seed
    pj.p.tick_once(NOW)
    records = []
    for i in range(600):                            # alternate error edges
        records.append(rec(i % 60, error="X" if i % 2 else ""))
    write(pj.src, *records)
    pj.p.tick_once(NOW)
    # record 0 is healthy (steady with the seed), records 1..599 each flip -> 599 edges
    assert len(pj.store.events) == 599              # every edge narrated, none lost
    assert len(set(pj.store.keys)) == 599           # all keys distinct


def test_partial_trailing_line_held_back_then_consumed(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)                             # seed
    half = json.dumps(rec(2, error="E"))
    write(pj.src, partial=half[: len(half) // 2])   # incomplete line
    cursor_before = json.loads(pj.ckpt.read_text())["cursor"]
    pj.p.tick_once(NOW)
    assert pj.store.events == []                    # not parsed, not emitted
    assert json.loads(pj.ckpt.read_text())["cursor"] == cursor_before  # not advanced
    with pj.src.open("a") as fh:                    # complete it later
        fh.write(half[len(half) // 2:] + "\n")
    pj.p.tick_once(NOW)
    assert [e["code"] for e in pj.store.events] == ["CYCLE_ERROR"]


def test_malformed_complete_line_skipped_without_stall(pj, caplog):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)                             # seed healthy
    write(pj.src, raw_lines=["{this is not json"])
    write(pj.src, rec(2, error="E"))                # valid record AFTER the bad line
    with caplog.at_level(logging.WARNING, logger="ops_journal_test"):
        s = pj.p.tick_once(NOW)
    assert s["skipped_lines"] == 1
    assert [e["code"] for e in pj.store.events] == ["CYCLE_ERROR"]  # later record processed
    assert any("malformed cycles line" in r.getMessage() for r in caplog.records)
    pj.p.tick_once(NOW)                             # cursor advanced past the bad line
    assert len(pj.store.events) == 1


def test_file_shrink_rebaselines_silently(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1), rec(2, error="E"))
    pj.p.tick_once(NOW)
    assert len(pj.store.events) == 1
    pj.src.write_text("")                           # shrink below cursor
    write(pj.src, rec(3, error="E2", frozen=True))
    s = pj.p.tick_once(NOW)
    assert len(pj.store.events) == 1                # ZERO new events on discontinuity
    assert s["error"] is None
    write(pj.src, rec(4, frozen=True))              # steady vs new baseline
    pj.p.tick_once(NOW)
    assert len(pj.store.events) == 1


def test_replacement_identity_mismatch_rebaselines_silently(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)
    # replace the file with same-or-longer content whose last line differs
    pj.src.write_text(json.dumps(rec(9, error="XX")) + "\n" +
                      json.dumps(rec(8, error="YY", frozen=True)) + "\n")
    s = pj.p.tick_once(NOW)
    assert pj.store.events == [] or all(e["code"] != "CYCLE_ERROR" for e in pj.store.events)
    assert s["appended"] == 0                       # no back-narration of replacement


# ── Detector ─────────────────────────────────────────────────────────────────

def test_all_six_edges_and_steady_silence(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))                           # seed all kinds
    pj.p.tick_once(NOW)
    write(pj.src,
          rec(2, error="E"),                        # CYCLE_ERROR
          rec(3, error="E"),                        # steady — nothing
          rec(4),                                   # CYCLE_ERROR_CLEARED
          rec(5, frozen=True),                      # EXECUTION_FROZEN
          rec(6, frozen=True),                      # steady
          rec(7),                                   # EXECUTION_UNFROZEN
          rec(8, delivered=False),                  # PUBLISH_DELIVERY_LOST
          rec(9, delivered=False),                  # steady
          rec(10),                                  # PUBLISH_DELIVERY_RESTORED
          )
    pj.p.tick_once(NOW)
    assert [e["code"] for e in pj.store.events] == [
        "CYCLE_ERROR", "CYCLE_ERROR_CLEARED", "EXECUTION_FROZEN",
        "EXECUTION_UNFROZEN", "PUBLISH_DELIVERY_LOST", "PUBLISH_DELIVERY_RESTORED"]


def test_multiple_edges_in_one_tick_not_collapsed(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)
    write(pj.src, rec(2, error="A"), rec(3), rec(4, error="B"))  # healthy/err/healthy/err
    pj.p.tick_once(NOW)
    assert [e["code"] for e in pj.store.events] == [
        "CYCLE_ERROR", "CYCLE_ERROR_CLEARED", "CYCLE_ERROR"]     # every edge, in order


def test_absent_publish_state_never_invents_transition(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1, delivered=None), rec(2, delivered=None))  # no delivery info
    pj.p.tick_once(NOW)
    write(pj.src, rec(3, delivered=False))          # FIRST comparable state — seeds
    pj.p.tick_once(NOW)
    assert pj.store.events == []
    write(pj.src, rec(4, delivered=True))           # genuine edge after seed
    pj.p.tick_once(NOW)
    assert [e["code"] for e in pj.store.events] == ["PUBLISH_DELIVERY_RESTORED"]


# ── Envelope and identity ────────────────────────────────────────────────────

def test_envelope_complete_at_from_source_and_now_independent(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)
    write(pj.src, rec(2, error="E"))
    later = datetime(2027, 1, 1, tzinfo=timezone.utc)
    pj.p.tick_once(later)                           # advanced clock
    ev = pj.store.events[0]
    assert set(ev.keys()) == ENVELOPE_FIELDS
    assert ev["category"] == "operational" and ev["code"] in CODES
    assert ev["at"] == rec(2)["cycle_end"]          # source record time, not NOW
    assert ev["who"] == "system" and ev["packageHash"] == "hash"
    assert ev["causedBy"].startswith("l2_projector:")
    assert ev["before"] == {"error_present": False}
    assert ev["after"]["error_present"] is True and "source_identity" in ev["after"]
    # advancing NOW alone produces nothing
    n = len(pj.store.events)
    pj.p.tick_once(datetime(2028, 1, 1, tzinfo=timezone.utc))
    assert len(pj.store.events) == n


def test_replay_identical_key_recurrence_distinct_key(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)
    write(pj.src, rec(2, error="E"))
    pj.p.tick_once(NOW)
    key1 = pj.store.keys[0]
    assert key1.startswith("l2|CYCLE_ERROR|")
    # crash replay: rewind checkpoint to the pre-record state -> same key -> dedup
    ck = json.loads(pj.ckpt.read_text())
    pre = json.loads(pj.ckpt.read_text())
    # simulate by reloading a stale checkpoint: reset in-memory state and cursor
    stale = pj.p._fresh_state()
    stale["cursor"] = ck["cursor"] - (len(json.dumps(rec(2, error="E"))) + 1)
    stale["detector"] = {"error_present": False, "frozen": False, "delivered": True}
    pj.ckpt.write_text(json.dumps(stale))
    pj.p._state = None                              # force reload (restart sim)
    pj.p.tick_once(NOW)
    assert pj.store.keys.count(key1) == 1           # deduplicated, no second row
    assert len(pj.store.events) == 1
    # genuine recurrence: clear then re-error -> different record -> new key
    write(pj.src, rec(3), rec(4, error="E"))
    pj.p.tick_once(NOW)
    new_keys = [k for k in pj.store.keys if "CYCLE_ERROR|" in k and "CLEARED" not in k]
    assert len(set(new_keys)) == 2                  # distinct identity


# ── Checkpoint ───────────────────────────────────────────────────────────────

def test_checkpoint_schema_and_silent_recovery_paths(pj, caplog):
    pj.p.tick_once(NOW)
    ck = json.loads(pj.ckpt.read_text())
    assert ck["schema_version"] == 1
    assert set(ck) >= {"schema_version", "cursor", "last_record_span",
                       "last_record_identity", "detector", "pending"}
    for corrupt in ["{not json", json.dumps({"schema_version": 99}),
                    json.dumps(["wrong shape"])]:
        pj.ckpt.write_text(corrupt)
        pj.p._state = None
        with caplog.at_level(logging.WARNING, logger="ops_journal_test"):
            s = pj.p.tick_once(NOW)
        assert s["error"] is None and pj.store.events == []   # silent re-baseline
    # deletion: narration-only restart
    pj.ckpt.unlink()
    pj.p._state = None
    assert pj.p.tick_once(NOW)["error"] is None
    assert pj.store.events == []


def test_checkpoint_write_is_atomic(pj, monkeypatch):
    pj.p.tick_once(NOW)
    good = pj.ckpt.read_text()
    real_replace = ops_journal.os.replace

    def fail_replace(a, b):
        raise OSError("injected replace failure")

    monkeypatch.setattr(ops_journal.os, "replace", fail_replace)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)                             # save fails, contained
    monkeypatch.setattr(ops_journal.os, "replace", real_replace)
    assert pj.ckpt.read_text() == good              # old checkpoint intact, not corrupted
    assert not pj.ckpt.with_name(pj.ckpt.name + ".tmp").exists()  # temp cleaned


def test_pending_roundtrip_drain_and_append_failure(pj):
    pj.p.tick_once(NOW)
    write(pj.src, rec(1))
    pj.p.tick_once(NOW)
    pj.store.fail = True                            # append will fail
    write(pj.src, rec(2, error="E"))
    s = pj.p.tick_once(NOW)
    assert s["error"] is not None                   # tick failed safely
    ck = json.loads(pj.ckpt.read_text())
    assert len(ck["pending"]) == 1                  # frozen payload+key retained
    item = ck["pending"][0]
    assert set(item) == {"event", "idempotency_key", "source_identity"}
    assert item["event"]["at"] == rec(2)["cycle_end"]
    frozen_key = item["idempotency_key"]
    pj.store.fail = False
    pj.p._state = None                              # restart sim: reload from disk
    pj.p.tick_once(NOW)                             # drains pending FIRST
    assert pj.store.keys == [frozen_key]            # exact frozen key reused
    assert len(pj.store.events) == 1
    assert json.loads(pj.ckpt.read_text())["pending"] == []


# ── Lifecycle ────────────────────────────────────────────────────────────────

def test_start_stop_idempotent_bounded_daemon(pj):
    pj.p.start()
    t1 = pj.p._thread
    pj.p.start()                                    # idempotent
    assert pj.p._thread is t1 and t1.daemon is True
    pj.p.stop(timeout_s=2)
    assert pj.p._thread is None
    pj.p.stop(timeout_s=2)                          # idempotent, safe repeated
    assert threading.active_count() < 40            # no thread leak


def test_tick_exception_contained(pj, monkeypatch):
    monkeypatch.setattr(pj.p, "_load_checkpoint",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    pj.p._state = None
    s = pj.p.tick_once(NOW)                         # contained, no raise
    assert s["error"] is not None


def test_disabled_flag_no_thread_no_checkpoint(monkeypatch):
    monkeypatch.delenv("OPS_JOURNAL_ENABLED", raising=False)
    assert ops_journal.env_flag(None) is False
    assert ops_journal.env_flag("0") is False and ops_journal.env_flag("false") is False
    assert ops_journal.env_flag("1") and ops_journal.env_flag("TRUE") and ops_journal.env_flag("on")
    before_threads = threading.active_count()
    with TestClient(server.app):                    # lifespan runs; flag disabled
        pass
    assert server._ops_journal._thread is None      # no projector thread
    assert threading.active_count() <= before_threads + 1
    assert not (BACKEND_DIR / "ops_journal_checkpoint.json").exists()  # no file


def test_enabled_lifecycle_uses_temp_paths_and_cleans_up(tmp_path, monkeypatch):
    store = Store()
    proj = ops_journal.OpsJournalProjector(
        cycles_path=tmp_path / "cycles.jsonl", checkpoint_path=tmp_path / "ck.json",
        append_event=store.append, interval_s=0.05)
    monkeypatch.setattr(server, "_ops_journal", proj)
    monkeypatch.setattr(server, "OPS_JOURNAL_ENABLED", True)
    with TestClient(server.app) as c:
        assert proj._thread is not None and proj._thread.is_alive()
        assert c.get("/api/events").status_code == 200   # API usable alongside
    assert proj._thread is None                     # shutdown stopped it
    assert not (BACKEND_DIR / "ops_journal_checkpoint.json").exists()


# ── Integration-lite (real shared store + P-2 retention) ─────────────────────

@pytest.fixture()
def real_store(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())
    return tmp_path / "events.db"


def command_event(i):
    return {"eventId": f"ev_CMD{i:04d}", "seq": 0, "category": "system", "code": "CMD",
            "humanExplanation": "cmd", "scenarioKey": None, "packageHash": "h",
            "who": "op", "causedBy": "c", "before": None, "after": None, "at": "T"}


def test_real_store_shared_seq_and_retention(tmp_path, real_store, monkeypatch):
    src = tmp_path / "cycles.jsonl"
    proj = ops_journal.OpsJournalProjector(
        cycles_path=src, checkpoint_path=tmp_path / "ck.json",
        append_event=lambda ev, k: server._append_event(ev, k))
    proj.tick_once(NOW)
    write(src, rec(1))
    proj.tick_once(NOW)
    server._append_event(command_event(1), None)    # interleaved command event
    write(src, rec(2, error="E"))
    proj.tick_once(NOW)
    conn = sqlite3.connect(real_store)
    rows = [json.loads(r[0]) for r in conn.execute("SELECT payload FROM bot_events ORDER BY seq")]
    conn.close()
    assert [r["code"] for r in rows] == ["CMD", "CYCLE_ERROR"]
    assert rows[1]["seq"] == rows[0]["seq"] + 1     # shared monotonic sequence
    # crash replay through the REAL store dedups
    stale = proj._fresh_state()
    stale["cursor"] = json.loads((tmp_path / "ck.json").read_text())["cursor"] - (
        len(json.dumps(rec(2, error="E"))) + 1)
    stale["detector"] = {"error_present": False, "frozen": False, "delivered": True}
    (tmp_path / "ck.json").write_text(json.dumps(stale))
    proj._state = None
    proj.tick_once(NOW)
    conn = sqlite3.connect(real_store)
    n = conn.execute("SELECT COUNT(*) FROM bot_events").fetchone()[0]
    conn.close()
    assert n == 2                                   # P-2 store deduplicated the replay
    # retention pruning old L2 events does not disturb the projector cursor
    monkeypatch.setattr(server, "EVENTS_RETENTION_MAX", 1)
    server._append_event(command_event(2), None)    # triggers prune of older rows
    cursor_before = json.loads((tmp_path / "ck.json").read_text())["cursor"]
    write(src, rec(3))
    proj.tick_once(NOW)                             # CYCLE_ERROR_CLEARED, post-prune
    assert json.loads((tmp_path / "ck.json").read_text())["cursor"] > cursor_before
    assert not (BACKEND_DIR / "events.db").exists() # no second/stray database


def test_exactly_four_append_event_production_call_sites():
    src = (BACKEND_DIR / "server.py").read_text()
    calls = [m for m in re.finditer(r"_append_event\(", src)
             if not src[max(0, m.start() - 4):m.start()].endswith("def ")]
    assert len(calls) == 4, (
        "expected exactly 4 production _append_event call sites "
        "(command, broker-sync, LIVE_STATUS, L2 projector wiring); "
        f"found {len(calls)}")
