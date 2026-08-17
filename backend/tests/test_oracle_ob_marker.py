"""The order-block creation marker, in every renderer that draws a block.

WHY IT EXISTS. Two of the three renderers anchor the box at the ORIGIN CANDLE —
the bar whose parsed high/low set the block's bounds — while the block is not
CONFIRMED until price breaks structure, which can be many bars later. A wide box
therefore says where the block sits but not where the strategy found it. The
Research Lab draws the same distinction (`lux_style_swing_ob_backtester_v1.pine`
anchors at `obTime` and marks `time`); this is the same marker.

WHAT WOULD BREAK WITHOUT THESE TESTS. A drawing object created per setup and not
added to its renderer's retention path leaks until Pine silently evicts
something else — the failure mode that produced boxes surviving while their
labels vanished. Each assertion below pairs a `line.new` with the eviction or
deletion that owns it.
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
GEN = CT_ROOT / "pine" / "generated"


def frag(name: str) -> str:
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


def code(text: str) -> str:
    return "\n".join(l.split("//")[0] for l in text.splitlines())


# ── the colour constant ──────────────────────────────────────────────────────

@pytest.mark.parametrize("inputs", ["10_inputs", "s10_inputs"])
def test_the_marker_colour_is_declared_in_both_input_fragments(inputs):
    """`65_order_blocks` compiles into the companion as well as the oracle, and
    Pine resolves identifiers at COMPILE time — an unreachable reference to an
    undeclared constant is still CE10272. The companion's OB drawing is switched
    off and it still needs the constant to exist."""
    assert re.search(r"^COL_OB_MARK\s*=", code(frag(inputs)), re.M)


# ── one marker per renderer, at the confirmation bar ─────────────────────────

def test_chart_derived_marks_the_confirmation_bar():
    """`box.new(s5_bull_idx … bar_index)` runs origin -> confirmation, so the
    marker sits on the right edge and the box's width is the wait between the
    origin candle forming and structure breaking."""
    body = code(frag("65_order_blocks"))
    marks = re.findall(
        r"line\.new\(bar_index,\s*(\w+),\s*bar_index,\s*(\w+),\s*"
        r"color = COL_OB_MARK", body)
    assert len(marks) == 2, f"expected a bull and a bear marker, got {marks}"
    assert ("t", "b") in marks and ("t2", "b2") in marks, (
        f"markers must span the block's own bounds, got {marks}")


def test_live_marks_the_confirmation_bar_inside_its_box():
    """The live box runs origin -> frontier, so the marker falls INSIDE it.
    This is the renderer where it earns its place."""
    body = code(frag("68_live_setups"))
    assert "line  markLine" in body, "LiveOb has no markLine field"
    assert re.search(
        r"markLine = line\.new\(bar_index,\s*obTop,\s*bar_index,\s*obBottom",
        body), "the live marker must span the block at the confirmation bar"


def test_replay_marks_its_detection_time():
    """A replay box already starts at `t0`, which IS `detection_time` — the
    export carries no origin-candle timestamp. The marker coincides with the
    left edge, and is drawn anyway so one line means one thing on every layer."""
    body = code(frag("67_replay_visuals"))
    assert re.search(
        r"line\.new\(t0,\s*obTop,\s*t0,\s*obBot,[^)]*COL_OB_MARK", body), \
        "the replay marker must sit at t0"


# ── retention: every marker is owned by something that frees it ──────────────

def test_chart_derived_markers_are_evicted_with_their_boxes():
    body = code(frag("65_order_blocks"))
    assert "var line[] s5_obMarks" in body
    assert "line.delete(array.shift(s5_obMarks))" in body, (
        "markers accumulate without an eviction loop, and Pine drops other "
        "objects to make room rather than reporting it")


def test_live_markers_are_deleted_with_the_rest_of_the_setup():
    body = code(frag("68_live_setups"))
    delete_block = body[body.index("box.delete(o.obBox)"):]
    delete_block = delete_block[:delete_block.index("label.delete(o.tag)")]
    assert "line.delete(o.markLine)" in delete_block, (
        "markLine must be freed in the same place as every other LiveOb object")


def test_replay_markers_go_through_the_shared_line_budget():
    body = code(frag("67_replay_visuals"))
    assert re.search(r"f_keepLine\(line\.new\(t0,\s*obTop", body), (
        "the replay marker must be registered with f_keepLine or it escapes "
        "RP_MAX_LINES")


# ── the generated artefacts actually carry it ────────────────────────────────

@pytest.mark.skipif(not (GEN / "tradingview_visual_oracle_detection_15m.pine")
                    .is_file(), reason="oracle not generated")
def test_the_generated_oracle_carries_every_marker():
    src = code((GEN / "tradingview_visual_oracle_detection_15m.pine")
               .read_text(encoding="utf-8"))
    sites = [l for l in src.splitlines() if "COL_OB_MARK" in l]
    # one declaration + bull + bear + replay + live
    assert len(sites) == 5, f"expected 5 COL_OB_MARK sites, found {len(sites)}"
