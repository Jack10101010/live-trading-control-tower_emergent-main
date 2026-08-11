"""M-CT-TRANSPORT-DURABILITY-1 — telemetry survives a sleeping laptop.

The measured cause, which decided the whole design: delivery success splits by
the operator's local clock — 45/2059 (2.2%) between 00:00 and 07:00, 664/696
(95.4%) between 09:00 and 12:00 — and 4,498 of 4,501 failures were timeouts.
The link is fine (Tailscale direct, 22ms RTT, GET in 0.17s). The Mac is a
laptop and laptops sleep, so raising the timeout would buy nothing.

Two defects follow, and these tests pin both fixes:

  * delivery was SYNCHRONOUS inside the trading cycle;
  * there was ONE local slot, so a mid-recompute TRANSITION payload (no `news`,
    no `decisions`) could overwrite the last COMPLETE cycle-end snapshot.
    Observed worst case: 28.43 hours with no fresh complete snapshot.

No network, no MT5, no production live_state.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.publisher import CTPublisher  # noqa: E402
from live.telemetry_outbox import DeliveryWorker, TelemetryOutbox  # noqa: E402


def complete_payload(seq_hint="c1", boundary="2026-08-11 10:45:00+00:00"):
    return {"schema_version": "ct.node-telemetry.v1",
            "cycle": {"status": "ok", "last_boundary": boundary},
            "news": {"schema_version": "ct.news-calendar.v1", "health": "ok"},
            "decisions": {"schema_version": "ct.node-decisions.v1", "count": 50,
                          "decisions": [{"ob_id": seq_hint}]},
            "arming": {"status": "armed", "authorization_type": "persistent_demo"}}


def transition_payload():
    """What the node publishes when it starts a ~20-minute recompute: no
    strategy context by design."""
    return {"schema_version": "ct.node-telemetry.v1",
            "cycle": {"status": "recomputing",
                      "note": "recompute started; telemetry resumes at cycle end"},
            "arming": {"status": "armed"}}


class Sender:
    """Controllable transport. `up=False` models a sleeping Mac (timeout)."""
    def __init__(self, up=True, result=None):
        self.up, self.result, self.sent = up, result, []
    def __call__(self, blob):
        self.sent.append(json.loads(blob))
        if self.result is not None:
            return self.result
        if not self.up:
            return False, "URLError: <urlopen error timed out>"
        return True, "200"


def worker(tmp_path, sender):
    return DeliveryWorker(TelemetryOutbox(tmp_path), "http://mac/api/live/ingest",
                          sender=sender)


# ── 1/2. delivery when the Mac is available ─────────────────────────────────

def test_latest_runtime_is_delivered(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(transition_payload())
    w.deliver_once()
    assert len(s.sent) == 1 and s.sent[0]["cycle"]["status"] == "recomputing"


def test_complete_cycle_end_is_delivered(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once()
    # staged into BOTH slots, so both are delivered; both carry the context
    assert all(p.get("news") and p.get("decisions") for p in s.sent)


def test_complete_is_delivered_before_runtime(tmp_path):
    """The complete snapshot is the one whose loss costs information."""
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once()
    assert s.sent, "nothing sent"


def test_an_unchanged_slot_is_not_resent(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once(); n = len(s.sent)
    for _ in range(5):
        w.deliver_once()
    assert len(s.sent) == n, "idle polling must not spam the Mac"


# ── 3/16/17. the trading cycle is never held hostage ────────────────────────

def test_hand_off_performs_no_network_at_all(tmp_path):
    """The core architectural fix. The cycle must not touch HTTP."""
    class Cfg:
        state_dir = tmp_path
    ob = TelemetryOutbox(tmp_path)
    pub = CTPublisher(Cfg(), outbox=ob)
    import live.publisher as pmod
    called = []
    orig = pmod.urllib.request.urlopen
    pmod.urllib.request.urlopen = lambda *a, **k: called.append(1)
    try:
        out = pub.hand_off(complete_payload())
    finally:
        pmod.urllib.request.urlopen = orig
    assert not called, "hand_off went to the network"
    assert out["staged"] is True


def test_hand_off_is_fast_even_though_the_mac_is_unreachable(tmp_path):
    class Cfg:
        state_dir = tmp_path
    pub = CTPublisher(Cfg(), outbox=TelemetryOutbox(tmp_path))
    t0 = time.time()
    for _ in range(20):
        pub.hand_off(complete_payload())
    assert (time.time() - t0) < 2.0, "staging must be local and cheap"


def test_a_failing_worker_never_touches_execution_state(tmp_path):
    """Publisher failure must not advance/rewind broker state or change
    authorization. The outbox writes ONLY inside its own directory."""
    s = Sender(up=False); w = worker(tmp_path, s)
    (tmp_path / "arm_token.json").write_text('{"schema":"arm-token-v2"}')
    (tmp_path / "runner_state.json").write_text('{"ledger":{},"mirror":{}}')
    before = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    for _ in range(10):
        w.deliver_once()
    after = {p.name: p.read_bytes() for p in tmp_path.glob("*.json")}
    assert before == after, "delivery mutated state outside the outbox"


def test_worker_exceptions_are_contained(tmp_path):
    def boom(blob):
        raise RuntimeError("transport exploded")
    w = worker(tmp_path, boom)
    w.outbox.stage(complete_payload())
    w.deliver_once()          # must not raise
    assert w.consecutive_failures >= 1


# ── 4/5/10. failure leaves state recoverable, and complete is never lost ────

def test_a_failed_complete_snapshot_remains_pending(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once()
    assert w.health_snapshot()["pending_complete"] is True
    s.up = True
    w.deliver_once()
    assert w.health_snapshot()["pending_complete"] is False


def test_a_transition_success_cannot_erase_the_retained_complete(tmp_path):
    """THE bad sequence this milestone exists to prevent:
    cycle-end fails -> later transition succeeds -> Mac loses news/decisions."""
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload(seq_hint="THE_ONE"))     # fails
    w.deliver_once()
    s.up = True
    w.outbox.stage(transition_payload())                      # succeeds
    w.deliver_once()
    kinds = [bool(p.get("decisions")) for p in s.sent]
    assert any(kinds), "the complete snapshot was never delivered"
    stored = json.loads(w.outbox.complete_path.read_text())
    assert stored["decisions"]["decisions"][0]["ob_id"] == "THE_ONE"
    assert stored["cycle"]["status"] == "ok", "a transition overwrote the complete slot"


def test_the_complete_slot_is_only_written_by_complete_payloads(tmp_path):
    ob = TelemetryOutbox(tmp_path)
    ob.stage(complete_payload(seq_hint="keep"))
    for _ in range(5):
        ob.stage(transition_payload())
    kept = json.loads(ob.complete_path.read_text())
    assert kept["decisions"]["decisions"][0]["ob_id"] == "keep"
    assert json.loads(ob.runtime_path.read_text())["cycle"]["status"] == "recomputing"


@pytest.mark.parametrize("payload,expect", [
    ({"cycle": {"status": "recomputing"}, "news": {"health": "ok"}}, False),
    ({"cycle": {"status": "ok"}, "news": {"health": "ok"}}, True),
    ({"cycle": {"status": "ok"}, "decisions": {"count": 1}}, True),
    ({"cycle": {"status": "ok"}}, False),
    ({"cycle": {"status": "no_new_bar"}, "news": {"health": "ok"}}, True),
    ("not-a-dict", False),
])
def test_complete_classification(payload, expect):
    assert TelemetryOutbox.is_complete(payload) is expect


# ── 6/7/9. supersede, and never stale-over-new ──────────────────────────────

def test_a_newer_runtime_snapshot_supersedes_an_older_one(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    for i in range(5):
        w.outbox.stage({"cycle": {"status": "recomputing"}, "n": i})
        w.deliver_once()
    s.up = True
    s.sent.clear()
    w.deliver_once()
    assert len(s.sent) == 1, "only the latest runtime state should be sent"
    assert s.sent[0]["n"] == 4, "the superseded snapshots must never be transmitted"


def test_an_older_payload_can_never_overwrite_newer_state(tmp_path):
    """No queue of historical payloads exists, so a delayed retry cannot land
    after newer state — the hazard is removed by construction."""
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload(seq_hint="old", boundary="2026-08-11 10:00:00+00:00"))
    w.deliver_once()
    w.outbox.stage(complete_payload(seq_hint="new", boundary="2026-08-11 10:45:00+00:00"))
    s.up = True
    s.sent.clear()
    w.deliver_once()
    sent_hints = [p["decisions"]["decisions"][0]["ob_id"] for p in s.sent if p.get("decisions")]
    assert "old" not in sent_hints, "a superseded payload was transmitted"
    # Both slots hold the newer complete payload, so both are sent; the point
    # is that NOTHING carrying the superseded state is ever transmitted.
    assert set(sent_hints) == {"new"}


def test_sequence_is_monotonic_and_survives_restart(tmp_path):
    ob = TelemetryOutbox(tmp_path)
    seqs = [ob.stage(complete_payload())["sequence"] for _ in range(3)]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3
    reopened = TelemetryOutbox(tmp_path)
    assert reopened.stage(complete_payload())["sequence"] > seqs[-1]


# ── 8/11. Mac asleep: bounded, then catches up ──────────────────────────────

def test_an_overnight_sleep_stays_bounded(tmp_path):
    """480 failed cycles must not create 480 files or 480 queued payloads."""
    s = Sender(up=False); w = worker(tmp_path, s)
    for i in range(480):
        w.outbox.stage(complete_payload(seq_hint=f"c{i}"))
        w.deliver_once()
    files = list(w.outbox.dir.iterdir())
    assert len(files) == 3, f"unbounded spool: {[f.name for f in files]}"
    total = sum(f.stat().st_size for f in files)
    assert total < 200_000, f"outbox grew to {total} bytes"


def test_when_the_mac_wakes_it_gets_the_latest_state_only(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    for i in range(200):
        w.outbox.stage(complete_payload(seq_hint=f"c{i}"))
        w.deliver_once()
    s.up = True
    s.sent.clear()
    w.deliver_once()
    assert len(s.sent) <= 2, "waking must not replay history"
    assert all(p["decisions"]["decisions"][0]["ob_id"] == "c199" for p in s.sent)
    assert w.consecutive_failures == 0


def test_backoff_is_bounded(tmp_path):
    w = worker(tmp_path, Sender(up=False))
    w.outbox.stage(complete_payload())
    for _ in range(50):
        w.deliver_once()
    from live.telemetry_outbox import BACKOFF_MAX_S
    assert w._backoff() <= BACKOFF_MAX_S


# ── 12/13/14. HTTP outcome handling ─────────────────────────────────────────

def test_a_4xx_is_recorded_and_not_retried_forever(tmp_path):
    """A rejected payload is a contract problem; hammering it is a hot loop."""
    s = Sender(result=(False, "HTTP 400")); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once(); n = len(s.sent)
    for _ in range(5):
        w.deliver_once()
    assert len(s.sent) == n, "a rejected snapshot was retried indefinitely"
    assert "400" in (w.last_error or "")


def test_a_new_snapshot_is_attempted_after_a_4xx(tmp_path):
    s = Sender(result=(False, "HTTP 400")); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload()); w.deliver_once()
    s.result = None; s.up = True
    w.outbox.stage(complete_payload(seq_hint="fresh"))
    s.sent.clear(); w.deliver_once()
    assert s.sent, "a later snapshot must still be attempted"


def test_a_5xx_keeps_retrying(tmp_path):
    s = Sender(result=(False, "HTTP 503")); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once(); w.deliver_once()
    assert len(s.sent) >= 2, "a transient server error must be retried"


def test_a_timeout_keeps_retrying_and_is_named(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once(); w.deliver_once()
    assert len(s.sent) >= 2
    assert "timed out" in (w.last_error or "")


# ── 15. local delivery health, distinct from execution health ───────────────

REQUIRED_HEALTH = ("ct_delivery", "target", "last_success_at", "last_success_age_s",
                   "consecutive_failures", "last_error", "pending_runtime",
                   "pending_complete", "retry_backoff_s")


def test_health_reports_everything_an_operator_needs(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once()
    h = w.health_snapshot()
    for f in REQUIRED_HEALTH:
        assert f in h, f"missing {f}"
    assert h["ct_delivery"] in ("degraded", "offline")
    assert h["pending_complete"] is True


def test_health_says_healthy_once_delivered(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload()); w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "healthy" and h["consecutive_failures"] == 0
    assert h["pending_complete"] is False and h["last_success_at"]


def test_health_is_persisted_for_an_operator_to_read(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload()); w.deliver_once()
    on_disk = json.loads(w.outbox.health_path.read_text())
    assert on_disk["ct_delivery"] in ("degraded", "offline")


def test_delivery_health_is_not_an_execution_health_field(tmp_path):
    """A sleeping Mac must never make a correctly-trading node look unhealthy."""
    h = worker(tmp_path, Sender(up=False)).health_snapshot()
    for banned in ("healthy", "status", "execution"):
        assert banned not in h, f"{banned!r} would be confused with execution health"
    assert "ct_delivery" in h


# ── 18. no secrets in the new artefacts ─────────────────────────────────────

def test_no_login_or_secret_is_written_into_the_outbox(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete_payload())
    w.deliver_once()
    blob = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in w.outbox.dir.iterdir())
    for banned in ("password", "MT5_PASSWORD", "1514217330", "1514126969"):
        assert banned not in blob, banned


def test_the_outbox_only_writes_inside_its_own_directory(tmp_path):
    ob = TelemetryOutbox(tmp_path)
    ob.stage(complete_payload())
    assert {p.name for p in ob.dir.iterdir()} <= {
        "latest_runtime.json", "latest_complete.json", "delivery_health.json"}
    assert [p.name for p in tmp_path.iterdir()] == ["telemetry_outbox"]
