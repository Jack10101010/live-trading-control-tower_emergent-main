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


# ── L3-A boundary: nothing delivered ─────────────────────────────────────────

def test_records_are_pending_never_delivered(nf):
    nf.n.tick_once(T(0))
    nf.holder["m"] = model(["frozen"], gen_at="GP"); nf.n.tick_once(T(1))
    r = outbox(nf)[0]
    assert r["status"] == "pending" and r["delivered_at"] is None and r["attempts"] == 0
