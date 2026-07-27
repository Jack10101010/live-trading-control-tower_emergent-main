"""P-1B — broker-sync event producer hardening tests.

Verifies the broker-sync transition gate after P-1B: silent first-observation
and restart seeding, transition-only emission with the existing BROKER_SYNC_*
codes, complete EventEntry envelopes carrying prior/current transition state,
anchor-derived deterministic idempotency keys (retries of an unadvanced
transition reconstruct identical keys regardless of the new sync timestamp),
append-before-gate-advance ordering with preserved route failure semantics,
the non-emitting reconciliation route, and the producer-inventory guard.

Fast, in-process; the reconciliation engine is stubbed at the sync_layer seam
so status/findings are fully controllable. Isolated module state + a temp
events DB per test — safe under `pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import json
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

client = TestClient(server.app)

ENVELOPE_FIELDS = {
    "eventId", "seq", "category", "code", "humanExplanation", "scenarioKey",
    "packageHash", "who", "causedBy", "before", "after", "at",
}


def fake_result(status="ok", findings=()):
    """A minimal reconciliation result with controllable transition identity."""
    n_err = sum(1 for _ in findings if status == "error")
    return {
        "status": status,
        "at": server._now_iso(),
        "durationMs": 5,
        "reconciliation": {
            "findings": [{"type": t, "entityId": e} for t, e in findings],
            "summary": {"warnings": 0, "errors": n_err,
                        "brokerPositions": 0, "brokerOrders": 0},
        },
    }


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """Isolated events DB + broker-sync module state + stubbed sync engine."""
    db = tmp_path / "events.db"
    monkeypatch.setattr(server, "EVENTS_DB_PATH", db)
    monkeypatch.setattr(server, "_BROKER_SYNC_GATE", None)
    monkeypatch.setattr(server, "_SYNC_CACHE", None)
    monkeypatch.setattr(server, "_SYNC_SEQ", 0)
    monkeypatch.setattr(server, "_LAST_SUCCESSFUL_SYNC_AT", None)
    # Stub the reconciliation seam: _run_broker_sync's inputs become inert and
    # sync_layer.run_sync returns whatever the test queued in holder["result"].
    holder = {"result": fake_result()}
    monkeypatch.setattr(server, "_normalized_broker_views",
                        lambda: ([], [], [], "connected", {}))
    monkeypatch.setattr(server, "_deployments_view", lambda: [])
    monkeypatch.setattr(server, "_active_package_runtime", lambda: {})
    monkeypatch.setattr(server, "_overlay_count", lambda: 0)
    monkeypatch.setattr(server, "_event_count", lambda: 0)
    monkeypatch.setattr(server, "sync_layer", SimpleNamespace(
        RuntimeViews=lambda **kw: None,
        run_sync=lambda **kw: dict(holder["result"]),
    ))
    return SimpleNamespace(db=db, holder=holder)


def rows(db: Path) -> list[dict]:
    if not db.exists():
        return []
    conn = sqlite3.connect(db)
    try:
        return [json.loads(r[0]) for r in
                conn.execute("SELECT payload FROM bot_events ORDER BY seq ASC")]
    finally:
        conn.close()


def sync(iso, status="ok", findings=(), append_event=True):
    iso.holder["result"] = fake_result(status, findings)
    return server._run_broker_sync(append_event=append_event)


def key_spy(monkeypatch):
    """Wrap the real _append_event, recording every idempotency key passed."""
    keys: list = []
    real = server._append_event
    monkeypatch.setattr(server, "_append_event",
                        lambda ev, k: keys.append(k) or real(ev, k))
    return keys


# ── 1. Producer inventory guard ──────────────────────────────────────────────

def test_exactly_eight_append_event_production_call_sites():
    src = (BACKEND_DIR / "server.py").read_text()
    calls = [m for m in re.finditer(r"_append_event\(", src)
             if not src[max(0, m.start() - 4):m.start()].endswith("def ")]
    # LIVE-2 deliberately added two reviewed producers: the market-order route's
    # denial event and its result event.
    assert len(calls) == 8, (
        f"expected exactly 8 production _append_event call sites "
        f"(command, broker-sync, LIVE_STATUS, L2 projector wiring, market-order "
        f"denial/result, entity-operation denial/result); found {len(calls)} — "
        "a new producer must be reviewed before it ships")


# ── 2/3. Silent seeding ──────────────────────────────────────────────────────

def test_silent_first_seed(iso):
    r = sync(iso)
    assert rows(iso.db) == []
    assert r["eventAppended"] is False
    assert r["syncSequence"] == 1
    assert server._BROKER_SYNC_GATE is not None          # gate seeded
    assert server._SYNC_CACHE is not None                # cache still updates
    assert server._LAST_SUCCESSFUL_SYNC_AT is not None   # health field updates


def test_restart_silence(iso):
    sync(iso)                                            # seed
    sync(iso, status="warning", findings=(("ORPHAN", "p1"),))  # real transition
    assert len(rows(iso.db)) == 1
    server._BROKER_SYNC_GATE = None                      # simulate restart
    r = sync(iso, status="warning", findings=(("ORPHAN", "p1"),))  # same outcome
    assert len(rows(iso.db)) == 1                        # zero new rows
    assert r["eventAppended"] is False
    assert server._BROKER_SYNC_GATE is not None          # re-seeded


# ── 4. Transition detection ──────────────────────────────────────────────────

def test_status_change_emits_exactly_one(iso):
    sync(iso)
    r = sync(iso, status="error", findings=(("UNKNOWN_POSITION", "x9"),))
    evs = rows(iso.db)
    assert len(evs) == 1 and evs[0]["code"] == "BROKER_SYNC_ERROR"
    assert r["eventAppended"] is True and r["eventCode"] == "BROKER_SYNC_ERROR"


def test_finding_added_removed_and_changed_each_emit(iso):
    sync(iso)                                                        # seed: ok, none
    sync(iso, status="warning", findings=(("ORPHAN", "p1"),))        # added
    sync(iso, status="ok", findings=())                              # removed
    sync(iso, status="ok", findings=(("SIZE_DRIFT", "p2"),))         # changed, status same as prior? no: ok->ok with new finding
    evs = rows(iso.db)
    assert [e["code"] for e in evs] == ["BROKER_SYNC_WARNING", "BROKER_SYNC_OK", "BROKER_SYNC_OK"]
    # findings change with unchanged status still emitted (last event)
    assert evs[2]["before"]["findings"] == [] and evs[2]["after"]["findings"] == [["SIZE_DRIFT", "p2"]]


def test_steady_repetition_emits_nothing(iso):
    sync(iso)
    sync(iso, status="warning", findings=(("ORPHAN", "p1"),))
    for _ in range(5):
        r = sync(iso, status="warning", findings=(("ORPHAN", "p1"),))
        assert r["eventAppended"] is False
    assert len(rows(iso.db)) == 1


# ── 5. Envelope contract ─────────────────────────────────────────────────────

def test_envelope_complete_and_transition_context(iso):
    sync(iso, status="ok", findings=(("A", "1"),))
    sync(iso, status="warning", findings=(("B", "2"), ("A", "1")))
    ev = rows(iso.db)[0]
    assert set(ev.keys()) == ENVELOPE_FIELDS
    assert ev["category"] == "broker" and ev["who"] == "system"
    assert ev["code"] == "BROKER_SYNC_WARNING"
    assert ev["before"] == {"status": "ok", "findings": [["A", "1"]]}
    # canonical sorted findings identity, not request order
    assert ev["after"] == {"status": "warning", "findings": [["A", "1"], ["B", "2"]]}
    assert ev["causedBy"].startswith("broker_sync:")
    assert "sync_" not in ev["causedBy"]                 # counter dependence removed
    assert ev["scenarioKey"] is None
    assert isinstance(ev["seq"], int) and ev["seq"] > 0


# ── 6/7. Retry, dedup, crash window ─────────────────────────────────────────

def test_unadvanced_retry_reconstructs_identical_key_and_dedups(iso, monkeypatch):
    keys = key_spy(monkeypatch)
    sync(iso)                                            # silent seed
    prior_gate = server._BROKER_SYNC_GATE
    sync(iso, status="error", findings=(("X", "1"),))    # transition -> append
    assert len(rows(iso.db)) == 1 and len(keys) == 1
    # Crash window: append landed but the gate never advanced.
    server._BROKER_SYNC_GATE = prior_gate
    # Retry arrives later — _now_iso() differs — same broker outcome.
    r = sync(iso, status="error", findings=(("X", "1"),))
    assert len(keys) == 2 and keys[1] == keys[0]         # identical anchor-based key
    assert len(rows(iso.db)) == 1                        # store deduplicated
    assert r["eventAppended"] is True
    assert r["eventSeqAppended"] == rows(iso.db)[0]["seq"]  # existing row returned
    assert server._BROKER_SYNC_GATE[0] != prior_gate[0]  # gate advanced after dedup
    # After advancement the same outcome is steady state.
    r2 = sync(iso, status="error", findings=(("X", "1"),))
    assert r2["eventAppended"] is False and len(keys) == 2


# ── 8. Genuine recurrence ────────────────────────────────────────────────────

def test_recurrence_mints_distinct_keys(iso, monkeypatch):
    keys = key_spy(monkeypatch)
    sync(iso)                                            # seed at A (ok)
    sync(iso, status="error", findings=(("X", "1"),))    # A -> B
    sync(iso, status="ok", findings=())                  # B -> A
    sync(iso, status="error", findings=(("X", "1"),))    # A -> B again
    assert len(rows(iso.db)) == 3
    assert len(keys) == 3 and len(set(keys)) == 3        # three distinct keys


# ── 9. Append failure ────────────────────────────────────────────────────────

def test_append_failure_preserves_gate_and_route_semantics(iso, monkeypatch):
    sync(iso)
    prior_gate = server._BROKER_SYNC_GATE
    real = server._append_event

    def boom(ev, k):
        raise sqlite3.OperationalError("injected append failure")

    monkeypatch.setattr(server, "_append_event", boom)
    with pytest.raises(sqlite3.OperationalError):        # direct-call semantics
        sync(iso, status="error", findings=(("X", "1"),))
    assert server._BROKER_SYNC_GATE == prior_gate        # gate + anchor untouched
    assert rows(iso.db) == []

    # Route-level: the POST surfaces the failure (500), unchanged semantics.
    raw = TestClient(server.app, raise_server_exceptions=False)
    r = raw.post("/api/broker/sync")
    assert r.status_code == 500
    assert server._BROKER_SYNC_GATE == prior_gate

    # Equivalent retry after recovery reconstructs the same key and appends once.
    keys = key_spy(monkeypatch)                          # re-wraps the real helper
    monkeypatch.setattr(server, "_append_event",
                        lambda ev, k: keys.append(k) or real(ev, k))
    sync(iso, status="error", findings=(("X", "1"),))
    assert len(rows(iso.db)) == 1 and len(keys) == 1
    assert keys[0].startswith("broker|")
    assert prior_gate[1] in keys[0]                      # keyed to the PRIOR anchor


# ── 10. Non-emitting reconciliation route ────────────────────────────────────

def test_reconciliation_route_never_appends(iso, monkeypatch):
    keys = key_spy(monkeypatch)
    r = client.get("/api/broker/reconciliation")         # lazy first run
    assert r.status_code == 200
    assert keys == [] and rows(iso.db) == []
    assert server._BROKER_SYNC_GATE is None              # gate untouched on this path


# ── 11. Regression protection ────────────────────────────────────────────────

def test_sync_seq_and_response_contract_unchanged(iso):
    r1 = sync(iso)
    r2 = sync(iso)
    assert (r1["syncSequence"], r2["syncSequence"]) == (1, 2)
    assert r1["eventAppended"] is False and "status" in r1


def test_command_and_live_status_producers_untouched():
    """Source-level guard: the two other producers keep their exact contracts
    (behavioural coverage lives in their own suites; the scope audit proves
    byte-identity against HEAD)."""
    src = (BACKEND_DIR / "server.py").read_text()
    assert '_append_event(event, request.headers.get("Idempotency-Key"))' in src
    assert "_LIVE_SIGNATURE_FIELDS = (\"mode\", \"engine_version\", " \
           "\"deployment_profile\", \"data_seam\")" in src


def test_no_stray_events_db_in_repo():
    assert not (BACKEND_DIR / "events.db").exists(), \
        "tests must never create backend/events.db"
