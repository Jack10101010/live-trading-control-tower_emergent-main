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
LEDGER_FROZEN = "frozen"

# Statuses that mean "this intent was ACTED ON" and therefore make the
# duplicate-intent rail fire. SafetyRails imports this tuple rather than
# re-listing it, so suppression and persistence can never drift apart.
LEDGER_SUPPRESSING = (LEDGER_SENT, LEDGER_CONFIRMED, LEDGER_SIMULATED)
# Rail/reconcile annotations. These describe why an intent was NOT acted on, so
# they must never overwrite a suppressing status (see ledger_set).
LEDGER_ANNOTATIONS = (LEDGER_BLOCKED, LEDGER_FROZEN)


def frame_hash(frame) -> str:
    return hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest() if frame is not None else ""


def atomic_write_text(path: Path, text: str) -> None:
    """Durably replace a file's contents: write temp, fsync, rename.

    `os.replace` alone is not enough. It makes the *rename* atomic, but without
    an fsync the file's DATA may still be in the page cache when power is lost,
    leaving a correctly-named file with truncated or zero-length content. This
    module's contract is crash safety, so the flush is not optional.
    """
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


class RunnerState:
    def __init__(self, state_dir: Path):
        self.path = Path(state_dir) / "runner_state.json"
        self.frames_dir = Path(state_dir) / "frames"
        self.frames_dir.mkdir(parents=True, exist_ok=True)
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text())
            except (ValueError, OSError) as exc:
                # Fail CLOSED with an actionable message. Raw JSONDecodeError out
                # of __init__ crashed the process before any logging existed, and
                # the supervisor then restarted it into the same crash forever.
                # The corrupt file is preserved for forensics; recovery is a
                # deliberate operator action (restore a backup, or delete the file
                # to bootstrap a fresh baseline) — never silent data invention.
                quarantine = self.path.with_suffix(".corrupt")
                try:
                    self.path.replace(quarantine)
                except OSError:
                    quarantine = self.path
                raise RuntimeError(
                    f"runner_state.json is unreadable ({exc}). Quarantined at "
                    f"{quarantine}. Restore the last good state from backup, or "
                    f"delete it to re-bootstrap (the next cycle then rebuilds the "
                    f"baseline frame and emits no intents).") from exc
        return {"last_boundary": None, "prev_frame_hash": "", "prev_frame_file": None,
                "ledger": {}, "mirror": {}, "broker_closed": {},
                "daily": {"date": None, "realized_r": 0.0}, "updated_at": None}

    def save(self) -> None:
        self.data["updated_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_text(self.path, json.dumps(self.data, indent=1))

    # ── frames ───────────────────────────────────────────────────────────────
    def store_frame(self, frame, boundary: str) -> None:
        """Persist the diff baseline atomically.

        This used to be a bare `frame.to_csv(path)` — a direct, non-atomic
        write of the very file the next cycle diffs against. A crash mid-write
        left a truncated CSV that `load_prev_frame` parsed happily, so the next
        diff compared live trades against a partial baseline and emitted intents
        for trades that were already open. Measured: a half-written frame
        produced a spurious OPEN_POSITION for an already-open trade.
        """
        f = self.frames_dir / "prev_trades.csv"
        csv_text = frame.to_csv(index=False)
        atomic_write_text(f, csv_text)
        self.data["prev_frame_file"] = str(f)
        self.data["prev_frame_hash"] = frame_hash(frame)
        self.data["last_boundary"] = boundary

    def load_prev_frame(self):
        """Load the diff baseline, refusing to proceed on a corrupt one.

        The hash was recorded but never checked. Verifying it turns a silently
        wrong diff — which invents or loses orders — into a loud, actionable
        stop. Recovery is deliberate: delete the frame to re-bootstrap (the next
        cycle re-establishes the baseline and emits no intents, and
        reconciliation then converges the mirror back to broker truth).
        """
        import pandas as pd
        f = self.data.get("prev_frame_file")
        if not f or not Path(f).exists():
            return None
        try:
            frame = pd.read_csv(f, dtype=str, keep_default_na=False)
        except (ValueError, OSError) as exc:
            raise RuntimeError(
                f"previous trades frame at {f} is unreadable ({exc}). Delete it to "
                f"re-bootstrap the baseline; the next cycle emits no intents.") from exc
        expected = self.data.get("prev_frame_hash") or ""
        actual = frame_hash(frame)
        if expected and actual != expected:
            quarantine = Path(f).with_suffix(".corrupt.csv")
            try:
                Path(f).replace(quarantine)
                self.data["prev_frame_file"] = None
                self.save()
            except OSError:
                quarantine = Path(f)
            raise RuntimeError(
                f"previous trades frame is corrupt: hash {actual[:16]}… != recorded "
                f"{expected[:16]}… (likely a crash during the frame write). "
                f"Quarantined at {quarantine}. The next start re-bootstraps the "
                f"baseline and emits no intents; reconciliation restores the mirror "
                f"from broker truth.")
        return frame

    # ── ledger / mirror ──────────────────────────────────────────────────────
    def ledger_status(self, intent_id: str) -> str | None:
        entry = self.data["ledger"].get(intent_id)
        return entry["status"] if entry else None

    def ledger_set(self, intent_id: str, status: str, detail: dict | None = None) -> None:
        """Record an intent's status, protecting acted-on states from annotations.

        An intent that was already sent/confirmed/simulated is terminal for
        duplicate-suppression purposes. Letting a later annotation (blocked,
        frozen) overwrite it destroyed suppression: the duplicate rail only
        fires on LEDGER_SUPPRESSING, so a replay that got blocked would rewrite
        the entry to `blocked` and the NEXT replay would re-execute the intent —
        a duplicate order. The annotation is counted instead of applied, which
        keeps the audit trail without ever weakening suppression.
        """
        existing = self.data["ledger"].get(intent_id)
        if (existing and existing.get("status") in LEDGER_SUPPRESSING
                and status in LEDGER_ANNOTATIONS):
            existing["suppressed_annotations"] = existing.get("suppressed_annotations", 0) + 1
            existing["last_annotation"] = {"status": status, "detail": detail or {},
                                           "at": datetime.now(timezone.utc).isoformat()}
            return
        self.data["ledger"][intent_id] = {"status": status, "detail": detail or {},
                                          "at": datetime.now(timezone.utc).isoformat()}

    # ── broker-initiated closes (SL / TP / manual) ───────────────────────────
    def mark_broker_closed(self, trade_id: str, ticket: int) -> None:
        """Record that the BROKER closed a mirrored position, and drop the mirror.

        The engine stays the state machine, so its own CLOSE intent remains the
        single accounting event. This marker tells the executor the position is
        already gone (account for it, do NOT send a second close) and tells the
        safety rail the trade is legitimately closable rather than unknown —
        without it, every server-side stop-out was blocked as `unknown_position`
        and its realised R never reached the daily-loss counter.
        """
        self.data.setdefault("broker_closed", {})[str(trade_id)] = {
            "ticket": ticket, "at": datetime.now(timezone.utc).isoformat()}
        self.mirror_set(trade_id, None)

    def broker_closed_ticket(self, trade_id: str) -> int | None:
        entry = (self.data.get("broker_closed") or {}).get(str(trade_id))
        return entry["ticket"] if entry else None

    def clear_broker_closed(self, trade_id: str) -> None:
        (self.data.get("broker_closed") or {}).pop(str(trade_id), None)

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
