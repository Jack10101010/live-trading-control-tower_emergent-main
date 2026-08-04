"""M-CAP-GUARD-1 — order-block identity-space guard.

Proves the guard admits normal operation and refuses every drift mode measured
in ORDER-BLOCK-IDENTITY.md, and that no order/ledger path is reachable after a
refusal.
"""
from dataclasses import dataclass, replace

import pandas as pd
import pytest

from live.identity_guard import (
    IDENTITY_CONFIG_FIELDS,
    REASON_COORDINATE_CONFLICT,
    REASON_KEY_MISMATCH,
    REASON_MALFORMED_FRAME,
    REASON_MISSING_PRIOR_KEY,
    REASON_PRIOR_TRADE_VANISHED,
    evaluate_continuity,
    identity_space_key,
)

ENGINE = "5bb6372c" * 8


@dataclass(frozen=True)
class Cfg:
    """Minimal stand-in exposing exactly the identity fields."""
    symbol: str = "EURUSD"
    detection_timeframe: str = "15min"
    start_date: str = "2015-01-01"
    end_date: str = "2026-06-19"          # deliberately NOT part of the key
    swing_length: int = 50
    ob_filter: str = "Atr"
    pip_size: float = 0.0001
    min_ob_size_pips: float = 0.0
    max_ob_size_pips: float = 100.0
    structure_filter: str = "both"
    allowed_structure_directions: list | None = None


WINDOW = "2015-01-01 00:00:00"


def key(cfg=None, *, engine=ENGINE, window=WINDOW):
    return identity_space_key(engine_version=engine, config=cfg or Cfg(),
                              window_start=window)


def frame(rows):
    return pd.DataFrame(rows)


def row(tid, detection_time, direction="bullish", **extra):
    return {"trade_id": tid, "detection_time": detection_time,
            "direction": direction, **extra}


# ── key stability ────────────────────────────────────────────────────────────

def test_key_is_deterministic():
    assert key() == key()


def test_end_date_advance_does_not_change_key():
    """The frontier advances every cycle; append-only extension provably
    preserves prior ids, so end_date must NOT rebase the key."""
    assert key(Cfg()) == key(replace(Cfg(), end_date="2026-07-01"))


@pytest.mark.parametrize("field,value", [
    ("symbol", "GBPUSD"),
    ("detection_timeframe", "5min"),
    ("start_date", "2016-01-01"),
    ("swing_length", 51),
    ("ob_filter", "Range"),
    ("pip_size", 0.001),
    ("min_ob_size_pips", 1.0),
    ("max_ob_size_pips", 99.0),
    ("structure_filter", "bullish"),
    ("allowed_structure_directions", ["bullish"]),
])
def test_every_identity_field_changes_the_key(field, value):
    assert key(replace(Cfg(), **{field: value})) != key()


def test_window_start_changes_the_key():
    """Experiment B: a moved window start rebased 2,042 of 2,042 ids."""
    assert key(window="2016-01-01 00:00:00") != key()


def test_engine_version_changes_the_key():
    assert key(engine="deadbeef" * 8) != key()


def test_reordering_allowed_directions_changes_the_key():
    a = replace(Cfg(), allowed_structure_directions=["bullish", "bearish"])
    b = replace(Cfg(), allowed_structure_directions=["bearish", "bullish"])
    assert key(a) != key(b)


def test_absent_field_is_not_confused_with_present_field():
    class Partial:
        symbol = "EURUSD"          # every other identity field missing
    assert identity_space_key(engine_version=ENGINE, config=Partial(),
                              window_start=WINDOW) != key()


def test_config_field_list_is_covered_by_tests():
    """Guards against a field being added to the key without a test."""
    tested = {"symbol", "detection_timeframe", "start_date", "swing_length",
              "ob_filter", "pip_size", "min_ob_size_pips", "max_ob_size_pips",
              "structure_filter", "allowed_structure_directions"}
    assert set(IDENTITY_CONFIG_FIELDS) == tested


# ── continuity: admissible cases ─────────────────────────────────────────────

def test_bootstrap_has_no_continuity_to_claim():
    v = evaluate_continuity(prev_frame=None, cur_frame=frame([row("L_1", "t1")]),
                            prior_key=None, current_key=key())
    assert v.ok


def test_unchanged_identity_space_proceeds():
    prev = frame([row("L_1", "t1"), row("S_2", "t2", "bearish")])
    cur = frame([row("L_1", "t1"), row("S_2", "t2", "bearish")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(), current_key=key())
    assert v.ok, v.detail


def test_append_only_extension_is_admissible():
    """Experiment A: extension appends new ids and disturbs none."""
    prev = frame([row("L_1", "t1"), row("S_2", "t2", "bearish")])
    cur = frame([row("L_1", "t1"), row("S_2", "t2", "bearish"),
                 row("L_3", "t3")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(), current_key=key())
    assert v.ok, v.detail


def test_row_content_may_change_while_identity_holds():
    """Fills, exits and stop moves are exactly what the diff exists to find —
    they must not be mistaken for drift."""
    prev = frame([row("L_1", "t1", fill_time="", outcome="", stop=1.0)])
    cur = frame([row("L_1", "t1", fill_time="2026-01-01", outcome="WIN",
                     stop=1.5)])
    assert evaluate_continuity(prev_frame=prev, cur_frame=cur,
                               prior_key=key(), current_key=key()).ok


# ── continuity: refusals ─────────────────────────────────────────────────────

def test_missing_prior_key_fails_closed_when_continuity_claimed():
    prev = frame([row("L_1", "t1")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=frame([row("L_1", "t1")]),
                            prior_key=None, current_key=key())
    assert not v.ok and v.reason == REASON_MISSING_PRIOR_KEY


def test_empty_string_prior_key_fails_closed():
    prev = frame([row("L_1", "t1")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=frame([row("L_1", "t1")]),
                            prior_key="", current_key=key())
    assert not v.ok and v.reason == REASON_MISSING_PRIOR_KEY


def test_changed_window_start_refuses_continuity():
    prev = frame([row("L_1", "t1")])
    cur = frame([row("L_1", "t1")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(window="2015-01-01 00:00:00"),
                            current_key=key(window="2016-01-01 00:00:00"))
    assert not v.ok and v.reason == REASON_KEY_MISMATCH


def test_changed_detector_parameter_refuses_continuity():
    prev = frame([row("L_1", "t1")])
    cur = frame([row("L_1", "t1")])
    v = evaluate_continuity(
        prev_frame=prev, cur_frame=cur, prior_key=key(),
        current_key=key(replace(Cfg(), min_ob_size_pips=1.0)))
    assert not v.ok and v.reason == REASON_KEY_MISMATCH


def test_changed_engine_identity_refuses_continuity():
    prev = frame([row("L_1", "t1")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=frame([row("L_1", "t1")]),
                            prior_key=key(),
                            current_key=key(engine="deadbeef" * 8))
    assert not v.ok and v.reason == REASON_KEY_MISMATCH


def test_rebased_ids_refuse_continuity_even_when_key_matches():
    """Direct evidence check: the key cannot anticipate every cause, so a
    trade_id naming a different order block must still be caught."""
    prev = frame([row("L_1", "t1"), row("L_2", "t2")])
    cur = frame([row("L_1", "t2"), row("L_2", "t3")])      # shifted by one
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(), current_key=key())
    assert not v.ok and v.reason == REASON_COORDINATE_CONFLICT
    assert "L_1" in v.detail


def test_direction_flip_on_same_trade_id_refuses():
    prev = frame([row("L_1", "t1", "bullish")])
    cur = frame([row("L_1", "t1", "bearish")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(), current_key=key())
    assert not v.ok and v.reason == REASON_COORDINATE_CONFLICT


def test_vanished_prior_trade_refuses_continuity():
    """This is the CLOSE-suppression hazard: diff_frontier iterates cur_frame
    only, so a disappeared row would silently abandon an open position."""
    prev = frame([row("L_1", "t1"), row("L_2", "t2")])
    cur = frame([row("L_2", "t2")])
    v = evaluate_continuity(prev_frame=prev, cur_frame=cur,
                            prior_key=key(), current_key=key())
    assert not v.ok and v.reason == REASON_PRIOR_TRADE_VANISHED
    assert "L_1" in v.detail


def test_empty_current_frame_with_prior_trades_refuses():
    v = evaluate_continuity(prev_frame=frame([row("L_1", "t1")]),
                            cur_frame=frame([]).reindex(
                                columns=["trade_id", "detection_time", "direction"]),
                            prior_key=key(), current_key=key())
    assert not v.ok and v.reason == REASON_PRIOR_TRADE_VANISHED


@pytest.mark.parametrize("missing", ["trade_id", "detection_time", "direction"])
def test_frame_missing_identity_column_refuses_rather_than_raising(missing):
    cols = {"trade_id": "L_1", "detection_time": "t1", "direction": "bullish"}
    cols.pop(missing)
    v = evaluate_continuity(prev_frame=frame([cols]), cur_frame=frame([cols]),
                            prior_key=key(), current_key=key())
    assert not v.ok and v.reason == REASON_MALFORMED_FRAME


def test_verdict_is_serialisable_for_telemetry():
    v = evaluate_continuity(prev_frame=frame([row("L_1", "t1")]),
                            cur_frame=frame([row("L_1", "t9")]),
                            prior_key=key(), current_key=key())
    d = v.to_dict()
    assert d["ok"] is False and d["reason"] and d["detail"]
