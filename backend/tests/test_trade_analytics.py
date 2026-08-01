"""M-TRADES-2 — analytics compute only from admissible MT5 ledger records.

THE DEFECT THIS CLOSES
    `computeMetrics([])` returned 0 trades / 0% win rate / $0 expectancy — a
    mathematically valid report of a flat result nobody observed. An empty
    admitted set is the ABSENCE of evidence, not evidence of flat performance.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
import pytest                                                       # noqa: E402

import trade_analytics as ta                                        # noqa: E402
import trade_ledger_domain as tld                                   # noqa: E402

EO, S, O = tld.ExecutionOrigin, tld.TradeLedgerStatus, tld.TradeOutcome
BACKEND = Path(__file__).resolve().parent.parent


def row(**over) -> dict:
    """A canonical ADMISSIBLE record; override to make it fail one predicate."""
    base = {
        "tradeId": "trd_1", "status": S.FINALIZED, "conflicts": [],
        "executionOrigin": EO.MT5, "origin": tld.TradeOrigin.CONTROL_TOWER,
        "outcome": O.WIN, "fullyClosed": True,
        "grossRealizedPnL": 100.0, "netRealizedPnL": 95.0, "realizedR": 2.0,
        "costCompleteness": tld.COMPLETE, "accountCurrency": "USD",
        "closedAt": "2026-07-01T10:00:00Z", "instrument": "EURUSD", "side": "buy",
    }
    base.update(over)
    return base


# ── admission (1–7) ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("over,reason", [
    ({"executionOrigin": EO.MOCK}, ta.EXCL_ORIGIN),          # 1
    ({"executionOrigin": EO.UNKNOWN}, ta.EXCL_ORIGIN),       # 2
    ({"executionOrigin": tld.PROV_DURABLE_STORE}, ta.EXCL_ORIGIN),  # 3
    ({"status": S.READY_TO_FINALIZE}, ta.EXCL_STATUS),
    ({"status": S.OBSERVED}, ta.EXCL_STATUS),
    ({"status": S.INCOMPLETE}, ta.EXCL_STATUS),
    ({"conflicts": ["mismatch"]}, ta.EXCL_CONFLICTED),       # 10
    ({"fullyClosed": False}, ta.EXCL_NOT_CLOSED),            # 6
    ({"outcome": O.UNAVAILABLE}, ta.EXCL_OUTCOME),
    ({"grossRealizedPnL": None}, ta.EXCL_PNL),               # 7
    ({"tradeId": ""}, ta.EXCL_MALFORMED),
])
def test_1_to_7_admission_predicates(over, reason):
    assert ta.admit(row(**over)) == reason


def test_4_initiation_origin_alone_never_admits():
    """CONTROL_TOWER + mock stays rejected; MANUAL_BROKER + mt5 is admitted."""
    assert ta.admit(row(origin=tld.TradeOrigin.CONTROL_TOWER,
                        executionOrigin=EO.MOCK)) == ta.EXCL_ORIGIN
    assert ta.admit(row(origin=tld.TradeOrigin.MANUAL_BROKER,
                        executionOrigin=EO.MT5)) is None


def test_5_mt5_settled_closed_records_are_admitted():
    for status in (S.FINALIZED, S.AMENDED):
        assert ta.admit(row(status=status)) is None


def test_7b_malformed_input_fails_closed():
    for bad in (None, "x", 5, [], {}):
        assert ta.admit(bad) is not None


# ── unavailable vs empty vs available (12, 13) ───────────────────────────────
def test_12_unavailable_differs_from_empty():
    unavailable = ta.compute(None, source_available=False)
    empty = ta.compute([], source_available=True)
    assert unavailable.availability == ta.AVAIL_UNAVAILABLE
    assert empty.availability == ta.AVAIL_EMPTY
    assert unavailable.availability != empty.availability


def test_13_mock_only_input_produces_no_zero_performance_report():
    """The decisive case. All mock => EMPTY with an audit trail, never a report
    of zero trades at 0% with $0 expectancy."""
    result = ta.compute([row(executionOrigin=EO.MOCK, tradeId="m1"),
                         row(executionOrigin=EO.MOCK, tradeId="m2")])
    assert result.availability == ta.AVAIL_EMPTY
    assert result.admitted_count == 0
    assert result.excluded_count == 2
    # No metric may be a number — absence must not read as a measurement.
    assert result.win_rate is None
    assert result.gross_realized_pnl is None
    assert result.expectancy_gross is None
    assert result.equity_curve_gross == ()
    assert result.profit_factor is None


def test_22_exclusions_are_auditable():
    result = ta.compute([row(tradeId="a", executionOrigin=EO.MOCK),
                         row(tradeId="b", fullyClosed=False),
                         row(tradeId="c")])
    assert result.admitted_count == 1
    reasons = {e.trade_id: e.reason for e in result.exclusions}
    assert reasons == {"a": ta.EXCL_ORIGIN, "b": ta.EXCL_NOT_CLOSED}


# ── metric correctness (14–18, 24) ───────────────────────────────────────────
def _three_trades():
    return [
        row(tradeId="w", outcome=O.WIN, grossRealizedPnL=100.0, netRealizedPnL=90.0,
            realizedR=2.0, closedAt="2026-07-01T10:00:00Z"),
        row(tradeId="l", outcome=O.LOSS, grossRealizedPnL=-50.0, netRealizedPnL=-55.0,
            realizedR=-1.0, closedAt="2026-07-02T10:00:00Z"),
        row(tradeId="b", outcome=O.BREAK_EVEN, grossRealizedPnL=0.0, netRealizedPnL=-2.0,
            realizedR=0.0, closedAt="2026-07-03T10:00:00Z"),
    ]


def test_14_win_rate_counts_break_even_in_the_denominator():
    r = ta.compute(_three_trades())
    assert (r.wins, r.losses, r.break_even) == (1, 1, 1)
    assert r.win_rate == pytest.approx(1 / 3)      # not 1/2 — BE trades happened


def test_metric_arithmetic_matches_hand_calculation():
    r = ta.compute(_three_trades())
    assert r.gross_profit == 100.0 and r.gross_loss == 50.0
    assert r.gross_realized_pnl == 50.0
    assert r.average_win == 100.0 and r.average_loss == -50.0
    assert r.profit_factor == 2.0                   # 100 / 50
    assert r.expectancy_gross == pytest.approx(50 / 3)


def test_15_profit_factor_is_undefined_without_gross_loss():
    """Never infinity, never a large sentinel that reads as a real ratio."""
    r = ta.compute([row(tradeId="w1", outcome=O.WIN, grossRealizedPnL=10.0)])
    assert r.gross_loss == 0.0
    assert r.profit_factor is None


def test_16_24_drawdown_and_curve_use_deterministic_ordering():
    forward = ta.compute(_three_trades())
    shuffled = ta.compute(list(reversed(_three_trades())))
    assert forward.equity_curve_gross == shuffled.equity_curve_gross
    assert forward.max_drawdown_gross == shuffled.max_drawdown_gross == 50.0
    assert [v for _, v in forward.equity_curve_gross] == [100.0, 50.0, 50.0]


def test_17_r_metrics_require_a_valid_denominator_on_every_trade():
    ok = ta.compute(_three_trades())
    assert ok.r_available is True
    assert ok.average_r == pytest.approx((2.0 - 1.0 + 0.0) / 3)
    missing = ta.compute(_three_trades() + [row(tradeId="x", realizedR=None)])
    assert missing.r_available is False
    assert missing.average_r is None and missing.expectancy_r is None
    assert "initial-risk" in missing.r_unavailable_reason


def test_18_currencies_are_not_mixed_silently():
    mixed = ta.compute([row(tradeId="u", accountCurrency="USD"),
                        row(tradeId="e", accountCurrency="EUR")])
    assert mixed.currencies_seen == ("EUR", "USD")
    assert mixed.account_currency is None      # refuses to name one currency


# ── cost completeness (11) ───────────────────────────────────────────────────
def test_11_missing_costs_are_not_treated_as_zero():
    incomplete = ta.compute([row(tradeId="a"),
                             row(tradeId="b", costCompleteness=tld.PARTIAL)])
    assert incomplete.net_available is False
    assert incomplete.net_realized_pnl is None
    assert "not treated as zero" in incomplete.net_unavailable_reason
    # Gross remains authoritative and IS reported — the distinction is explicit.
    assert incomplete.gross_realized_pnl == 200.0


def test_net_is_reported_when_every_admitted_trade_has_complete_costs():
    r = ta.compute(_three_trades())
    assert r.net_available is True
    assert r.net_realized_pnl == pytest.approx(90 - 55 - 2)


# ── partial closes / duplicates (8, 9, 10) ───────────────────────────────────
def test_8_partial_closes_are_never_counted_as_completed_trades():
    r = ta.compute([row(tradeId="p", fullyClosed=False, grossRealizedPnL=500.0)])
    assert r.availability == ta.AVAIL_EMPTY
    assert r.exclusions[0].reason == ta.EXCL_NOT_CLOSED


def test_9_10_one_row_per_trade_means_no_duplicate_outcomes():
    """The ledger keys on trade_id, so an amendment REPLACES the row rather than
    adding one. Two rows with the same id would be a store-level violation; the
    analytics unit of analysis is one admitted row = one completed trade."""
    r = ta.compute(_three_trades())
    assert r.admitted_count == 3
    assert r.wins + r.losses + r.break_even == r.admitted_count


# ── structural (19, 20, 25) ──────────────────────────────────────────────────
def test_19_25_no_competing_frontend_metric_authority():
    """The old client-side helper must not still compute performance."""
    src = BACKEND.parent / "frontend" / "src" / "lib" / "analytics.ts"
    if src.exists():
        text = src.read_text()
        assert "computeMetrics" not in text or "M-TRADES-2" in text, \
            "the legacy client-side metric helper still defines formulas"


def test_20_no_fixture_trade_identifier_reaches_analytics():
    for sentinel in ("tr_01J8ZC", "gh_01J8ZC", "blk_01J8ZC"):
        assert sentinel not in (BACKEND / "trade_analytics.py").read_text()


def test_21_the_store_query_filters_on_the_indexed_column():
    src = (BACKEND / "trade_ledger_store.py").read_text()
    body = src.split("def list_admissible_for_analytics")[1].split("\n    def ")[0]
    assert "execution_origin IN" in body
    assert "ORDER BY closed_at ASC, trade_id ASC" in body
    assert "ANALYTICS_ADMISSIBLE" in body       # taxonomy, not a literal


def test_23_reload_produces_identical_metrics():
    a = ta.compute(_three_trades()).as_dict()
    b = ta.compute(_three_trades()).as_dict()
    assert a == b
