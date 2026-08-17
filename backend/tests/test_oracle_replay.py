"""Setup replay — production's decisions, drawn on the chart.

Deliberately small. The values are not recomputed here, so there is no algorithm
to pin; what CAN go wrong is the mapping (an outcome landing on the wrong status),
the staleness guard (drawing decisions a different engine made), and the arrays
falling out of step with each other.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle.export_replay import (DEFAULT_OUT, OUTCOME,  # noqa: E402
                                        REASON, STATUS, _reason, _status)
from tools.oracle.generate_pine import (BUILD_TARGETS,  # noqa: E402
                                        build_generated_replay)

ARRAYS = ("RP_DETECTED", "RP_OBID", "RP_DIRECTION", "RP_STATUS", "RP_OUTCOME",
          "RP_FILL", "RP_EXIT", "RP_ENTRY", "RP_STOP", "RP_TARGET", "RP_NETR",
          "RP_REASON")


@pytest.mark.parametrize("outcome,reason,status", [
    ("WIN", "", "COMPLETED"), ("LOSS", "", "COMPLETED"), ("BE", "", "COMPLETED"),
    ("OPEN", "", "FILLED"),
    ("STATE_BLOCKED", "state_target_block", "BLOCKED"),
    ("COHORT_DISABLED", "cohort_disabled", "BLOCKED"),
    ("REGIME_BLOCKED", "regime_blocked", "BLOCKED"),
    ("INVALID", "invalidated_before_edge_entry", "CANCELLED"),
    ("UNFILLED", "never_triggered", "PENDING"),
    ("UNFILLED", "never_filled_after_trigger", "ALLOWED"),
])
def test_outcome_maps_to_the_status_a_reader_expects(outcome, reason, status):
    """UNFILLED is the interesting one: "never armed" and "armed and missed" are
    the same production outcome and completely different stories on a chart."""
    got, _ = _status({"outcome": outcome, "cancel_reason": reason})
    assert got == status


def test_a_nan_reason_is_not_rendered_as_a_blocking_reason():
    """The trades frame is a DataFrame, so a column blank for SOME rows becomes a
    float NaN — and `str(nan)` is "nan", which sailed through the first version
    and rendered as a reason on 19 of 60 setups."""
    assert _reason({"cancel_reason": float("nan"), "missed_reason": ""}) == ""
    assert _reason({"cancel_reason": "nan"}) == ""
    assert _reason({"cancel_reason": "", "missed_reason": "never_filled"}) == ""
    assert _reason({"cancel_reason": "regime_blocked"}) == "regime_blocked"


def test_every_status_and_outcome_has_a_pine_constant():
    block = build_generated_replay()
    for name in STATUS:
        assert re.search(rf"^RP_ST_{name}\s", block, re.M), name
    for name in OUTCOME:
        assert re.search(rf"^RP_OUT_{name}\s", block, re.M), name


def test_replay_is_dropped_when_it_predates_the_current_engine(monkeypatch,
                                                               tmp_path):
    """A setup recorded against a different engine describes a decision this
    build's production no longer makes. A stale trade on the chart is worse than
    no trade."""
    stale = tmp_path / "replay.json"
    stale.write_text(json.dumps({
        "engine_hash": "0" * 64,
        "setups": [{"detected_ms": 1, "ob_id": 1, "direction": 1, "structure": 1,
                    "ob_top": 1.1, "ob_bottom": 1.0, "entry": 1.1, "stop": 1.0,
                    "target": 1.3, "status": "COMPLETED", "outcome": "WIN",
                    "reason": "", "armed_ms": 0, "fill_ms": 2, "exit_ms": 3,
                    "net_r": 2.0, "session": "London"}],
        "dropped_oldest": 0}), encoding="utf-8")
    monkeypatch.setattr("tools.oracle.export_replay.DEFAULT_OUT", stale)
    block = build_generated_replay()
    assert "RP_COUNT           = 0" in block
    assert "RP_STALE           = true" in block


def test_a_missing_replay_file_yields_an_empty_set_not_an_error(monkeypatch,
                                                                tmp_path):
    monkeypatch.setattr("tools.oracle.export_replay.DEFAULT_OUT",
                        tmp_path / "absent.json")
    block = build_generated_replay()
    assert "RP_COUNT           = 0" in block
    assert "RP_STALE           = false" in block


def test_the_recorded_set_is_internally_consistent():
    if not DEFAULT_OUT.is_file():
        pytest.skip("no replay recorded yet — run export_replay --write")
    data = json.loads(DEFAULT_OUT.read_text(encoding="utf-8"))
    setups = data["setups"]
    assert setups, "an empty recording would draw nothing"
    assert setups == sorted(setups, key=lambda s: (s["detected_ms"], s["ob_id"])), \
        "the Pine cursor is forward-only and relies on this order"
    for s in setups:
        assert s["status"] in STATUS
        assert s["outcome"] in OUTCOME
        assert s["detected_ms"] > 0
        # Risk and reward must sit on OPPOSITE sides of entry, or the boxes draw
        # inside out and a losing setup looks like a winning one.
        if s["stop"] and s["target"]:
            if s["direction"] == 1:
                assert s["stop"] < s["entry"] < s["target"], s
            else:
                assert s["target"] < s["entry"] < s["stop"], s
        if s["status"] == "COMPLETED":
            assert s["fill_ms"] > 0 and s["exit_ms"] >= s["fill_ms"], s


def test_the_generated_arrays_are_all_the_same_length():
    """They are indexed by one cursor; a short array is an out-of-bounds read."""
    block = build_generated_replay()
    count = int(re.search(r"^RP_COUNT\s+=\s+(\d+)", block, re.M).group(1))
    for name in ARRAYS:
        m = re.search(rf"{name} = array\.from\(([^)]*)\)", block)
        got = len(m.group(1).split(",")) if m else 0
        assert got == count, f"{name}: {got} entries against RP_COUNT {count}"


def test_the_replay_fragment_is_in_the_detection_build_only():
    assert "67_replay_visuals" in BUILD_TARGETS["detection_15m"]["fragments"]
    assert "67_replay_visuals" not in BUILD_TARGETS["execution_1m"]["fragments"]


SRC = CT_ROOT / "pine" / "src"


def _frag(name):
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


def _all_fragments():
    return {p.stem: p.read_text(encoding="utf-8") for p in SRC.glob("*.pinefrag")}


def test_every_input_actually_controls_something():
    """A switch wired to nothing is worse than a missing switch: it tells the
    reader the feature is off when it was never on. `i_showParsed` was exactly
    that — it survived the removal of the parsed-price plots at the 64-plot
    ceiling and then controlled nothing for two waves."""
    frags = _all_fragments()
    defined = set(re.findall(r"^(i_[A-Za-z0-9_]+)\s*=\s*input\.",
                             frags["10_inputs"], re.M))
    assert defined, "no inputs found"
    for name in sorted(defined):
        uses = sum(len(re.findall(rf"\b{name}\b", body))
                   for frag, body in frags.items() if frag != "10_inputs")
        assert uses > 0, f"{name} is defined but never used"


@pytest.mark.parametrize("name,fragment", [
    ("i_showLegend", "70_legend"),
    ("i_legendPos", "70_legend"),
    ("i_shadeSessions", "50_visuals"),
    ("i_showSwingMarks", "59_structure_visuals"),
    ("i_showStructure", "59_structure_visuals"),
    ("i_showOrderBlocks", "65_order_blocks"),
    ("i_showRegime", "66_regime"),
    ("i_showReplay", "67_replay_visuals"),
    ("i_replayCount", "67_replay_visuals"),
    ("i_replayTailBars", "67_replay_visuals"),
    ("i_showPending", "67_replay_visuals"),
    ("i_showHud", "99_hud"),
    ("i_debug", "99_hud"),
])
def test_each_setting_is_read_by_the_fragment_it_names(name, fragment):
    assert re.search(rf"\b{name}\b", _frag(fragment)), \
        f"{name} does not reach {fragment}"


def test_the_legend_renders_even_on_an_unsupported_chart():
    """It used to gate every cell write behind CTX_OK, so on a wrong symbol or
    timeframe the table stayed empty — indistinguishable from a broken toggle.
    Now the context problem is stated IN the legend."""
    body = _frag("70_legend")
    guard = re.search(r"^if barstate\.islast and ([^\n]*)", body, re.M).group(1)
    assert "CTX_OK" not in guard, \
        "the legend must not be gated on the data context"
    assert "UNSUPPORTED CHART" in body
    # …and it must not clear itself before writing, which left it blank whenever
    # anything returned between the two.
    assert "table.clear(legend" not in body


def test_the_legend_table_is_sized_from_the_generated_schedule():
    assert "LEGEND_ROWS" in _frag("70_legend")
    assert re.search(r"table\.new\([^)]*LEGEND_ROWS", _frag("70_legend"))


def test_structure_breaks_draw_a_level_line_not_just_a_badge():
    """A break is a LEVEL being crossed; the drawing has to say WHICH."""
    body = _frag("59_structure_visuals")
    assert "s4_broken_bar" in body, "the line needs the broken swing's pivot bar"
    assert re.search(r"line\.new\(x0, s4_broken_level", body)
    assert '"CH"' in body and '"BOS"' in body, "compact chips, not CHoCH badges"


def test_the_broken_swing_bar_is_tracked_on_both_sides():
    body = _frag("58_structure")
    assert body.count("s4_broken_bar :=") >= 3   # reset + bull + bear
    assert "s4_broken_bar := s3_swing_high_bar" in body
    assert "s4_broken_bar := s3_swing_low_bar" in body


def test_lifecycle_colours_cover_every_status():
    """Blue live, orange in progress, green win, red loss, grey ended without a
    trade — and no status may fall through to an unstyled default."""
    body = _frag("67_replay_visuals")
    for const in ("COL_LC_LIVE", "COL_LC_ACTIVE", "COL_LC_WIN", "COL_LC_LOSS",
                  "COL_LC_NONE"):
        assert const in body, const
    picker = body[body.index("f_lcColor"):body.index("f_statusChip")]
    for status in ("RP_ST_COMPLETED", "RP_ST_FILLED", "RP_ST_ALLOWED",
                   "RP_ST_PENDING"):
        assert status in picker, f"{status} has no lifecycle colour"
    assert "RP_OUT_LOSS" in picker and "RP_OUT_WIN" in picker, \
        "a completed setup must be coloured by its OUTCOME, not merely completed"


def test_order_blocks_extend_past_the_event_that_consumed_them():
    """The tail is still there — a box that stops dead on the candle that ended
    it is unreadable. What changed is WHICH event it runs to: a block is
    consumed at the FILL, and everything after that belongs to the trade, not
    to the block."""
    body = _frag("67_replay_visuals")
    assert "i_replayTailBars * RP_BAR_MS" in body
    assert "blockEnd = tBlockEnd + i_replayTailBars * RP_BAR_MS" in body
    assert "tBlockEnd = fill > 0 ? fill : tEnd" in body
    assert re.search(r"box\.new\(t0, obTop, boxEnd, obBot", body), \
        "the lifecycle box runs to the consumed-plus-tail instant, capped"


def test_the_setup_marker_stays_compact_and_puts_detail_in_the_tooltip():
    """Pine has no click handler on a box, so the tooltip IS the detail view —
    but the visible text has to stay short or the chart is unreadable."""
    body = _frag("67_replay_visuals")
    visible = re.search(r'"OB " \+ str\.tostring\(array\.get\(RP_OBID, i\)\) \+ '
                        r'" · " \+\s*f_statusChip', body)
    assert visible, "the visible marker should be `OB <id> · <chip>`"
    for field in ("detected", "state", "session", "cohort", "entry", "stop",
                  "target", "planned", "status", "armed", "filled", "exited",
                  "gross R", "net R"):
        assert field in body, f"tooltip is missing {field}"


def test_the_historical_setup_carries_the_state_it_was_judged_under():
    """The S6 band shows the state NOW. A setup from March must show March's."""
    assert "RP_STATE" in _frag("67_replay_visuals")
    block = build_generated_replay()
    assert "RP_STATE" in block and "RP_SESSION" in block and "RP_COHORT" in block


def test_the_hud_hides_engineering_metadata_by_default():
    """Comments stripped first: the fragment's own header DISCUSSES these
    constants, and matching prose would make the test pass or fail on wording."""
    code = "\n".join(l.split("//")[0] for l in _frag("99_hud").splitlines())
    # The block lives inside `f_hudRender()` since it was lifted out of the
    # main body for CE10295. Anchoring on the `if` still "worked" afterwards —
    # it just bracketed the two-line CALL SITE, so both halves came out empty
    # and every "X not in normal" assertion passed vacuously. The non-emptiness
    # check below exists so that can never be why this goes green.
    start = code.index("f_hudRender() =>")
    end = code.index("var label warnLabel")
    # The HUD INTERLEAVES normal and debug blocks, so "before the first
    # `if i_debug`" is the wrong test. Walk the block instead: a debug region
    # opens at `    if i_debug` and closes at the next statement back at
    # four-space indentation. Plain indentation is not enough — a WRAPPED
    # argument list is indented further than its own statement.
    normal_lines, debug_lines, in_debug = [], [], False
    for line in code[start:end].splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        if line.strip() == "if i_debug" and indent == 4:
            in_debug = True
            continue
        if in_debug and indent <= 4:
            in_debug = False
        (debug_lines if in_debug else normal_lines).append(line)
    normal, debug = "\n".join(normal_lines), "\n".join(debug_lines)
    assert len(normal_lines) > 20 and len(debug_lines) > 5, (
        f"the walk found {len(normal_lines)} normal and {len(debug_lines)} "
        f"debug lines — the anchors are not bracketing the HUD block")

    for engineering in ("ORACLE_ENGINE_ID_COMPACT", "ORACLE_CONFIG_HASH_SHORT",
                        "ORACLE_S1_LOGIC", "ORACLE_GLOBAL_STATUS",
                        "ORACLE_CONTRACT_SCHEMA"):
        assert engineering not in normal, \
            f"{engineering} renders unconditionally"
        assert engineering in debug, f"{engineering} is not in the debug block"

    for essential in ("sessLabel", "REGIME_STATE_NAME", "s4_bias", "RP_COUNT",
                      "s5_last_ob_id"):
        assert essential in normal, f"{essential} is only in debug mode"


def test_the_unsuppressible_warning_survives_both_hud_modes():
    body = _frag("99_hud")
    assert body.count("SHADOW MODE") >= 2, \
        "the warning must render on the HUD-on and HUD-off paths"


def test_object_retention_is_bounded_below_pines_ceiling():
    body = _frag("67_replay_visuals")
    for cap in ("RP_MAX_LINES", "RP_MAX_BOXES", "RP_MAX_LABELS"):
        assert f"array.size(rp_lines) > {cap}" in body or \
               f"array.size(rp_boxes) > {cap}" in body or \
               f"array.size(rp_labels) > {cap}" in body, cap
    block = build_generated_replay()
    for cap in ("RP_MAX_LINES", "RP_MAX_BOXES", "RP_MAX_LABELS"):
        value = int(re.search(rf"^{cap}\s+=\s+(\d+)", block, re.M).group(1))
        assert value <= 500, f"{cap} is at or above Pine's hard ceiling"


def test_the_replay_count_input_bounds_what_is_created():
    body = _frag("67_replay_visuals")
    assert "rp_first" in body and "i >= rp_first" in body, \
        "older setups must never be CREATED, not merely deleted afterwards"


def test_order_blocks_are_never_suppressed_outside_the_recorded_window():
    """THE defect the first on-chart build had: `i_obSource` defaulted to
    production-only, so on a view PAST the newest recorded setup the lifecycle
    boxes had no data and the chart-derived boxes were switched off — the chart
    showed no order blocks at all while the HUD reported 134 of them."""
    body = _frag("10_inputs")
    default = re.search(r'i_obSource\s+=\s+input\.string\("([^"]+)"', body).group(1)
    assert default == "Both", (
        "the default must draw the chart's own blocks too, or a chart scrolled "
        "past the recording renders nothing")


def test_the_hud_says_when_the_view_is_past_the_recording():
    """An empty chart is otherwise ambiguous: no setups here, or no data here?"""
    body = _frag("99_hud")
    assert "RP_LAST_MS" in body
    assert "VIEW IS PAST THIS" in body
    assert "RP_LAST_MS" in build_generated_replay()


def test_the_order_block_counter_is_not_labelled_active():
    """`s5_active_count` only ever increments — it counts blocks CREATED. The
    HUD called it "active", which read as 134 live blocks on screen."""
    assert "s5_active_count := s5_active_count - 1" not in _frag("65_order_blocks")
    hud = _frag("99_hud")
    assert 'str.tostring(s5_active_count) + " created"' in hud
    assert 'str.tostring(s5_active_count) + " active"' not in hud


def test_session_shading_strength_is_a_working_input():
    """It was baked in at 90 and the bands competed with the candles."""
    assert "i_shadeStrength" in _frag("10_inputs")
    visuals = _frag("50_visuals")
    assert "color.new(array.get(SESSION_SWATCH" in visuals, \
        "shading must derive from the OPAQUE swatch — color.new REPLACES " \
        "transparency, so re-shading the shaded array ignores the input"
    assert "i_shadeStrength" in visuals


def test_the_detection_build_still_fits_the_plot_budget():
    """The replay draws with lines, boxes and labels — NOT plots — so it must not
    have moved the budget at all."""
    src = BUILD_TARGETS["detection_15m"]["pine"].read_text(encoding="utf-8")
    code = "\n".join(l.split("//")[0] for l in src.splitlines())
    used = sum(len(re.findall(rf"(?<![\w.]){f}\s*\(", code))
               for f in ("plot", "plotshape", "plotchar", "plotarrow",
                         "plotcandle", "plotbar"))
    assert used == 50, f"{used} plots — the replay should have added none"
