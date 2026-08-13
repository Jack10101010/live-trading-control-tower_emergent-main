"""MS-A Phase 1/3 — characterization of the canonical realised-R calculation.

WHY THESE EXIST
    The realised-R formula is about to be extracted from
    `backend/trade_reconstruction.py:_risk_summary` into a shared pure module so
    the autonomous runner and the trade ledger can consume ONE implementation.

    These tests pin the CURRENT behaviour first, so extraction is provably a
    move and not a rewrite. They are deliberately written against observable
    output — every field of `TradeRiskSummary` — rather than the internals, so
    they keep their meaning after the code moves.

    They also protect historical trade-ledger records: a change in this formula
    would silently re-value every trade the ledger has already reconstructed.

TWO CHARACTERIZATION FINDINGS WORTH STATING
    1. The formula NEVER consults side. Long vs short is already encoded in the
       sign of `gross_pnl`, so a losing long and a losing short with identical
       PnL produce identical R. The parametrised cases below assert this rather
       than implying direction is handled separately.
    2. The basis is GROSS, by explicit documented policy — costs are excluded
       because commission/swap evidence often arrives late, and an R that
       changed when a swap settled would not be comparable across trades.
"""

from __future__ import annotations

import pytest

import trade_ledger_domain as tld
from trade_reconstruction import _risk_summary


def summary(*, stop=1.0950, entry=1.1000, avg_entry=1.1000, qty=1.0, pnl=50.0,
            intent=True):
    """One evaluation, with the intent row shaped as the production store emits."""
    intent_row = None
    if intent:
        intent_row = {"stop_loss": stop, "entry": entry}
    return _risk_summary(intent_row=intent_row, average_entry=avg_entry,
                         entry_quantity=qty, gross_pnl=pnl)


# ── the arithmetic ───────────────────────────────────────────────────────────

def test_risk_amount_is_planned_stop_distance_times_entry_quantity():
    r = summary(stop=1.0950, entry=1.1000, qty=2.0, pnl=0.0)
    assert r.initial_risk_amount == pytest.approx(0.01, abs=1e-9)   # 0.005 × 2
    assert r.initial_stop_price == 1.0950
    assert r.initial_entry_price == 1.1000
    assert r.planned_r == 1.0
    assert r.realized_r_basis == "gross"


@pytest.mark.parametrize("pnl,expected_r", [
    (10.0, 2.0),      # 2R win
    (-10.0, -2.0),    # 2R loss
    (5.0, 1.0),       # exactly 1R
    (-5.0, -1.0),     # exactly -1R
    (0.0, 0.0),       # breakeven
    (-2.5, -0.5),     # fractional loss
])
def test_realized_r_is_gross_pnl_over_risk_amount(pnl, expected_r):
    # stop distance 0.005 × qty 1000 = risk amount 5.0
    r = summary(stop=1.0950, entry=1.1000, qty=1000.0, pnl=pnl)
    assert r.initial_risk_amount == pytest.approx(5.0)
    assert r.realized_r == pytest.approx(expected_r)
    assert r.completeness == tld.COMPLETE


def test_direction_is_carried_by_pnl_sign_not_by_side():
    """CHARACTERIZATION: the formula takes no `side` argument.

    A long and a short with the same planned distance and the same gross PnL are
    indistinguishable here. Anything that later needs direction must not expect
    this function to supply it.
    """
    long_loss = summary(stop=1.0950, entry=1.1000, qty=1000.0, pnl=-5.0)
    # A short: stop ABOVE entry. Distance is absolute, so risk is identical.
    short_loss = summary(stop=1.1050, entry=1.1000, qty=1000.0, pnl=-5.0)
    assert long_loss.initial_risk_amount == short_loss.initial_risk_amount
    assert long_loss.realized_r == short_loss.realized_r == pytest.approx(-1.0)


def test_stop_distance_is_absolute_so_stop_above_or_below_entry_both_work():
    below = summary(stop=1.0950, entry=1.1000, qty=1.0, pnl=0.0)
    above = summary(stop=1.1050, entry=1.1000, qty=1.0, pnl=0.0)
    assert below.initial_risk_amount == above.initial_risk_amount


# ── entry basis policy ───────────────────────────────────────────────────────

def test_intended_entry_is_preferred_over_the_executed_average():
    """The PLANNED entry defines planned risk; slippage must not change it."""
    r = summary(entry=1.1000, avg_entry=1.1020, stop=1.0950, qty=1.0, pnl=0.0)
    assert r.initial_entry_price == 1.1000
    assert r.initial_risk_amount == pytest.approx(0.005)


def test_executed_average_is_the_fallback_when_no_intended_entry_exists():
    r = summary(entry=None, avg_entry=1.1020, stop=1.0950, qty=1.0, pnl=0.0)
    assert r.initial_entry_price == 1.1020
    assert r.initial_risk_amount == pytest.approx(0.007)


# ── completeness ladder ──────────────────────────────────────────────────────

def test_no_intent_row_is_unavailable_and_carries_nothing():
    r = summary(intent=False)
    assert r.completeness == tld.UNAVAILABLE
    assert r.realized_r is None and r.initial_risk_amount is None
    assert r.initial_stop_price is None and r.initial_entry_price is None


def test_absent_stop_is_unavailable_but_still_reports_the_entry():
    """No planned stop means no planned risk. It is NOT inferred from a moved
    broker stop — that would fabricate the number the rail depends on."""
    r = summary(stop=None, entry=1.1000)
    assert r.completeness == tld.UNAVAILABLE
    assert r.initial_entry_price == 1.1000
    assert r.initial_stop_price is None
    assert r.realized_r is None


@pytest.mark.parametrize("entry,avg_entry,qty", [
    (None, None, 1.0),      # no entry basis at all
    (1.1000, 1.1000, None),  # no quantity
])
def test_missing_entry_basis_or_quantity_is_partial(entry, avg_entry, qty):
    r = summary(entry=entry, avg_entry=avg_entry, qty=qty)
    assert r.completeness == tld.PARTIAL
    assert r.realized_r is None
    assert r.initial_stop_price == 1.0950


def test_zero_stop_distance_is_partial_with_an_explicit_zero_risk():
    """Entry == stop. Zero risk is reported as 0.0 rather than divided by."""
    r = summary(stop=1.1000, entry=1.1000, qty=1.0, pnl=25.0)
    assert r.completeness == tld.PARTIAL
    assert r.initial_risk_amount == 0.0
    assert r.realized_r is None


def test_zero_quantity_is_partial_and_never_divides_by_zero():
    r = summary(qty=0.0, pnl=25.0)
    assert r.completeness == tld.PARTIAL
    assert r.initial_risk_amount == 0.0
    assert r.realized_r is None


def test_absent_gross_pnl_is_partial_with_risk_still_reported():
    """An open or unpriced trade: planned risk is known, the outcome is not."""
    r = summary(pnl=None)
    assert r.completeness == tld.PARTIAL
    assert r.realized_r is None
    assert r.initial_risk_amount == pytest.approx(0.005)
    assert r.planned_r == 1.0


# ── numerical behaviour ──────────────────────────────────────────────────────

def test_realized_r_is_rounded_to_six_places():
    # risk 3.0; 1/3 → 0.333333
    r = summary(stop=1.0970, entry=1.1000, qty=1000.0, pnl=1.0)
    assert r.initial_risk_amount == pytest.approx(3.0)
    assert r.realized_r == pytest.approx(0.333333, abs=1e-9)


def test_risk_amount_is_rounded_to_eight_places():
    r = summary(stop=1.09501234567, entry=1.1, qty=1.0, pnl=0.0)
    assert r.initial_risk_amount == round(abs(1.1 - 1.09501234567) * 1.0, 8)


def test_negative_quantity_is_treated_as_non_positive_risk():
    """CHARACTERIZATION of the current guard: `risk_amount <= 0` catches it."""
    r = summary(qty=-1.0, pnl=10.0)
    assert r.completeness == tld.PARTIAL
    assert r.initial_risk_amount == 0.0


# ── output schema ────────────────────────────────────────────────────────────

def test_output_schema_and_serialization_are_stable():
    """The ledger persists these keys; a rename silently breaks stored records."""
    r = summary(stop=1.0950, entry=1.1000, qty=1000.0, pnl=-5.0)
    assert r.as_dict() == {
        "initialStopPrice": 1.0950,
        "initialEntryPrice": 1.1000,
        "initialRiskAmount": pytest.approx(5.0),
        "plannedR": 1.0,
        "realizedR": pytest.approx(-1.0),
        "realizedRBasis": "gross",
        "riskCompleteness": tld.COMPLETE,
    }


def test_basis_is_gross_on_every_outcome_including_incomplete_ones():
    """`realized_r_basis` is stated on every record, so a consumer never has to
    guess whether costs were deducted."""
    for r in (summary(), summary(intent=False), summary(stop=None),
              summary(pnl=None), summary(qty=0.0)):
        assert r.realized_r_basis == "gross"


def test_costs_are_excluded_from_r_by_documented_policy():
    """Two trades with identical gross PnL yield identical R regardless of what
    commission or swap later settles — that is the point of the gross basis."""
    a = summary(pnl=-5.0, qty=1000.0)
    b = summary(pnl=-5.0, qty=1000.0)
    assert a.realized_r == b.realized_r == pytest.approx(-1.0)
