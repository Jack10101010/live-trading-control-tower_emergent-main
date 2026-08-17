"""Daily market-state store and O(1) state lookup.

M-LIVE-BOUNDED-WORKING-SET-1.

The measured finding that shaped this module
--------------------------------------------
The architecture audit assumed daily history could be bounded to ~300 bars.
That assumption is WRONG, and bounding it would have silently corrupted the
matrix key.

`strategy_core.regime` builds EMA200 / ATR14 / ADX14 with `ewm_adjust_false`,
a pure recursion seeded at y0 = x0. Seed influence decays like (1-alpha)^n, and
for EMA200 alpha is 2/201, so it decays slowly. Measured against the full
archive panel:

    fresh seed   last |dEMA|      categorical state diffs (last 400 trading days)
      300 bars   1.978e-03  (19.78 pips)   103      <-- unusable
      600 bars   5.165e-05  ( 0.52 pips)     0
     1000 bars   5.155e-08                   0
     2000 bars   5.888e-11                   0

19.78 pips of EMA error flips Bull/Bear anywhere near the crossover.

The resolution is NOT a bigger bounded window and NOT a tolerance. It is the
observation that daily was never the cost problem in the first place: the whole
11-year daily panel is 3,634 rows and computes in 0.257 s. So this store keeps
the FULL daily series, derives it incrementally from M1, and recomputes the
canonical panel from it. That is bit-identical to production by construction,
because it is the same function over the same series.

For the record, the alternative was also verified: continuing EMA200 from a
persisted accumulator is bitwise exact (max err 0.0, array_equal True). It is
not used because keeping 3,634 daily rows is simpler and has no corrupt-
accumulator failure mode to recover from.

BBW's threshold is `fixed` mode (EURUSD 2.342, a locked per-symbol constant),
so it carries no in-sample history dependence. BBW itself is rolling(20).
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from live.state import atomic_write_text

DAILY_COLUMNS = ["time", "open", "high", "low", "close"]

#: Warm-up below which a from-scratch daily panel is refused. Chosen from the
#: table above: 600 bars already reproduces every categorical state, 1000 is
#: numerically exact to 5e-8. We require 1000 and normally hold ~3,600.
MIN_DAILY_BARS = 1000


class DailyStateError(RuntimeError):
    """Daily panel could not be produced safely. Fail closed."""


def daily_from_m1(m1: pd.DataFrame, drop_leading_partial: bool = False) -> pd.DataFrame:
    """Aggregate M1 to UTC daily OHLC.

    Mirrors `strategy_core.regime.resample_daily`: group by UTC date,
    open=first, high=max, low=min, close=last, chronological.

    `drop_leading_partial` MUST be set when the input is a bounded rolling
    window. A rolling window starts at an arbitrary instant, so its first UTC
    day is usually truncated — merging that truncated day would overwrite a
    complete day in the store with a partial one, and because ADX/ATR are
    Wilder recursions the corruption then propagates forward through every
    later day. Observed live: a 120-day window starting 20:53 on a Thursday
    produced 103 ADX differences. A 90-day window did not, purely because its
    boundary happened to land on a Saturday — which is exactly why this is a
    flag with a default that callers must think about, not silent behaviour.
    """
    if not len(m1):
        return pd.DataFrame(columns=DAILY_COLUMNS)
    t = pd.to_datetime(m1["time"], utc=True)
    g = m1.assign(_d=t.dt.strftime("%Y-%m-%d")).groupby("_d", sort=True)
    out = pd.DataFrame({
        "time": list(g.groups.keys()),
        "open": g["open"].first().values, "high": g["high"].max().values,
        "low": g["low"].min().values, "close": g["close"].last().values,
    })
    if drop_leading_partial and len(out) > 1:
        out = out.iloc[1:].reset_index(drop=True)
    return out


class DailyStore:
    """Append-only daily OHLC store — full history, ~200 KB, never bounded."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.frame = (pd.read_csv(self.path) if self.path.exists()
                      else pd.DataFrame(columns=DAILY_COLUMNS))

    def merge(self, daily: pd.DataFrame) -> int:
        """Upsert daily rows by date; returns the number of rows added/changed.

        The most recent day is legitimately revised as its M1 arrives, so a
        later observation of the same date replaces the earlier one.
        """
        if not len(daily):
            return 0
        before = self.frame.copy()
        combined = pd.concat([self.frame, daily[DAILY_COLUMNS]], ignore_index=True)
        combined = (combined.drop_duplicates(subset=["time"], keep="last")
                            .sort_values("time").reset_index(drop=True))
        changed = 0 if before.equals(combined) else \
            len(combined) - len(before) or len(daily)
        self.frame = combined
        return changed

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self.path, self.frame.to_csv(index=False))

    def records(self) -> list[dict]:
        return self.frame.to_dict("records")


class DailyStateCache:
    """Canonical daily regime panel with O(1) lookup by authoritative timestamp.

    Timing authority is UNCHANGED. This class only changes *when* the panel is
    computed, never *when* it is read: `lookup` resolves the UTC day of the
    authoritative (fill) candle and returns the same leakage-shifted row the
    full-history path would return.
    """

    def __init__(self, regime_module, cfg: dict | None = None, symbol: str = "EURUSD"):
        self._regime = regime_module
        self._cfg = cfg
        self._symbol = symbol
        self._panel: list[dict] = []
        self._index: dict[str, dict] = {}
        self._built_through: str | None = None

    def build(self, daily_records: list[dict]) -> "DailyStateCache":
        if len(daily_records) < MIN_DAILY_BARS:
            raise DailyStateError(
                f"daily history has {len(daily_records)} bars, need >= {MIN_DAILY_BARS} "
                "for EMA200 parity — refusing to compute market state")
        self._panel = self._regime.daily_regime_panel(
            daily_records, self._cfg, self._symbol)
        self._index = self._regime.panel_by_date(self._panel)
        self._built_through = self._panel[-1]["date"] if self._panel else None
        return self

    @property
    def panel(self) -> list[dict]:
        return self._panel

    @property
    def built_through(self) -> str | None:
        return self._built_through

    def lookup(self, authoritative_timestamp) -> dict | None:
        """State production would have known at this timestamp. O(1).

        Returns None when the day is outside the panel — callers must treat
        that as "state unavailable" and fail closed, never as "no filter".
        """
        if not self._index:
            raise DailyStateError("state cache not built — refusing to answer a lookup")
        ts = pd.Timestamp(authoritative_timestamp)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        return self._index.get(ts.strftime("%Y-%m-%d"))
