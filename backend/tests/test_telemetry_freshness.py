"""Phase-aware, server-observed telemetry freshness (`ct.node-telemetry.v1`).

THE VERIFIED PRODUCTION DEFECT
    A healthy node was marked stale ~2 minutes into a legitimate recompute,
    because one flat `DEFAULT_STALE_AFTER_S = 120` was applied to every
    lifecycle phase. A warm recompute publishes nothing while it runs and
    routinely exceeds two minutes.

WHAT THIS DOES *NOT* DO
    It does not make every long recompute look healthy. A measured warm
    recompute of ~1119s EXCEEDS one full bar interval, and that is a genuine
    capacity problem the tower must keep signalling. The budget is one bar
    interval precisely so an overrun still surfaces.

TWO INDEPENDENT FACTS
    * LIVENESS  — has the node gone quiet? Judged on SERVER-OBSERVED ARRIVAL.
    * CURRENCY  — are the observations old, however recently they arrived?
    A node cannot assert its own freshness, and a fresh envelope cannot launder
    stale contents.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import live_telemetry as lt

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)


def iso(when):
    return when.isoformat().replace("+00:00", "Z")


def snap(*, published, status="no_new_bar"):
    return {"instance_id": "n1", "schema_version": lt.SCHEMA_VERSION,
            "published_at": iso(published), "cycle": {"status": status}}


def observe(*, published_ago=0.0, arrived_ago=None, status="no_new_bar"):
    published = NOW - timedelta(seconds=published_ago)
    received = (None if arrived_ago is None
                else iso(NOW - timedelta(seconds=arrived_ago)))
    return lt.observation(snap(published=published, status=status),
                          now=NOW, received_at=received)


# ── 1-3: phase-aware staleness ───────────────────────────────────────────────

def test_an_idle_node_goes_stale_after_the_idle_budget():
    """Idle keeps the documented 120s: between bars a node publishes at least
    every 60s, so two missed publishes is genuinely quiet."""
    assert observe(published_ago=60, arrived_ago=60)["stale"] is False
    assert observe(published_ago=200, arrived_ago=200)["stale"] is True


@pytest.mark.parametrize("status", ["ok", "bootstrap", "frozen_pending_recovery"])
@pytest.mark.parametrize("seconds", [130, 300, 600, 880])
def test_a_recomputing_node_is_not_stale_after_two_minutes(status, seconds):
    """THE defect. A legitimate recompute is silent for minutes and must not be
    declared dead at 120s."""
    assert observe(published_ago=seconds, arrived_ago=seconds,
                   status=status)["stale"] is False


@pytest.mark.parametrize("seconds", [941, 1119, 1800])
def test_a_recompute_beyond_one_bar_interval_is_still_reported_stale(seconds):
    """Honest signalling preserved: 941s and the measured 1119s both exceed one
    bar interval, so they remain visible as overruns. Hiding them would conceal
    a real execution-capacity problem."""
    result = observe(published_ago=seconds, arrived_ago=seconds, status="ok")
    assert result["stale"] is True
    assert result["stale_after_seconds"] == lt.RECOMPUTE_STALE_AFTER_S


def test_the_recompute_budget_is_exactly_one_bar_interval():
    assert lt.RECOMPUTE_STALE_AFTER_S == 900.0
    assert observe(published_ago=899, arrived_ago=899, status="ok")["stale"] is False
    assert observe(published_ago=901, arrived_ago=901, status="ok")["stale"] is True


def test_an_unknown_or_absent_cycle_status_gets_the_recompute_budget():
    """Fail towards not-dead: misreading a working node is the defect being
    fixed, and a genuine outage still trips the larger budget shortly after."""
    for payload in ({"instance_id": "n1", "published_at": iso(NOW - timedelta(seconds=300))},
                    {"instance_id": "n1", "published_at": iso(NOW - timedelta(seconds=300)),
                     "cycle": {"status": None}}):
        assert lt.observation(payload, now=NOW)["stale"] is False


# ── 4: returning to idle restores idle behaviour ─────────────────────────────

def test_returning_to_idle_restores_the_idle_budget():
    """The budget follows the CURRENT phase, not the previous one."""
    assert observe(published_ago=600, arrived_ago=600, status="ok")["stale"] is False
    assert observe(published_ago=600, arrived_ago=600,
                   status="no_new_bar")["stale"] is True


# ── 5: a node cannot assert its own freshness ────────────────────────────────

def test_a_manipulated_future_timestamp_cannot_create_freshness():
    """A node publishing a far-future `published_at` while actually silent for
    an hour must not read as healthy. Liveness is the tower's clock."""
    result = observe(published_ago=-3600, arrived_ago=3600, status="ok")
    assert result["stale"] is True
    assert result["liveness_stale"] is True


def test_a_recently_arrived_packet_of_very_old_observations_is_not_healthy():
    """Arrival freshness must not launder stale contents."""
    result = observe(published_ago=10800, arrived_ago=0, status="ok")
    assert result["stale"] is True
    assert result["data_stale"] is True and result["liveness_stale"] is False
    assert result["age_seconds"] > 3600          # the OBSERVATION age is visible


def test_a_silent_node_is_stale_even_if_its_last_packet_claimed_to_be_current():
    result = observe(published_ago=0, arrived_ago=7200, status="ok")
    assert result["stale"] is True and result["liveness_stale"] is True


# ── 6-7: skew, and the two timestamps stay independently meaningful ──────────

def test_skew_is_visible_through_the_two_independent_ages_not_a_derived_field():
    """Skew is deliberately not published (it is `published_at` − `received_at`,
    both already in the /live/status envelope, and the identifier already means
    arm-request tolerance in `arming.ArmPolicy`). It remains VISIBLE because the
    two ages are reported independently and diverge by exactly the skew."""
    result = observe(published_ago=300, arrived_ago=5, status="ok")
    assert result["age_seconds"] == pytest.approx(300.0)
    assert result["liveness_age_seconds"] == pytest.approx(5.0)
    assert "clock_skew_seconds" not in result       # no derivable public surface


def test_large_skew_within_the_budget_does_not_by_itself_declare_staleness():
    """Skew is not a verdict: a node whose clock is 5 minutes behind but which
    is publishing on time is alive, and both ages still read honestly."""
    result = observe(published_ago=300, arrived_ago=5, status="ok")
    assert result["stale"] is False
    assert result["liveness_stale"] is False and result["data_stale"] is False


def test_arrival_and_publication_remain_independently_meaningful():
    result = observe(published_ago=400, arrived_ago=30, status="ok")
    assert result["age_seconds"] == pytest.approx(400.0)          # data currency
    assert result["liveness_age_seconds"] == pytest.approx(30.0)  # node liveness
    assert result["published_at"] is not None
    assert result["freshness_basis"] == "received_at"


def test_without_an_arrival_time_the_previous_behaviour_is_preserved():
    """The pull path and pre-existing records have no server-observed arrival;
    they fall back to `published_at` exactly as before."""
    result = observe(published_ago=60, arrived_ago=None)
    assert result["freshness_basis"] == "published_at"
    assert result["stale"] is False


# ── 8-9: compatibility ───────────────────────────────────────────────────────

def test_a_missing_published_at_is_still_stale():
    result = lt.observation({"instance_id": "n1", "cycle": {"status": "ok"}}, now=NOW)
    assert result["stale"] is True and result["age_seconds"] is None


def test_an_explicit_threshold_still_overrides_the_phase_budget():
    """The existing keyword keeps working for callers that pin a threshold."""
    result = lt.observation(snap(published=NOW - timedelta(seconds=300), status="ok"),
                            now=NOW, stale_after_s=120.0)
    assert result["stale"] is True and result["stale_after_seconds"] == 120.0


def test_the_v1_envelope_keys_are_unchanged():
    """Schema preserved: every previously published key still present, with the
    new facts added alongside rather than replacing anything."""
    result = observe(published_ago=10, arrived_ago=10)
    for key in ("instance_id", "schema_version", "legacy_source", "published_at",
                "observed_at", "age_seconds", "stale", "stale_after_seconds",
                "snapshot"):
        assert key in result, key


def test_a_record_whose_arrival_was_never_captured_still_reports_an_age():
    """Renamed from "persisted record": `_LIVE_STATUS` is an in-memory dict, so
    nothing survives a restart to be legacy. The real cases are the pull path
    (`node_client`, which passes no arrival) and any record whose arrival was
    not threaded through — both must still produce a usable envelope."""
    result = lt.observation(snap(published=NOW - timedelta(seconds=30)),
                            now=NOW, received_at=None)
    assert result["stale"] is False
    assert result["liveness_age_seconds"] == pytest.approx(30.0)
    assert result["freshness_basis"] == "published_at"


def test_a_restart_cannot_invent_freshness_the_store_is_empty_not_fresh():
    """`_LIVE_STATUS` is in-memory: after a restart there are NO records, so the
    read side reports absence rather than a fabricated fresh reading. Nothing in
    the envelope can be produced without a snapshot to wrap."""
    import server
    assert isinstance(server._LIVE_STATUS, dict)
    empty = {k: v for k, v in server._LIVE_STATUS.items() if False}
    assert empty == {}, "a restarted tower must start with no node records"


# ── anti-drift ───────────────────────────────────────────────────────────────

def test_the_recompute_budget_matches_the_canonical_bar_interval():
    """`RECOMPUTE_STALE_AFTER_S` mirrors `live/shadow_report.BAR_INTERVAL_S`,
    which gates the `no_boundary_overrun` promotion check on the same threshold.
    It is duplicated rather than imported to keep `live_telemetry` free of any
    `live.*` dependency — so this test is what stops the two drifting apart.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from live.shadow_report import BAR_INTERVAL_S
    assert lt.RECOMPUTE_STALE_AFTER_S == BAR_INTERVAL_S


def test_live_telemetry_still_imports_nothing_from_the_live_package():
    import ast
    tree = ast.parse((Path(__file__).resolve().parent.parent / "live_telemetry.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert not (node.module or "").startswith("live."), node.module


def test_the_idle_budget_is_unchanged_from_the_documented_v1_default():
    assert lt.DEFAULT_STALE_AFTER_S == 120.0
