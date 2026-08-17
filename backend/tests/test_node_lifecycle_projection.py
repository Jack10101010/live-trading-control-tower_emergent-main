"""Setup lifecycle in node telemetry: terminal rows must not read as live.

WHAT WENT WRONG. `decisions._action` treated only `COHORT_DISABLED` and
`STATE_BLOCKED` as refusals; every other row without a `fill_time` became
`action="PENDING"`, and `eligible` was computed as `action != "REFUSED"`. So an
order block that price had already invalidated, or that the regime gate had
turned down, or that a news touch had cancelled, was published to the Control
Tower as a pending, eligible candidate. Measured on the live node: **19 of 50**
published decisions were terminal setups presented as live.

The fixtures below are REAL ROWS lifted from `live_state/frames/prev_trades.csv`
— L_2107 is the invalidated block from the audit, L_2106 and S_2108 are the
lifecycle controls, L_2109 is the genuinely-resting one. Hermetic copies, so the
test does not depend on a live frame that changes under it, but the shapes and
values are the engine's own.

`test_the_old_projection_would_have_failed_these` keeps the whole module
non-vacuous: it recomputes v1's rule and asserts it disagrees.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from live import decisions as D  # noqa: E402

# ── real rows, captured from the live frame ──────────────────────────────────

INVALIDATED = {  # L_2107 — the row named in the audit
    "trade_id": "L_2107", "ob_id": "2107", "direction": "bullish",
    "structure_tag": "BOS", "detection_time": "2026-08-12 12:30:00+00:00",
    "fill_time": None, "exit_time": "2026-08-12 16:20:00+00:00",
    "outcome": "INVALID", "cancel_reason": "invalidated_before_edge_entry",
    "missed_reason": "invalidated_before_fill", "entry": 1.15335,
    "stop": 1.15311, "tp": 1.15383, "rr_multiple": 2.0,
    "trigger_time": "2026-08-12 16:19:00+00:00",
    "armed_at": "2026-08-12 16:19:00+00:00",
    "portfolio_cohort_key": "EURUSD|unknown|bos_long",
}
REGIME_BLOCKED = {  # L_2077
    "trade_id": "L_2077", "ob_id": "2077", "direction": "bullish",
    "structure_tag": "CHoCH", "detection_time": "2026-06-11 19:00:00+00:00",
    "fill_time": None, "exit_time": None, "outcome": "REGIME_BLOCKED",
    "cancel_reason": "regime_blocked", "missed_reason": "regime_blocked",
    "entry": 1.15092, "stop": 1.15022, "rr_multiple": 2.0,
    "regime_block_reason": "state_not_allowed", "market_state": "Bear/Chop",
}
STATE_BLOCKED = {  # L_2098
    "trade_id": "L_2098", "ob_id": "2098", "direction": "bullish",
    "structure_tag": "CHoCH", "detection_time": "2026-07-23 02:30:00+00:00",
    "fill_time": None, "exit_time": None, "outcome": "STATE_BLOCKED",
    "cancel_reason": "state_target_block", "missed_reason": "state_target_block",
    "entry": 1.14076, "stop": 1.14044, "market_state": "Bear/Chop",
}
NEWS_CANCELLED = {  # S_1890
    "trade_id": "S_1890", "ob_id": "1890", "direction": "bearish",
    "structure_tag": "BOS", "detection_time": "2025-03-21 15:45:00+00:00",
    "fill_time": None, "exit_time": None, "outcome": "NEWS_TOUCH_CANCEL",
    "cancel_reason": "news_touch_cancel", "missed_reason": "news_touch_cancel",
    "entry": 1.08493, "stop": 1.08621,
}
CLOSED_LOSS = {  # L_2106 — the state-column incident, now resolved
    "trade_id": "L_2106", "ob_id": "2106", "direction": "bullish",
    "structure_tag": "BOS", "detection_time": "2026-08-07 09:00:00+00:00",
    "fill_time": "2026-08-12 18:11:00+00:00",
    "exit_time": "2026-08-13 06:30:00+00:00", "outcome": "LOSS",
    "cancel_reason": None, "entry": 1.15234, "stop": 1.15166, "tp": 1.15319,
    "rr_multiple": 1.25, "market_state": "Bear/Chop", "state_confirmed": True,
    "portfolio_cohort_key": "EURUSD|outside|bos_long", "net_r": -1.088,
}
CLOSED_S2108 = {  # S_2108 — the incident that produced the divergence rails
    "trade_id": "S_2108", "ob_id": "2108", "direction": "bearish",
    "structure_tag": "CHoCH", "detection_time": "2026-08-12 16:30:00+00:00",
    "fill_time": "2026-08-14 07:52:00+00:00",
    "exit_time": "2026-08-14 08:39:00+00:00", "outcome": "LOSS",
    "cancel_reason": None, "entry": 1.15493, "stop": 1.15567,
    "rr_multiple": 2.75, "market_state": "Bear/Chop",
}
RESTING = {  # L_2109 — still carried by the engine at the frontier
    "trade_id": "L_2109", "ob_id": "2109", "direction": "bullish",
    "structure_tag": "CHoCH", "detection_time": "2026-08-14 07:15:00+00:00",
    "fill_time": None, "exit_time": None, "outcome": "UNFILLED",
    "cancel_reason": "never_triggered", "missed_reason": "never_filled",
    "entry": 1.15292, "stop": 1.15232, "rr_multiple": 2.0,
}
#: The armed variant of the frontier flush. Synthetic only because the live
#: frame currently holds none — the engine writes this reason at
#: `execution.py:3296` when `triggered_edge_armed` is set.
ARMED = dict(RESTING, trade_id="L_ARMED",
             cancel_reason="never_filled_after_trigger",
             trigger_time="2026-08-14 08:00:00+00:00",
             armed_at="2026-08-14 08:00:00+00:00")

TERMINAL_ROWS = [INVALIDATED, REGIME_BLOCKED, STATE_BLOCKED, NEWS_CANCELLED,
                 CLOSED_LOSS, CLOSED_S2108]
ACTIVE_ROWS = [RESTING, ARMED]


# ── the classifier ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("row,expected", [
    (INVALIDATED, "INVALIDATED"), (REGIME_BLOCKED, "BLOCKED"),
    (STATE_BLOCKED, "BLOCKED"), (NEWS_CANCELLED, "CANCELLED"),
    (CLOSED_LOSS, "CLOSED"), (CLOSED_S2108, "CLOSED"),
    (RESTING, "RESTING"), (ARMED, "ARMED"),
], ids=lambda v: v if isinstance(v, str) else v.get("trade_id", "?"))
def test_lifecycle_classification(row, expected):
    assert D.lifecycle(row) == expected


def test_lifecycle_needs_more_than_one_column():
    """`outcome=UNFILLED` alone cannot say resting from armed — only the
    frontier-flush `cancel_reason` distinguishes them."""
    assert D.lifecycle(dict(RESTING, cancel_reason="never_triggered")) == "RESTING"
    assert D.lifecycle(
        dict(RESTING, cancel_reason="never_filled_after_trigger")) == "ARMED"


def test_an_unrecognised_outcome_is_unknown_not_resting():
    """A new engine outcome must surface, not be absorbed into something
    reassuring."""
    assert D.lifecycle(dict(RESTING, outcome="SOMETHING_NEW",
                            cancel_reason=None)) == "UNKNOWN"
    assert D.lifecycle(dict(RESTING, outcome="UNFILLED",
                            cancel_reason="a_reason_we_do_not_know")) == "UNKNOWN"


def test_a_win_without_an_exit_is_still_open():
    assert D.lifecycle(dict(CLOSED_LOSS, exit_time=None)) == "POSITION_OPEN"


# ── §2/§4: terminal rows can never publish as live ───────────────────────────

@pytest.mark.parametrize("row", TERMINAL_ROWS,
                         ids=lambda r: r["trade_id"])
def test_terminal_rows_are_never_pending_and_never_eligible(row):
    assert D.is_terminal(row) is True
    assert D._action(row) != "PENDING"
    assert D._eligible(row) is False


@pytest.mark.parametrize("row", ACTIVE_ROWS, ids=lambda r: r["trade_id"])
def test_active_rows_are_not_terminal(row):
    assert D.is_terminal(row) is False


def test_policy_block_and_lifecycle_invalidation_stay_distinct():
    """§4 — do not collapse everything into REFUSED. A block price broke is not
    the same event as a matrix that declined it."""
    assert D._action(STATE_BLOCKED) == "REFUSED"
    assert D._action(REGIME_BLOCKED) == "REFUSED"
    assert D._action(INVALIDATED) == "INVALIDATED"
    assert D._action(NEWS_CANCELLED) == "CANCELLED"
    assert D._action(CLOSED_LOSS) == "CLOSED"


def test_every_terminal_row_carries_a_reason_except_a_closed_trade():
    for row in (INVALIDATED, REGIME_BLOCKED, STATE_BLOCKED, NEWS_CANCELLED):
        assert D._refusal_reason(row), f"{row['trade_id']} has no reason"
    # A closed trade ended by trading, not by refusal.
    assert D._refusal_reason(CLOSED_LOSS) is None


# ── §5: eligibility is tri-state and never optimistic ────────────────────────

def test_a_resting_setup_is_unknown_not_eligible():
    """Candidate-specific rails (arm, divergence, staleness, duplicate) are
    evaluated by the executor at OPEN time, not by this projection. `true`
    would be a claim the node cannot support."""
    assert D._eligible(RESTING) is None
    assert D._eligible(ARMED) is None


def test_an_open_position_cannot_open_another_one():
    """`eligible` asks whether the setup can still OPEN. A row already holding
    a position answers no — that is known, not unknown, so it is False and not
    None even though the lifecycle is not terminal."""
    open_row = dict(CLOSED_LOSS, exit_time=None)
    assert D.lifecycle(open_row) == "POSITION_OPEN"
    assert D.is_terminal(open_row) is False
    assert D._eligible(open_row) is False


def test_eligible_is_none_exactly_when_the_setup_is_still_a_live_candidate():
    """The invariant a live-setups view will read: None ⟺ RESTING or ARMED."""
    rows = TERMINAL_ROWS + ACTIVE_ROWS + [dict(CLOSED_LOSS, exit_time=None)]
    for row in rows:
        assert (D._eligible(row) is None) == (
            D.lifecycle(row) in ("RESTING", "ARMED")), row["trade_id"]


def test_no_row_in_any_state_reports_eligible_true():
    for row in TERMINAL_ROWS + ACTIVE_ROWS:
        assert D._eligible(row) is not True


# ── the regression is non-vacuous ────────────────────────────────────────────

def _v1_action(row):
    """v1's rule, reproduced verbatim from the pre-fix source."""
    if str(row.get("outcome", "") or "") in D.REFUSAL_OUTCOMES:
        return "REFUSED"
    return "FILLED" if str(row.get("fill_time", "") or "") else "PENDING"


@pytest.mark.parametrize("row", [INVALIDATED, REGIME_BLOCKED, NEWS_CANCELLED],
                         ids=lambda r: r["trade_id"])
def test_the_old_projection_would_have_failed_these(row):
    """Without this the module could pass by describing the new arrangement
    rather than detecting the old fault."""
    assert _v1_action(row) == "PENDING", "v1 no longer reproduces the defect"
    assert _v1_action(row) != D._action(row)
    assert (_v1_action(row) != "REFUSED") is True   # v1's `eligible` was this


def test_the_schema_version_moved_with_the_meaning():
    """`action` and `eligible` changed meaning; a receiver must be able to tell."""
    assert D.SCHEMA_VERSION == "ct.node-decisions.v2"


# ── the published record ─────────────────────────────────────────────────────

def _frame(rows):
    pd = pytest.importorskip("pandas")
    return pd.DataFrame(rows)


def test_build_decision_records_publishes_lifecycle_and_terminal():
    out = D.build_decision_records(_frame(TERMINAL_ROWS + ACTIVE_ROWS))
    assert out["schema_version"] == "ct.node-decisions.v2"
    by = {r["trade_id"]: r for r in out["decisions"]}
    assert by["L_2107"]["lifecycle"] == "INVALIDATED"
    assert by["L_2107"]["terminal"] is True
    assert by["L_2107"]["action"] == "INVALIDATED"
    assert by["L_2107"]["eligible"] is False
    assert by["L_2107"]["refusal_reason"] == "invalidated_before_edge_entry"
    assert by["L_2109"]["lifecycle"] == "RESTING"
    assert by["L_2109"]["terminal"] is False
    assert by["L_2109"]["eligible"] is None


def test_no_published_record_is_pending_and_eligible():
    """The exact shape the audit found on the live node."""
    out = D.build_decision_records(_frame(TERMINAL_ROWS + ACTIVE_ROWS))
    bad = [r for r in out["decisions"]
           if r["action"] == "PENDING" and r["eligible"] is True]
    assert not bad, f"still publishing pending+eligible: {bad}"


def test_truncation_still_reports_the_real_total():
    out = D.build_decision_records(_frame(TERMINAL_ROWS + ACTIVE_ROWS), limit=2)
    assert out["count"] == 2
    assert out["total_candidates"] == len(TERMINAL_ROWS + ACTIVE_ROWS)
    assert out["truncated"] is True
