"""MT5 bar bridge — closed M1 bars into the canonical live segment.

Writes append-only, deduplicated, chronological rows to
`<market_data_dir>/EURUSD_1m_live.csv` using the frozen dataset's exact column
schema (time,open,high,low,close,volume with `%Y-%m-%d %H:%M:%S+00:00` UTC
times), plus `heartbeat.json` and a one-time `provenance.json` recording the
accepted Dukascopy→MT5 data seam.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

from live.config import DATA_SEAM, SYMBOL, TIME_BASE

COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def verify_segment_time_base(config) -> tuple[bool, str]:
    """Refuse to extend a segment whose time base is not this build's.

    Pure: reads only, creates nothing. Constructing a bridge to run this check
    used to stamp provenance as a side effect, which both broke deploy_check's
    read-only contract and let an unverified segment certify itself.

    Fails closed in two directions:
      * provenance present but from an older base, with a segment on disk —
        appending would interleave two bases in one file and silently
        mis-assign trading sessions;
      * segment on disk with NO provenance at all — its base cannot be
        established, so it must not be trusted.
    """
    path = config.market_data_dir / "provenance.json"
    segment = config.live_segment_csv
    if not path.exists():
        if segment.exists():
            return False, (f"live segment {segment.name} present with NO provenance — its time "
                           f"base cannot be certified. Archive it and re-backfill as {TIME_BASE}.")
        return True, "no provenance yet (fresh segment)"
    try:
        stored = json.loads(path.read_text()).get("time_base")
    except (OSError, ValueError) as exc:
        return False, f"provenance.json unreadable: {exc}"
    if stored == TIME_BASE:
        return True, f"time_base {stored}"
    if not segment.exists():
        # Legitimate rebuild: provenance is refreshed by the first append, so
        # this must NOT latch the deployment into permanent refusal.
        return True, f"time_base {stored!r} superseded, segment absent — will rebuild as {TIME_BASE}"
    return False, (f"live segment written under time_base {stored!r} but this build emits "
                   f"{TIME_BASE!r} — timestamps are not comparable. Archive "
                   f"{segment.name} + provenance.json and re-backfill.")


class MT5BarBridge:
    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        config.ensure_dirs()

    # ── provenance / heartbeat ───────────────────────────────────────────────
    def ensure_provenance(self) -> None:
        """Stamp/refresh provenance for the CURRENT time base.

        Deliberately NOT called from __init__. Writing provenance at construction
        certified whatever was already on disk: a segment recorded under the old
        server-time base but missing its provenance file was silently relabelled
        canonical, defeating the very guard it feeds. Provenance is now written
        only when rows of the current base are actually appended, so the file
        always describes data that this build produced.
        """
        path = self.config.market_data_dir / "provenance.json"
        superseded = None
        if path.exists():
            try:
                existing = json.loads(path.read_text())
            except (OSError, ValueError):
                existing = {}
            if existing.get("time_base") == TIME_BASE:
                return
            superseded = existing.get("time_base")
        self.config.market_data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "symbol": SYMBOL,
            "data_seam": DATA_SEAM,
            "frozen_history": "Lux data/candles/EURUSD_1m_extended_2015_2026.csv "
                              "(sha256 314a0efa…, Dukascopy-derived, ends 2026-06-19)",
            "live_vendor": "MT5 terminal feed (broker symbol %s)" % self.config.broker_symbol,
            "seam_policy": "frozen rows win at or before frozen end; live rows win after",
            "time_base": TIME_BASE,
            "server_clock": (f"base{self.config.mt5_server_base_utc_offset_hours:+d}h "
                             f"dst={self.config.mt5_server_dst_rule}"),
            "superseded_time_base": superseded,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, indent=1))

    def verify_time_base(self) -> tuple[bool, str]:
        """Instance delegate for the pure module-level check (back-compat)."""
        return verify_segment_time_base(self.config)

    def _heartbeat(self, last_bar_time: str | None, appended: int, error: str = "") -> None:
        (self.config.market_data_dir / "heartbeat.json").write_text(json.dumps({
            "at": datetime.now(timezone.utc).isoformat(),
            "last_bar_time": last_bar_time, "appended": appended, "error": error,
        }))

    # ── segment I/O ──────────────────────────────────────────────────────────
    def last_stored_time(self) -> datetime | None:
        path = self.config.live_segment_csv
        if not path.exists():
            return None
        last = None
        with path.open() as fh:
            for row in csv.DictReader(fh):
                last = row["time"]
        if last is None:
            return None
        try:
            return datetime.strptime(last, "%Y-%m-%d %H:%M:%S+00:00").replace(tzinfo=timezone.utc)
        except (ValueError, TypeError) as exc:
            # A malformed tail row previously raised a bare ValueError every cycle,
            # producing an endless identical error with no hint of the cause.
            raise RuntimeError(
                f"live segment {self.config.live_segment_csv.name} has an unparseable "
                f"last row ({last!r}: {exc}). Truncate the bad tail rows or archive "
                f"the segment + provenance.json and let the bridge re-backfill.") from exc

    def append_bars(self, bars: list[dict]) -> int:
        """Append chronological, deduplicated closed bars. Returns rows written."""
        if not bars:
            return 0
        path = self.config.live_segment_csv
        existing_last = self.last_stored_time()
        new_rows = []
        seen: set[str] = set()
        for bar in sorted(bars, key=lambda b: b["time"]):
            if bar["time"] in seen:
                continue  # duplicate inside batch: first wins
            seen.add(bar["time"])
            t = datetime.strptime(bar["time"], "%Y-%m-%d %H:%M:%S+00:00").replace(tzinfo=timezone.utc)
            if existing_last is not None and t <= existing_last:
                continue  # already stored
            new_rows.append(bar)
        if not new_rows:
            return 0
        write_header = not path.exists()
        with path.open("a", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=COLUMNS)
            if write_header:
                writer.writeheader()
            for bar in new_rows:
                writer.writerow({c: bar[c] for c in COLUMNS})
        # Rows of the CURRENT base are now on disk, so the segment can honestly be
        # certified. This is the ONLY place provenance is written, which is what
        # stops it from ever describing data this build did not produce, and what
        # releases the rebuild path instead of latching it into refusal.
        self.ensure_provenance()
        return len(new_rows)

    # ── poll loop ────────────────────────────────────────────────────────────
    def poll_once(self, backfill_from: datetime | None = None) -> dict:
        since = self.last_stored_time() or backfill_from
        if since is None:
            since = datetime(2026, 6, 19, 0, 0, tzinfo=timezone.utc)  # frozen dataset end
        ok, result = self.gateway.closed_m1_bars(since)
        if not ok:
            self._heartbeat(None, 0, error=str(result))
            return {"ok": False, "error": str(result), "appended": 0}
        appended = self.append_bars(result)
        last = result[-1]["time"] if result else None
        self._heartbeat(last, appended)
        return {"ok": True, "appended": appended, "last_bar_time": last}

    def run(self, poll_seconds: int = 5, stop_after: int | None = None) -> None:  # pragma: no cover
        polls = 0
        while stop_after is None or polls < stop_after:
            self.poll_once()
            polls += 1
            time.sleep(poll_seconds)
