"""M-LIVE-STATE-2 Phase 3 — reproduce the index-rebasing failure, then fix it.

These tests are written to FAIL against the naive representation first. The whole
point is that carrying a positional index across a rolling window is silently
wrong, so a test that only exercised the fix would prove nothing.

The arithmetic under test is taken verbatim from strategy_core/execution.py:

    2620  candle_index >= item["trigger_candle_index"] + item["trigger_delay_candles"]
    2568  candle_index >= active_trade["_be_stop_active_from"]
    3125  candle_index > item.get("tapped_candle_index", -1) + 1
    1369  bars_to_fill = candle_index - item["created_candle_index"]

Each is a comparison between a STORED index and the LIVE one, so each breaks by
exactly the window shift.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.live_state import (ACTIVE_TRADE_INDEX_FIELDS,  # noqa: E402
                            PENDING_INDEX_FIELDS, FrameOrigin, IndexRebaseError,
                            LiveState, active_trade_to_local, pending_to_absolute,
                            pending_to_local)

# One M15 boundary is 15 M1 candles: the exact shift a rolling window applies.
SHIFT = 15


# ── the engine's own predicates, lifted verbatim ────────────────────────────
def triggered_edge_may_fill(candle_index: int, item: dict) -> bool:
    return candle_index >= item["trigger_candle_index"] + item["trigger_delay_candles"]


def be_stop_active(candle_index: int, trade: dict) -> bool:
    return candle_index >= trade.get("_be_stop_active_from", 0)


def retap_allowed(candle_index: int, item: dict) -> bool:
    return candle_index > item.get("tapped_candle_index", -1) + 1


def bars_to_fill(candle_index: int, item: dict) -> int:
    return candle_index - item["created_candle_index"]


# ── Phase 3: reproduce the failure ──────────────────────────────────────────
def test_carrying_a_raw_index_across_a_window_shift_changes_the_fill_decision():
    """THE failure. A 3-candle delay evaluated in a frame shifted by 15."""
    # Frame A: trigger at local 100, delay 3 -> may fill from local 103.
    item = {"trigger_candle_index": 100, "trigger_delay_candles": 3}
    assert not triggered_edge_may_fill(102, item)
    assert triggered_edge_may_fill(103, item)

    # Frame B is the same instant one boundary later: every candle's local index
    # is 15 lower, so the SAME instant that was local 102 is now local 87.
    assert not triggered_edge_may_fill(102 - SHIFT, item), (
        "sanity: without rebasing the delay simply looks unexpired")
    # ...and the instant that legitimately should NOT fill now does, because the
    # stored 100 is still expressed in frame A's origin.
    assert triggered_edge_may_fill(103, item)          # frame A: correct
    # In frame B the correct trigger index is 85, so 88 should fill and 87 not:
    correct_b = {"trigger_candle_index": 100 - SHIFT, "trigger_delay_candles": 3}
    assert triggered_edge_may_fill(88, correct_b)
    assert not triggered_edge_may_fill(87, correct_b)
    # The naive carried item disagrees with the correct one at every index in
    # between -- 15 candles of wrong answers.
    disagreements = [i for i in range(80, 120)
                     if triggered_edge_may_fill(i, item) != triggered_edge_may_fill(i, correct_b)]
    assert len(disagreements) == SHIFT, (
        f"expected exactly {SHIFT} candles of divergence, got {len(disagreements)}")


def test_rebasing_restores_the_original_decision_exactly():
    """The fix: same absolute instant, same answer, in either frame."""
    frame_a = FrameOrigin(origin_ordinal=1_000_000, length=500)
    frame_b = FrameOrigin(origin_ordinal=1_000_000 + SHIFT, length=500)

    local_a = {"trigger_candle_index": 100, "trigger_delay_candles": 3,
               "created_candle_index": 90}
    absolute = pending_to_absolute(local_a, frame_a)
    assert absolute["trigger_candle_index"] == 1_000_100
    local_b = pending_to_local(absolute, frame_b)
    assert local_b["trigger_candle_index"] == 100 - SHIFT

    # For every absolute instant both frames can express, the decision matches.
    for absolute_i in range(1_000_100, 1_000_140):
        a = triggered_edge_may_fill(frame_a.to_local(absolute_i), local_a)
        b = triggered_edge_may_fill(frame_b.to_local(absolute_i), local_b)
        assert a == b, f"divergence at absolute {absolute_i}"


def test_break_even_activation_survives_a_shift():
    frame_a = FrameOrigin(1_000_000, 500)
    frame_b = FrameOrigin(1_000_000 + SHIFT, 500)
    trade_a = {"_be_stop_active_from": 200, "fill_candle_index": 195}
    trade_b = active_trade_to_local(
        {k: (v + 1_000_000 if k in ACTIVE_TRADE_INDEX_FIELDS else v)
         for k, v in trade_a.items()}, frame_b)
    for absolute_i in range(1_000_190, 1_000_230):
        assert (be_stop_active(frame_a.to_local(absolute_i), trade_a)
                == be_stop_active(frame_b.to_local(absolute_i), trade_b))


def test_exact_threshold_boundary_is_preserved():
    """Exactly-at-threshold is where an off-by-one would hide."""
    frame_a, frame_b = FrameOrigin(1_000_000, 500), FrameOrigin(1_000_000 + SHIFT, 500)
    item_a = {"trigger_candle_index": 100, "trigger_delay_candles": 3}
    item_b = pending_to_local(pending_to_absolute(item_a, frame_a), frame_b)
    threshold_abs = 1_000_103
    assert triggered_edge_may_fill(frame_a.to_local(threshold_abs), item_a) is True
    assert triggered_edge_may_fill(frame_b.to_local(threshold_abs), item_b) is True
    assert triggered_edge_may_fill(frame_a.to_local(threshold_abs - 1), item_a) is False
    assert triggered_edge_may_fill(frame_b.to_local(threshold_abs - 1), item_b) is False


def test_bars_to_fill_is_shift_invariant():
    frame_a, frame_b = FrameOrigin(1_000_000, 500), FrameOrigin(1_000_000 + SHIFT, 500)
    item_a = {"created_candle_index": 90}
    item_b = pending_to_local(pending_to_absolute(item_a, frame_a), frame_b)
    for absolute_i in range(1_000_095, 1_000_130):
        assert (bars_to_fill(frame_a.to_local(absolute_i), item_a)
                == bars_to_fill(frame_b.to_local(absolute_i), item_b))


# ── sentinels must not be rebased ───────────────────────────────────────────
@pytest.mark.parametrize("sentinel", [-1, None, ""])
def test_sentinels_are_never_shifted(sentinel):
    """Rebasing 'unset' into a real position would silently arm or disarm logic."""
    frame = FrameOrigin(1_000_000, 500)
    item = {"tapped_candle_index": sentinel, "trigger_candle_index": 100,
            "trigger_delay_candles": 3}
    out = pending_to_absolute(item, frame)
    assert out["tapped_candle_index"] == sentinel
    assert out["trigger_candle_index"] == 1_000_100
    back = pending_to_local(out, frame)
    assert back["tapped_candle_index"] == sentinel


def test_unset_retap_sentinel_keeps_its_meaning_after_a_round_trip():
    frame_a, frame_b = FrameOrigin(1_000_000, 500), FrameOrigin(1_000_000 + SHIFT, 500)
    item_a = {"tapped_candle_index": -1}
    item_b = pending_to_local(pending_to_absolute(item_a, frame_a), frame_b)
    # -1 means "never tapped", so a re-tap is allowed at every index in both.
    for absolute_i in range(1_000_100, 1_000_120):
        assert (retap_allowed(frame_a.to_local(absolute_i), item_a)
                == retap_allowed(frame_b.to_local(absolute_i), item_b) is True)


# ── failing closed ──────────────────────────────────────────────────────────
def test_pre_window_ordinal_maps_to_a_negative_local_index():
    """CORRECTED after running against the real engine.

    This originally asserted that a pre-window ordinal RAISES. That was wrong,
    and the real engine proved it: a two-year window carrying 34 pending plans
    hit an ordinal from ~2018. Rejecting it would force a full-oracle rebuild
    almost every cycle and defeat Model B for the wrong reason.

    Negative is correct because `candle_records` is never indexed by a carried
    value -- every reader is a comparison or a difference, and all are exact
    under a negative operand. Clamping remains forbidden; only that would
    fabricate an expired delay.
    """
    frame = FrameOrigin(origin_ordinal=1_000_000, length=500)
    assert frame.to_local(999_990) == -10
    assert frame.to_local(1_000_000) == 0


def test_bars_to_fill_is_exact_for_a_plan_older_than_the_window():
    """The arithmetic that makes negative locals safe, on real magnitudes."""
    created_abs, fill_abs = 1_168_182, 4_314_000       # ~2018 plan, 2026 fill
    oracle_frame = FrameOrigin(origin_ordinal=0, length=4_314_284)
    window_frame = FrameOrigin(origin_ordinal=3_569_511, length=744_773)

    item_o = {"created_candle_index": oracle_frame.to_local(created_abs)}
    item_w = {"created_candle_index": window_frame.to_local(created_abs)}
    assert item_w["created_candle_index"] < 0                  # genuinely negative
    assert (bars_to_fill(oracle_frame.to_local(fill_abs), item_o)
            == bars_to_fill(window_frame.to_local(fill_abs), item_w) == 3_145_818)


def test_ordinal_beyond_the_frame_end_still_fails_closed():
    """A FUTURE candle is impossible, not merely historical."""
    frame = FrameOrigin(origin_ordinal=1_000_000, length=500)
    with pytest.raises(IndexRebaseError, match="beyond the frame end"):
        frame.to_local(1_000_500)


def test_state_beyond_the_frame_end_fails_closed():
    frame = FrameOrigin(origin_ordinal=1_000_000, length=500)
    with pytest.raises(IndexRebaseError, match="beyond the frame end"):
        frame.to_local(1_000_500)


def test_live_state_round_trips_through_a_shifted_frame():
    frame_a, frame_b = FrameOrigin(1_000_000, 5_000), FrameOrigin(1_000_000 + SHIFT, 5_000)
    st = LiveState(
        pending=[{"trigger_candle_index": 100, "trigger_delay_candles": 3,
                  "created_candle_index": 90, "tapped_candle_index": -1}],
        active_trades=[{"fill_candle_index": 120, "_be_stop_active_from": 130}],
        ghost_states={"7": {"ob_id": "7"}}, ob_cursor=42, be_applied_any=True,
        last_boundary="2026-07-31 20:30:00+00:00", last_candle_ordinal=1_000_140)
    moved = st.to_absolute(frame_a).to_local(frame_b)
    assert moved.pending[0]["trigger_candle_index"] == 100 - SHIFT
    assert moved.pending[0]["tapped_candle_index"] == -1          # sentinel intact
    assert moved.active_trades[0]["_be_stop_active_from"] == 130 - SHIFT
    # non-positional state must be carried untouched
    assert moved.ob_cursor == 42
    assert moved.be_applied_any is True
    assert moved.ghost_states == {"7": {"ob_id": "7"}}
    assert moved.last_boundary == "2026-07-31 20:30:00+00:00"


def test_every_audited_index_field_is_covered_by_the_rebase_contract():
    """Guards against a new positional field being added and silently missed."""
    assert set(PENDING_INDEX_FIELDS) >= {
        "created_candle_index", "trigger_candle_index", "tapped_candle_index"}
    assert set(ACTIVE_TRADE_INDEX_FIELDS) >= {"fill_candle_index", "_be_stop_active_from"}


# ── Phase 1 recursive audit: nested records and two ordinal spaces ──────────
from live.live_state import (FIELD_SPACES, GHOST_INDEX_FIELDS,  # noqa: E402
                            NON_POSITIONAL_COUNTS, OB_M15_INDEX_FIELDS, SPACE_M1,
                            SPACE_M15_DETECTION, TRADE_ROW_INDEX_FIELDS,
                            active_trade_to_absolute, ghost_state_to_absolute,
                            ghost_state_to_local, trade_row_to_local,
                            unregistered_positional_fields)


def test_nested_trade_row_inside_an_active_trade_is_converted():
    """The top-level audit missed this: active_trade["row"] holds 8 positions."""
    frame_a, frame_b = FrameOrigin(1_000_000, 5_000), FrameOrigin(1_000_000 + SHIFT, 5_000)
    trade = {"_be_stop_active_from": 200,
             "row": {"fill_candle_index": 195, "exit_candle_index": 260,
                     "be_arm_candle_index": 210, "bars_to_exit": 65},
             "plan": {"entry_model": "triggered_edge"}}
    absolute = active_trade_to_absolute(trade, frame_a)
    assert absolute["row"]["fill_candle_index"] == 1_000_195
    local_b = active_trade_to_local(absolute, frame_b)
    assert local_b["row"]["fill_candle_index"] == 195 - SHIFT
    assert local_b["row"]["exit_candle_index"] == 260 - SHIFT
    assert local_b["_be_stop_active_from"] == 200 - SHIFT
    # a DIFFERENCE is invariant and must be left alone
    assert local_b["row"]["bars_to_exit"] == 65
    # the caller's dict must not be mutated
    assert trade["row"]["fill_candle_index"] == 195


def test_ghost_state_positions_are_converted():
    frame_a, frame_b = FrameOrigin(1_000_000, 5_000), FrameOrigin(1_000_000 + SHIFT, 5_000)
    g = {"ob_id": "7", "fill_candle_index": 300, "trigger_candle_index": 290,
         "_delay_remaining": 2, "te_delay": 3}
    moved = ghost_state_to_local(ghost_state_to_absolute(g, frame_a), frame_b)
    assert moved["fill_candle_index"] == 300 - SHIFT
    assert moved["trigger_candle_index"] == 290 - SHIFT
    assert moved["_delay_remaining"] == 2      # counter, not a position
    assert moved["te_delay"] == 3
    assert moved["ob_id"] == "7"


def test_counts_are_never_rebased():
    """`fill_delay_candles_val` and `te_delay` are counts despite their names."""
    for name in NON_POSITIONAL_COUNTS:
        assert name not in FIELD_SPACES, f"{name} is a count, not a position"


def test_m15_detection_indices_live_in_a_different_space():
    """origin/detection/pivot_index index the M15 frame, not the M1 frame."""
    for f in OB_M15_INDEX_FIELDS:
        assert FIELD_SPACES[f] == SPACE_M15_DETECTION
    for f in TRADE_ROW_INDEX_FIELDS + GHOST_INDEX_FIELDS:
        assert FIELD_SPACES[f] == SPACE_M1
    # and they are NOT in the M1 conversion sets
    assert not set(OB_M15_INDEX_FIELDS) & set(TRADE_ROW_INDEX_FIELDS)


def test_an_unregistered_positional_field_is_rejected_not_ignored():
    """A new engine field must not ride across a frame shift unconverted."""
    row = {"fill_candle_index": 10, "some_new_candle_index": 42}
    unknown = unregistered_positional_fields(row, TRADE_ROW_INDEX_FIELDS)
    assert unknown == ["some_new_candle_index"]
    trade = {"row": row}
    with pytest.raises(IndexRebaseError, match="unregistered positional field"):
        active_trade_to_absolute(trade, FrameOrigin(1_000_000, 5_000))


def test_known_fields_do_not_trip_the_unregistered_check():
    row = {f: 10 for f in TRADE_ROW_INDEX_FIELDS}
    row.update({"bars_to_exit": 5, "outcome": "WIN", "net_r": 1.2})
    assert unregistered_positional_fields(row, TRADE_ROW_INDEX_FIELDS) == []
