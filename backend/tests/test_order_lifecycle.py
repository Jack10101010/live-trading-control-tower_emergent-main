"""ARCH-2 — canonical order intent, identity, and the lifecycle state machine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import order_lifecycle as ol                                        # noqa: E402

AT = "2026-07-27T00:00:00Z"
LATER = "2026-07-27T00:00:05Z"


# ── identity ──────────────────────────────────────────────────────────────────

def test_id_generators_are_collision_resistant_and_prefixed():
    intents = {ol.new_intent_id() for _ in range(500)}
    recons = {ol.new_reconciliation_id() for _ in range(500)}
    assert len(intents) == 500 and len(recons) == 500
    assert all(i.startswith("intent_") for i in intents)
    assert all(r.startswith("recon_") for r in recons)


def test_id_prefixes_are_unambiguous_against_every_existing_prefix():
    existing = {"cmd_", "ev_", "tr_", "ord_", "pos_", "dpl_", "acctfp_", "brk_", "acc_"}
    for new in (ol.INTENT_PREFIX, ol.RECONCILIATION_PREFIX):
        for old in existing:
            assert not new.startswith(old) and not old.startswith(new)
    assert ol.INTENT_PREFIX != ol.RECONCILIATION_PREFIX


# ── the intent model ─────────────────────────────────────────────────────────

def _intent(**over):
    kw = dict(intent_id=ol.new_intent_id(), command_name="CloseTrade",
              kind=ol.KIND_CLOSE, created_at=AT)
    kw.update(over)
    return ol.OrderIntent(**kw)


def test_intent_is_immutable():
    i = _intent()
    with pytest.raises(Exception):
        i.kind = ol.KIND_CANCEL


def test_intent_rejects_unknown_kind_and_foreign_id():
    with pytest.raises(ValueError):
        _intent(kind="teleport")
    with pytest.raises(ValueError):
        _intent(intent_id="cmd_notanintent")


def test_intent_metadata_is_bounded_and_json_safe():
    with pytest.raises(ValueError):
        _intent(metadata={"blob": "x" * (ol.MAX_METADATA_BYTES + 100)})
    with pytest.raises(ValueError):
        _intent(metadata={"x": object()})


def test_intent_safe_view_redacts_metadata():
    i = _intent(metadata={"api_token": "TOPSECRETVALUE", "note": "fine"})
    view = i.safe_view()
    assert "TOPSECRETVALUE" not in str(view)
    assert view["metadata"]["note"] == "fine"


# ── the transition table ─────────────────────────────────────────────────────

def test_every_allowed_transition_validates():
    for frm, to in ol.ALLOWED_TRANSITIONS:
        t = ol.transition("intent_x", frm, to, at=AT, reason="r",
                          evidence="ev" if frm == ol.RECONCILED else "")
        assert t.from_state == frm and t.to_state == to


def test_terminal_states_appear_in_no_left_column():
    lefts = {frm for frm, _ in ol.ALLOWED_TRANSITIONS}
    assert not (lefts & ol.TERMINAL_STATES)


@pytest.mark.parametrize("frm,to", [
    (ol.CREATED, ol.FILLED),              # no skipping to completion
    (ol.READY, ol.FILLED),
    (ol.UNKNOWN, ol.FILLED),              # unknown never upgraded without evidence
    (ol.UNKNOWN, ol.CLOSED),
    (ol.UNKNOWN, ol.ACKNOWLEDGED),
    (ol.FILLED, ol.CLOSED),               # terminal protection
    (ol.CANCELLED, ol.CANCEL_PENDING),
    (ol.SAFETY_DENIED, ol.READY),
    (ol.CANCEL_PENDING, ol.CLOSED),       # a cancel request cannot confirm a close
    (ol.CLOSE_PENDING, ol.CANCELLED),
])
def test_forbidden_transitions_raise(frm, to):
    with pytest.raises(ol.LifecycleError):
        ol.transition("intent_x", frm, to, at=AT, reason="r", evidence="e")


def test_unknown_state_vocabulary_raises():
    with pytest.raises(ol.LifecycleError) as e:
        ol.transition("intent_x", "levitating", ol.FILLED, at=AT, reason="r")
    assert e.value.reason == "unknown_state"


def test_acknowledgement_is_distinct_from_fill_and_partial_from_full():
    assert (ol.SUBMITTED, ol.ACKNOWLEDGED) in ol.ALLOWED_TRANSITIONS
    assert (ol.ACKNOWLEDGED, ol.PARTIALLY_FILLED) in ol.ALLOWED_TRANSITIONS
    assert (ol.PARTIALLY_FILLED, ol.FILLED) in ol.ALLOWED_TRANSITIONS
    assert ol.ACKNOWLEDGED not in ol.TERMINAL_STATES
    assert ol.PARTIALLY_FILLED not in ol.TERMINAL_STATES
    assert ol.FILLED in ol.TERMINAL_STATES


def test_request_states_are_distinct_from_confirmations():
    # cancellation request != confirmed cancellation; close request != confirmed close
    for pending, confirmed in ((ol.CANCEL_PENDING, ol.CANCELLED),
                               (ol.CLOSE_PENDING, ol.CLOSED),
                               (ol.MODIFY_PENDING, ol.MODIFIED)):
        assert pending != confirmed
        assert (pending, confirmed) in ol.ALLOWED_TRANSITIONS
        assert pending not in ol.TERMINAL_STATES and confirmed in ol.TERMINAL_STATES


def test_reason_is_mandatory_and_evidence_required_from_reconciled():
    with pytest.raises(ol.LifecycleError) as e:
        ol.transition("intent_x", ol.CREATED, ol.VALIDATED, at=AT, reason="")
    assert e.value.reason == "reason_required"
    with pytest.raises(ol.LifecycleError) as e:
        ol.transition("intent_x", ol.RECONCILED, ol.FILLED, at=AT, reason="r", evidence="")
    assert e.value.reason == "evidence_required"
    t = ol.transition("intent_x", ol.RECONCILED, ol.FILLED, at=AT, reason="r",
                      evidence="broker statement row 12")
    assert t.evidence


def test_timestamps_are_monotonic():
    with pytest.raises(ol.LifecycleError) as e:
        ol.transition("intent_x", ol.CREATED, ol.VALIDATED, at=AT, reason="r",
                      previous_at=LATER)
    assert e.value.reason == "non_monotonic_timestamp"
    ol.transition("intent_x", ol.CREATED, ol.VALIDATED, at=LATER, reason="r",
                  previous_at=AT)                     # forward is fine
    ol.transition("intent_x", ol.CREATED, ol.VALIDATED, at=AT, reason="r",
                  previous_at=AT)                     # equal is fine (same instant)


def test_unknown_only_resolves_through_reconciliation():
    targets = {to for frm, to in ol.ALLOWED_TRANSITIONS if frm == ol.UNKNOWN}
    assert targets == {ol.RECONCILIATION_REQUIRED, ol.FAILED}


# ── restart recovery ─────────────────────────────────────────────────────────

def test_recovery_never_fabricates_completion():
    for state in ol.PRE_DISPATCH_STATES:
        assert ol.recovery_plan(state) == (ol.FAILED, "restart_before_dispatch")
    for state in ol.IN_FLIGHT_STATES:
        assert ol.recovery_plan(state) == (ol.UNKNOWN, "restart_recovery_in_flight")
    for state in ol.TERMINAL_STATES:
        assert ol.recovery_plan(state) is None
    assert ol.recovery_plan(ol.UNKNOWN) is None       # preserved, not upgraded
    assert ol.recovery_plan(ol.RECONCILIATION_REQUIRED) is None


def test_every_state_is_classified_exactly_once():
    groups = [ol.TERMINAL_STATES, ol.PRE_DISPATCH_STATES, ol.IN_FLIGHT_STATES,
              frozenset({ol.UNKNOWN, ol.RECONCILIATION_REQUIRED, ol.RECONCILED})]
    union = frozenset().union(*groups)
    assert union == ol.ALL_STATES
    assert sum(len(g) for g in groups) == len(ol.ALL_STATES)   # disjoint
