"""P1 shadow — lightweight operational logging.

Append-only JSONL (`<state_dir>/ops/cycles.jsonl`) + a per-cycle heartbeat
(`<state_dir>/ops/heartbeat.json`). One record per loop cycle: start/end,
duration, boundary/bar timestamps, intent counts, reconcile findings, errors.
No rotation, no analytics, no dependencies — the shadow report reads this file.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class OpsLog:
    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / "ops"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.cycles_path = self.dir / "cycles.jsonl"
        self.heartbeat_path = self.dir / "heartbeat.json"
        self._last_boundary = self._recover_last_boundary()

    def _recover_last_boundary(self):
        """Survive a restart so the first cycle's beat still names a boundary."""
        try:
            return json.loads(self.heartbeat_path.read_text()).get("boundary")
        except (OSError, ValueError):
            return None

    def cycle_start(self) -> dict:
        """Open a cycle record and emit a `cycle_running` liveness beat.

        A cycle legitimately runs for many minutes (full-history recompute),
        which exceeds any idle staleness threshold. Beating at the START with an
        explicit phase lets a monitor distinguish "working" from "hung" instead
        of inferring death from silence; consumers apply a longer allowance
        while the phase is `cycle_running` (see deploy_check.runtime).
        """
        record = {"cycle_start": _now()}
        self.heartbeat_path.write_text(json.dumps({
            "at": record["cycle_start"], "phase": "cycle_running",
            # Carry the LAST processed boundary forward: blanking it while a
            # cycle runs hid the engine's position from any monitor for the whole
            # multi-minute recompute.
            "status": "running", "boundary": self._last_boundary, "error": "",
        }))
        return record

    def cycle_end(self, record: dict, *, boundary=None, last_bar=None, appended=0,
                  status="", intents=0, applied=0, blocked=0, skipped=0,
                  reconcile_findings=None, frozen=False, published=None,
                  error: str = "") -> dict:
        end = datetime.now(timezone.utc)
        start = datetime.fromisoformat(record["cycle_start"])
        record.update({
            "cycle_end": end.isoformat(),
            "duration_s": round((end - start).total_seconds(), 3),
            "boundary": boundary, "last_bar": last_bar, "bars_appended": appended,
            "status": status, "intents": intents, "applied": applied,
            "blocked": blocked, "skipped": skipped,
            "reconcile_findings": reconcile_findings or [],
            "frozen": bool(frozen), "published": published, "error": error,
        })
        if boundary:
            self._last_boundary = boundary
        with self.cycles_path.open("a") as fh:
            fh.write(json.dumps(record, default=str) + "\n")
        self.heartbeat_path.write_text(json.dumps({
            "at": record["cycle_end"], "phase": "idle", "boundary": boundary,
            "status": status, "error": error, "duration_s": record["duration_s"],
        }))
        return record

    def read_cycles(self) -> list[dict]:
        if not self.cycles_path.exists():
            return []
        out = []
        for line in self.cycles_path.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    out.append({"corrupt_line": line[:100]})
        return out
