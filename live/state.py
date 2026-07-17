"""Crash-safe runner state: atomic JSON writes (tmp + rename).

Holds: last processed 15m boundary, hash of the previous trades frame, the
intent ledger (intent_id -> status/ticket), the trade->ticket mirror map and
the daily realised-R counter for the loss kill switch. Restart = reload state,
re-run pipeline, re-diff; idempotent intent ids make replays harmless.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

LEDGER_PENDING = "pending"
LEDGER_SENT = "sent"
LEDGER_CONFIRMED = "confirmed"
LEDGER_FAILED = "failed"
LEDGER_BLOCKED = "blocked"
LEDGER_SIMULATED = "simulated"   # dry_run terminal state


def frame_hash(frame) -> str:
    return hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest() if frame is not None else ""


class RunnerState:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "runner_state.json"
        self.frames_dir = Path(state_dir) / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            return json.loads(self.path.read_text())
        return {"last_boundary": None, "prev_frame_hash": "", "prev_frame_file": None,
                "ledger": {}, "mirror": {}, "daily": {"date": None, "realized_r": 0.0},
                "updated_at": None}

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1))
        os.replace(tmp, self.path)

    # ── frames ───────────────────────────────────────────────────────────────
    def store_frame(self, frame, boundary: str) -> None:
        f = self.frames_dir / "prev_trades.csv"
        frame.to_csv(f, index=False)
        self.data["prev_frame_file"] = str(f)
        self.data["prev_frame_hash"] = frame_hash(frame)
        self.data["last_boundary"] = boundary

    def load_prev_frame(self):
        import pandas as pd
        f = self.data.get("prev_frame_file")
        if not f or not Path(f).exists():
            return None
        return pd.read_csv(f, dtype=str, keep_default_na=False)

    # ── ledger / mirror ──────────────────────────────────────────────────────
    def ledger_status(self, intent_id: str) -> str | None:
        entry = self.data["ledger"].get(intent_id)
        return entry["status"] if entry else None

    def ledger_set(self, intent_id: str, status: str, detail: dict | None = None) -> None:
        self.data["ledger"][intent_id] = {"status": status, "detail": detail or {},
                                          "at": datetime.now(timezone.utc).isoformat()}

    def mirror_ticket(self, trade_id: str) -> int | None:
        return self.data["mirror"].get(trade_id)

    def mirror_set(self, trade_id: str, ticket: int | None) -> None:
        if ticket is None:
            self.data["mirror"].pop(trade_id, None)
        else:
            self.data["mirror"][trade_id] = ticket

    def open_mirror_count(self) -> int:
        return len(self.data["mirror"])

    # ── daily loss tracking ──────────────────────────────────────────────────
    def add_realized_r(self, r: float, on_date: str) -> float:
        daily = self.data["daily"]
        if daily["date"] != on_date:
            daily["date"] = on_date
            daily["realized_r"] = 0.0
        daily["realized_r"] += float(r)
        return daily["realized_r"]

    def daily_realized_r(self, on_date: str) -> float:
        daily = self.data["daily"]
        return float(daily["realized_r"]) if daily["date"] == on_date else 0.0
