"""The 2026-07-29 parity case, end to end.

Production (`live_state/frames/prev_trades.csv`, trade S_2094):

    bearish CHoCH   detected 2026-07-16 12:30Z
    tapped 20:46 · ARMED 20:48 · left the block 20:50
    earliest legal fill = arm + 3 candles = 20:51 — price already gone
    FILLED 2026-07-29 22:03Z   session Outside   state Bear/Chop
    entry 1.14686  stop 1.14772  tp 1.14600  rr 1.0   WIN, net +0.9302R

The chart used to freeze the execution cell on the bar that ARMED. Here that is
20:45 — seventy-five minutes and five bars before production filled. In this
case the arm bar and the fill bar happen to agree on session and state, so the
selected cell was right by luck; across the 659 filled trades on record they
disagree 8.6% of the time.

These tests run the live layer's state machine — the same transcription
`test_oracle_live_lifecycle.py` keeps pinned to the fragment — over the REAL
15-minute bars, and assert it now lands on production's fill bar.
"""

from __future__ import annotations

import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from test_oracle_live_lifecycle import (ARMED, NEWS_OK,  # noqa: E402
                                        NEWS_UNKNOWN, RESTING, TRADE, WIN,
                                        Block, Ctx, run)
from tools.oracle import target_table as tt  # noqa: E402
from tools.oracle.engine_access import (_in_lux, load_engine,  # noqa: E402
                                        resolve_config)

LONDON = ZoneInfo("Europe/London")

#: The block, exactly as production recorded it.
TOP, BOTTOM = 1.14762, 1.14686
ENTRY, STOP, TP = 1.14686, 1.14772, 1.14600
ARM = 1.14705                      # bottom + 25% of a 7.6-pip block
FILL_BAR = "2026-07-29 22:00"      # the 15m bar containing the 22:03 fill
ARM_BAR = "2026-07-29 20:45"       # the 15m bar containing the 20:48 arm

#: The 15-minute bars from the arm to the fill, measured off the production
#: seam. Inlined so the test is deterministic and needs no candle file.
BARS = [
    ("2026-07-29 20:00", 1.14525, 1.14468),
    ("2026-07-29 20:15", 1.14562, 1.14486),
    ("2026-07-29 20:30", 1.14656, 1.14553),
    ("2026-07-29 20:45", 1.14707, 1.14642),   # ARMS (H >= 1.14705)
    ("2026-07-29 21:00", 1.14620, 1.14559),
    ("2026-07-29 21:15", 1.14628, 1.14561),
    ("2026-07-29 21:30", 1.14643, 1.14626),
    ("2026-07-29 21:45", 1.14639, 1.14594),
    ("2026-07-29 22:00", 1.14690, 1.14598),   # FILLS (range contains 1.14686)
    ("2026-07-29 22:15", 1.14691, 1.14672),
    ("2026-07-29 22:30", 1.14694, 1.14676),
    ("2026-07-29 22:45", 1.14714, 1.14694),
]


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def cfg(engine):
    return resolve_config(engine)[0]


@pytest.fixture(scope="module")
def table(engine, cfg):
    return tt.build(engine, cfg)


def _sessions(engine):
    with _in_lux(Path(engine.lux_root)):
        from strategy_core import sessions as mod
        return mod


# ══ the geometry is production's ═════════════════════════════════════════════

def test_the_block_yields_productions_entry_and_stop(cfg):
    """Bearish: entry is the BOTTOM edge, stop is the top plus the buffer."""
    buf = float(cfg.stop_buffer_pips) * float(cfg.pip_size)
    assert BOTTOM == pytest.approx(ENTRY)
    assert TOP + buf == pytest.approx(STOP)


def test_the_arm_level_is_25_percent_into_the_block(cfg):
    pct = float((getattr(cfg, "triggered_edge_trigger_thresholds", None)
                 or [0])[0])
    assert BOTTOM + (TOP - BOTTOM) * pct / 100.0 == pytest.approx(ARM, abs=1e-9)


# ══ session and state, at BOTH candidate instants ════════════════════════════

@pytest.mark.parametrize("stamp,london_hour", [
    ("2026-07-29 20:45", 21), ("2026-07-29 20:48", 21),
    ("2026-07-29 22:00", 23), ("2026-07-29 22:03", 23),
])
def test_every_candidate_instant_is_Outside_in_BST(engine, stamp, london_hour):
    """29 July is BST, so 22:03Z is 23:03 London. Production recorded the fill
    session as Outside; both engines must reach that from the London clock."""
    import pandas as pd
    s = _sessions(engine)
    ts = pd.Timestamp(stamp, tz="UTC")
    assert ts.astimezone(LONDON).hour == london_hour
    assert s._london_hour(ts) == london_hour
    assert s._session_for_hour(london_hour) == ("Outside", "outside")


def test_the_cohort_cell_is_productions(table):
    """The base cell is 2.0R; the CONFIRMED Bear/Chop state override takes it to
    1.0R, which is the `rr_multiple` production recorded."""
    base, src_b, ok_b, _ = tt.resolve(table, "outside", "CHoCH", "Short",
                                      None, False)
    rr, src, ok, why = tt.resolve(table, "outside", "CHoCH", "Short",
                                  "Bear/Chop", True)
    assert (base, src_b, ok_b) == (2.0, "BASE", True)
    assert (rr, src, ok, why) == (1.0, "STATE", True, "")
    assert ENTRY - (STOP - ENTRY) * rr == pytest.approx(TP, abs=1e-9)


def test_no_news_blackout_at_either_instant(engine, cfg):
    import pandas as pd
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.news import _news_blackout_match
        rows = list(engine.rb.load_news_events(cfg))
    for stamp in ("2026-07-29 20:48", "2026-07-29 22:03"):
        assert _news_blackout_match(pd.Timestamp(stamp), rows) is None


# ══ THE FIX: the chart resolves at the FILL bar, not the ARM bar ═════════════

def _run(rr=1.0, news=NEWS_OK, sess="outside", state="Bear/Chop"):
    bars = [(hi, lo) for _t, hi, lo in BARS]
    ctx = Ctx({0: (rr, True, news)},
              sessions={0: sess}, states={0: state})
    return run(Block(TOP, BOTTOM, False, stop_buffer=0.0001), bars, ctx)


def test_the_setup_arms_on_the_2045_bar_and_does_not_resolve_there():
    """The old model froze the cell here. It must now only mark ARMED."""
    o = _run()
    assert BARS[o.armed_bar][0] == ARM_BAR
    assert o.armed_bar == 3
    # …and the arm bar DID straddle the entry, which is why it cannot be
    # assumed to be the fill: 1.14642 <= 1.14686 <= 1.14707.
    assert o.arm_bar_straddled is True


def test_the_setup_fills_on_the_bar_production_filled_on():
    o = _run()
    assert BARS[o.resolved_bar][0] == FILL_BAR, (
        f"resolved on {BARS[o.resolved_bar][0]}, production filled at 22:03")
    assert o.resolved_bar == 8
    assert o.phase in (TRADE, WIN)


def test_the_fill_bar_is_five_bars_after_the_arm_bar():
    """75 minutes. Under the old model those five bars did not exist."""
    o = _run()
    assert o.resolved_bar - o.armed_bar == 5


def test_the_resolved_cell_matches_production():
    o = _run()
    assert o.final["sess"] == "outside"
    assert o.final["state"] == "Bear/Chop"
    assert o.final["ok"] is True
    assert o.final["rr"] == 1.0
    assert o.target == pytest.approx(TP, abs=1e-9)


def test_the_setup_is_a_TRADE_and_not_blocked():
    assert _run().phase in (TRADE, WIN)


def test_it_is_decidable_because_both_candidate_bars_agree():
    """The arm bar could itself have filled, so there are two candidates. They
    agree on session, state, eligibility and target here, so the answer is the
    same either way and the setup resolves rather than reporting UNDECIDABLE."""
    o = _run()
    assert o.phase != 7, "UNDECIDABLE — the candidate bars disagreed"
    assert o.arm_cell == (o.final["sess"], o.final["state"], True, 1.0)


def test_unknown_news_does_not_suppress_it():
    """The embedded schedule ends 2026-05-20, so this bar is NEWS UNKNOWN. That
    must qualify the label, not delete the trade."""
    o = _run(news=NEWS_UNKNOWN)
    assert o.phase in (TRADE, WIN)
    assert o.final["news"] == NEWS_UNKNOWN


def test_the_old_arm_time_model_would_have_resolved_five_bars_early():
    """Proof the tests above are not vacuous: the rejected model resolved on the
    first bar that armed, which is 20:45."""
    armed_at = next(i for i, (_t, hi, _lo) in enumerate(BARS)
                    if i > 0 and hi >= ARM)
    assert BARS[armed_at][0] == ARM_BAR
    assert armed_at != 8


# ══ a case where it genuinely cannot be decided ══════════════════════════════

def test_a_disagreement_between_the_candidate_bars_reports_UNDECIDABLE():
    """Same bars, but the session rolls between the arm and the fill. The chart
    cannot say which cell production used and must not pick one."""
    bars = [(hi, lo) for _t, hi, lo in BARS]
    ctx = Ctx({0: (2.0, True, NEWS_OK), 5: (1.0, True, NEWS_OK)},
              sessions={0: "ny_pm", 5: "outside"},
              states={0: "Bear/Chop"})
    o = run(Block(TOP, BOTTOM, False, stop_buffer=0.0001), bars, ctx)
    assert o.phase == 7, f"expected UNDECIDABLE, got phase {o.phase}"
    assert o.final["why"] == "M15 cannot prove which bar filled"


def test_an_arm_bar_that_cannot_have_filled_is_fully_decidable():
    """When the arm bar sits entirely on one side of the entry no fill could
    have happened in it, so the next straddle is proof and a session change
    between them costs nothing."""
    bars = [(1.0860, 1.0850)] * 3 + [(1.0930, 1.0910)] + [(1.0860, 1.0850)] * 4
    bars += [(1.0905, 1.0895)] + [(1.0860, 1.0850)] * 3
    ctx = Ctx({0: (2.0, True, NEWS_OK), 5: (1.0, True, NEWS_OK)},
              sessions={0: "ny_pm", 5: "outside"}, states={0: "Bear/Chop"})
    o = run(Block(1.10, 1.09, False), bars, ctx)
    assert o.arm_bar_straddled is False
    assert o.phase == TRADE and o.final["sess"] == "outside"
    assert o.final["rr"] == 1.0


# ══ the recording now reaches the trade ══════════════════════════════════════
#
# It could not before: `export_replay` ran over production's FROZEN candle file,
# which ends 2026-06-19 09:53, and this order block was detected 2026-07-16
# 12:30 — twenty-seven days out of range. Nothing about the lifecycle was wrong
# there; the recording's INPUT stopped before the trade existed.

import json  # noqa: E402


@pytest.fixture(scope="module")
def recording():
    path = CT_ROOT / "artifacts" / "tradingview_oracle" / "replay_setups.json"
    if not path.is_file():
        pytest.skip("no recording")
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def recorded(recording):
    import datetime as dt
    want = dt.datetime(2026, 7, 16, 12, 30,
                       tzinfo=dt.timezone.utc).timestamp() * 1000
    hits = [x for x in recording["setups"]
            if abs(x["detected_ms"] - want) < 60_000]
    assert len(hits) == 1, f"{len(hits)} setups detected at 2026-07-16 12:30"
    return hits[0]


def test_the_recording_extends_past_the_frozen_candle_file(recording):
    seam = recording.get("seam")
    assert seam, "no seam — the recording is still frozen-file only"
    assert seam["frozen_end"].startswith("2026-06-19")
    assert seam["live_rows"] > 40_000
    assert seam["live_source"] and seam["live_sha256"], \
        "the live segment must be identified, not blended in silently"
    assert "LIVE MT5" in seam["_note"]


def test_the_seam_is_declared_in_the_build():
    """A recording that spans two feeds must say so on the chart."""
    gen = (CT_ROOT / "pine" / "generated"
           / "tradingview_visual_oracle_detection_15m.pine")
    if not gen.is_file():
        pytest.skip("not generated")
    text = gen.read_text(encoding="utf-8")
    assert "RP_SEAM_MS         = 1781862780000" in text
    assert 'RP_SEAM_TXT        = "2026-06-19"' in text
    assert 'f_row(r, "feed seam", "frozen to " + RP_SEAM_TXT' in text


def test_the_july_trade_is_in_the_recording(recorded):
    """OBLIGATION A. Every field against production's own frame row S_2094."""
    assert recorded["status"] == "COMPLETED"
    assert recorded["outcome"] == "WIN"
    assert recorded["direction"] == 2          # bearish
    assert recorded["structure"] == 2          # CHoCH
    assert recorded["entry"] == pytest.approx(ENTRY)
    assert recorded["stop"] == pytest.approx(STOP)
    assert recorded["target"] == pytest.approx(TP)
    assert recorded["rr"] == pytest.approx(1.0)
    assert recorded["session"] == "Outside"
    assert recorded["cohort"] == "EURUSD|outside|choch_short"
    assert recorded["net_r"] == pytest.approx(0.9302, abs=5e-5)


def test_the_recorded_timings_are_productions(recorded):
    import datetime as dt

    def utc(ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M")

    assert utc(recorded["detected_ms"]) == "2026-07-16 12:30"
    assert utc(recorded["armed_ms"]) == "2026-07-29 20:48"
    assert utc(recorded["fill_ms"]) == "2026-07-29 22:03"
    assert utc(recorded["exit_ms"]) == "2026-07-30 00:26"
    # 75 minutes between the arm and the fill — the gap the chart used to erase.
    assert (recorded["fill_ms"] - recorded["armed_ms"]) / 60_000 == 75


def test_the_run_local_ob_id_confirms_the_renumbering(recorded):
    """The Research Lab's "OB-149" is run-local. This recording starts
    2025-09-30 and numbers the same block 124, which is exactly what a scan of
    production's full frame predicts for that start date — so the identifier is
    a function of the run window, not of the trade."""
    assert recorded["ob_id"] == 124


# ══ the 2026-07-02 class-D case ══════════════════════════════════════════════
#
# Production (ob_id 115, Long CHoCH, detected 2026-07-02 08:00):
#     armed 2026-07-23 12:48 · FILLED 13:06 · stopped 13:11 · LOSS, net -1.1333R
#
# The 15-minute bar at 13:00 holds BOTH events. Its 1-minute detail:
#     13:06  L 1.13752  straddles the entry 1.13771   <- production fills
#     13:11  L 1.13723  below the far edge 1.13736    <- and then stops out
#
# OHLC cannot carry that ordering. Preferring `through` reported INVALIDATED,
# which is the chart claiming an ordering it cannot see — the same defect as
# the arm-bar case, and it gets the same answer: UNDECIDABLE.

D_TOP, D_BOTTOM = 1.13771, 1.13736
D_ENTRY, D_STOP = 1.13771, 1.13726
D_BARS = [
    ("2026-07-23 12:30", 1.13856, 1.13769),   # straddles, does not arm
    ("2026-07-23 12:45", 1.13859, 1.13756),   # ARMS
    ("2026-07-23 13:00", 1.13862, 1.13679),   # straddle AND through
]


def test_the_0702_bar_holds_both_the_fill_and_the_breach():
    """Stated from the numbers, so the premise cannot rot."""
    hi, lo = D_BARS[2][1], D_BARS[2][2]
    assert lo <= D_ENTRY <= hi, "the bar's range contains the entry"
    assert lo < D_BOTTOM, "…and it breaches the far edge"


def test_the_0702_case_is_UNDECIDABLE_not_INVALIDATED():
    o = run(Block(D_TOP, D_BOTTOM, True, stop_buffer=0.0001),
            [(hi, lo) for _t, hi, lo in D_BARS],
            Ctx({0: (2.0, True, NEWS_UNKNOWN)},
                sessions={0: "newYork"}, states={0: "Bull/Expand"}))
    assert o.phase == 7, f"expected UNDECIDABLE, got phase {o.phase}"
    assert o.final["why"] == "M15 cannot order fill vs invalidation"
    assert o.armed_bar == 1, "it armed on the 12:45 bar"
    assert o.resolved_bar == 2, "and ended on the 13:00 bar"


def test_the_0702_case_is_not_read_as_a_LOSS_either():
    """Production's answer was LOSS. Inferring it would be just as unproven as
    inferring the invalidation — the chart must decline, not guess right."""
    o = run(Block(D_TOP, D_BOTTOM, True, stop_buffer=0.0001),
            [(hi, lo) for _t, hi, lo in D_BARS],
            Ctx({0: (2.0, True, NEWS_UNKNOWN)}))
    assert o.phase not in (4, 5, 6), "no WIN, LOSS or INVALIDATED may be claimed"
