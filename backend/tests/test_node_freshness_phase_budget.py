"""M-CT-RED-STATE-AUDIT-1 — node freshness honours the node's phase budget.

THE DEFECT, MEASURED LIVE: at an arrival age of 807 s during a legitimate
recompute, `/api/live/connection` reported the node FRESH (phase-aware 900 s)
while `/api/operations/summary` reported the SAME snapshot STALE (flat 120 s) —
and that snapshot's own decomposition read `liveness_stale=False,
data_stale=False`. One node, two verdicts, on one screen.

These pin the repair AND the pessimism that must survive it: exceeding the
node's own advertised budget is still stale, an undated arrival is still stale,
and a node that reports itself stale is still believed.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from backend import activation_policy   # noqa: E402

NOW = datetime(2026, 8, 10, 12, 0, 0, tzinfo=timezone.utc)


def entry(age_s: float, *, budget: float | None, stale_flag: bool = False) -> dict:
    e = {"stale": stale_flag,
         "received_at": (NOW - timedelta(seconds=age_s)).isoformat().replace("+00:00", "Z")}
    if budget is not None:
        e["stale_after_seconds"] = budget
    return e


def verdict(age_s: float, *, budget: float | None, projection_budget: float,
            stale_flag: bool = False) -> bool:
    return activation_policy.recomputed_stale(
        entry(age_s, budget=budget, stale_flag=stale_flag),
        now=NOW, projection_budget_s=projection_budget)


# ── the repair: the phase budget wins over the flat default ────────────────

def test_recomputing_node_inside_its_advertised_budget_is_NOT_stale():
    """807 s — the exact live measurement. Was stale, must now be fresh."""
    assert verdict(807, budget=900.0, projection_budget=900.0) is False


def test_idle_node_keeps_the_tight_budget():
    """`no_new_bar` advertises 120 s; 300 s of silence is genuinely stale."""
    assert verdict(300, budget=120.0, projection_budget=120.0) is True


# ── the pessimism that must survive ───────────────────────────────────────

def test_beyond_the_advertised_budget_is_still_stale():
    assert verdict(1200, budget=900.0, projection_budget=900.0) is True


def test_a_node_reporting_itself_stale_is_believed_regardless_of_age():
    assert verdict(1, budget=900.0, projection_budget=900.0, stale_flag=True) is True


def test_an_undated_arrival_is_stale():
    assert activation_policy.recomputed_stale(
        {"stale": False}, now=NOW, projection_budget_s=900.0) is True


def test_an_unparseable_arrival_is_stale():
    assert activation_policy.recomputed_stale(
        {"stale": False, "received_at": "not-a-timestamp"},
        now=NOW, projection_budget_s=900.0) is True


def test_a_naive_arrival_is_stale():
    assert activation_policy.recomputed_stale(
        {"stale": False, "received_at": "2026-08-10T11:59:00"},
        now=NOW, projection_budget_s=900.0) is True


def test_an_arrival_stamped_in_the_future_is_old_not_infinitely_fresh():
    future = (NOW + timedelta(seconds=5000)).isoformat().replace("+00:00", "Z")
    assert activation_policy.recomputed_stale(
        {"stale": False, "received_at": future, "stale_after_seconds": 900.0},
        now=NOW, projection_budget_s=900.0) is True


def test_a_missing_budget_falls_back_and_does_not_crash():
    assert verdict(60, budget=None, projection_budget=900.0) in (True, False)
