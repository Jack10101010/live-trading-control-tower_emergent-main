"""Incremental M15 construction from closed M1 bars.

M-LIVE-BOUNDED-WORKING-SET-1.

Parity contract with `src/resample.py::resample_candles(df, "15min")`:

    UTC, 15-minute boundaries, label='left', closed='left'
    bar 09:00 covers M1 09:00 .. 09:14 inclusive
    open=first  high=max  low=min  close=last  volume=sum
    a bucket with no finite OHLC produces NO row (dropna), never a filled gap

Aggregations skip non-finite values, matching pandas' groupby first/max/min/last,
and a bucket is emitted only if every one of open/high/low/close resolved to a
finite number — which is exactly `dropna(subset=[...])`.

Fail-closed inputs
------------------
Out-of-order M1 and *conflicting* duplicate M1 both raise. An identical
duplicate is idempotent (the feed may legitimately re-deliver a bar). We never
quietly manufacture, reorder, or interpolate a candle: a wrong M15 bar silently
moves every order block downstream of it.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pandas as pd

from live.state import atomic_write_text
from live.working_set import M1_COLUMNS

FIFTEEN_MIN = pd.Timedelta(minutes=15)


class M15ParityError(RuntimeError):
    """Ambiguous or non-monotonic M1 input. Never downgrade to a guess."""


def bucket_of(ts: pd.Timestamp) -> pd.Timestamp:
    """label='left' bucket start for an M1 timestamp."""
    return pd.Timestamp(ts).tz_convert("UTC").floor("15min")


def _finite(v) -> bool:
    try:
        return math.isfinite(float(v))
    except (TypeError, ValueError):
        return False


class M15Accumulator:
    """Durable incremental M15 builder.

    `state_path` persists the partially-filled bucket so a restart part-way
    through an M15 candle resumes rather than dropping or double-counting it.
    """

    def __init__(self, state_path: Path | None = None):
        self.state_path = Path(state_path) if state_path else None
        self._bucket: dict | None = None      # open (incomplete) bar
        self._last_m1: pd.Timestamp | None = None
        self._last_row: tuple | None = None   # for duplicate comparison
        if self.state_path and self.state_path.exists():
            self._load()

    # ── durability ───────────────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            d = json.loads(self.state_path.read_text())
        except (OSError, ValueError) as exc:
            raise M15ParityError(
                f"persisted M15 accumulator unreadable ({exc}) — refusing to "
                "resume from an unknown partial bucket") from exc
        b = d.get("bucket")
        if b:
            self._bucket = dict(b, start=pd.Timestamp(b["start"]))
        if d.get("last_m1"):
            self._last_m1 = pd.Timestamp(d["last_m1"])
        self._last_row = tuple(d["last_row"]) if d.get("last_row") else None

    def save(self) -> None:
        if not self.state_path:
            return
        b = self._bucket
        atomic_write_text(self.state_path, json.dumps({
            "bucket": dict(b, start=str(b["start"])) if b else None,
            "last_m1": str(self._last_m1) if self._last_m1 is not None else None,
            "last_row": list(self._last_row) if self._last_row else None,
        }, indent=2))

    # ── ingest ───────────────────────────────────────────────────────────────
    def ingest(self, row: dict) -> list[dict]:
        """Absorb one closed M1 bar; return any M15 bars completed by it."""
        ts = pd.Timestamp(row["time"])
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        sig = (float(row["open"]), float(row["high"]),
               float(row["low"]), float(row["close"]), float(row.get("volume") or 0.0))

        if self._last_m1 is not None:
            if ts < self._last_m1:
                raise M15ParityError(
                    f"out-of-order M1: {ts} arrived after {self._last_m1} — refusing")
            if ts == self._last_m1:
                if self._last_row is not None and sig != self._last_row:
                    raise M15ParityError(
                        f"conflicting duplicate M1 at {ts}: {self._last_row} vs {sig} "
                        "— refusing to choose")
                return []                      # identical re-delivery: idempotent

        emitted: list[dict] = []
        start = bucket_of(ts)
        if self._bucket is not None and start != self._bucket["start"]:
            done = self._close_bucket()
            if done:
                emitted.append(done)
            self._bucket = None
        if self._bucket is None:
            self._bucket = {"start": start, "open": None, "high": None,
                            "low": None, "close": None, "volume": 0.0}

        b = self._bucket
        o, h, l, c, v = sig
        if _finite(o) and b["open"] is None:
            b["open"] = o
        if _finite(h):
            b["high"] = h if b["high"] is None else max(b["high"], h)
        if _finite(l):
            b["low"] = l if b["low"] is None else min(b["low"], l)
        if _finite(c):
            b["close"] = c
        if _finite(v):
            b["volume"] += v

        self._last_m1, self._last_row = ts, sig
        return emitted

    def _close_bucket(self) -> dict | None:
        """Finalise the open bucket, or None if it would have been dropna'd."""
        b = self._bucket
        if b is None:
            return None
        if any(b[k] is None for k in ("open", "high", "low", "close")):
            return None                      # mirrors dropna(subset=OHLC)
        return {"time": b["start"], "open": b["open"], "high": b["high"],
                "low": b["low"], "close": b["close"], "volume": b["volume"]}

    def ingest_frame(self, m1: pd.DataFrame) -> list[dict]:
        out: list[dict] = []
        for r in m1.to_dict("records"):
            out.extend(self.ingest(r))
        return out

    # ── views ────────────────────────────────────────────────────────────────
    @property
    def open_bucket_start(self) -> pd.Timestamp | None:
        return self._bucket["start"] if self._bucket else None

    def completed_frame(self, m1: pd.DataFrame) -> pd.DataFrame:
        """Build the frame of COMPLETED M15 bars from an M1 frame, row by row.

        The final, still-open bucket is deliberately excluded — the canonical
        resample has no notion of "open", so parity is only meaningful over
        buckets whose 15 minutes have fully elapsed.

        This is the INCREMENTAL implementation walking every bar. It exists to
        be compared against `completed_m15` in tests; bulk callers should use
        that instead (see its docstring for why).
        """
        bars = self.ingest_frame(m1)
        return pd.DataFrame(bars, columns=M1_COLUMNS) if bars else \
            pd.DataFrame(columns=M1_COLUMNS)


def completed_m15(m1: pd.DataFrame) -> pd.DataFrame:
    """All COMPLETED M15 bars from an M1 frame — vectorised bulk path.

    Two implementations exist on purpose and are proven equal in tests:

      * `M15Accumulator.ingest`  — one M1 bar at a time. This is the live
        steady-state path (~one call per minute), where per-call cost is what
        matters and durability across restart is what it buys.
      * `completed_m15`          — this function, for building a whole window
        at once. The row-wise loop costs 14.5 s over a 92k-row bootstrap
        window; vectorised it is ~0.3 s. Using the loop for bulk work would
        have made the bounded path *slower than the thing it replaces* in its
        dominant stage.

    Identical semantics to `src/resample.py`, minus the still-open final bucket.
    """
    if not len(m1):
        return pd.DataFrame(columns=M1_COLUMNS)
    c = m1.copy()
    t = pd.to_datetime(c["time"], utc=True)
    c["time"] = t
    out = (c.set_index("time")
            .resample("15min")
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last", "volume": "sum"})
            .dropna(subset=["open", "high", "low", "close"])
            .reset_index()[M1_COLUMNS])
    # drop the bucket the newest M1 bar is still filling
    open_bucket = bucket_of(t.max())
    return out[out["time"] < open_bucket].reset_index(drop=True)
