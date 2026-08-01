"""M-CAPACITY-1 — equivalence tests for the ingestion fast path.

`run_once` used to read the 260 MB / 4.31 M-row frozen dataset and run
`pd.to_datetime(...).max()` over it on EVERY poll, then discard the result when
the boundary had not moved. Measured: ~23.6 s for the read plus ~11.2 s for the
parse, which is why an idle `no_new_bar` cycle took ~36 s while doing no work.

The optimisation only reorders WHEN history is materialised; it must not change
WHICH boundary is chosen or WHAT frame the pipeline sees. These tests pin exactly
that, because a frontier that disagreed with the assembled frame by even one bar
would silently change which boundary is traded.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.runner import (assemble_candles, frontier_time,  # noqa: E402
                         frozen_max_time, latest_closed_boundary)

COLS = ["time", "open", "high", "low", "close", "volume"]


def _csv(path: Path, times: list[str]) -> Path:
    rows = [{"time": t, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05, "volume": 10}
            for t in times]
    pd.DataFrame(rows, columns=COLS).to_csv(path, index=False)
    return path


def _reference_frontier(frozen: Path, live: Path):
    """What the original implementation computed, verbatim."""
    return pd.to_datetime(assemble_candles(frozen, live)["time"]).max()


FROZEN = ["2026-07-30 09:00:00", "2026-07-30 09:01:00", "2026-07-30 09:02:00"]


@pytest.mark.parametrize("live_times, case", [
    (None,                                                        "live file absent"),
    ([],                                                          "live file empty"),
    (["2026-07-30 08:59:00"],                                     "live entirely before frozen end"),
    (["2026-07-30 09:02:00"],                                     "live equal to frozen end (not strictly after)"),
    (["2026-07-30 09:03:00", "2026-07-30 09:04:00"],              "live strictly after"),
    (["2026-07-30 09:01:00", "2026-07-30 09:05:00"],              "live straddles the seam"),
    (["2026-07-30 09:07:00", "2026-07-30 09:03:00"],              "live unsorted"),
])
def test_frontier_time_matches_full_assembly(tmp_path, live_times, case):
    frozen = _csv(tmp_path / "frozen.csv", FROZEN)
    live = tmp_path / "live.csv"
    if live_times is not None:
        _csv(live, live_times)

    expected = _reference_frontier(frozen, live)
    actual = frontier_time(frozen, live, {})
    assert actual == expected, f"{case}: {actual} != {expected}"
    # the boundary is what actually drives trading -- pin it too
    assert latest_closed_boundary(actual) == latest_closed_boundary(expected), case


def test_frozen_max_is_cached_and_reused(tmp_path):
    frozen = _csv(tmp_path / "frozen.csv", FROZEN)
    cache: dict = {}
    first = frozen_max_time(frozen, cache)
    assert cache["identity"] is not None

    reads = {"n": 0}
    real_read = pd.read_csv

    def counting_read(*a, **k):
        reads["n"] += 1
        return real_read(*a, **k)

    pd.read_csv = counting_read
    try:
        second = frozen_max_time(frozen, cache)
    finally:
        pd.read_csv = real_read
    assert second == first
    assert reads["n"] == 0, "cached call must not re-read the frozen dataset"


def test_cache_invalidates_when_the_frozen_dataset_changes(tmp_path):
    """A re-baselined dataset must not be served a stale frontier."""
    frozen = _csv(tmp_path / "frozen.csv", FROZEN)
    cache: dict = {}
    before = frozen_max_time(frozen, cache)

    _csv(frozen, FROZEN + ["2026-07-30 09:30:00"])       # changes size and mtime
    after = frozen_max_time(frozen, cache)
    assert after > before
    assert after == pd.Timestamp("2026-07-30 09:30:00")


def test_no_new_bar_does_not_read_full_history(tmp_path, monkeypatch):
    """The whole point: an idle poll must not touch the 260 MB dataset."""
    from live.config import LiveConfig
    from live.runner import LiveRunner

    lux = tmp_path / "lux" / "data" / "candles"
    lux.mkdir(parents=True)
    frozen = _csv(lux / "EURUSD_1m_extended_2015_2026.csv", FROZEN)
    md = tmp_path / "state" / "market_data"
    md.mkdir(parents=True)
    _csv(md / "EURUSD_1m_live.csv", ["2026-07-30 09:03:00"])

    cfg = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                     market_data_dir=md, kill_file=tmp_path / "state" / "KILL")
    cfg.ensure_dirs()
    runner = LiveRunner(cfg, session=None, pipeline=lambda *a, **k: pd.DataFrame())

    expected = latest_closed_boundary(_reference_frontier(frozen, cfg.live_segment_csv))
    runner.state.data["last_boundary"] = str(expected)      # pretend already processed

    full_reads = {"n": 0}
    real_read = pd.read_csv

    def watch(path, *a, **k):
        # a read WITHOUT usecols is a full-history materialisation
        if str(path).endswith("EURUSD_1m_extended_2015_2026.csv") and "usecols" not in k:
            full_reads["n"] += 1
        return real_read(path, *a, **k)

    monkeypatch.setattr(pd, "read_csv", watch)
    out = runner.run_once()

    assert out["status"] == "no_new_bar"
    assert out["boundary"] == str(expected)
    assert full_reads["n"] == 0, "idle poll must not materialise full history"


def test_work_path_still_receives_the_fully_assembled_frame(tmp_path):
    """Equivalence where it matters: the pipeline input must be unchanged."""
    from live.config import LiveConfig
    from live.runner import LiveRunner

    lux = tmp_path / "lux" / "data" / "candles"
    lux.mkdir(parents=True)
    frozen = _csv(lux / "EURUSD_1m_extended_2015_2026.csv", FROZEN)
    md = tmp_path / "state" / "market_data"
    md.mkdir(parents=True)
    _csv(md / "EURUSD_1m_live.csv", ["2026-07-30 09:03:00", "2026-07-30 09:04:00"])

    cfg = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                     market_data_dir=md, kill_file=tmp_path / "state" / "KILL")
    cfg.ensure_dirs()

    seen = {}

    def capture(candles, frontier_date, artifacts=None):
        seen["candles"] = candles.copy()
        seen["frontier_date"] = frontier_date
        return pd.DataFrame({"trade_id": [], "fill_time": []})

    runner = LiveRunner(cfg, session=None, pipeline=capture)
    out = runner.run_once()

    assert out["status"] == "bootstrap"
    expected_frame = assemble_candles(frozen, cfg.live_segment_csv)
    pd.testing.assert_frame_equal(seen["candles"], expected_frame)
    expected_boundary = latest_closed_boundary(_reference_frontier(frozen, cfg.live_segment_csv))
    assert out["boundary"] == str(expected_boundary)
    assert seen["frontier_date"] == str(expected_boundary.date())
