"""M-NODE-READ-1 — node operational truth is admitted; broker truth still is not.

THE DEFECT THIS SUITE PINS DOWN
    `NodeOperationalView` carries provenance `node-telemetry`, and every ordinary
    surface filtered nodes through the BROKER gate, which admits `live_mt5` and
    `node_mt5` only. `node-telemetry` is correctly absent from that set — a
    heartbeat is not an account — so every node view was dropped and a genuinely
    reporting VPS node was invisible everywhere.

THE SECOND, LARGER DEFECT THE FIX EXPOSED
    Restoring the cards as they stood would have been worse than the
    invisibility. Seven of the nine fields on a node view were the CONTROL TOWER
    describing itself: `adapter` was the Mac's active adapter kind,
    `connection_state` the Mac adapter's connection, `reconciliation_state` the
    Mac execution store's posture, `execution_mode` the Mac's governed mode,
    `authorization_summary` the Mac's grant, `active_scenario_count` the Mac's
    scenario store, `broker` the Mac broker snapshot's provenance — and
    `account_fingerprint_masked` fell back to the Mac broker snapshot's account,
    which under the mock adapter is the FIXTURE account, masked.

    All of it was stamped `node-telemetry` and displayed on a card titled with
    the VPS node's instance id. A fabrication with a real node's name on it.

WHAT IS NOT TESTED HERE
    Any live VPS. Every snapshot below is synthetic and shaped exactly like
    `ct.node-telemetry.v1`, so what is proved is the admission and projection
    behaviour — the part that must be right before real telemetry drives it.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import broker_provenance as bp                                       # noqa: E402
import live_telemetry as lt                                          # noqa: E402
import node_provenance as np_                                        # noqa: E402
import operational_projection as op                                  # noqa: E402

NOW = "2026-08-02T12:00:00Z"
NOW_DT = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_ago: float) -> str:
    return (NOW_DT - timedelta(seconds=seconds_ago)).isoformat().replace("+00:00", "Z")


def _snapshot(*, instance_id="vps-node-1", cycle_status="ok", frozen=False,
              kill_switch=False, cycle_frozen=False, snapshot_status="ok",
              positions=None, health_available=False, engine=None, note=None):
    return {
        "schema_version": lt.SCHEMA_VERSION,
        "instance_id": instance_id,
        "published_at": _iso(25),
        "cycle": {"status": cycle_status, "sequence": 412,
                  "last_boundary": "2026-08-02T11:45:00Z",
                  "last_bar_time": "2026-08-02T11:44:00Z", "note": note},
        "runtime": {"mode": "dry_run", "submission_disabled": True,
                    "kill_switch_active": kill_switch,
                    "open_eligibility": {"eligible": None, "reasons": []}},
        "engine": engine if engine is not None else {
            "strategy_family": "lux", "engine_version_actual": "lux@1.4.2",
            "engine_version_expected": "lux@1.4.2",
            "config_fingerprint": "a1b2c3d4e5f60718", "input_revision": "rev-9",
            "symbol": "EURUSD", "timeframe": "M15",
            "deployment_profile": "vps-dry-run", "data_seam": "dukascopy->mt5"},
        "account": {
            "identity": {"available": False, "fingerprint": None},
            "health": ({"available": True, "balance": 4211.5, "equity": 4180.25,
                        "free_margin": 4000.0, "trade_allowed": False,
                        "trade_expert": True, "currency": "USD"}
                       if health_available else {"available": False}),
        },
        "arming": {"status": "unarmed", "armed": False},
        "market": {"available": False},
        "reconciliation": {"available": True, "frozen": frozen,
                           "snapshot_status": snapshot_status},
        "risk": {},
        "positions": positions if positions is not None else [],
        "execution": {"cycle_frozen": cycle_frozen},
    }


def _entry(*, instance_id="vps-node-1", stale=False, received_ago=20.0,
           legacy=False, **snap_kw):
    """One read-side observation envelope, as `_live_status_entry` produces it."""
    snapshot = _snapshot(instance_id=instance_id, **snap_kw)
    return {
        "instance_id": instance_id,
        "published_at": snapshot["published_at"],
        "received_at": _iso(received_ago),
        "stale": stale,
        "age_seconds": 25.0,
        "liveness_age_seconds": received_ago,
        "liveness_stale": stale,
        "data_stale": False,
        "freshness_basis": "received_at",
        "stale_after_seconds": lt.RECOMPUTE_STALE_AFTER_S,
        "legacy_source": legacy,
        "snapshot": snapshot,
    }


def _sources(**over):
    """Sources with a HOSTILE local environment: the mock adapter is active and
    the fixture broker snapshot is present. Every test below must show that none
    of it reaches a node view."""
    base = dict(
        adapter_kind=lambda: "mock",
        connection_state=lambda: "Connected",
        execution_mode=lambda: "observe",
        authorization=lambda: {"granted": True},
        reconciliation_posture=lambda: {"criticalUnresolved": True},
        broker_snapshot=lambda: {
            "at": NOW, "provenance": "mock-fixture",
            "accountIdentity": "acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F",
            "positions": [{"positionId": "FIXTURE-P1", "canonicalSymbol": "EURUSD",
                           "side": "long", "size": 0.1, "state": "open"}],
            "orders": [{"orderId": "FIXTURE-O1", "canonicalSymbol": "EURUSD",
                        "side": "long", "size": 0.2, "state": "pending"}],
            "accounts": [{"accountId": "acct_1", "balance": 100000.0,
                          "equity": 100412.0, "baseCurrency": "USD"}],
        },
    )
    base.update(over)
    return op.ProjectionSources(**base)


# ══════════════════════════════════════════════════════════════════════════════
# 1–4  Admission: node truth is admitted, and grants nothing else
# ══════════════════════════════════════════════════════════════════════════════

def test_1_canonical_node_telemetry_is_admitted_as_node_truth():
    node = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert node.provenance == np_.PROV_NODE_TELEMETRY
    assert np_.is_authoritative_node_observation({"provenance": node.provenance})
    assert node.lifecycle_state == np_.NODE_CURRENT
    assert node.node_id == "vps-node-1"


def test_2_node_provenance_is_never_admitted_as_broker_truth():
    """The load-bearing separation, asserted in BOTH directions."""
    assert bp.is_broker_truth(np_.PROV_NODE_TELEMETRY) is False
    assert bp.is_broker_truth(np_.PROV_FIXTURE_NODE) is False
    # ...and broker origins are not node observations either.
    for origin in (bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5, bp.PROV_MOCK_FIXTURE):
        assert np_.is_authoritative_node_observation({"provenance": origin}) is False
    # The two admitted sets are disjoint. If they ever intersect, one authority
    # has begun granting the other.
    assert not (np_.AUTHORITATIVE_NODE_PROVENANCE
                & {bp.PROV_LIVE_MT5, bp.PROV_NODE_MT5})


def test_3_fixture_and_unknown_node_provenance_fail_closed():
    for value in (np_.PROV_FIXTURE_NODE, "fixture", "mock", "NODE-TELEMETRY",
                  "node_telemetry", "remote-node", np_.PROV_ABSENT, "", None, 7):
        assert np_.is_authoritative_node_observation({"provenance": value}) is False, value
    assert np_.is_authoritative_node_observation(None) is False
    assert np_.is_authoritative_node_observation({}) is False
    assert np_.for_node_observation(validated=False) == np_.PROV_ABSENT


def test_4_mock_adapter_configuration_cannot_create_a_node():
    """No entries means no node, whatever the local adapter is doing."""
    nodes = op.build_nodes(_sources(node_entries=list), now=NOW)
    assert len(nodes) == 1                       # one EXPLICIT absent view
    assert nodes[0].provenance == np_.PROV_ABSENT
    assert nodes[0].lifecycle_state == np_.NODE_ABSENT
    assert np_.is_authoritative_node_observation({"provenance": nodes[0].provenance}) is False


# ══════════════════════════════════════════════════════════════════════════════
# 5–9  Lifecycle states stay distinct
# ══════════════════════════════════════════════════════════════════════════════

def test_5_current_node_is_current():
    node = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_CURRENT
    assert node.degraded_reasons == ()
    assert node.freshness.stale is False


def test_6_stale_node_stays_admitted_and_visibly_stale():
    node = op.build_nodes(
        _sources(node_entries=lambda: [_entry(stale=True, received_ago=4000)]), now=NOW)[0]
    assert node.provenance == np_.PROV_NODE_TELEMETRY      # still genuine
    assert node.lifecycle_state == np_.NODE_STALE
    assert node.freshness.stale is True
    assert any("stale" in w for w in node.warnings)


@pytest.mark.parametrize("kwargs,expected", [
    ({"cycle_status": "error"}, "node cycle status: error"),
    ({"cycle_status": "frozen_pending_recovery"}, "node cycle status: frozen_pending_recovery"),
    ({"kill_switch": True}, "node reports its kill switch is active"),
    ({"cycle_frozen": True}, "node froze its execution cycle"),
    ({"frozen": True}, "node froze reconciliation against broker state"),
    ({"snapshot_status": "unavailable"}, "node could not read its broker position snapshot"),
])
def test_7_degraded_node_stays_admitted_and_names_the_failure(kwargs, expected):
    node = op.build_nodes(_sources(node_entries=lambda: [_entry(**kwargs)]), now=NOW)[0]
    assert node.provenance == np_.PROV_NODE_TELEMETRY      # real data about real trouble
    assert node.lifecycle_state == np_.NODE_DEGRADED
    assert expected in node.degraded_reasons


def test_7b_degraded_outranks_stale():
    """A node that reported a freeze and then went quiet still reported a freeze.
    Showing only "stale" would replace a specific failure with a vague one."""
    node = op.build_nodes(
        _sources(node_entries=lambda: [_entry(stale=True, frozen=True)]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_DEGRADED
    assert node.freshness.stale is True                  # the staleness is still shown
    assert any("stale" in w for w in node.warnings)


def test_8_stopped_is_not_synthesised_from_silence():
    """`ct.node-telemetry.v1` carries NO stopped signal.

    A node that halts stops publishing, which is indistinguishable from one that
    cannot reach this tower — and invariant I-10 says the node keeps trading and
    protecting the account while the tower is offline. So silence is ambiguous,
    and a `stopped` state would assert a halt on the evidence of a network
    problem. This is a DEFERRED CONTRACT GAP, deliberately not filled.
    """
    assert not hasattr(np_, "NODE_STOPPED")
    states = {np_.NODE_ABSENT, np_.NODE_CURRENT, np_.NODE_STALE, np_.NODE_DEGRADED}
    assert "stopped" not in states
    node = op.build_nodes(
        _sources(node_entries=lambda: [_entry(stale=True, received_ago=99999)]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_STALE          # NOT "stopped"


def test_9_absent_node_is_unavailable_not_a_zero_metric():
    node = op.build_nodes(_sources(node_entries=list), now=NOW)[0]
    assert node.freshness.available is False
    assert node.open_position_count is None
    assert node.open_order_count is None
    assert node.active_scenario_count is None
    assert node.lifecycle_state == np_.NODE_ABSENT


# ══════════════════════════════════════════════════════════════════════════════
# 10–12  The node's facts are the node's; the tower's are not
# ══════════════════════════════════════════════════════════════════════════════

def test_10_node_with_no_account_observation_is_still_a_visible_node():
    """The state this milestone exists to make expressible: node operational,
    broker unknown."""
    node = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_CURRENT
    assert node.mt5_observation is None            # said nothing — not disconnected
    accounts = op.build_accounts(_sources(node_entries=lambda: [_entry()]), now=NOW)
    assert all(a.provenance != bp.PROV_NODE_MT5 for a in accounts)


def test_10b_mt5_observation_only_when_the_node_actually_said_something():
    seen = op.build_nodes(
        _sources(node_entries=lambda: [_entry(health_available=True)]), now=NOW)[0]
    assert seen.mt5_observation == "observed"
    gone = op.build_nodes(
        _sources(node_entries=lambda: [_entry(snapshot_status="unavailable")]), now=NOW)[0]
    assert gone.mt5_observation == "unreachable"
    # An unsampled cycle is the ORDINARY case and is not a disconnection.
    quiet = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert quiet.mt5_observation is None


def test_11_pending_order_exposure_stays_unknown_not_zero():
    node = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert node.open_order_count is None
    # A genuinely empty mirror IS a real measurement of zero.
    assert node.open_position_count == 0


def test_12_no_tower_state_reaches_a_node_view():
    """The core assertion of this milestone.

    The sources are deliberately hostile: mock adapter active, a fixture broker
    snapshot with a fixture position, a fixture order, a fixture account and a
    critical reconciliation posture. None of it may appear on the node.
    """
    node = op.build_nodes(
        _sources(node_entries=lambda: [_entry(positions=[{"trade_id": "t1",
                                                          "broker_ticket": 555}])]),
        now=NOW)[0]
    assert node.open_position_count == 1           # the NODE's one mirrored position
    for field in ("adapter", "broker", "connection_state", "execution_mode",
                  "authorization_summary", "reconciliation_state",
                  "active_scenario_count", "account_fingerprint_masked"):
        assert getattr(node, field) is None, field
    serialized = str(node.as_dict())
    for leak in ("mock", "FIXTURE-P1", "FIXTURE-O1", "100000", "100412",
                 "acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F", "observe"):
        assert leak not in serialized, leak
    assert "unresolved critical reconciliation" not in node.warnings


def test_12b_tower_warnings_are_raised_at_the_summary_not_on_a_node():
    """The warnings are real and needed — they are just not facts about a node."""
    summary = op.build_summary(_sources(node_entries=lambda: [_entry()]), now=NOW)
    assert "unresolved critical reconciliation" in summary.warnings
    for node in summary.nodes:
        assert "unresolved critical reconciliation" not in node.warnings


# ══════════════════════════════════════════════════════════════════════════════
# 13–17  Identity and freshness
# ══════════════════════════════════════════════════════════════════════════════

def test_13_multiple_nodes_retain_distinct_identities():
    entries = [_entry(instance_id="vps-b", cycle_status="error"),
               _entry(instance_id="vps-a")]
    nodes = op.build_nodes(_sources(node_entries=lambda: entries), now=NOW)
    assert [n.node_id for n in nodes] == ["vps-a", "vps-b"]      # stable ordering
    by_id = {n.node_id: n for n in nodes}
    assert by_id["vps-a"].lifecycle_state == np_.NODE_CURRENT
    assert by_id["vps-b"].lifecycle_state == np_.NODE_DEGRADED


def test_14_one_node_cannot_overwrite_another():
    entries = [_entry(instance_id="vps-a"), _entry(instance_id="vps-b")]
    forward = op.build_nodes(_sources(node_entries=lambda: entries), now=NOW)
    backward = op.build_nodes(
        _sources(node_entries=lambda: list(reversed(entries))), now=NOW)
    assert [n.as_dict() for n in forward] == [n.as_dict() for n in backward]
    assert len({n.node_id for n in forward}) == 2


def test_15_node_timestamps_cannot_assert_server_freshness():
    """A node publishing a flattering `published_at` gains nothing: the envelope
    judged liveness on ARRIVAL, and this projection passes that verdict through
    rather than re-deriving one from the node's own clock."""
    entry = _entry(stale=True, received_ago=9000)
    entry["published_at"] = _iso(1)                # node claims it published 1s ago
    entry["snapshot"]["published_at"] = entry["published_at"]
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_STALE
    assert node.freshness.stale is True
    assert node.freshness_basis == "received_at"


def test_16_the_projection_recomputes_no_freshness_verdict():
    """The envelope's verdict wins even when the arrival timestamp alone would
    have read fresh. Two authorities computing staleness is the defect M-TEL-1
    removed; this projection must not become a third."""
    entry = _entry(stale=True, received_ago=5)      # arrived 5s ago, envelope says stale
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert node.freshness.stale is True
    assert node.lifecycle_state == np_.NODE_STALE


def test_17_the_m_tel_1_decomposition_is_carried_through_verbatim():
    entry = _entry()
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert node.published_at == entry["published_at"]
    assert node.received_at == entry["received_at"]
    assert node.liveness_age_seconds == entry["liveness_age_seconds"]
    assert node.liveness_stale is entry["liveness_stale"]
    assert node.data_stale is entry["data_stale"]
    assert node.freshness_basis == entry["freshness_basis"]
    assert node.stale_after_seconds == entry["stale_after_seconds"]


# ══════════════════════════════════════════════════════════════════════════════
# 18–25  Sentinels, gating, bounds, exhaustiveness
# ══════════════════════════════════════════════════════════════════════════════

def test_18_no_fixture_deployment_or_account_sentinel_survives():
    summary = op.build_summary(_sources(node_entries=lambda: [_entry()]), now=NOW)
    serialized = str([n.as_dict() for n in summary.nodes])
    for sentinel in ("100000", "100412", "InTrade", "BullExpand", "regime@2.3.0",
                     "acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F", "FIXTURE"):
        assert sentinel not in serialized, sentinel


def test_19_account_admission_is_gated_independently_of_node_health():
    """A perfectly healthy node grants nothing about the account, and an account
    observation grants nothing about node health. Both directions."""
    healthy_node = _entry()          # publishing fine, account.available: false
    accounts = op.build_accounts(_sources(node_entries=lambda: [healthy_node]), now=NOW)
    assert all(a.provenance != bp.PROV_NODE_MT5 for a in accounts)
    # Reverse: an account observation on a DEGRADED node does not make it current.
    degraded = _entry(cycle_status="error", health_available=True)
    node = op.build_nodes(_sources(node_entries=lambda: [degraded]), now=NOW)[0]
    assert node.lifecycle_state == np_.NODE_DEGRADED
    admitted = op.build_accounts(_sources(node_entries=lambda: [degraded]), now=NOW)
    assert admitted[0].provenance == bp.PROV_NODE_MT5     # the account IS admitted


def test_20_no_execution_or_command_surface_is_introduced():
    """Node presence must not become execution safety. This milestone adds no
    non-GET route, and the node seam holds no execution vocabulary."""
    import inspect
    import server
    non_get = {
        route.path for route in server.app.routes
        if getattr(route, "methods", None)
        and {"POST", "PUT", "PATCH", "DELETE"} & route.methods
        and (route.path.startswith("/api/live") or route.path.startswith("/api/operations"))
    }
    assert non_get == {"/api/live/ingest", "/api/live-runtime/tick"}, sorted(non_get)
    source = inspect.getsource(np_)
    for forbidden in ("authorize", "submit", "order_send", "execute", "arm("):
        assert forbidden not in source, forbidden


def test_21_the_node_seam_reads_no_fixture_and_no_broker_path():
    import inspect
    source = inspect.getsource(np_)
    for forbidden in ("fixture_world", "broker_adapter", "get_broker", "WORLD"):
        assert forbidden not in source, forbidden


def test_22_node_reported_text_is_bounded_and_sanitised():
    hostile = "x" * 5000 + "\x00\x07 dangerous"
    entry = _entry(note=hostile,
                   engine={"deployment_profile": hostile, "symbol": "EURUSD",
                           "engine_version_actual": hostile})
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    for value in (node.cycle_note, node.deployment_profile, node.engine_version):
        assert value is not None
        assert len(value) <= np_.MAX_DETAIL_CHARS
        assert "\x00" not in value and "\x07" not in value
    assert np_.clip_detail("") is None
    assert np_.clip_detail("   ") is None
    assert np_.clip_detail(12345) is None


def test_23_engine_identity_appears_only_when_the_node_reported_it():
    absent = op.build_nodes(
        _sources(node_entries=lambda: [_entry(engine={})]), now=NOW)[0]
    for field in ("engine_version", "engine_version_expected", "config_fingerprint",
                  "deployment_profile", "strategy_family", "input_revision",
                  "symbol", "timeframe", "data_seam"):
        assert getattr(absent, field) is None, field
    present = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert present.engine_version == "lux@1.4.2"
    assert present.config_fingerprint == "a1b2c3d4e5f60718"


def test_24_absent_numerics_and_booleans_never_become_zero_or_false():
    entry = _entry()
    entry["snapshot"]["cycle"]["sequence"] = "412"        # wrong type, not an int
    entry["snapshot"]["runtime"]["kill_switch_active"] = "no"
    entry["snapshot"]["positions"] = "not-a-list"
    node = op.build_nodes(_sources(node_entries=lambda: [entry]), now=NOW)[0]
    assert node.cycle_sequence is None                    # not 0
    assert node.kill_switch_active is None                # not False
    assert node.open_position_count is None               # not 0
    # A genuine False and a genuine 0 still survive.
    ok = op.build_nodes(_sources(node_entries=lambda: [_entry()]), now=NOW)[0]
    assert ok.kill_switch_active is False
    assert ok.open_position_count == 0
    assert ok.cycle_sequence == 412


def test_25_the_lifecycle_mapping_is_exhaustive_and_fails_closed():
    for bad in (None, "not-a-dict", 7, [], ""):
        assert np_.classify_lifecycle(bad, stale=False) == np_.NODE_ABSENT
        assert np_.degraded_reasons(bad) == ()
        assert np_.mt5_connection_observation(bad) is None
    # Every lifecycle value the projection can emit is one the frontend knows.
    emitted = {np_.NODE_ABSENT, np_.NODE_CURRENT, np_.NODE_STALE, np_.NODE_DEGRADED}
    gate = (REPO_ROOT / "frontend" / "src" / "lib" / "nodeProvenance.ts").read_text()
    for state in emitted:
        assert f"'{state}'" in gate, state
    assert f"PROV_NODE_TELEMETRY = '{np_.PROV_NODE_TELEMETRY}'" in gate


def test_25b_a_malformed_stored_record_yields_no_node_rather_than_a_fake_one():
    for broken in ({"instance_id": "x"}, {"instance_id": "x", "snapshot": "nope"},
                   "not-a-dict", None):
        nodes = op.build_nodes(_sources(node_entries=lambda b=broken: [b]), now=NOW)
        assert nodes == () or all(n.provenance == np_.PROV_ABSENT for n in nodes)
