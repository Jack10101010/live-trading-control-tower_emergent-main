"""B3 — durable confirmed-close accounting: idempotency, transaction, flag.

WHAT THIS ACTIVATES
    B1 read confirmed closes; B2 proved they resolve to canonical R. B3 makes
    that durable — the first milestone in which a broker close actually changes
    the state the daily-loss rail reads.

THE TWO PROPERTIES THAT MATTER
    1. EXACTLY ONCE. A confirmed loss must never be counted twice (overlapping
       history windows, a replayed cycle, a restart) and must never be lost.
       Deal id is the single idempotency identity; there is no second mechanism.

    2. ONE EVENT, ONE DURABLE REPLACEMENT. The R bucket and the accounted deal id
       must land together or not at all. Persisting them separately would either
       double-count (bucket saved, id not — the deal is re-read and posted again)
       or permanently lose a confirmed loss (id saved, bucket not — the deal is
       skipped forever). Both are unacceptable.

DEFAULT OFF
    Enabling accounting is the switch that activates a previously dormant safety
    rail, so it is a deliberate operator action rather than a deploy side effect.
    With the flag off, nothing changes at all.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import close_accounting as ca                          # noqa: E402
from live import deal_records as dr                              # noqa: E402
from live.deal_records import DealReadOutcome, DealReadResult    # noqa: E402
from live.intents import OPEN_POSITION                           # noqa: E402
from live.state import RunnerState                               # noqa: E402

BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
TODAY, YESTERDAY = "2026-07-28", "2026-07-27"
POSITION = "5001"


def deal(deal_id="9001", profit=-5.0, when=BASE, entry=dr.ENTRY_OUT,
         position_id=POSITION):
    return dr.DealRecord(
        deal_id=deal_id, position_id=position_id, order_id="4001", entry=entry,
        deal_type=dr.DEAL_TYPE_SELL, volume=1000.0, price=1.1050, profit=profit,
        commission=-0.07, fee=0.0, swap=-0.02, execution_time_utc=when,
        symbol="EURUSD.r", magic=77001, comment="c")


def seed_ledger(state, *, stop=1.0950, entry=1.1000, volume=1000.0):
    """A ledger record shaped exactly as the runner writes it post-Milestone A."""
    state.data["ledger"]["i1"] = {
        "status": "confirmed", "at": "2026-07-28T12:00:00Z",
        "detail": {"order": int(POSITION), "price": entry, "filled_volume": volume,
                   "trade_id": "t1",
                   "intent": {"intent_id": "i1", "action": OPEN_POSITION,
                              "trade_id": "t1", "side": "long",
                              "frontier_bar": "b", "entry": entry, "stop": stop,
                              "target": 1.11}}}


class FakeGateway:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def closed_deals(self, since, until):
        self.calls.append((since, until))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def ok(deals):
    return DealReadResult(outcome=DealReadOutcome.OK, deals=tuple(deals))


def executor(tmp_path, gateway, *, enabled=True, lookback=48.0):
    """An Executor with only the collaborators accounting touches."""
    from live.executor import Executor
    ex = Executor.__new__(Executor)
    ex.state = RunnerState(tmp_path)
    ex.gateway = gateway
    ex.config = SimpleNamespace(close_accounting_enabled=enabled,
                                close_accounting_lookback_hours=lookback)
    seed_ledger(ex.state)
    return ex


# ── feature flag ─────────────────────────────────────────────────────────────

def test_disabled_by_default_in_real_config():
    """Existing production behaviour must be byte-identical until switched on.

    Asserted against the dataclass FIELD DEFAULT rather than by manipulating the
    environment: `LiveConfig`'s env-derived defaults are evaluated once at class
    definition time, so setting an env var after import would not change them.
    That is correct for a trading process — configuration is fixed at start — but
    it means an env-based test here would prove nothing.
    """
    import dataclasses

    from live.config import LiveConfig
    field = {f.name: f for f in dataclasses.fields(LiveConfig)}["close_accounting_enabled"]
    assert field.default is False
    assert LiveConfig().close_accounting_enabled is False


def test_the_flag_off_touches_no_state_and_reads_no_history(tmp_path):
    gw = FakeGateway(ok([deal()]))
    ex = executor(tmp_path, gw, enabled=False)
    result = ex.account_closed_deals(BASE)
    assert result == {"enabled": False, "committed": 0, "mode": "off"}
    assert gw.calls == []                                   # not even a read
    assert ex.state.realized_r_by_date() == {}
    assert ex.state.accounted_deal_ids() == {}


def test_the_flag_on_accounts_a_confirmed_close(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    result = ex.account_closed_deals(BASE)
    assert result["enabled"] is True and result["committed"] == 1
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)
    assert ex.state.is_accounted("9001")


def test_the_configured_lookback_bounds_the_window(tmp_path):
    gw = FakeGateway(ok([]))
    executor(tmp_path, gw, lookback=6.0).account_closed_deals(BASE)
    since, until = gw.calls[0]
    assert until == BASE and since == BASE - timedelta(hours=6)


# ── idempotency ──────────────────────────────────────────────────────────────

def test_the_same_deal_in_two_overlapping_windows_posts_once(tmp_path):
    """The core exactly-once property. Windows overlap generously by design."""
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    assert ex.account_closed_deals(BASE)["committed"] == 1
    assert ex.account_closed_deals(BASE)["committed"] == 0     # skipped
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_a_duplicate_within_one_batch_posts_once(tmp_path):
    """B2 deliberately computed a repeated deal twice; B3 deduplicates it. This
    is the test that makes that change visible rather than silent."""
    ex = executor(tmp_path, FakeGateway(ok([deal("9001", -5.0), deal("9001", -5.0)])))
    assert ex.account_closed_deals(BASE)["committed"] == 1
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_multiple_unique_deals_all_post(tmp_path):
    deals = [deal("1", -1.0), deal("2", -2.0), deal("3", 3.0)]
    ex = executor(tmp_path, FakeGateway(ok(deals)))
    assert ex.account_closed_deals(BASE)["committed"] == 3
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(0.0)
    assert set(ex.state.accounted_deal_ids()) == {"1", "2", "3"}


def test_deal_id_is_the_only_idempotency_mechanism(tmp_path):
    """Two deals identical in every respect EXCEPT id must both post — otherwise
    something other than the deal id is deduplicating, and a genuine repeated
    fill at the same price and instant would be silently swallowed."""
    ex = executor(tmp_path, FakeGateway(ok([deal("1", -5.0), deal("2", -5.0)])))
    assert ex.account_closed_deals(BASE)["committed"] == 2
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-2.0)


# ── the transaction ──────────────────────────────────────────────────────────

def test_bucket_and_accounted_id_always_appear_together(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.account_closed_deals(BASE)
    reloaded = RunnerState(tmp_path)
    assert reloaded.daily_realized_r(TODAY) == pytest.approx(-1.0)
    assert reloaded.is_accounted("9001")


def test_a_save_failure_commits_neither_half(tmp_path, monkeypatch):
    """The transaction's whole purpose. Neither the bucket nor the id may survive
    a failed save — in memory OR on disk."""
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    result = ex.account_closed_deals(BASE)

    assert result["committed"] == 0 and "commit failed" in result["error"]
    assert ex.state.daily_realized_r(TODAY) == 0.0          # in-memory rolled back
    assert not ex.state.is_accounted("9001")
    assert RunnerState(tmp_path).daily_realized_r(TODAY) == 0.0   # nothing on disk


def test_a_rolled_back_transaction_cannot_leak_through_a_later_unrelated_save(tmp_path,
                                                                              monkeypatch):
    """`self.data` is shared with every other writer in the process. If a failed
    commit left its mutation in memory, the next ordinary save would persist the
    accounting outside any transaction — and a retry would then double-count."""
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    real_save = ex.state.save
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    ex.account_closed_deals(BASE)

    monkeypatch.setattr(ex.state, "save", real_save)
    ex.state.save()                                          # an ordinary cycle save
    assert RunnerState(tmp_path).daily_realized_r(TODAY) == 0.0
    assert RunnerState(tmp_path).accounted_deal_ids() == {}


def test_a_failed_commit_is_retried_successfully_next_cycle(tmp_path, monkeypatch):
    """Nothing is lost: the deal stays inside the bounded window."""
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    real_save = ex.state.save
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    assert ex.account_closed_deals(BASE)["committed"] == 0

    monkeypatch.setattr(ex.state, "save", real_save)
    assert ex.account_closed_deals(BASE)["committed"] == 1
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_a_partial_batch_failure_commits_nothing_from_that_batch(tmp_path,
                                                                 monkeypatch):
    ex = executor(tmp_path, FakeGateway(ok([deal("1", -1.0), deal("2", -2.0)])))
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    ex.account_closed_deals(BASE)
    assert ex.state.accounted_deal_ids() == {}
    assert ex.state.daily_realized_r(TODAY) == 0.0


def test_one_accounting_event_is_one_durable_replacement(tmp_path, monkeypatch):
    """Not one save per deal: the accounting TRANSACTION is a single state
    replacement, asserted on the transaction itself.

    The executor may save again afterwards to persist telemetry. That is a
    separate, clearly-labelled diagnostics write and is deliberately outside the
    transaction — counting total cycle saves would conflate the two.
    """
    saves = {"n": 0}
    ex = executor(tmp_path, FakeGateway(ok([])))
    real_save = ex.state.save

    def counting():
        saves["n"] += 1
        real_save()
    monkeypatch.setattr(ex.state, "save", counting)
    seed_ledger(ex.state)
    assert ex.state.commit_accounting([
        ("1", TODAY, -1.0, BASE.isoformat(), POSITION),
        ("2", TODAY, -2.0, BASE.isoformat(), POSITION),
        ("3", TODAY, -1.0, BASE.isoformat(), POSITION)]) == 3
    assert saves["n"] == 1


def test_the_transaction_saves_nothing_when_there_is_nothing_to_commit(tmp_path,
                                                                        monkeypatch):
    saves = {"n": 0}
    ex = executor(tmp_path, FakeGateway(ok([])))
    monkeypatch.setattr(ex.state, "save", lambda: saves.__setitem__("n", saves["n"] + 1))
    assert ex.state.commit_accounting([]) == 0
    assert saves["n"] == 0


# ── restart ──────────────────────────────────────────────────────────────────

def test_a_restart_after_a_successful_save_does_not_repost(tmp_path):
    executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)]))).account_closed_deals(BASE)
    fresh = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    assert fresh.account_closed_deals(BASE)["committed"] == 0
    assert fresh.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_a_restart_before_a_successful_save_reposts_exactly_once(tmp_path,
                                                                 monkeypatch):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("crash")))
    ex.account_closed_deals(BASE)

    fresh = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    assert fresh.account_closed_deals(BASE)["committed"] == 1
    assert fresh.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


# ── failure behaviour: the cycle always continues ────────────────────────────

def test_an_unavailable_read_skips_accounting_without_posting(tmp_path):
    """A failed read is not a quiet market. Nothing is posted and the cycle
    continues; the deal stays in the window and is retried."""
    ex = executor(tmp_path, FakeGateway(
        DealReadResult(outcome=DealReadOutcome.UNAVAILABLE, detail="not connected")))
    result = ex.account_closed_deals(BASE)
    assert result["committed"] == 0 and result["read"] == "unavailable"
    assert ex.state.realized_r_by_date() == {}


def test_a_gateway_exception_never_escapes_into_the_cycle(tmp_path):
    """Bookkeeping about events that already happened must never stop the
    executor from managing live positions."""
    ex = executor(tmp_path, FakeGateway(RuntimeError("terminal died")))
    result = ex.account_closed_deals(BASE)
    assert result["committed"] == 0 and "RuntimeError" in result["error"]


def test_a_malformed_batch_still_accounts_its_valid_deals(tmp_path):
    read = DealReadResult(outcome=DealReadOutcome.MALFORMED, deals=(deal("1", -5.0),),
                          rejected=(dr.RejectedDeal(reason=dr.REJECT_BAD_PROFIT,
                                                    raw_ticket="2"),))
    ex = executor(tmp_path, FakeGateway(read))
    result = ex.account_closed_deals(BASE)
    assert result["committed"] == 1 and result["rejected"] == 1
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


@pytest.mark.parametrize("case,setup", [
    ("unresolved", lambda ex: ex.state.data["ledger"].clear()),
    ("unaccountable", lambda ex: ex.state.data["ledger"]["i1"]["detail"]["intent"]
        .__setitem__("stop", None)),
])
def test_non_accountable_deals_are_not_posted_but_are_surfaced(tmp_path, case, setup):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    setup(ex)
    result = ex.account_closed_deals(BASE)
    assert result["committed"] == 0
    assert ex.state.realized_r_by_date() == {}
    assert not ex.state.is_accounted("9001")
    assert result["counts"]                              # surfaced to the caller


def test_ignored_and_quarantined_deals_are_never_posted(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([
        deal("1", -5.0, entry=dr.ENTRY_IN),
        deal("2", -5.0, entry=dr.ENTRY_INOUT),
        deal("3", -5.0, entry=dr.ENTRY_OUT_BY)])))
    assert ex.account_closed_deals(BASE)["committed"] == 0
    assert ex.state.realized_r_by_date() == {}


# ── day allocation and parity with B2 ────────────────────────────────────────

def test_a_previous_day_close_posts_to_its_own_day(tmp_path):
    """The delayed-close case MS-A's per-date buckets exist for."""
    ex = executor(tmp_path, FakeGateway(ok([
        deal("1", -2.0, when=BASE),
        deal("2", -1.0, when=datetime(2026, 7, 27, 23, 58, tzinfo=timezone.utc))])))
    assert ex.account_closed_deals(BASE)["committed"] == 2
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-0.4)
    assert ex.state.daily_realized_r(YESTERDAY) == pytest.approx(-0.2)


@pytest.mark.parametrize("profits", [[-5.0], [-2.0, -3.0], [1.0, -6.0],
                                     [-1.0, -1.0, -1.0, -1.0, -1.0]])
def test_persisted_totals_match_the_b2_dry_run_exactly(tmp_path, profits):
    """Persistence must not change the arithmetic B2 proved."""
    deals = [deal(str(i), p) for i, p in enumerate(profits)]
    ex = executor(tmp_path, FakeGateway(ok(deals)))
    ex.account_closed_deals(BASE)

    dry = ca.account_deals(deals, ex.state.data["ledger"])
    expected = sum(r.realised_r for r in dry if r.accountable)
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(expected, abs=1e-8)


def test_a_short_position_persists_the_same_r_as_the_dry_run(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    seed_ledger(ex.state, entry=1.1000, stop=1.1050)      # stop above entry
    ex.account_closed_deals(BASE)
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_a_partial_close_sequence_accumulates_to_the_whole_trade_r(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal("1", -2.0)])))
    ex.account_closed_deals(BASE)
    ex.gateway = FakeGateway(ok([deal("1", -2.0), deal("2", -3.0)]))   # window overlaps
    ex.account_closed_deals(BASE)
    assert ex.state.daily_realized_r(TODAY) == pytest.approx(-1.0)
    assert set(ex.state.accounted_deal_ids()) == {"1", "2"}


# ── the rail actually reacts ─────────────────────────────────────────────────

def test_accounted_losses_block_the_next_open_through_the_real_rail(tmp_path,
                                                                    monkeypatch):
    """End to end: a confirmed broker close now moves the rail that was dormant
    for two milestones."""
    from live.config import LiveConfig
    from live.intents import OrderIntent
    from live.safety import SafetyRails

    # Constructed explicitly: env-derived defaults bind at import time, so an
    # env var set here would be ignored and the test would silently assert
    # against the 5.0 default instead of the 1.0 it means to.
    config = LiveConfig(state_dir=tmp_path, kill_file=tmp_path / "KILL",
                        daily_loss_limit_r=1.0)
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    intent = OrderIntent(intent_id="x", action=OPEN_POSITION, trade_id="t9",
                         side="long", frontier_bar="b", entry=1.1, stop=1.095)

    rails = SafetyRails(config, ex.state)
    assert rails.evaluate(intent, "EURUSD", TODAY).allowed        # before

    ex.account_closed_deals(BASE)                                 # -1.0R posted
    verdict = rails.evaluate(intent, "EURUSD", TODAY)
    assert not verdict.allowed and verdict.rail == "daily_loss_limit"


# ── scope guards ─────────────────────────────────────────────────────────────

def _code(path: Path) -> str:
    import ast
    text = path.read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_b3_added_no_pruning_and_no_telemetry():
    source = _code(REPO_ROOT / "live" / "close_accounting.py")
    assert "prune" not in source
    for name in ("_prune", "publish", "telemetry"):
        assert name not in _code(REPO_ROOT / "live" / "close_accounting.py")


def test_only_the_transaction_persists_accounting_state():
    """No helper may independently persist accounting state — one event, one
    durable replacement."""
    source = _code(REPO_ROOT / "live" / "executor.py")
    assert "commit_accounting" in source
    accounting = source[source.index("def account_closed_deals"):
                        source.index("def apply")]
    assert "add_realized_r" not in accounting, "accounting posts outside the transaction"
    # The only save in the entry point is the telemetry write, which is
    # diagnostics and explicitly not accounting state.
    assert accounting.count("self.state.save()") <= 1
    assert "telemetry is diagnostics" in accounting


def test_the_r_formula_is_still_defined_exactly_once():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "gross_pnl / risk_amount", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout.splitlines()
    real = [h for h in hits if "/tests/" not in h and "test_" not in h]
    assert [h for h in real if "live/risk_math.py" not in h] == []
