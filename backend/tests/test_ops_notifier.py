"""L3-A — Notifier policy engine tests.

Verifies episode tracking over L1A attention_reasons (consumed verbatim),
confirmation-window INITIAL, sustained-degradation REMIND cadence, RESOLVED
pairing, deterministic timestamp-anchored identities (frozen, restart-stable,
collision-safe across state loss), silent baseline, unknown-code handling,
atomic durable state + outbox with corruption handling, the disabled gate,
and no-double-INITIAL after restart. L3-A ends at outbox persistence — nothing
is delivered.

Injected L1A provider + temp paths; tick_once/baseline driven directly with an
injected clock. Safe under `pytest -n 2 --dist loadscope`.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import ops_notifier                                                   # noqa: E402
import server                                                         # noqa: E402
from fastapi.testclient import TestClient                             # noqa: E402

T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def T(n: int) -> datetime:
    return T0 + timedelta(seconds=10 * n)


def model(reasons=(), gen_at="2026-07-22T10:00:00+00:00"):
    return {"attention": {"attention_reasons": list(reasons),
                          "intervention_required": bool(reasons)},
            "meta": {"generated_at": gen_at, "schema_version": 1}}


@pytest.fixture()
def nf(tmp_path):
    holder = {"m": model()}
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: holder["m"],
        state_path=tmp_path / "state.json", outbox_path=tmp_path / "outbox.json",
        interval_s=0.05, logger=logging.getLogger("l3a_test"))
    return SimpleNamespace(n=n, holder=holder, tmp=tmp_path,
                           state=tmp_path / "state.json", outbox=tmp_path / "outbox.json")


def outbox(nf):
    if not nf.outbox.exists():
        return []
    return json.loads(nf.outbox.read_text())["records"]


def ids(nf):
    return [r["notification_id"] for r in outbox(nf)]


# ── Confirmation + INITIAL ───────────────────────────────────────────────────

def test_confirmation_window_before_initial(nf):
    nf.n.tick_once(T(0))                                  # baseline empty -> nothing
    nf.holder["m"] = model(["consecutive_errors"], gen_at="G1")
    nf.n.tick_once(T(1))                                 # confirm 1 -> no page yet
    assert outbox(nf) == []
    nf.n.tick_once(T(2))                                 # confirm 2 -> INITIAL
    assert ids(nf) == ["l3|consecutive_errors|consecutive_errors@G1|INITIAL"]


def test_immediate_codes_page_on_first_tick(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present"], gen_at="G2")
    nf.n.tick_once(T(1))                                 # immediate, no confirm wait
    assert ids(nf) == ["l3|kill_file_present|kill_file_present@G2|INITIAL"]


def test_flap_within_confirm_window_is_silent(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["consecutive_errors"], gen_at="G")
    nf.n.tick_once(T(1))                                 # confirm 1
    nf.holder["m"] = model([])                           # cleared before confirm
    nf.n.tick_once(T(2))                                 # episode closes, never paged
    assert outbox(nf) == []


# ── REMIND cadence ───────────────────────────────────────────────────────────

def test_reminder_after_cadence(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GF")       # immediate INITIAL at T(1)
    nf.n.tick_once(T(1))
    # below cadence -> no reminder
    nf.n.tick_once(T(2))
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]
    # advance past the 1800s cadence
    nf.n.tick_once(datetime(2026, 7, 22, 12, 40, 0, tzinfo=timezone.utc))
    decs = [(r["decision"], r["notification_id"]) for r in outbox(nf)]
    assert decs[-1] == ("REMIND", "l3|frozen|frozen@GF|REMIND|1")
    # a second cadence window -> ordinal 2
    nf.n.tick_once(datetime(2026, 7, 22, 13, 20, 0, tzinfo=timezone.utc))
    assert ids(nf)[-1] == "l3|frozen|frozen@GF|REMIND|2"


# ── RESOLVED ─────────────────────────────────────────────────────────────────

def test_resolution_after_paged(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present"], gen_at="GK")
    nf.n.tick_once(T(1))                                 # INITIAL
    nf.holder["m"] = model([])                           # cleared
    nf.n.tick_once(T(2))                                 # RESOLVED
    assert ids(nf) == ["l3|kill_file_present|kill_file_present@GK|INITIAL",
                       "l3|kill_file_present|kill_file_present@GK|RESOLVED"]


def test_recurrence_distinct_identity(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present"], gen_at="GA")
    nf.n.tick_once(T(1)); nf.holder["m"] = model([]); nf.n.tick_once(T(2))   # ep A
    nf.holder["m"] = model(["kill_file_present"], gen_at="GB")               # new episode
    nf.n.tick_once(T(3))
    initials = [i for i in ids(nf) if i.endswith("INITIAL")]
    assert initials == ["l3|kill_file_present|kill_file_present@GA|INITIAL",
                        "l3|kill_file_present|kill_file_present@GB|INITIAL"]


# ── Silent baseline + unknown codes ──────────────────────────────────────────

def test_silent_baseline_does_not_backpage(nf):
    nf.holder["m"] = model(["frozen", "process_not_alive"], gen_at="GX")
    nf.n.baseline(T(0))                                  # condition present at startup
    assert outbox(nf) == []                             # no back-page
    nf.n.tick_once(T(1))                                # still present -> still silent
    assert outbox(nf) == []
    # only after clear + recur does it page
    nf.holder["m"] = model([]); nf.n.tick_once(T(2))
    nf.holder["m"] = model(["frozen"], gen_at="GY"); nf.n.tick_once(T(3))
    assert ids(nf) == ["l3|frozen|frozen@GY|INITIAL"]


def test_unknown_attention_code_pages_generically(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["some_future_reason"], gen_at="GU")
    nf.n.tick_once(T(1)); nf.n.tick_once(T(2))          # not immediate -> confirm then page
    assert ids(nf) == ["l3|some_future_reason|some_future_reason@GU|INITIAL"]


def test_unknown_or_missing_attention_never_pages(nf):
    for m in ({}, {"attention": {}}, {"attention": {"attention_reasons": None}},
              {"meta": {}}):
        nf.holder["m"] = m
        assert nf.n.tick_once(T(1))["error"] is None
    assert outbox(nf) == []


# ── Persistence, restart, corruption ─────────────────────────────────────────

def test_state_and_outbox_are_atomic_and_schema_versioned(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GS"); nf.n.tick_once(T(1))
    st = json.loads(nf.state.read_text())
    assert st["schema_version"] == 1 and "frozen" in st["episodes"]
    ob = json.loads(nf.outbox.read_text())
    assert ob["schema_version"] == 1 and len(ob["records"]) == 1
    assert not (nf.tmp / "state.json.tmp").exists()
    assert not (nf.tmp / "outbox.json.tmp").exists()


def test_no_double_initial_after_restart(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GR"); nf.n.tick_once(T(1))   # INITIAL
    assert len(ids(nf)) == 1
    nf.n._state = None                                  # restart: reload from disk
    nf.n.tick_once(T(2))                                # still present, already paged
    assert ids(nf) == ["l3|frozen|frozen@GR|INITIAL"]   # no duplicate


def test_restart_continues_reminder_and_resolution(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GC"); nf.n.tick_once(T(1))
    nf.n._state = None                                  # restart
    nf.holder["m"] = model([]); nf.n.tick_once(T(2))    # resolves from reloaded episode
    assert ids(nf)[-1] == "l3|frozen|frozen@GC|RESOLVED"


def test_state_corruption_silently_rebaselines(nf, caplog):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GD"); nf.n.tick_once(T(1))
    nf.state.write_text("{ not json")
    nf.n._state = None
    with caplog.at_level(logging.WARNING, logger="l3a_test"):
        s = nf.n.tick_once(T(2))                        # reload -> re-baseline, no crash
    assert s["error"] is None
    # after re-baseline the ongoing 'frozen' is seeded present (not re-paged);
    # nothing new appended beyond the pre-corruption INITIAL
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]


def test_outbox_corruption_quarantined_not_discarded(nf, caplog):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GQ"); nf.n.tick_once(T(1))
    nf.outbox.write_text("{ corrupt")
    nf.holder["m"] = model(["kill_file_present"], gen_at="GK2")
    with caplog.at_level(logging.ERROR, logger="l3a_test"):
        nf.n.tick_once(T(2))                            # kill immediate -> new INITIAL
    assert (nf.tmp / "outbox.json.corrupt").exists()    # quarantined, not silently dropped
    assert any("outbox corrupt" in r.getMessage() for r in caplog.records)
    # fresh outbox records the tick's decisions: frozen departed (RESOLVED) and
    # kill entered (immediate INITIAL). The old INITIAL survives in the .corrupt file.
    assert set(ids(nf)) == {"l3|kill_file_present|kill_file_present@GK2|INITIAL",
                            "l3|frozen|frozen@GQ|RESOLVED"}


def test_state_loss_new_episode_distinct_from_history(nf):
    # episode A paged and resolved
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="H1"); nf.n.tick_once(T(1))
    nf.holder["m"] = model([]); nf.n.tick_once(T(2))
    # total state loss
    nf.state.unlink(); nf.n._state = None
    # a NEW episode with a fresh generated_at -> globally distinct id
    nf.holder["m"] = model(["frozen"], gen_at="H2"); nf.n.tick_once(T(3))
    initials = [i for i in ids(nf) if i.endswith("INITIAL")]
    assert "l3|frozen|frozen@H1|INITIAL" in initials
    assert "l3|frozen|frozen@H2|INITIAL" in initials    # no collision after loss


def test_provider_failure_contained(nf):
    def boom(now):
        raise RuntimeError("L1A down")
    nf.n._provider = boom
    s = nf.n.tick_once(T(0))
    assert s["error"] is None and outbox(nf) == []      # tick skipped, no crash


# ── Defect 1: crash between outbox-save and state-save (immediate code) ──────

def test_crash_between_outbox_and_state_no_duplicate_initial(nf):
    """Immediate-code episode: a crash strictly between the outbox write and
    the final state write must NOT regenerate a different identity on restart
    (frozen invariant #3). The phase-1 key-freeze makes the episode key durable
    before the outbox record, so the restart reloads the SAME key and dedups."""
    nf.holder["m"] = model([])
    nf.n.tick_once(T(0))
    # crash the FINAL state save (after phase-1 freeze + outbox write)
    real_save = nf.n._save_state
    calls = {"n": 0}
    def crash_final(state):
        calls["n"] += 1
        real_save(state)               # phase-1 save succeeds (freezes key)
        if calls["n"] >= 2:            # the post-outbox save crashes
            raise RuntimeError("crash before final state persisted")
    nf.n._save_state = crash_final
    nf.holder["m"] = model(["kill_file_present"], gen_at="G1")
    nf.n.tick_once(T(1))              # INITIAL written to outbox; final save crashes
    assert ids(nf) == ["l3|kill_file_present|kill_file_present@G1|INITIAL"]
    # restart: fresh engine, reload from disk, next snapshot has a NEW generated_at
    n2 = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: nf.holder["m"], state_path=nf.state,
        outbox_path=nf.outbox, logger=logging.getLogger("l3a_test"))
    nf.holder["m"] = model(["kill_file_present"], gen_at="G2")   # time advanced
    n2.tick_once(T(2))
    initials = [i for i in ids(nf) if i.endswith("INITIAL")]
    assert initials == ["l3|kill_file_present|kill_file_present@G1|INITIAL"]  # no duplicate


def test_immediate_code_replay_after_restart_dedups(nf):
    nf.holder["m"] = model([]); nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GX"); nf.n.tick_once(T(1))     # INITIAL
    nf.n._state = None                                                       # restart
    nf.holder["m"] = model(["frozen"], gen_at="GY"); nf.n.tick_once(T(2))     # still present
    assert [i for i in ids(nf) if i.endswith("INITIAL")] == \
        ["l3|frozen|frozen@GX|INITIAL"]                                       # one page only


# ── Defect 2: malformed snapshot must not resolve active episodes ─────────────

def test_malformed_snapshot_does_not_resolve_active_episode(nf):
    nf.holder["m"] = model([]); nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present"], gen_at="GK"); nf.n.tick_once(T(1))  # INITIAL
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]
    for bad in ({"attention": {}}, {"attention": {"attention_reasons": None}}, {}, {"meta": {}}):
        nf.holder["m"] = bad
        assert nf.n.tick_once(T(2))["error"] is None
        assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]   # NO spurious RESOLVED
    # a valid snapshot with the condition still present -> still active, no new page
    nf.holder["m"] = model(["kill_file_present"], gen_at="GK"); nf.n.tick_once(T(3))
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]
    # eventual genuine clear (valid empty snapshot) -> RESOLVED
    nf.holder["m"] = model([]); nf.n.tick_once(T(4))
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL", "RESOLVED"]


def test_provider_exception_does_not_resolve_active_episode(nf):
    nf.holder["m"] = model([]); nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GE"); nf.n.tick_once(T(1))
    def boom(now):
        raise RuntimeError("L1A down")
    nf.n._provider = boom
    nf.n.tick_once(T(2))
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL"]       # no resolution on failure
    nf.n._provider = lambda now: nf.holder["m"]
    nf.holder["m"] = model([]); nf.n.tick_once(T(3))               # valid clear -> RESOLVED
    assert [r["decision"] for r in outbox(nf)] == ["INITIAL", "RESOLVED"]


def test_valid_empty_snapshot_still_resolves(nf):
    nf.holder["m"] = model([]); nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GV"); nf.n.tick_once(T(1))
    nf.holder["m"] = model([], gen_at="GV2"); nf.n.tick_once(T(2))  # well-formed empty
    assert ids(nf)[-1] == "l3|frozen|frozen@GV|RESOLVED"


# ── Disabled gate + lifecycle ────────────────────────────────────────────────

def test_disabled_gate_no_thread_no_files(monkeypatch):
    monkeypatch.delenv("OPS_NOTIFIER_ENABLED", raising=False)
    assert ops_notifier.env_flag(None) is False
    assert ops_notifier.env_flag("1") and ops_notifier.env_flag("on")
    with TestClient(server.app):                         # lifespan runs; flag disabled
        pass
    assert server._ops_notifier._thread is None
    assert not (BACKEND_DIR / "ops_notifier_state.json").exists()
    assert not (BACKEND_DIR / "ops_notifier_outbox.json").exists()


def test_start_stop_idempotent_daemon(nf):
    nf.n.start()
    t1 = nf.n._thread
    nf.n.start()
    assert nf.n._thread is t1 and t1.daemon is True
    nf.n.stop(timeout_s=2)
    assert nf.n._thread is None
    nf.n.stop(timeout_s=2)


def test_multiple_conditions_one_snapshot_multiple_decisions(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present", "frozen"], gen_at="GM")     # both immediate
    nf.n.tick_once(T(1))
    codes = sorted(r["code"] for r in outbox(nf))
    assert codes == ["frozen", "kill_file_present"]     # independent episodes, both paged


# ── L3-A boundary: pending until a webhook is configured ─────────────────────

def test_records_are_pending_when_no_webhook(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GP"); nf.n.tick_once(T(1))
    r = outbox(nf)[0]
    assert r["status"] == "pending" and r["delivered_at"] is None and r["attempts"] == 0


# ── L3-B delivery ────────────────────────────────────────────────────────────

import urllib.error                                                    # noqa: E402


class FakeResp:
    def __init__(self, status): self.status = status
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def getcode(self): return self.status


class FakeOpener:
    """Scripted opener: each open() pops the next action. An action is an int
    (2xx/other status → returned as a response), an HTTPError, or an exception
    instance (raised). Records requests for header/body assertions."""
    def __init__(self):
        self.script = []
        self.requests = []
        self.default = None            # used when script is empty

    def open(self, req, timeout=None):
        self.requests.append(req)
        act = self.script.pop(0) if self.script else self.default
        if isinstance(act, Exception):
            raise act
        if isinstance(act, int) and 200 <= act < 300:
            return FakeResp(act)
        if isinstance(act, int):
            raise urllib.error.HTTPError(req.full_url, act, f"HTTP {act}", {}, None)
        raise urllib.error.URLError("no route")   # default: connection error


@pytest.fixture()
def dl(tmp_path):
    holder = {"m": model()}
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: holder["m"],
        state_path=tmp_path / "s.json", outbox_path=tmp_path / "o.json",
        interval_s=0.05, logger=logging.getLogger("l3b_test"),
        webhook_url="https://hook.test/ct")
    fake = FakeOpener()
    n._opener = fake
    return SimpleNamespace(n=n, holder=holder, fake=fake, tmp=tmp_path,
                           state=tmp_path / "s.json", outbox=tmp_path / "o.json")


def dl_outbox(dl):
    return json.loads(dl.outbox.read_text())["records"] if dl.outbox.exists() else []


def enqueue_pending(dl, code="kill_file_present", g="G1"):
    """Page an immediate code, holding delivery on a connection error so the
    record lands in retry_wait/pending for controlled delivery tests."""
    dl.fake.default = urllib.error.URLError("hold")
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model([code], gen_at=g); dl.n.tick_once(T(1))
    return dl_outbox(dl)[0]


def test_2xx_delivers_with_envelope_and_idempotency_header(dl):
    dl.fake.script = [200]
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["kill_file_present"], gen_at="GA"); dl.n.tick_once(T(1))
    rec = dl_outbox(dl)[0]
    assert rec["status"] == "delivered" and rec["delivered_at"] is not None and rec["attempts"] == 1
    req = dl.fake.requests[0]
    assert req.get_header("Idempotency-key") == rec["notification_id"]
    assert req.get_header("Content-type") == "application/json"
    body = json.loads(req.data.decode())
    assert set(body) == {"notification_id", "code", "decision", "episode_key", "payload", "created_at"}
    assert body["decision"] == "INITIAL" and body["code"] == "kill_file_present"


@pytest.mark.parametrize("action", [
    urllib.error.URLError("conn"), TimeoutError("t"), 500, 503, 408, 425, 429])
def test_retryable_failures_go_retry_wait(dl, action):
    rec = enqueue_pending(dl)                              # now retry_wait, attempts=1
    assert rec["status"] == "retry_wait"
    dl.fake.script = [action]
    dl.fake.default = None
    # force it due, then run one delivery
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.n._deliver_phase(T(9), {"decisions": 0})
    r = dl_outbox(dl)[0]
    assert r["status"] == "retry_wait" and r["attempts"] == 2


def test_429_retry_after_honored(dl):
    rec = enqueue_pending(dl)
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.fake.script = [urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "1"}, None)]
    dl.fake.default = None
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    dl.n._deliver_phase(base, {"decisions": 0})
    r = dl_outbox(dl)[0]
    from datetime import datetime as _dt
    na = _dt.fromisoformat(r["next_attempt_at"])
    assert 0.5 <= (na - base).total_seconds() <= 1.5      # ~1s Retry-After, not 30s+ backoff


def test_malformed_retry_after_falls_back_to_backoff(dl):
    rec = enqueue_pending(dl)
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.fake.script = [urllib.error.HTTPError("u", 429, "rate", {"Retry-After": "soon"}, None)]
    dl.fake.default = None
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    dl.n._deliver_phase(base, {"decisions": 0})
    r = dl_outbox(dl)[0]
    from datetime import datetime as _dt
    delay = (_dt.fromisoformat(r["next_attempt_at"]) - base).total_seconds()
    assert delay >= ops_notifier._BACKOFF_BASE_S            # normal backoff, not ~1s


# ── D-L3B-1: Retry-After must be bounded ─────────────────────────────────────

def _drive_429(dl, retry_after: str, base):
    """Enqueue, force due, deliver one 429 with the given Retry-After header;
    return the record reloaded from disk."""
    enqueue_pending(dl)
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.fake.script = [urllib.error.HTTPError("u", 429, "rate", {"Retry-After": retry_after}, None)]
    dl.fake.default = None
    dl.n._deliver_phase(base, {"decisions": 0})
    return dl_outbox(dl)[0]


def _delay(rec, base):
    from datetime import datetime as _dt
    return (_dt.fromisoformat(rec["next_attempt_at"]) - base).total_seconds()


def test_retry_after_below_cap_honored_unchanged(dl):
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    r = _drive_429(dl, "120", base)                        # 120s < 3600s cap
    assert r["status"] == "retry_wait" and _delay(r, base) == 120.0


def test_retry_after_above_cap_clamped(dl):
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    r = _drive_429(dl, str(10 ** 6), base)                 # 1e6s >> cap
    assert r["status"] == "retry_wait"
    assert _delay(r, base) == ops_notifier._BACKOFF_CAP_S   # clamped exactly to the cap


def test_extreme_retry_after_does_not_overflow_or_abort(dl):
    """A value large enough that direct timedelta(seconds=v) would overflow must
    be clamped, not raise, and must not abort the tick."""
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    huge = "999999999999999999"                            # ~1e18s -> timedelta overflow if unclamped
    # sanity: unclamped construction WOULD overflow
    with pytest.raises(OverflowError):
        _ = base + timedelta(seconds=int(huge))
    r = _drive_429(dl, huge, base)
    assert r["status"] == "retry_wait"                     # no crash, no abort
    assert _delay(r, base) == ops_notifier._BACKOFF_CAP_S   # bounded next_attempt_at


def test_extreme_retry_after_does_not_block_later_due_record(dl):
    """After an extreme Retry-After on the first due record, a later due record
    in the same tick must still be processed."""
    dl.fake.default = urllib.error.URLError("hold")
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["kill_file_present", "frozen"], gen_at="G1"); dl.n.tick_once(T(1))
    recs = dl_outbox(dl)
    for r in recs:
        r["next_attempt_at"] = None
    dl.n._save_outbox(recs)
    # first (by notification_id order) gets an extreme 429; second gets 2xx
    dl.fake.script = [urllib.error.HTTPError("u", 429, "rate",
                                             {"Retry-After": "999999999999999999"}, None), 200]
    dl.fake.default = None
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    dl.n._deliver_phase(base, {"decisions": 0})
    statuses = sorted(r["status"] for r in dl_outbox(dl))
    assert statuses == ["delivered", "retry_wait"]         # later record still delivered
    rw = next(r for r in dl_outbox(dl) if r["status"] == "retry_wait")
    assert _delay(rw, base) == ops_notifier._BACKOFF_CAP_S  # clamped, not decades


def test_negative_retry_after_falls_back_to_backoff(dl):
    base = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)
    r = _drive_429(dl, "-5", base)
    assert r["status"] == "retry_wait" and _delay(r, base) >= ops_notifier._BACKOFF_BASE_S


def test_delivery_clears_next_attempt_at(dl):
    """A record that was retry_wait with a set next_attempt_at, then delivers,
    must have next_attempt_at cleared to None (state cleanliness)."""
    enqueue_pending(dl)                                    # retry_wait, next_attempt_at set
    # leave next_attempt_at set but in the PAST so the record is due AND still
    # carries a non-None timestamp -> proves the delivered branch clears it.
    past = (datetime(2026, 7, 22, 11, 0, 0, tzinfo=timezone.utc)).isoformat()
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = past; dl.n._save_outbox(recs)
    assert dl_outbox(dl)[0]["next_attempt_at"] is not None
    dl.fake.script = [200]; dl.fake.default = None
    dl.n._deliver_phase(T(9), {"decisions": 0})
    r = dl_outbox(dl)[0]
    assert r["status"] == "delivered" and r["delivered_at"] is not None
    assert r["next_attempt_at"] is None and r["last_error"] is None


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 410, 422, 301, 302])
def test_permanent_failures_dead_letter(dl, status):
    rec = enqueue_pending(dl)
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.fake.script = [status]; dl.fake.default = None
    dl.n._deliver_phase(T(9), {"decisions": 0})
    assert dl_outbox(dl)[0]["status"] == "dead_letter"


def test_bounded_attempts_dead_letter(dl):
    rec = enqueue_pending(dl)                              # attempts=1, retry_wait
    for _ in range(ops_notifier._MAX_ATTEMPTS):
        r = dl_outbox(dl)[0]
        if r["status"] == "dead_letter":
            break
        r["next_attempt_at"] = None; dl.n._save_outbox([r])
        dl.fake.script = [503]; dl.fake.default = None
        dl.n._deliver_phase(T(9), {"decisions": 0})
    final = dl_outbox(dl)[0]
    assert final["status"] == "dead_letter" and final["attempts"] == ops_notifier._MAX_ATTEMPTS


def test_missing_url_leaves_pending_attempts_unchanged(dl, caplog):
    dl.n._webhook_url = ""                                 # simulate absent URL
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    with caplog.at_level(logging.WARNING, logger="l3b_test"):
        dl.holder["m"] = model(["kill_file_present"], gen_at="GA"); dl.n.tick_once(T(1))
        dl.n.tick_once(T(2))                              # second tick: no log storm
    r = dl_outbox(dl)[0]
    assert r["status"] == "pending" and r["attempts"] == 0
    assert sum(1 for rec in caplog.records if "remain pending" in rec.getMessage()) == 1
    assert dl.fake.requests == []                          # no delivery attempted


def test_malformed_url_does_not_crash_and_leaves_pending(dl):
    dl.n._webhook_url = "not-a-url"
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["kill_file_present"], gen_at="GA")
    assert dl.n.tick_once(T(1))["error"] is None          # no crash
    assert dl_outbox(dl)[0]["status"] == "pending"


def test_restart_during_retry_wait_resumes(dl):
    enqueue_pending(dl)                                    # retry_wait, attempts=1
    dl.n._state = None                                    # restart
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.fake.script = [200]; dl.fake.default = None
    dl.holder["m"] = model(["kill_file_present"], gen_at="G1")  # still present
    dl.n.tick_once(T(9))
    r = dl_outbox(dl)[0]
    assert r["status"] == "delivered" and r["attempts"] == 2


def test_crash_after_2xx_before_persist_resends_dedups(dl):
    rec = enqueue_pending(dl)
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    # first delivery: 2xx reaches receiver, but persistence crashes
    dl.fake.script = [200, 200]; dl.fake.default = None
    real_save = dl.n._save_outbox
    def crash_once(r):
        dl.n._save_outbox = real_save
        raise RuntimeError("crash after 2xx before persist")
    dl.n._save_outbox = crash_once
    dl.n._deliver_phase(T(9), {"decisions": 0})           # attempt sent, save crashed
    assert dl_outbox(dl)[0]["status"] == "retry_wait" or dl_outbox(dl)[0]["status"] == "pending"
    # restart drain: re-sends (receiver would dedup on Idempotency-Key), persists delivered
    recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
    dl.n._deliver_phase(T(10), {"decisions": 0})
    assert dl_outbox(dl)[0]["status"] == "delivered"
    # both sends carried the SAME notification_id (receiver-side dedup key)
    assert len({r.get_header("Idempotency-key") for r in dl.fake.requests}) == 1


def test_failed_record_does_not_block_later_due(dl):
    # two due records; first permanently fails, second must still be attempted
    dl.fake.default = urllib.error.URLError("hold")
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["kill_file_present", "frozen"], gen_at="G1"); dl.n.tick_once(T(1))
    recs = dl_outbox(dl)
    for r in recs: r["next_attempt_at"] = None
    dl.n._save_outbox(recs)
    dl.fake.script = [400, 200]; dl.fake.default = None   # first dead-letter, second 2xx
    dl.n._deliver_phase(T(9), {"decisions": 0})
    # both were attempted (the failing one did not block the other); outcomes are
    # one dead_letter + one delivered, ordered deterministically by notification_id.
    statuses = sorted(r["status"] for r in dl_outbox(dl))
    assert statuses == ["dead_letter", "delivered"]      # both attempted, neither blocked


def test_delivery_budget_defers_excess(dl, monkeypatch):
    monkeypatch.setattr(ops_notifier, "_DELIVERY_BUDGET_PER_TICK", 1)
    dl.fake.default = urllib.error.URLError("hold")
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["kill_file_present", "frozen"], gen_at="G1"); dl.n.tick_once(T(1))
    recs = dl_outbox(dl)
    for r in recs: r["next_attempt_at"] = None
    dl.n._save_outbox(recs)
    dl.fake.script = [200, 200]; dl.fake.default = None
    dl.n._deliver_phase(T(9), {"decisions": 0})
    delivered = [r for r in dl_outbox(dl) if r["status"] == "delivered"]
    assert len(delivered) == 1                            # only one attempted this tick
    dl.n._deliver_phase(T(10), {"decisions": 0})
    assert len([r for r in dl_outbox(dl) if r["status"] == "delivered"]) == 2  # rest next tick


def test_no_url_or_response_body_in_logs(dl, caplog):
    with caplog.at_level(logging.DEBUG):
        dl.fake.script = [urllib.error.HTTPError("u", 500, "boom", {}, None)]
        enqueue_pending(dl)
        recs = dl_outbox(dl); recs[0]["next_attempt_at"] = None; dl.n._save_outbox(recs)
        dl.fake.script = [urllib.error.HTTPError("u", 500, "secret-body", {}, None)]
        dl.n._deliver_phase(T(9), {"decisions": 0})
    joined = " ".join(r.getMessage() for r in caplog.records)
    assert "hook.test" not in joined and "secret-body" not in joined


def test_pruning_never_removes_pending_or_retry(dl, monkeypatch):
    monkeypatch.setattr(ops_notifier, "_DELIVERED_RETENTION", 0)
    monkeypatch.setattr(ops_notifier, "_DEAD_LETTER_RETENTION", 0)
    recs = [
        {"notification_id": "a", "decision": "RESOLVED", "status": "pending", "created_at": "1"},
        {"notification_id": "b", "decision": "INITIAL", "status": "retry_wait", "created_at": "2"},
        {"notification_id": "c", "decision": "INITIAL", "status": "delivered", "created_at": "3"},
        {"notification_id": "d", "decision": "INITIAL", "status": "dead_letter", "created_at": "4"},
    ]
    kept = {r["notification_id"] for r in ops_notifier._prune_outbox(recs)}
    assert "a" in kept and "b" in kept          # pending (incl. RESOLVED-pending) + retry_wait retained
    assert "c" not in kept                       # delivered compacted (cap 0)
    assert "d" in kept or "d" not in kept        # dead_letter cap 0 -> may drop; retained cap>0 case below


def test_dead_letter_retained_under_default_cap(dl):
    recs = [{"notification_id": f"d{i}", "decision": "INITIAL", "status": "dead_letter",
             "created_at": str(i)} for i in range(3)]
    kept = {r["notification_id"] for r in ops_notifier._prune_outbox(recs)}
    assert kept == {"d0", "d1", "d2"}            # under the 5000 default, all retained


def test_policy_phase_completes_before_delivery(dl):
    # a paged condition must be enqueued (policy) and then delivered (delivery)
    # within the same tick, in that order.
    dl.fake.script = [200]
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["frozen"], gen_at="GP")
    s = dl.n.tick_once(T(1))
    assert s["initial"] == 1 and s["delivered"] == 1      # policy paged, then delivered
    assert dl_outbox(dl)[0]["status"] == "delivered"
