"""B2 — close-deal accounting, dry run.

WHAT THIS PROVES
    That a confirmed broker close can be turned into the SAME realised R the
    Control Tower's reconstruction produces, from durable facts the runner
    already holds — before anything is persisted and before the daily-loss rail
    depends on it. An accounting error found after activation is an error found
    with money at risk.

THE IDENTITY UNDER TEST
    R is linear in PnL with a fixed denominator, so for one position:

        Σᵢ (profitᵢ / risk_amount)  ==  (Σᵢ profitᵢ) / risk_amount

    which is why one delta per exit deal needs no special case for partial
    closes, and why each delta can land in the UTC day it actually occurred.
    The parity section proves this rather than asserting it.

SCOPE
    Nothing here writes RunnerState, posts R, records accounted ids, prunes, or
    touches SafetyRails. Guards at the foot assert that structurally.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import close_accounting as ca                          # noqa: E402
from live import deal_records as dr                              # noqa: E402
from live import risk_math                                       # noqa: E402
from live.intents import CLOSE_POSITION, OPEN_POSITION           # noqa: E402

BASE = datetime(2026, 7, 28, 12, 0, tzinfo=timezone.utc)
POSITION = "5001"

# Planned: entry 1.1000, stop 1.0950 → distance 0.005; volume 1000 → risk 5.0
PLANNED_RISK = 5.0


def deal(**kw):
    base = dict(deal_id="9001", position_id=POSITION, order_id="4001",
                entry=dr.ENTRY_OUT, deal_type=dr.DEAL_TYPE_SELL, volume=1000.0,
                price=1.1050, profit=-5.0, commission=-0.07, fee=0.0, swap=-0.02,
                execution_time_utc=BASE, symbol="EURUSD.r", magic=77001,
                comment="intent_abc")
    base.update(kw)
    return dr.DealRecord(**base)


def ledger(*, stop=1.0950, entry=1.1000, volume=1000.0, price=1.1000,
           ticket=POSITION, intent=True, action=OPEN_POSITION, extra=None):
    """A ledger shaped exactly as the runner writes it after Milestone A."""
    detail = {"order": int(ticket), "deal": 999, "price": price,
              "filled_volume": volume, "trade_id": "t1"}
    if intent:
        detail["intent"] = {"intent_id": "i1", "action": action, "trade_id": "t1",
                            "side": "long", "frontier_bar": "2026-07-28 10:00",
                            "entry": entry, "stop": stop, "target": 1.1100}
    if extra:
        detail.update(extra)
    return {"i1": {"status": "confirmed", "detail": detail, "at": "2026-07-28T12:00:00Z"}}


def account(d=None, led=None):
    return ca.account_deal(d or deal(), led if led is not None else ledger())


# ── the accountable cases ────────────────────────────────────────────────────

@pytest.mark.parametrize("profit,expected_r", [
    (-5.0, -1.0),     # long SL: exactly -1R
    (10.0, 2.0),      # long TP: 2R
    (-2.5, -0.5),     # partial adverse exit
    (0.0, 0.0),       # breakeven exit
])
def test_a_confirmed_close_produces_the_canonical_r(profit, expected_r):
    record = account(deal(profit=profit))
    assert record.classification == ca.ACCOUNTABLE
    assert record.realised_r == pytest.approx(expected_r)
    assert record.utc_date == "2026-07-28"
    assert record.intent_id == "i1" and record.trade_id == "t1"


def test_a_short_position_is_accounted_from_the_profit_sign():
    """Direction is carried by the sign of profit; the stop distance is absolute,
    so a short with the stop ABOVE entry yields the same risk denominator."""
    short_ledger = ledger(entry=1.1000, stop=1.1050)     # stop above entry
    record = account(deal(profit=-5.0), short_ledger)
    assert record.classification == ca.ACCOUNTABLE
    assert record.realised_r == pytest.approx(-1.0)


@pytest.mark.parametrize("origin", ["engine close", "server-side SL",
                                    "server-side TP", "external manual close"])
def test_every_close_origin_accounts_identically(origin):
    """The pipeline never asks HOW a position closed. A server-side stop-out and
    an operator's manual close are the same confirmed evidence — which is exactly
    why history is the source of truth rather than a local acknowledgement."""
    record = account(deal(profit=-5.0, comment=origin))
    assert record.classification == ca.ACCOUNTABLE
    assert record.realised_r == pytest.approx(-1.0)


def test_the_executed_entry_is_the_fallback_when_no_planned_entry_exists():
    led = ledger(entry=None, price=1.1000)
    record = account(deal(profit=-5.0), led)
    assert record.classification == ca.ACCOUNTABLE
    assert record.realised_r == pytest.approx(-1.0)


def test_the_utc_day_comes_from_the_broker_execution_time():
    """Not ingestion time. A deal executed at 23:58 UTC belongs to that day even
    if it is read after midnight — the reason MS-A made R buckets per-date."""
    late = datetime(2026, 7, 27, 23, 58, tzinfo=timezone.utc)
    assert account(deal(execution_time_utc=late)).utc_date == "2026-07-27"


# ── classification: nothing is ever silently discarded ───────────────────────

def test_an_entry_deal_is_ignored_not_accounted():
    record = account(deal(entry=dr.ENTRY_IN))
    assert record.classification == ca.IGNORED
    assert record.realised_r is None


@pytest.mark.parametrize("entry", [dr.ENTRY_INOUT, dr.ENTRY_OUT_BY])
def test_reversal_and_closed_by_deals_are_quarantined(entry):
    """Terminal for the position, but not attributable to one trade by price —
    the Control Tower's reconstruction blocks finalization on exactly these."""
    record = account(deal(entry=entry))
    assert record.classification == ca.QUARANTINED
    assert record.realised_r is None
    assert entry in record.explanation


def test_a_position_with_no_ledger_record_is_unresolved():
    record = account(deal(position_id="99999"))
    assert record.classification == ca.UNRESOLVED
    assert record.realised_r is None and record.intent_id is None


def test_a_legacy_record_without_an_intent_is_unaccountable_not_unresolved():
    """Pre-MS-A records lost their intent at confirmation. The position IS ours —
    the ledger owns its ticket — so reporting it as UNRESOLVED would conflate
    "a foreign trade" with "our trade whose planned risk we lost". Those need
    different operator responses, and only the second is a permanent gap."""
    record = account(deal(), ledger(intent=False))
    assert record.classification == ca.UNACCOUNTABLE
    assert record.realised_r is None
    assert "no opening intent survives" in record.explanation


def test_a_genuinely_foreign_position_is_unresolved():
    """No ledger record claims this ticket at all — nothing of ours."""
    record = account(deal(position_id="99999"))
    assert record.classification == ca.UNRESOLVED
    assert "no ledger record owns" in record.explanation


def test_a_missing_planned_stop_is_unaccountable():
    """No planned stop means risk was never recorded. It is not inferred from a
    moved broker stop — that would fabricate the rail's denominator."""
    record = account(deal(), ledger(stop=None))
    assert record.classification == ca.UNACCOUNTABLE
    assert record.realised_r is None
    assert "must not be inferred" in record.explanation


@pytest.mark.parametrize("volume", [None, 0.0])
def test_an_incomplete_ledger_volume_is_unaccountable(volume):
    record = account(deal(), ledger(volume=volume))
    assert record.classification == ca.UNACCOUNTABLE
    assert record.realised_r is None


def test_a_zero_risk_distance_is_unaccountable_rather_than_dividing():
    record = account(deal(), ledger(entry=1.1000, stop=1.1000))
    assert record.classification == ca.UNACCOUNTABLE
    assert record.realised_r is None


def test_every_deal_ends_in_exactly_one_classification():
    """Nothing is silently discarded: a batch out equals a batch in."""
    deals = [deal(deal_id="1"), deal(deal_id="2", entry=dr.ENTRY_IN),
             deal(deal_id="3", entry=dr.ENTRY_INOUT),
             deal(deal_id="4", position_id="99999")]
    records = ca.account_deals(deals, ledger())
    assert len(records) == len(deals)
    assert {r.deal_id for r in records} == {"1", "2", "3", "4"}
    valid = {ca.ACCOUNTABLE, ca.UNRESOLVED, ca.UNACCOUNTABLE, ca.QUARANTINED,
             ca.IGNORED}
    assert all(r.classification in valid for r in records)


def test_no_classification_other_than_accountable_carries_an_r():
    for record in ca.account_deals(
            [deal(entry=dr.ENTRY_IN), deal(entry=dr.ENTRY_INOUT),
             deal(position_id="9"), deal()], ledger(stop=None)):
        if not record.accountable:
            assert record.realised_r is None, record.classification


# ── ledger resolution ────────────────────────────────────────────────────────

def test_resolution_selects_the_OPENING_intent_not_the_close_record():
    """A CLOSE intent records {"ticket": <position>, "closed": True}, and
    "ticket" is itself a canonical ticket key — so both records match the same
    position. Only the opening one carries planned entry and stop."""
    led = ledger()
    led["i2"] = {"status": "confirmed", "at": "2026-07-28T12:05:00Z",
                 "detail": {"ticket": int(POSITION), "closed": True,
                            "intent": {"intent_id": "i2", "action": CLOSE_POSITION,
                                       "trade_id": "t1", "entry": None,
                                       "stop": None}}}
    record = account(deal(), led)
    assert record.classification == ca.ACCOUNTABLE
    assert record.intent_id == "i1"                  # the OPEN record
    assert record.realised_r == pytest.approx(-1.0)


def test_resolution_uses_the_canonical_ticket_keys_not_a_second_list():
    from live.executor import Executor
    assert ca._ticket_keys() is Executor._TICKET_KEYS


@pytest.mark.parametrize("key", ["order", "ticket", "broker_order_ticket",
                                 "adopted_ticket"])
def test_every_canonical_ticket_key_resolves(key):
    """The reconcile-adoption path records `adopted_ticket` rather than `order`;
    both must resolve or adopted positions would silently go unaccounted."""
    led = ledger()
    detail = led["i1"]["detail"]
    for existing in ("order", "ticket", "broker_order_ticket", "adopted_ticket"):
        detail.pop(existing, None)
    detail[key] = int(POSITION)
    assert account(deal(), led).classification == ca.ACCOUNTABLE


def test_resolution_is_deterministic_across_ledger_ordering():
    led = ledger()
    led["a0"] = {"status": "confirmed", "detail": {"order": 12345}, "at": "x"}
    led["z9"] = {"status": "confirmed", "detail": {"order": 67890}, "at": "x"}
    assert account(deal(), led).intent_id == "i1"


def test_resolution_never_consults_the_mirror():
    """`reconcile()` clears the mirror the instant a position vanishes — the same
    event that produces the close deal. A mirror join could be destroyed before
    the deal is read, so accounting must not depend on it."""
    led = ledger()
    assert account(deal(), led).classification == ca.ACCOUNTABLE   # no mirror given


# ── partial closes and the parity identity ───────────────────────────────────

def test_two_partial_exits_sum_to_the_whole_trade_r():
    parts = [deal(deal_id="1", profit=-2.0), deal(deal_id="2", profit=-3.0)]
    records = ca.account_deals(parts, ledger())
    assert all(r.accountable for r in records)
    assert sum(r.realised_r for r in records) == pytest.approx(-1.0)


def test_many_staggered_exits_sum_to_the_whole_trade_r():
    profits = [-0.5, -1.25, 2.0, -3.75, -1.5]
    parts = [deal(deal_id=str(i), profit=p,
                  execution_time_utc=BASE + timedelta(minutes=i))
             for i, p in enumerate(profits)]
    records = ca.account_deals(parts, ledger())
    assert sum(r.realised_r for r in records) == pytest.approx(sum(profits) / PLANNED_RISK)


@pytest.mark.parametrize("profits", [
    [-5.0], [-2.0, -3.0], [1.0, -6.0], [-1.0, -1.0, -1.0, -1.0, -1.0],
    [2.5, 2.5], [-0.01, -4.99],
])
def test_partial_deltas_equal_the_canonical_whole_trade_reconstruction(profits):
    """THE parity identity, proven against the canonical implementation itself
    rather than a re-derived expectation."""
    parts = [deal(deal_id=str(i), profit=p) for i, p in enumerate(profits)]
    incremental = sum(r.realised_r for r in ca.account_deals(parts, ledger()))

    whole = risk_math.realised_risk(
        initial_stop=1.0950, initial_entry=1.1000, average_entry=1.1000,
        entry_quantity=1000.0, gross_pnl=sum(profits))
    assert incremental == pytest.approx(whole.realized_r, abs=1e-8)


def test_partial_exits_on_different_days_keep_their_own_dates():
    """A model that posted once at final close could not express this — and would
    attribute a Monday loss to Tuesday."""
    monday = deal(deal_id="1", profit=-2.0,
                  execution_time_utc=datetime(2026, 7, 27, 15, 0, tzinfo=timezone.utc))
    tuesday = deal(deal_id="2", profit=-3.0,
                   execution_time_utc=datetime(2026, 7, 28, 9, 0, tzinfo=timezone.utc))
    records = ca.account_deals([monday, tuesday], ledger())
    assert [r.utc_date for r in records] == ["2026-07-27", "2026-07-28"]
    assert sum(r.realised_r for r in records) == pytest.approx(-1.0)


def test_a_duplicate_out_deal_is_computed_twice_in_dry_run():
    """B2 has no idempotency by design — deduplication by deal id is B3's job,
    and it must be crash-safe, which is a persistence concern. Pinning the
    current behaviour makes the B3 change visible rather than silent."""
    same = [deal(deal_id="9001", profit=-5.0), deal(deal_id="9001", profit=-5.0)]
    records = ca.account_deals(same, ledger())
    assert sum(r.realised_r for r in records) == pytest.approx(-2.0)


# ── canonical delegation ─────────────────────────────────────────────────────

def test_accountable_cases_delegate_to_risk_math(monkeypatch):
    calls = []
    real = risk_math.realised_risk

    def spy(**kw):
        calls.append(kw)
        return real(**kw)
    monkeypatch.setattr(ca.risk_math, "realised_risk", spy)

    assert account().classification == ca.ACCOUNTABLE
    assert len(calls) == 1
    assert calls[0]["initial_stop"] == 1.0950
    assert calls[0]["entry_quantity"] == 1000.0
    assert calls[0]["gross_pnl"] == -5.0


def test_the_history_window_is_never_widened_to_find_entry_quantity():
    """Entry quantity comes from the ledger's `filled_volume`. Rediscovering it
    from history would make the denominator depend on WHEN the read happened, so
    R would stop being comparable between trades."""
    source = (REPO_ROOT / "live" / "close_accounting.py").read_text()
    for forbidden in ("closed_deals", "history_deals_get", "ENTRY_IN)",
                      "sum(d.volume"):
        assert forbidden not in source, f"accounting reaches for history: {forbidden}"


def test_summarise_reports_counts_without_posting_anything():
    records = ca.account_deals(
        [deal(deal_id="1", profit=-5.0), deal(deal_id="2", entry=dr.ENTRY_IN),
         deal(deal_id="3", position_id="99999")], ledger())
    summary = ca.summarise(records)
    assert summary["counts"][ca.ACCOUNTABLE] == 1
    assert summary["counts"][ca.IGNORED] == 1
    assert summary["counts"][ca.UNRESOLVED] == 1
    assert summary["accountable_r_total"] == pytest.approx(-1.0)


# ── scope guards: B2 is a dry run ────────────────────────────────────────────

def _code(path: Path) -> str:
    import ast
    text = path.read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_accounting_persists_nothing():
    source = _code(REPO_ROOT / "live" / "close_accounting.py")
    for forbidden in ("add_realized_r", "ledger_set", "mirror_set", ".save(",
                      "accounted", "prune", "RunnerState"):
        assert forbidden not in source, f"B2 mutates state: {forbidden}"


def test_no_executor_hook_was_added():
    """B2 ships inert: nothing in production calls the accounting engine."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "close_accounting", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout
    callers = [ln for ln in out.splitlines() if "/tests/" not in ln]
    assert callers == [], f"unexpected production caller: {callers}"


def test_no_duplicate_r_formula_exists():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "gross_pnl / risk_amount", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout.splitlines()
    real = [h for h in hits if "/tests/" not in h and "test_" not in h]
    outside = [h for h in real if "live/risk_math.py" not in h]
    assert outside == [], f"a second R formula exists: {outside}"


def test_safety_rails_were_not_modified():
    source = _code(REPO_ROOT / "live" / "safety.py")
    assert "close_accounting" not in source
    assert "daily_realized_r" in source          # the rail is untouched
