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


class MT5BarBridge:
    def __init__(self, config, gateway):
        self.config = config
        self.gateway = gateway
        config.ensure_dirs()
        self._write_provenance_once()

    # ── provenance / heartbeat ───────────────────────────────────────────────
    def _write_provenance_once(self) -> None:
        path = self.config.market_data_dir / "provenance.json"
        if path.exists():
            return
        path.write_text(json.dumps({
            "symbol": SYMBOL,
            "data_seam": DATA_SEAM,
            "frozen_history": "Lux data/candles/EURUSD_1m_extended_2015_2026.csv "
                              "(sha256 314a0efa…, Dukascopy-derived, ends 2026-06-19)",
            "live_vendor": "MT5 terminal feed (broker symbol %s)" % self.config.broker_symbol,
            "seam_policy": "frozen rows win at or before frozen end; live rows win after",
            "time_base": TIME_BASE,
            "server_tz": self.config.mt5_server_tz,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }, indent=1))

    def verify_time_base(self) -> tuple[bool, str]:
        """Refuse to extend a segment recorded under a different time base.

        Segments written before the server-time fix carry broker wall-clock
        labels, so appending canonical-UTC rows to them would interleave two
        bases in one file and silently mis-assign trading sessions. Mixing is
        undetectable downstream, so it fails closed here and the operator
        archives + re-backfills instead.
        """
        path = self.config.market_data_dir / "provenance.json"
        if not path.exists():
            return True, "no provenance yet (fresh segment)"
        try:
            stored = json.loads(path.read_text()).get("time_base")
        except (OSError, ValueError) as exc:
            return False, f"provenance.json unreadable: {exc}"
        if stored == TIME_BASE:
            return True, f"time_base {stored}"
        if not self.config.live_segment_csv.exists():
            return True, f"time_base {stored!r} superseded, segment absent — will rebuild as {TIME_BASE}"
        return False, (f"live segment written under time_base {stored!r} but this build emits "
                       f"{TIME_BASE!r} — timestamps are not comparable. Archive "
                       f"{self.config.live_segment_csv.name} + provenance.json and re-backfill.")

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
        return datetime.strptime(last, "%Y-%m-%d %H:%M:%S+00:00").replace(tzinfo=timezone.utc)

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
