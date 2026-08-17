"""Bounded live working set — the ONE place live data limits are declared.

M-LIVE-BOUNDED-WORKING-SET-1.

Why this module exists
----------------------
The live node used to feed the entire 11-year archive (4.33M M1 rows, 493 MB
in memory) through the strategy pipeline on every 15-minute boundary. That
archive exists to produce a broad BACKTEST SAMPLE; it was never meant to sit
inside the live execution clock. Measured cost of the old path, before the
simulation stage even starts:

    load      20.96 s
    prepare   50.48 s
    resample   0.95 s
    detector 156.92 s   (289,674 M15 bars)
    -------------------
    subtotal 229.31 s        total run_once warm median ~1119 s

This module declares the bounded replacement and — just as importantly — makes
accidental reintroduction of the archive into the live path *fail loudly*
rather than silently costing twenty minutes again.

Choice of limits (each is measured, not guessed — see the milestone report)
--------------------------------------------------------------------------
M15_DETECTOR_BARS = 8000
    Detector parity against the full-history detector is EXACT at 2k/4k/8k.
    8,000 bars (~83 days) is a conservative operational margin over the ~2,000
    at which the recent OB population converges. Runtime 4.44 s.

M15_WARMUP_BARS = 2000
    The detector carries sequential state (current leg, swing trend bias), so
    OBs detected very close to the START of a bounded window can disagree with
    the full-history detector. Measured: every divergence across 8k/16k/24k
    windows occurred at offset < 500 bars; at offset >= 500 geometry AND
    structure_tag were 100% correct. 2,000 is a 4x margin, and still leaves
    6,000 trusted bars (~62 days) of detection horizon.

M1_WINDOW_DAYS = 120
    M15 bars only exist during market hours, so calendar days and bar counts
    are not interchangeable: 90 days of live M1 yields only 6,239 M15 bars,
    which would silently UNDER-FILL the 8,000-bar detector window and quietly
    shrink the trusted zone to ~44 days. 120 days yields ~8,300 M15 bars, so
    the configured window is actually achievable. Cost is ~124k M1 rows
    (~8 MB) — still ~35x below the archive and well inside the row ceiling.

DAILY history is deliberately NOT bounded
    Market state is a DAILY panel: 3,634 rows for the whole archive, computed
    in 0.257 s. Daily was never a cost problem, and bounding it is actively
    dangerous: EMA200 is recursive, so a fresh 300-bar seed reproduces the
    production EMA to only ~19.78 pips and flips categorical states. Keeping
    the full daily series is both cheaper to reason about and bit-exact. See
    live/daily_state.py.
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pandas as pd

# ── canonical limits — no scattered literals anywhere else ───────────────────
M15_DETECTOR_BARS = 8000        # bounded detector input
M15_WARMUP_BARS = 2000          # leading bars whose detections are NOT trusted
M1_WINDOW_DAYS = 120            # rolling live M1 history

#: Hard ceiling on the assembled live M1 frame. 120 days at the observed
#: 1,036 rows/day is ~124k rows; 250k gives ~2x tolerance for a denser feed or
#: a deliberately widened window, while still being ~17x below the 4.33M-row
#: archive. Exceeding it means archival history has leaked into the live path.
LIVE_M1_MAX_ROWS = 250_000

#: Detector input may never exceed the configured window.
LIVE_M15_MAX_ROWS = M15_DETECTOR_BARS

ARCHIVE_BASENAME = "EURUSD_1m_extended_2015_2026.csv"

M1_COLUMNS = ["time", "open", "high", "low", "close", "volume"]


class WorkingSetError(RuntimeError):
    """Bounded path refused to produce a frame. Always fail closed.

    Never caught-and-downgraded into "load the archive instead": that is the
    exact regression this milestone exists to prevent.
    """


def assert_within_ceiling(frame: pd.DataFrame, ceiling: int, what: str) -> None:
    """Refuse an oversized frame. Do NOT silently truncate.

    Truncating after loading would hide the defect (we would already have paid
    the load cost) and would quietly change which candles the strategy saw.
    """
    if len(frame) > ceiling:
        raise WorkingSetError(
            f"{what} has {len(frame):,} rows, ceiling is {ceiling:,} — refusing. "
            "This normally means archival history reached the live path.")


def _read_csv_tail(path: Path, want_bytes: int) -> pd.DataFrame:
    """Read approximately the last `want_bytes` of a CSV without parsing it all.

    Seeks from the end, discards the (probably partial) first line, and reuses
    the real header. Falls back to a whole-file read only when the file is
    already smaller than the requested tail.
    """
    size = os.path.getsize(path)
    with open(path, "rb") as fh:
        header = fh.readline().decode("utf-8")
        if size <= want_bytes + len(header):
            fh.seek(0)
            return pd.read_csv(fh)
        fh.seek(size - want_bytes)
        fh.readline()                      # drop the partial line
        body = fh.read().decode("utf-8", errors="strict")
    return pd.read_csv(io.StringIO(header + body))


def load_bounded_m1(archive_csv: Path, live_segment_csv: Path,
                    window_days: int = M1_WINDOW_DAYS,
                    now: pd.Timestamp | None = None) -> pd.DataFrame:
    """Assemble the bounded live M1 frame: archive tail + live segment.

    Seam policy is identical to `live.runner.assemble_candles` — archive rows
    win at or before the archive end, live rows win strictly after. The only
    difference is that the archive is TAIL-READ instead of fully parsed.
    """
    archive_csv, live_segment_csv = Path(archive_csv), Path(live_segment_csv)
    # ~66 B/row observed; ask for the window plus a generous margin so the
    # timestamp filter, not the byte estimate, decides the boundary.
    want = int(window_days * 1400 * 80 * 1.6)
    archive = _read_csv_tail(archive_csv, want) if archive_csv.exists() \
        else pd.DataFrame(columns=M1_COLUMNS)

    frames = [archive]
    if live_segment_csv.exists():
        live = pd.read_csv(live_segment_csv)
        if len(live):
            if len(archive):
                a_end = pd.to_datetime(archive["time"], utc=True).max()
                live = live[pd.to_datetime(live["time"], utc=True) > a_end]
            frames.append(live)
    out = pd.concat([f for f in frames if len(f)], ignore_index=True) \
        if any(len(f) for f in frames) else pd.DataFrame(columns=M1_COLUMNS)
    if not len(out):
        raise WorkingSetError("bounded M1 assembled to zero rows")

    t = pd.to_datetime(out["time"], utc=True)
    end = pd.Timestamp(now, tz="UTC") if now is not None else t.max()
    out = out[t >= end - pd.Timedelta(days=window_days)].reset_index(drop=True)
    assert_within_ceiling(out, LIVE_M1_MAX_ROWS, "bounded live M1 frame")
    return out


def trusted_detection_floor(m15: pd.DataFrame,
                            warmup_bars: int = M15_WARMUP_BARS) -> pd.Timestamp:
    """First M15 timestamp whose detections are trusted from a bounded window.

    Detections at or after this bar matched the full-history detector exactly
    in every measured window; earlier ones sit inside the sequential-state
    warm-up zone and must be treated as unproven.
    """
    t = pd.to_datetime(m15["time"], utc=True).reset_index(drop=True)
    if not len(t):
        raise WorkingSetError("cannot derive a detection floor from an empty M15 frame")
    if len(t) <= warmup_bars:
        raise WorkingSetError(
            f"M15 window has {len(t)} bars, warm-up alone needs {warmup_bars} — "
            "insufficient warm-up, refusing to detect")
    return t.iloc[warmup_bars]
