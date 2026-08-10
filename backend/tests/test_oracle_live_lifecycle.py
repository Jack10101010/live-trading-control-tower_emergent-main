"""The live order-block lifecycle: opportunity, trigger, outcome.

Two things this module is built around.

FIRST — the semantic. Production does not decide a trade when it detects an
order block. `strategy_core/execution.py` reads the session (2882), the cohort
cell (2950), the market state (2965) and rewrites the target (3008) at the FILL
candle. A block that rests from Monday to Thursday is evaluated against
Thursday's context, not Monday's. So a resting block's preview is provisional,
and a resolved block's context is frozen.

SECOND — the method. The state machine is TRANSCRIBED into Python and RUN, not
described with substring assertions. The last lifecycle defect (a box that never
stopped growing) survived a test that only checked the source contained the
right words; the shape of that bug was invisible when read and obvious when run.
`test_the_transcription_still_matches_the_pine_source` fails if the two drift.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

SRC = CT_ROOT / "pine" / "src"
GEN = CT_ROOT / "pine" / "generated" / "tradingview_visual_oracle_detection_15m.pine"

RESTING, ARMED, TRADE, NOTRADE, WIN, LOSS, INVALID, UNDECID = range(8)
NEWS_OK, NEWS_BLOCKED, NEWS_UNKNOWN = 0, 1, 2

BLUE, ORANGE, GREEN, RED, GREY, PURPLE, CYAN = (
    "BLUE", "ORANGE", "GREEN", "RED", "GREY", "PURPLE", "CYAN")
#: ARMED is BLUE on purpose: production has an armed order and no fill, which is
#: still an opportunity, not a trade. The label says ARMED.
PHASE_COLOUR = {RESTING: BLUE, ARMED: BLUE, TRADE: ORANGE, WIN: GREEN,
                LOSS: RED, NOTRADE: GREY, INVALID: PURPLE, UNDECID: CYAN}
LIVE_PHASES = (RESTING, ARMED)


def _frag(name: str) -> str:
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


class Block:
    """One `LiveOb`."""

    def __init__(self, top, bottom, is_long, born=0, trigger_pct=25.0,
                 entry_pct=0.0, stop_buffer=0.0001):
        depth = top - bottom
        self.top, self.bottom, self.is_long = top, bottom, is_long
        self.entry = (top - depth * entry_pct / 100.0 if is_long
                      else bottom + depth * entry_pct / 100.0)
        self.arm = (top - depth * trigger_pct / 100.0 if is_long
                    else bottom + depth * trigger_pct / 100.0)
        self.stop = bottom - stop_buffer if is_long else top + stop_buffer
        self.target = None
        self.phase = RESTING
        self.born, self.resolved_bar, self.armed_bar = born, None, None
        self.arm_bar_straddled = False
        self.arm_cell = None
        # Mirrors the Pine defaults: finalRr na, finalOk true, finalNews UNKNOWN.
        self.final = {"rr": None, "ok": True, "news": NEWS_UNKNOWN, "why": "",
                      "sess": "", "state": ""}
        self.right = born
        self.rr_visible = False
        self.rr_area = False
        self.rr_provisional = True
        self.rr_muted = False
        self.painted = 0


def run(block, bars, ctx, tail=4, last_bar=None):
    """The per-bar pass of `68_live_setups.pinefrag`, transcribed.

    `bars` is [(high, low), …]; `ctx(i)` returns the bar's execution context as
    (rr, allowed, news) — the values `f_resolveCell` / `f_applyOverrides` /
    `f_newsState` produce for that bar.
    """
    last_bar = len(bars) - 1 if last_bar is None else last_bar
    o = block
    for i, (hi, lo) in enumerate(bars):
        at_right = i >= last_bar - 1

        # 1. LIFECYCLE — resting -> ARM -> (proven fill | invalidated)
        was_live = o.phase in LIVE_PHASES
        just_armed = just_filled = just_resolved = False
        if was_live:
            if i > o.born:
                armed = lo <= o.arm if o.is_long else hi >= o.arm
                through = lo < o.bottom if o.is_long else hi > o.top
                straddle = lo <= o.entry <= hi
                if through:
                    just_resolved = True
                    o.resolved_bar = i
                    o.phase = INVALID
                    o.final = dict(o.final, why="invalidated")
                elif o.phase == RESTING:
                    if armed:
                        just_armed = True
                        o.phase = ARMED
                        o.armed_bar = i
                        o.arm_bar_straddled = straddle
                elif straddle:
                    just_filled = just_resolved = True
                    o.resolved_bar = i
            o.right = i + tail

        # 2. THE CELL
        do_paint = just_resolved or just_armed or (was_live and at_right)
        if was_live and do_paint:
            rr, ok, news = ctx(i)
            risk = abs(o.entry - o.stop)
            o.target = (None if rr is None or risk <= 0
                        else (o.entry + risk * rr if o.is_long
                              else o.entry - risk * rr))
            use = (rr, ok, news)
            cell = (ctx.sess(i), ctx.state(i), ok, rr)
            if just_armed:
                o.arm_cell = cell
            if just_filled:
                # THE FILL BAR IS THE AUTHORITY — execution.py 2882/2950/2965.
                o.final = {"rr": rr, "ok": ok, "news": news, "why": "",
                           "sess": ctx.sess(i), "state": ctx.state(i)}
                tradeable = ok and news != NEWS_BLOCKED and rr is not None
                same_cell = (not o.arm_bar_straddled) or o.arm_cell == cell
                o.phase = (UNDECID if not same_cell
                           else TRADE if tradeable else NOTRADE)
                if not same_cell:
                    o.final["why"] = "fill bar undecidable on 15m"
        elif not was_live:
            use = (o.final["rr"], o.final["ok"], o.final["news"])
        else:
            use = (None, True, NEWS_UNKNOWN)

        # 3. OUTCOME
        if o.phase == TRADE and not just_filled \
                and o.resolved_bar is not None \
                and i > o.resolved_bar and o.target is not None:
            hit_stop = lo <= o.stop if o.is_long else hi >= o.stop
            hit_tgt = hi >= o.target if o.is_long else lo <= o.target
            if hit_stop or hit_tgt:
                o.phase = LOSS if hit_stop else WIN
                do_paint = True
                use = (o.final["rr"], o.final["ok"], o.final["news"])

        # 4/5. PAINT
        if do_paint:
            o.painted += 1
            rr, ok, news = use
            resting = o.phase in LIVE_PHASES
            o.rr_visible = (o.target is not None and (ok or not resting)
                            and news != NEWS_BLOCKED)
            # The AREAS are the trade; the LINES are the plan. A block that
            # ended without a trade keeps its levels and loses its areas.
            o.rr_area = o.rr_visible and o.phase in (
                RESTING, ARMED, TRADE, WIN, LOSS)
            o.rr_provisional = resting
            o.rr_muted = news == NEWS_UNKNOWN
    return o


class Ctx:
    """A per-bar execution context. `schedule` maps bar index -> (rr, ok, news);
    the last entry at or before the bar wins, so a context "changes" the way a
    session roll changes it."""

    def __init__(self, schedule, sessions=None, states=None):
        self.schedule = dict(schedule)
        self.sessions = sessions or {}
        self.states = states or {}

    def _at(self, table, i, default):
        keys = [k for k in table if k <= i]
        return table[max(keys)] if keys else default

    def __call__(self, i):
        return self._at(self.schedule, i, (None, True, NEWS_UNKNOWN))

    def sess(self, i):
        return self._at(self.sessions, i, "london")

    def state(self, i):
        return self._at(self.states, i, "Bull/Expand")


# ══ the transcription is the source ══════════════════════════════════════════

def test_the_transcription_still_matches_the_pine_source():
    body = _frag("68_live_setups")
    for needle in (
            "armed = o.isLong ? low <= o.arm : high >= o.arm",
            "wasLive = f_phLive(o.phase)",
            "through = o.isLong ? low < o.bottom : high > o.top",
            "box.set_right(o.obBox, bar_index + i_liveTailBars)",
            "lsAtRight = bar_index >= last_bar_index - 1",
            "bool doPaint = justResolved or justArmed or (wasLive and lsAtRight)",
            "straddle = low <= o.entry and high >= o.entry",
            "o.phase := LS_PH_ARMED",
            "o.armBarStraddled := straddle",
            "justFilled := true",
            "o.phase := not sameCell ? LS_PH_UNDECID :",
            "tradeable ? LS_PH_TRADE : LS_PH_NOTRADE",
            "o.phase := hitStop ? LS_PH_LOSS : LS_PH_WIN",
            "hitStop = o.isLong ? low <= o.stop : high >= o.stop",
            "hitTgt  = o.isLong ? high >= o.target : low <= o.target",
            "visible = not na(o.target) and (useOk or not resting)",
            "and useNews != LS_NEWS_BLOCKED",
            "tradeable = cOk and ls_nowNews != LS_NEWS_BLOCKED",
            "sameCell = not o.armBarStraddled"):
        assert needle in body, needle


def test_the_phase_to_colour_map_is_the_one_in_the_source():
    body = _frag("68_live_setups")
    m = re.search(r"f_phColor\(int ph\) =>(.*?)\n\n", body, re.S).group(1)
    assert "ph == LS_PH_TRADE   ? COL_LC_ACTIVE" in m
    assert "ph == LS_PH_WIN    ? COL_LC_WIN" in m
    assert "ph == LS_PH_LOSS   ? COL_LC_LOSS" in m
    assert "ph == LS_PH_INVALID ? COL_LC_INVALID" in m
    assert "ph == LS_PH_NOTRADE ? COL_LC_NONE" in m
    assert m.rstrip().endswith("COL_LC_LIVE")     # …resting is the default
    assert "COL_LC_INVALID = #9B59B6" in _frag("67_replay_visuals")


# ══ a resting block is an opportunity, not a trade ═══════════════════════════

#: A 1.10/1.09 BULLISH block:  entry 1.10 (the edge) · arm 1.0975 (25% in)
#: stop 1.0899 · target at 2R 1.1202.  Bars are (high, low).
QUIET_LONG = (1.1050, 1.1040)      # above the block: no touch, no arm, no fill
QUIET_SHORT = (1.0860, 1.0850)     # below a bearish block, likewise

#: ARMS **and** straddles the entry. This is the normal approach — price that
#: penetrates 25% has crossed the edge on the way in — and it is exactly why the
#: arm bar cannot be assumed to be the fill.
ARM_STRADDLE_LONG = (1.1050, 1.0970)
#: ARMS without straddling: the whole bar sits below the entry edge, so no fill
#: could have happened in it and the next straddle is provable.
ARM_ONLY_LONG = (1.0990, 1.0970)
#: A later bar whose range contains the entry: production's fill condition.
FILL_LONG = (1.1010, 1.0995)
#: Clean through the far edge — production's INVALIDATED_BEFORE_EDGE_ENTRY.
THROUGH_LONG = (1.1100, 1.0850)

ARM_STRADDLE_SHORT = (1.0930, 1.0850)
ARM_ONLY_SHORT = (1.0930, 1.0910)
FILL_SHORT = (1.0905, 1.0895)
THROUGH_SHORT = (1.1150, 1.0850)


def _quiet(n, bar=QUIET_LONG):
    return [bar] * n


def _seq(n, arm_at, fill_at=None, arm_bar=ARM_ONLY_LONG, fill_bar=FILL_LONG,
         quiet=QUIET_LONG):
    """`n` bars, quiet except for an arm bar and an optional later fill bar."""
    bars = [quiet] * n
    bars[arm_at] = arm_bar
    if fill_at is not None:
        bars[fill_at] = fill_bar
    return bars


def test_a_resting_block_stays_blue_while_the_cell_says_trade():
    """THE regression. `NOW: TRADE 2.25R` is a statement about a hypothetical
    trigger. No broker order exists, so the lifecycle colour must not move."""
    o = run(Block(1.10, 1.09, True),
            _quiet(30), Ctx({0: (2.25, True, NEWS_OK)}))
    assert o.phase == RESTING
    assert PHASE_COLOUR[o.phase] == BLUE
    assert o.target == pytest.approx(1.10 + (1.10 - 1.0899) * 2.25)


def test_a_resting_block_stays_blue_and_alive_when_the_cell_says_block():
    """Structural existence and execution eligibility are different things. A
    BLOCK verdict must not delete the block, grey it out, or stop it growing."""
    o = run(Block(1.10, 1.09, True),
            _quiet(30), Ctx({0: (None, False, NEWS_OK)}))
    assert o.phase == RESTING and PHASE_COLOUR[o.phase] == BLUE
    assert o.right == 29 + 4, "a blocked block must keep extending"
    assert not o.rr_visible, "…but it has no executable target to draw"


def test_a_block_becomes_tradeable_again_when_the_context_moves():
    """No new block is created. The same one changes its answer."""
    bars = _quiet(40)
    ctx = Ctx({0: (None, False, NEWS_OK), 20: (1.25, True, NEWS_OK)},
              sessions={0: "lull", 20: "newYork"},
              states={0: "Bear/Chop", 20: "Bull/Expand"})
    o = run(Block(1.10, 1.09, True), bars, ctx)
    assert o.phase == RESTING and PHASE_COLOUR[o.phase] == BLUE
    assert o.rr_visible and o.rr_provisional
    assert o.target == pytest.approx(1.10 + (1.10 - 1.0899) * 1.25)


def test_the_provisional_target_follows_the_current_cell():
    """Same block, two different contexts, two different targets — which is the
    whole reason a creation-time target is wrong."""
    bars = _quiet(30)
    a = run(Block(1.10, 1.09, True), bars, Ctx({0: (3.0, True, NEWS_OK)}))
    b = run(Block(1.10, 1.09, True), bars, Ctx({0: (1.25, True, NEWS_OK)}))
    assert a.target != b.target
    assert a.target > b.target


def test_an_untouched_block_keeps_extending():
    o = run(Block(1.10, 1.09, True), _quiet(30), Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == RESTING and o.right == 29 + 4


# ══ the trigger freezes the context ══════════════════════════════════════════

def test_arming_does_not_resolve_anything():
    """THE parity fix. Production arms, waits `delay` candles, and only fills
    when a candle straddles the entry — which may be hours later and in another
    session. Measured over 659 filled trades: arm and fill land on a different
    15m bar 49.2% of the time and in a different SESSION 8.2% of the time."""
    ctx = Ctx({0: (3.0, True, NEWS_OK), 20: (1.25, True, NEWS_OK)},
              sessions={0: "london", 20: "newYork"},
              states={0: "Bull/Expand", 20: "Bear/Chop"})
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=25), ctx)
    assert o.armed_bar == 10 and o.resolved_bar == 25
    assert o.phase in (TRADE, WIN, LOSS)
    # the FILL bar's cell, not the arm bar's
    assert o.final["rr"] == 1.25
    assert o.final["sess"] == "newYork" and o.final["state"] == "Bear/Chop"


def test_a_setup_stays_ARMED_and_BLUE_until_a_fill_is_proven():
    """An armed order is not a trade. No position exists, so the lifecycle
    colour must not move off blue."""
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10),
            Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == ARMED and PHASE_COLOUR[o.phase] == BLUE
    assert o.armed_bar == 10 and o.resolved_bar is None


def test_the_target_freezes_at_the_FILL_and_not_before_or_after():
    """It must not freeze at the arm (too early) and must not keep moving after
    the fill (too late)."""
    ctx = Ctx({0: (3.0, True, NEWS_OK), 15: (1.25, True, NEWS_OK),
               30: (4.0, True, NEWS_OK)})
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=20), ctx)
    assert o.final["rr"] == 1.25, "the cell in force at the fill bar"
    assert o.target == pytest.approx(1.10 + (1.10 - 1.0899) * 1.25)


def test_unknown_news_at_the_fill_does_not_force_a_terminal_no_trade():
    """THE second grey regression. A KNOWN blackout is knowledge and ends the
    block; NEWS UNKNOWN is a MISSING INPUT and must not decide the lifecycle at
    all. Every block resolving after the calendar's end went terminal-grey on
    that rule — which was the entire recent chart.

    The block resolves on the gates the chart CAN evaluate; the label carries
    "unconfirmed" from then on, and never says ALLOWED."""
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=20),
            Ctx({0: (2.0, True, NEWS_UNKNOWN)}))
    assert o.phase in (TRADE, WIN, LOSS)
    assert PHASE_COLOUR[o.phase] != GREY
    assert o.final["news"] == NEWS_UNKNOWN, "the qualification must survive"


def test_a_known_blackout_still_ends_the_block():
    """The contrast that makes the test above meaningful: here the chart KNOWS
    production would have refused, so there is no trade."""
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=20),
            Ctx({0: (2.0, True, NEWS_BLOCKED)}))
    assert o.phase == NOTRADE and PHASE_COLOUR[o.phase] == GREY


def test_the_final_label_qualifies_rather_than_verdicts_on_unknown_news():
    body = _frag("68_live_setups")
    text = body[body.index("f_liveText(LiveOb o"):body.index("f_liveTip(LiveOb o")]
    assert 'string q = o.finalNews == LS_NEWS_UNKNOWN ? " · unconfirmed" : ""' in text
    assert 'R · open" + q' in text
    assert "NOT CONFIRMED" not in text, \
        "unknown news must not be rendered as a terminal verdict"


def test_passing_clean_through_ends_PURPLE_and_never_orange():
    """Price that penetrates 25% has by construction touched the edge too, so
    arming alone cannot tell "came back and filled" from "kept going".
    Production's INVALIDATED_BEFORE_EDGE_ENTRY is what separates them.

    And invalidation gets its OWN colour, not another shade of grey: the block
    failed STRUCTURALLY and no policy gate was ever consulted, so reading it as
    a policy rejection blames the wrong thing. It is also production's single
    most common non-outcome — 39 of the 109 recorded setups."""
    bars = _quiet(3) + [THROUGH_LONG] + _quiet(20)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == INVALID and PHASE_COLOUR[o.phase] == PURPLE
    assert o.final["why"] == "invalidated"
    assert o.right == 3 + 4, "a dead block must stop growing"


def test_invalidation_outranks_an_allowed_cell():
    """Even with every policy gate saying TRADE, a block price went straight
    through was never viable."""
    bars = _quiet(3) + [THROUGH_LONG] + _quiet(20)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (3.0, True, NEWS_OK)}))
    assert o.phase == INVALID


def test_invalidation_after_arming_is_still_purple():
    """Production's INVALIDATED_BEFORE_EDGE_ENTRY fires on armed orders too —
    it is 39 of the 109 recorded setups."""
    bars = _seq(30, arm_at=5)
    bars[12] = THROUGH_LONG
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == INVALID and o.resolved_bar == 12


def test_a_policy_block_is_grey_not_purple():
    """The contrast. Grey means the policy said no; purple means the block was
    never viable. Collapsing them loses which gate actually fired."""
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=20),
            Ctx({0: (None, False, NEWS_OK)}))
    assert o.phase == NOTRADE and PHASE_COLOUR[o.phase] == GREY


def test_a_bearish_block_arms_from_below_and_fills_later():
    o = run(Block(1.10, 1.09, False),
            _seq(30, arm_at=8, fill_at=18, arm_bar=ARM_ONLY_SHORT,
                 fill_bar=FILL_SHORT, quiet=QUIET_SHORT),
            Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.armed_bar == 8 and o.resolved_bar == 18
    assert o.phase in (TRADE, WIN, LOSS) and o.final["why"] == ""


def test_touching_the_edge_without_penetrating_25_percent_does_not_arm():
    """The edge is the ENTRY; 25% inside is the ARM. Touching the edge alone is
    not production's trigger."""
    bars = _quiet(5) + [(1.1050, 1.0999)] + _quiet(20)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == RESTING, "1.0999 is above the 1.0975 arm level"


# ══ outcomes, and the geometry that must survive them ════════════════════════

def test_a_winner_turns_green_and_keeps_its_geometry():
    bars = _seq(60, arm_at=10, fill_at=20)
    bars[40] = (1.2200, 1.1900)                  # sails past a 2R target
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == WIN and PHASE_COLOUR[o.phase] == GREEN
    assert o.rr_visible, "a completed winner must KEEP its risk-reward tool"
    assert not o.rr_provisional, "…drawn as a fact, not a preview"
    assert o.target is not None and o.stop is not None


def test_a_loser_turns_red_and_keeps_its_geometry():
    bars = _seq(60, arm_at=10, fill_at=20)
    bars[40] = (1.0950, 1.0895)                  # through the stop
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == LOSS and PHASE_COLOUR[o.phase] == RED
    assert o.rr_visible and not o.rr_provisional


def test_a_same_bar_stop_and_target_is_read_as_a_loss():
    """Intrabar order is not resolvable on a 15-minute chart, so the
    conservative reading wins — and the tooltip says the outcome is the chart's
    own first-touch verdict, not production's exit chain."""
    bars = _seq(60, arm_at=10, fill_at=20)
    bars[40] = (1.2500, 1.0500)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == LOSS


def test_the_outcome_is_not_evaluated_on_the_fill_bar():
    """The fill and a same-bar exit cannot be ordered, so the outcome starts the
    bar AFTER."""
    bars = _seq(40, arm_at=10)
    bars[20] = (1.2500, 1.0995)                  # fills and blows past target
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == TRADE, "the fill bar must not also settle the outcome"


def test_a_resolved_block_is_never_re_evaluated():
    """The self-perpetuating guard that made boxes grow forever."""
    bars = _seq(40, arm_at=5, fill_at=15)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.right == 15 + 4, f"froze at {o.right}"


# ══ the areas are the trade; the lines are the plan ══════════════════════════

def test_a_resting_block_shows_its_areas_because_they_are_the_preview():
    o = run(Block(1.10, 1.09, True), _quiet(30),
            Ctx({0: (2.25, True, NEWS_OK)}))
    assert o.phase == RESTING and o.rr_area


def test_a_trade_and_its_outcome_keep_their_areas():
    for mutate, want in ((lambda b: b.__setitem__(40, (1.2200, 1.1900)), WIN),
                         (lambda b: b.__setitem__(40, (1.0950, 1.0895)), LOSS)):
        bars = _seq(60, arm_at=10, fill_at=20)
        mutate(bars)
        o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
        assert o.phase == want and o.rr_area, want


def test_an_invalidated_block_keeps_its_levels_but_loses_its_areas():
    """It never had a risk or a reward — no position ever existed. Drawing the
    areas anyway is inventing an outcome, which is the one thing this layer
    must not do."""
    bars = _quiet(3) + [THROUGH_LONG] + _quiet(20)
    o = run(Block(1.10, 1.09, True), bars, Ctx({0: (2.0, True, NEWS_OK)}))
    assert o.phase == INVALID
    assert not o.rr_area
    assert o.target is not None, "the planned levels are still known"


def test_a_policy_refusal_also_loses_its_areas():
    o = run(Block(1.10, 1.09, True), _seq(40, arm_at=10, fill_at=20),
            Ctx({0: (None, False, NEWS_OK)}))
    assert o.phase == NOTRADE and not o.rr_area


def test_the_live_area_gate_is_the_one_in_the_source():
    body = _frag("68_live_setups")
    assert ("showArea = f_phLive(o.phase) or o.phase == LS_PH_TRADE"
            in body)
    assert "or o.phase == LS_PH_WIN or o.phase == LS_PH_LOSS" in body
    for setter in ("box.set_bgcolor(o.riskBox, visible and showArea",
                   "box.set_bgcolor(o.rewardBox, showTarget and showArea",
                   "box.set_border_color(o.riskBox, visible and showArea"):
        assert setter in body, setter


def test_the_replay_area_starts_at_the_fill_not_at_detection():
    """THE regression, and it is measured below rather than asserted: anchoring
    the area at `t0` drew a slab spanning the candidate's whole life over price
    action during which no position existed."""
    body = _frag("67_replay_visuals")
    assert "rrEnd = exitT > 0 ? exitT : tTail" in body
    assert "if fill > 0" in body
    assert "f_keepBox(box.new(fill, math.max(entry, stop), rrEnd," in body
    assert "f_keepBox(box.new(fill, math.max(entry, target)," in body
    assert "box.new(t0, math.max(entry, stop)" not in body, \
        "the risk area must not be anchored at detection"
    # …and the LINES still span the whole life, because they are the plan.
    assert "f_keepLine(line.new(t0, entry, tEnd, entry," in body


def test_the_replay_area_span_is_the_trade_not_the_candidate():
    """Measured against the real recording. If this ever regresses the numbers
    move by two orders of magnitude, which is what made it visible on screen."""
    import json
    import statistics
    path = (CT_ROOT / "artifacts" / "tradingview_oracle" / "replay_setups.json")
    if not path.is_file():
        pytest.skip("no recording")
    setups = json.loads(path.read_text(encoding="utf-8"))["setups"]
    bar_ms = 15 * 60 * 1000

    from_detect = [(x["exit_ms"] - x["detected_ms"]) / bar_ms
                   for x in setups if x.get("exit_ms")]
    from_fill = [(x["exit_ms"] - x["fill_ms"]) / bar_ms
                 for x in setups if x.get("exit_ms") and x.get("fill_ms")]
    never_filled = [x for x in setups if not x.get("fill_ms")]

    assert statistics.median(from_detect) > 100, statistics.median(from_detect)
    assert statistics.median(from_fill) < 20, statistics.median(from_fill)
    assert max(from_fill) < max(from_detect) / 10
    # And the majority of the recording never filled at all, so anchoring at
    # detection drew an area for a trade that never happened.
    assert len(never_filled) > len(setups) / 2, len(never_filled)


# ══ news never becomes permission, and never erases the evidence ═════════════

def test_the_risk_reward_tool_has_no_fill_and_no_broken_lines():
    """Three translucent layers over the candles — session shading, the
    market-state band and the risk/reward fills — washed the price action out.
    And a broken edge reads as uncertainty about the LEVEL, which is never what
    is uncertain: the levels are arithmetic. Provisional-vs-final is carried by
    transparency instead, so nothing is lost."""
    body = _frag("68_live_setups")
    paint = body[body.index("f_rrPaint(LiveOb o"):body.index("f_liveText(LiveOb")]
    assert "rf = math.min(100, i_rrFill + fade)" in paint, \
        "the fill must be the operator's dial, and must respect the fade"
    assert "box.set_bgcolor(o.riskBox, visible" in paint
    assert "box.set_border_color(o.riskBox, visible" in paint
    assert "line.style_dashed" not in paint
    for ln in ("entryLine", "stopLine", "targetLine"):
        assert f"line.set_style(o.{ln}, line.style_solid)" in paint
    # …and the register is still distinguishable, by fade.
    assert "fade = muted ? 30 : provisional ? 18 : 0" in paint


def test_the_replay_layer_shares_the_same_fill_dial():
    """One setting, both layers — two dials that mean the same thing would be a
    settings page that lies about how many decisions there are."""
    body = _frag("67_replay_visuals")
    assert "bgcolor = color.new(COL_STOP, i_rrFill)" in body
    assert "math.min(100, i_rrFill + 2)" in body, \
        "the reward area stays two points fainter — a 3R box is 3x the height"
    assert "border_width = 1" in body
    # the hard-coded fills are gone from both layers
    assert "color.new(COL_STOP, 90)" not in body
    assert "color.new(COL_TARGET, 92)" not in body


def test_the_fill_dial_defaults_to_clear_and_cannot_go_opaque():
    body = _frag("10_inputs")
    assert 'i_rrFill        = input.int(100, "Risk/reward area fill"' in body
    assert "minval = 60, maxval = 100" in body, \
        "a fully opaque area would hide the candles behind it"


def test_the_replay_layer_colours_invalidation_by_REASON_not_status():
    """`invalidated_before_edge_entry` is a cancellation REASON, and it is the
    biggest bucket in the recording. Colouring by status alone folds it into the
    same grey as a policy block."""
    body = _frag("67_replay_visuals")
    assert "f_lcColor(int st, int outc, string reason) =>" in body
    assert ('reason == "invalidated_before_edge_entry" ? COL_LC_INVALID'
            in body)
    assert "col    = f_lcColor(status, outc, reason)" in body
    assert ('reason == "invalidated_before_edge_entry" ? "INVALIDATED"'
            in body), "…and the chip should say so too"


def test_the_reason_is_read_before_it_is_used():
    """Pine resolves top-to-bottom (CE10272): `reason` now feeds the colour, so
    it has to be read above the colour, not down at the label."""
    body = _frag("67_replay_visuals")
    assert (body.index("reason = array.get(RP_REASON, i)")
            < body.index("col    = f_lcColor(status, outc, reason)"))
    assert body.count("reason = array.get(RP_REASON, i)") == 1


def test_unknown_news_keeps_the_geometry_visible_but_muted():
    """The chart is a debugging instrument; the calculated levels are what an
    operator opens it to inspect. Hiding them makes the tool useless for the
    whole period past the calendar's end."""
    o = run(Block(1.10, 1.09, True), _quiet(30),
            Ctx({0: (2.25, True, NEWS_UNKNOWN)}))
    assert o.phase == RESTING
    assert o.rr_visible, "NEWS UNKNOWN must not erase the geometry"
    assert o.rr_muted, "…but it must be unmistakably not a confirmation"


def test_a_news_blackout_hides_the_geometry():
    """Different from UNKNOWN: here the chart KNOWS production would refuse."""
    o = run(Block(1.10, 1.09, True), _quiet(30),
            Ctx({0: (2.25, True, NEWS_BLOCKED)}))
    assert not o.rr_visible


def test_the_label_never_says_allowed_under_unknown_news():
    body = _frag("68_live_setups")
    text = body[body.index("f_liveText(LiveOb o"):body.index("f_liveTip(LiveOb o")]
    assert "NEWS UNKNOWN, not confirmed" in text
    assert "ALLOWED" not in text
    tip = body[body.index("f_liveTip(LiveOb o"):]
    assert 'ok and news == LS_NEWS_OK ? "ALLOWED"' in tip, \
        "the tooltip verdict must require BOTH gates"


# ══ arm vs entry stays distinct ══════════════════════════════════════════════

def test_arm_and_entry_are_different_levels_in_the_transcription():
    b = Block(1.10, 1.09, True)
    assert b.entry == pytest.approx(1.10), "entry is the block EDGE"
    assert b.arm == pytest.approx(1.0975), "arm is 25% INTO the block"
    s = Block(1.10, 1.09, False)
    assert s.entry == pytest.approx(1.09) and s.arm == pytest.approx(1.0925)


def test_the_two_levels_have_their_own_lines_and_colours():
    body = _frag("68_live_setups")
    assert "armLine = line.new(obIdx, arm," in body
    assert "style = line.style_dotted" in body
    assert "COL_ARM" in body and "COL_ENTRY" in body
    assert "f_entryLevel(bool isLong" in body and "f_armLevel(bool isLong" in body
    assert "TGT_ENTRY_PCT" in body and "TGT_TRIGGER_PCT" in body


# ══ objects and settings ═════════════════════════════════════════════════════

def test_the_provisional_refresh_updates_in_place_and_does_not_churn():
    """Part of the budget argument: an object created once and moved cannot
    exhaust the retention cap however long a block rests."""
    body = _frag("68_live_setups")
    loop = body[body.index("if CTX_OK and barClosed and i_showLive and array.size(lo_all) > 0"):]
    for banned in ("box.delete", "line.delete", "label.delete", "label.new"):
        assert banned not in loop, f"{banned} inside the per-bar loop"
    for setter in ("box.set_lefttop", "line.set_xy1", "label.set_text",
                   "label.set_tooltip", "box.set_bgcolor"):
        assert setter in body, setter


def test_the_object_budget_fits_pines_ceiling():
    """Pine's limit is 500 per OBJECT TYPE for the WHOLE script. The live layer
    alone was blowing it because `max_lines_count` was never set and defaults to
    FIFTY — which is what evicted the risk-reward lines."""
    src = GEN.read_text(encoding="utf-8")
    assert "max_lines_count = 500" in src, \
        "unset max_lines_count defaults to 50 and silently evicts lines"
    assert "max_boxes_count = 500" in src and "max_labels_count = 500" in src

    def const(name):
        return int(re.search(rf"^{name}\s*=\s*(\d+)$", src, re.M).group(1))

    def input_max(name):
        m = re.search(rf"{name}\s*=\s*input\.int\([^)]*maxval\s*=\s*(\d+)",
                      src, re.S)
        return int(m.group(1))

    live_obs = input_max("i_liveMaxObs")
    markers = input_max("i_maxObjects")
    # `i_maxObjects` backs FOUR buffers: oracleLines, oracleLabels, s5_boxes
    # and s5_obLabels. Counting it once per type would understate the labels.
    worst = {
        "lines": const("RP_MAX_LINES") + 4 * live_obs + markers + 2,
        "boxes": const("RP_MAX_BOXES") + 3 * live_obs + markers,
        "labels": const("RP_MAX_LABELS") + live_obs + 2 * markers,
    }
    for kind, total in worst.items():
        assert total <= 500, f"worst-case {kind} = {total} exceeds Pine's 500"


def test_every_setting_reaches_the_visual():
    """A setting whose only occurrence is its own declaration is a defect."""
    src = GEN.read_text(encoding="utf-8")
    code = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("//"))
    names = sorted(set(re.findall(r"^(i_\w+)\s*=\s*input\.", code, re.M)))
    assert len(names) >= 40, len(names)
    unwired = [n for n in names if len(re.findall(rf"\b{n}\b", code)) < 2]
    assert not unwired, f"declared but never used: {unwired}"


@pytest.mark.parametrize("setting,frag,needle", [
    ("i_showLive", "68_live_setups", "if CTX_OK and barClosed and i_showLive"),
    ("i_showLiveRr", "68_live_setups", "if i_showLiveRr and doPaint"),
    ("i_showArm", "68_live_setups", "i_showArm ? color.new(COL_ARM, 30)"),
    ("i_showReplay", "67_replay_visuals", "and i_showReplay and RP_COUNT > 0"),
    ("i_obSource", "67_replay_visuals", 'i_obSource != "Chart-derived"'),
    ("i_shadeSessions", "50_visuals", "i_shadeSessions"),
    ("i_showLegend", "70_legend", "if barstate.islast and i_showLegend"),
    ("i_ovMode", "68_live_setups", 'if i_ovMode == "Local overrides"'),
    ("i_replayCount", "67_replay_visuals", "rp_wanted = str.tonumber(i_replayCount)"),
    ("i_liveMaxObs", "68_live_setups", "if array.size(lo_all) > i_liveMaxObs"),
    ("i_rrFill", "68_live_setups", "rf = math.min(100, i_rrFill + fade)"),
    ("i_rrFill", "67_replay_visuals", "bgcolor = color.new(COL_STOP, i_rrFill)"),
])
def test_named_settings_reach_their_intended_path(setting, frag, needle):
    assert needle in _frag(frag), f"{setting} does not reach {frag}"


def test_the_arm_setting_is_honoured_after_creation_too():
    """Toggling it must take effect without reloading the script."""
    body = _frag("68_live_setups")
    assert body.count("i_showArm ? color.new(COL_ARM, 30)") == 2, \
        "set at creation AND maintained on the per-bar pass"
