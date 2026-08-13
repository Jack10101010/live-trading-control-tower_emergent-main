"""M-TRADES-2 — performance analytics over ADMISSIBLE ledger records only.

WHY THIS IS BACKEND-COMPUTED
    The formulas live here, once. The previous analytics were a frontend helper
    (`lib/analytics.ts`) computing expectancy and equity curves over whatever
    array it was handed — which was the fixture. A helper that computes
    correctly over an array is not trustworthy; what makes a metric trustworthy
    is the admission decision that precedes it, and that decision belongs beside
    the ledger. The frontend renders what this module reports and derives
    nothing.

THE TWO INDEPENDENT GATES
    admission     may this record count at all?      (origin + status + integrity)
    completeness  is this metric derivable from it?  (cost / risk evidence)

    They are deliberately separate. A perfectly admissible MT5 trade may still
    be unable to support a NET figure because its cost evidence is incomplete —
    that is a fact about the metric, not about the record's authority. Merging
    them would silently drop good trades or, worse, publish net figures computed
    from partial costs.

WHAT THIS MODULE WILL NOT DO
    It reports `unavailable` rather than zero whenever there is nothing to
    measure. `computeMetrics([])` returning 0 trades / 0% / $0 was the defect
    that made the old analytics dangerous: a mathematically valid report of a
    flat result nobody observed. An empty admitted set is not evidence of flat
    performance; it is the absence of evidence.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import ledger_admission as adm
import trade_ledger_domain as tld

SCHEMA_VERSION = 1

# ── availability, kept distinct ───────────────────────────────────────────────
AVAIL_UNAVAILABLE = "unavailable"   # the source could not be read at all
AVAIL_EMPTY = "empty"               # the source answered; nothing was admissible
AVAIL_AVAILABLE = "available"       # admitted records exist

# ── exclusion reasons (auditable, machine-readable) ───────────────────────────
EXCL_ORIGIN = "execution_origin_not_admissible"
EXCL_STATUS = "status_not_settled"
EXCL_NOT_CLOSED = "not_fully_closed"
EXCL_OUTCOME = "outcome_unavailable"
EXCL_PNL = "gross_pnl_absent"
EXCL_CONFLICTED = "reconciliation_conflicted"
EXCL_MALFORMED = "record_malformed"

#: Statuses representing SETTLED truth. `READY_TO_FINALIZE` is assessed but not
#: settled, so it is excluded: an assessment that may still change is not a
#: performance fact. `CONFLICTED` and `INCOMPLETE` are excluded by definition.
SETTLED_STATUSES = frozenset({
    tld.TradeLedgerStatus.FINALIZED,
    tld.TradeLedgerStatus.AMENDED,
})


@dataclass(frozen=True)
class Exclusion:
    trade_id: str | None
    reason: str


@dataclass(frozen=True)
class AnalyticsResult:
    """Metrics plus a full account of what was refused and why."""
    availability: str
    schema_version: int = SCHEMA_VERSION
    admitted_count: int = 0
    excluded_count: int = 0
    exclusions: tuple = field(default_factory=tuple)
    account_currency: str | None = None
    currencies_seen: tuple = field(default_factory=tuple)

    # counts
    wins: int = 0
    losses: int = 0
    break_even: int = 0

    # gross-only figures (always safe when P&L evidence exists)
    gross_profit: float | None = None
    gross_loss: float | None = None
    gross_realized_pnl: float | None = None
    win_rate: float | None = None
    average_win: float | None = None
    average_loss: float | None = None
    profit_factor: float | None = None
    expectancy_gross: float | None = None
    max_drawdown_gross: float | None = None
    equity_curve_gross: tuple = field(default_factory=tuple)

    # net figures — ONLY when every admitted trade has complete cost evidence
    net_available: bool = False
    net_realized_pnl: float | None = None
    net_unavailable_reason: str | None = None

    # R figures — ONLY when every admitted trade has a valid risk denominator
    r_available: bool = False
    average_r: float | None = None
    expectancy_r: float | None = None
    r_unavailable_reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "schemaVersion": self.schema_version,
            "availability": self.availability,
            "admittedCount": self.admitted_count,
            "excludedCount": self.excluded_count,
            "exclusions": [{"tradeId": e.trade_id, "reason": e.reason}
                           for e in self.exclusions],
            "accountCurrency": self.account_currency,
            "currenciesSeen": list(self.currencies_seen),
            "wins": self.wins, "losses": self.losses, "breakEven": self.break_even,
            "grossProfit": self.gross_profit, "grossLoss": self.gross_loss,
            "grossRealizedPnL": self.gross_realized_pnl,
            "winRate": self.win_rate,
            "averageWin": self.average_win, "averageLoss": self.average_loss,
            "profitFactor": self.profit_factor,
            "expectancyGross": self.expectancy_gross,
            "maxDrawdownGross": self.max_drawdown_gross,
            "equityCurveGross": [{"at": at, "cumulative": v}
                                 for at, v in self.equity_curve_gross],
            "netAvailable": self.net_available,
            "netRealizedPnL": self.net_realized_pnl,
            "netUnavailableReason": self.net_unavailable_reason,
            "rAvailable": self.r_available,
            "averageR": self.average_r, "expectancyR": self.expectancy_r,
            "rUnavailableReason": self.r_unavailable_reason,
        }


def _num(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) and value == value else None


def admit(row: dict) -> str | None:
    """Return an exclusion reason, or None when the record is admissible.

    Order matters only for reporting quality: origin is checked first so the
    common development case reports the honest reason rather than a downstream
    symptom.
    """
    if not isinstance(row, dict):
        return EXCL_MALFORMED
    if not adm.admits_analytics(row.get("executionOrigin")):
        return EXCL_ORIGIN
    if row.get("status") not in SETTLED_STATUSES:
        return EXCL_STATUS
    if row.get("conflicts"):
        return EXCL_CONFLICTED
    # A partial close is a fact about quantity. Counting one as a completed
    # trade would treat a still-open position as a finished outcome.
    if not row.get("fullyClosed"):
        return EXCL_NOT_CLOSED
    if row.get("outcome") in (None, tld.TradeOutcome.UNAVAILABLE):
        return EXCL_OUTCOME
    if _num(row.get("grossRealizedPnL")) is None:
        return EXCL_PNL
    if not isinstance(row.get("tradeId"), str) or not row["tradeId"]:
        return EXCL_MALFORMED
    return None


def compute(rows, *, source_available: bool = True) -> AnalyticsResult:
    """Compute metrics over admitted records.

    `rows` is a sequence of serialized ledger entries. `source_available=False`
    means the ledger could not be read — which is NOT the same as reading it and
    finding nothing, and the two must never render identically.
    """
    if not source_available or rows is None:
        return AnalyticsResult(availability=AVAIL_UNAVAILABLE)

    admitted: list[dict] = []
    exclusions: list[Exclusion] = []
    for row in rows:
        reason = admit(row)
        if reason is None:
            admitted.append(row)
        else:
            tid = row.get("tradeId") if isinstance(row, dict) else None
            exclusions.append(Exclusion(trade_id=tid, reason=reason))

    if not admitted:
        # The source answered and nothing qualified. Report EMPTY with the full
        # exclusion account — never a zero-performance report.
        return AnalyticsResult(availability=AVAIL_EMPTY,
                               excluded_count=len(exclusions),
                               exclusions=tuple(exclusions))

    # Deterministic ordering: closed time, then trade id to break ties. Drawdown
    # and the equity curve are order-dependent, so this must not vary by
    # query plan or dict ordering.
    admitted.sort(key=lambda r: (r.get("closedAt") or "", r.get("tradeId") or ""))

    currencies = tuple(sorted({r.get("accountCurrency") for r in admitted
                               if r.get("accountCurrency")}))

    wins = [r for r in admitted if r.get("outcome") == tld.TradeOutcome.WIN]
    losses = [r for r in admitted if r.get("outcome") == tld.TradeOutcome.LOSS]
    breakeven = [r for r in admitted if r.get("outcome") == tld.TradeOutcome.BREAK_EVEN]

    gross = [_num(r.get("grossRealizedPnL")) or 0.0 for r in admitted]
    gross_profit = round(sum(g for g in gross if g > 0), 6)
    gross_loss = round(abs(sum(g for g in gross if g < 0)), 6)
    total_gross = round(sum(gross), 6)

    n = len(admitted)
    # Break-even trades are counted in the denominator: they happened, and
    # excluding them would inflate the rate.
    win_rate = round(len(wins) / n, 6)
    avg_win = round(sum(_num(r.get("grossRealizedPnL")) or 0.0 for r in wins) / len(wins), 6) if wins else None
    avg_loss = round(sum(_num(r.get("grossRealizedPnL")) or 0.0 for r in losses) / len(losses), 6) if losses else None
    # Profit factor is undefined with no gross loss — reported as None, never
    # as infinity and never as a large sentinel that reads as a real ratio.
    profit_factor = round(gross_profit / gross_loss, 6) if gross_loss > 0 else None
    expectancy_gross = round(total_gross / n, 6)

    cumulative, curve, peak, max_dd = 0.0, [], 0.0, 0.0
    for r in admitted:
        cumulative = round(cumulative + (_num(r.get("grossRealizedPnL")) or 0.0), 6)
        curve.append((r.get("closedAt"), cumulative))
        peak = max(peak, cumulative)
        max_dd = max(max_dd, round(peak - cumulative, 6))

    # ── net: requires COMPLETE cost evidence on every admitted trade ─────────
    incomplete_costs = [r for r in admitted
                        if r.get("costCompleteness") != tld.COMPLETE]
    if incomplete_costs:
        net_available, net_pnl = False, None
        net_reason = (f"{len(incomplete_costs)} of {n} admitted trades lack complete "
                      "cost evidence; net performance is not derivable and missing "
                      "costs are not treated as zero")
    else:
        nets = [_num(r.get("netRealizedPnL")) for r in admitted]
        if any(v is None for v in nets):
            net_available, net_pnl = False, None
            net_reason = "cost evidence is complete but a net figure is absent"
        else:
            net_available, net_pnl, net_reason = True, round(sum(nets), 6), None

    # ── R: requires a valid initial-risk denominator on every admitted trade ──
    rs = [_num(r.get("realizedR")) for r in admitted]
    if any(v is None for v in rs):
        r_available, avg_r, exp_r = False, None, None
        r_reason = ("one or more admitted trades has no valid initial-risk "
                    "denominator; R metrics are not derivable")
    else:
        r_available = True
        avg_r = round(sum(rs) / n, 6)
        exp_r = avg_r          # expectancy in R IS the mean R per trade
        r_reason = None

    return AnalyticsResult(
        availability=AVAIL_AVAILABLE,
        admitted_count=n, excluded_count=len(exclusions),
        exclusions=tuple(exclusions),
        account_currency=currencies[0] if len(currencies) == 1 else None,
        currencies_seen=currencies,
        wins=len(wins), losses=len(losses), break_even=len(breakeven),
        gross_profit=gross_profit, gross_loss=gross_loss,
        gross_realized_pnl=total_gross, win_rate=win_rate,
        average_win=avg_win, average_loss=avg_loss,
        profit_factor=profit_factor, expectancy_gross=expectancy_gross,
        max_drawdown_gross=max_dd, equity_curve_gross=tuple(curve),
        net_available=net_available, net_realized_pnl=net_pnl,
        net_unavailable_reason=net_reason,
        r_available=r_available, average_r=avg_r, expectancy_r=exp_r,
        r_unavailable_reason=r_reason)
