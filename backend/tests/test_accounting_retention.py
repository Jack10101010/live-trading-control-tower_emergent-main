"""B4 — bounded retention for ledger entries and accounted deal ids.

HOUSEKEEPING ONLY
    Correctness never depends on pruning. It exists to remove state that can no
    longer participate in future accounting, and where there is any doubt the
    data is retained. Every test here is therefore written the same way round:
    it asks what pruning must NOT remove at least as often as what it may.

THE RETENTION PROOF (not a heuristic)
    The history reader never requests deals older than LOOKBACK. So a deal
    executed before `now - LOOKBACK` can never be returned again; it can never
    be re-accounted; and its idempotency record is therefore no longer required.
    That is the whole justification, and it is why the stored value had to become
    the deal's EXECUTION instant rather than the day it was posted.

ORDERING IS THE SAFETY PROPERTY
    Accounting → commit → pruning eligibility. Never accounting → pruning →
    commit. The accounted map read during pruning IS the durable record of a
    committed transaction, so eligibility is only ever judged on evidence that
    has already survived a save.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.state import (LEDGER_CONFIRMED, LEDGER_PARTIAL,        # noqa: E402
                        LEDGER_PENDING, LEDGER_SENT, RunnerState)

NOW = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
LOOKBACK = 48.0
OLD = NOW - timedelta(hours=2 * LOOKBACK + 1)      # beyond 2x lookback
RECENT = NOW - timedelta(hours=1)
POSITION = "5001"


@pytest.fixture()
def state(tmp_path):
    return RunnerState(tmp_path)


def add_ledger(state, intent_id="i1", *, status=LEDGER_CONFIRMED,
               ticket=POSITION, at=OLD, accounted_at=None):
    """A ledger entry. `accounted_at` is the B4.5 DURABLE MARKER — the proof that
    a close of this entry was accounted in a committed transaction. It lives on
    the entry itself precisely so it outlives the accounted-id map."""
    detail = {"order": int(ticket), "filled_volume": 1000.0, "price": 1.1,
              "trade_id": "t1",
              "intent": {"intent_id": intent_id, "action": "OPEN_POSITION",
                         "entry": 1.1, "stop": 1.095}}
    if accounted_at is not None:
        detail["accounted_at"] = accounted_at.isoformat()
    state.data["ledger"][intent_id] = {
        "status": status, "at": at.isoformat(), "detail": detail}


def add_accounted(state, deal_id="9001", *, at=RECENT, position=POSITION):
    state.data.setdefault("accounted_deal_ids", {})[deal_id] = {
        "at": at.isoformat(), "position": position}


def prune(state, **kw):
    return state.prune(now=NOW, lookback_hours=LOOKBACK, **kw)


# ── ledger: what must be RETAINED ────────────────────────────────────────────

def test_a_terminal_entry_is_retained_before_its_close_is_accounted(state):
    """Condition 3+4: no accounted close for this position means the accounting
    transaction has not committed, so its lineage is still needed."""
    add_ledger(state)
    assert prune(state)["ledger_removed"] == 0
    assert "i1" in state.data["ledger"]


def test_a_terminal_entry_is_retained_when_another_position_was_accounted(state):
    """The durable proof must belong to THIS entry. A marker on some other
    position's entry says nothing about this one."""
    add_ledger(state, "mine", ticket="5001", at=OLD)
    add_ledger(state, "other", ticket="99999", at=OLD, accounted_at=RECENT)
    assert prune(state)["ledger_removed"] == 1
    assert "mine" in state.data["ledger"]


def test_a_terminal_entry_is_retained_inside_the_retention_window(state):
    """Condition 5: a late close deal must have ample time to arrive and be
    accounted before its planned-risk lineage is discarded."""
    add_ledger(state, at=RECENT, accounted_at=RECENT)
    assert prune(state)["ledger_removed"] == 0
    assert "i1" in state.data["ledger"]


def test_an_open_position_is_never_pruned(state):
    """Condition 2. Even fully accounted and old, an open position keeps its
    lineage — a further close is still possible."""
    add_ledger(state, accounted_at=RECENT)
    state.data["mirror"]["t1"] = int(POSITION)
    assert prune(state, open_tickets={POSITION})["ledger_removed"] == 0


def test_a_partially_closed_position_is_retained(state):
    """An observed-but-unaccounted close for this position means more accounting
    is still pending, whatever its age."""
    add_ledger(state, accounted_at=RECENT)
    assert prune(state, pending_positions={POSITION})["ledger_removed"] == 0


@pytest.mark.parametrize("status", [LEDGER_PENDING, LEDGER_SENT, LEDGER_PARTIAL])
def test_non_terminal_entries_are_never_pruned(state, status):
    """Condition 1. `pending`/`sent` are in flight; `partial` may still be open."""
    add_ledger(state, status=status, accounted_at=RECENT)
    assert prune(state)["ledger_removed"] == 0


def test_an_entry_with_an_unreadable_timestamp_is_retained(state):
    """An unknown age is not an expired one."""
    add_ledger(state, accounted_at=RECENT)
    state.data["ledger"]["i1"]["at"] = "not-a-timestamp"
    assert prune(state)["ledger_removed"] == 0


def test_an_entry_with_no_ticket_lineage_is_retained(state):
    add_ledger(state, accounted_at=RECENT)
    state.data["ledger"]["i1"]["detail"] = {"trade_id": "t1"}
    assert prune(state)["ledger_removed"] == 0


# ── ledger: what may be REMOVED ──────────────────────────────────────────────

def test_a_fully_accounted_closed_old_entry_is_pruned(state):
    """All five conditions satisfied, condition 3 proven by the durable marker.

    Note the accounted-ID map is deliberately NOT seeded: after B4.5 the two
    retention proofs are independent, and this entry must prune even though its
    idempotency record has already expired — which is exactly the case the
    original implementation got wrong.
    """
    add_ledger(state, accounted_at=RECENT)
    assert prune(state)["ledger_removed"] == 1
    assert state.data["ledger"] == {}


def test_pruning_removes_only_the_eligible_entry(state):
    add_ledger(state, "old", ticket="5001", at=OLD, accounted_at=RECENT)
    add_ledger(state, "young", ticket="5002", at=RECENT, accounted_at=RECENT)
    assert prune(state)["ledger_removed"] == 1
    assert set(state.data["ledger"]) == {"young"}


# ── accounted-id retention: the proof ────────────────────────────────────────

def test_an_accounted_deal_inside_the_lookback_is_retained(state):
    """It could still be returned by the next read, so its idempotency record is
    still required — removing it would allow a double count."""
    add_accounted(state, at=NOW - timedelta(hours=LOOKBACK - 1))
    assert prune(state)["accounted_removed"] == 0
    assert state.is_accounted("9001")


def test_an_accounted_deal_outside_the_lookback_is_pruned(state):
    """The reader never asks for deals this old, so it can never be returned,
    never re-accounted, and its record is no longer needed."""
    add_accounted(state, at=NOW - timedelta(hours=LOOKBACK + 1))
    assert prune(state)["accounted_removed"] == 1
    assert not state.is_accounted("9001")


def test_a_deal_exactly_at_the_boundary_is_retained(state):
    """`<` not `<=`: the boundary case keeps the record."""
    add_accounted(state, at=NOW - timedelta(hours=LOOKBACK))
    assert prune(state)["accounted_removed"] == 0


def test_a_mixed_map_prunes_only_the_expired_ids(state):
    add_accounted(state, "fresh", at=RECENT)
    add_accounted(state, "stale", at=NOW - timedelta(hours=LOOKBACK + 5))
    assert prune(state)["accounted_removed"] == 1
    assert state.is_accounted("fresh") and not state.is_accounted("stale")


def test_a_record_with_a_missing_or_unreadable_timestamp_is_retained(state):
    """When in doubt, retain: an unknown age must never justify removal."""
    state.data["accounted_deal_ids"] = {"a": {"at": None, "position": POSITION},
                                        "b": {"at": "garbage", "position": POSITION},
                                        "c": "not-even-a-dict"}
    assert prune(state)["accounted_removed"] == 0
    assert set(state.accounted_deal_ids()) == {"a", "b", "c"}


def test_a_legacy_b3_string_value_migrates_conservatively(state):
    """B3 stored the posted DAY as a bare string. It is read as the LAST instant
    of that day, so a migrated record is retained longer rather than pruned
    early."""
    day = (NOW - timedelta(hours=LOOKBACK + 2)).strftime("%Y-%m-%d")
    state.data["accounted_deal_ids"] = {"legacy": day}
    # 23:59:59 of that day is still inside the window, so it survives.
    assert prune(state)["accounted_removed"] == 0
    assert state.is_accounted("legacy")


def test_a_sufficiently_old_legacy_value_is_eventually_pruned(state):
    state.data["accounted_deal_ids"] = {"ancient": "2026-07-01"}
    assert prune(state)["accounted_removed"] == 1


# ── idempotency and ordering invariants ──────────────────────────────────────

def test_repeated_pruning_produces_identical_state(state):
    add_ledger(state, accounted_at=RECENT)
    add_accounted(state, "keep", at=RECENT)
    add_accounted(state, "drop", at=NOW - timedelta(hours=LOOKBACK + 5),
                  position=POSITION)
    first = prune(state)
    snapshot = (dict(state.data["ledger"]), dict(state.accounted_deal_ids()),
                dict(state.realized_r_by_date()))
    for _ in range(3):
        again = prune(state)
        assert again == {"accounted_removed": 0, "ledger_removed": 0}
    assert (dict(state.data["ledger"]), dict(state.accounted_deal_ids()),
            dict(state.realized_r_by_date())) == snapshot
    assert first["ledger_removed"] == 1


def test_pruning_never_changes_realised_r_totals(state):
    state.add_realized_r(-2.5, "2026-07-28")
    state.add_realized_r(-1.0, "2026-07-27")
    add_ledger(state, accounted_at=RECENT)
    add_accounted(state, at=NOW - timedelta(hours=LOOKBACK + 5))
    before = dict(state.realized_r_by_date())
    prune(state)
    assert state.realized_r_by_date() == before


def test_pruning_cannot_make_an_expired_deal_re_accountable_within_the_window(state):
    """The safety of forgetting: a pruned id is only ever one the reader can no
    longer return, so idempotency is unweakened for anything still readable."""
    add_accounted(state, "expired", at=NOW - timedelta(hours=LOOKBACK + 1))
    add_accounted(state, "live", at=RECENT)
    prune(state)
    assert not state.is_accounted("expired")     # unreachable by any future read
    assert state.is_accounted("live")            # still protected


def test_pruning_is_safe_on_empty_state(state):
    assert prune(state) == {"accounted_removed": 0, "ledger_removed": 0}
    assert state.data["ledger"] == {} and state.accounted_deal_ids() == {}


def test_pruning_with_no_accounting_this_cycle_is_still_safe(state):
    """Pruning must remain correct when no accounting occurred."""
    add_ledger(state, at=RECENT)
    assert prune(state)["ledger_removed"] == 0


def test_nothing_is_saved_when_nothing_was_pruned(state, monkeypatch):
    saves = {"n": 0}
    monkeypatch.setattr(state, "save", lambda: saves.__setitem__("n", saves["n"] + 1))
    add_ledger(state, at=RECENT)
    prune(state)
    assert saves["n"] == 0


# ── persistence and restart ──────────────────────────────────────────────────

def test_pruned_state_survives_a_restart(tmp_path):
    state = RunnerState(tmp_path)
    add_ledger(state, accounted_at=RECENT)
    add_accounted(state, "drop", at=NOW - timedelta(hours=LOOKBACK + 5))
    add_accounted(state, "keep", at=RECENT)
    prune(state)

    reloaded = RunnerState(tmp_path)
    assert reloaded.data["ledger"] == {}
    assert reloaded.is_accounted("keep") and not reloaded.is_accounted("drop")


def test_a_restart_before_pruning_simply_prunes_next_cycle(tmp_path):
    """Pruning is derived: nothing is lost by never having run it."""
    state = RunnerState(tmp_path)
    add_ledger(state, accounted_at=RECENT)
    state.save()                                  # crash before pruning

    reloaded = RunnerState(tmp_path)
    assert "i1" in reloaded.data["ledger"]        # still present
    assert reloaded.prune(now=NOW, lookback_hours=LOOKBACK)["ledger_removed"] == 1


def test_a_pruning_failure_leaves_accounting_and_idempotency_intact(state,
                                                                    monkeypatch):
    """Housekeeping may fail; trading must not care."""
    state.add_realized_r(-2.0, "2026-07-28")
    add_accounted(state, "keep", at=RECENT)
    monkeypatch.setattr(state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    add_ledger(state, accounted_at=RECENT)
    add_accounted(state, "drop", at=NOW - timedelta(hours=LOOKBACK + 5))
    with pytest.raises(OSError):
        prune(state)
    assert state.daily_realized_r("2026-07-28") == -2.0
    assert state.is_accounted("keep")


def test_the_executor_reports_a_pruning_failure_without_raising(tmp_path,
                                                               monkeypatch):
    """A pruning failure must never reach the cycle."""
    from test_durable_close_accounting import FakeGateway, deal, executor, ok
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    monkeypatch.setattr(ex.state, "prune",
                        lambda **kw: (_ for _ in ()).throw(RuntimeError("boom")))
    result = ex.account_closed_deals(NOW)
    assert result["committed"] == 1                       # accounting succeeded
    assert result["pruned"] == {"error": "RuntimeError"}
    assert ex.state.daily_realized_r("2026-07-28") == pytest.approx(-1.0)


# ── ordering invariant ───────────────────────────────────────────────────────

def test_pruning_runs_after_the_commit_never_before():
    """The ordering that makes eligibility safe: the accounted map consulted by
    pruning is the durable record of a committed transaction. Pruning first could
    discard a ledger entry whose accounting was then lost."""
    import ast
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    body = source[source.index("def account_closed_deals"):source.index("def apply")]
    assert body.index("commit_accounting") < body.index("self.state.prune"), \
        "pruning must not precede the accounting commit"
    ast.parse(source)


def test_the_executor_supplies_open_positions_so_live_trades_are_never_pruned():
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    body = source[source.index("def account_closed_deals"):source.index("def apply")]
    assert "open_tickets=" in body and "pending_positions=" in body


def test_b4_changed_no_accounting_mathematics():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "gross_pnl / risk_amount", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout.splitlines()
    real = [h for h in hits if "/tests/" not in h and "test_" not in h]
    assert [h for h in real if "live/risk_math.py" not in h] == []


# ═════════════════════════════════════════════════════════════════════════════
# B4.5 — TIMELINE TESTS
#
# WHY THESE EXIST, AND WHY THE ORIGINAL TESTS MISSED THE DEFECT
#     The B4 suite tested each of the five pruning conditions in ISOLATION. To
#     exercise condition 3 it seeded a RECENT accounted deal beside an OLD ledger
#     entry — a pairing that cannot occur in production, because a closed
#     position's deal cannot be recent when its ledger entry is four days old.
#     Every condition passed and the system was still unbounded.
#
#     Condition coverage is not timeline coverage. These tests advance ONE clock
#     through the real sequence — open, close, account, LOOKBACK, 2 x LOOKBACK —
#     and assert what the state looks like at each step.
# ═════════════════════════════════════════════════════════════════════════════

OPEN_AT = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def timeline_state(tmp_path, *, ticket="5001", intent_id="i1"):
    state = RunnerState(tmp_path)
    state.data["ledger"][intent_id] = {
        "status": LEDGER_CONFIRMED, "at": OPEN_AT.isoformat(),
        "detail": {"order": int(ticket), "filled_volume": 1000.0, "price": 1.1,
                   "trade_id": "t1",
                   "intent": {"intent_id": intent_id, "action": "OPEN_POSITION",
                              "entry": 1.1, "stop": 1.095}}}
    return state


def at(days=0.0, hours=0.0):
    return OPEN_AT + timedelta(days=days, hours=hours)


def test_intraday_trade_full_timeline_becomes_bounded(tmp_path):
    """THE defect case, on one clock. A trade closing 2h after opening.

    Before B4.5 the accounted id expired at close+LOOKBACK (~day 2.1), after
    which condition 3 was permanently unsatisfiable and the ledger entry
    survived forever. Now the durable marker outlives the map.
    """
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -1.0, close.isoformat(), "5001")])

    # Day 1 — inside both windows: everything retained.
    assert state.prune(now=at(1), lookback_hours=LOOKBACK)["ledger_removed"] == 0
    assert state.is_accounted("d1")
    assert "i1" in state.data["ledger"]

    # Day 3 — past LOOKBACK: the idempotency record expires INDEPENDENTLY.
    state.prune(now=at(3), lookback_hours=LOOKBACK)
    assert not state.is_accounted("d1")
    assert "i1" in state.data["ledger"], "lineage must outlive the idempotency record"

    # Day 5 — past 2 x LOOKBACK: the entry finally prunes, on the marker alone.
    assert state.prune(now=at(5), lookback_hours=LOOKBACK)["ledger_removed"] == 1
    assert state.data["ledger"] == {}

    # Bounded: nothing accumulates thereafter.
    assert state.prune(now=at(30), lookback_hours=LOOKBACK) == {
        "accounted_removed": 0, "ledger_removed": 0}


def test_planned_risk_survives_until_accounting_completes(tmp_path):
    """A multi-day trade: open, still open past 2 x LOOKBACK, then closes.

    The most dangerous case — age alone would have discarded the planned stop
    while its close was still readable and unaccounted.
    """
    state = timeline_state(tmp_path)

    # Day 5: older than 2 x LOOKBACK, position closed, close NOT yet accounted.
    assert state.prune(now=at(5), lookback_hours=LOOKBACK)["ledger_removed"] == 0
    intent = state.data["ledger"]["i1"]["detail"]["intent"]
    assert intent["stop"] == 1.095, "planned risk discarded before accounting"

    # Day 5 + 1h: the close is finally read and accounted.
    close = at(5)
    state.commit_accounting([("d1", "2026-07-06", -1.0, close.isoformat(), "5001")])
    assert state.daily_realized_r("2026-07-06") == pytest.approx(-1.0)

    # Only now does the entry become eligible, once past 2 x LOOKBACK.
    assert state.prune(now=at(5), lookback_hours=LOOKBACK)["ledger_removed"] == 1


def test_a_trade_still_open_is_never_pruned_however_old(tmp_path):
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -0.5, close.isoformat(), "5001")])
    for day in (5, 30, 365):
        result = state.prune(now=at(day), lookback_hours=LOOKBACK,
                             open_tickets={"5001"})
        assert result["ledger_removed"] == 0
    assert "i1" in state.data["ledger"]


def test_delayed_broker_reporting_still_accounts_then_prunes(tmp_path):
    """The close happened on day 1 but was not reported until day 3."""
    state = timeline_state(tmp_path)
    assert state.prune(now=at(2), lookback_hours=LOOKBACK)["ledger_removed"] == 0

    close = at(1)                                   # executed day 1...
    state.commit_accounting([("d1", "2026-07-02", -1.0, close.isoformat(), "5001")])
    assert state.daily_realized_r("2026-07-02") == pytest.approx(-1.0)  # its OWN day

    assert state.prune(now=at(5), lookback_hours=LOOKBACK)["ledger_removed"] == 1


def test_a_trade_whose_accounting_never_completed_retains_its_lineage(tmp_path):
    """No marker is ever stamped, so the entry is retained indefinitely.

    Deliberate: this is unbounded growth ONLY for trades that could never be
    accounted, which is a condition worth surfacing rather than quietly
    discarding. Telemetry (the next milestone) is what makes it visible.
    """
    state = timeline_state(tmp_path)
    for day in (5, 30, 365):
        assert state.prune(now=at(day), lookback_hours=LOOKBACK)["ledger_removed"] == 0
    assert state.data["ledger"]["i1"]["detail"]["intent"]["stop"] == 1.095


def test_repeated_pruning_across_the_whole_timeline_is_idempotent(tmp_path):
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -1.0, close.isoformat(), "5001")])
    for day in (1, 3, 5, 10):
        first = state.prune(now=at(day), lookback_hours=LOOKBACK)
        snapshot = (dict(state.data["ledger"]), dict(state.accounted_deal_ids()))
        for _ in range(3):
            repeat = state.prune(now=at(day), lookback_hours=LOOKBACK)
            assert repeat == {"accounted_removed": 0, "ledger_removed": 0}
        assert (dict(state.data["ledger"]),
                dict(state.accounted_deal_ids())) == snapshot, first


def test_the_two_retention_proofs_are_independent(tmp_path):
    """Idempotency retention is governed SOLELY by LOOKBACK; ledger retention by
    the durable marker plus 2 x LOOKBACK. Neither was extended to repair the
    other — that would have coupled two proofs that must stand alone."""
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -1.0, close.isoformat(), "5001")])
    state.prune(now=at(3), lookback_hours=LOOKBACK)
    assert not state.is_accounted("d1")            # expired on its own schedule
    assert "i1" in state.data["ledger"]            # ledger unaffected by that


def test_realised_r_totals_are_untouched_across_the_timeline(tmp_path):
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -1.0, close.isoformat(), "5001")])
    before = dict(state.realized_r_by_date())
    for day in (1, 3, 5, 30):
        state.prune(now=at(day), lookback_hours=LOOKBACK)
    assert state.realized_r_by_date() == before


def test_the_marker_is_stamped_inside_the_accounting_transaction(tmp_path):
    """One transaction: the marker cannot be durable without the R it attests
    to, nor the R without the marker."""
    state = timeline_state(tmp_path)
    close = at(hours=2)
    state.commit_accounting([("d1", "2026-07-01", -1.0, close.isoformat(), "5001")])
    reloaded = RunnerState(tmp_path)
    assert reloaded.data["ledger"]["i1"]["detail"]["accounted_at"] == close.isoformat()
    assert reloaded.daily_realized_r("2026-07-01") == pytest.approx(-1.0)
    assert reloaded.is_accounted("d1")


def test_a_failed_commit_leaves_no_marker(tmp_path, monkeypatch):
    """Rollback must restore the ledger too, or a marker could outlive a
    transaction that never committed — and prune lineage for unrecorded R."""
    state = timeline_state(tmp_path)
    monkeypatch.setattr(state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        state.commit_accounting([("d1", "2026-07-01", -1.0,
                                  at(hours=2).isoformat(), "5001")])
    assert "accounted_at" not in state.data["ledger"]["i1"]["detail"]


# ── lineage audit ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key", ["intent", "accounted_at", "filled_volume", "price"])
def test_irreplaceable_facts_survive_a_later_lifecycle_transition(tmp_path, key):
    """The lineage test: could a LATER state reproduce this fact? If not, losing
    it is unrecoverable. `filled_volume` is the R denominator and `price` the
    entry-basis fallback — neither is recoverable once the trade closes, so both
    joined `intent` and `accounted_at` as carried lineage."""
    from live.state import LEDGER_SENT
    state = RunnerState(tmp_path)
    state.ledger_set("i1", LEDGER_SENT, {
        "intent": {"action": "OPEN_POSITION", "stop": 1.095},
        "accounted_at": "2026-07-01T14:00:00+00:00",
        "filled_volume": 1000.0, "price": 1.1})
    state.ledger_set("i1", "frozen", {"reason": "reconcile_freeze"})
    assert key in state.data["ledger"]["i1"]["detail"]


def test_an_explicit_value_still_overrides_a_carried_one(tmp_path):
    """Carrying is gap-filling only — a writer supplying a fresh value wins."""
    from live.state import LEDGER_SENT
    state = RunnerState(tmp_path)
    state.ledger_set("i1", LEDGER_SENT, {"filled_volume": 0.005, "price": 1.1})
    state.ledger_set("i1", LEDGER_CONFIRMED, {"filled_volume": 0.01, "price": 1.2})
    detail = state.data["ledger"]["i1"]["detail"]
    assert detail["filled_volume"] == 0.01 and detail["price"] == 1.2


def test_transient_diagnostics_are_still_not_carried(tmp_path):
    """Widening lineage must not accidentally make stale errors permanent."""
    from live.state import LEDGER_SENT
    state = RunnerState(tmp_path)
    state.ledger_set("i1", LEDGER_SENT, {"diagnostic": "timeout", "error": "x",
                                         "filled_volume": 0.01})
    state.ledger_set("i1", LEDGER_CONFIRMED, {"order": 1})
    detail = state.data["ledger"]["i1"]["detail"]
    assert "diagnostic" not in detail and "error" not in detail
    assert detail["filled_volume"] == 0.01
