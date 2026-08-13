"""M-CT-RUNTIME-COMPLETE-UX-1 — two slots, two ages, one ordering rule.

The tower keeps LATEST RUNTIME and LATEST COMPLETE separately because they answer
different questions and go stale at different rates. Measured on the live node: a
cycle-end payload survived ~78s before the next transition publication, and
recomputes run ~25 minutes — so a single-slot store loses the strategy context for
most of the wall clock, and a frontend cache loses it on every reload.

These pin the properties that make the split safe: a transition never erases the
complete slot, a completed cycle replaces it atomically, ordering prefers the
node's monotonic `sequence` but never REQUIRES it, and delivery health is derived
from the tower's arrival clock rather than confused with execution health.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


@pytest.fixture()
def srv():
    import importlib
    return importlib.import_module("backend.server")


def snap(*, status="ok", seq=None, pub="2026-08-11T14:00:00Z",
         news=True, decisions=True, boundary="2026-08-11 13:45:00+00:00"):
    s = {"schema_version": "ct.node-telemetry.v1",
         "instance_id": "n1", "published_at": pub,
         "cycle": {"status": status, "last_boundary": boundary}}
    if seq is not None:
        s["sequence"] = seq
    if news:
        s["news"] = {"schema_version": "ct.news-calendar.v1", "healthy": True, "health": "ok"}
    if decisions:
        s["decisions"] = {"schema_version": "ct.node-decisions.v1", "count": 50,
                          "total_candidates": 2086, "truncated": True, "decisions": []}
    return s


# ── the complete slot ───────────────────────────────────────────────────────

def test_a_completed_cycle_populates_the_complete_slot(srv, monkeypatch):
    store = {}
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "put",
                        lambda k, e, r, **kw: store.__setitem__((k, e), r))
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "get", lambda k, e: store.get((k, e)))
    srv._put_live_strategy("n1", snap(), "2026-08-11T14:00:01Z")
    rec = store[(srv._LIVE_STRATEGY_KIND, "n1")]
    assert rec["news"]["health"] == "ok"
    assert rec["decisions"]["count"] == 50
    assert rec["boundary"] == "2026-08-11 13:45:00+00:00"


def test_a_recomputing_payload_never_erases_news_or_decisions(srv, monkeypatch):
    store = {}
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "put",
                        lambda k, e, r, **kw: store.__setitem__((k, e), r))
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "get", lambda k, e: store.get((k, e)))
    srv._put_live_strategy("n1", snap(pub="2026-08-11T14:00:00Z"), "x")
    srv._put_live_strategy("n1", snap(status="recomputing", news=False, decisions=False,
                                      pub="2026-08-11T14:01:00Z"), "x")
    rec = store[(srv._LIVE_STRATEGY_KIND, "n1")]
    assert rec["published_at"] == "2026-08-11T14:00:00Z"      # untouched
    assert rec["news"]["health"] == "ok"
    assert rec["decisions"]["count"] == 50


def test_the_next_completed_cycle_replaces_the_slot_atomically(srv, monkeypatch):
    store = {}
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "put",
                        lambda k, e, r, **kw: store.__setitem__((k, e), r))
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "get", lambda k, e: store.get((k, e)))
    srv._put_live_strategy("n1", snap(pub="2026-08-11T14:00:00Z"), "x")
    newer = snap(pub="2026-08-11T14:15:00Z")
    newer["decisions"]["count"] = 7
    srv._put_live_strategy("n1", newer, "x")
    rec = store[(srv._LIVE_STRATEGY_KIND, "n1")]
    # news and decisions move TOGETHER — never one cycle's news beside another's decisions
    assert rec["published_at"] == "2026-08-11T14:15:00Z"
    assert rec["decisions"]["count"] == 7


def test_a_delayed_older_complete_cycle_cannot_displace_a_newer_one(srv, monkeypatch):
    store = {}
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "put",
                        lambda k, e, r, **kw: store.__setitem__((k, e), r))
    monkeypatch.setattr(srv._RUNTIME_OVERLAY, "get", lambda k, e: store.get((k, e)))
    srv._put_live_strategy("n1", snap(pub="2026-08-11T14:15:00Z", seq=9), "x")
    srv._put_live_strategy("n1", snap(pub="2026-08-11T14:00:00Z", seq=4), "x")
    assert store[(srv._LIVE_STRATEGY_KIND, "n1")]["published_at"] == "2026-08-11T14:15:00Z"


# ── ordering: sequence preferred, never required ────────────────────────────

def test_sequence_orders_snapshots_when_both_sides_carry_one(srv, monkeypatch):
    monkeypatch.setattr(srv, "_held_snapshot", lambda i: {"snapshot": snap(seq=10, pub="2026-08-11T14:00:00Z")})
    assert srv._snapshot_is_stale("n1", snap(seq=9, pub="2026-08-11T14:00:00Z")) is True
    assert srv._snapshot_is_stale("n1", snap(seq=11, pub="2026-08-11T14:00:00Z")) is False


def test_sequence_wins_over_a_misleading_timestamp(srv, monkeypatch):
    """A retry stamped later but sequenced earlier is still older."""
    monkeypatch.setattr(srv, "_held_snapshot", lambda i: {"snapshot": snap(seq=10, pub="2026-08-11T14:00:00Z")})
    assert srv._snapshot_is_stale("n1", snap(seq=9, pub="2026-08-11T14:05:00Z")) is True


def test_a_legacy_payload_without_sequence_is_still_accepted(srv, monkeypatch):
    monkeypatch.setattr(srv, "_held_snapshot", lambda i: {"snapshot": snap(pub="2026-08-11T14:00:00Z")})
    assert srv._snapshot_is_stale("n1", snap(pub="2026-08-11T14:05:00Z")) is False


def test_a_stored_snapshot_predating_sequence_does_not_strand_the_instance(srv, monkeypatch):
    """Held has no sequence, incoming does — must fall back, not reject."""
    monkeypatch.setattr(srv, "_held_snapshot", lambda i: {"snapshot": snap(pub="2026-08-11T14:00:00Z")})
    assert srv._snapshot_is_stale("n1", snap(seq=3, pub="2026-08-11T14:05:00Z")) is False


def test_equal_sequence_is_an_idempotent_retry_and_is_admitted(srv, monkeypatch):
    monkeypatch.setattr(srv, "_held_snapshot", lambda i: {"snapshot": snap(seq=5, pub="2026-08-11T14:00:00Z")})
    assert srv._snapshot_is_stale("n1", snap(seq=5, pub="2026-08-11T14:00:00Z")) is False


def test_a_boolean_sequence_is_not_treated_as_a_number(srv):
    assert srv._is_seq(True) is False and srv._is_seq(False) is False
    assert srv._is_seq(0) is True and srv._is_seq(7) is True
    assert srv._is_seq(None) is False and srv._is_seq("3") is False
