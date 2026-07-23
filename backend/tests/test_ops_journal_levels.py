"""L2-B — level-derived operational transition narration tests.

Verifies: two-tick debounce confirmation with persisted windows (restart
continues, not restarts), kill-file immediate confirmation, unknown silence
with the SOURCES_DEGRADED/RESTORED aggregate and post-restoration reseeding,
frozen pending-emission lifecycle for level events (confirmation timestamp and
key never regenerated), crash replay dedup against both a capturing store and
the real P-2 store, intervention_required exclusion, provider-failure
containment, runtime-reset independence, within-tick ordering (sequence before
level), monotonic shared seq, and retention compatibility.

All projectors run on temporary paths with injected clocks and a scripted
level provider. Safe under `pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import ops_journal                                                     # noqa: E402
import ops_status                                                      # noqa: E402
import server                                                          # noqa: E402
from fastapi.testclient import TestClient                              # noqa: E402

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def T(n: int) -> datetime:
    return T0 + timedelta(seconds=10 * n)


ALL_OK = {"liveness": True, "cycle_heartbeat": True, "cycles": True,
          "feed_heartbeat": True, "runner_state": True, "publish_payload": True,
          "kill_file": True}


def model(aliveness="ALIVE", freshness="FRESH", polling=True, kill=False,
          available=None, intervention=False):
    return {"process": {"aliveness": aliveness},
            "cycle": {"cycle_freshness": freshness},
            "data_feed": {"polling_healthy": polling},
            "attention": {"kill_file_present": kill,
                          "intervention_required": intervention},
            "meta": {"sources_available": dict(available if available is not None else ALL_OK)}}


class Store:
    def __init__(self):
        self.events, self.keys = [], []
        self.fail = False

    def append(self, event, key):
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
    store = Store()
    holder = {"m": model()}
    proj = ops_journal.OpsJournalProjector(
        cycles_path=tmp_path / "cycles.jsonl", checkpoint_path=tmp_path / "ck.json",
        append_event=store.append, package_hash=lambda: "hash",
        interval_s=0.05, logger=logging.getLogger("l2b_test"),
        level_provider=lambda now: holder["m"])
    return SimpleNamespace(p=proj, store=store, holder=holder,
                           ckpt=tmp_path / "ck.json", src=tmp_path / "cycles.jsonl",
                           tmp=tmp_path)


def codes(pj):
    return [e["code"] for e in pj.store.events]


def seed(pj, n=1):
    """Baseline + first-observation seed (silent)."""
    for i in range(n + 1):
        pj.p.tick_once(T(i))
    assert pj.store.events == []
    return n + 1


# ── Debounce and confirmation ────────────────────────────────────────────────

def test_one_tick_blip_is_silent_two_ticks_confirm(pj):
    t = seed(pj)
    pj.holder["m"] = model(aliveness="UNAVAILABLE")
    pj.p.tick_once(T(t))                            # observation 1 — window open
    assert pj.store.events == []
    pj.holder["m"] = model()                        # blip reverts
    pj.p.tick_once(T(t + 1))                        # window dies (steady vs confirmed)
    pj.holder["m"] = model(aliveness="UNAVAILABLE")
    pj.p.tick_once(T(t + 2))                        # observation 1 again
    assert pj.store.events == []
    pj.p.tick_once(T(t + 3))                        # observation 2 — confirmed
    assert codes(pj) == ["NODE_UNAVAILABLE"]
    pj.holder["m"] = model()                        # recovery, also debounced
    pj.p.tick_once(T(t + 4))
    assert len(pj.store.events) == 1
    pj.p.tick_once(T(t + 5))
    assert codes(pj) == ["NODE_UNAVAILABLE", "NODE_RECOVERED"]


def test_stale_and_computing_are_silent_intermediates(pj):
    t = seed(pj)
    for i in range(4):
        pj.holder["m"] = model(aliveness="STALE", freshness="COMPUTING")
        pj.p.tick_once(T(t + i))
    assert pj.store.events == []                    # neither narrates, ever


def test_restart_during_debounce_continues_window(pj):
    t = seed(pj)
    pj.holder["m"] = model(polling=False)
    pj.p.tick_once(T(t))                            # observation 1 persisted
    ck = json.loads(pj.ckpt.read_text())
    assert ck["debounce"]["feed_unhealthy"] == {"state": True, "count": 1}
    pj.p._state = None                              # restart: reload from disk
    pj.p.tick_once(T(t + 1))                        # observation 2 -> confirmed
    assert codes(pj) == ["FEED_UNHEALTHY"]


def test_kill_file_confirms_immediately(pj):
    t = seed(pj)
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(t))                            # no debounce
    assert codes(pj) == ["KILL_FILE_ENGAGED"]
    pj.holder["m"] = model(kill=False)
    pj.p.tick_once(T(t + 1))
    assert codes(pj) == ["KILL_FILE_ENGAGED", "KILL_FILE_CLEARED"]


def test_steady_state_and_now_advance_are_silent(pj):
    t = seed(pj)
    for i in range(10):                             # unchanged model, advancing clock
        pj.p.tick_once(T(t + i))
    assert pj.store.events == []


# ── Unknown handling and SOURCES aggregate ───────────────────────────────────

def test_unknown_fields_silent_sources_pair_and_reseed(pj):
    t = seed(pj)
    degraded = model(aliveness="unknown", freshness="unknown", polling="unknown",
                     kill="unknown", available={**ALL_OK, "liveness": False,
                                                "cycle_heartbeat": False})
    pj.holder["m"] = degraded
    pj.p.tick_once(T(t)); pj.p.tick_once(T(t + 1))  # two ticks -> confirmed aggregate
    assert codes(pj) == ["SOURCES_DEGRADED"]        # no per-field unknown events
    ev = pj.store.events[0]
    assert ev["after"]["unavailable_sources"] == ["cycle_heartbeat", "liveness"]
    # recovery, but with a DIFFERENT post-outage world (node now unavailable)
    pj.holder["m"] = model(aliveness="UNAVAILABLE")
    pj.p.tick_once(T(t + 2)); pj.p.tick_once(T(t + 3))
    assert codes(pj) == ["SOURCES_DEGRADED", "SOURCES_RESTORED"]
    # per-field state was reseeded silently: UNAVAILABLE seeds, does NOT narrate
    pj.p.tick_once(T(t + 4)); pj.p.tick_once(T(t + 5))
    assert codes(pj) == ["SOURCES_DEGRADED", "SOURCES_RESTORED"]
    # a genuine post-reseed edge narrates again
    pj.holder["m"] = model()                        # UNAVAILABLE -> ALIVE
    pj.p.tick_once(T(t + 6)); pj.p.tick_once(T(t + 7))
    assert codes(pj)[-1] == "NODE_RECOVERED"


def test_intervention_required_is_never_narrated(pj):
    t = seed(pj)
    for i in range(4):
        pj.holder["m"] = model(intervention=bool((i // 2) % 2))  # flips
        pj.p.tick_once(T(t + i))
    assert pj.store.events == []
    assert not any("INTERVENTION" in c for c in ops_journal.CODES)


def test_provider_failure_contained_and_silent(pj):
    t = seed(pj)
    def boom(now):
        raise RuntimeError("provider down")
    pj.p._level_provider = boom
    s = pj.p.tick_once(T(t))
    assert s["error"] is None and pj.store.events == []   # level pass skipped only
    pj.p._level_provider = lambda now: pj.holder["m"]
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(t + 1))
    assert codes(pj) == ["KILL_FILE_ENGAGED"]       # recovers next tick


# ── Pending lifecycle, replay, envelopes ─────────────────────────────────────

def test_pending_frozen_before_append_and_replay_identical(pj, monkeypatch):
    t = seed(pj)
    order = []
    real_save = pj.p._save_checkpoint
    real_append = pj.p._append           # the projector's captured callable
    monkeypatch.setattr(pj.p, "_save_checkpoint",
                        lambda s: order.append(("save", len(s["pending"]))) or real_save(s))
    monkeypatch.setattr(pj.p, "_append",
                        lambda ev, k: order.append(("append", k)) or real_append(ev, k))
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(t))
    first_append = next(i for i, o in enumerate(order) if o[0] == "append")
    assert any(o[0] == "save" and o[1] > 0 for o in order[:first_append]), \
        "pending must be persisted (non-empty) BEFORE the append attempt"
    assert [e["code"] for e in pj.store.events] == ["KILL_FILE_ENGAGED"]


def test_append_failure_retains_frozen_pending_and_restart_drains(pj):
    t = seed(pj)
    pj.store.fail = True
    pj.holder["m"] = model(kill=True)
    s = pj.p.tick_once(T(t))
    assert s["error"] is not None
    ck = json.loads(pj.ckpt.read_text())
    assert len(ck["pending"]) == 1
    frozen = ck["pending"][0]
    assert frozen["event"]["at"] == T(t).isoformat()      # confirmation time frozen
    frozen_key = frozen["idempotency_key"]
    pj.store.fail = False
    pj.p._state = None                                    # restart
    pj.p.tick_once(T(t + 7))                              # much later clock
    assert pj.store.keys == [frozen_key]                  # exact frozen key reused
    assert pj.store.events[0]["at"] == T(t).isoformat()   # timestamp NOT regenerated
    assert json.loads(pj.ckpt.read_text())["pending"] == []


def test_dlb1_pending_survives_source_discontinuity(pj):
    """D-L2B-1 regression: a confirmed level pending whose append failed must
    survive a cycles.jsonl discontinuity re-baseline and drain exactly once on
    restart — no omission, no duplicate, timestamp not regenerated."""
    healthy = {"cycle_start": "CS1", "cycle_end": "CE1", "boundary": "B",
               "frozen": False, "published": {"delivered": True}, "error": ""}
    with pj.src.open("a") as fh:
        fh.write(json.dumps(healthy) + "\n")
    pj.p.tick_once(T(0)); pj.p.tick_once(T(1))            # baseline + seed (consumes record)
    assert json.loads(pj.ckpt.read_text())["cursor"] > 0
    # confirmed kill engagement, append DOWN -> frozen pending persisted
    pj.store.fail = True
    pj.holder["m"] = model(kill=True)
    assert pj.p.tick_once(T(2))["error"] is not None
    ck = json.loads(pj.ckpt.read_text())
    assert len(ck["pending"]) == 1
    frozen_key = ck["pending"][0]["idempotency_key"]
    frozen_at = ck["pending"][0]["event"]["at"]
    # cycles.jsonl REPLACED (shrinks below cursor) -> continuity re-baseline
    pj.src.write_text(json.dumps(dict(healthy, cycle_start="XX", cycle_end="YY")) + "\n")
    pj.store.fail = False
    pj.p._state = None                                    # restart: reload from disk
    pj.p.tick_once(T(9))                                  # re-baseline (preserves pending) + drain
    assert pj.store.keys == [frozen_key]                  # drained exactly once, frozen key
    assert pj.store.events[0]["code"] == "KILL_FILE_ENGAGED"
    assert pj.store.events[0]["at"] == frozen_at          # timestamp NOT regenerated
    assert json.loads(pj.ckpt.read_text())["pending"] == []   # cleared — no omission
    pj.p.tick_once(T(10))                                 # steady thereafter
    assert len(pj.store.events) == 1                      # no duplicate


def test_dlb1_complement_append_succeeded_then_discontinuity_dedups(pj):
    """Complementary: append succeeded, crash before pending removal, then a
    discontinuity — the preserved pending re-drains and the store dedup
    guarantees exactly one durable event."""
    healthy = {"cycle_start": "CS1", "cycle_end": "CE1", "boundary": "B",
               "frozen": False, "published": {"delivered": True}, "error": ""}
    with pj.src.open("a") as fh:
        fh.write(json.dumps(healthy) + "\n")
    pj.p.tick_once(T(0)); pj.p.tick_once(T(1))
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(2))                                  # KILL_FILE_ENGAGED appended cleanly
    assert [e["code"] for e in pj.store.events] == ["KILL_FILE_ENGAGED"]
    frozen_key = pj.store.keys[0]
    # simulate crash AFTER append, BEFORE pending removal: reinstate pre-drain
    # checkpoint (pending still holds the already-appended event) + discontinuity
    ck = json.loads(pj.ckpt.read_text())
    ck["pending"] = [{"event": pj.store.events[0], "idempotency_key": frozen_key,
                      "source_identity": "level:kill_file_present"}]
    pj.ckpt.write_text(json.dumps(ck))
    pj.src.write_text("")                                 # shrink -> discontinuity
    pj.p._state = None
    pj.p.tick_once(T(9))                                  # re-baseline preserves pending; drain dedups
    assert len(pj.store.events) == 1                      # store dedup: exactly one durable event
    assert json.loads(pj.ckpt.read_text())["pending"] == []


def test_level_envelope_complete_and_keys_distinguish(pj):
    t = seed(pj)
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(t))
    pj.holder["m"] = model(kill=False)
    pj.p.tick_once(T(t + 1))
    need = {"eventId", "seq", "category", "code", "humanExplanation", "scenarioKey",
            "packageHash", "who", "causedBy", "before", "after", "at"}
    for ev in pj.store.events:
        assert set(ev.keys()) == need
        assert ev["category"] == "operational" and ev["code"] in ops_journal.CODES
        assert ev["who"] == "system" and ev["causedBy"].startswith("l2_projector:level:")
    k1, k2 = pj.store.keys
    assert k1 != k2                                       # type+direction+identity distinct
    assert k1.startswith("l2|KILL_FILE_ENGAGED|False|True|")
    assert "ev_" not in k1 and T(t).isoformat() in k1     # no uuid/seq; frozen identity


# ── Checkpoint compatibility ─────────────────────────────────────────────────

def test_l2a_checkpoint_loads_with_additive_defaults(pj):
    # simulate an L2-A-era checkpoint (no level/debounce keys)
    l2a = {"schema_version": 1, "cursor": 0, "last_record_span": None,
           "last_record_identity": None,
           "detector": {"error_present": None, "frozen": None, "delivered": None},
           "pending": []}
    pj.ckpt.write_text(json.dumps(l2a))
    pj.p._state = None
    s = pj.p.tick_once(T(0))                              # silent upgrade
    assert s["error"] is None and pj.store.events == []
    ck = json.loads(pj.ckpt.read_text())
    assert ck["schema_version"] == 1                      # no version bump
    assert set(ck["level"]) == set(ops_journal._LEVEL_KINDS)


# ── Runtime reset, sequence ordering, real store ─────────────────────────────

def test_runtime_reset_leaves_level_state_untouched(pj, tmp_path, monkeypatch):
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    t = seed(pj)
    pj.holder["m"] = model(polling=False)
    pj.p.tick_once(T(t))                                  # open debounce window
    before = pj.ckpt.read_bytes()
    client = TestClient(server.app, raise_server_exceptions=False)
    r = client.post("/api/runtime/reset")
    assert r.status_code == 200
    assert pj.ckpt.read_bytes() == before                 # checkpoint/debounce survive
    pj.p.tick_once(T(t + 1))
    assert codes(pj) == ["FEED_UNHEALTHY"]                # window still confirms


def test_sequence_before_level_within_one_tick(pj):
    t = seed(pj)
    # write a sequence edge into cycles.jsonl AND flip the kill file this tick
    healthy = {"cycle_start": "CS1", "cycle_end": "CE1", "boundary": "B",
               "frozen": False, "published": {"delivered": True}, "error": ""}
    err = dict(healthy, cycle_start="CS2", cycle_end="CE2", error="E")
    with pj.src.open("a") as fh:
        fh.write(json.dumps(healthy) + "\n")
    pj.p.tick_once(T(t))                                  # seed sequence detector
    with pj.src.open("a") as fh:
        fh.write(json.dumps(err) + "\n")
    pj.holder["m"] = model(kill=True)
    pj.p.tick_once(T(t + 1))
    assert codes(pj) == ["CYCLE_ERROR", "KILL_FILE_ENGAGED"]   # sequence first
    assert pj.store.events[0]["at"] == "CE2"              # record time
    assert pj.store.events[1]["at"] == T(t + 1).isoformat()   # frozen confirmation time


def test_real_store_dedup_monotonic_seq_and_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "EVENTS_DB_PATH", tmp_path / "events.db")
    monkeypatch.setattr(server, "_MALFORMED_WARNED", set())
    holder = {"m": model()}
    proj = ops_journal.OpsJournalProjector(
        cycles_path=tmp_path / "cycles.jsonl", checkpoint_path=tmp_path / "ck.json",
        append_event=lambda ev, k: server._append_event(ev, k),
        level_provider=lambda now: holder["m"])
    proj.tick_once(T(0)); proj.tick_once(T(1))            # baseline + seed
    holder["m"] = model(kill=True)
    proj.tick_once(T(2))                                  # KILL_FILE_ENGAGED
    cmd = {"eventId": "ev_CMD1", "seq": 0, "category": "system", "code": "CMD",
           "humanExplanation": "c", "scenarioKey": None, "packageHash": "h",
           "who": "op", "causedBy": "c", "before": None, "after": None, "at": "T"}
    server._append_event(cmd, None)                       # interleaved producer event
    conn = sqlite3.connect(tmp_path / "events.db")
    rows = [json.loads(r[0]) for r in conn.execute(
        "SELECT payload FROM bot_events ORDER BY seq")]
    conn.close()
    assert [r["code"] for r in rows] == ["KILL_FILE_ENGAGED", "CMD"]
    assert rows[1]["seq"] == rows[0]["seq"] + 1           # shared monotonic seq
    # crash replay: restore pending into checkpoint, drain against real store
    ck = json.loads((tmp_path / "ck.json").read_text())
    ck["pending"] = [{"event": rows[0], "idempotency_key":
                      f"l2|KILL_FILE_ENGAGED|False|True|{T(2).isoformat()}",
                      "source_identity": "level:kill"}]
    (tmp_path / "ck.json").write_text(json.dumps(ck))
    proj._state = None
    proj.tick_once(T(3))
    conn = sqlite3.connect(tmp_path / "events.db")
    n = conn.execute("SELECT COUNT(*) FROM bot_events").fetchone()[0]
    conn.close()
    assert n == 2                                         # P-2 store deduplicated
    # retention pruning does not disturb level/debounce state
    monkeypatch.setattr(server, "EVENTS_RETENTION_MAX", 1)
    server._append_event(dict(cmd, eventId="ev_CMD2"), None)
    holder["m"] = model(kill=False)
    proj.tick_once(T(4))                                  # KILL_FILE_CLEARED post-prune
    ck = json.loads((tmp_path / "ck.json").read_text())
    assert ck["level"]["kill_file_present"] is False
    assert not (BACKEND_DIR / "events.db").exists()
    assert not (BACKEND_DIR / "ops_journal_checkpoint.json").exists()


def test_real_collect_sources_kill_file_integration(tmp_path):
    """End-to-end level pass through the REAL frozen L1A public API against a
    temp state tree — proving no re-derivation is needed anywhere."""
    state_dir = tmp_path / "state"; (state_dir / "ops").mkdir(parents=True)
    md_dir = tmp_path / "md"; md_dir.mkdir()
    kill = tmp_path / "KILL"
    store = Store()
    proj = ops_journal.OpsJournalProjector(
        cycles_path=state_dir / "ops" / "cycles.jsonl",
        checkpoint_path=tmp_path / "ck.json",
        append_event=store.append,
        level_provider=lambda now: ops_status.build_operational_status(
            ops_status.collect_sources(state_dir, md_dir, kill), now))
    proj.tick_once(T(0)); proj.tick_once(T(1))            # baseline + seed (no kill file)
    assert store.events == []
    kill.write_text("")                                   # operator engages kill file
    proj.tick_once(T(2))
    assert [e["code"] for e in store.events] == ["KILL_FILE_ENGAGED"]
    kill.unlink()                                         # operator clears it
    proj.tick_once(T(3))
    assert [e["code"] for e in store.events] == ["KILL_FILE_ENGAGED", "KILL_FILE_CLEARED"]
