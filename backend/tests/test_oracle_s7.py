"""Stage S7 — the pre-fill path, and the fifth parity dimension it forced.

The load-bearing test is `test_reference_matches_production_on_a_slice`: the
replay is only a REFERENCE if production's own `simulate_trades` agrees with it
about where every order block ended up before any policy ran. Everything else
here pins a branch that a "reasonable" implementation gets wrong — and two of
them were written because a reasonable implementation DID get them wrong:

  * the touched-during-blackout cancel reuses the fill gate's arm/delay clause,
    so an UNARMED triggered-edge order can never be news-cancelled;
  * an armed order's touch test is CONTAINMENT (`low <= entry <= high`), not the
    one-sided test used before arming.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import parity_status as ps  # noqa: E402
from tools.oracle import replay_prefill as rp  # noqa: E402
from tools.oracle.engine_access import load_engine, resolve_config  # noqa: E402
from tools.oracle.generate_pine import STAGE_DEPENDS_ON, STAGE_STATUS  # noqa: E402


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def cfg(engine):
    return resolve_config(engine)[0]


@pytest.fixture(scope="module")
def prefill(cfg):
    return rp.resolve_prefill_config(cfg)


def _ob(ob_id, direction, top, bottom, detection_time, tag="BOS"):
    return {"ob_id": ob_id, "direction": direction, "top": top,
            "bottom": bottom, "detection_time": detection_time,
            "structure_tag": tag}


def _c(t, o, h, l, c):     # noqa: E741 — `l` mirrors the production column name
    return {"time": t, "open": o, "high": h, "low": l, "close": c}


# ══ the production boundary ══════════════════════════════════════════════════

def test_execution_basis_is_the_raw_candle_file_not_the_detection_resample(cfg):
    """The single fact S7 turns on. `simulate_trades` is handed `candles` — the
    raw candle_file — while only detection uses the 15-minute resample."""
    assert str(cfg.candle_file).endswith(".csv")
    assert "1m" in str(cfg.candle_file), (
        "the execution basis is the candle file; if it stops being 1-minute, "
        "every S7 delay and touch statement in the docs is wrong")
    assert cfg.detection_timeframe == "15min"
    assert cfg.execution_timeframe == "1min"


def test_delay_is_counted_in_execution_candles(prefill):
    """`trigger_delay_candles = 3` is three EXECUTION candles. On the deployed
    1-minute basis that is three minutes — a fifth of one 15-minute chart bar,
    which is why the chart cannot resolve it."""
    assert prefill["delay_candles"] == 3
    assert rp.delay_satisfied(102, 100, 3) is False
    assert rp.delay_satisfied(103, 100, 3) is True
    # production's escape hatch: no trigger index means the clause passes
    assert rp.delay_satisfied(0, None, 3) is True


def test_can_fill_is_constant_under_the_deployed_execution_mode(prefill):
    assert prefill["execution_mode"] == "allow_multi_position"
    assert prefill["can_fill_is_constant"] is True


def test_resolve_refuses_an_execution_mode_that_reads_account_state(cfg):
    """The fail-closed guard the brief asked for: if the mode changes to one
    where `active_trades` decides `can_fill`, S7 stops being reproducible from a
    chart and must not quietly continue."""
    import dataclasses
    for mode in ("single_position", "one_per_direction"):
        bad = dataclasses.replace(cfg, execution_modes=[mode])
        with pytest.raises(rp.PrefillError, match="allow_multi_position"):
            rp.resolve_prefill_config(bad)


@pytest.mark.parametrize("name", sorted(rp.INERT_BRANCHES))
def test_inert_branches_are_recorded_with_a_source_and_a_flag(name):
    meta = rp.INERT_BRANCHES[name]
    assert meta["source"].startswith("strategy_core/execution.py:")
    assert meta["flag"] and meta["reason"] and meta["stage"] in ("S7", "S8")


def test_inert_branches_are_actually_inert(cfg):
    """A claim, checked. If any of these flags flips, the parity contract's
    'implemented in production, not mirrored in Pine' entry becomes a lie."""
    assert cfg.session_filter_enabled is False
    assert cfg.regime_gate_enabled is False
    assert cfg.reverse_touch_cancel_enabled is False


def test_resolve_refuses_when_an_inert_branch_becomes_reachable(cfg):
    import dataclasses
    bad = dataclasses.replace(cfg, session_filter_enabled=True)
    with pytest.raises(rp.PrefillError, match="no longer inert"):
        rp.resolve_prefill_config(bad)


# ══ the arithmetic ═══════════════════════════════════════════════════════════

def test_entry_is_the_edge_when_the_level_pct_is_zero():
    assert rp.planned_entry(1.1, 1.0, "bullish", 0.0) == pytest.approx(1.1)
    assert rp.planned_entry(1.1, 1.0, "bearish", 0.0) == pytest.approx(1.0)
    # …and moves INTO the block as the pct grows, on both sides
    assert rp.planned_entry(1.1, 1.0, "bullish", 50.0) == pytest.approx(1.05)
    assert rp.planned_entry(1.1, 1.0, "bearish", 50.0) == pytest.approx(1.05)


def test_a_zero_depth_block_can_never_arm():
    """Production guards `depth <= 0` BEFORE the comparison. Without the guard a
    zero-depth block arms on any bar at all, because 0 >= 0."""
    assert rp.trigger_touched(1.1, 1.1, "bullish", 1.2, 1.0, 25.0) is False


def test_the_arm_threshold_is_inclusive():
    """`>=`, not `>`. A penetration of exactly the threshold ARMS.

    The magnitudes are deliberately unrealistic (depth 1.0, not 100 pips) so
    every intermediate is EXACTLY representable in float64. At real FX
    magnitudes the boundary is not reachable at all: `top - (top - need)` is
    2.5e-3 - 1.1e-16 rather than 2.5e-3, so a test written with literal prices
    measures float64 rather than the comparison operator. Same result as the S5
    width gate, where an exact 100.0-pip block turned out to be unrepresentable.
    """
    import math
    top, bottom, pct = 2.0, 1.0, 25.0
    need = (top - bottom) * (pct / 100.0)              # 0.25, exact

    at_bull = top - need                                # 1.75, exact
    assert top - at_bull == need
    assert rp.trigger_touched(top, bottom, "bullish", top, at_bull, pct) is True
    assert rp.trigger_touched(top, bottom, "bullish", top,
                              math.nextafter(at_bull, top), pct) is False

    at_bear = bottom + need                             # 1.25, exact
    assert at_bear - bottom == need
    assert rp.trigger_touched(top, bottom, "bearish", at_bear, bottom, pct) is True
    assert rp.trigger_touched(top, bottom, "bearish",
                              math.nextafter(at_bear, bottom), bottom, pct) is False


def test_penetration_never_goes_negative():
    assert rp.penetration(1.1, 1.0, "bullish", 1.3, 1.2) == 0.0
    assert rp.penetration(1.1, 1.0, "bearish", 0.9, 0.8) == 0.0


def test_an_armed_touch_is_containment_not_one_sided():
    """THIS IS THE SUBTLE ONE. A bar that gapped clean past the entry satisfies
    the one-sided test and NOT the containment test, so reading the gate as
    one-sided invents fills production never had."""
    entry = 1.1000
    gapped = dict(direction="bullish", entry=entry, high=1.0950, low=1.0900)
    assert rp.gate_touch(**gapped, armed=False) is True     # low <= entry
    assert rp.gate_touch(**gapped, armed=True) is False     # not contained
    touched = dict(direction="bullish", entry=entry, high=1.1010, low=1.0990)
    assert rp.gate_touch(**touched, armed=True) is True


def test_direction_filter_matches_production_vocabulary():
    for word in ("both", "all", "", None):
        assert rp.direction_allowed("bullish", word) is True
        assert rp.direction_allowed("bearish", word) is True
    for word in ("long", "buy", "bull", "bullish"):
        assert rp.direction_allowed("bullish", word) is True
        assert rp.direction_allowed("bearish", word) is False
    # An UNRECOGNISED value allows everything — production's behaviour, and not
    # what a careful reader would guess.
    assert rp.direction_allowed("bearish", "sideways") is True


# ══ the loop ═════════════════════════════════════════════════════════════════

def _run(obs, candles, prefill, news=None, **over):
    cfg = dict(prefill, **over)
    return rp.replay(obs, candles, cfg,
                     news_match=(lambda t: news.get(t)) if news else None)


def test_a_block_is_admitted_on_its_own_detection_candle(prefill):
    """`detection_time <= candle["time"]`, so a block detected on this candle is
    tracked from this candle — it is NOT excluded from its own bar."""
    obs = [_ob(1, "bullish", 1.1, 1.09, 10)]
    candles = [_c(10, 1.1, 1.1, 1.09, 1.1)]
    setups, _ = _run(obs, candles, prefill)
    assert setups[0]["admitted_index"] == 0
    assert setups[0]["events"][0] == ("ADMITTED", 0)


def test_arm_then_delay_then_touch_in_order(prefill):
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    #        0 admit   1 arm (25% pen)  2,3 wait   4 touch entry
    candles = [
        _c(0, 1.11, 1.1200, 1.1100, 1.115),      # nothing
        _c(1, 1.11, 1.1100, 1.0975, 1.105),      # penetration 25% -> ARM
        _c(2, 1.10, 1.1050, 1.1010, 1.103),      # inside delay
        _c(3, 1.10, 1.1050, 1.1010, 1.103),      # inside delay
        _c(4, 1.10, 1.1010, 1.0990, 1.100),      # contains entry -> GATE
    ]
    setups, stats = _run(obs, candles, prefill)
    s = setups[0]
    assert s["trigger_candle_index"] == 1
    assert s["delay_satisfied_index"] == 4
    assert s["terminal"] == rp.GATE_READY
    assert s["terminal_index"] == 4
    assert [e for e, _i in s["events"]] == [
        "ADMITTED", "ARMED", "DELAY_SATISFIED", "GATE_READY"]
    assert stats["gate_ready"] == 1


def test_a_touch_inside_the_delay_window_does_not_fill(prefill):
    """The delay is the whole point of the triggered-edge model: price may sit on
    the entry for two candles and still not fill."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [
        _c(0, 1.11, 1.1100, 1.0975, 1.105),      # ARM at index 0
        _c(1, 1.10, 1.1010, 1.0990, 1.100),      # contains entry — too early
        _c(2, 1.10, 1.1010, 1.0990, 1.100),      # contains entry — too early
        _c(3, 1.10, 1.1010, 1.0990, 1.100),      # delay satisfied -> GATE
    ]
    setups, _ = _run(obs, candles, prefill)
    assert setups[0]["terminal_index"] == 3


def test_a_pre_trigger_tap_is_recorded_once_and_does_not_arm(prefill):
    """`_triggered_edge_mark_tap` fires only on a candle that did NOT arm, and
    only the first time."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    # touches the entry (top) without reaching 25% penetration
    candles = [_c(i, 1.101, 1.1010, 1.0999, 1.101) for i in range(3)]
    setups, stats = _run(obs, candles, prefill)
    s = setups[0]
    assert s["armed"] is False
    assert s["tapped_before_trigger"] is True
    assert [e for e, _i in s["events"]].count("TAPPED_BEFORE_TRIGGER") == 1
    assert s["terminal"] == rp.NEVER_TRIGGERED
    assert stats["tapped_before_trigger"] == 1


def test_never_filled_after_trigger_is_distinct_from_never_triggered(prefill):
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.11, 1.1100, 1.0975, 1.105)] + [
        _c(i, 1.12, 1.1300, 1.1200, 1.125) for i in range(1, 6)]
    setups, _ = _run(obs, candles, prefill)
    assert setups[0]["armed"] is True
    assert setups[0]["terminal"] == rp.NEVER_FILLED


def test_a_non_positive_risk_block_is_never_admitted(prefill):
    """`_planned_trade` returns None and production never appends it to
    `pending`. That is an S7 admission decision, not a plan detail."""
    obs = [_ob(1, "bullish", 1.1, 1.1, 0)]        # zero depth -> risk = buffer only
    setups, stats = _run(obs, [_c(0, 1.1, 1.1, 1.1, 1.1)], prefill,
                         stop_buffer=-1.0)        # force risk <= 0
    assert setups[0]["admitted"] is False
    assert setups[0]["suppressed_reason"] == "NON_POSITIVE_RISK"
    assert stats["admitted"] == 0


def test_direction_filter_suppresses_before_tracking(prefill):
    obs = [_ob(1, "bearish", 1.1, 1.09, 0)]
    setups, _ = _run(obs, [_c(0, 1.1, 1.1, 1.09, 1.1)], prefill,
                     trade_direction="long")
    assert setups[0]["suppressed_reason"] == "DIRECTION_NOT_ALLOWED"


def test_price_through_the_block_invalidates_the_setup(prefill):
    """`execution.py:2958`. The branch the first version of this replay missed:
    two setups were reported as news-cancelled when production had already
    invalidated them, and the self-check agreed because BOTH sides mapped the
    outcome to 'reached the gate'."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.09, 1.0950, 1.0899, 1.089)]     # low < bottom
    setups, stats = _run(obs, candles, prefill)
    assert setups[0]["terminal"] == rp.INVALIDATED
    assert stats["invalidated_before_edge_entry"] == 1


def test_invalidation_is_strict_and_measured_against_the_far_edge(prefill):
    """`<`, not `<=`, and against the block's far edge rather than the entry. A
    bar that merely REACHES the far edge leaves the setup alive."""
    assert rp.invalidated(1.1, 1.09, "bullish", 1.1, 1.08999) is True
    assert rp.invalidated(1.1, 1.09, "bullish", 1.1, 1.09) is False
    assert rp.invalidated(1.1, 1.09, "bearish", 1.10001, 1.09) is True
    assert rp.invalidated(1.1, 1.09, "bearish", 1.1, 1.09) is False


def test_a_blackout_candle_cannot_invalidate(prefill):
    """The news branch `continue`s before the invalidation check, so price can
    pass clean through a block during a blackout and the setup survives. This is
    production behaviour, and it is the reason branch ORDER is load-bearing
    rather than cosmetic."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.09, 1.0950, 1.0800, 1.085)]     # well below the bottom
    setups, _ = _run(obs, candles, prefill, news={0: {"event": "CPI"}})
    assert setups[0]["terminal"] != rp.INVALIDATED


def test_the_gate_wins_over_invalidation_on_the_same_candle(prefill):
    """Production checks the fill gate FIRST and `continue`s, so a candle that
    both touches the entry and blows through the block is a FILL, not an
    invalidation. Reversing the order would delete real trades."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [
        _c(0, 1.11, 1.1100, 1.0975, 1.105),            # ARM
        _c(1, 1.10, 1.1050, 1.1010, 1.103),
        _c(2, 1.10, 1.1050, 1.1010, 1.103),
        _c(3, 1.10, 1.1010, 1.0800, 1.085),            # contains entry AND < bottom
    ]
    setups, _ = _run(obs, candles, prefill)
    assert setups[0]["terminal"] == rp.GATE_READY


# ══ news ═════════════════════════════════════════════════════════════════════

def test_an_unarmed_order_is_never_news_cancelled(prefill):
    """The regression. The blackout cancel reuses the fill gate's clause
    `entry_model != "triggered_edge" or (armed and delay_ok)`, which is FALSE for
    an unarmed triggered-edge order however deep price goes. Reading it as
    'unarmed passes' cancelled two setups production filled."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.10, 1.1010, 1.0990, 1.100)]      # contains entry
    setups, _ = _run(obs, candles, prefill, news={0: {"event": "CPI"}})
    s = setups[0]
    assert s["terminal"] != rp.NEWS_TOUCH_CANCEL
    assert s["news_pause_count"] == 1


def test_an_armed_order_touched_inside_a_blackout_is_cancelled(prefill):
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [
        _c(0, 1.11, 1.1100, 1.0975, 1.105),      # ARM
        _c(1, 1.10, 1.1050, 1.1010, 1.103),
        _c(2, 1.10, 1.1050, 1.1010, 1.103),
        _c(3, 1.10, 1.1010, 1.0990, 1.100),      # delay ok + touch, in blackout
    ]
    setups, stats = _run(obs, candles, prefill, news={3: {"event": "NFP"}})
    assert setups[0]["terminal"] == rp.NEWS_TOUCH_CANCEL
    assert stats["news_touch_cancel"] == 1


def test_a_paused_order_rearms_when_the_blackout_ends(prefill):
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.12, 1.1300, 1.1200, 1.125),
               _c(1, 1.12, 1.1300, 1.1200, 1.125)]
    setups, _ = _run(obs, candles, prefill, news={0: {"event": "CPI"}})
    names = [e for e, _i in setups[0]["events"]]
    assert "NEWS_PAUSED" in names and "NEWS_REARMED" in names


def test_no_arming_happens_during_a_blackout(prefill):
    """The pause `continue`s past the arm logic entirely — a blackout candle
    cannot arm an order, whatever its penetration."""
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    candles = [_c(0, 1.11, 1.1100, 1.0900, 1.105)]      # 100% penetration
    setups, _ = _run(obs, candles, prefill, news={0: {"event": "CPI"}})
    assert setups[0]["armed"] is False


# ══ against production ═══════════════════════════════════════════════════════

def test_reference_matches_production_on_a_slice(engine, cfg):
    """The only thing that makes this a reference. Two months, every admitted
    order block, compared on its pre-fill terminal against `simulate_trades`."""
    prepared, obs, news, seam = rp._load(engine, cfg, "2026-01-01",
                                     "2026-03-01")
    assert seam is None, "a frozen-only load must not report a feed seam"
    rep = rp.selfcheck(engine, cfg, obs, prepared.to_dict("records"), news)
    assert rep["compared"] >= 20, "slice too thin to mean anything"
    assert rep["mismatches"] == 0, rep["examples"]


# ══ the fifth dimension ══════════════════════════════════════════════════════

def test_partial_input_coverage_requires_numbers():
    rec = ps.new_stage_record(ps.IMPL_IMPLEMENTED)
    rec["input_availability"].update(status=ps.INPUT_PARTIAL,
                                     reason="NEWS_CALENDAR_UNAVAILABLE")
    with pytest.raises(ps.ParityStatusError, match="requires a measured"):
        ps.validate_stage_record("S7", rec)


def test_partial_input_coverage_must_account_for_every_bar():
    rec = ps.new_stage_record(ps.IMPL_IMPLEMENTED)
    rec["input_availability"].update(
        status=ps.INPUT_PARTIAL, reason="NEWS_CALENDAR_UNAVAILABLE",
        total_bars=100, unavailable_bars=5, scored_bars=90, coverage_pct=90.0)
    with pytest.raises(ps.ParityStatusError, match="has to be accounted for"):
        ps.validate_stage_record("S7", rec)


def test_matched_for_available_inputs_needs_a_declared_gap():
    """Otherwise the status is LOGIC_MATCHED wearing a hedge."""
    rec = ps.new_stage_record(ps.IMPL_IMPLEMENTED)
    rec["logic_parity"].update(status=ps.LOGIC_MATCHED_AVAILABLE_INPUTS,
                               input_basis=ps.BASIS_CHART)
    with pytest.raises(ps.ParityStatusError, match="does not exist"):
        ps.validate_stage_record("S7", rec)


def test_unattainable_logic_requires_a_reason():
    rec = ps.new_stage_record(ps.IMPL_IMPLEMENTED)
    rec["logic_parity"]["status"] = ps.LOGIC_UNATTAINABLE
    with pytest.raises(ps.ParityStatusError, match="requires a reason"):
        ps.validate_stage_record("S7", rec)
    rec["logic_parity"]["unattainable_reason"] = "NOT_A_REAL_REASON"
    with pytest.raises(ps.ParityStatusError, match="bad unattainable_reason"):
        ps.validate_stage_record("S7", rec)


def test_a_v2_manifest_migrates_to_v3_with_inputs_available():
    """Sound only because every v2 stage reads price and time alone. S7 is the
    first stage with an external input and cannot appear in a v2 manifest."""
    doc = {"schema": ps.MANIFEST_SCHEMA_V2,
           "stages": {"S1": ps.new_stage_record(ps.IMPL_IMPLEMENTED)}}
    doc["stages"]["S1"].pop("input_availability")
    out = ps.migrate_manifest(doc)
    assert out["schema"] == ps.MANIFEST_SCHEMA_V3
    assert out["stages"]["S1"]["input_availability"]["status"] == ps.INPUT_AVAILABLE


def test_an_unknown_manifest_schema_is_still_refused():
    with pytest.raises(ps.ParityStatusError, match="unsupported"):
        ps.migrate_manifest({"schema": "tradingview-oracle-parity-manifest-v9"})


# ══ scope ════════════════════════════════════════════════════════════════════

def test_s8_is_not_implemented_and_its_gate_cannot_open_by_omission():
    assert STAGE_STATUS["S8"] == ps.IMPL_UNIMPLEMENTED
    assert STAGE_DEPENDS_ON["S8"], (
        "an undeclared stage makes evaluate_gate return OPEN on an empty "
        "dependency list — 'you may begin S8'")
    assert "S7" in STAGE_DEPENDS_ON["S8"]


def test_the_reference_carries_no_post_fill_field(prefill):
    """S7 stops at the gate. A stop, a target or an R anywhere in the replay's
    output would mean the boundary had moved without anyone deciding to move
    it."""
    banned = ("stop", "tp", "target", "rr", "net_r", "pnl", "risk_amount",
              "weighted_r", "fill_price", "exit")
    obs = [_ob(1, "bullish", 1.1000, 1.0900, 0)]
    setups, _ = _run(obs, [_c(0, 1.1, 1.1, 1.09, 1.1)], prefill)
    for key in setups[0]:
        assert not any(b in key.lower() for b in banned), key
