"""M-CT-FLEET-AUTHORITY-1 — liveness, readiness and delivery, from the node.

Three signals the Control Tower cannot honestly derive on its own:

  * NODE HEARTBEAT — the node publishes at recompute start then goes silent for
    ~20-25 minutes. Telemetry age was therefore never a liveness signal: a
    healthy long recompute and a process that died mid-recompute look
    identical. The beat now comes from the DELIVERY worker, which is alive
    throughout.
  * EXECUTION READINESS — computed by the SAME rail machinery execution uses.
    After L_2106, where telemetry and execution consulted different account
    authorities and the dashboard showed the reassuring one, a readiness signal
    derived independently would be a repeat of that defect with extra steps.
  * DELIVERY HEALTH — the VPS already knows; the Mac should not infer it from
    arrival timing.

No network, no MT5, no production live_state, and no order is ever submitted.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import news_feed as nf  # noqa: E402
from live.account_observation import (STATUS_OK, STATUS_UNAVAILABLE,  # noqa: E402
                                      AccountObservation, ObservedIdentity)
from live.arming import ArmRuntime  # noqa: E402
from live.safety import SafetyRails  # noqa: E402
from live.telemetry_outbox import (HEARTBEAT_INTERVAL_S, DeliveryWorker,  # noqa: E402
                                   NodeHeartbeat, TelemetryOutbox)

LOGIN, SERVER, DEMO = 9000000001, "FTMO-Demo", 0


# ── fixtures ────────────────────────────────────────────────────────────────

class Cfg:
    def __init__(self, root, mode="live"):
        self.state_dir = root
        self.kill_file = root / "KILL"
        self.mode = mode
        self.broker_symbol = "EURUSD"
        self.daily_loss_limit_r = 5.0
        self.max_open_positions = 6
        self.fixed_risk_lots = 0.01
        self.submission_disabled = False


class State:
    def __init__(self, mirror=0, realized=0.0):
        self._m, self._r = mirror, realized
        self.ledger_writes = []
    def ledger_status(self, i): return None
    def ledger_set(self, *a, **k): self.ledger_writes.append(a)
    def mirror_ticket(self, t): return None
    def broker_closed_ticket(self, t): return None
    def daily_realized_r(self, d): return self._r
    def open_mirror_count(self): return self._m
    def record_block(self, *a, **k): self.ledger_writes.append(("record_block",))


class News:
    def __init__(self, ok=True, reason=None): self._v = (ok, reason, "detail")
    def verdict(self, now=None): return self._v


def obs(*, available=True, server=SERVER, login=LOGIN, trade_mode=DEMO):
    if not available:
        return AccountObservation(sampled_at="2026-08-14T09:00:00Z",
                                  status=STATUS_UNAVAILABLE, identity=None)
    return AccountObservation(
        sampled_at="2026-08-14T09:00:00Z", status=STATUS_OK,
        identity=ObservedIdentity(login=login, server=server, currency="USD",
                                  trade_mode=trade_mode),
        identity_observed_at="2026-08-14T09:00:00Z")


class SpyArm:
    """Wraps the real token and records any budget consumption."""
    def __init__(self, inner):
        self._inner = inner
        self.consumed = 0
        self.type = inner.type
        self.remaining_attempts = inner.remaining_attempts
        self.disarmed = inner.disarmed
        self.context = inner.context
    def authorize_open(self, **kw): return self._inner.authorize_open(**kw)
    def consume_open_attempt(self, now=None):
        self.consumed += 1
        return self._inner.consume_open_attempt(now)


def rails(root, *, observation=None, arm=None, news=None, state=None, mode="live", cfg=None):
    o = obs() if observation is None else observation
    return SafetyRails(cfg or Cfg(root, mode), state or State(),
                       arm_runtime=arm if arm is not None else ArmRuntime.create_persistent(
                           root, login=LOGIN, server=SERVER, mode="live", daily_open_cap=12),
                       observed_account=o.as_arm_binding(),
                       news_gate=news or News())


# ── 12. EXECUTION READINESS — positive first ────────────────────────────────

def test_a_healthy_persistent_demo_posture_is_READY(tmp_path):
    """The positive case. A refusal-only suite is what hid L_2106."""
    r = rails(tmp_path).readiness(reconcile_frozen=False, today="2026-08-14")
    assert r["status"] == "ready", f"{r['reasons']} {r['checks']}"
    assert r["eligible"] is True and r["reasons"] == []
    for name in ("authorization", "account_identity", "demo_mode", "broker_connected",
                 "kill", "reconciliation", "news", "daily_open_cap", "max_positions",
                 "submission"):
        assert r["checks"][name]["ok"] is True, f"{name}: {r['checks'][name]}"


def test_candidate_specific_rails_are_named_not_assumed_passing(tmp_path):
    r = rails(tmp_path).readiness(reconcile_frozen=False)
    assert set(r["candidate_checks_not_evaluated"]) == {"duplicate_intent", "stale_open"}
    # a ready verdict must not silently include them
    assert "duplicate_intent" not in r["checks"]


def test_unreported_reconciliation_yields_partial_not_ready(tmp_path):
    """Unknown is never rounded up to ready."""
    r = rails(tmp_path).readiness(reconcile_frozen=None)
    assert r["status"] == "partial" and r["eligible"] is None
    assert "reconciliation" in r["reasons"]


@pytest.mark.parametrize("kw,check", [
    (dict(observation=obs(login=9000000999)), "authorization"),
    (dict(observation=obs(server="Other-Demo")), "authorization"),
    (dict(observation=obs(trade_mode=2)), "demo_mode"),
    (dict(observation=obs(trade_mode=None)), "demo_mode"),
    (dict(observation=obs(available=False)), "account_identity"),
    (dict(mode="dry_run"), "submission"),
    (dict(news=News(False, nf.REFUSE_STALE)), "news"),
    (dict(news=News(False, nf.REFUSE_BLACKOUT)), "news"),
    (dict(state=State(mirror=6)), "max_positions"),
    (dict(state=State(realized=-6.0)), "daily_loss"),
])
def test_each_blocker_reports_BLOCKED_and_names_itself(tmp_path, kw, check):
    r = rails(tmp_path, **kw).readiness(reconcile_frozen=False, today="2026-08-14")
    assert r["status"] == "blocked" and r["eligible"] is False
    assert check in r["reasons"], f"expected {check} in {r['reasons']}"


def test_kill_blocks(tmp_path):
    (tmp_path / "KILL").write_text("stop")
    r = rails(tmp_path).readiness(reconcile_frozen=False)
    assert r["status"] == "blocked" and "kill" in r["reasons"]


def test_reconcile_freeze_blocks(tmp_path):
    r = rails(tmp_path).readiness(reconcile_frozen=True)
    assert r["status"] == "blocked" and "reconciliation" in r["reasons"]


def test_daily_cap_exhausted_blocks(tmp_path):
    arm = ArmRuntime.create_persistent(tmp_path, login=LOGIN, server=SERVER,
                                       mode="live", daily_open_cap=1)
    arm.consume_open_attempt()
    r = rails(tmp_path, arm=ArmRuntime.load(tmp_path)).readiness(reconcile_frozen=False)
    assert r["status"] == "blocked"
    assert "daily_open_cap" in r["reasons"] or "authorization" in r["reasons"]


def test_revoked_authorization_blocks(tmp_path):
    arm = ArmRuntime.create_persistent(tmp_path, login=LOGIN, server=SERVER,
                                       mode="live", daily_open_cap=12)
    arm.revoke("operator")
    r = rails(tmp_path, arm=ArmRuntime.load(tmp_path)).readiness(reconcile_frozen=False)
    assert r["status"] == "blocked" and "authorization" in r["reasons"]


# ── 4/12. ZERO SIDE EFFECTS, proven by spies not by reading the source ──────

def test_readiness_consumes_no_open_attempt(tmp_path):
    spy = SpyArm(ArmRuntime.create_persistent(tmp_path, login=LOGIN, server=SERVER,
                                              mode="live", daily_open_cap=12))
    r = rails(tmp_path, arm=spy)
    for _ in range(25):
        r.readiness(reconcile_frozen=False)
    assert spy.consumed == 0, "readiness consumed an OPEN attempt"
    assert json.loads((tmp_path / "arm_token.json").read_text())["opens_today"] == 0


def test_readiness_writes_no_ledger_record(tmp_path):
    st = State()
    rails(tmp_path, state=st).readiness(reconcile_frozen=False)
    assert st.ledger_writes == [], f"readiness mutated the ledger: {st.ledger_writes}"


def test_readiness_calls_no_broker_method(tmp_path):
    """A gateway whose every attribute explodes: if readiness touched the
    broker at all, this raises."""
    class Boom:
        def __getattr__(self, name):
            raise AssertionError(f"readiness called broker method {name!r}")
    from live.executor import Executor
    ex = Executor(Cfg(tmp_path), State(), Boom(),
                  arm_runtime=ArmRuntime.create_persistent(
                      tmp_path, login=LOGIN, server=SERVER, mode="live", daily_open_cap=12),
                  observed_account=obs().as_arm_binding(), news_gate=News())
    out = ex.rails.readiness(reconcile_frozen=False)
    assert out["status"] in ("ready", "blocked", "partial")


def test_readiness_creates_no_intent(tmp_path):
    import live.intents as ints
    made = []
    orig = ints.OrderIntent
    class Trap(orig):
        def __init__(self, *a, **k):
            made.append(1)
            super().__init__(*a, **k)
    ints.OrderIntent = Trap
    try:
        rails(tmp_path).readiness(reconcile_frozen=False)
    finally:
        ints.OrderIntent = orig
    assert made == [], "readiness fabricated an intent"


def test_readiness_leaves_the_token_file_byte_identical(tmp_path):
    rails(tmp_path)                                   # creates the token
    before = (tmp_path / "arm_token.json").read_bytes()
    for _ in range(10):
        rails(tmp_path, arm=ArmRuntime.load(tmp_path)).readiness(reconcile_frozen=False)
    assert (tmp_path / "arm_token.json").read_bytes() == before


# ── 6/12. SAME AUTHORITY AS EXECUTION ───────────────────────────────────────

def test_readiness_and_real_rail_evaluation_agree(tmp_path):
    """17: equivalent inputs, equivalent verdicts -- for allow AND for refuse."""
    from live.intents import OPEN_POSITION, OrderIntent
    def intent():
        return OrderIntent(intent_id="x", action=OPEN_POSITION, trade_id="T",
                           side="long", frontier_bar="2026-08-14 09:00:00+00:00",
                           entry=1.1, stop=1.0, target=1.3)
    for kw, expect_ready in ((dict(), True),
                             (dict(observation=obs(server="Other")), False),
                             (dict(observation=obs(trade_mode=2)), False),
                             (dict(news=News(False, nf.REFUSE_STALE)), False)):
        r = rails(tmp_path, **kw)
        ready = r.readiness(reconcile_frozen=False, today="2026-08-14")["eligible"]
        allowed = r.evaluate(intent(), "EURUSD", "2026-08-14").allowed
        assert ready is expect_ready and allowed is expect_ready, \
            f"{kw}: readiness={ready} rail={allowed}"


def test_readiness_and_fingerprint_use_the_same_observation(tmp_path):
    from live.telemetry import account_fingerprint
    o = obs()
    r = rails(tmp_path, observation=o)
    assert r.observed_account == o.as_arm_binding()
    ident = o.as_observed_mapping()["identity"]
    assert account_fingerprint(r.observed_account["login"],
                               r.observed_account["server"]) == \
           account_fingerprint(ident.login, ident.server)
    assert r.readiness(reconcile_frozen=False)["account_fingerprint_source"] == \
        "canonical_cycle_observation"


def test_lost_observation_fails_closed(tmp_path):
    r = rails(tmp_path, observation=obs(available=False)).readiness(reconcile_frozen=False)
    assert r["status"] == "blocked" and r["eligible"] is False


def test_readiness_exposes_no_login_or_secret(tmp_path):
    import os
    blob = json.dumps(rails(tmp_path).readiness(reconcile_frozen=False))
    for banned in ("password", "login", "token", str(LOGIN)):
        assert banned not in blob, banned
    real = os.environ.get("MT5_LOGIN", "").strip()
    if real.isdigit():
        assert real not in blob


# ── 11. HEARTBEAT ───────────────────────────────────────────────────────────

class Sender:
    def __init__(self, up=True): self.up, self.sent = up, []
    def __call__(self, blob):
        self.sent.append(json.loads(blob))
        return (True, "200") if self.up else (False, "URLError: timed out")


def worker(tmp_path, sender, hb=True):
    ob = TelemetryOutbox(tmp_path)
    return DeliveryWorker(ob, "http://mac/api/live/ingest", sender=sender,
                          heartbeat=NodeHeartbeat("node-1", pid=4242) if hb else None)


def complete(seq="c1"):
    return {"schema_version": "ct.node-telemetry.v1",
            "cycle": {"status": "ok", "last_boundary": "2026-08-14 09:00:00+00:00"},
            "published_at": "2026-08-14T09:05:00Z",
            "news": {"health": "ok"},
            "decisions": {"count": 50, "decisions": [{"ob_id": seq}]}}


def test_heartbeat_continues_while_a_recompute_is_in_progress(tmp_path):
    """THE defect: ~20 minutes of silence during recompute."""
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete())
    w.deliver_once()
    w.heartbeat.update(phase="recomputing", boundary="2026-08-14 09:15:00+00:00")
    beats = 0
    for _ in range(6):                       # simulate 6 worker passes, no new cycle
        w._last_beat = None                  # force the interval to have elapsed
        s.sent.clear()
        w.deliver_once()
        hb = [p for p in s.sent if p.get("heartbeat")]
        beats += len(hb)
        assert hb, "no heartbeat emitted during recompute"
        assert hb[0]["heartbeat"]["phase"] == "recomputing"
    assert beats == 6


def test_heartbeat_does_not_refresh_runtime_or_complete_timestamps(tmp_path):
    """A beat must never make stale strategy data look current."""
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete())
    rt_before = w.outbox.runtime_path.read_bytes()
    cp_before = w.outbox.complete_path.read_bytes()
    for _ in range(5):
        w._last_beat = None
        w.beat_if_due()
    assert w.outbox.runtime_path.read_bytes() == rt_before, "heartbeat rewrote runtime slot"
    assert w.outbox.complete_path.read_bytes() == cp_before, "heartbeat rewrote complete slot"


def test_heartbeat_carries_the_three_ages_as_distinct_facts(tmp_path):
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete())
    w.beat_if_due(force=True)
    beat = json.loads(w.outbox.heartbeat_path.read_text())["heartbeat"]
    assert beat["emitted_at"] and beat["runtime_snapshot_at"] == "2026-08-14T09:05:00Z"
    assert beat["last_complete_at"] == "2026-08-14T09:05:00Z"
    assert beat["emitted_at"] != beat["runtime_snapshot_at"], \
        "the beat's own time must be distinguishable from the snapshot's"


def test_heartbeat_never_carries_news_or_decisions(tmp_path):
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete())
    w.heartbeat.update(phase="ok")
    w.outbox.stage_heartbeat({**complete(), "heartbeat": {"emitted_at": "x"}})
    stored = json.loads(w.outbox.heartbeat_path.read_text())
    assert "news" not in stored and "decisions" not in stored


def test_heartbeat_cannot_overwrite_latest_complete(tmp_path):
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete("KEEP"))
    for _ in range(30):
        w._last_beat = None
        w.beat_if_due()
    kept = json.loads(w.outbox.complete_path.read_text())
    assert kept["decisions"]["decisions"][0]["ob_id"] == "KEEP"


def test_heartbeat_respects_its_interval(tmp_path):
    w = worker(tmp_path, Sender())
    assert w.beat_if_due() is True            # first beat
    assert w.beat_if_due() is False           # inside the interval
    assert HEARTBEAT_INTERVAL_S <= 30, "cadence must keep a long recompute visibly alive"
    assert HEARTBEAT_INTERVAL_S >= 10, "a beat per second is pointless cost"


def test_heartbeat_stops_when_the_worker_stops(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.start(); w.stop()
    s.sent.clear()
    assert w._thread is None


def test_no_heartbeat_source_means_no_beat(tmp_path):
    w = worker(tmp_path, Sender(), hb=False)
    assert w.beat_if_due(force=True) is False
    assert not w.outbox.heartbeat_path.exists()


def test_heartbeat_contains_no_secrets(tmp_path):
    import os
    w = worker(tmp_path, Sender())
    w.beat_if_due(force=True)
    blob = w.outbox.heartbeat_path.read_text(encoding="utf-8")
    for banned in ("password", "login", "MT5_PASSWORD", "acctfp_"):
        assert banned not in blob, banned
    real = os.environ.get("MT5_LOGIN", "").strip()
    if real.isdigit():
        assert real not in blob


def test_heartbeat_delivery_failure_cannot_block_computation(tmp_path):
    """A dead Mac must not make a beat raise into anything."""
    s = Sender(up=False); w = worker(tmp_path, s)
    for _ in range(10):
        w._last_beat = None
        w.deliver_once()                      # must not raise
    assert w.consecutive_failures > 0
    assert w.outbox.heartbeat_path.exists()


def test_a_heartbeat_is_never_classified_complete(tmp_path):
    w = worker(tmp_path, Sender())
    w.beat_if_due(force=True)
    beat = json.loads(w.outbox.heartbeat_path.read_text())
    assert TelemetryOutbox.is_complete(beat) is False


# ── 13. OUTBOX / DELIVERY HEALTH ────────────────────────────────────────────

REQUIRED = ("ct_delivery", "last_success_at", "last_failure_at", "consecutive_failures",
            "pending_runtime", "pending_complete", "runtime_sequence",
            "complete_sequence", "retry_backoff_s", "next_retry_at")


def test_health_exposes_everything_the_fleet_view_needs(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete()); w.deliver_once()
    h = w.health_snapshot()
    for f in REQUIRED:
        assert f in h, f"missing {f}"
    assert h["ct_delivery"] in ("degraded", "offline")
    assert h["pending_complete"] is True and h["last_failure_at"]


def test_healthy_worker_reports_healthy_and_nothing_pending(tmp_path):
    s = Sender(); w = worker(tmp_path, s)
    w.outbox.stage(complete()); w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "healthy" and h["consecutive_failures"] == 0
    assert h["pending_complete"] is False and h["pending_runtime"] is False


def test_recovery_returns_to_healthy_and_resets_the_count(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete())
    for _ in range(4):
        w.deliver_once()
    assert w.health_snapshot()["consecutive_failures"] >= 4
    s.up = True
    w.deliver_once()
    h = w.health_snapshot()
    assert h["ct_delivery"] == "healthy" and h["consecutive_failures"] == 0


def test_reading_health_does_not_alter_delivery_state(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    w.outbox.stage(complete()); w.deliver_once()
    before = (w.consecutive_failures, len(s.sent))
    for _ in range(10):
        w.health_snapshot()
    assert (w.consecutive_failures, len(s.sent)) == before


def test_storage_stays_bounded_with_heartbeats(tmp_path):
    s = Sender(up=False); w = worker(tmp_path, s)
    for i in range(200):
        w.outbox.stage(complete(f"c{i}"))
        w._last_beat = None
        w.deliver_once()
    files = sorted(p.name for p in w.outbox.dir.iterdir())
    assert files == ["delivery_health.json", "latest_complete.json",
                     "latest_heartbeat.json", "latest_runtime.json"], files
    assert sum(p.stat().st_size for p in w.outbox.dir.iterdir()) < 300_000


def test_published_delivery_block_omits_the_target_url(tmp_path):
    """The tailnet address is the operator's; the Mac knows who it called."""
    from live.main import _delivery_block
    s = Sender(); w = worker(tmp_path, s)
    class Pub:
        delivery_worker = w
    b = _delivery_block(Pub())
    assert b["schema_version"] == "ct.node-delivery.v1"
    assert "target" not in b and "100." not in json.dumps(b)


# ── 8. two-slot semantics preserved ─────────────────────────────────────────

def test_transition_still_cannot_destroy_the_complete_slot(tmp_path):
    w = worker(tmp_path, Sender())
    w.outbox.stage(complete("KEEP"))
    w.outbox.stage({"cycle": {"status": "recomputing"}})
    w._last_beat = None; w.beat_if_due()
    kept = json.loads(w.outbox.complete_path.read_text())
    assert kept["decisions"]["decisions"][0]["ob_id"] == "KEEP"
    assert json.loads(w.outbox.runtime_path.read_text())["cycle"]["status"] == "recomputing"


def test_sequences_remain_monotonic_across_all_three_slots(tmp_path):
    ob = TelemetryOutbox(tmp_path)
    seqs = [ob.stage(complete())["sequence"],
            ob.stage_heartbeat({"heartbeat": {}})["sequence"],
            ob.stage({"cycle": {"status": "recomputing"}})["sequence"]]
    assert seqs == sorted(seqs) and len(set(seqs)) == 3


# ── deferred imports: the production-only path ──────────────────────────────

def test_every_deferred_import_inside_build_and_cycle_resolves():
    """`import live.main` does NOT exercise imports written inside functions.

    That gap took the node down: `from live.telemetry import INSTANCE_ID` was
    added inside build(), the module imported fine, the whole suite passed, and
    the process died on boot with ImportError because INSTANCE_ID lives in
    `live/__init__.py`. Function-local imports are a deliberate pattern here
    (keeping heavy modules off the import path), so they need their own guard.
    """
    import ast
    import importlib
    src = (REPO_ROOT / "live" / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    targets = [n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name in ("build", "cycle", "main")]
    assert targets, "build/cycle not found"
    missing = []
    for fn in targets:
        for node in ast.walk(fn):
            if isinstance(node, ast.ImportFrom) and node.module:
                try:
                    mod = importlib.import_module(node.module)
                except Exception as exc:
                    missing.append(f"{fn.name}: import {node.module} -> {exc}")
                    continue
                for alias in node.names:
                    if not hasattr(mod, alias.name):
                        try:
                            importlib.import_module(f"{node.module}.{alias.name}")
                        except Exception:
                            missing.append(
                                f"{fn.name}: {alias.name!r} not in {node.module}")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    try:
                        importlib.import_module(alias.name)
                    except Exception as exc:
                        missing.append(f"{fn.name}: import {alias.name} -> {exc}")
    assert not missing, "deferred imports that would fail at boot:\n  " + "\n  ".join(missing)
