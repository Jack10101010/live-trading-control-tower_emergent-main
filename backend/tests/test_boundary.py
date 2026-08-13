"""C2 — standalone verification of the candidate-boundary helper
(`live.runner.latest_closed_boundary`). Fast, pure, no pipeline/IO.

The helper's formula is preserved byte-for-byte by C2; these tests pin its exact
existing semantics (equivalence to the inline formula, edge cases, and structural
invariants) so C3 can build the InputRevision gate on a locked contract.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.runner import FIFTEEN_MIN, latest_closed_boundary        # noqa: E402

ONE_MIN = pd.Timedelta(minutes=1)


def _reference(last_m1_time):
    """Independent restatement of the exact production formula — guards against
    any future accidental drift in `latest_closed_boundary`."""
    return (last_m1_time + pd.Timedelta(minutes=1)).floor("15min") - pd.Timedelta(minutes=15)


def _grid(tz="UTC"):
    """A full day at 1-minute resolution → every phase within the 15m grid."""
    base = pd.Timestamp("2026-07-17 00:00:00", tz=tz)
    return [base + pd.Timedelta(minutes=m) for m in range(24 * 60)]


# ── equivalence to the byte-preserved formula ────────────────────────────────
def test_equivalence_grid_utc():
    for t in _grid("UTC"):
        assert latest_closed_boundary(t) == _reference(t)


def test_equivalence_grid_naive():
    for t in _grid(None):
        assert latest_closed_boundary(t) == _reference(t)


# ── canonical cases (mirrors the existing runner semantics; new file) ────────
@pytest.mark.parametrize("last_m1,expected", [
    ("2026-07-17 10:14:00", "2026-07-17 10:00:00"),   # 10:00 bar closed at 10:15
    ("2026-07-17 10:15:00", "2026-07-17 10:00:00"),   # 10:15 bar still open
    ("2026-07-17 10:29:00", "2026-07-17 10:15:00"),
    ("2026-07-17 10:06:00", "2026-07-17 09:45:00"),
])
def test_canonical_cases(last_m1, expected):
    assert latest_closed_boundary(pd.Timestamp(last_m1)) == pd.Timestamp(expected)


def test_exact_boundary_input():
    # last M1 open sits exactly on a 15m grid line → only that bar's first minute
    # is present, so the latest *closed* 15m bar is the previous one.
    assert latest_closed_boundary(pd.Timestamp("2026-07-17 10:00:00")) == pd.Timestamp("2026-07-17 09:45:00")
    assert latest_closed_boundary(pd.Timestamp("2026-07-17 10:15:00")) == pd.Timestamp("2026-07-17 10:00:00")


def test_between_boundaries_input():
    assert latest_closed_boundary(pd.Timestamp("2026-07-17 10:06:00")) == pd.Timestamp("2026-07-17 09:45:00")
    assert latest_closed_boundary(pd.Timestamp("2026-07-17 10:22:00")) == pd.Timestamp("2026-07-17 10:00:00")


# ── timezone behaviour (no conversion; tz preserved) ─────────────────────────
def test_timezone_aware_preserved():
    b = latest_closed_boundary(pd.Timestamp("2026-07-17 10:29:00", tz="UTC"))
    assert b == pd.Timestamp("2026-07-17 10:15:00", tz="UTC")
    assert str(b.tz) == "UTC"


def test_timezone_naive_preserved():
    b = latest_closed_boundary(pd.Timestamp("2026-07-17 10:29:00"))
    assert b == pd.Timestamp("2026-07-17 10:15:00")
    assert b.tz is None


# ── NaT propagation (empty-frame .max() → NaT) ───────────────────────────────
def test_nat_propagation():
    assert pd.isna(latest_closed_boundary(pd.NaT))


# ── caller invariance to duplicate / unsorted timestamps (via .max()) ────────
def test_duplicate_unsorted_caller_invariance():
    times = pd.to_datetime(pd.Series([
        "2026-07-17 10:06:00+00:00", "2026-07-17 10:00:00+00:00",
        "2026-07-17 10:06:00+00:00", "2026-07-17 09:59:00+00:00"]))
    # run_once feeds `candles["time"].max()`; order and duplicates cannot change it.
    assert latest_closed_boundary(times.max()) == latest_closed_boundary(
        pd.Timestamp("2026-07-17 10:06:00+00:00"))


# ── structural invariants over the whole grid ────────────────────────────────
def test_invariant_lands_on_15min_grid():
    for t in _grid("UTC"):
        b = latest_closed_boundary(t)
        assert b.minute % 15 == 0 and b.second == 0
        assert b.microsecond == 0 and b.nanosecond == 0


def test_invariant_never_after_supplied_timestamp():
    for t in _grid("UTC"):
        assert latest_closed_boundary(t) <= t


def test_invariant_latest_closed_bar():
    # Precise, always-true characterization of "latest CLOSED 15m bar":
    #   the boundary bar [b, b+15m) is closed by the data (b+15m <= last_m1+1min),
    #   and the NEXT bar is not yet closed (b+30m > last_m1+1min).
    # NOTE: the boundary b is therefore ALWAYS 15–30 min before (last_m1 + 1min);
    # the "<=15 min from (last_m1+1min)" phrasing in the C2 plan describes
    # floor(next_open) — one 15m bar ABOVE the boundary — not b itself.
    for t in _grid("UTC"):
        b = latest_closed_boundary(t)
        next_open = t + ONE_MIN
        assert b + FIFTEEN_MIN <= next_open                    # boundary bar is closed
        assert b + 2 * FIFTEEN_MIN > next_open                 # next bar is not yet closed
        assert FIFTEEN_MIN <= (next_open - b) < 2 * FIFTEEN_MIN
