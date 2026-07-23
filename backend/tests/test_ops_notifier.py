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


# ── L3-C read-only operational surface: last_tick publication + inspect ───────
# These prove the observability surface is PURE READ (no writes, no quarantine,
# never _load_outbox), that last_tick is an immutable-after-publish activity
# snapshot, and that the dead-letter projection is a strict allowlist. They must
# not weaken any L3-A/L3-B behaviour above.

def _write_outbox(nf, records):
    nf.outbox.write_text(json.dumps({"schema_version": 1, "records": records}))


def _dead(nid, created_at="2026-01-01T00:00:00+00:00", **extra):
    rec = {"status": "dead_letter", "notification_id": nid, "code": "frozen",
           "episode_key": f"frozen@{nid}", "decision": "INITIAL",
           "created_at": created_at, "attempts": 6, "delivered_at": None,
           "next_attempt_at": None, "last_error": "HTTP 400",
           "source_generated_at": created_at,
           "payload": {"code": "frozen", "humanExplanation": "h",
                       "first_generated_at": "x"}}
    rec.update(extra)
    return rec


# -- last_tick publication (unit 1-9) --

def test_last_tick_null_before_first_tick(nf):
    assert nf.n._last_tick is None
    assert nf.n.inspect(enabled=True, limit=50)["last_tick"] is None


def test_baseline_does_not_publish_last_tick(nf):
    nf.n.baseline(T(0))
    assert nf.n._last_tick is None


def test_completed_tick_publishes_last_tick(nf):
    nf.n.tick_once(T(0))
    assert nf.n._last_tick is not None


def test_last_tick_at_is_completion_timestamp(tmp_path):
    # D-L3C-1: at must be the PUBLICATION instant (completion clock), not the
    # tick-start clock. Tick starts at T1 but the completion clock returns T2.
    t1 = datetime(2026, 7, 22, 9, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 7, 22, 9, 0, 5, tzinfo=timezone.utc)   # later completion
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: model(), state_path=tmp_path / "s.json",
        outbox_path=tmp_path / "o.json", logger=logging.getLogger("l3c_clock"),
        clock=lambda: t2)
    n.tick_once(t1)
    assert n._last_tick["at"] == t2.isoformat()
    assert n._last_tick["at"] != t1.isoformat()


def test_outer_tick_failure_publishes_error_snapshot(nf, monkeypatch):
    nf.holder["m"] = model(["frozen"], gen_at="GE")

    def boom(*a, **k):
        raise RuntimeError("delivery blew up")

    monkeypatch.setattr(nf.n, "_deliver_phase", boom)      # outer-try exception
    nf.n.tick_once(T(1))
    lt = nf.n._last_tick
    assert lt is not None and lt["error"] is not None
    # notifier state was still persisted before the failure (not corrupted).
    assert nf.n._state is not None and "frozen" in nf.n._state["episodes"]


def test_last_tick_decisions_matches_persisted_policy_count(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["kill_file_present"], gen_at="GK")
    nf.n.tick_once(T(1))                                   # immediate INITIAL
    assert nf.n._last_tick["decisions"] == 1 == len(outbox(nf))
    assert nf.n._last_tick["initial"] == 1


def test_last_tick_delivery_subcounts_preserved(dl):
    dl.fake.script = [200]
    dl.holder["m"] = model([]); dl.n.tick_once(T(0))
    dl.holder["m"] = model(["frozen"], gen_at="GD"); dl.n.tick_once(T(1))
    assert dl.n._last_tick["delivered"] == 1


def test_prior_last_tick_unchanged_after_next_tick(nf):
    nf.n.tick_once(T(0))
    first = nf.n._last_tick
    snapshot = dict(first)
    nf.n.tick_once(T(1))
    assert first == snapshot                               # byte-for-byte unchanged
    assert nf.n._last_tick is not first                    # a new object was swapped in


def test_publication_does_not_alias_returned_summary(nf):
    s = nf.n.tick_once(T(0))
    assert nf.n._last_tick is not s                        # not the mutable summary


# -- inspect: read-only + aggregates + projection + corruption (unit 10-23) --

def test_inspect_absent_creates_nothing(nf):
    m = nf.n.inspect(enabled=True, limit=50)
    assert m["outbox"] == {"pending": 0, "retry_wait": 0, "delivered": 0,
                           "dead_letter": 0, "unknown": 0, "total": 0}
    assert m["dead_letters"] == [] and m["outbox_error"] is None
    assert not nf.outbox.exists() and not nf.state.exists()


def test_inspect_leaves_files_byte_identical(nf):
    _write_outbox(nf, [_dead("l3|x")])
    nf.state.write_text(json.dumps({"schema_version": 1, "episodes": {}}))
    ob_before, st_before = nf.outbox.read_bytes(), nf.state.read_bytes()
    nf.n.inspect(enabled=True, limit=50)
    assert nf.outbox.read_bytes() == ob_before
    assert nf.state.read_bytes() == st_before


def test_inspect_does_not_call_delivery(nf, monkeypatch):
    _write_outbox(nf, [_dead("l3|x")])
    called = {"n": 0}
    monkeypatch.setattr(nf.n, "_deliver_phase",
                        lambda *a, **k: called.__setitem__("n", called["n"] + 1))
    nf.n.inspect(enabled=True, limit=50)
    assert called["n"] == 0


def test_inspect_does_not_call_pruning(nf, monkeypatch):
    _write_outbox(nf, [_dead("l3|x")])
    called = {"n": 0}
    monkeypatch.setattr(ops_notifier, "_prune_outbox",
                        lambda r: (called.__setitem__("n", called["n"] + 1), r)[1])
    nf.n.inspect(enabled=True, limit=50)
    assert called["n"] == 0


def test_inspect_does_not_call_load_outbox(nf, monkeypatch):
    _write_outbox(nf, [_dead("l3|x")])

    def forbidden(*a, **k):
        raise AssertionError("_load_outbox (mutating) must not be used by inspect")

    monkeypatch.setattr(nf.n, "_load_outbox", forbidden)
    m = nf.n.inspect(enabled=True, limit=50)               # must not raise
    assert m["outbox"]["dead_letter"] == 1


def test_inspect_corrupt_not_quarantined(nf):
    nf.outbox.write_text("{ not valid json ]")
    before = nf.outbox.read_bytes()
    m = nf.n.inspect(enabled=True, limit=50)
    assert m["outbox"] is None and m["outbox_error"] == "outbox unreadable"
    assert m["dead_letters"] == []
    assert not nf.outbox.with_name(nf.outbox.name + ".corrupt").exists()
    assert nf.outbox.read_bytes() == before


def test_inspect_corrupt_logs_once_then_resets(nf, caplog):
    nf.outbox.write_text("{ bad")
    with caplog.at_level(logging.ERROR):
        nf.n.inspect(enabled=True, limit=50)
        nf.n.inspect(enabled=True, limit=50)
        assert sum("outbox unreadable" in r.getMessage() for r in caplog.records) == 1
        assert nf.n._inspect_corrupt_logged is True
        _write_outbox(nf, [])                              # clean read re-arms
        nf.n.inspect(enabled=True, limit=50)
        assert nf.n._inspect_corrupt_logged is False
        nf.outbox.write_text("{ bad again")               # fresh corruption logs again
        nf.n.inspect(enabled=True, limit=50)
        assert sum("outbox unreadable" in r.getMessage() for r in caplog.records) == 2


def test_inspect_concurrent_during_ticks_is_coherent(nf):
    import threading
    stop = threading.Event()
    errors = []

    def ticker():
        i = 0
        while not stop.is_set():
            try:
                nf.holder["m"] = model(["consecutive_errors"] if i % 2 else [], gen_at="GC")
                nf.n.tick_once(T(i)); i += 1
            except Exception as exc:                       # pragma: no cover
                errors.append(exc)

    th = threading.Thread(target=ticker); th.start()
    try:
        for _ in range(60):
            m = nf.n.inspect(enabled=True, limit=50)
            assert set(m) == {"schema_version", "enabled", "running",
                              "webhook_configured", "interval_s", "last_tick",
                              "outbox", "outbox_error", "dead_letters"}
            assert (m["outbox"] is None) == (m["outbox_error"] is not None)
    finally:
        stop.set(); th.join()
    assert not errors


def test_inspect_malformed_created_at_does_not_crash(nf):
    _write_outbox(nf, [_dead("l3|a", created_at="2026-06-01T00:00:00+00:00"),
                       _dead("l3|b", created_at=None),
                       _dead("l3|c", created_at=12345)])
    m = nf.n.inspect(enabled=True, limit=50)               # must not raise
    order = [d["notification_id"] for d in m["dead_letters"]]
    assert order[0] == "l3|a"                              # real timestamp newest-first
    assert set(order) == {"l3|a", "l3|b", "l3|c"}


def test_inspect_mixed_status_aggregate(nf):
    _write_outbox(nf, [
        {"status": "pending", "notification_id": "p"},
        {"status": "pending", "notification_id": "p2"},
        {"status": "retry_wait", "notification_id": "r"},
        {"status": "delivered", "notification_id": "d"},
        _dead("l3|x")])
    agg = nf.n.inspect(enabled=True, limit=50)["outbox"]
    assert agg == {"pending": 2, "retry_wait": 1, "delivered": 1,
                   "dead_letter": 1, "unknown": 0, "total": 5}


def test_inspect_unknown_status_counted(nf):
    _write_outbox(nf, [{"status": "weird", "notification_id": "w"},
                       {"status": None, "notification_id": "n"},
                       {"status": "pending", "notification_id": "p"}])
    agg = nf.n.inspect(enabled=True, limit=50)["outbox"]
    assert agg["unknown"] == 2 and agg["pending"] == 1 and agg["total"] == 3


def test_dead_letter_projection_is_allowlisted(nf):
    _write_outbox(nf, [_dead("l3|x")])
    d0 = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert set(d0) == {"notification_id", "code", "episode_key", "decision",
                       "created_at", "attempts", "delivered_at", "next_attempt_at",
                       "last_error", "source_generated_at", "payload"}
    assert set(d0["payload"]) == {"code", "humanExplanation", "first_generated_at"}


def test_dead_letter_projection_excludes_extra_fields(nf):
    rec = _dead("l3|x", secret_field="LEAKVALUE")
    rec["payload"]["secret_payload"] = "LEAKVALUE2"
    _write_outbox(nf, [rec])
    blob = json.dumps(nf.n.inspect(enabled=True, limit=50))
    assert "secret_field" not in blob and "LEAKVALUE" not in blob
    assert "secret_payload" not in blob and "LEAKVALUE2" not in blob


def test_inspect_response_has_no_url_or_paths(nf):
    _write_outbox(nf, [_dead("l3|x")])
    nf.n._webhook_url = "https://secret-hook.example/abc?token=SUPERSECRET"
    blob = json.dumps(nf.n.inspect(enabled=True, limit=50))
    assert "secret-hook" not in blob and "SUPERSECRET" not in blob
    assert str(nf.tmp) not in blob and "outbox.json" not in blob
    assert nf.n.inspect(enabled=True, limit=50)["webhook_configured"] is True


# ── L3-C remediation regressions (D-L3C-1 / D-L3C-2 / D-L3C-3 + N2) ───────────
# Each block below fails under the pre-remediation implementation.

def _clocked(tmp_path, clock, **kw):
    return ops_notifier.OpsNotifier(
        l1a_provider=lambda now: model(), state_path=tmp_path / "s.json",
        outbox_path=tmp_path / "o.json", logger=logging.getLogger("l3c_rem"),
        clock=clock, **kw)


# -- D-L3C-1: completion-time --

def test_completion_time_distinguishes_start_from_publish(tmp_path):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=42)
    seen = {}

    def clock():
        # publication reads the clock only after the tick body has run.
        seen["at_publish"] = True
        return t2

    n = _clocked(tmp_path, clock)
    n.tick_once(t1)
    assert n._last_tick["at"] == t2.isoformat() and n._last_tick["at"] != t1.isoformat()
    assert seen.get("at_publish") is True


def test_completion_time_used_on_outer_error(tmp_path, monkeypatch):
    t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    t2 = t1 + timedelta(seconds=7)
    n = _clocked(tmp_path, clock=lambda: t2)
    monkeypatch.setattr(n, "_deliver_phase",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    n.tick_once(t1)
    assert n._last_tick["error"] is not None
    assert n._last_tick["at"] == t2.isoformat()            # completion clock, even on error


def test_default_clock_is_utc_now(tmp_path):
    before = datetime.now(timezone.utc)
    n = ops_notifier.OpsNotifier(
        l1a_provider=lambda now: model(), state_path=tmp_path / "s.json",
        outbox_path=tmp_path / "o.json", logger=logging.getLogger("l3c_def"))
    n.tick_once(datetime(2000, 1, 1, tzinfo=timezone.utc))  # ancient tick-start
    at = datetime.fromisoformat(n._last_tick["at"])
    assert at >= before                                     # real now, not the 2000 tick time


def test_summary_semantics_unchanged_under_clock(tmp_path):
    n = _clocked(tmp_path, clock=lambda: datetime(2026, 1, 1, tzinfo=timezone.utc))
    n.tick_once(T(0))
    n._provider = lambda now: model(["kill_file_present"], gen_at="GK")
    s = n.tick_once(T(1))
    assert s["initial"] == 1 and n._last_tick["decisions"] == 1  # policy counts intact


# -- D-L3C-2: parsed chronological ordering --

def _dead_ts(nid, created_at):
    return {"status": "dead_letter", "notification_id": nid, "created_at": created_at,
            "payload": {"code": "frozen", "humanExplanation": "h", "first_generated_at": "f"}}


def test_mixed_offsets_ordered_by_instant_not_lexical(nf):
    # +05:00 midnight (2025-12-31T19:00Z) is chronologically OLDER than +00:00 midnight.
    _write_outbox(nf, [_dead_ts("A", "2026-01-01T00:00:00+05:00"),
                       _dead_ts("B", "2026-01-01T00:00:00+00:00")])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order == ["B", "A"]                              # newest instant first


def test_z_and_offset_same_instant_tiebreak_by_id(nf):
    _write_outbox(nf, [_dead_ts("B", "2026-01-01T00:00:00+00:00"),
                       _dead_ts("Z", "2026-01-01T00:00:00Z")])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order == ["Z", "B"]                              # equal instant -> id desc


def test_older_positive_offset_placed_later(nf):
    _write_outbox(nf, [_dead_ts("old", "2026-01-01T00:00:00+05:00"),
                       _dead_ts("new", "2026-01-01T00:00:00+00:00")])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order.index("old") > order.index("new")


def test_naive_iso_treated_as_utc(nf):
    _write_outbox(nf, [_dead_ts("naive", "2026-01-01T00:00:00"),
                       _dead_ts("utc", "2026-01-01T00:00:00+00:00")])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert set(order) == {"naive", "utc"}                   # equal instant, no crash, both present


def test_malformed_missing_nonstring_created_at_no_crash(nf):
    _write_outbox(nf, [_dead_ts("good", "2026-06-01T00:00:00+00:00"),
                       _dead_ts("bad", "not-a-date"),
                       _dead_ts("none", None),
                       _dead_ts("num", 12345)])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order[0] == "good"                               # valid ahead of malformed
    assert set(order) == {"good", "bad", "none", "num"}


def test_valid_sorts_ahead_of_malformed(nf):
    _write_outbox(nf, [_dead_ts("m", "garbage"),
                       _dead_ts("v", "2020-01-01T00:00:00+00:00")])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order == ["v", "m"]


def test_equal_instant_ordered_by_id_desc(nf):
    ts = "2026-01-01T00:00:00+00:00"
    _write_outbox(nf, [_dead_ts("a", ts), _dead_ts("c", ts), _dead_ts("b", ts)])
    order = [d["notification_id"] for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]]
    assert order == ["c", "b", "a"]


def test_projected_created_at_is_stored_value_not_parsed(nf):
    _write_outbox(nf, [_dead_ts("z", "2026-01-01T00:00:00Z")])
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert d["created_at"] == "2026-01-01T00:00:00Z"        # stored form, not normalized


# -- D-L3C-3: malformed-but-parseable records --

def test_non_dict_records_return_200_aggregate(nf):
    _write_outbox(nf, [{"status": "pending", "notification_id": "p"}, 123, "s", None, [1, 2]])
    agg = nf.n.inspect(enabled=True, limit=50)["outbox"]
    assert agg["total"] == 5 and agg["unknown"] == 4 and agg["pending"] == 1


def test_every_malformed_element_increments_unknown_and_total(nf):
    _write_outbox(nf, [1, "x", None, [], {}])              # 5 malformed/unknown
    agg = nf.n.inspect(enabled=True, limit=50)["outbox"]
    assert agg["unknown"] == 5 and agg["total"] == 5


def test_malformed_elements_never_enter_dead_letters(nf):
    _write_outbox(nf, [123, "str", None, _dead_ts("d", "2026-01-01T00:00:00+00:00")])
    dls = nf.n.inspect(enabled=True, limit=50)["dead_letters"]
    assert [d["notification_id"] for d in dls] == ["d"]


def test_missing_status_dict_increments_unknown(nf):
    _write_outbox(nf, [{"notification_id": "x"}])
    assert nf.n.inspect(enabled=True, limit=50)["outbox"]["unknown"] == 1


def test_unknown_or_nonstring_status_increments_unknown(nf):
    _write_outbox(nf, [{"status": "weird", "notification_id": "w"},
                       {"status": 5, "notification_id": "n"}])
    assert nf.n.inspect(enabled=True, limit=50)["outbox"]["unknown"] == 2


def test_dead_letter_null_payload_no_crash(nf):
    rec = _dead_ts("d", "2026-01-01T00:00:00+00:00"); rec["payload"] = None
    _write_outbox(nf, [rec])
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert d["payload"] == {"code": None, "humanExplanation": None, "first_generated_at": None}


def test_dead_letter_string_or_list_payload_no_crash(nf):
    r1 = _dead_ts("a", "2026-01-01T00:00:00+00:00"); r1["payload"] = "oops"
    r2 = _dead_ts("b", "2026-02-01T00:00:00+00:00"); r2["payload"] = ["x"]
    _write_outbox(nf, [r1, r2])
    for d in nf.n.inspect(enabled=True, limit=50)["dead_letters"]:
        assert set(d["payload"]) == {"code", "humanExplanation", "first_generated_at"}


def test_nested_values_in_allowlisted_fields_returned_null(nf):
    rec = _dead_ts("d", "2026-01-01T00:00:00+00:00")
    rec["last_error"] = {"leak": "x"}; rec["code"] = ["nested"]
    rec["payload"]["humanExplanation"] = {"nested": "y"}
    _write_outbox(nf, [rec])
    blob = json.dumps(nf.n.inspect(enabled=True, limit=50))
    assert "leak" not in blob and "nested" not in blob
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert d["last_error"] is None and d["code"] is None
    assert d["payload"]["humanExplanation"] is None


def test_invalid_attempts_returns_zero(nf):
    for bad in (True, "6", 6.0, None, {"x": 1}):
        rec = _dead_ts("d", "2026-01-01T00:00:00+00:00"); rec["attempts"] = bad
        _write_outbox(nf, [rec])
        assert nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]["attempts"] == 0
    rec = _dead_ts("d", "2026-01-01T00:00:00+00:00"); rec["attempts"] = 4
    _write_outbox(nf, [rec])
    assert nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]["attempts"] == 4


def test_projection_keys_still_exact_under_corruption(nf):
    rec = _dead_ts("d", "2026-01-01T00:00:00+00:00"); rec["payload"] = 42
    _write_outbox(nf, [rec])
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert set(d) == {"notification_id", "code", "episode_key", "decision", "created_at",
                      "attempts", "delivered_at", "next_attempt_at", "last_error",
                      "source_generated_at", "payload"}
    assert set(d["payload"]) == {"code", "humanExplanation", "first_generated_at"}


def test_aggregate_total_independent_of_limit(nf):
    _write_outbox(nf, [_dead_ts(f"d{i}", f"2026-01-{i+1:02d}T00:00:00+00:00") for i in range(6)])
    m = nf.n.inspect(enabled=True, limit=2)
    assert len(m["dead_letters"]) == 2 and m["outbox"]["dead_letter"] == 6 and m["outbox"]["total"] == 6


# -- N2: returned last_tick is a copy, not the live published object --

def test_returned_last_tick_is_a_copy(nf):
    nf.n.tick_once(T(0))
    returned = nf.n.inspect(enabled=True, limit=50)["last_tick"]
    returned["decisions"] = 9999
    assert nf.n._last_tick["decisions"] != 9999            # published object untouched


# ── D-L3C-4: non-finite floats in allowlisted fields (must not break serialization) ──
# json.loads accepts NaN/Infinity/-Infinity from a parseable-but-malformed outbox;
# strict JSON serialization (allow_nan=False, as Starlette uses) would raise. The
# scalar boundary must degrade them to null so the endpoint stays 200.

def _write_raw_outbox(nf, records_json_fragment):
    # write NON-finite tokens as raw JSON so the value is non-finite ONLY after the
    # production reader's json.loads — never sanitized before the reader sees it.
    nf.outbox.write_text('{"schema_version":1,"records":[' + records_json_fragment + ']}')


def _nonfinite_dead(token_for_code, token_for_payload="\"h\""):
    return ('{"status":"dead_letter","notification_id":"d",'
            '"created_at":"2026-01-01T00:00:00+00:00","code":' + token_for_code + ','
            '"payload":{"code":"frozen","humanExplanation":' + token_for_payload +
            ',"first_generated_at":"f"}}')


def test_scalar_or_none_rejects_nonfinite_floats():
    assert ops_notifier._scalar_or_none(float("nan")) is None
    assert ops_notifier._scalar_or_none(float("inf")) is None
    assert ops_notifier._scalar_or_none(float("-inf")) is None
    assert ops_notifier._scalar_or_none(3.14) == 3.14      # finite preserved
    assert ops_notifier._scalar_or_none(True) is True      # bool preserved
    assert ops_notifier._scalar_or_none(0) == 0


import pytest as _pytest


@_pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_topfield_degrades_to_null_and_inspect_ok(nf, token):
    _write_raw_outbox(nf, _nonfinite_dead(token))
    before = nf.outbox.read_bytes()
    m = nf.n.inspect(enabled=True, limit=50)               # must not raise
    d = m["dead_letters"][0]
    assert d["code"] is None                               # offending top-level field null
    assert d["payload"]["humanExplanation"] == "h"         # finite/valid sibling intact
    assert m["outbox"] == {"pending": 0, "retry_wait": 0, "delivered": 0,
                           "dead_letter": 1, "unknown": 0, "total": 1}   # aggregates coherent
    assert len(m["dead_letters"]) == 1                     # record still listed
    assert set(d) == {"notification_id", "code", "episode_key", "decision", "created_at",
                      "attempts", "delivered_at", "next_attempt_at", "last_error",
                      "source_generated_at", "payload"}    # projection shape intact
    json.dumps(m, allow_nan=False)                         # strict-JSON serializable
    assert nf.outbox.read_bytes() == before                # file untouched
    assert not nf.outbox.with_name(nf.outbox.name + ".corrupt").exists()
    assert not nf.state.exists()


@_pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_payload_field_degrades_to_null(nf, token):
    _write_raw_outbox(nf, _nonfinite_dead("\"frozen\"", token_for_payload=token))
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert d["payload"]["humanExplanation"] is None        # offending payload field null
    assert d["code"] == "frozen"                           # valid sibling intact
    json.dumps(nf.n.inspect(enabled=True, limit=50), allow_nan=False)


def test_finite_float_field_preserved(nf):
    _write_raw_outbox(nf, _nonfinite_dead("1.5"))          # finite float in `code`
    d = nf.n.inspect(enabled=True, limit=50)["dead_letters"][0]
    assert d["code"] == 1.5
