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
               ticket=POSITION, at=OLD):
    state.data["ledger"][intent_id] = {
        "status": status, "at": at.isoformat(),
        "detail": {"order": int(ticket), "filled_volume": 1000.0,
                   "trade_id": "t1",
                   "intent": {"intent_id": intent_id, "action": "OPEN_POSITION",
                              "entry": 1.1, "stop": 1.095}}}


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


def test_a_terminal_entry_is_retained_when_only_another_position_was_accounted(state):
    """The durable commit must belong to THIS position."""
    add_ledger(state)
    add_accounted(state, position="99999")
    assert prune(state)["ledger_removed"] == 0


def test_a_terminal_entry_is_retained_inside_the_retention_window(state):
    """Condition 5: a late close deal must have ample time to arrive and be
    accounted before its planned-risk lineage is discarded."""
    add_ledger(state, at=RECENT)
    add_accounted(state)
    assert prune(state)["ledger_removed"] == 0
    assert "i1" in state.data["ledger"]


def test_an_open_position_is_never_pruned(state):
    """Condition 2. Even fully accounted and old, an open position keeps its
    lineage — a further close is still possible."""
    add_ledger(state)
    add_accounted(state)
    state.data["mirror"]["t1"] = int(POSITION)
    assert prune(state, open_tickets={POSITION})["ledger_removed"] == 0


def test_a_partially_closed_position_is_retained(state):
    """An observed-but-unaccounted close for this position means more accounting
    is still pending, whatever its age."""
    add_ledger(state)
    add_accounted(state)
    assert prune(state, pending_positions={POSITION})["ledger_removed"] == 0


@pytest.mark.parametrize("status", [LEDGER_PENDING, LEDGER_SENT, LEDGER_PARTIAL])
def test_non_terminal_entries_are_never_pruned(state, status):
    """Condition 1. `pending`/`sent` are in flight; `partial` may still be open."""
    add_ledger(state, status=status)
    add_accounted(state)
    assert prune(state)["ledger_removed"] == 0


def test_an_entry_with_an_unreadable_timestamp_is_retained(state):
    """An unknown age is not an expired one."""
    add_ledger(state)
    state.data["ledger"]["i1"]["at"] = "not-a-timestamp"
    add_accounted(state)
    assert prune(state)["ledger_removed"] == 0


def test_an_entry_with_no_ticket_lineage_is_retained(state):
    add_ledger(state)
    state.data["ledger"]["i1"]["detail"] = {"trade_id": "t1"}
    add_accounted(state)
    assert prune(state)["ledger_removed"] == 0


# ── ledger: what may be REMOVED ──────────────────────────────────────────────

def test_a_fully_accounted_closed_old_entry_is_pruned(state):
    """All five conditions satisfied."""
    add_ledger(state)
    add_accounted(state)
    assert prune(state)["ledger_removed"] == 1
    assert state.data["ledger"] == {}


def test_pruning_removes_only_the_eligible_entry(state):
    add_ledger(state, "old", ticket="5001", at=OLD)
    add_ledger(state, "young", ticket="5002", at=RECENT)
    add_accounted(state, "d1", position="5001")
    add_accounted(state, "d2", position="5002")
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
    add_ledger(state)
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
    add_ledger(state)
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
    add_ledger(state)
    add_accounted(state, "drop", at=NOW - timedelta(hours=LOOKBACK + 5))
    add_accounted(state, "keep", at=RECENT)
    prune(state)

    reloaded = RunnerState(tmp_path)
    assert reloaded.data["ledger"] == {}
    assert reloaded.is_accounted("keep") and not reloaded.is_accounted("drop")


def test_a_restart_before_pruning_simply_prunes_next_cycle(tmp_path):
    """Pruning is derived: nothing is lost by never having run it."""
    state = RunnerState(tmp_path)
    add_ledger(state)
    add_accounted(state)
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
    add_ledger(state)
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
