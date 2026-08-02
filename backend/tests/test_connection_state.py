"""UI-1 — truthful connection state.

The properties under test are honesty properties. A Control Tower that is running
must never imply a node is alive; a node that is publishing must never imply MT5
is reachable; a snapshot that exists must never imply it is current. Each of the
four dimensions has its own evidence and its own vocabulary, and the two
"we don't know" words mean genuinely different things:

    unknown      no evidence either way (silence is ambiguous — the node keeps
                 trading with the Control Tower offline, invariant I-10)
    unavailable  POSITIVE evidence that something could not be read or reached

Optimism is the failure mode these tests exist to prevent, so the assertions are
mostly of the form "must NOT claim X" rather than "claims Y".
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi.testclient import TestClient                              # noqa: E402

import connection_state as cs                                         # noqa: E402
import live_telemetry as lt                                           # noqa: E402
import server                                                         # noqa: E402

client = TestClient(server.app)

NOW = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)


def snapshot(published_at="2026-07-25T12:00:00Z", **over):
    snap = {
        "schema_version": lt.SCHEMA_VERSION,
        "instance_id": "ui1-node",
        "published_at": published_at,
        "cycle": {"status": "ok"},
        "runtime": {"mode": "dry_run"},
        "engine": {"engine_version_actual": "5bb6372c"},
        "account": {"identity": {"available": False}, "health": {"available": False}},
        "arming": {"status": "unarmed", "armed": False},
        "market": {"available": False, "feed_healthy": None},
        "reconciliation": {"available": True, "frozen": False, "snapshot_status": "ok"},
        "risk": {},
        "positions": [],
        "execution": {},
    }
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(snap.get(key), dict):
            snap[key] = {**snap[key], **value}
        else:
            snap[key] = value
    return snap


def record(published_at="2026-07-25T12:00:00Z", **over):
    return {"snapshot": snapshot(published_at, **over),
            "received_at": published_at, "legacy_source": False}


@pytest.fixture()
def iso_store(tmp_path, monkeypatch):
    """Isolate the durable store and module state; never touch the repo DB."""
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    prior = dict(server._LIVE_STATUS)
    server._LIVE_STATUS.clear()
    # The node's files are not visible from this host in the target topology.
    monkeypatch.setattr(server, "_node_beacon", lambda: None)
    yield
    server._LIVE_STATUS.clear()
    server._LIVE_STATUS.update(prior)


# ── no snapshot: an explicit first run, never "healthy" ──────────────────────

def test_no_snapshot_is_first_run_and_claims_nothing():
    state = cs.build_connection_state({}, NOW)
    assert state["firstRun"] is True
    assert state["telemetry"] == cs.TELEMETRY_NEVER
    assert state["node"] == cs.NODE_UNKNOWN
    assert state["bridge"] == cs.BRIDGE_UNKNOWN
    assert state["instanceCount"] == 0 and state["instances"] == []
    assert isinstance(state["emptyState"], str) and state["emptyState"]


def test_first_run_never_uses_optimistic_vocabulary():
    body = str(cs.build_connection_state({}, NOW)).lower()
    for forbidden in ("healthy", "connected", "online", "ok\"", "'ok'"):
        assert forbidden not in body, f"first run implied {forbidden!r}"


# ── fresh snapshot ───────────────────────────────────────────────────────────

def test_fresh_snapshot_connects_the_node_but_not_the_bridge():
    """Fresh telemetry proves the NODE reached us. It says nothing about MT5 —
    the node samples the bridge only on cycles containing an OPEN."""
    state = cs.build_connection_state({"ui1-node": record()}, NOW)
    assert state["telemetry"] == cs.TELEMETRY_FRESH
    assert state["node"] == cs.NODE_CONNECTED
    assert state["bridge"] == cs.BRIDGE_UNKNOWN          # not sampled ≠ broken
    view = state["instances"][0]
    assert view["ageSeconds"] == 0.0
    assert view["publishedAt"] == "2026-07-25T12:00:00Z"
    assert view["schemaVersion"] == lt.SCHEMA_VERSION
    assert view["problem"] is None


def test_freshness_derives_only_from_published_at_never_received_at():
    """`received_at` is the tower's clock; the contract says age comes from the
    node's own timestamp."""
    stale_published = (NOW - timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    rec = record(stale_published)
    rec["received_at"] = NOW.isoformat().replace("+00:00", "Z")   # arrived "just now"
    state = cs.build_connection_state({"ui1-node": rec}, NOW)
    assert state["telemetry"] == cs.TELEMETRY_STALE
    assert state["instances"][0]["ageSeconds"] == pytest.approx(7200, abs=1)


def test_threshold_boundary_is_inclusive_of_fresh():
    """M-NODE-READ-1 — the boundary is now the PHASE-AWARE budget.

    This asserted the flat 120s default against a snapshot whose cycle status is
    `ok`. That was the second-freshness-authority defect in test form: M-TEL-1
    gives an `ok` (non-idle) cycle one bar interval, because a warm recompute
    legitimately publishes nothing while it runs, and the flat threshold marked
    a healthy node stale about two minutes in.

    Both phases are asserted explicitly, so the budget can never silently revert
    to one number for both.
    """
    def at(seconds_ago):
        return (NOW - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")

    # RECOMPUTE phase (`cycle.status: ok`) — one bar interval.
    budget = lt.RECOMPUTE_STALE_AFTER_S
    assert cs.build_connection_state({"n": record(at(budget))}, NOW)["telemetry"] == cs.TELEMETRY_FRESH
    assert cs.build_connection_state({"n": record(at(budget + 1))}, NOW)["telemetry"] == cs.TELEMETRY_STALE

    # IDLE phase (`no_new_bar`) — the documented 120s, unchanged.
    idle = cs.DEFAULT_STALE_AFTER_S
    idle_rec = record(at(idle), cycle={"status": "no_new_bar"})
    idle_past = record(at(idle + 1), cycle={"status": "no_new_bar"})
    assert cs.build_connection_state({"n": idle_rec}, NOW)["telemetry"] == cs.TELEMETRY_FRESH
    assert cs.build_connection_state({"n": idle_past}, NOW)["telemetry"] == cs.TELEMETRY_STALE


def test_connection_state_and_live_status_never_disagree():
    """M-NODE-READ-1 — ONE freshness verdict, proven across both surfaces.

    `instance_view` used to compute `now - published_at` against a flat 120s
    while `/api/live/status` applied the canonical rule: arrival clock, phase
    aware. During any recompute the two genuinely disagreed about the same
    snapshot at the same instant — one node, two answers.

    This pins them together at the case that used to break: a node six minutes
    into a legitimate recompute.
    """
    six_minutes = (NOW - timedelta(seconds=360)).isoformat().replace("+00:00", "Z")
    rec = record(six_minutes)
    rec["received_at"] = six_minutes
    canonical = lt.observation(rec["snapshot"], now=NOW, received_at=rec["received_at"])
    state = cs.build_connection_state({"n": rec}, NOW)
    assert canonical["stale"] is False
    assert state["telemetry"] == cs.TELEMETRY_FRESH
    assert state["instances"][0]["staleAfterSeconds"] == canonical["stale_after_seconds"]


# ── stale snapshot: stop claiming, do not start accusing ─────────────────────

def test_stale_snapshot_makes_the_node_unknown_not_disconnected():
    """Silence is ambiguous. The node keeps trading and protecting the account
    with the Control Tower offline (I-10), so staleness is not evidence of death."""
    old = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    state = cs.build_connection_state({"ui1-node": record(old)}, NOW)
    assert state["telemetry"] == cs.TELEMETRY_STALE
    assert state["node"] == cs.NODE_UNKNOWN
    assert state["node"] != cs.NODE_DISCONNECTED


def test_stale_telemetry_discards_the_previous_bridge_reading():
    """A healthy bridge observation from six hours ago is not a current one."""
    old = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    rec = record(old, market={"available": True, "feed_healthy": True})
    state = cs.build_connection_state({"ui1-node": rec}, NOW)
    assert state["bridge"] == cs.BRIDGE_UNKNOWN, "a stale reading was carried forward"


# ── bridge: node-reported MT5 facts only ─────────────────────────────────────

def test_healthy_feed_reports_a_healthy_bridge():
    rec = record(market={"available": True, "feed_healthy": True})
    assert cs.build_connection_state({"n": rec}, NOW)["bridge"] == cs.BRIDGE_HEALTHY


def test_unhealthy_feed_is_degraded_not_unavailable():
    rec = record(market={"available": True, "feed_healthy": False})
    state = cs.build_connection_state({"n": rec}, NOW)
    assert state["bridge"] == cs.BRIDGE_DEGRADED


def test_unreadable_broker_snapshot_is_positive_evidence_of_unavailability():
    rec = record(reconciliation={"snapshot_status": "unavailable"})
    state = cs.build_connection_state({"n": rec}, NOW)
    assert state["bridge"] == cs.BRIDGE_UNAVAILABLE
    assert "could not read" in state["instances"][0]["bridgeEvidence"]


def test_frozen_reconciliation_degrades_the_bridge():
    rec = record(reconciliation={"frozen": True, "snapshot_status": "ok"})
    assert cs.build_connection_state({"n": rec}, NOW)["bridge"] == cs.BRIDGE_DEGRADED


def test_unsampled_bridge_is_unknown_not_unavailable():
    """The distinction that matters: not looking is not the same as looking and
    failing."""
    state = cs.build_connection_state({"n": record()}, NOW)
    assert state["bridge"] == cs.BRIDGE_UNKNOWN
    assert state["bridge"] != cs.BRIDGE_UNAVAILABLE


def test_legacy_snapshot_yields_unknown_bridge_not_a_fault():
    """The pinned pre-UI-2 node carries no market block at all."""
    rec = record(market={"available": False})
    rec["legacy_source"] = True
    state = cs.build_connection_state({"n": rec}, NOW)
    assert state["bridge"] == cs.BRIDGE_UNKNOWN
    assert state["instances"][0]["legacySource"] is True


# ── node: the only route to `disconnected` is positive evidence ──────────────

def test_beacon_unavailable_is_the_only_path_to_disconnected():
    fresh = {"n": record()}
    assert cs.build_connection_state(fresh, NOW, beacon="ALIVE")["node"] == cs.NODE_CONNECTED
    assert cs.build_connection_state(fresh, NOW, beacon=None)["node"] == cs.NODE_CONNECTED
    assert cs.build_connection_state(fresh, NOW, beacon="UNAVAILABLE")["node"] == cs.NODE_DISCONNECTED


def test_absent_beacon_never_implies_absent_node():
    """In the target topology the node's files are not visible here at all."""
    old = (NOW - timedelta(hours=6)).isoformat().replace("+00:00", "Z")
    state = cs.build_connection_state({"n": record(old)}, NOW, beacon=None)
    assert state["node"] == cs.NODE_UNKNOWN


# ── malformed / unknown / unsupported schema are distinguishable ─────────────

@pytest.mark.parametrize("mutate, problem", [
    (lambda s: s.pop("schema_version"), cs.PROBLEM_UNKNOWN_SCHEMA),
    (lambda s: s.update(schema_version="ct.node-telemetry.v9"), cs.PROBLEM_UNSUPPORTED_SCHEMA),
    (lambda s: s.pop("reconciliation"), cs.PROBLEM_MALFORMED),
    (lambda s: s.update(published_at="not-a-time"), cs.PROBLEM_MALFORMED),
    (lambda s: s.update(runtime=[]), cs.PROBLEM_MALFORMED),
])
def test_unusable_snapshots_are_classified_distinctly(mutate, problem):
    snap = snapshot()
    mutate(snap)
    state = cs.build_connection_state(
        {"n": {"snapshot": snap, "received_at": "2026-07-25T12:00:00Z"}}, NOW)
    view = state["instances"][0]
    assert view["problem"] == problem
    assert state["telemetry"] == cs.TELEMETRY_UNAVAILABLE
    # An unusable snapshot is evidence of a read failure, never of node health.
    assert state["node"] == cs.NODE_UNKNOWN
    assert state["bridge"] == cs.BRIDGE_UNKNOWN


def test_malformed_snapshot_never_reports_an_age():
    snap = snapshot(); snap.pop("cycle")
    state = cs.build_connection_state(
        {"n": {"snapshot": snap, "received_at": "x"}}, NOW)
    assert state["instances"][0]["ageSeconds"] is None
    assert state["instances"][0]["publishedAt"] is None


# ── multiple instances ───────────────────────────────────────────────────────

def test_multiple_instances_are_reported_independently():
    old = (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    state = cs.build_connection_state(
        {"node-a": record(), "node-b": record(old)}, NOW)
    assert state["instanceCount"] == 2
    by_id = {v["instanceId"]: v for v in state["instances"]}
    assert by_id["node-a"]["telemetry"] == cs.TELEMETRY_FRESH
    assert by_id["node-a"]["node"] == cs.NODE_CONNECTED
    assert by_id["node-b"]["telemetry"] == cs.TELEMETRY_STALE
    assert by_id["node-b"]["node"] == cs.NODE_UNKNOWN


def test_fleet_rollup_takes_the_worst_state_never_the_best():
    """One silent node must not hide behind a healthy one."""
    old = (NOW - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
    state = cs.build_connection_state(
        {"node-a": record(market={"available": True, "feed_healthy": True}),
         "node-b": record(old)}, NOW)
    assert state["telemetry"] == cs.TELEMETRY_STALE      # not "fresh"
    assert state["node"] == cs.NODE_UNKNOWN              # not "connected"
    assert state["bridge"] == cs.BRIDGE_UNKNOWN          # not "healthy"


def test_one_broken_instance_does_not_erase_a_good_one():
    broken = snapshot(); broken.pop("market")
    state = cs.build_connection_state(
        {"node-a": record(), "node-b": {"snapshot": broken, "received_at": "x"}}, NOW)
    by_id = {v["instanceId"]: v for v in state["instances"]}
    assert by_id["node-a"]["problem"] is None
    assert by_id["node-b"]["problem"] == cs.PROBLEM_MALFORMED
    assert by_id["node-a"]["telemetry"] == cs.TELEMETRY_FRESH


# ── API surface ──────────────────────────────────────────────────────────────

def test_endpoint_returns_first_run_before_any_telemetry(iso_store):
    body = client.get("/api/live/connection").json()
    assert body["firstRun"] is True
    assert body["telemetry"] == cs.TELEMETRY_NEVER
    assert body["instances"] == []


def test_endpoint_reflects_a_real_ingested_snapshot(iso_store):
    from datetime import datetime as _dt
    snap = snapshot(_dt.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    market={"available": True, "feed_healthy": True})
    assert client.post("/api/live/ingest", json=snap).status_code == 200
    body = client.get("/api/live/connection").json()
    assert body["firstRun"] is False
    assert body["telemetry"] == cs.TELEMETRY_FRESH
    assert body["node"] == cs.NODE_CONNECTED
    assert body["bridge"] == cs.BRIDGE_HEALTHY
    assert body["instanceCount"] == 1
    assert body["instances"][0]["instanceId"] == "ui1-node"


def test_endpoint_exposes_no_credentials_or_paths(iso_store):
    client.post("/api/live/ingest", json=snapshot(
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")))
    text = client.get("/api/live/connection").text
    for forbidden in ("password", "MT5_LOGIN", "MT5_PASSWORD", "Traceback", "/Users/",
                      "nonce", "digest"):
        assert forbidden not in text


def test_live_status_no_longer_publishes_a_single_connected_boolean(iso_store):
    """UI-1: `connected` ignored freshness entirely — a node dead for days read as
    connected. Connection is four dimensions with separate evidence."""
    body = client.get("/api/live/status").json()
    assert "connected" not in body


def test_backend_never_reports_its_own_availability():
    """`backend` is the client's judgement of whether it could reach the API at
    all; a backend claiming `available` in its own payload would be vacuous."""
    body = client.get("/api/live/connection").json()
    assert "backend" not in body


# ── UI-2 regression: the telemetry contract itself is untouched ──────────────

def test_ui2_telemetry_contract_is_unchanged(iso_store):
    from datetime import datetime as _dt
    snap = snapshot(_dt.now(timezone.utc).isoformat().replace("+00:00", "Z"))
    assert client.post("/api/live/ingest", json=snap).status_code == 200
    entry = client.get("/api/live/status?instance_id=ui1-node").json()
    assert entry["schema_version"] == lt.SCHEMA_VERSION
    assert entry["snapshot"]["instance_id"] == "ui1-node"
    assert entry["stale"] is False
    assert entry["age_seconds"] is not None
    # Malformed ingest still rejected, still cannot overwrite.
    assert client.post("/api/live/ingest", json={"schema_version": lt.SCHEMA_VERSION,
                                                 "instance_id": "x"}).status_code == 400
    assert client.get("/api/live/status?instance_id=ui1-node").status_code == 200


# ── AUDIT: future / clock-skew timestamps must not read as fresh ──────────────

def test_small_future_skew_within_the_window_still_reads_fresh():
    """A node a few seconds ahead of the tower is normal; clamping it to fresh is
    correct. The window is symmetric around zero."""
    ahead = (NOW + timedelta(seconds=cs.DEFAULT_STALE_AFTER_S - 1)).isoformat().replace("+00:00", "Z")
    state = cs.build_connection_state({"n": record(ahead)}, NOW)
    assert state["telemetry"] == cs.TELEMETRY_FRESH
    assert state["node"] == cs.NODE_CONNECTED


def test_implausible_future_timestamp_is_not_fresh_and_not_connected():
    """AUDIT: a snapshot dated beyond the window in the FUTURE (clock skew or a
    malformed node clock) must NOT create a false healthy state."""
    for offset in (timedelta(hours=1), timedelta(days=365 * 100)):
        far = (NOW + offset).isoformat().replace("+00:00", "Z")
        state = cs.build_connection_state({"n": record(far)}, NOW)
        assert state["telemetry"] == cs.TELEMETRY_STALE, offset
        assert state["node"] == cs.NODE_UNKNOWN, offset
        assert state["node"] != cs.NODE_CONNECTED, offset
        # Displayed age stays clamped at >= 0 (never a negative "ago").
        assert state["instances"][0]["ageSeconds"] >= 0.0


def test_freshness_bound_is_symmetric_around_zero():
    from connection_state import telemetry_state as ts
    thr = cs.DEFAULT_STALE_AFTER_S
    assert ts(0.0, thr) == cs.TELEMETRY_FRESH
    assert ts(thr, thr) == cs.TELEMETRY_FRESH          # exactly old boundary
    assert ts(-thr, thr) == cs.TELEMETRY_FRESH         # exactly future boundary
    assert ts(thr + 1, thr) == cs.TELEMETRY_STALE      # just too old
    assert ts(-(thr + 1), thr) == cs.TELEMETRY_STALE   # just too future
    assert ts(None, thr) == cs.TELEMETRY_NEVER


def test_ui2_observation_flags_an_implausible_future_snapshot_stale():
    """The UI-2 /api/live/status read envelope shares the same rule."""
    far = (NOW + timedelta(hours=2)).isoformat().replace("+00:00", "Z")
    obs = lt.observation(snapshot(far), now=NOW)
    assert obs["stale"] is True
    assert obs["age_seconds"] >= 0.0


def test_connection_state_shares_one_freshness_definition_with_ui2():
    assert cs.DEFAULT_STALE_AFTER_S == lt.DEFAULT_STALE_AFTER_S
