"""Which layer renders a given order block, and why exactly one of them does.

THE PROBLEM THIS PINS
---------------------
There are two authorities on the chart. REPLAY carries production's own decision,
resolved on 1-minute data — it knows the fill minute, the exit and the realised
R. LIVE infers from 15-minute bars and cannot order the minutes inside one.

They used to be deliberately decoupled, and once the recording reached the
present that meant **100% overlap**: every live block on screen was also a replay
setup, drawn twice from different data and coloured by different logic.

Ceding on coverage alone would have been worse. The recording can CONTAIN a
setup the chart is not DRAWING — `i_replayCount` shows the most recent N, and
PENDING setups are hidden unless asked for — so a coverage-only rule makes those
blocks vanish from both layers.

Hence three clauses, and the two properties that matter more than either:

    NOTHING IS DRAWN TWICE   and   NOTHING DISAPPEARS
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

SRC = CT_ROOT / "pine" / "src"

#: Replay status codes, positional with `export_replay.STATUS`.
ST_UNKNOWN, ST_PENDING, ST_ALLOWED, ST_BLOCKED, ST_FILLED, ST_CANCELLED, \
    ST_COMPLETED = range(7)
BAR_MS = 15 * 60 * 1000


def _frag(name: str) -> str:
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


def replay_owns(det_ms, *, detected, statuses, rp_count=None, stale=False,
                show_replay=True, show_pending=False, replay_count=50):
    """`f_replayOwns` from pine/src/68_live_setups.pinefrag, line for line.

    Transcribed rather than described: an ownership rule that is wrong in one
    clause produces either a duplicate or a hole, and neither is visible from
    reading the condition.
    """
    n = len(detected) if rp_count is None else rp_count
    rp_first = max(0, n - replay_count)
    owns = False
    if n > 0 and not stale and show_replay and rp_first <= n - 1:
        for i in range(rp_first, n):
            if not owns:
                shown = statuses[i] != ST_PENDING or show_pending
                near = abs(detected[i] - det_ms) <= BAR_MS
                if shown and near:
                    owns = True
    return owns


def test_the_transcription_still_matches_the_pine_source():
    body = _frag("68_live_setups")
    for needle in (
            "f_replayOwns(int detMs) =>",
            "if RP_COUNT > 0 and not RP_STALE and i_showReplay",
            "and rp_first <= RP_COUNT - 1",
            "for i = rp_first to RP_COUNT - 1",
            "shown = st != RP_ST_PENDING or i_showPending",
            "near = math.abs(array.get(RP_DETECTED, i) - detMs) <= RP_BAR_MS",
            "if shown and near",
            "if not f_replayOwns(time)"):
        assert needle in body, needle


# ══ a fixture recording ══════════════════════════════════════════════════════
#: 60 setups an hour apart; #10 is PENDING, the rest COMPLETED.
N = 60
DETECTED = [1_700_000_000_000 + i * 3_600_000 for i in range(N)]
STATUSES = [ST_PENDING if i == 10 else ST_COMPLETED for i in range(N)]


def _owns(i, **kw):
    return replay_owns(DETECTED[i], detected=DETECTED, statuses=STATUSES, **kw)


# ══ the three clauses ════════════════════════════════════════════════════════

def test_a_drawn_setup_inside_a_current_recording_is_replay_owned():
    assert _owns(59) and _owns(30) and _owns(10 + 1)


def test_a_setup_outside_the_draw_window_returns_to_live():
    """`i_replayCount` draws the most recent N. Setup 0 of 60 is outside a
    50-window, so the recording covers it but nothing draws it — the live layer
    must take it or it disappears."""
    assert not _owns(0, replay_count=50)
    assert not _owns(9, replay_count=50)
    assert _owns(10 + 1, replay_count=50)
    # …and widening the window hands it back to the replay.
    assert _owns(0, replay_count=200)


def test_reducing_the_replay_count_transfers_ownership_cleanly():
    """Every setup must be owned by exactly one layer at every setting."""
    for count in (25, 50, 100, 200):
        for i in range(N):
            drawn_by_replay = _owns(i, replay_count=count)
            expected = (i >= max(0, N - count)) and STATUSES[i] != ST_PENDING
            assert drawn_by_replay == expected, (count, i)


def test_a_stale_recording_never_suppresses_live_setups():
    """THE safety property the old blanket ban existed to protect. A recording
    from another engine describes decisions this build no longer makes."""
    for i in range(N):
        assert not _owns(i, stale=True)


def test_replay_disabled_returns_everything_to_live():
    for i in range(N):
        assert not _owns(i, show_replay=False)


def test_an_empty_recording_returns_everything_to_live():
    assert not replay_owns(DETECTED[0], detected=[], statuses=[], rp_count=0)


def test_a_hidden_PENDING_setup_stays_with_the_live_layer():
    """PENDING setups are not drawn unless asked for. Ceding on coverage alone
    would delete them from the chart entirely."""
    assert not _owns(10, show_pending=False)
    assert _owns(10, show_pending=True)


def test_detection_may_differ_by_one_bar_between_the_feeds():
    """The chart and production can disagree by a bar about which candle
    detected a block (L-02). An exact match would hand ownership to nobody."""
    assert replay_owns(DETECTED[30] + BAR_MS, detected=DETECTED,
                       statuses=STATUSES)
    assert replay_owns(DETECTED[30] - BAR_MS, detected=DETECTED,
                       statuses=STATUSES)
    # Setups here are an HOUR apart, so +3 bars from #30 is exactly one bar
    # short of #31 and would match IT. +2 bars is near neither.
    assert not replay_owns(DETECTED[30] + 2 * BAR_MS, detected=DETECTED,
                           statuses=STATUSES)


# ══ the two properties that matter ═══════════════════════════════════════════
#
# These model the two layers INDEPENDENTLY, from their own draw rules, and then
# compare. Deriving one from the other ("live draws it if replay does not") and
# asserting they differ proves nothing at all — it restates the definition.


def replay_draws(i, *, statuses, n, stale=False, show_replay=True,
                 show_pending=False, replay_count=50):
    """Fragment 67's own gate: `if i >= rp_first` and `drawn = status !=
    RP_ST_PENDING or i_showPending`, under `i_showReplay and RP_COUNT > 0`."""
    if not show_replay or stale or n == 0:
        return False
    if i < max(0, n - replay_count):
        return False
    return statuses[i] != ST_PENDING or show_pending


def live_draws(i, *, show_live=True, **kw):
    """Fragment 68's own gate: `if i_showLive` … `if not f_replayOwns(time)`."""
    return show_live and not _owns(i, **kw)


def test_no_setup_is_rendered_by_both_layers():
    """Each layer is asked separately, from its own rule."""
    both = []
    for count in (25, 50, 100, 200):
        for pending in (False, True):
            for i in range(N):
                r = replay_draws(i, statuses=STATUSES, n=N,
                                 show_pending=pending, replay_count=count)
                l = live_draws(i, show_pending=pending, replay_count=count)
                if r and l:
                    both.append((count, pending, i))
    assert not both, f"{len(both)} setups drawn twice, e.g. {both[:3]}"


def test_no_setup_disappears_while_the_live_layer_is_on():
    """A setup owned by nobody is the failure the third clause exists to
    prevent. Turning the live layer OFF is the operator's choice and is
    excluded; everything else must be covered."""
    orphans = []
    for count in (25, 50, 100, 200):
        for pending in (False, True):
            for stale in (False, True):
                for show in (False, True):
                    for i in range(N):
                        r = replay_draws(i, statuses=STATUSES, n=N,
                                         stale=stale, show_replay=show,
                                         show_pending=pending,
                                         replay_count=count)
                        l = live_draws(i, stale=stale, show_replay=show,
                                       show_pending=pending,
                                       replay_count=count)
                        if not r and not l:
                            orphans.append((count, pending, stale, show, i))
    assert not orphans, f"{len(orphans)} setups drawn by NOBODY, e.g. {orphans[:3]}"


def test_the_exclusivity_check_is_not_vacuous():
    """Proof the two tests above can fail: a coverage-only rule — ceding
    ownership without checking the draw window or the pending filter — orphans
    every setup the replay holds but does not draw."""
    def coverage_only(i, **kw):
        return True          # "the recording contains it, so replay owns it"

    orphans = [i for i in range(N)
               if not replay_draws(i, statuses=STATUSES, n=N, replay_count=50)
               and not (True and not coverage_only(i))]
    assert len(orphans) >= 11, len(orphans)


def test_the_live_layer_creates_nothing_when_the_replay_owns():
    """Suppressing after creation would leave an orphan and a window in which
    both had drawn. Not creating is the only form with neither."""
    body = _frag("68_live_setups")
    i = body.index("if CTX_OK and barClosed and i_showLive and s5_created_count")
    block = body[i:i + 900]
    assert "if not f_replayOwns(time)" in block
    assert block.index("if not f_replayOwns(time)") < block.index("f_liveCreate(")


# ══ provenance is visible ════════════════════════════════════════════════════

def test_each_layer_declares_what_it_is():
    assert 'source: REPLAY · M1 authority' in _frag("67_replay_visuals")
    assert 'source: LIVE · M15 inference' in _frag("68_live_setups")


def test_the_hud_states_the_split():
    hud = _frag("99_hud")
    assert 'f_row(r, "authority"' in hud
    assert "REPLAY · M1 authority where drawn, else LIVE · M15" in hud
    assert "recording STALE" in hud and "replay off" in hud


def test_the_undecidable_wording_states_what_is_unknown():
    """Not "assumed" — nothing is assumed. The ordering is unknown at M15."""
    body = _frag("68_live_setups")
    assert '"M15 cannot order fill vs invalidation"' in body
    assert '"M15 cannot prove which bar filled"' in body
    # "assumed" must not appear in anything the operator READS. It survives in
    # one comment explaining why the arm bar cannot be assumed to be the fill,
    # which is the opposite claim.
    label = body[body.index("f_liveText(LiveOb o"):body.index("f_liveTip(LiveOb o")]
    tip = body[body.index("f_liveTip(LiveOb o"):body.index("// ── create, on the bar")]
    for text in (label, tip):
        quoted = " ".join(text.split('"')[1::2])
        assert "assumed" not in quoted.lower(), quoted[:200]


# ══ CE10156, the fourth Pine parse error this project has hit ════════════════
#
# Pine continues a statement onto the next line only while that line is indented
# MORE THAN THE LINE THAT STARTED THE STATEMENT. A trailing `+` followed by a
# same-indent line is two statements, the first of which ends in an operator.
# It looks completely reasonable in the source, which is why the linter has to
# know about it — the previous three (CE10088, CE10172, CE10272) each cost a
# paste-and-fail round trip before getting a rule.

from tools.oracle.lint_pine import lint  # noqa: E402

HDR = '//@version=6\nindicator("x")\n'


@pytest.mark.parametrize("name,src", [
    ("same indent in a function body",
     HDR + 'f_x() =>\n    a = 1\n    "lead" +\n    (a > 0 ? "y" : "n")\n'),
    ("same indent at top level", HDR + "x = 1 +\n2\n"),
])
def test_the_linter_catches_a_broken_continuation(name, src):
    assert [f for f in lint(src) if f["rule"] == "line_continuation"], name


@pytest.mark.parametrize("name,src", [
    ("continuation indented deeper",
     HDR + 'f_x() =>\n    a = 1\n    "lead" +\n     (a > 0 ? "y" : "n")\n'),
    # An expression may indent 9 then 5 and still continue a statement that
    # began at 4. Comparing against the PRECEDING line flags sixteen healthy
    # continuations in this build; the comparison is against the STATEMENT.
    ("9 then 5, both continuing a statement that began at 4",
     HDR + 'f_x() =>\n    a = 1\n    (a > 0\n         ? "y"\n     : "n") +\n'
           '     "tail" +\n         "more" +\n     "end"\n'),
    ("inside brackets, where Pine allows free newlines",
     HDR + 'plot(close, title =\n     "a" +\n     "b")\n'),
    ("a nested block body", HDR + "f_x() =>\n    if close > 0\n"
                                  "        a = 1 +\n             2\n        a\n"),
])
def test_the_continuation_rule_has_no_false_positives(name, src):
    assert not [f for f in lint(src) if f["rule"] == "line_continuation"], name


def test_the_generated_build_has_no_broken_continuations():
    gen = (CT_ROOT / "pine" / "generated"
           / "tradingview_visual_oracle_detection_15m.pine")
    if not gen.is_file():
        pytest.skip("not generated")
    bad = [f for f in lint(gen.read_text(encoding="utf-8"))
           if f["rule"] == "line_continuation"]
    assert not bad, bad[:3]
