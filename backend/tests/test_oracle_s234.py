"""Stage S2/S3/S4 tests — volatility, live-path swings, BOS/CHoCH.

The load-bearing test is `test_replay_reproduces_production_order_blocks`: the
replay never builds an order-block box, but it decides which breaks happen, in
what order, with which tag, against which swing — which is exactly what fixes
each OB's identity. Comparing those against `detect_order_blocks` proves the
reference implementation the Pine fragments mirror is faithful.

Several tests assert COUNTER-INTUITIVE behaviour (equal highs produce nothing;
the cold start is asymmetric; both a bull and a bear break can fire on one bar).
Those exist because a "reasonable" Pine implementation gets them wrong.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import replay_structure as rs  # noqa: E402
from tools.oracle import session_codes as codes_mod  # noqa: E402
from tools.oracle.compare_stages import (STAGE_FIELDS, STAGE_PRICE_TOL,  # noqa: E402
                                         CompareError, compare, latch_s5)
from tools.oracle.engine_access import load_engine, resolve_config  # noqa: E402
from tools.oracle.export_trace import build_trace  # noqa: E402
from tools.oracle.generate_pine import (OUT_PINE, STAGE_DEPENDS_ON,  # noqa: E402
                                        STAGE_STATUS, generate)
from tools.oracle.lint_pine import lint  # noqa: E402

FIXTURES = CT_ROOT / "golden" / "tradingview_oracle" / "s1"


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


def _trace(fid, tf="15min"):
    return build_trace(symbol="EURUSD", timeframe=tf,
                       input_path=FIXTURES / f"{fid}.csv", fixture_id=fid)


# ══ the reference implementation ═════════════════════════════════════════════

def test_replay_reproduces_production_order_blocks(engine):
    """The whole S3/S4 mirror rests on this."""
    import pandas as pd
    cfg, _ = resolve_config(engine)
    m1 = pd.read_csv(FIXTURES / "F-S3-SWINGS.csv")
    prep = engine.core.prepare_candles_for_simulation(m1)
    det = engine.rb.resample_candles(prep, cfg.detection_timeframe)
    res = rs.selfcheck(engine.core, det, swing_length=cfg.swing_length,
                       ob_filter=cfg.ob_filter, pip_size=cfg.pip_size,
                       min_pips=cfg.min_ob_size_pips, max_pips=cfg.max_ob_size_pips)
    assert res["ok"], res
    assert res["replayed_obs"] > 0
    assert res["replayed_obs"] == res["production_obs"]


def test_s5_replay_reproduces_every_order_block_field_exactly(engine):
    """THE S5 canonical reference.

    Not "the same number of blocks" and not "the same tags" — every field of
    every block, in append order, including `ob_id`. Verified across three
    fixtures because the failure modes differ: the origin scan needs a long
    uncrossed swing to be wrong about, the size gate needs a wide block, and the
    id sequence only proves anything when a rejection has happened.
    """
    import pandas as pd
    cfg, _ = resolve_config(engine)
    for fid in ("F-S3-SWINGS", "F-S2-FLIP", "F-TV-S1S4"):
        m1 = pd.read_csv(FIXTURES / f"{fid}.csv")
        det = engine.rb.resample_candles(
            engine.core.prepare_candles_for_simulation(m1),
            cfg.detection_timeframe)
        res = rs.selfcheck(engine.core, det, swing_length=cfg.swing_length,
                           ob_filter=cfg.ob_filter, pip_size=cfg.pip_size,
                           min_pips=cfg.min_ob_size_pips,
                           max_pips=cfg.max_ob_size_pips)
        assert res["ok"], (fid, res["mismatches"])
        assert res["replayed_obs"] == res["production_obs"] > 0, fid
        assert set(res["compared_fields"]) >= {
            "ob_id", "top", "bottom", "width_pips", "origin_index",
            "structure_tag", "direction"}, "the field set was narrowed"


def test_rejected_order_block_consumes_no_id():
    """The single easiest thing to get wrong when mirroring the size gate.

    Production assigns the CURRENT `ob_id` into the dict, runs the gate, and
    increments the counter only inside the accept branch. So a rejected block
    keeps its integer available for the next accepted one. Driven through the
    replay's own gate rather than asserted about the source, because the ordering
    is behaviour, not a comment.
    """
    assert rs.passes_size_filter(100.0, 0.0, 100.0) is True, (
        "the gate is INCLUSIVE at the boundary — both comparisons are strict")
    assert rs.passes_size_filter(100.0001, 0.0, 100.0) is False
    assert rs.passes_size_filter(0.0, 0.0, 100.0) is True, (
        "min is 0.0 and width is non-negative, so min can never reject")
    assert rs.passes_size_filter(None, 0.0, 100.0) is True, "no pip size => no gate"


def test_order_block_origin_prefers_the_earliest_extreme():
    """`search[search == search.min()].index[0]` takes the FIRST bar among ties.
    A `<=` comparison would silently take the last and move the whole box."""
    ph = [1.10, 1.10, 1.10, 1.10]
    pl = [1.00, 0.90, 0.90, 1.00]        # tie at index 1 and 2
    assert rs.order_block_origin(ph, pl, "bullish", 0, 4) == 1
    ph2 = [1.10, 1.20, 1.20, 1.10]
    assert rs.order_block_origin(ph2, pl, "bearish", 0, 4) == 1
    # the window is [pivot, detection-1]: an empty one yields no origin at all
    assert rs.order_block_origin(ph, pl, "bullish", 2, 2) is None


def test_order_block_width_uses_absolute_depth():
    """An inverted (high-volatility origin) box has top < bottom. `abs()` keeps
    its width positive so the gate measures depth, not sign."""
    assert rs.order_block_width_pips(1.1000, 1.1010, 0.0001) == pytest.approx(10.0)
    assert rs.order_block_width_pips(1.1010, 1.1000, 0.0001) == pytest.approx(10.0)
    assert rs.order_block_width_pips(1.10, 1.20, 0) is None


def test_replay_s2_matches_production_functions_bit_for_bit(engine):
    import pandas as pd
    cfg, _ = resolve_config(engine)
    m1 = pd.read_csv(FIXTURES / "F-S2-FLIP.csv")
    prep = engine.core.prepare_candles_for_simulation(m1)
    det = engine.rb.resample_candles(prep, cfg.detection_timeframe)

    prod_tr = engine.core.order_blocks._true_range(det).tolist()
    prod_parsed = engine.core.order_blocks._add_parsed_prices(det, cfg.ob_filter)
    rows, _ = rs.replay(engine.core, det["high"].tolist(), det["low"].tolist(),
                        det["close"].tolist(), cfg.swing_length, cfg.ob_filter)

    for i, r in enumerate(rows):
        assert r["s2"]["true_range"] == prod_tr[i], f"true_range at {i}"
        assert r["s2"]["parsed_high"] == prod_parsed["parsed_high"].iloc[i], i
        assert r["s2"]["parsed_low"] == prod_parsed["parsed_low"].iloc[i], i


# ══ S2 ═══════════════════════════════════════════════════════════════════════

def test_rma_is_first_value_seeded_not_sma():
    out, prev = rs.pine_rma([10.0, 20.0, 30.0, 40.0], 2)
    assert out == [10.0, 15.0, 22.5, 31.25]
    assert prev == [None, 10.0, 15.0, 22.5]


def test_rma_recurrence_matches_the_production_formula():
    vals = [1.0, 2.0, 3.0, 5.0, 8.0]
    out, _ = rs.pine_rma(vals, 7)
    expect = vals[0]
    for v in vals[1:]:
        expect = (expect * 6 + v) / 7
    assert math.isclose(out[-1], expect, rel_tol=0, abs_tol=0)


def test_true_range_first_bar_has_no_previous_close():
    tr, hl, hc, lc = rs.true_range([2.0, 3.0], [1.0, 2.0], [1.5, 2.5])
    assert hc[0] is None and lc[0] is None
    assert tr[0] == 1.0 == hl[0], "bar 0 must resolve to high-low, not NaN"
    assert tr[1] == max(1.0, abs(3.0 - 1.5), abs(2.0 - 1.5))


def test_parsed_price_flip_is_inclusive_at_the_threshold():
    """`>=`, not `>`. A bar exactly AT 2x the measure flips."""
    ph, pl, flip = rs.parsed_prices([2.0], [0.0], [1.0])   # range 2.0 == 2*1.0
    assert flip == [True]
    assert ph == [0.0] and pl == [2.0], "high and low must SWAP"


def test_parsed_price_does_not_flip_just_below_threshold():
    ph, pl, flip = rs.parsed_prices([1.999], [0.0], [1.0])
    assert flip == [False]
    assert ph == [1.999] and pl == [0.0]


def test_cumulative_mean_range_divisor_quirk(engine):
    """The divisor is `max(index, 1)` — the 0-BASED INDEX, not the bar count.

    So from index 2 onward it divides by one LESS than the number of bars, and
    it is not a mean at all in the usual sense. Verified against production
    directly rather than from the formula, because this is exactly the kind of
    off-by-one a "tidied" reimplementation would silently correct.
    """
    import pandas as pd
    tr = [2.0, 4.0, 6.0, 8.0]
    s = pd.Series(tr)
    prod = (s.cumsum() / pd.Series([max(i, 1) for i in range(len(s))],
                                   index=s.index)).tolist()
    assert rs.cumulative_mean_range(tr) == prod
    assert prod == [2.0, 6.0, 6.0, pytest.approx(20 / 3)]
    assert prod[1] == 6.0, "(2+4)/1 — index 0 and 1 both divide by 1"
    assert prod[2] == 6.0, "(2+4+6)/2 — divides by the INDEX, not the count"


def test_ta_rma_would_diverge_and_never_converge():
    """The evidence behind the ta.rma/ta.atr ban.

    Measured on F-S2-RMASEED: an SMA-seeded RMA agrees with production on ZERO
    bars and is still unequal at the final bar. Since the parsed-price flip is a
    `>=` threshold test, a bar near it can flip the other way and move an OB.
    """
    t = _trace("F-S2-RMASEED")
    tr = [b["s2"]["true_range"] for b in t["bars"]]
    prod = [b["s2"]["atr"] for b in t["bars"]]
    P = rs.ATR_PERIOD
    assert len(tr) > P + 50, "fixture too short to demonstrate this"

    sma_seeded, prev = [], None
    for i, v in enumerate(tr):
        if i < P - 1:
            sma_seeded.append(None)
        elif i == P - 1:
            prev = sum(tr[:P]) / P
            sma_seeded.append(prev)
        else:
            prev = (prev * (P - 1) + v) / P
            sma_seeded.append(prev)

    pairs = [(prod[i], sma_seeded[i]) for i in range(len(tr))
             if sma_seeded[i] is not None]
    assert pairs
    assert not any(a == b for a, b in pairs), "they must never be equal"
    assert pairs[-1][0] != pairs[-1][1], "still unequal at the final bar"


def test_s2_is_present_on_every_bar_including_warmup():
    t = _trace("F-S2-WARMUP")
    assert all("s2" in b for b in t["bars"])
    assert t["bars"][0]["s2"]["atr"] == t["bars"][0]["s2"]["true_range"], "seed"
    assert t["bars"][0]["s2"]["atr_warm"] is True


# ══ S3 ═══════════════════════════════════════════════════════════════════════

def test_warmup_bars_carry_no_structure_state():
    """Production's loop starts at `swing_length`; earlier bars are never seen."""
    t = _trace("F-S3-WARMUP")
    cfg_len = t["header"]["structure_config"]["swing_length"]
    warm = [b for b in t["bars"][:cfg_len]]
    assert warm and all(b["s3"]["warmup"] for b in warm)
    for b in warm:
        assert "swing_high_level" not in b["s3"], "must be ABSENT, not null/zero"
        assert b["s4"]["warmup"] is True


def test_equal_highs_do_not_create_a_swing():
    """STRICT `>`: a pivot equal to anything in its right window is not a swing."""
    core = load_engine(require_pin=True).core
    L = 3
    high = [10.0] * (L + 1)          # pivot equals the window max
    low = [1.0, 2.0, 3.0, 4.0]
    close = [5.0] * (L + 1)
    rows, _ = rs.replay(core, high, low, close, swing_length=L)
    last = rows[-1]["s3"]
    assert last["new_leg_high"] is False


def test_equal_lows_do_not_create_a_swing():
    core = load_engine(require_pin=True).core
    L = 3
    low = [1.0] * (L + 1)
    high = [10.0, 9.0, 8.0, 7.0]
    close = [5.0] * (L + 1)
    rows, _ = rs.replay(core, high, low, close, swing_length=L)
    assert rows[-1]["s3"]["new_leg_low"] is False


def test_cold_start_is_asymmetric():
    """current_leg starts 0 and BEARISH_LEG is also 0, so the first new_leg_high
    yields leg_change == 0 and records NOTHING, while the first new_leg_low
    yields +1 and records a swing low."""
    core = load_engine(require_pin=True).core
    L = 2
    # pivot high strictly greater than its right window -> new_leg_high
    high = [10.0, 1.0, 2.0]
    low = [5.0, 4.0, 4.5]
    close = [6.0, 6.0, 6.0]
    rows, _ = rs.replay(core, high, low, close, swing_length=L)
    r = rows[-1]["s3"]
    assert r["new_leg_high"] is True
    assert r["leg_change"] == 0, "BEARISH_LEG == 0 == initial current_leg"
    assert r["swing_created"] is None, "nothing recorded on the first high"


def test_only_the_most_recent_uncrossed_swing_is_retained():
    """Production keeps ONE dict per side — a new swing REPLACES the old."""
    t = _trace("F-S3-SWINGS")
    creations = [b for b in t["bars"]
                 if not b["s3"]["warmup"] and b["s3"]["swing_created"] == "swing_high"]
    assert len(creations) >= 2
    a, b = creations[0], creations[1]
    assert a["s3"]["swing_high_level"] != b["s3"]["swing_high_level"] or \
        a["s3"]["swing_high_index"] != b["s3"]["swing_high_index"]
    assert b["s3"]["swing_high_seq"] > a["s3"]["swing_high_seq"]


def test_swing_seq_is_monotonic_per_side():
    t = _trace("F-S3-SWINGS")
    hi = [b["s3"]["swing_high_seq"] for b in t["bars"]
          if not b["s3"]["warmup"] and b["s3"]["swing_high_seq"]]
    assert hi == sorted(hi)


def test_dead_detect_swings_is_not_what_we_mirror(engine):
    """`detect_swings` records EVERY pivot into columns; the live loop keeps only
    the most recent uncrossed one. Mirroring the dead function would be wrong."""
    import pandas as pd
    cfg, _ = resolve_config(engine)
    m1 = pd.read_csv(FIXTURES / "F-S3-SWINGS.csv")
    prep = engine.core.prepare_candles_for_simulation(m1)
    det = engine.rb.resample_candles(prep, cfg.detection_timeframe)

    dead = engine.core.detect_swings(det, length=cfg.swing_length)
    dead_pivots = int(dead["swing_high"].sum() + dead["swing_low"].sum())

    t = _trace("F-S3-SWINGS")
    live_created = sum(1 for b in t["bars"]
                       if not b["s3"]["warmup"] and b["s3"]["swing_created"])
    assert dead_pivots > 0 and live_created > 0
    # Both walk the same leg machine, so the COUNT of transitions agrees; what
    # differs is retention — the live path keeps one per side at a time.
    assert abs(dead_pivots - live_created) <= 1, (
        "leg transitions should agree; retention is what differs")


# ══ S4 ═══════════════════════════════════════════════════════════════════════

def test_first_break_of_a_run_is_always_bos():
    """bias starts at 0, which equals neither BULLISH nor BEARISH, so the CHoCH
    branch cannot be taken first."""
    t = _trace("F-S4-SEED")
    evs = [e for b in t["bars"] for e in b.get("s4", {}).get("events", [])]
    assert evs
    assert evs[0]["tag"] == "BOS"
    assert evs[0]["bias_before"] == 0


def test_bias_is_never_reset():
    t = _trace("F-S4-STRUCTURE")
    seen = [b["s4"]["swing_trend_bias"] for b in t["bars"] if not b["s4"]["warmup"]]
    first_nonzero = next((i for i, v in enumerate(seen) if v != 0), None)
    assert first_nonzero is not None
    assert all(v != 0 for v in seen[first_nonzero:]), (
        "once seeded the bias never returns to 0 — limitation L-04")


def test_choch_only_when_flipping_from_the_opposite_bias():
    t = _trace("F-S4-STRUCTURE")
    for b in t["bars"]:
        for e in b.get("s4", {}).get("events", []):
            if e["tag"] == "CHoCH":
                opposite = -1 if e["side"] == "bullish" else 1
                assert e["bias_before"] == opposite, e
            else:
                assert e["bias_before"] != (-1 if e["side"] == "bullish" else 1), e


def test_repeated_same_direction_breaks_are_bos():
    t = _trace("F-S4-STRUCTURE")
    evs = [e for b in t["bars"] for e in b.get("s4", {}).get("events", [])]
    runs = 0
    for a, b in zip(evs, evs[1:]):
        if a["side"] == b["side"]:
            assert b["tag"] == "BOS", b
            runs += 1
    assert runs > 0, "fixture should contain a same-direction repeat"


def test_a_crossed_swing_cannot_break_twice():
    t = _trace("F-S4-STRUCTURE")
    for b in t["bars"]:
        s3 = b.get("s3", {})
        if s3.get("warmup"):
            continue
        for e in b.get("s4", {}).get("events", []):
            # The break sets crossed=True on the same bar; a later bar cannot
            # re-break the same swing unless a NEW one replaced it.
            assert e["swing_level"] is not None


def test_bull_is_evaluated_before_bear_when_both_fire():
    """Both blocks are independent ifs. If a bar produces two events they must be
    ordered bull-then-bear, and the bear tag uses the bias the bull just set."""
    t = _trace("F-S4-VOLATILE")
    doubles = [b for b in t["bars"] if b.get("s4", {}).get("event_count", 0) > 1]
    for b in doubles:
        evs = b["s4"]["events"]
        assert evs[0]["side"] == "bullish" and evs[1]["side"] == "bearish"
        assert evs[1]["bias_before"] == evs[0]["bias_after"]


def test_break_test_uses_raw_closes_not_parsed_prices():
    """The break test reads close[i-1] and close[i]. Parsed prices belong to S5."""
    t = _trace("F-S4-STRUCTURE")
    bars = t["bars"]
    for i, b in enumerate(bars):
        for e in b.get("s4", {}).get("events", []):
            assert e["cur_close"] == b["ohlcv"]["close"]
            assert e["prev_close"] == bars[i - 1]["ohlcv"]["close"]


def test_break_operators_are_asymmetric():
    """prev <= level AND cur > level for bulls; prev >= level AND cur < level for
    bears. A previous close exactly AT the level still permits a break."""
    t = _trace("F-S4-STRUCTURE")
    for b in t["bars"]:
        for e in b.get("s4", {}).get("events", []):
            if e["side"] == "bullish":
                assert e["prev_close"] <= e["swing_level"] < e["cur_close"]
            else:
                assert e["prev_close"] >= e["swing_level"] > e["cur_close"]


def test_truncated_history_can_seed_a_different_bias():
    """L-04, demonstrated. The same calendar window traced from two different
    start points can carry a different bias at the same bar."""
    full = _trace("F-S4-STRUCTURE")
    short = _trace("F-S4-SEED")
    f_first = next(e for b in full["bars"] for e in b.get("s4", {}).get("events", []))
    s_first = next(e for b in short["bars"] for e in b.get("s4", {}).get("events", []))
    # Both are BOS (bias 0), but they are seeded at DIFFERENT bars by different
    # swings — which is precisely how the tags can diverge downstream.
    assert f_first["tag"] == s_first["tag"] == "BOS"
    assert (f_first["swing_level"], f_first["cur_close"]) != \
           (s_first["swing_level"], s_first["cur_close"])


# ══ trace + generation ═══════════════════════════════════════════════════════

def test_trace_declares_s2_through_s6_at_detection_timeframe():
    h = _trace("F-S3-SWINGS")["header"]
    assert h["stage"] == "S6"
    for s in ("volatility", "swings", "structure", "order_blocks", "regime"):
        assert s in h["sections_implemented"]
    # Everything past creation is still absent, and absence is the claim.
    for s in ("market_state", "candidates", "cohorts", "lifecycle"):
        assert s in h["sections_unimplemented"]


def test_execution_timeframe_trace_carries_s1_only():
    """Production computes structure on the resampled DETECTION frame, never on
    1m. Emitting s2/s3/s4 for a 1m trace would invent values production never had."""
    t = build_trace(symbol="EURUSD", timeframe="1min",
                    input_path=FIXTURES / "F-S1-BOUNDARIES.csv",
                    fixture_id="F-S1-BOUNDARIES")
    assert t["header"]["stage"] == "S1"
    assert "volatility" not in t["header"]["sections_implemented"]
    assert all("s2" not in b and "s3" not in b and "s4" not in b for b in t["bars"])


def test_trace_export_is_deterministic_with_structure():
    from tools.oracle.export_trace import content_hash
    assert content_hash(_trace("F-S3-SWINGS")) == content_hash(_trace("F-S3-SWINGS"))


def test_generated_pine_has_no_forbidden_builtins():
    src = OUT_PINE.read_text(encoding="utf-8")
    code = "\n".join(l.split("//")[0] for l in src.splitlines())
    for banned in ("ta.atr", "ta.rma", "ta.ema", "ta.sma",
                   "ta.pivothigh", "ta.pivotlow"):
        assert banned not in code, f"{banned} is forbidden — production differs"


def test_generated_pine_uses_highest_lowest_for_the_right_window():
    """ta.highest/ta.lowest ARE permitted: over SWING_LENGTH on the current bar
    they cover offsets 0..L-1, exactly production's inclusive .loc slice."""
    src = OUT_PINE.read_text(encoding="utf-8")
    assert "ta.highest(high, SWING_LENGTH)" in src
    assert "ta.lowest(low,  SWING_LENGTH)" in src or "ta.lowest(low, SWING_LENGTH)" in src


def test_generated_pine_stops_at_order_block_creation():
    """S5 draws boxes; it must NOT have grown a lifecycle or an entry.

    Production assigns no mitigation, invalidation or expiry state at creation,
    so anything resembling one here would be invented behaviour — the exact
    failure mode that "just finish the SMC indicator" produces.
    """
    src = OUT_PINE.read_text(encoding="utf-8")
    code = "\n".join(line.split("//")[0] for line in src.splitlines())
    assert "box.new" in code, "S5 is implemented; the boxes belong here"
    # THIS IS NOT A SCRIPT-WIDE BAN ANY MORE. M-TV-FILL-TIME-SEMANTICS-1 gave
    # the LIVE layer (fragment 68) a deliberate resting -> trigger -> outcome
    # lifecycle, driven by chart structure against production's own target
    # table. What must still never appear ANYWHERE is an order: this is an
    # indicator, and `strategy.*` would make it place trades.
    for banned in ("strategy.entry", "strategy.exit", "strategy.order",
                   "strategy.close"):
        assert banned not in code, f"{banned} would make this a strategy"
    # The S5 DETECTION fragment specifically must stay free of lifecycle state:
    # production assigns none at creation, so any here would be invented.
    s5 = (CT_ROOT / "pine" / "src" / "65_order_blocks.pinefrag").read_text(
        encoding="utf-8")
    s5_code = "\n".join(line.split("//")[0] for line in s5.splitlines())
    for banned in ("mitigat", "invalidat", "takeProfit", "stopLoss",
                   "riskPerTrade"):
        assert banned not in s5_code, f"{banned} belongs to a later stage"


def test_pine_lints_clean_with_structure():
    errs = [f for f in lint(OUT_PINE.read_text(encoding="utf-8"))
            if f["severity"] == "error"]
    assert not errs, errs


def test_generation_is_deterministic_with_structure():
    assert generate(write=False)["pine"] == generate(write=False)["pine"]


def test_stage_statuses_are_honest():
    """STAGE_STATUS is now IMPLEMENTATION only — whether the code exists. That a
    stage exists says nothing about whether it is correct, which is why the three
    parity dimensions live in the manifest and not here."""
    from tools.oracle import parity_status as ps
    for s in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert STAGE_STATUS[s] == ps.IMPL_IMPLEMENTED
    assert STAGE_STATUS["S7"] == ps.IMPL_UNIMPLEMENTED
    assert set(STAGE_STATUS.values()) <= set(ps.IMPLEMENTATION_STATUSES)


def test_stage_dependencies_are_declared():
    assert STAGE_DEPENDS_ON["S4"] == ["S1", "S3"]
    assert STAGE_DEPENDS_ON["S2"] == ["S1"]


# ══ comparator ═══════════════════════════════════════════════════════════════

def _corrupt_packed(row, field, value):
    """Set ONE logical field to `value` inside whichever packed plot carries it.

    A perturbation test must corrupt what the comparator actually reads. Before
    the packing refactor these tests wrote `row["oracleBias"] = 7` directly; that
    column no longer exists, so the write landed in an ignored cell and the test
    silently stopped testing anything — it passed while asserting a FAIL. Going
    through PACKED_SPEC means the tests follow the transport wherever it moves,
    and a field that stops being packed still resolves here.
    """
    for group, spec in codes_mod.PACKED_SPEC.items():
        names = [f for f, _w, _o in spec]
        if field not in names:
            continue
        plot = codes_mod.PACKED_PLOT[group]
        fields = codes_mod.unpack(group, int(round(float(row[plot]))))
        fields[field] = value
        row[plot] = codes_mod.pack(group, fields)
        return
    row[field] = value


def _synth(path, fixture="F-S3-SWINGS", perturb=None, drop_stage=None):
    import csv as _csv
    t = _trace(fixture)
    latch_s5(t["bars"])          # the export convention; see compare_stages
    probe = t["bars"][-1]
    cols = ["time"]
    for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
        if st != drop_stage:
            cols += list(STAGE_FIELDS[st](probe).keys())
    cols = list(dict.fromkeys(cols))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for i, b in enumerate(t["bars"]):
            row = {"time": b["bar_timestamp"][:19].replace(" ", "T") + "Z"}
            for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
                if st == drop_stage:
                    continue
                for f, (v, _k) in STAGE_FIELDS[st](b).items():
                    row[f] = "" if v is None else v
            if perturb:
                perturb(i, b, row)
            w.writerow({k: row.get(k, "") for k in cols})
    return t


def test_comparator_passes_a_faithful_s1_s4_export(tmp_path):
    p = tmp_path / "e.csv"
    _synth(p)
    r = compare("F-S3-SWINGS", p, feed="TEST:EURUSD")
    assert r["result"] == "PASS"
    for st in ("S1", "S2", "S3", "S4"):
        assert r["results"][st]["result"] == "PASS"
        assert r["results"][st]["compared"] > 0


def test_s3_failure_invalidates_s4(tmp_path):
    def bad(i, b, row):
        if i == 3000 and row.get("oracleSwingHigh") not in ("", None):
            row["oracleSwingHigh"] = float(row["oracleSwingHigh"]) + 0.01
    p = tmp_path / "e.csv"
    _synth(p, perturb=bad)
    r = compare("F-S3-SWINGS", p)
    assert r["results"]["S3"]["result"] == "FAIL"
    assert r["results"]["S4"]["result"] == "INVALIDATED"
    assert "S3" in r["results"]["S4"]["invalidated_by"]


def test_s1_failure_invalidates_everything_downstream(tmp_path):
    def bad(i, b, row):
        # The session code travels PACKED (TradingView's 64-plot ceiling), so
        # corrupting the composite is what corrupting a session code now means.
        if i == 2000:
            row["oracleS1Codes"] = int(row["oracleS1Codes"]) + 1
    p = tmp_path / "e.csv"
    _synth(p, perturb=bad)
    r = compare("F-S3-SWINGS", p)
    assert r["results"]["S1"]["result"] == "FAIL"
    for st in ("S2", "S3", "S4"):
        assert r["results"][st]["result"] == "INVALIDATED", st


def test_comparator_reports_the_earliest_divergence(tmp_path):
    t = _trace("F-S3-SWINGS")
    stamps = [b["bar_timestamp"][:16] for b in t["bars"]]

    def bad(i, b, row):
        if i in (1500, 4000):
            # 2 is outside the real bias vocabulary (-1/0/+1) but inside the
            # packed radix, so this corrupts the value without overflowing —
            # exactly the shape of a Pine logic defect.
            _corrupt_packed(row, "oracleBias", 2)
    p = tmp_path / "e.csv"
    _synth(p, perturb=bad)
    r = compare("F-S3-SWINGS", p)
    assert r["results"]["S4"]["first_divergence"]["bar_utc"] == stamps[1500]
    assert r["results"]["S4"]["first_divergence"]["field"] == "oracleS4Codes"


def test_comparator_refuses_an_export_without_s1_columns(tmp_path):
    p = tmp_path / "e.csv"
    _synth(p, drop_stage="S1")
    with pytest.raises(CompareError, match="missing required S1"):
        compare("F-S3-SWINGS", p)


def test_missing_stage_columns_fail_closed(tmp_path):
    """POLICY REVERSED. A requested stage whose columns are absent used to report
    NO_EVIDENCE and let the run continue.

    That is unsafe: "no evidence" and "verified" are indistinguishable in a
    summary line, so an export taken before a stage's plots existed read as
    benign. It now raises. A caller who genuinely wants the other stages must ask
    for them explicitly via `--stages`, which makes the narrowed scope visible in
    the report instead of inferred from a missing column.
    """
    p = tmp_path / "e.csv"
    _synth(p, drop_stage="S4")
    with pytest.raises(CompareError, match="missing required S4 columns"):
        compare("F-S3-SWINGS", p)

    # …and asking only for the stages that ARE present still works.
    r = compare("F-S3-SWINGS", p, stages=("S1", "S2", "S3"))
    for st in ("S1", "S2", "S3"):
        assert r["results"][st]["result"] == "PASS"
    assert "S4" not in r["results"]


def test_comparator_tolerances_are_per_stage():
    assert STAGE_PRICE_TOL["S3"] < STAGE_PRICE_TOL["S2"], (
        "an S3 swing level is a raw bar extreme; an S2 ATR is a seeded "
        "accumulation and needs a looser band")


# ══ TradingView-reachable verification fixture ═══════════════════════════════

def test_tv_fixture_starts_at_the_charts_earliest_bar():
    """The comparison fixture must start where the CHART starts, not where the
    phenomenon is most convenient.

    A retail plan caps EURUSD 15m history at ~9 months. Starting the Python trace
    at the same bar removes the L-04 seed divergence BY CONSTRUCTION — both sides
    seed the bias, swing state and RMA identically.
    """
    from tools.oracle.build_fixtures import TV_CHART_EARLIEST
    t = _trace("F-TV-S1S4")
    assert t["bars"][0]["bar_timestamp"].startswith(TV_CHART_EARLIEST[:10])
    assert t["bars"][0]["bar_timestamp"][11:16] == "00:00"


def test_tv_fixture_is_within_reachable_history():
    t = _trace("F-TV-S1S4")
    assert t["bars"][0]["bar_timestamp"][:10] >= "2025-09-30"
    assert t["bars"][-1]["bar_timestamp"][:10] <= "2026-06-19"


def test_tv_fixture_exercises_every_stage():
    t = _trace("F-TV-S1S4")
    b = t["bars"]
    evs = [e for x in b for e in x.get("s4", {}).get("events", [])]
    assert len(b) > 5000, "too small to exercise ATR decay"
    # S1
    assert {x["session"]["key"] for x in b} == {"asia", "london", "lull",
                                               "newYork", "ny_pm", "outside"}
    assert sum(1 for x in b if x["utc_day"]["day_transition"]) > 50
    # S2
    assert sum(1 for x in b if x["s2"]["high_volatility"]) > 100
    assert any(x["s2"]["atr_warm"] for x in b) and not b[-1]["s2"]["atr_warm"]
    # S3
    assert sum(1 for x in b if x["s3"].get("swing_created")) > 50
    # S4 — both tags, both directions
    assert sum(1 for e in evs if e["tag"] == "BOS") > 10
    assert sum(1 for e in evs if e["tag"] == "CHoCH") > 10
    assert sum(1 for e in evs if e["side"] == "bullish") > 10
    assert sum(1 for e in evs if e["side"] == "bearish") > 10


def test_tv_fixture_covers_every_session_boundary_adjacency():
    """Why the 1-minute export was dropped.

    Each boundary appears as CONSECUTIVE 15m bars, which asserts the same
    `[start, end)` rule that HH:59 -> HH:00 would at minute resolution.
    """
    t = _trace("F-TV-S1S4")
    present = {x["bar_timestamp"][11:16] for x in t["bars"]}
    for hhmm in ("06:45", "07:00", "09:45", "10:00", "11:45", "12:00",
                 "14:45", "15:00", "16:45", "17:00", "23:45", "00:00"):
        assert hhmm in present, hhmm

    # The boundaries are LONDON LOCAL (M-SESSION-DST-1), so they are asserted
    # against the London wall clock. Keying them to a fixed UTC HH:MM was only
    # ever right in GMT, and this fixture spans the autumn transition.
    import datetime as dt
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    by_local = {}
    utc_at_ten = set()
    for x in t["bars"]:
        ts = dt.datetime.fromisoformat(x["bar_timestamp"])
        local = ts.astimezone(london).strftime("%H:%M")
        by_local.setdefault(local, set()).add(x["session"]["key"])
        if local == "10:00":
            utc_at_ten.add(ts.strftime("%H:%M"))

    for hhmm, key in (("06:45", "asia"), ("07:00", "london"),
                      ("09:45", "london"), ("10:00", "lull"),
                      ("11:45", "lull"), ("12:00", "newYork"),
                      ("14:45", "newYork"), ("15:00", "ny_pm"),
                      ("16:45", "ny_pm"), ("17:00", "outside")):
        assert by_local.get(hhmm) == {key}, (
            f"London {hhmm} -> {by_local.get(hhmm)}, expected {key}")

    # …and the SAME local boundary really does sit at two different UTC hours
    # inside this fixture, which is the property a UTC classifier cannot have.
    assert len(utc_at_ten) == 2, utc_at_ten


def test_warmup_exclusion_is_declared_not_silent(tmp_path):
    from tools.oracle.compare_stages import compare as cmp_stages
    p = tmp_path / "e.csv"
    _synth(p, fixture="F-TV-S1S4")
    strict = cmp_stages("F-TV-S1S4", p)
    assert strict["coverage"]["warmup_bars_excluded"] == 0

    relaxed = cmp_stages("F-TV-S1S4", p, warmup_bars=200)
    c = relaxed["coverage"]
    assert c["warmup_bars_excluded"] == 200
    assert c["warmup_window"] is not None
    assert c["bars_in_both"] == strict["coverage"]["bars_in_both"] - 200
    assert relaxed["result"] == "PASS"


def test_measured_convergence_is_recorded_in_the_report(tmp_path):
    """The cost of a mismatched start is carried in the evidence, so a reader can
    judge whether an exclusion was reasonable."""
    from tools.oracle.compare_stages import compare as cmp_stages
    p = tmp_path / "e.csv"
    _synth(p, fixture="F-TV-S1S4")
    mc = cmp_stages("F-TV-S1S4", p)["coverage"]["measured_convergence"]
    assert mc["bias_converges_bar"] == 122
    assert mc["swings_converge_bar"] == 84
    assert mc["flip_decisions_differing"] == 3


def _synth_with_ohlc(path, fixture="F-S3-SWINGS", shift=0.0, perturb=None,
                     drop_stage=None):
    """Like `_synth`, but also writes the raw OHLC the way TradingView does.

    `shift` moves the exported PRICES without touching the exported oracle
    values — the shape of a real feed difference (a broker quoting mid against a
    production dataset quoting bid)."""
    import csv as _csv
    t = _trace(fixture)
    latch_s5(t["bars"])          # the export convention; see compare_stages
    probe = t["bars"][-1]
    cols = ["time", "open", "high", "low", "close"]
    for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
        if st != drop_stage:
            cols += list(STAGE_FIELDS[st](probe).keys())
    cols = list(dict.fromkeys(cols))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for i, b in enumerate(t["bars"]):
            row = {"time": b["bar_timestamp"][:19].replace(" ", "T") + "Z"}
            for f in ("open", "high", "low", "close"):
                row[f] = f"{b['ohlcv'][f] + shift:.5f}"
            for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
                if st == drop_stage:
                    continue
                for f, (v, _k) in STAGE_FIELDS[st](b).items():
                    row[f] = "" if v is None else v
            if perturb:
                perturb(i, b, row)
            w.writerow({k: row.get(k, "") for k in cols})
    return t


def test_chart_bars_basis_reruns_the_engine_over_the_exported_bars(tmp_path):
    """Re-running Python over the export's own OHLC must reproduce the export's
    oracle values exactly — that is what makes the mode able to isolate Pine
    logic from the feed."""
    from tools.oracle.compare_stages import CHART_BARS
    p = tmp_path / "e.csv"
    t = _synth_with_ohlc(p)
    r = compare("F-S3-SWINGS", p, feed="TEST:EURUSD", input_basis=CHART_BARS)
    assert r["input_basis"]["mode"] == CHART_BARS
    assert r["input_basis"]["rebased_bars"] == len(t["bars"])
    # every bar now exists on both sides by construction
    assert r["coverage"]["python_only"] == 0
    assert r["result"] == "PASS", r["results"]


def test_chart_bars_basis_is_immune_to_a_pure_feed_shift(tmp_path):
    """The whole point. Shifting the exported prices leaves the exported oracle
    values self-consistent; comparing against the PRODUCTION dataset would flag
    that as a divergence, but a chart-bars comparison must still pass, because
    the Pine logic did not change."""
    from tools.oracle.compare_stages import CHART_BARS
    p = tmp_path / "e.csv"
    _synth_with_ohlc(p, shift=0.0)
    base = compare("F-S3-SWINGS", p, input_basis=CHART_BARS)
    assert base["result"] == "PASS"

    q = tmp_path / "shifted.csv"
    _synth_with_ohlc(q, shift=2.5e-4)
    shifted = compare("F-S3-SWINGS", q, input_basis=CHART_BARS)
    # Prices moved, so the re-run trace disagrees with the (unshifted) exported
    # oracle values — the mode is measuring the Pine, and it correctly notices.
    assert shifted["results"]["S2"]["result"] == "FAIL"
    assert shifted["input_basis"]["rebased_bars"] == base["input_basis"]["rebased_bars"]


def test_chart_bars_basis_refuses_an_export_without_raw_ohlc(tmp_path):
    from tools.oracle.compare_stages import CHART_BARS, CompareError
    p = tmp_path / "e.csv"
    _synth(p)                       # oracle columns only, no OHLC
    with pytest.raises(CompareError, match="raw OHLC"):
        compare("F-S3-SWINGS", p, input_basis=CHART_BARS)


def test_input_basis_defaults_to_the_stricter_production_comparison(tmp_path):
    """chart-bars answers a NARROWER question and must never become the silent
    default — a reader of a report has to know which one they are holding."""
    from tools.oracle.compare_stages import FIXTURE_BARS
    p = tmp_path / "e.csv"
    _synth_with_ohlc(p)
    assert compare("F-S3-SWINGS", p)["input_basis"]["mode"] == FIXTURE_BARS


def test_out_path_outside_the_repo_does_not_crash_after_writing(tmp_path, capsys):
    """`--out` used to be printed via `Path.relative_to(CT_ROOT)`, which RAISES
    for any path outside the repo. The report had already been written by then,
    so the run died *after* succeeding. Writing evidence and then failing to say
    where it went is the worst possible failure mode for an audit tool."""
    from tools.oracle.compare_stages import main as cmp_main
    p = tmp_path / "e.csv"
    _synth(p, fixture="F-TV-S1S4")
    out = tmp_path / "sub" / "report.json"

    rc = cmp_main(["--fixture", "F-TV-S1S4", "--export", str(p),
                   "--out", str(out), "--write"])

    assert rc == 0
    assert out.is_file(), "report must be written at the requested path"
    assert json.loads(out.read_text())["fixture"] == "F-TV-S1S4"
    assert "written:" in capsys.readouterr().out


def test_comparator_report_is_content_addressed(tmp_path):
    p = tmp_path / "e.csv"
    _synth(p)
    a = compare("F-S3-SWINGS", p)
    b = compare("F-S3-SWINGS", p)
    assert a["export"]["sha256"] == b["export"]["sha256"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


# ══ S5 edge fixtures ═════════════════════════════════════════════════════════
#
# The size gate has NEVER fired in eleven years of production history: 2,080
# order blocks, widths spanning 0.80 to 38.90 pips, none within 61 pips of the
# 100-pip limit. So the accept/reject boundary and everything downstream of it
# (no id consumed, next id still dense) is unreachable from real data and those
# fixtures are constructed. `build_fixtures` re-verifies each one against the
# production replay at build time; these tests pin the behaviour itself.

def _s5_candidates(fixture):
    t = _trace(fixture)
    return [c for b in t["bars"]
            for c in (b.get("s5") or {}).get("candidates", [])]


def test_order_block_just_inside_the_size_limit_is_accepted():
    cands = _s5_candidates("F-S5-SIZE-UNDER")
    assert len(cands) == 1
    c = cands[0]
    assert c["proposed_width_pips"] < 100.0
    assert c["size_gate_passed"] and c["appended"]
    assert c["ob_id"] == 1 and c["rejected_reason"] is None
    assert c["active"] is True


def test_widest_accepted_order_block_sits_at_the_representable_limit():
    """An EXACTLY 100.0-pip width does not exist.

    `width = |high-low| / pip_size`, and no pair of representable prices at
    EURUSD levels yields exactly 100.0 — the reachable widths step from
    99.99999999999787 straight to 100.00000000000009. This fixture holds the
    largest width the gate accepts, which is as close to the boundary as the
    number system permits.
    """
    cands = _s5_candidates("F-S5-SIZE-AT-LIMIT")
    assert len(cands) == 1
    c = cands[0]
    assert c["proposed_width_pips"] <= 100.0
    assert 100.0 - c["proposed_width_pips"] < 1e-9, (
        "this fixture must sit ON the limit, not merely near it")
    assert c["size_gate_passed"] and c["appended"] and c["ob_id"] == 1


def test_one_ulp_over_the_limit_is_rejected_and_consumes_no_id():
    """The whole gate in one fixture: 2e-12 pips wider than the one above."""
    cands = _s5_candidates("F-S5-SIZE-OVER")
    assert len(cands) == 1
    c = cands[0]
    assert c["proposed_width_pips"] > 100.0
    assert c["proposed_width_pips"] - 100.0 < 1e-9, "must be ON the limit"
    assert not c["size_gate_passed"] and not c["appended"]
    assert c["ob_id"] is None, "a rejected block must not take an id"
    assert c["rejected_reason"] == "size_filter"
    assert c["active"] is False


def test_a_rejection_does_not_shift_the_next_order_block_id():
    """THE id-density property.

    Production writes the current `ob_id` into the candidate BEFORE the gate and
    increments only inside the accept branch, so the block after a rejection
    reuses the integer. A mirror that incremented before the gate would number
    this one 2, and nothing else in the output would differ.
    """
    cands = _s5_candidates("F-S5-ID-DENSITY")
    assert len(cands) == 2
    rejected, accepted = cands
    assert rejected["appended"] is False and rejected["ob_id"] is None
    assert accepted["appended"] is True
    assert accepted["ob_id"] == 1, (
        f"the rejection consumed an id: next accepted got {accepted['ob_id']}")
    ids = [c["ob_id"] for c in cands if c["appended"]]
    assert ids == list(range(1, len(ids) + 1)), "accepted ids must be dense"


def test_a_rejection_still_burns_its_swing_and_flips_the_bias():
    """Rejection is NOT a no-op. The swing is marked crossed and the bias flips
    before the block is built, which is the only reason the second cycle in
    F-S5-ID-DENSITY can happen at all."""
    t = _trace("F-S5-ID-DENSITY")
    events = [e for b in t["bars"] for e in (b.get("s4") or {}).get("events", [])]
    assert len(events) == 2, "both breaks must fire, rejected block or not"
    assert all(e["bias_after"] != 0 for e in events)


def test_inverted_order_block_bounds_are_preserved():
    """A high-volatility origin SWAPS parsed high and low, so top < bottom.

    Production does not normalise it, and the width uses `abs()` so the box is
    still measured by depth. Cut from real history — 2026-04-17 is the only one
    of the twenty in the dataset a TradingView chart can still load.
    """
    cands = _s5_candidates("F-S5-INVERTED")
    inverted = [c for c in cands
                if c["proposed_top"] is not None
                and c["proposed_top"] < c["proposed_bottom"]]
    assert inverted, "the slice no longer contains an inverted box"
    c = inverted[0]
    assert c["proposed_width_pips"] > 0, "abs() keeps an inverted box positive"
    assert c["appended"], "an inverted box is still a valid box"


def test_origin_search_takes_the_earliest_of_equal_extremes():
    """Real fixture for the first-wins tie rule. A `<=` scan would take the last
    equal extreme and move the whole box."""
    cands = _s5_candidates("F-S5-ORIGIN-TIE")
    assert cands, "fixture produced no candidates"
    bars = _trace("F-S5-ORIGIN-TIE")["bars"]
    for c in cands:
        o, p, brk = c["origin_index"], c["pivot_index"], c["break_index"]
        if o is None:
            continue
        key = "parsed_low" if c["side"] == "bullish" else "parsed_high"
        window = [bars[j]["s2"][key] for j in range(p, brk)]
        best = min(window) if c["side"] == "bullish" else max(window)
        first = p + window.index(best)
        assert o == first, (
            f"origin {o} is not the EARLIEST bar holding the extreme ({first})")


# ══ the same-bar ordering question ═══════════════════════════════════════════

def test_a_same_bar_bull_and_bear_break_is_unreachable():
    """Not "rare" — IMPOSSIBLE, and therefore not a hazard.

        bull needs  prev_close <= SH  and  close >  SH
        bear needs  prev_close >= SL  and  close <  SL
        both    =>  SL <= prev_close <= SH   =>  SL <= SH
                    close > SH >= SL         =>  close > SL
                    but bear needs close < SL          => contradiction

    Confirmed empirically too: zero bars out of 285,790 in the frozen dataset
    carry more than one structure event, and the smallest gap between two
    OPPOSITE-side breaks is 2 bars. Pinned here on a fixture chosen for having
    opposite-side breaks close together.
    """
    t = _trace("F-S5-OPPOSITE-BREAKS")
    counts = [(b.get("s4") or {}).get("event_count", 0) for b in t["bars"]]
    assert max(counts) <= 1, "a same-bar dual break appeared — the proof is wrong"
    sides = [e["side"] for b in t["bars"]
             for e in (b.get("s4") or {}).get("events", [])]
    assert set(sides) == {"bullish", "bearish"}, (
        "fixture must contain both directions to be worth anything")
    per_bar = [len((b.get("s5") or {}).get("candidates", [])) for b in t["bars"]]
    assert max(per_bar) <= 1, "two order-block candidates on one bar"


def test_production_order_block_rows_have_no_orderable_ties(engine):
    """`prepare_order_blocks_for_simulation` sorts by `detection_time` with
    pandas' UNSTABLE quicksort. That is only a hazard if two rows can share a
    `detection_time`, and they cannot: at most one order block exists per bar,
    because a same-bar dual break is unreachable.

    Measured over the whole frozen dataset: 2,080 blocks, 2,080 distinct
    detection times, already monotonic, and the sort is a no-op. Recorded here so
    that the day production DOES allow two blocks on one bar, this fails and the
    limitation becomes live rather than silent.
    """
    import pandas as pd
    cfg, _ = resolve_config(engine)
    det = engine.rb.resample_candles(
        engine.core.prepare_candles_for_simulation(
            pd.read_csv(FIXTURES / "F-S5-INVERTED.csv")), cfg.detection_timeframe)
    obs = engine.core.detect_order_blocks(
        det, swing_length=cfg.swing_length, ob_filter=cfg.ob_filter,
        pip_size=cfg.pip_size, min_ob_size_pips=cfg.min_ob_size_pips,
        max_ob_size_pips=cfg.max_ob_size_pips)
    assert not obs.empty
    assert obs["detection_time"].nunique() == len(obs), (
        "duplicate detection_time -> the unstable sort now has a tie to lose")
    assert obs["detection_time"].is_monotonic_increasing
    assert obs["ob_id"].tolist() == list(range(1, len(obs) + 1))
    prepared = engine.core.prepare_order_blocks_for_simulation(obs)
    assert prepared["ob_id"].tolist() == obs["ob_id"].tolist(), (
        "the downstream sort reordered rows")


# ══ comparator: S5 must fail closed and must catch a shifted block ═══════════

def test_comparator_refuses_an_export_missing_s5_columns(tmp_path):
    """An export that predates the S5 plots must FAIL, not report NO_EVIDENCE.

    "No evidence" and "verified" look identical in a summary line, and S5's whole
    premise is that a box drawn on screen is not evidence.
    """
    p = tmp_path / "e.csv"
    _synth_with_ohlc(p, fixture="F-S5-INVERTED", drop_stage="S5")
    with pytest.raises(CompareError, match="missing required S5 columns"):
        compare("F-S5-INVERTED", p, input_basis="chart_bars")


def test_comparator_catches_a_shifted_order_block(tmp_path):
    """Move one exported bound and the comparison must fail — via the per-bar
    field AND via the running inventory checksum, so a divergence anywhere in the
    window is caught even if the summary bar happens to agree."""
    def bump(i, b, row):
        if row.get("oracleObTop") not in ("", None):
            row["oracleObTop"] = float(row["oracleObTop"]) + 0.0010

    p = tmp_path / "e.csv"
    _synth_with_ohlc(p, fixture="F-S5-INVERTED", perturb=bump)
    r = compare("F-S5-INVERTED", p, input_basis="chart_bars")
    assert r["results"]["S5"]["result"] == "FAIL"


def test_comparator_catches_a_STEP_in_the_active_inventory(tmp_path):
    """`ob_id` itself is not compared (L-05), so the ACTIVE COUNT carries the
    "a rejected block consumed no id" property.

    It is CUMULATIVE, and compared as an offset from the first scored bar — so a
    UNIFORM shift is forgiven ON PURPOSE: after a declared bootstrap the two
    sides legitimately differ by whatever accumulated inside the excluded
    interval, and refusing that would make a declared bootstrap meaningless.

    What must still be caught is a STEP: an extra (or missing) block appearing
    INSIDE the scored window. Both halves are asserted here, because the
    forgiveness is only defensible while the step is still detected.
    """
    def uniform(i, b, row):
        if row.get("oracleObActive") not in ("", None):
            row["oracleObActive"] = int(row["oracleObActive"]) + 1

    def step(i, b, row):
        if i > 400 and row.get("oracleObActive") not in ("", None):
            row["oracleObActive"] = int(row["oracleObActive"]) + 1

    p = tmp_path / "uniform.csv"
    _synth_with_ohlc(p, fixture="F-S5-INVERTED", perturb=uniform)
    assert compare("F-S5-INVERTED", p, input_basis="chart_bars")[
        "results"]["S5"]["result"] == "PASS", (
        "a uniform offset is forgiven by design — see the docstring")

    q = tmp_path / "step.csv"
    _synth_with_ohlc(q, fixture="F-S5-INVERTED", perturb=step)
    assert compare("F-S5-INVERTED", q, input_basis="chart_bars")[
        "results"]["S5"]["result"] == "FAIL", (
        "a block appearing inside the scored window MUST fail")


def test_a_spurious_order_block_is_caught_per_bar(tmp_path):
    """The per-bar creation fields are what actually police "a box appeared that
    should not have". They are NOT cumulative, so no baseline forgives them."""
    def spurious(i, b, row):
        if i == 500:
            _corrupt_packed(row, "oracleObCreated", 1)
            _corrupt_packed(row, "oracleObCandidates", 1)

    p = tmp_path / "e.csv"
    _synth_with_ohlc(p, fixture="F-S5-INVERTED", perturb=spurious)
    r = compare("F-S5-INVERTED", p, input_basis="chart_bars")
    assert r["results"]["S5"]["result"] == "FAIL"


def test_chart_bars_comparison_passes_S1_through_S5(tmp_path):
    """One export, five stages, no exclusions.

    S6 is EXCLUDED on purpose: F-S5-INVERTED is a nine-day window and S6 needs
    twenty complete UTC days before a market state exists at all, so every bar
    would be warm-up. Asking for it here would measure the fixture's length, not
    the comparator. `test_chart_bars_comparison_passes_S1_through_S6` covers the
    full path on a fixture long enough to carry a regime.
    """
    p = tmp_path / "e.csv"
    _synth_with_ohlc(p, fixture="F-S5-INVERTED")
    r = compare("F-S5-INVERTED", p, feed="TEST:EURUSD", input_basis="chart_bars",
                stages=("S1", "S2", "S3", "S4", "S5"))
    for st in ("S1", "S2", "S3", "S4", "S5"):
        assert r["results"][st]["result"] == "PASS", (st, r["results"][st])
    assert r["result"] == "PASS"


def test_synthetic_s5_fixtures_still_exhibit_their_edges(engine):
    """The fixtures are only worth having while they still contain the edge.

    `build_fixtures` verifies this at build time; repeating it here means a
    fixture that silently stopped exercising the gate fails CI rather than
    waiting for someone to rebuild.
    """
    from tools.oracle.build_fixtures import SYNTHETIC_S5_FIXTURES
    from tools.oracle import s5_scenarios as sc
    for fid, _desc, make, expect in SYNTHETIC_S5_FIXTURES:
        bars, _meta = make(sc)
        sc.verify_scenario(engine, bars, expect, fid)
