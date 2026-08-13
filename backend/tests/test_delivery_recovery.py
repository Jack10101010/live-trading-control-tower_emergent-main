"""M-CT-DELIVERY-RECOVERY-1 — delivery health describes NOW, not history.

Two defects, opposite directions, one root cause: status was derived from a
sticky `consecutive_failures` counter instead of from current worker facts.

  * FALSE HEALTHY (the dangerous one) — a succeeding heartbeat reset the streak
    that a failed COMPLETE snapshot had earned, so the Control Tower read
    `healthy` while `pending_complete` was true.
  * LATCHED DEGRADED (the reported one) — the streak could only be
    re-evaluated on a pass that had new work, so after one HTTP 400 with
    nothing new to send the status stayed degraded with nothing pending.

Status is now derived from: every slot's current content being delivered AND
that slot's last attempt having succeeded. `last_failure_at` and `last_error`
are retained as audit evidence and are explicitly NOT allowed to imply current
degradation.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.telemetry_outbox import DeliveryWorker, NodeHeartbeat, TelemetryOutbox  # noqa: E402


def complete(tag="c1"):
    return {"cycle": {"status": "ok"}, "news": {"h": "ok"},
            "decisions": {"count": 1, "decisions": [{"ob_id": tag}]}}


def runtime():
    return {"cycle": {"status": "recomputing"}}


class Sender:
    """mode: True=200, False=timeout, "400"=rejected, or a callable per payload."""

    def __init__(self, mode=True):
        self.mode, self.sent = mode, []

    def __call__(self, blob):
        payload = json.loads(blob)
        self.sent.append(payload)
        m = self.mode(payload) if callable(self.mode) else self.mode
        if m == "400":
            return False, "HTTP 400"
        return (True, "200") if m else (False, "URLError: timed out")


def worker(tmp_path, sender, hb=True):
    return DeliveryWorker(TelemetryOutbox(tmp_path), "http://mac/api/live/ingest",
                          sender=sender,
                          heartbeat=NodeHeartbeat("n1", pid=1) if hb else None)


# ── 1-5. the basic recovery cycle ───────────────────────────────────────────

def test_initial_state_is_healthy(tmp_path):
    assert worker(tmp_path, Sender()).health_snapshot()["ct_delivery"] == "healthy"


def test_one_failure_degrades(tmp_path):
    w = worker(tmp_path, Sender(False), hb=False)
    w.outbox.stage(complete())
    w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "degraded" and h["pending_complete"] is True


def test_a_later_success_recovers_and_resets_the_streak(tmp_path):
    s = Sender(False)
    w = worker(tmp_path, s, hb=False)
    w.outbox.stage(complete())
    w.deliver_once()
    assert w.health_snapshot()["consecutive_failures"] >= 1
    s.mode = True
    w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "healthy" and h["consecutive_failures"] == 0
    assert h["pending_complete"] is False


def test_old_last_failure_at_remains_without_forcing_degraded(tmp_path):
    """Audit evidence must not imply current degradation."""
    s = Sender(False)
    w = worker(tmp_path, s, hb=False)
    w.outbox.stage(complete())
    w.deliver_once()
    s.mode = True
    w.deliver_once()
    h = w.health_snapshot()
    assert h["last_failure_at"] is not None, "historical evidence was erased"
    assert h["ct_delivery"] == "healthy", "history forced current degradation"


def test_a_retained_last_error_does_not_by_itself_degrade(tmp_path):
    s = Sender("400")
    w = worker(tmp_path, s, hb=False)
    w.outbox.stage(complete("old"))
    w.deliver_once()
    assert w.last_error and "400" in w.last_error
    s.mode = True
    w.outbox.stage(complete("new"))
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"


# ── 3. THE EXACT INCIDENT ───────────────────────────────────────────────────

def test_the_heartbeat_400_incident_recovers_exactly_as_specified(tmp_path):
    """beat N rejected 400 -> streak 1 -> beat N+1 accepted 200 -> pending
    false, delivered == N+1, healthy, streak 0."""
    s = Sender("400")
    w = worker(tmp_path, s)
    w.beat_if_due(force=True)
    seq_n = json.loads(w.outbox.heartbeat_path.read_text())["sequence"]
    w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "degraded" and h["consecutive_failures"] == 1
    assert "400" in (w.last_error or "")

    s.mode = True                                  # the Mac ingest fix lands
    w._last_beat = None
    w.deliver_once()                               # emits + delivers beat N+1
    seq_n1 = json.loads(w.outbox.heartbeat_path.read_text())["sequence"]
    h = w.health_snapshot()
    assert seq_n1 > seq_n
    assert h["pending_heartbeat"] is False
    assert h["delivered_sequences"]["heartbeat"] == seq_n1
    assert h["ct_delivery"] == "healthy", f"still latched: {h}"
    assert h["consecutive_failures"] == 0


def test_a_rejected_snapshot_does_not_poison_the_next_one(tmp_path):
    """The no-retry marking must not make FUTURE snapshots undeliverable."""
    s = Sender("400")
    w = worker(tmp_path, s, hb=False)
    w.outbox.stage(complete("poison"))
    w.deliver_once()
    n_before = len(s.sent)
    for _ in range(4):
        w.deliver_once()
    assert len(s.sent) == n_before, "the rejected snapshot was retried forever"
    s.mode = True
    w.outbox.stage(complete("fresh"))
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"
    assert s.sent[-1]["decisions"]["decisions"][0]["ob_id"] == "fresh"


# ── 4. multi-slot recovery ──────────────────────────────────────────────────

def test_A_heartbeat_succeeds_with_others_current_is_healthy(tmp_path):
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete())
    w.deliver_once()
    w._last_beat = None
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"


def test_C_complete_pending_stays_degraded_even_while_beats_succeed(tmp_path):
    """THE false-healthy defect: a succeeding heartbeat must not mask an
    undelivered strategy snapshot."""
    s = Sender(lambda p: False if p.get("decisions") else True)
    w = worker(tmp_path, s)
    w.outbox.stage(complete())
    w.deliver_once()
    for _ in range(5):
        w._last_beat = None
        w.deliver_once()
    h = w.health_snapshot()
    assert h["pending_complete"] is True
    assert h["ct_delivery"] == "degraded", "healthy while the complete slot never landed"


def test_D_one_slot_failing_while_others_succeed_stays_degraded(tmp_path):
    s = Sender(lambda p: False if p.get("decisions") else True)
    w = worker(tmp_path, s)
    w.outbox.stage(complete())
    for _ in range(6):
        w._last_beat = None
        w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "degraded"
    s.mode = True                                   # that slot finally lands
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"


def test_B_runtime_recovers_after_a_heartbeat_failure(tmp_path):
    s = Sender(False)
    w = worker(tmp_path, s)
    w.outbox.stage(runtime())
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "degraded"
    s.mode = True
    w._last_beat = None
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"


def test_a_superseded_failed_snapshot_is_not_still_actionable(tmp_path):
    """Only the CURRENT slot content matters; the old payload is gone."""
    s = Sender(False)
    w = worker(tmp_path, s, hb=False)
    w.outbox.stage(complete("old"))
    w.deliver_once()
    w.outbox.stage(complete("new"))
    s.mode = True
    w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "healthy"
    assert s.sent[-1]["decisions"]["decisions"][0]["ob_id"] == "new"
    assert "old" not in json.dumps(s.sent[-1])


# ── 11/12. bounded and side-effect-free ─────────────────────────────────────

def test_no_unbounded_state_or_history(tmp_path):
    s = Sender(False)
    w = worker(tmp_path, s)
    for i in range(300):
        w.outbox.stage(complete(f"c{i}"))
        w._last_beat = None
        w.deliver_once()
    assert len(w._delivered) <= 3 and len(w._outcome) <= 3
    assert len(list(w.outbox.dir.iterdir())) == 4
    assert sum(p.stat().st_size for p in w.outbox.dir.iterdir()) < 300_000


def test_reading_health_is_side_effect_free(tmp_path):
    s = Sender(False)
    w = worker(tmp_path, s)
    w.outbox.stage(complete())
    w.deliver_once()
    before = (w.consecutive_failures, dict(w._delivered), dict(w._outcome),
              len(s.sent), w.last_error, w.last_failure_at)
    for _ in range(15):
        w.health_snapshot()
    after = (w.consecutive_failures, dict(w._delivered), dict(w._outcome),
             len(s.sent), w.last_error, w.last_failure_at)
    assert before == after, "health_snapshot mutated delivery state"


def test_offline_after_a_long_streak(tmp_path):
    s = Sender(False)
    w = worker(tmp_path, s)
    for _ in range(12):
        w.outbox.stage(complete())
        w._last_beat = None
        w.deliver_once()
    assert w.health_snapshot()["ct_delivery"] == "offline"
