"""P-1 — LIVE_STATUS re-envelope + transition gate tests.

Verifies POST /api/live/ingest after the P-1 correction: unconditional
runtime-state updates, zero per-cycle event flood, silent first-observation and
restart seeding, transition-gated emission with frozen cardinality (at most one
LIVE_MODE_CHANGED + one aggregated LIVE_CONFIG_CHANGED per ingest), complete
EventEntry envelopes, anchor-derived idempotency keys (crash-window retries
reconstruct identical keys even with a fresh payload `at`), and
append-before-signature-advance ordering (a failed append leaves the prior
gate intact; retries deduplicate instead of double-narrating).

Fast, in-process (TestClient); isolated module state + a temp events DB per
test, so the suite is safe under `pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
# server.py imports its siblings flat (uvicorn runs from backend/), so the
# backend directory must be importable for the app to load as in production.
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
CONFIG_FIELDS = ("engine_version", "deployment_profile", "data_seam")


def payload(instance="lux-eurusd-01", at="2026-07-22T10:00:00+00:00",
            mode="dry_run", engine_version="5bb6372c", profile="GOLDEN_COMPATIBLE",
            seam="dukascopy_mt5", boundary="2026-07-22T09:45:00+00:00",
            intents=None, runner_status="ok", frozen=False):
    return {
        "instance_id": instance,
        "symbol": "EURUSD",
        "deployment_profile": profile,
        "data_seam": seam,
        "engine_version": engine_version,
        "mode": mode,
        "at": at,
        "runner": {"status": runner_status, "boundary": boundary},
        "intents": intents or [],
        "execution": {"frozen": frozen},
        "reconciliation": {},
        "positions": [],
    }


def stored_rows(db_path: Path) -> list[dict]:
    if not db_path.exists():
        return []
    conn = sqlite3.connect(db_path)
    try:
        import json
        return [json.loads(r[0]) for r in
                conn.execute("SELECT payload FROM bot_events ORDER BY seq ASC")]
    finally:
        conn.close()


@pytest.fixture()
def iso(tmp_path, monkeypatch):
    """Isolated events DB + fresh gate/state, restored after each test."""
    db = tmp_path / "events.db"
    monkeypatch.setattr(server, "EVENTS_DB_PATH", db)
    monkeypatch.setattr(server, "_LIVE_STATUS", {})
    monkeypatch.setattr(server, "_LIVE_GATE", {})
    return db


def assert_envelope(ev: dict):
    assert set(ev.keys()) == ENVELOPE_FIELDS, f"envelope mismatch: {sorted(ev)}"
    assert ev["eventId"].startswith("ev_")
    assert ev["category"] == "operational"
    assert ev["scenarioKey"] is None
    assert ev["who"] == "system"
    assert ev["causedBy"].startswith("live_ingest:")
    assert isinstance(ev["seq"], int) and ev["seq"] > 0
    assert isinstance(ev["humanExplanation"], str) and ev["humanExplanation"]
    assert isinstance(ev["packageHash"], str)
    assert isinstance(ev["before"], dict) and isinstance(ev["after"], dict)


# ── Flood elimination ────────────────────────────────────────────────────────

def test_first_ingest_seeds_silently(iso):
    r = client.post("/api/live/ingest", json=payload())
    assert r.status_code == 200
    assert r.json() == {"ok": True, "seq": None, "deduplicated": None}
    assert stored_rows(iso) == []
    assert "lux-eurusd-01" in server._LIVE_STATUS


def test_steady_state_appends_zero_events(iso):
    client.post("/api/live/ingest", json=payload(at="2026-07-22T10:00:00+00:00"))
    for i in range(5):  # fresh `at` every cycle — the old flood trigger
        p = payload(at=f"2026-07-22T10:0{i + 1}:00+00:00",
                    boundary=f"2026-07-22T09:{45 + i}:00+00:00")
        r = client.post("/api/live/ingest", json=p)
        assert r.status_code == 200
        assert r.json() == {"ok": True, "seq": None, "deduplicated": None}
        assert server._LIVE_STATUS["lux-eurusd-01"]["at"] == p["at"]  # state updated
    assert stored_rows(iso) == []


def test_silent_restart_reseed(iso):
    client.post("/api/live/ingest", json=payload())
    server._LIVE_GATE.clear()  # simulate backend restart (in-memory gate lost)
    r = client.post("/api/live/ingest", json=payload(at="2026-07-22T11:00:00+00:00"))
    assert r.json() == {"ok": True, "seq": None, "deduplicated": None}
    assert stored_rows(iso) == []


# ── Transitions and cardinality ──────────────────────────────────────────────

def test_mode_transition_single_event(iso):
    client.post("/api/live/ingest", json=payload(mode="dry_run"))
    r = client.post("/api/live/ingest",
                    json=payload(mode="live", at="2026-07-22T10:05:00+00:00"))
    body = r.json()
    rows = stored_rows(iso)
    assert len(rows) == 1
    ev = rows[0]
    assert_envelope(ev)
    assert ev["code"] == "LIVE_MODE_CHANGED"
    assert ev["before"] == {"mode": "dry_run"}
    assert ev["after"]["mode"] == "live"
    assert ev["after"]["instance_id"] == "lux-eurusd-01"
    assert ev["after"]["boundary"] == "2026-07-22T09:45:00+00:00"
    assert body == {"ok": True, "seq": ev["seq"], "deduplicated": False}


def test_config_single_field_one_event(iso):
    client.post("/api/live/ingest", json=payload())
    client.post("/api/live/ingest",
                json=payload(engine_version="deadbeef", at="2026-07-22T10:05:00+00:00"))
    rows = stored_rows(iso)
    assert len(rows) == 1
    ev = rows[0]
    assert_envelope(ev)
    assert ev["code"] == "LIVE_CONFIG_CHANGED"
    assert ev["before"] == {"engine_version": "5bb6372c"}
    assert ev["after"]["engine_version"] == "deadbeef"
    # unchanged config fields never enter the delta
    for f in ("deployment_profile", "data_seam", "mode"):
        assert f not in ev["before"]


def test_config_three_fields_still_one_aggregated_event(iso):
    client.post("/api/live/ingest", json=payload())
    client.post("/api/live/ingest",
                json=payload(engine_version="deadbeef", profile="LIVE_V2",
                             seam="mt5_native", at="2026-07-22T10:05:00+00:00"))
    rows = stored_rows(iso)
    assert len(rows) == 1
    ev = rows[0]
    assert ev["code"] == "LIVE_CONFIG_CHANGED"
    assert set(ev["before"]) == set(CONFIG_FIELDS)
    assert ev["before"]["deployment_profile"] == "GOLDEN_COMPATIBLE"
    assert ev["after"]["deployment_profile"] == "LIVE_V2"


def test_mode_plus_config_exactly_two_events_fixed_order(iso):
    client.post("/api/live/ingest", json=payload())
    r = client.post("/api/live/ingest",
                    json=payload(mode="live", engine_version="deadbeef",
                                 profile="LIVE_V2", seam="mt5_native",
                                 at="2026-07-22T10:05:00+00:00"))
    rows = stored_rows(iso)
    assert [e["code"] for e in rows] == ["LIVE_MODE_CHANGED", "LIVE_CONFIG_CHANGED"]
    assert r.json() == {"ok": True, "seq": rows[1]["seq"], "deduplicated": False}
    # gate advanced only after both: a replay is now steady state
    r2 = client.post("/api/live/ingest",
                     json=payload(mode="live", engine_version="deadbeef",
                                  profile="LIVE_V2", seam="mt5_native",
                                  at="2026-07-22T10:06:00+00:00"))
    assert r2.json() == {"ok": True, "seq": None, "deduplicated": None}
    assert len(stored_rows(iso)) == 2


def test_per_cycle_fields_never_narrate(iso):
    client.post("/api/live/ingest", json=payload())
    client.post("/api/live/ingest",
                json=payload(at="2026-07-22T10:05:00+00:00",
                             boundary="2026-07-22T10:00:00+00:00",
                             intents=[{"k": 1}, {"k": 2}],
                             runner_status="error", frozen=True))
    assert stored_rows(iso) == []


def test_change_and_revert_two_events_distinct_keys(iso):
    client.post("/api/live/ingest", json=payload(mode="dry_run"))
    client.post("/api/live/ingest",
                json=payload(mode="live", at="2026-07-22T10:05:00+00:00"))
    client.post("/api/live/ingest",
                json=payload(mode="dry_run", at="2026-07-22T10:10:00+00:00"))
    rows = stored_rows(iso)
    assert [e["code"] for e in rows] == ["LIVE_MODE_CHANGED", "LIVE_MODE_CHANGED"]
    assert rows[0]["before"] == {"mode": "dry_run"} and rows[1]["before"] == {"mode": "live"}


# ── Replay, crash window, partial failure ────────────────────────────────────

def test_ordinary_replay_after_advance_attempts_no_append(iso, monkeypatch):
    client.post("/api/live/ingest", json=payload(mode="dry_run"))
    flip = payload(mode="live", at="2026-07-22T10:05:00+00:00")
    client.post("/api/live/ingest", json=flip)
    assert len(stored_rows(iso)) == 1

    calls = []
    real = server._append_event
    monkeypatch.setattr(server, "_append_event",
                        lambda ev, key: calls.append(key) or real(ev, key))
    r = client.post("/api/live/ingest", json=flip)  # byte-identical replay
    assert calls == []                              # zero append attempts
    assert r.json() == {"ok": True, "seq": None, "deduplicated": None}
    assert len(stored_rows(iso)) == 1


def test_crash_window_retry_dedups_with_fresh_at(iso):
    client.post("/api/live/ingest", json=payload(mode="dry_run"))
    prior_gate = server._LIVE_GATE["lux-eurusd-01"]
    client.post("/api/live/ingest",
                json=payload(mode="live", at="2026-07-22T10:05:00+00:00"))
    assert len(stored_rows(iso)) == 1
    # Crash window: event landed but the gate never advanced.
    server._LIVE_GATE["lux-eurusd-01"] = prior_gate
    # Retry is the NEXT equivalent ingest — different payload `at`.
    r = client.post("/api/live/ingest",
                    json=payload(mode="live", at="2026-07-22T10:06:00+00:00"))
    rows = stored_rows(iso)
    assert len(rows) == 1                     # identical anchor-derived key deduped
    assert r.json() == {"ok": True, "seq": rows[0]["seq"], "deduplicated": True}
    # Gate advanced after the dedup: replay is now steady state.
    r2 = client.post("/api/live/ingest",
                     json=payload(mode="live", at="2026-07-22T10:07:00+00:00"))
    assert r2.json() == {"ok": True, "seq": None, "deduplicated": None}


def test_partial_failure_keeps_gate_then_retry_completes(iso, monkeypatch):
    client.post("/api/live/ingest", json=payload())
    prior_gate = server._LIVE_GATE["lux-eurusd-01"]

    real = server._append_event

    def fail_config(ev, key):
        if ev["code"] == "LIVE_CONFIG_CHANGED":
            raise sqlite3.OperationalError("injected append failure")
        return real(ev, key)

    monkeypatch.setattr(server, "_append_event", fail_config)
    flip = payload(mode="live", engine_version="deadbeef",
                   at="2026-07-22T10:05:00+00:00")
    r = client.post("/api/live/ingest", json=flip)
    assert r.status_code == 200
    assert len(stored_rows(iso)) == 1                       # mode landed, config didn't
    assert server._LIVE_GATE["lux-eurusd-01"] == prior_gate  # gate NOT advanced
    assert server._LIVE_STATUS["lux-eurusd-01"]["at"] == flip["at"]  # state updated

    monkeypatch.setattr(server, "_append_event", real)
    retry = payload(mode="live", engine_version="deadbeef",
                    at="2026-07-22T10:06:00+00:00")
    r2 = client.post("/api/live/ingest", json=retry)
    rows = stored_rows(iso)
    assert [e["code"] for e in rows] == ["LIVE_MODE_CHANGED", "LIVE_CONFIG_CHANGED"]
    assert len(rows) == 2                    # mode deduped (identical key), config inserted
    assert r2.json() == {"ok": True, "seq": rows[1]["seq"], "deduplicated": False}
    assert server._LIVE_GATE["lux-eurusd-01"] != prior_gate  # advanced now


# ── Contract ─────────────────────────────────────────────────────────────────

def test_missing_instance_id_still_400(iso):
    r = client.post("/api/live/ingest", json={"mode": "dry_run"})
    assert r.status_code == 400


def test_multi_instance_gate_independence(iso):
    client.post("/api/live/ingest", json=payload(instance="a"))
    client.post("/api/live/ingest", json=payload(instance="b"))
    client.post("/api/live/ingest",
                json=payload(instance="a", mode="live", at="2026-07-22T10:05:00+00:00"))
    rows = stored_rows(iso)
    assert len(rows) == 1 and rows[0]["after"]["instance_id"] == "a"
    r = client.post("/api/live/ingest",
                    json=payload(instance="b", at="2026-07-22T10:06:00+00:00"))
    assert r.json() == {"ok": True, "seq": None, "deduplicated": None}


def test_publisher_ignores_response_body():
    """The node's publisher must not parse the ingest response body — P-1 changes
    the body shape while preserving HTTP 200. The publisher's publish() uses only
    resp.status; verify no body read exists in the module."""
    src = (REPO_ROOT / "live" / "publisher.py").read_text()
    assert "resp.status" in src
    for body_use in ("resp.read", "json.loads(resp", "json.load(resp"):
        assert body_use not in src


def test_envelope_at_uses_producer_timestamp_with_backend_fallback(iso):
    client.post("/api/live/ingest", json=payload())
    client.post("/api/live/ingest",
                json=payload(mode="live", at="2026-07-22T10:05:00+00:00"))
    assert stored_rows(iso)[0]["at"] == "2026-07-22T10:05:00+00:00"
    # fallback: transition payload with NO `at` still narrates, at = backend now
    p = payload(mode="dry_run")
    del p["at"]
    client.post("/api/live/ingest", json=p)
    rows = stored_rows(iso)
    assert len(rows) == 2
    assert isinstance(rows[1]["at"], str) and rows[1]["at"]  # backend UTC now
