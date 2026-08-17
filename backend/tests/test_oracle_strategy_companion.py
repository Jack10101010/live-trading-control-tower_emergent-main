"""The Strategy Tester companion: modes, delay, order type, cost.

WHY THIS MODULE EXISTS SEPARATELY FROM THE ORACLE'S TESTS. The companion is the
first build in this project that can place an order, and an order is the one
artefact whose defects are expensive rather than merely wrong. Three of them are
already known by name here, because each was made once:

  * ARM == FILL. Filling on the bar the arm was detected on ignores production's
    3-minute delay and back-dates the entry into a bar that has already closed.
  * A SECOND POSITION. `strategy.entry` with an id that has already filled does
    not replace an order; it opens another one, six times over at
    `pyramiding = 6`.
  * THE WRONG ORDER TYPE. A buy limit submitted while the market is already
    below it fills at the next bar's OPEN, not at the block edge — measured at a
    median of 0.44R away and up to 4.46R.

The lifecycle is TRANSCRIBED and RUN, as in `test_oracle_live_lifecycle.py`,
rather than asserted about with substrings; `test_the_transcription_still_
matches_the_pine_source` fails if the Python and the Pine drift apart.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

pd = pytest.importorskip("pandas")

from tools.oracle import companion_backtest as cb  # noqa: E402
from tools.oracle import lint_pine  # noqa: E402
from tools.oracle.generate_pine import BUILD_TARGETS  # noqa: E402

SRC = CT_ROOT / "pine" / "src"
GEN = (CT_ROOT / "pine" / "generated"
       / "tradingview_strategy_companion_15m.pine")

#: A block from 1.1000 to 1.0900, long. Entry 1.1000, arm 1.0975 (25%),
#: stop 1.0899 with a 1-pip buffer, so risk is 101 pips and 1R is 0.0101.
TOP, BOTTOM, BUF, TRIG = 1.1000, 1.0900, 0.0001, 25.0


def frames(rows):
    """`rows` is [(open, high, low, close), ...] on a 15-minute grid."""
    t = pd.date_range("2026-06-23", periods=len(rows), freq="15min", tz="UTC")
    return pd.DataFrame(rows, columns=["open", "high", "low", "close"], index=t)


def block(is_long=True, born=0, top=TOP, bottom=BOTTOM, is_choch=False):
    return [{"id": "OB1", "top": top, "bottom": bottom, "is_long": is_long,
             "is_choch": is_choch, "born": born}]


def always(rr=2.0, ok=True, news_ok=True):
    return lambda ts, s: (rr, ok, "london", "Bull/Expand", news_ok, True)


def run(mode, rows, obs=None, cell=None):
    return cb.simulate(mode, obs or block(), frames(rows), cell or always(),
                       BUF, TRIG, None)


# ── the delay ────────────────────────────────────────────────────────────────

def test_no_fill_on_the_bar_the_arm_was_detected():
    """The arm bar has already closed by the time the script can see it.

    Bar 1 dips to the arm level AND spans the entry edge: production, on
    1-minute candles, could well have filled inside it. This build cannot, in
    either mode, and the point of the test is that it does not pretend to.
    """
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),      # 0: born, above the block
            (1.1005, 1.1005, 1.0970, 1.0980),      # 1: arms AND spans entry
            (1.0980, 1.0985, 1.0975, 1.0980)]      # 2: nothing
    for mode in (cb.STRICT, cb.PRACTICAL):
        n, trades, _ = run(mode, rows)
        assert n["armed"] == 1
        assert n["filled"] == 0, "filled inside the arm bar — that is arm==fill"
        assert n["delayed"] == 1, "an arm bar that spanned the entry must be counted"
        assert trades == []


ARMS_CLEANLY = (1.0995, 1.0998, 1.0970, 1.0980)
"""Reaches the arm level (1.0975) WITHOUT reaching the entry edge (1.1000).

Production could not have filled inside this bar either, so the fill lies in a
later bar for production and for this build alike — which is the condition
under which STRICT is willing to trade at all.
"""


def test_the_order_becomes_live_on_the_very_next_bar():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,                          # 1: arms, order placed
            (1.0980, 1.1005, 1.0975, 1.1000)]      # 2: revisits the edge
    n, _t, open_pos = run(cb.PRACTICAL, rows)
    assert n["orders"] == 1
    assert n["filled"] == 1
    assert open_pos[0]["fill"] == pytest.approx(TOP), \
        "the fill must be AT the block edge, not at a bar open"


# ── the order type ───────────────────────────────────────────────────────────

def test_a_long_below_the_entry_uses_a_stop_not_a_limit():
    """The market is below the edge at placement, so only a STOP is valid.

    A limit here is not merely the wrong word: TradingView would treat it as
    marketable and fill at the next bar's open.
    """
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY]                          # closes BELOW the entry
    live = _armed_setup(rows)
    assert live.order[0] == "stop"
    assert live.order[1] == pytest.approx(TOP)


def test_a_long_above_the_entry_uses_a_limit():
    """Reachable only via a closed gate, and worth reaching.

    While the cohort said BLOCK there was no order to fill, so price could climb
    back above the edge. When the gate reopens the market is ABOVE the entry and
    a stop order would be rejected outright.
    """
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,                          # arms while BLOCKED
            (1.0980, 1.1030, 1.0975, 1.1020)]      # climbs back above the edge

    def gate(ts, s):
        ok = ts > pd.Timestamp("2026-06-23 00:15", tz="UTC")
        return 2.0, ok, "london", "Bull/Expand", True, True

    live = _armed_setup(rows, cell=gate)
    assert live.order[0] == "limit"


def _armed_setup(rows, cell=None):
    """Run the pass and hand back the one tracked setup, order intact."""
    captured = []
    real = cb.Setup

    class Spy(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            captured.append(self)

    cb.Setup = Spy
    try:
        run(cb.PRACTICAL, rows, cell=cell)
    finally:
        cb.Setup = real
    assert len(captured) == 1
    assert captured[0].order is not None, "never armed — the fixture is wrong"
    return captured[0]


def test_a_gap_through_the_level_fills_at_the_open_and_is_counted():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,                          # arms; stop order at 1.1000
            (1.1030, 1.1040, 1.1025, 1.1035)]      # opens ABOVE the stop level
    n, _t, open_pos = run(cb.PRACTICAL, rows)
    assert n["filled"] == 1
    assert n["gap_fill"] == 1
    assert open_pos[0]["fill"] == pytest.approx(1.1030), \
        "a gapped stop fills at the open, not at the untraded stop price"


# ── the two modes ────────────────────────────────────────────────────────────

UNPROVABLE = [(1.1010, 1.1015, 1.1005, 1.1010),
              ARMS_CLEANLY,
              # This bar BOTH spans the entry and breaks the far edge, and no
              # order was live on it (the gate below was shut), so production's
              # fill and production's invalidation are both inside it.
              (1.0980, 1.1005, 1.0890, 1.0950)]


def _shut_after(when):
    def gate(ts, s):
        return 2.0, ts < pd.Timestamp(when, tz="UTC"), "l", "Bull/Expand", 1, 1
    return gate


def test_neither_mode_trades_an_unprovable_bar():
    """The honest answer, and NOT the one the two-mode design first assumed.

    TradingView matches orders against a bar BEFORE the script runs at that
    bar's close, so a working order on such a bar has already filled and no mode
    can decline it after the fact. What both modes can do is refuse to START —
    and that is what this asserts, identically for both, because the truth is
    identical for both. A mode that claimed to filter this would be claiming a
    power the platform does not give it.
    """
    for mode in (cb.STRICT, cb.PRACTICAL):
        n, trades, open_pos = run(mode, UNPROVABLE,
                                  cell=_shut_after("2026-06-23 00:15"))
        assert n["undecidable"] == 1
        assert trades == [] and open_pos == []


def test_strict_declines_a_setup_whose_arm_bar_already_reached_the_entry():
    """THE ONE PLACE THE MODES DIFFER, and the reason there are two of them.

    Production, on 1-minute candles, may have filled inside that arm bar — 61%
    of recorded fills did. This build cannot; its order waits for a later bar to
    revisit the edge, at the same price but a different hour, hence a different
    session, market state and possibly matrix cell. STRICT will not call that
    production's trade. PRACTICAL takes it and flags it.
    """
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            (1.1005, 1.1005, 1.0970, 1.0980),      # arms AND spans the entry
            (1.0980, 1.1005, 1.0975, 1.1000),      # revisits the edge
            (1.1000, 1.1210, 1.0995, 1.1200)]      # runs to 2R
    s_n, s_t, _ = run(cb.STRICT, rows)
    p_n, p_t, _ = run(cb.PRACTICAL, rows)

    assert s_n["delayed"] == p_n["delayed"] == 1
    assert s_n["omitted"] == 1 and s_t == []
    assert p_n["emulator"] == 1 and len(p_t) == 1
    assert p_t[0]["emu_dependent"] is True, (
        "a trade whose fill time is the emulator's must say so on the trade")


def test_the_modes_agree_wherever_the_arm_bar_did_not_reach_the_entry():
    """Everything else must be identical, or no per-mode number means anything.

    If the arm bar never touched the edge, production could not have filled
    inside it either — so both modes are looking at the same later fill, and any
    divergence here would be a defect rather than a policy.
    """
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,
            (1.0980, 1.1005, 1.0975, 1.1000),      # fills at the edge
            (1.1000, 1.1210, 1.0995, 1.1200)]      # runs to 2R
    a_n, a_t, _ = run(cb.STRICT, rows)
    b_n, b_t, _ = run(cb.PRACTICAL, rows)
    assert a_n["delayed"] == 0 and a_n["omitted"] == 0
    assert a_t == b_t
    assert [t["outcome"] for t in a_t] == ["WIN"]


def test_invalidation_is_not_undecidable():
    """Through the far edge WITHOUT touching the entry is provable, and both
    modes must call it invalidated rather than hedge."""
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,
            (1.0980, 1.0985, 1.0890, 1.0895)]      # never reaches 1.1000
    for mode in (cb.STRICT, cb.PRACTICAL):
        n, trades, _ = run(mode, rows)
        assert n["invalidated"] == 1
        assert n["undecidable"] == 0
        assert trades == []


# ── the gates are read at the ORDER bar, not at detection ────────────────────

def test_a_blocked_cell_cancels_a_working_order():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,                          # arms, order placed
            (1.0980, 1.0985, 1.0975, 1.0980),      # gate closes here
            (1.0980, 1.1005, 1.0975, 1.1000)]      # would have filled

    def gate(ts, s):
        ok = ts < pd.Timestamp("2026-06-23 00:30", tz="UTC")
        return 2.0, ok, "london", "Bull/Expand", True, True

    n, trades, open_pos = run(cb.PRACTICAL, rows, cell=gate)
    assert n["blocked"] == 1
    assert trades == [] and open_pos == []


def test_news_blackout_cancels_a_working_order():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,
            (1.0980, 1.0985, 1.0975, 1.0980),
            (1.0980, 1.1005, 1.0975, 1.1000)]

    def gate(ts, s):
        ok = ts < pd.Timestamp("2026-06-23 00:30", tz="UTC")
        return 2.0, True, "london", "Bull/Expand", ok, True

    n, trades, _ = run(cb.PRACTICAL, rows, cell=gate)
    assert n["news_blocked"] == 1
    assert trades == []


# ── the cost model ───────────────────────────────────────────────────────────

def test_cost_reconciles_with_production_on_S_2094():
    """The one number the whole cost model is pinned to.

    Production recorded `total_cost_r` = 0.0698 for setup S_2094, whose risk was
    8.6 pips. Cash-per-contract at 0.3 pip a side, against a quantity derived
    from the stop distance, must reproduce it — and must do so WITHOUT being
    tuned to, which is why the arithmetic is spelled out rather than asserted
    against a stored constant.
    """
    risk = 8.6 * 0.0001
    cost_r = 2 * cb.COMMISSION_PER_SIDE / risk
    assert cost_r == pytest.approx(0.0698, abs=5e-5)


@pytest.mark.parametrize("risk_pips", [5.0, 8.6, 15.0, 30.0, 60.0])
def test_cost_is_the_same_fraction_of_R_at_every_stop_distance(risk_pips):
    """WHY COMMISSION AND NOT SLIPPAGE. A tick slippage is a fixed PRICE, so it
    is 7% of R on an 8.6-pip stop and 2% on a 30-pip one. Production books a
    fixed fraction. Only the cash-per-contract form has that property, and only
    because quantity is derived from the stop distance."""
    risk = risk_pips * 0.0001
    qty = 1000.0 / risk                       # `f_qty`, at 1000 per trade
    cash = 2 * cb.COMMISSION_PER_SIDE * qty   # what TradingView charges
    assert cash / 1000.0 == pytest.approx(2 * cb.COMMISSION_PER_SIDE / risk)
    assert cash / 1000.0 == pytest.approx(0.6 / risk_pips, rel=1e-9)


def test_the_cost_is_deducted_from_every_trade():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,
            (1.0980, 1.1005, 1.0975, 1.1000),
            (1.1000, 1.1210, 1.0995, 1.1200)]
    _n, trades, _ = run(cb.PRACTICAL, rows)
    t = trades[0]
    assert t["gross_r"] == 2.0
    assert t["cost_r"] > 0
    assert t["net_r"] == pytest.approx(t["gross_r"] - t["cost_r"])


# ── TradingView's intrabar assumption ────────────────────────────────────────

def test_an_up_bar_is_assumed_to_have_moved_open_low_high_close():
    assert cb.four_price_path(1.0, 1.2, 0.9, 1.1) == [1.0, 0.9, 1.2, 1.1]
    assert cb.four_price_path(1.1, 1.2, 0.9, 1.0) == [1.1, 1.2, 0.9, 1.0]


def test_a_bar_holding_both_stop_and_target_is_counted_as_emulator_resolved():
    rows = [(1.1010, 1.1015, 1.1005, 1.1010),
            ARMS_CLEANLY,
            (1.0980, 1.1005, 1.0975, 1.1000),      # fills at 1.1000
            # up bar: low first, so the stop is met before the target
            (1.1000, 1.1210, 1.0890, 1.1200)]
    n, trades, _ = run(cb.PRACTICAL, rows)
    assert n["ambiguous_exit"] >= 1
    assert trades[0]["outcome"] == "LOSS"
    assert trades[0]["ambiguous"] is True


# ── the build target ─────────────────────────────────────────────────────────

def test_the_companion_is_declared_as_a_strategy():
    decl = BUILD_TARGETS["strategy_companion"]["declaration"]
    assert decl and decl.startswith('strategy(')


def test_the_companion_owns_no_stages():
    """It reuses the detection build's fragments, so its parity question is
    already answered there. A second claim would be one measurement with two
    homes and no way to say which is authoritative."""
    assert BUILD_TARGETS["strategy_companion"]["stages"] == ()


def test_every_target_declares_what_it_embeds():
    for name, spec in BUILD_TARGETS.items():
        assert "embed" in spec, f"{name} does not declare its embedded blocks"


def test_the_companion_embeds_the_target_table_and_the_news_schedule():
    """Both are FILL-TIME authority. Without the table every cell falls back to
    the global RR — a plausible number, and the wrong one."""
    assert set(BUILD_TARGETS["strategy_companion"]["embed"]) == {"targets",
                                                                 "news"}


@pytest.mark.skipif(not GEN.is_file(), reason="companion not generated")
def test_the_generated_declaration_carries_the_position_and_cost_rails():
    src = GEN.read_text(encoding="utf-8")
    head = src.split("ORACLE_SOURCE_HASH_SHORT")[0]
    assert "pyramiding = 6" in head, \
        "production's rail is max_open_positions = 6, not the observed max of 2"
    assert "slippage = 0" in head
    assert "commission_type = strategy.commission.cash_per_contract" in head
    assert "commission_value = 0.00003" in head
    assert "process_orders_on_close = false" in head
    decl = [l for l in head.splitlines()
            if l.startswith(("indicator(", "strategy("))]
    assert len(decl) == 1 and decl[0].startswith("strategy("), (
        f"the declaration line is not a strategy(): {decl}")


@pytest.mark.skipif(not GEN.is_file(), reason="companion not generated")
def test_the_generated_build_states_its_limitation_where_the_numbers_are():
    src = GEN.read_text(encoding="utf-8")
    assert "APPROXIMATE" in src
    assert "production executes on 1m" in src


# ── the linter's own rules ───────────────────────────────────────────────────

def _companion_stub(**over):
    d = {"target": "strategy_companion", "slippage": "0",
         "commission": "strategy.commission.cash_per_contract",
         "pooc": "false"}
    d.update(over)
    return (f"//@version=6\n"
            f"// Build target      : {d['target']}  (x)\n"
            f"// DO NOT EDIT\n"
            f'strategy("t", "t", slippage = {d["slippage"]},\n'
            f"     commission_type = {d['commission']},\n"
            f"     process_orders_on_close = {d['pooc']})\n"
            f"// APPROXIMATE\n"
            f"// production executes on 1m; this is 15m\n")


def _rules(src):
    return {f["rule"] for f in lint_pine.lint(src)}


def test_the_linter_accepts_a_correct_companion():
    assert "cost_double_counted" not in _rules(_companion_stub())
    assert "arm_equals_fill" not in _rules(_companion_stub())
    assert "declaration" not in _rules(_companion_stub())


def test_the_linter_rejects_slippage_on_top_of_the_commission():
    assert "cost_double_counted" in _rules(_companion_stub(slippage="3"))


def test_the_linter_rejects_process_orders_on_close():
    assert "arm_equals_fill" in _rules(_companion_stub(pooc="true"))


def test_the_linter_rejects_an_undisclosed_approximation():
    src = _companion_stub().replace("// APPROXIMATE\n", "")
    assert "undisclosed_approximation" in _rules(src)


def test_input_time_uses_the_const_form_of_timestamp():
    """CE10123, and the reason the wall-clock rule is scoped rather than obeyed.

    `input.time` requires a CONST int. `timestamp("UTC", 2026, 6, 23, 0, 0)` is
    a SIMPLE int and TradingView refuses it; only the single ISO-string form
    folds to a constant. Writing it the other way to keep a text-level lint rule
    quiet cost a compile, which is the wrong side of that trade — the rule was
    scoped instead, and the two tests below hold both halves of that.
    """
    inputs = (SRC / "s10_inputs.pinefrag").read_text(encoding="utf-8")
    assert 'input.time(timestamp("2026-06-23T00:00:00+0000")' in inputs
    assert 'input.time(timestamp("2026-08-01T00:00:00+0000")' in inputs
    # CODE lines only. The comment beside those inputs names the broken form on
    # purpose — a warning that reads "do not write X" would otherwise trip a
    # check looking for X, and the fix would be to delete the warning.
    code = [l for l in inputs.splitlines() if not l.lstrip().startswith("//")]
    assert not [l for l in code if 'timestamp("UTC",' in l]


def test_the_linter_rejects_the_simple_int_form_of_input_time():
    src = _companion_stub() + (
        '    t = input.time(timestamp("UTC", 2026, 6, 23, 0, 0))' + chr(10))
    assert "input_time_not_const" in _rules(src)


def test_the_linter_accepts_the_const_form_of_input_time():
    """The first version of this rule fired on the FIX as well as the defect,
    because its argument capture stopped at the first `)`. A rule that flags the
    correct code is worse than no rule — it trains you to ignore it."""
    src = _companion_stub() + (
        '    t = input.time(timestamp("2026-06-23T00:00:00+0000"))' + chr(10))
    assert "input_time_not_const" not in _rules(src)


def test_a_date_inside_timestamp_is_not_a_wallclock_stamp():
    src = _companion_stub() + '    t = input.time(timestamp("2026-06-23T00:00:00+0000"))' + chr(10)
    assert "wallclock_stamp" not in _rules(src)


def test_a_wallclock_stamp_anywhere_else_is_still_caught():
    """The guard must survive the scoping, or regeneration stops being
    byte-identical and the tamper check quietly means nothing."""
    src = _companion_stub() + "// Generated at : 2026-08-11T13:45" + chr(10)
    assert "wallclock_stamp" in _rules(src)


def test_the_linter_catches_a_bare_literal_statement():
    """CE10197, which the linter could not see until it did.

    The structural rules run against a view with string literals BLANKED, so a
    line whose whole content is a literal had already become an empty line
    before any rule looked at it — invisible here, and the compiler's first
    complaint. It is the residue a deleted line leaves behind.
    """
    src = _companion_stub() + "    ''" + "\n"
    assert "bare_literal" in _rules(src)


def test_a_pure_literal_continuation_line_is_still_legal():
    """The rule must not fire on this, or every wrapped string breaks."""
    src = _companion_stub() + '    x = "a" +' + "\n" + '        "b"' + "\n"
    assert "bare_literal" not in _rules(src)


def test_the_linter_still_forbids_orders_in_an_oracle_build():
    """The oracle rule must be SCOPED, not deleted — a companion that can place
    orders is not a licence for the oracle to."""
    src = ("//@version=6\n"
           "// Build target      : detection_15m  (x)\n"
           "// DO NOT EDIT\n"
           'indicator("t", "t")\n'
           "strategy.entry(\"x\", strategy.long)\n")
    assert "not_a_strategy" in _rules(src)


def test_the_linter_rejects_an_indicator_declared_under_a_strategy_target():
    src = _companion_stub().replace('strategy("t", "t",', 'indicator("t", "t",')
    assert "declaration" in _rules(src)


# ── the named divergence, kept rather than patched away ──────────────────────

def test_the_2026_07_21_block_is_the_documented_intrabar_divergence():
    """OB detected 2026-07-21 18:00, short, entry 1.14242, stop 1.14296.

    Production armed it at 02:42 on the 23rd and then CANCELLED it:
    `invalidated_before_edge_entry` — on 1-minute candles the far edge broke
    BEFORE price returned to the entry. On 15-minute bars both events live in
    one candle, TradingView matches the resting order against that candle
    before the script is evaluated at its close, and the trade happens. It cost
    -1.111R, which is the entire difference between PRACTICAL's -2.393R and
    production's -1.282R over the validation window.

    THIS TEST EXISTS TO KEEP IT. It is the cleanest available specimen of what
    a 15-minute chart cannot know, and a build that quietly stopped producing it
    would have stopped being honest about that — so the assertion is that the
    trade still happens AND still carries its flag, not that it goes away.
    """
    top, bottom = 1.14296 - BUF, 1.14242          # short: entry is the bottom
    depth = top - bottom
    arm = bottom + depth * TRIG / 100.0
    # A bearish block is approached from BELOW: price rises to the bottom edge
    # (the entry), penetrates 25% upward to the arm, and is invalidated above
    # the top. Getting that the wrong way round makes the arm bar read as an
    # invalidation and the fixture silently tests nothing.
    assert bottom < arm < top
    rows = [(1.14230, 1.14235, 1.14225, 1.14230),                 # 0: born, below
            (1.14248, 1.14260, 1.14246, 1.14250),                 # 1: arms only
            # 2: reaches the entry edge AND breaks the far edge. Production saw
            # the break first; this chart cannot see either one first.
            (1.14250, 1.14300, 1.14238, 1.14290)]
    obs = [{"id": "OB1", "top": top, "bottom": bottom, "is_long": False,
            "is_choch": False, "born": 0}]
    n, trades, open_pos = run(cb.PRACTICAL, rows, obs=obs)

    assert n["filled"] == 1, "the divergent fill must still occur"
    assert n["fill_bar_also_broke"] == 1, (
        "the fill bar broke the block and the counter must say so — this is "
        "the only signal a reader gets that production may have cancelled")
    live = (trades + open_pos)
    assert live, "the trade vanished; the fixture no longer reproduces the case"


# ── the diagnostics panel's shape ────────────────────────────────────────────

PANEL_COUNTERS = (
    # full-history half
    "detected", "armed", "invalidated", "undecidable", "expired unfilled",
    "delayed_because_M15", "omitted_because_unprovable",
    # order-window half
    "orders placed", "orders filled", "closed trades", "cohort blocked",
    "news blocked", "news_unknown", "emulator_dependent",
    "fill_bar_also_broke_block", "emulator_ordered_exit",
    "opposite_direction_overlap",
)


def _panel():
    body = (SRC / "s90_strategy.pinefrag").read_text(encoding="utf-8")
    return body[body.index("if barstate.islast and i_showPanel"):]


def test_the_panel_still_carries_every_counter():
    """The panel was reflowed to fit the pane, not trimmed to fit it."""
    panel = _panel()
    missing = [c for c in PANEL_COUNTERS if f'"{c}"' not in panel]
    assert not missing, f"counters dropped from the panel: {missing}"


def test_the_panel_labels_which_counters_are_window_scoped():
    """`detected` reading 137 next to `orders placed` 12 is not a fault — the
    two are counted over different populations. The panel has to say so, or the
    next reader repeats the same investigation."""
    panel = _panel()
    assert '"FULL HISTORY"' in panel
    assert '"ORDER WINDOW"' in panel


def test_every_panel_row_index_is_a_literal():
    """FIXED LAYOUT. A row index that is a variable can only be checked by a
    heuristic, and a heuristic on an overflow guard is a liability — it under-
    fired once already. Literals make the maximum reachable row a fact."""
    panel = _panel()
    for call in re.findall(r"f_scCell\(\s*([^,]+),", panel):
        assert call.strip().isdigit(), f"non-literal panel row: {call!r}"
    for call in re.findall(r"table\.cell\(sc_panel,\s*[^,]+,\s*([^,]+),",
                           panel):
        assert call.strip().isdigit(), f"non-literal panel row: {call!r}"


def test_the_panel_fits_the_table_it_declares():
    body = (SRC / "s90_strategy.pinefrag").read_text(encoding="utf-8")
    decl = re.search(r"table\.new\(f_panelPos\(\),\s*(\d+),\s*(\d+)", body)
    cols, rows = int(decl.group(1)), int(decl.group(2))
    panel = _panel()
    used_rows = [int(m) for m in re.findall(r"f_scCell\(\s*(\d+),", panel)]
    used_rows += [int(m) for m in re.findall(
        r"table\.cell\(sc_panel,\s*\d+,\s*(\d+),", panel)]
    used_cols = [int(m) for m in re.findall(r"f_scCell\(\s*\d+,\s*(\d+),", panel)]
    used_cols += [int(m) for m in re.findall(
        r"table\.cell\(sc_panel,\s*(\d+),", panel)]
    assert max(used_rows) < rows, f"row {max(used_rows)} into {rows} rows"
    assert max(used_cols) + 1 <= cols, f"col {max(used_cols)} into {cols} cols"
    # and it must actually be short enough to fit a pane with the Strategy
    # Tester open, which is what the 24-row version failed at
    assert rows <= 16, f"{rows} rows will clip on a short pane"


def test_the_state_name_never_indexes_an_array_at_minus_one():
    """`s6_stateCode == 0 ? … : array.get(arr, s6_stateCode - 1)` computes -1
    in the untaken branch, and Pine does not reliably decline to evaluate it."""
    body = (SRC / "s90_strategy.pinefrag").read_text(encoding="utf-8")
    assert "f_stateName() =>" in body
    assert "? \"(warming up)\" :" not in body


# ── the linter's own overflow guard ──────────────────────────────────────────

def test_the_row_overflow_rule_reads_a_single_line_declaration():
    """It could not. `re.split(r\",(?![^(]*\\))\", …)` refused to split
    `a, 2, 3)` at all, so any table declared on one line was never registered
    and the check passed by never having looked."""
    src = ("//@version=6" + chr(10) + "// Build target      : detection_15m  (x)"
           + chr(10) + "// DO NOT EDIT" + chr(10) + 'indicator("t","t")' + chr(10)
           + "var table t = table.new(position.top_right, 2, 3)" + chr(10))
    for i in range(9):
        src += f'table.cell(t, 0, {i}, "x")' + chr(10)
    assert "table_row_overflow" in _rules(src)


def test_the_row_overflow_rule_counts_rows_not_cell_writes():
    """Four columns write two cells per row. A rule counting writes reports
    twice the height and demands twice the capacity."""
    src = ("//@version=6" + chr(10) + "// Build target      : detection_15m  (x)"
           + chr(10) + "// DO NOT EDIT" + chr(10) + 'indicator("t","t")' + chr(10)
           + "var table t = table.new(position.top_right, 4, 13)" + chr(10))
    for i in range(13):
        src += f'table.cell(t, 0, {i}, "x")' + chr(10)
        src += f'table.cell(t, 2, {i}, "y")' + chr(10)
    assert "table_row_overflow" not in _rules(src)


# ── drift ────────────────────────────────────────────────────────────────────

def test_the_transcription_still_matches_the_pine_source():
    """The Python above is a MODEL of the Pine. These lines are the load-bearing
    ones; if any is edited, the model is re-derived rather than assumed."""
    body = (SRC / "s90_strategy.pinefrag").read_text(encoding="utf-8")
    for needle in (
            "hitArm = o.isLong ? low <= o.arm : high >= o.arm",
            "through = o.isLong ? low < o.bottom : high > o.top",
            "straddle = low <= o.entry and high >= o.entry",
            "if o.working and f_isFilled(o.id)",
            "useStop = o.isLong ? close < o.entry : close > o.entry",
            "limit = useStop ? na : o.entry",
            "stop  = useStop ? o.entry : na",
            "if o.armBarStraddled",
            "if f_strict() and o.armBarStraddled",
            "inWindow = not i_useWindow or (time >= i_windowFrom and time < i_windowTo)",
            "canFill = armed and inWindow",
            "n_emuExit += 1",
            "if f_strict()",
            "n_omitted += 1",
            "o.emuDependent := true",
    ):
        assert needle in body, f"the Pine no longer contains: {needle}"


def test_the_panel_names_every_counter_the_brief_requires():
    """Seven counters, by name, on the same surface as the metrics.

    A Strategy Tester headline travels without its caveats unless the caveat is
    rendered beside it, so these are asserted on the PANEL, not in a comment.
    """
    body = (SRC / "s90_strategy.pinefrag").read_text(encoding="utf-8")
    panel = body.split("the diagnostics panel")[1]
    for name in ("emulator_dependent", "fill_bar_also_broke_block",
                 "emulator_ordered_exit", "delayed_because_M15",
                 "omitted_because_unprovable", "opposite_direction_overlap",
                 "news_unknown"):
        assert name in panel, f"the panel does not render {name}"


def test_strict_is_documented_as_a_diagnostic_and_not_tuned_for_activity():
    """It produces zero trades on the validation window and that is the answer.

    The temptation is a heuristic that makes STRICT more active; the decision
    was explicitly against one, because an instrument tuned to read better stops
    measuring. This pins the intent next to the code.
    """
    inputs = (SRC / "s10_inputs.pinefrag").read_text(encoding="utf-8")
    assert "A DIAGNOSTIC, NOT A BACKTEST" in inputs
    assert "THE MODE TO ACTUALLY BACKTEST WITH" in inputs


def test_the_two_modes_are_the_only_two_and_neither_claims_parity():
    inputs = (SRC / "s10_inputs.pinefrag").read_text(encoding="utf-8")
    assert 'options = ["STRICT / PROVABLE", "PRACTICAL / TV EMULATOR"]' in inputs
    assert cb.STRICT in inputs and cb.PRACTICAL in inputs
    # The word must not appear as a MODE NAME anywhere. M15 cannot reproduce
    # all M1 timing, and a mode called parity would be the one claim this
    # project exists to avoid.
    assert '"PARITY"' not in inputs
