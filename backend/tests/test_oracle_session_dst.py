"""Session parity after M-SESSION-DST-1: London wall-clock, not UTC.

Production classifies on `ZoneInfo("Europe/London")` (`strategy_core/sessions.py`,
commit e76fa92 on `golden-run-001-engine`). The Pine classifier read the UTC hour
until M-TV-FILL-TIME-SEMANTICS-1, which put roughly seven months a year into the
wrong cohort — silently, because both answers are valid session keys and nothing
downstream could tell them apart.

The load-bearing test is `test_the_utc_boundary_moves_by_an_hour_between_gmt_and_bst`:
it is the one that fails for every approximation anybody reaches for — a fixed
offset, a month test, or a hand-written transition table.
"""

from __future__ import annotations

import datetime as dt
import re
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle.engine_access import _in_lux, load_engine  # noqa: E402

SRC = CT_ROOT / "pine" / "src"
LONDON = ZoneInfo("Europe/London")

#: The canonical schedule, LONDON LOCAL, half-open [start, end).
SCHEDULE = ((0, 7, "asia"), (7, 10, "london"), (10, 12, "lull"),
            (12, 15, "newYork"), (15, 17, "ny_pm"))


def _frag(name: str) -> str:
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def sessions(engine):
    """Production's own module, imported from the pinned tree."""
    with _in_lux(Path(engine.lux_root)):
        from strategy_core import sessions as mod
        return mod


def _pine_session_key(utc_ts: dt.datetime) -> str:
    """The Pine classifier, transcribed.

        londonHour = hour(time, "Europe/London")        # fragment 30
        sessIdx    = f_sessionIndexForHour(londonHour)  # fragment 40

    Transcribed rather than described, because the previous version of this
    fragment passed a test that only checked the source contained the right
    words while it was classifying on the wrong hour.
    """
    london_hour = utc_ts.astimezone(LONDON).hour
    for start, end, key in SCHEDULE:
        if start <= london_hour < end:
            return key
    return "outside"


def _utc(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc)


# ══ production is what the brief says it is ══════════════════════════════════

def test_production_uses_the_iana_zone_not_an_offset(sessions):
    src = Path(sessions.__file__).read_text(encoding="utf-8")
    assert 'ZoneInfo("Europe/London")' in src
    assert sessions._LONDON.key == "Europe/London"
    # None of the approximations the brief forbids.
    body = "\n".join(l for l in src.splitlines()
                     if not l.strip().startswith("#"))
    for banned in ("timedelta(hours=1)", "month in", "is_summer", "BST_START"):
        assert banned not in body, banned


def test_production_schedule_is_the_canonical_london_windows(sessions):
    got = [(s, e, k) for s, e, _lbl, k in sessions._SESSION_SCHEDULE]
    assert got == list(SCHEDULE)
    assert sessions._SESSION_OUTSIDE[1] == "outside"


def test_the_pinned_engine_carries_the_dst_commit(engine):
    """The pin must cover sessions.py, or a chart could be generated against a
    tree whose classifier nobody verified."""
    import subprocess
    out = subprocess.run(
        ["git", "log", "-1", "--format=%H %s", "--", "strategy_core/sessions.py"],
        cwd=engine.lux_root, capture_output=True, text=True, check=True).stdout
    assert "M-SESSION-DST-1" in out, out
    branch = subprocess.run(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                            cwd=engine.lux_root, capture_output=True,
                            text=True, check=True).stdout.strip()
    assert branch == "golden-run-001-engine", branch


# ══ the transcription matches the Pine source ════════════════════════════════

def test_pine_classifies_on_the_london_hour(engine):
    time_frag = _frag("30_time")
    sess_frag = _frag("40_sessions")
    assert 'londonHour = hour(time, "Europe/London")' in time_frag
    assert "sessIdx = f_sessionIndexForHour(londonHour)" in sess_frag
    # …and no longer on the UTC hour.
    assert "f_sessionIndexForHour(utcHour)" not in sess_frag


def test_pine_does_not_approximate_the_transition():
    """A month test, a fixed +1, or a hand-rolled table would each show up as
    one of these. `hour(time, "Europe/London")` is the only mechanism allowed."""
    body = "\n".join(l for l in (_frag("30_time") + _frag("40_sessions")).splitlines()
                     if not l.strip().startswith("//"))
    # `month(time, "UTC")` is legitimate — it builds the UTC day key. What must
    # not exist is the month being used to DECIDE the offset.
    for banned in ("isSummer", "BST", "dstOffset", "summerOffset",
                   "utcMonth >=", "utcMonth <", "utcMonth =="):
        assert banned not in body, banned
    assert len(re.findall(r'hour\(time,\s*"Europe/London"\)', body)) == 1


# ══ agreement with production, instant by instant ════════════════════════════

def _agree(sessions, utc_ts):
    prod = sessions._cohort_session_key({"time": utc_ts})
    pine = _pine_session_key(utc_ts)
    return prod, pine


@pytest.mark.parametrize("local_h,local_m,expect", [
    (9, 59, "london"), (10, 0, "lull"), (11, 59, "lull"), (12, 0, "newYork"),
    (6, 59, "asia"), (7, 0, "london"), (14, 59, "newYork"), (15, 0, "ny_pm"),
    (16, 59, "ny_pm"), (17, 0, "outside"), (23, 59, "outside"), (0, 0, "asia"),
])
def test_summer_boundaries_are_london_local(sessions, local_h, local_m, expect):
    """BST. 2026-07-01 is unambiguously inside British Summer Time."""
    local = dt.datetime(2026, 7, 1, local_h, local_m, tzinfo=LONDON)
    utc_ts = local.astimezone(dt.timezone.utc)
    prod, pine = _agree(sessions, utc_ts)
    assert prod == expect == pine, (
        f"London {local_h:02d}:{local_m:02d} BST = {utc_ts:%H:%M}Z -> "
        f"production {prod}, pine {pine}, expected {expect}")


@pytest.mark.parametrize("local_h,local_m,expect", [
    (9, 59, "london"), (10, 0, "lull"), (11, 59, "lull"), (12, 0, "newYork"),
    (6, 59, "asia"), (7, 0, "london"), (14, 59, "newYork"), (15, 0, "ny_pm"),
    (16, 59, "ny_pm"), (17, 0, "outside"), (23, 59, "outside"), (0, 0, "asia"),
])
def test_winter_boundaries_are_the_same_london_local(sessions, local_h, local_m,
                                                     expect):
    """GMT. The LOCAL boundaries are identical to summer — that is the point."""
    local = dt.datetime(2026, 1, 15, local_h, local_m, tzinfo=LONDON)
    utc_ts = local.astimezone(dt.timezone.utc)
    prod, pine = _agree(sessions, utc_ts)
    assert prod == expect == pine


def test_the_utc_boundary_moves_by_an_hour_between_gmt_and_bst(sessions):
    """THE test. Same LOCAL boundary, one hour apart in UTC — which is exactly
    what a UTC-hour classifier cannot represent and what every approximation
    gets wrong at the edges."""
    winter = dt.datetime(2026, 1, 15, 10, 0, tzinfo=LONDON).astimezone(dt.timezone.utc)
    summer = dt.datetime(2026, 7, 1, 10, 0, tzinfo=LONDON).astimezone(dt.timezone.utc)
    assert winter.hour == 10 and summer.hour == 9, (winter, summer)
    for ts in (winter, summer):
        assert sessions._cohort_session_key({"time": ts}) == "lull"
        assert _pine_session_key(ts) == "lull"
    # …and the UTC instant one hour EARLIER is Lull in summer but still London
    # in winter. A fixed-UTC chart calls both "London".
    assert sessions._cohort_session_key({"time": _utc(2026, 7, 1, 9)}) == "lull"
    assert sessions._cohort_session_key({"time": _utc(2026, 1, 15, 9)}) == "london"
    assert _pine_session_key(_utc(2026, 7, 1, 9)) == "lull"
    assert _pine_session_key(_utc(2026, 1, 15, 9)) == "london"


def test_the_old_utc_classifier_would_fail_this(sessions):
    """The defect, pinned. If someone reverts fragment 40 to `utcHour` this is
    the disagreement they reintroduce."""
    def utc_classifier(ts):
        for start, end, key in SCHEDULE:
            if start <= ts.hour < end:
                return key
        return "outside"

    disagree = 0
    ts = _utc(2026, 6, 1, 0)
    for _ in range(24 * 30):
        if utc_classifier(ts) != sessions._cohort_session_key({"time": ts}):
            disagree += 1
        ts += dt.timedelta(hours=1)
    # One hour in every 24 lands in a different window during BST.
    assert disagree >= 100, disagree


# ══ the transitions themselves ═══════════════════════════════════════════════
#
# UK transitions are at 01:00 UTC on the last Sunday of March and October. These
# are deterministic instants, not "some time in spring".

SPRING_2026 = _utc(2026, 3, 29, 1)     # GMT -> BST
AUTUMN_2026 = _utc(2026, 10, 25, 1)    # BST -> GMT


def test_spring_forward_flips_the_classification_at_the_exact_instant(sessions):
    before = SPRING_2026 - dt.timedelta(minutes=1)   # 00:59 UTC = 00:59 London
    after = SPRING_2026                              # 01:00 UTC = 02:00 London
    assert before.astimezone(LONDON).hour == 0
    assert after.astimezone(LONDON).hour == 2
    for ts in (before, after):
        assert sessions._cohort_session_key({"time": ts}) == _pine_session_key(ts)
    # 07:00 UTC is London in GMT and Lull-adjacent in BST: on the day AFTER the
    # switch, 07:00 UTC is 08:00 London, still London; 06:00 UTC is 07:00 London.
    assert sessions._cohort_session_key({"time": _utc(2026, 3, 28, 6)}) == "asia"
    assert sessions._cohort_session_key({"time": _utc(2026, 3, 30, 6)}) == "london"
    assert _pine_session_key(_utc(2026, 3, 28, 6)) == "asia"
    assert _pine_session_key(_utc(2026, 3, 30, 6)) == "london"


def test_autumn_back_flips_it_the_other_way(sessions):
    assert (AUTUMN_2026 - dt.timedelta(minutes=1)).astimezone(LONDON).hour == 1
    assert AUTUMN_2026.astimezone(LONDON).hour == 1     # the repeated hour
    # 09:00 UTC: Lull the day before (10:00 London BST), London the day after
    # (09:00 London GMT).
    assert sessions._cohort_session_key({"time": _utc(2026, 10, 23, 9)}) == "lull"
    assert sessions._cohort_session_key({"time": _utc(2026, 10, 26, 9)}) == "london"
    assert _pine_session_key(_utc(2026, 10, 23, 9)) == "lull"
    assert _pine_session_key(_utc(2026, 10, 26, 9)) == "london"


def test_the_repeated_autumn_hour_needs_no_fold_heuristic(sessions):
    """Both instants of the repeated 01:00-02:00 London hour are unambiguous
    UTC instants, so conversion FROM UTC is exact in both."""
    first = _utc(2026, 10, 25, 0, 30)    # 01:30 BST
    second = _utc(2026, 10, 25, 1, 30)   # 01:30 GMT
    assert first.astimezone(LONDON).hour == second.astimezone(LONDON).hour == 1
    for ts in (first, second):
        assert sessions._cohort_session_key({"time": ts}) == "asia"
        assert _pine_session_key(ts) == "asia"


def test_pine_and_production_agree_over_two_full_years(sessions):
    """Hourly, across four transitions. Nothing sampled, nothing hand-picked."""
    ts = _utc(2025, 1, 1, 0)
    end = _utc(2027, 1, 1, 0)
    checked = 0
    while ts < end:
        prod = sessions._cohort_session_key({"time": ts})
        pine = _pine_session_key(ts)
        assert prod == pine, f"{ts}: production {prod}, pine {pine}"
        checked += 1
        ts += dt.timedelta(hours=1)
    assert checked == 730 * 24, checked   # 2025 and 2026, neither a leap year


# ══ the oracle's own reference had the same bug ══════════════════════════════

def test_the_trace_exporter_goes_through_the_london_converter():
    """`export_trace` derived the session from `ts.hour`, which would have
    scored a UTC-classifying chart as CORRECT."""
    raw = (CT_ROOT / "tools" / "oracle" / "export_trace.py").read_text(
        encoding="utf-8")
    assert "from strategy_core.sessions import _london_hour" in raw
    assert "core._session_for_hour(_london_hour(ts))" in raw
    # Code only — the comment above the call quotes the defect on purpose.
    code = "\n".join(l for l in raw.splitlines()
                     if not l.strip().startswith("#"))
    assert "core._session_for_hour(ts.hour)" not in code


def test_the_hud_reports_the_london_clock_and_its_offset():
    body = _frag("99_hud")
    assert "londonHour" in body and "londonOffsetH" in body
    assert '" London"' in body
    # The old label claimed the window hours were UTC ("z"), which was wrong
    # for every BST bar.
    assert '+ "z"' not in body
