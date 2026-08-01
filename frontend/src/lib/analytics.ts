/**
 * M-TRADES-2 — the client-side metric helper is RETIRED.
 *
 * `computeMetrics`, `equityCurve` and `groupMetrics` computed expectancy, win
 * rate and equity curves over whatever array they were handed — which was the
 * fixture. A helper that computes correctly over an array is not trustworthy;
 * what makes a metric trustworthy is the admission decision that precedes it.
 *
 * Formulas now live in ONE place, `backend/trade_analytics.py`, beside the
 * ledger and the admission policy. The frontend renders what the backend
 * reports and derives nothing, so there is no second metric authority that
 * could drift from the first.
 *
 * Only the presentational helper survives.
 */

/** Profit factor is `null` when gross loss is zero — never infinity. */
export function fmtPF(pf: number | null): string {
  return pf === null || pf === undefined ? '—' : pf.toFixed(2);
}
