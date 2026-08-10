"""News blackout on the chart, and the two levels that are not the same level.

Two defects motivated this module, both of them the kind that make a chart LOOK
right while being wrong:

  * the chart drew a clean entry/stop/target picture with no idea whether
    production's news gate would have blocked the fill. "No calendar" was
    rendered as "allowed";
  * `triggered_edge_trigger_thresholds = [25]` is the ARM threshold and
    `triggered_edge_entry_level_pct = 0` is the entry. Reading the 25 as the
    entry level puts the fill a quarter of the way into the block, which is not
    where production fills.

So the load-bearing tests here are `test_pine_news_state_agrees_with_production`
— the Pine minute-grid decision transcribed into Python and run against
production's own `_news_blackout_match` over the real exported schedule — and
`test_entry_is_the_edge_and_arm_is_the_penetration`, which asks production's own
`_planned_trade` where it fills.
"""

from __future__ import annotations

import json
import math
import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import export_news as en  # noqa: E402
from tools.oracle import target_table as tt  # noqa: E402
from tools.oracle.engine_access import (_in_lux, load_engine,  # noqa: E402
                                        resolve_config)

SRC = CT_ROOT / "pine" / "src"
MIN_MS = 60_000


def _frag(name: str) -> str:
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def cfg(engine):
    return resolve_config(engine)[0]


@pytest.fixture(scope="module")
def schedule():
    path = en.DEFAULT_OUT
    if not path.is_file():
        pytest.skip("no exported schedule; run `python -m tools.oracle.export_news --write`")
    return json.loads(path.read_text(encoding="utf-8"))


# ══ the export is production's, not a re-implementation ══════════════════════

def test_the_export_uses_productions_own_filter(schedule, cfg):
    """Every knob in the artefact is read off the deployed config, so a config
    change cannot leave the chart applying yesterday's calendar rules."""
    assert schedule["enabled"] is bool(cfg.news_blackout_enabled)
    assert schedule["impacts"] == list(cfg.news_blackout_impacts or [])
    assert schedule["currencies"] == list(cfg.news_blackout_currencies or [])
    assert schedule["minutes_before"] == int(cfg.news_blackout_minutes_before)
    assert schedule["minutes_after"] == int(cfg.news_blackout_minutes_after)


def test_every_window_brackets_its_event_by_the_configured_margins(schedule):
    before = schedule["minutes_before"] * MIN_MS
    after = schedule["minutes_after"] * MIN_MS
    for w in schedule["windows"]:
        assert w["at_ms"] - w["start_ms"] == before
        assert w["end_ms"] - w["at_ms"] == after


def test_the_coverage_edge_is_the_last_window(schedule):
    """`last_ms` is what the chart turns into NEWS UNKNOWN, so it must be the
    real end of the schedule and not, say, the last event TIME."""
    assert schedule["last_ms"] == max(w["end_ms"] for w in schedule["windows"])
    assert schedule["first_ms"] == min(w["start_ms"] for w in schedule["windows"])


def test_the_exporter_refuses_to_truncate(monkeypatch, engine, cfg):
    monkeypatch.setattr(en, "MAX_WINDOWS", 1)
    with pytest.raises(en.NewsExportError) as exc:
        en.build(engine, cfg, "2025-06-01", None)
    assert "truncated" in str(exc.value)


# ══ the Pine decision, transcribed ═══════════════════════════════════════════

def _pine_news_state(bar_start, bar_end, starts, ends, first_ms, last_ms,
                     present=True, enabled=True):
    """`f_newsState` from pine/src/68_live_setups.pinefrag, line for line.

    Kept as a transcription rather than a description: the previous lifecycle
    bug survived a test that only asserted the SOURCE contained the right words.
    """
    OK, BLOCKED, UNKNOWN = 0, 1, 2
    if not present:
        return UNKNOWN
    if not enabled:
        return OK
    last_k = math.floor((bar_end - bar_start) / MIN_MS) - 1
    hit = 0
    for ws, we in zip(starts, ends):
        if hit == 0:
            k_lo = max(0, math.ceil((ws - bar_start) / MIN_MS))
            k_hi = min(last_k, math.floor((we - bar_start) / MIN_MS))
            if k_lo <= k_hi:
                hit = 1
    if hit == 1:
        return BLOCKED
    if bar_start < first_ms or bar_end > last_ms:
        return UNKNOWN
    return OK


def _pine_source_matches_the_transcription():
    body = _frag("68_live_setups")
    for needle in ("lastK = math.floor((barEnd - barStart) / LS_MIN_MS) - 1",
                   "kLo = math.max(0, math.ceil((ws - barStart) / LS_MIN_MS))",
                   "kHi = math.min(lastK, math.floor((we - barStart) / LS_MIN_MS))",
                   "if kLo <= kHi",
                   "barStart < NEWS_FIRST_MS or barEnd > NEWS_LAST_MS"):
        assert needle in body, needle


def test_the_transcription_still_matches_the_pine_source():
    """If someone edits `f_newsState`, this fails before the agreement test can
    pass against a function that no longer exists in that form."""
    _pine_source_matches_the_transcription()


def test_pine_news_state_agrees_with_production(schedule, engine, cfg):
    """THE test. For every 15-minute bar in a real week of the schedule, the
    Pine decision must equal production's own per-minute verdict over the same
    fifteen candles."""
    import pandas as pd

    lux = Path(engine.lux_root)
    with _in_lux(lux):
        from strategy_core.news import _news_blackout_match
        events = engine.rb.load_news_events(cfg)

    rows = events.to_dict("records") if hasattr(events, "to_dict") else list(events)
    windows = schedule["windows"]
    starts = [w["start_ms"] for w in windows]
    ends = [w["end_ms"] for w in windows]
    first_ms, last_ms = schedule["first_ms"], schedule["last_ms"]

    # TWO samples, because each one alone passes for the wrong reason.
    #
    #   * a contiguous week is mostly quiet, so on its own it would mostly prove
    #     the chart can say "no";
    #   * the bars AROUND every embedded window are where the boundary decisions
    #     live — the rejected overlap implementation disagrees only there.
    bars = set()
    origin = first_ms + 30 * 24 * 3600 * 1000
    origin -= origin % (15 * MIN_MS)
    for b in range(4 * 24 * 7):
        bars.add(origin + b * 15 * MIN_MS)
    for w in windows:
        anchor = w["start_ms"] - w["start_ms"] % (15 * MIN_MS)
        for b in range(-2, 4):
            bars.add(anchor + b * 15 * MIN_MS)

    checked = blocked = 0
    for bar_start in sorted(bars):
        bar_end = bar_start + 15 * MIN_MS
        if bar_start < first_ms or bar_end > last_ms:
            continue
        want_blocked = any(
            _news_blackout_match(
                pd.Timestamp(bar_start + k * MIN_MS, unit="ms"), rows) is not None
            for k in range(15))
        got = _pine_news_state(bar_start, bar_end, starts, ends,
                               first_ms, last_ms)
        assert got == (1 if want_blocked else 0), (
            f"bar {pd.Timestamp(bar_start, unit='ms')} "
            f"pine={got} production_blocked={want_blocked}")
        checked += 1
        blocked += int(want_blocked)

    assert checked > 1500, f"only {checked} bars compared"
    # Both verdicts must be exercised, or the agreement is vacuous in one
    # direction: a function that always returned OK would pass a quiet sample.
    assert blocked > 300, f"only {blocked} blocked bars in the sample"
    assert checked - blocked > 300, f"only {checked - blocked} clear bars"


def test_a_window_after_the_last_minute_of_a_bar_does_not_block_that_bar():
    """The rejected OVERLAP implementation fails exactly here: the window opens
    30 seconds after the bar's final minute candle, so production's first
    blocked candle belongs to the NEXT bar."""
    bar = 1_700_000_000_000
    bar -= bar % (15 * MIN_MS)
    ws = bar + 14 * MIN_MS + 30_000
    we = ws + 6 * MIN_MS
    assert _pine_news_state(bar, bar + 15 * MIN_MS, [ws], [we],
                            bar - 10 * MIN_MS, bar + 10 * 60 * MIN_MS) == 0
    assert _pine_news_state(bar + 15 * MIN_MS, bar + 30 * MIN_MS, [ws], [we],
                            bar - 10 * MIN_MS, bar + 10 * 60 * MIN_MS) == 1


def test_the_window_ends_are_inclusive_like_production():
    """`window_start <= t <= window_end`. A minute landing exactly on either
    end is blocked."""
    bar = 1_700_000_000_000
    bar -= bar % (15 * MIN_MS)
    span = (bar - 10 * MIN_MS, bar + 10 * 60 * MIN_MS)
    # window that touches only the bar's first minute, at its END
    assert _pine_news_state(bar, bar + 15 * MIN_MS,
                            [bar - 6 * MIN_MS], [bar], *span) == 1
    # window that touches only the bar's last minute, at its START
    assert _pine_news_state(bar, bar + 15 * MIN_MS,
                            [bar + 14 * MIN_MS], [bar + 20 * MIN_MS], *span) == 1


def test_beyond_the_schedule_is_unknown_never_allowed(schedule):
    """The failure this whole path exists to prevent: an exhausted calendar
    reading as a quiet one."""
    starts = [w["start_ms"] for w in schedule["windows"]]
    ends = [w["end_ms"] for w in schedule["windows"]]
    after = schedule["last_ms"] + 30 * 24 * 3600 * 1000
    after -= after % (15 * MIN_MS)
    assert _pine_news_state(after, after + 15 * MIN_MS, starts, ends,
                            schedule["first_ms"], schedule["last_ms"]) == 2


def test_before_the_schedule_is_unknown_too(schedule):
    starts = [w["start_ms"] for w in schedule["windows"]]
    ends = [w["end_ms"] for w in schedule["windows"]]
    before = schedule["first_ms"] - 30 * 24 * 3600 * 1000
    before -= before % (15 * MIN_MS)
    assert _pine_news_state(before, before + 15 * MIN_MS, starts, ends,
                            schedule["first_ms"], schedule["last_ms"]) == 2


def test_an_absent_schedule_is_unknown_and_a_disabled_one_is_allowed():
    """These two must NOT collapse into each other. No data is not knowledge;
    production having no blackout rule IS."""
    assert _pine_news_state(0, 15 * MIN_MS, [], [], 0, 0, present=False) == 2
    assert _pine_news_state(0, 15 * MIN_MS, [], [], 0, 0, enabled=False) == 0


# ══ arm level vs entry level ═════════════════════════════════════════════════

def test_the_two_parameters_are_distinct_in_the_deployed_config(cfg):
    entry_pct = float(getattr(cfg, "triggered_edge_entry_level_pct", 0) or 0.0)
    trigger = list(getattr(cfg, "triggered_edge_trigger_thresholds", None) or [])
    assert entry_pct == 0.0, entry_pct
    assert trigger and float(trigger[0]) == 25.0, trigger


def test_entry_is_the_edge_and_arm_is_the_penetration(engine, cfg):
    """Production's own planner, asked where it fills. If the 25 were the entry
    level, the bullish entry would be 1.09750, not 1.10000."""
    top, bottom = 1.10000, 1.09000
    entry_pct = float(getattr(cfg, "triggered_edge_entry_level_pct", 0) or 0.0)
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.execution import _planned_trade

        def plan(direction):
            return _planned_trade(
                {"direction": direction, "top": top, "bottom": bottom},
                float(cfg.rr_multiple),
                stop_buffer=float(cfg.stop_buffer_pips) * float(cfg.pip_size),
                entry_model="triggered_edge",
                triggered_edge_entry_level_pct=entry_pct)

        long_plan, short_plan = plan("bullish"), plan("bearish")

    assert long_plan["entry"] == pytest.approx(top, abs=1e-9)
    assert short_plan["entry"] == pytest.approx(bottom, abs=1e-9)
    assert long_plan["entry"] != pytest.approx(top - 0.25 * (top - bottom))


def _pine_arm(is_long, top, bottom, trigger_pct):
    """`f_armLevel` from the fragment."""
    need = (top - bottom) * (trigger_pct / 100.0)
    return top - need if is_long else bottom + need


def test_the_pine_arm_level_is_25_percent_into_the_block(cfg):
    pct = float((getattr(cfg, "triggered_edge_trigger_thresholds", None)
                 or [0])[0])
    assert _pine_arm(True, 1.10, 1.09, pct) == pytest.approx(1.0975)
    assert _pine_arm(False, 1.10, 1.09, pct) == pytest.approx(1.0925)


def test_the_arm_level_sits_inside_the_block_on_the_approach_side(cfg):
    pct = float((getattr(cfg, "triggered_edge_trigger_thresholds", None)
                 or [0])[0])
    top, bottom = 1.10, 1.09
    for is_long in (True, False):
        arm = _pine_arm(is_long, top, bottom, pct)
        assert bottom < arm < top
        entry = top if is_long else bottom
        # The arm is always DEEPER into the block than the entry edge.
        assert (arm < entry) if is_long else (arm > entry)


def test_the_table_carries_the_trigger_pct_and_hashes_it(engine, cfg):
    table = tt.build(engine, cfg)
    assert table["trigger_pct"] == 25.0
    assert table["entry_level_pct"] == 0.0
    moved = dict(table, trigger_pct=30.0)
    assert tt.content_hash(moved) != tt.content_hash(table)


# ══ what the chart does with all of it ═══════════════════════════════════════

def test_the_risk_reward_tool_is_gated_on_news():
    """A normal entry/stop/target picture is a claim the trade is takeable, and
    inside a blackout the chart KNOWS production would refuse. UNKNOWN is a
    different case and is handled by the test below — it mutes, never hides."""
    body = _frag("68_live_setups")
    assert "and useNews != LS_NEWS_BLOCKED" in body
    paint = body[body.index("f_rrPaint(LiveOb o"):body.index("f_liveText(LiveOb")]
    assert "fade = muted ? 30" in paint, "an unknown-news tool must be faded"
    # Faded, NOT broken: a dashed edge reads as uncertainty about the LEVEL, and
    # the level is arithmetic — it is never the uncertain part.
    assert "line.style_dashed" not in paint
    # A hidden tool is hidden at every fill setting.
    assert "box.set_bgcolor(o.riskBox, visible" in paint
    assert "color.new(COL_STOP, LS_HIDDEN)" in paint


def test_unknown_news_never_renders_as_a_cleared_setup():
    """An unknown bar may still print its R multiple — the operator needs the
    number — but it must carry the qualification in the same breath, and the
    verdict line must never say ALLOWED."""
    body = _frag("68_live_setups")
    text = body[body.index("f_liveText(LiveOb o"):body.index("f_liveTip(LiveOb o")]
    assert 'news == LS_NEWS_BLOCKED ? "NEWS BLOCKED" + tail' in text
    assert 'news == LS_NEWS_UNKNOWN ? " · NEWS UNKNOWN, not confirmed"' in text
    assert '" · unconfirmed"' in text, "the FINAL label must qualify too"
    assert "ALLOWED" not in text


def test_news_does_not_touch_the_lifecycle_colour():
    """THE correction. Box colour is the STRUCTURAL/trade lifecycle; news is
    execution eligibility and belongs on the label. Overloading the box is what
    turned every block on the chart grey the moment the calendar ran out."""
    body = _frag("68_live_setups")
    colour = body[body.index("f_phColor(int ph) =>"):
                  body.index("// ── what the HUD reads")]
    for banned in ("NEWS", "ok", "elig"):
        assert banned not in colour, f"{banned} must not reach the colour map"
    assert "col = f_phColor(o.phase)" in body


def test_the_tooltip_names_both_levels():
    body = _frag("68_live_setups")
    assert 'str.tostring(TGT_TRIGGER_PCT, "0.#")' in body
    assert 'str.tostring(o.arm, format.mintick)' in body
    assert '"\\nentry / fill level      " + str.tostring(o.entry' in body
    assert '"   (block edge)"' in body


def test_the_arm_line_is_drawn_and_is_not_the_entry_line():
    body = _frag("68_live_setups")
    assert "armLine = line.new(obIdx, arm," in body
    assert "style = line.style_dotted" in body
    assert "COL_ARM" in _frag("67_replay_visuals")
    # entry keeps its own colour and its own line
    assert "o.entryLine  := line.new(x0, o.entry,  x1, o.entry,  color = na)" in body
    assert "line.set_color(o.entryLine, visible" in body


def test_the_arm_line_travels_and_freezes_with_its_block():
    body = _frag("68_live_setups")
    assert "line.set_x2(o.armLine, bar_index + i_liveTailBars)" in body
    assert "line.delete(o.armLine)" in body, "retirement leaks the arm line"


def test_the_hud_warns_when_the_chart_is_past_the_schedule():
    body = _frag("99_hud")
    assert 'if NEWS_PRESENT and NEWS_ENABLED and time > NEWS_LAST_MS' in body
    assert '"NEWS UNKNOWN - DATA ENDS " + NEWS_LAST_TXT' in body
    # …and it is in the unsuppressible block, not behind i_debug.
    warn = body.index('"NEWS UNKNOWN - DATA ENDS "')
    debug = body.rindex("if i_debug")
    assert warn > debug, "the warning is inside a debug-only branch"


def test_the_hud_no_longer_claims_news_is_unavailable():
    body = _frag("99_hud")
    assert "news unavailable" not in body
    assert 'f_row(r, "news"' in body


def test_the_generated_build_carries_the_schedule_and_the_edge():
    path = (CT_ROOT / "pine" / "generated"
            / "tradingview_visual_oracle_detection_15m.pine")
    if not path.is_file():
        pytest.skip("build not generated")
    text = path.read_text(encoding="utf-8")
    assert re.search(r"^NEWS_PRESENT   = true$", text, re.M)
    assert re.search(r"^NEWS_COUNT     = (\d+)$", text, re.M)
    assert re.search(r'^NEWS_LAST_TXT  = "\d{4}-\d{2}-\d{2}"$', text, re.M)
    count = int(re.search(r"^NEWS_COUNT     = (\d+)$", text, re.M).group(1))
    assert count > 0
    row = re.search(r"^var array<int> NEWS_START = array\.from\((.*)\)$",
                    text, re.M)
    assert row and len(row.group(1).split(",")) == count


# ══ staleness: the CHART's coverage vs the SOURCE's ══════════════════════════
#
# These exist because the two look identical on the chart and have completely
# different fixes. A narrow export is a tooling choice and is fixed here; a
# short calendar file is a PRODUCTION data problem — the live engine reads the
# same file, so its blackout gate is inert for the same period — and cannot be
# fixed from this repository at all.

def test_the_source_span_reports_the_calendar_files_own_end(engine, cfg):
    span = en.source_span(engine, cfg)
    assert span["exists"] and span["rows"] > 0
    assert span["last_event"] is not None
    # the impact-filtered end is never after the unfiltered end
    assert span["last_event_at_impact"] <= span["last_event"]


def test_the_artefact_records_the_source_end_not_just_its_own(schedule):
    """Without this the operator cannot tell WHY coverage stops."""
    assert schedule["source_last_event_ms"] > 0
    assert schedule["source_last_event_at_impact_ms"] > 0
    assert schedule["source_rows"] > schedule["count"]
    # The exported windows can never extend past the source they came from.
    assert schedule["last_ms"] <= schedule["source_last_event_at_impact_ms"] \
        + schedule["minutes_after"] * MIN_MS


def test_the_workflow_reports_coverage_and_names_a_stale_source(capsys, engine,
                                                                cfg):
    """`update_detection_visual` must print the five facts the operator needs —
    first, last, count, whether NOW is covered, and which side is stale."""
    from tools.oracle import update_detection_visual as udv

    covered = udv._news_step(engine, cfg, write=False)
    out = capsys.readouterr().out
    for needle in ("news source", "news windows", "news first", "news last",
                   "now covered"):
        assert needle in out, needle
    if not covered:
        assert "NO" in out
        assert ("PRODUCTION NEWS SOURCE IS STALE" in out
                or "THIS EXPORT IS NARROWER THAN THE SOURCE" in out), out


def test_the_workflow_does_not_claim_coverage_it_lacks(engine, cfg, schedule):
    """The one answer that must never be wrong in the optimistic direction."""
    import datetime as dt

    from tools.oracle import update_detection_visual as udv
    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    covered = udv._news_step(engine, cfg, write=False)
    assert covered == (now_ms <= schedule["last_ms"])
