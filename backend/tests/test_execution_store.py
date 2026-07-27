"""ARCH-2 — the durable execution/order store."""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import execution_store as xs                                        # noqa: E402
import order_lifecycle as ol                                        # noqa: E402

AT = "2026-07-27T00:00:00Z"


def _intent(**over):
    kw = dict(intent_id=ol.new_intent_id(), command_name="CloseTrade",
              kind=ol.KIND_CLOSE, command_id="cmd_abc", idempotency_key="idem-1",
              created_at=AT, metadata={"payload": {"tradeId": "tr_1"}})
    kw.update(over)
    return ol.OrderIntent(**kw)


@pytest.fixture()
def store(tmp_path):
    return xs.ExecutionStore(tmp_path / "exec.db")


def _advance_to_closed(store, intent_id):
    store.record_transition(intent_id, ol.VALIDATED, at=AT, reason="validated")
    store.record_transition(intent_id, ol.READY, at=AT, reason="authorized")
    store.record_transition(intent_id, ol.CLOSE_PENDING, at=AT, reason="dispatching")
    store.record_transition(intent_id, ol.CLOSED, at=AT, reason="confirmed",
                            evidence="mock result")


# ── durability + linkage ─────────────────────────────────────────────────────

def test_state_and_linkage_survive_restart(tmp_path):
    path = tmp_path / "exec.db"
    s1 = xs.ExecutionStore(path)
    i = _intent()
    s1.create_intent(i, now=AT)
    _advance_to_closed(s1, i.intent_id)
    del s1
    s2 = xs.ExecutionStore(path)                     # fresh handle = restart
    row = s2.get_intent(i.intent_id)
    assert row["state"] == ol.CLOSED
    assert row["command_id"] == "cmd_abc"            # command → intent linkage
    assert row["idempotency_key"] == "idem-1"
    assert len(s2.transitions_of(i.intent_id)) == 5


def test_transitions_are_append_only_history():
    # No update/delete surface exists for transitions; every transition adds a row.
    import inspect
    source = inspect.getsource(xs)
    assert "DELETE FROM intent_transitions" not in source
    assert "UPDATE intent_transitions" not in source
    assert "DELETE FROM intents" not in source        # no eviction of audit history
    assert "DELETE FROM recon_runs" not in source
    assert "DELETE FROM recon_items" not in source


def test_invalid_transition_writes_nothing(store):
    i = _intent()
    store.create_intent(i, now=AT)
    before = store.transitions_of(i.intent_id)
    with pytest.raises(ol.LifecycleError):
        store.record_transition(i.intent_id, ol.FILLED, at=AT, reason="skip!")
    assert store.get_intent(i.intent_id)["state"] == ol.CREATED
    assert store.transitions_of(i.intent_id) == before   # atomic: nothing written


def test_duplicate_intent_id_fails_closed(store):
    i = _intent()
    store.create_intent(i, now=AT)
    with pytest.raises(xs.StoreError) as e:
        store.create_intent(i, now=AT)
    assert e.value.reason == "duplicate_intent_id"


def test_secrets_never_reach_disk(tmp_path):
    path = tmp_path / "exec.db"
    s = xs.ExecutionStore(path)
    s.create_intent(_intent(metadata={"api_token": "SUPERSECRETVALUE"}), now=AT)
    raw = path.read_bytes().decode("utf-8", "ignore")
    assert "SUPERSECRETVALUE" not in raw


# ── schema versioning + corruption fail closed ───────────────────────────────

def test_schema_version_is_stamped(store):
    assert store.schema_version() == xs.SCHEMA_VERSION


def test_newer_schema_version_fails_closed(tmp_path):
    path = tmp_path / "exec.db"
    xs.ExecutionStore(path)
    conn = sqlite3.connect(path)
    conn.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    conn.commit(); conn.close()
    with pytest.raises(xs.StoreError) as e:
        xs.ExecutionStore(path)
    assert e.value.reason == "unsupported_schema_version"


def test_corrupt_database_fails_closed(tmp_path):
    path = tmp_path / "exec.db"
    path.write_bytes(b"this is not a sqlite database at all" * 10)
    with pytest.raises(xs.StoreError):
        xs.ExecutionStore(path)


# ── restart recovery through the store ───────────────────────────────────────

def test_recover_fails_predispatch_and_queues_inflight_without_fabrication(store):
    pre = _intent(idempotency_key="a")
    store.create_intent(pre, now=AT)                  # state: created (pre-dispatch)
    inflight = _intent(idempotency_key="b")
    store.create_intent(inflight, now=AT)
    store.record_transition(inflight.intent_id, ol.VALIDATED, at=AT, reason="v")
    store.record_transition(inflight.intent_id, ol.READY, at=AT, reason="r")
    store.record_transition(inflight.intent_id, ol.CLOSE_PENDING, at=AT, reason="d")
    done = _intent(idempotency_key="c")
    store.create_intent(done, now=AT)
    _advance_to_closed(store, done.intent_id)

    result = store.recover(now="2026-07-27T00:01:00Z")
    assert store.get_intent(pre.intent_id)["state"] == ol.FAILED
    assert store.get_intent(inflight.intent_id)["state"] == ol.RECONCILIATION_REQUIRED
    assert store.get_intent(done.intent_id)["state"] == ol.CLOSED   # terminal untouched
    assert pre.intent_id in result["failedPreDispatch"]
    assert inflight.intent_id in result["queuedForReconciliation"]
    # And recovery is recorded as ordinary audited transitions, not silent edits.
    reasons = [t["reason"] for t in store.transitions_of(inflight.intent_id)]
    assert "restart_recovery_in_flight" in reasons
    assert "restart_requires_reconciliation" in reasons


def test_recover_twice_is_idempotent(store):
    i = _intent()
    store.create_intent(i, now=AT)
    store.recover(now="2026-07-27T00:01:00Z")
    first = store.get_intent(i.intent_id)["state"]
    store.recover(now="2026-07-27T00:02:00Z")        # terminal now — untouched
    assert store.get_intent(i.intent_id)["state"] == first == ol.FAILED


# ── reconciliation persistence ───────────────────────────────────────────────

def test_reconciliation_runs_persist_and_survive_restart(tmp_path):
    path = tmp_path / "exec.db"
    s = xs.ExecutionStore(path)
    s.save_reconciliation(
        {"reconId": "recon_1", "at": AT, "accountIdentity": "acc_mock",
         "expectedAccountIdentity": "acc_mock", "clean": False, "critical": True,
         "stale": False, "failed": False, "sources": {"brokerSnapshotAt": AT}},
        [{"class": "broker_only_order", "entityId": "ord_9", "critical": True,
          "detail": "unmatched"}])
    s2 = xs.ExecutionStore(path)
    latest = s2.latest_reconciliation()
    assert latest["recon_id"] == "recon_1" and latest["items"][0]["class"] == "broker_only_order"
    assert s2.unresolved_critical_count() == 1


def test_resolution_requires_evidence_and_is_a_new_fact(store):
    store.save_reconciliation(
        {"reconId": "recon_2", "at": AT, "accountIdentity": None,
         "expectedAccountIdentity": None, "clean": False, "critical": True,
         "stale": False, "failed": False, "sources": {}},
        [{"class": "unknown", "entityId": None, "critical": True, "detail": "?"}])
    seq = store.latest_reconciliation()["items"][0]["seq"]
    with pytest.raises(xs.StoreError):
        store.resolve_item(seq, at=AT, evidence="")
    store.resolve_item(seq, at=AT, evidence="operator verified against statement")
    assert store.unresolved_critical_count() == 0
    item = store.latest_reconciliation()["items"][0]
    assert item["resolved"] == 1 and item["resolved_evidence"]
