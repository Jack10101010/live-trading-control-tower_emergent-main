"""Stage S6 tests — UTC daily aggregation and the market-state classifier.

The load-bearing test is `test_s6_replay_reproduces_production_panel_exactly`:
the replay recomputes every intermediate the Pine oracle must mirror, and is
checked field-for-field against `daily_regime_panel` over real history. If the
replay drifts, that fails rather than the trace quietly lying.

Several tests assert behaviour a "reasonable" implementation gets wrong: the
deviation is a SAMPLE deviation, the EMA is first-value seeded, a price exactly
ON the EMA is Bear, and a BBW exactly at the threshold is Compress.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import replay_regime as rr  # noqa: E402
from tools.oracle.engine_access import load_engine  # noqa: E402

FIXTURES = CT_ROOT / "golden" / "tradingview_oracle" / "s1"


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


# ══ the reference implementation ═════════════════════════════════════════════

def test_s6_replay_reproduces_production_panel_exactly(engine):
    """THE S6 canonical reference — every field, every day.

    Verified over the chart-reachable window rather than the full dataset so the
    suite stays fast; the full 3,586-day sweep is run on demand and reported in
    the audit.
    """
    import pandas as pd
    prep = engine.core.prepare_candles_for_simulation(
        pd.read_csv(FIXTURES / "F-TV-S1S5.csv"))
    res = rr.selfcheck(engine.core, prep, cfg=None, symbol="EURUSD")
    assert res["ok"], res["mismatches"]
    assert res["days"] == res["production_days"] > 0
    assert set(res["compared_fields"]) >= {
        "ema", "bbw", "adx", "marketState", "trendState", "volatilityState"}


def test_ema_and_adx_are_bit_exact_against_production(engine):
    """EMA and ADX reproduce to the LAST BIT. Only BBW carries float noise, and
    it is bounded far below the threshold's own resolution (see the margin
    test). Pinned so a future 'tidy-up' that loosens either is caught."""
    import pandas as pd
    prep = engine.core.prepare_candles_for_simulation(
        pd.read_csv(FIXTURES / "F-TV-S1S5.csv"))
    res = rr.selfcheck(engine.core, prep, cfg=None, symbol="EURUSD")
    assert res["max_abs_diff"].get("ema", 0.0) == 0.0
    assert res["max_abs_diff"].get("adx", 0.0) == 0.0
    assert res["max_abs_diff"].get("pxVsEma", 0.0) == 0.0
    assert res["max_abs_diff"].get("bbw", 0.0) < 1e-9


# ══ UTC daily aggregation ════════════════════════════════════════════════════

def test_daily_aggregation_is_utc_calendar_not_session():
    """open=first, high=max, low=min, close=last, grouped on the UTC DATE.

    Not the exchange session and not the broker's 22:00 rollover — a bar at
    23:59 belongs to the day it is stamped, and one at 00:00 starts the next.
    """
    times = ["2026-01-05T00:00:00Z", "2026-01-05T12:00:00Z",
             "2026-01-05T23:45:00Z", "2026-01-06T00:00:00Z"]
    rows = rr.aggregate_daily(times, [1.0, 2.0, 3.0, 9.0], [5.0, 6.0, 4.0, 9.5],
                              [0.5, 1.5, 2.5, 8.5], [2.0, 3.0, 3.5, 9.2])
    assert [r["date"] for r in rows] == ["2026-01-05", "2026-01-06"]
    d0 = rows[0]
    assert d0["open"] == 1.0 and d0["close"] == 3.5
    assert d0["high"] == 6.0 and d0["low"] == 0.5
    assert d0["bars"] == 3
    assert rows[1]["bars"] == 1


def test_a_day_with_no_bars_is_absent_not_zero_filled():
    """`.dropna()` removes empty bins, so weekends simply do not exist in the
    daily series and the day index is NOT a calendar grid."""
    times = ["2026-01-09T20:00:00Z", "2026-01-12T00:00:00Z"]   # Fri -> Mon
    rows = rr.aggregate_daily(times, [1.0, 1.0], [1.0, 1.0], [1.0, 1.0],
                              [1.0, 1.0])
    assert [r["date"] for r in rows] == ["2026-01-09", "2026-01-12"]
    assert len(rows) == 2, "the weekend must not appear as empty days"


def test_non_finite_bars_are_skipped():
    times = ["2026-01-05T00:00:00Z", "2026-01-05T01:00:00Z"]
    rows = rr.aggregate_daily(times, [1.0, float("nan")], [1.0, 1.0],
                              [1.0, 1.0], [1.0, 1.0])
    assert rows[0]["bars"] == 1


# ══ EMA ══════════════════════════════════════════════════════════════════════

def test_ema_is_first_value_seeded_not_sma():
    """`ewm(adjust=False)`: y0 = x0. Pine's `ta.ema` seeds with an SMA of the
    first `length` values and produces a different series forever."""
    out = rr.ewm_adjust_false([10.0, 20.0, 30.0], 0.5)
    assert out == [10.0, 15.0, 22.5]


def test_ema_recurrence_matches_the_production_formula():
    vals = [1.0, 2.0, 3.0, 5.0, 8.0]
    alpha = 2.0 / (200 + 1)
    out = rr.ewm_adjust_false(vals, alpha)
    expect = vals[0]
    for v in vals[1:]:
        expect = (1 - alpha) * expect + alpha * v
    assert math.isclose(out[-1], expect, rel_tol=0, abs_tol=0)


def test_ema_passes_previous_value_through_a_gap():
    """A non-finite input does NOT restart the recurrence — the seed survives."""
    out = rr.ewm_adjust_false([10.0, float("nan"), 20.0], 0.5)
    assert out[1] == 10.0, "a gap must carry the previous output, not reseed"
    assert out[2] == 15.0


# ══ Bollinger / BBW ══════════════════════════════════════════════════════════

def test_standard_deviation_is_the_SAMPLE_deviation():
    """ddof=1, NOT the population deviation Pine's `ta.stdev` returns.

    At length 20 the two differ by sqrt(20/19) — about 2.6% — which is four
    orders of magnitude larger than the threshold's own margin, so substituting
    the built-in would move the volatility state, not merely round it.
    """
    vals = [1.0, 2.0, 3.0, 4.0]
    ma, sd = rr.rolling_mean_std(vals, 4)
    m = 2.5
    sample = math.sqrt(sum((x - m) ** 2 for x in vals) / 3)
    population = math.sqrt(sum((x - m) ** 2 for x in vals) / 4)
    assert ma[-1] == m
    assert math.isclose(sd[-1], sample, rel_tol=0, abs_tol=1e-15)
    assert not math.isclose(sd[-1], population, rel_tol=1e-9)


def test_rolling_window_is_none_until_full():
    ma, sd = rr.rolling_mean_std([1.0, 2.0, 3.0], 3)
    assert ma[0] is None and ma[1] is None and ma[2] == 2.0
    assert sd[0] is None and sd[1] is None


def test_bollinger_width_guards_a_zero_basis():
    assert rr.bollinger_width([0.0], [1.0], 2.0) == [None]
    assert rr.bollinger_width([None], [1.0], 2.0) == [None]
    got = rr.bollinger_width([2.0], [0.5], 2.0)[0]
    assert math.isclose(got, (2.0 * 2.0 * 0.5) / 2.0 * 100.0)


# ══ Wilder ADX ═══════════════════════════════════════════════════════════════

def test_directional_movement_requires_a_STRICTLY_larger_move():
    """+DM only when up > down AND up > 0. An equal up/down move yields
    NEITHER, and bar 0 has no predecessor so both are zero."""
    high = [10.0, 11.0, 12.0]
    low = [9.0, 8.0, 7.0]          # bar 1 moves up 1 and down 1 -> tie -> neither
    close = [9.5, 10.0, 11.0]
    out = rr.wilder_adx(high, low, close, 14)
    assert out["plus_dm"][0] == 0.0 and out["minus_dm"][0] == 0.0
    assert out["plus_dm"][1] == 0.0 and out["minus_dm"][1] == 0.0, "a tie is neither"
    assert out["tr"][0] == 1.0, "bar 0 true range is high-low"


def test_adx_smoothing_is_first_value_seeded():
    """Production reuses `ewm(adjust=False)` at alpha=1/length for TR, +DM, -DM
    and DX. Pine's `ta.adx` uses an RMA seeded from a sum and will not agree."""
    high = [10.0 + i for i in range(30)]
    low = [9.0 + i for i in range(30)]
    close = [9.5 + i for i in range(30)]
    out = rr.wilder_adx(high, low, close, 14)
    assert out["atr"][0] == out["tr"][0], "the smoothed series seeds on bar 0"


# ══ the six-state classifier ═════════════════════════════════════════════════

def test_price_exactly_on_the_ema_is_bear():
    """The operator is `px_vs_ema > 0`, so exactly zero is NOT Bull. This is
    reachable: on the EMA seed day close == ema and px_vs_ema is exactly 0."""
    s = rr.classify_state(0.0, 5.0, 30.0, 2.342, 18.0)
    assert s["trendState"] == "Bear"


def test_bbw_exactly_at_the_threshold_is_compress():
    """`bbw > threshold`, so equality is Compress, not Expand."""
    s = rr.classify_state(1.0, 2.342, 30.0, 2.342, 18.0)
    assert s["volatilityState"] == "Compress"
    assert rr.classify_state(1.0, 2.3420001, 30.0, 2.342, 18.0)[
        "volatilityState"] == "Expand"


def test_adx_exactly_at_the_chop_level_is_not_chop():
    """`adx < adx_chop`, so equality is Trend."""
    assert rr.classify_state(1.0, 5.0, 18.0, 2.342, 18.0)["chopState"] == "Trend"
    assert rr.classify_state(1.0, 5.0, 17.999, 2.342, 18.0)["chopState"] == "Chop"


def test_chop_overrides_the_state_name_but_not_the_volatility_field():
    """`Bull/Compress` becomes `Bull/Chop` while `volatilityState` still reads
    Compress — the two fields disagree ON PURPOSE and both are exported."""
    s = rr.classify_state(1.0, 1.0, 5.0, 2.342, 18.0)
    assert s["marketState"] == "Bull/Chop"
    assert s["volatilityState"] == "Compress"


def test_any_invalid_input_yields_no_state_at_all():
    """Production returns None rather than falling back to a plausible default.
    A silent 'range' fallback is exactly the failure this pins."""
    assert rr.classify_state(float("nan"), 5.0, 30.0, 2.342, 18.0) is None
    assert rr.classify_state(1.0, None, 30.0, 2.342, 18.0) is None
    assert rr.classify_state(1.0, 5.0, float("inf"), 2.342, 18.0) is None


def test_state_vocabulary_matches_production_exactly(engine):
    k = rr.constants(engine.core)
    assert set(k["MARKET_STATES"]) == {
        "Bull/Expand", "Bull/Compress", "Bull/Chop",
        "Bear/Expand", "Bear/Compress", "Bear/Chop"}
    assert k["DEFAULTS"]["emaLength"] == 200
    assert k["DEFAULTS"]["bbwLength"] == 20
    assert k["DEFAULTS"]["adxLength"] == 14
    assert k["DEFAULTS"]["adxChop"] == 18
    assert k["BBW_THRESHOLD_BY_SYMBOL"]["EURUSD"] == 2.342


# ══ the one-day shift ════════════════════════════════════════════════════════

def test_panel_row_uses_the_PREVIOUS_day_features(engine):
    """Leakage safety: the row FOR day D is computed from day D-1's close, and
    day 0 is null because it has no prior completed day."""
    import pandas as pd
    prep = engine.core.prepare_candles_for_simulation(
        pd.read_csv(FIXTURES / "F-TV-S1S5.csv"))
    times = [t.strftime("%Y-%m-%dT%H:%M:%SZ")
             for t in pd.to_datetime(prep["time"], utc=True)]
    daily, panel = rr.replay(engine.core, times, prep["open"].tolist(),
                             prep["high"].tolist(), prep["low"].tolist(),
                             prep["close"].tolist(), None, "EURUSD")
    assert panel[0]["warmup"] is True
    assert panel[0]["market_state"] is None
    for row in panel[1:]:
        assert row["shifted_days"] == 1
        assert row["source_day"] < row["date"], "features must precede the row"
    assert panel[5]["source_day"] == daily[4]["date"]


def test_float_noise_cannot_reach_the_threshold_decision(engine):
    """The measured replay-vs-production BBW difference is ~1.2e-10, while the
    closest any real day comes to the 2.342 threshold is 3.9e-4 — a margin of
    about 3.2 million. So an intermediate may differ slightly while the STATE
    CODE must still match exactly, which is why they carry different tolerances.
    """
    import pandas as pd
    prep = engine.core.prepare_candles_for_simulation(
        pd.read_csv(FIXTURES / "F-TV-S1S5.csv"))
    res = rr.selfcheck(engine.core, prep, cfg=None, symbol="EURUSD")
    assert res["ok"], "state codes must match exactly despite float noise"
    assert res["max_abs_diff"]["bbw"] > 0.0, (
        "if BBW became bit-exact this test's premise changed — re-measure the "
        "margin rather than deleting it")
    assert res["max_abs_diff"]["bbw"] < 1e-8


# ══ comparator ═══════════════════════════════════════════════════════════════

def _synth_s6(path, fixture="F-S6-TRANSITIONS", perturb=None, drop_s6=False):
    """Synthesize a TradingView export from the trace, S6 columns included."""
    import csv as _csv
    from tools.oracle.compare_stages import STAGE_FIELDS, latch_s5
    from tools.oracle.export_trace import build_trace
    t = build_trace(symbol="EURUSD", timeframe="15min",
                    input_path=FIXTURES / f"{fixture}.csv", fixture_id=fixture)
    latch_s5(t["bars"])
    probe = t["bars"][-1]
    stages = ("S1", "S2", "S3", "S4", "S5") if drop_s6 else \
        ("S1", "S2", "S3", "S4", "S5", "S6")
    cols = ["time", "open", "high", "low", "close"]
    for st in stages:
        cols += list(STAGE_FIELDS[st](probe).keys())
    cols = list(dict.fromkeys(cols))
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for i, b in enumerate(t["bars"]):
            row = {"time": b["bar_timestamp"][:19].replace(" ", "T") + "Z"}
            for f in ("open", "high", "low", "close"):
                row[f] = f"{b['ohlcv'][f]:.5f}"
            for st in stages:
                for f, (v, _k) in STAGE_FIELDS[st](b).items():
                    row[f] = "" if v is None else v
            if perturb:
                perturb(i, b, row)
            w.writerow({k: row.get(k, "") for k in cols})
    return t


def test_chart_bars_comparison_passes_S1_through_S6(tmp_path):
    """One export, six stages — the shape the operator will actually run."""
    from tools.oracle.compare_stages import compare
    p = tmp_path / "e.csv"
    _synth_s6(p)
    r = compare("F-S6-TRANSITIONS", p, feed="TEST:EURUSD",
                input_basis="chart_bars")
    for st in ("S1", "S2", "S3", "S4", "S5", "S6"):
        assert r["results"][st]["result"] == "PASS", (st, r["results"][st])


def test_missing_s6_columns_fail_closed(tmp_path):
    """An export predating the S6 plots must FAIL, not read as "nothing to see"."""
    from tools.oracle.compare_stages import CompareError, compare
    p = tmp_path / "e.csv"
    _synth_s6(p, drop_s6=True)
    with pytest.raises(CompareError, match="missing required S6 columns"):
        compare("F-S6-TRANSITIONS", p, input_basis="chart_bars")


def test_comparator_catches_a_corrupted_market_state_code(tmp_path):
    """A state CODE is a decision. No float tolerance may excuse it."""
    from tools.oracle.compare_stages import compare

    def flip(i, b, row):
        # `oracleRgState` travels inside the packed `oracleRgCodes` plot, so the
        # corruption is applied at the state field's own multiplier — which is
        # exactly the bit pattern a wrong classification would produce.
        from tools.oracle import session_codes as codes_mod
        mult = dict(zip([f for f, _w, _o in codes_mod.PACKED_SPEC["S6"]],
                        codes_mod.packed_multipliers("S6")))["oracleRgState"]
        if row.get("oracleRgCodes") not in ("", None) and i > 3000:
            row["oracleRgCodes"] = int(row["oracleRgCodes"]) + mult

    p = tmp_path / "e.csv"
    _synth_s6(p, perturb=flip)
    r = compare("F-S6-TRANSITIONS", p, input_basis="chart_bars")
    assert r["results"]["S6"]["result"] == "FAIL"


def test_comparator_catches_a_one_day_shift(tmp_path):
    """Dropping the one-day shift is the single most likely S6 defect: the
    values still look plausible, they are just a day early. Pinned via the
    exported source-day key."""
    from tools.oracle.compare_stages import compare

    def shift(i, b, row):
        if row.get("oracleRgSrcDay") not in ("", None, 0, "0"):
            row["oracleRgSrcDay"] = int(row["oracleRgSrcDay"]) + 1

    p = tmp_path / "e.csv"
    _synth_s6(p, perturb=shift)
    r = compare("F-S6-TRANSITIONS", p, input_basis="chart_bars")
    assert r["results"]["S6"]["result"] == "FAIL"


def test_comparator_catches_a_corrupted_bbw(tmp_path):
    """A BBW off by more than the declared band must fail even though the state
    code it produced happens to be unchanged — the intermediate is evidence too."""
    from tools.oracle.compare_stages import compare

    def bump(i, b, row):
        if row.get("oracleRgBbw") not in ("", None):
            row["oracleRgBbw"] = float(row["oracleRgBbw"]) + 0.01

    p = tmp_path / "e.csv"
    _synth_s6(p, perturb=bump)
    r = compare("F-S6-TRANSITIONS", p, input_basis="chart_bars")
    assert r["results"]["S6"]["result"] == "FAIL"


def test_s6_depends_only_on_S1():
    """S6 reads the bar series and nothing else. Declaring S2-S5 as
    prerequisites would make an unrelated structural failure invalidate a
    correct regime panel."""
    from tools.oracle.generate_pine import STAGE_DEPENDS_ON
    assert STAGE_DEPENDS_ON["S6"] == ["S1"]
    assert "S6" in STAGE_DEPENDS_ON["S7"], "S7 admits candidates under a state"


def test_no_stage_defaults_to_an_empty_dependency_list():
    """An undeclared stage makes the gate return OPEN on an empty list — the one
    answer a gate must never give by accident."""
    from tools.oracle.generate_pine import STAGE_DEPENDS_ON, STAGE_STATUS
    implemented = [s for s, v in STAGE_STATUS.items() if v != "UNIMPLEMENTED"]
    for st in implemented:
        assert st in STAGE_DEPENDS_ON, f"{st} is implemented but has no gate"
    nxt = "S7"
    assert STAGE_DEPENDS_ON.get(nxt), f"the {nxt} gate must not be empty"


# ══ the daily aggregation matches production's 1m-sourced panel ══════════════

def test_daily_aggregate_from_15m_equals_the_1m_sourced_panel(engine):
    """Production builds the panel from the 1-MINUTE frame; the chart has 15m
    bars. The daily aggregate is identical — max of maxes, min of mins, last of
    lasts, and the first 15m open IS the first 1m open of that day — but that is
    ASSERTED here rather than assumed, because everything downstream rests on it.
    """
    import pandas as pd

    from tools.oracle import replay_regime as rr
    m1 = engine.core.prepare_candles_for_simulation(
        pd.read_csv(FIXTURES / "F-TV-S1S5.csv"))
    det = engine.rb.resample_candles(m1, "15min")

    def daily_of(frame):
        stamps = [t.strftime("%Y-%m-%dT%H:%M:%SZ")
                  for t in pd.to_datetime(frame["time"], utc=True)]
        return rr.aggregate_daily(stamps, frame["open"].tolist(),
                                  frame["high"].tolist(), frame["low"].tolist(),
                                  frame["close"].tolist())

    a, b = daily_of(m1), daily_of(det)
    assert len(a) == len(b)
    for x, y in zip(a, b):
        assert x["date"] == y["date"]
        for f in ("open", "high", "low", "close"):
            assert x[f] == y[f], (x["date"], f, x[f], y[f])
