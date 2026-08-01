/**
 * M-TRADES-1 — fixture trades, orders and blocked intents are gone from
 * ordinary routes, and no performance figure is derived from an inadmissible
 * input.
 *
 * The decisive case mirrors M-FLEET-2: under the mock adapter
 * `/api/operations/positions` returns a position complete with `entryPrice` and
 * `currentPrice`, stamped `mock-fixture`. If any test here can be made to pass
 * by rendering it, the milestone has failed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import {
  authoritativeOnly,
  classify,
  analyticsInputAdmissible,
  PROV_DURABLE_STORE,
  PROV_MOCK_FIXTURE,
  PROV_LIVE_MT5,
  isAuthoritative,
} from '@/lib/operationalProvenance';

/** The exact mock position the projection emits under the development adapter. */
const MOCK_POSITION = {
  brokerPositionReference: 'pos_mock_1', instrument: 'EURUSD', side: 'buy',
  quantity: 0.1, entryPrice: 1.0842, currentPrice: 1.0851,
  provenance: PROV_MOCK_FIXTURE, freshness: { stale: false },
};

const LIVE_POSITION = {
  brokerPositionReference: 'pos_real_1', instrument: 'EURUSD', side: 'sell',
  quantity: 0.25, entryPrice: 1.1002, currentPrice: null,
  provenance: PROV_LIVE_MT5, freshness: { stale: false },
};

const LIVE_ORDER = {
  brokerOrderReference: 'ord_real_1', instrument: 'GBPUSD', side: 'buy',
  provenance: PROV_LIVE_MT5, freshness: { stale: false },
};

describe('trade/order provenance', () => {
  it('rejects the mock-fixture position even though it carries an entry price', () => {
    expect(isAuthoritative(MOCK_POSITION)).toBe(false);
    const kept = authoritativeOnly([MOCK_POSITION]);
    expect(kept).toEqual([]);
    // The invented price must leave no trace at all.
    expect(JSON.stringify(kept)).not.toContain('1.0842');
  });

  it('rejects durable-store ledger records: storage is not origin', () => {
    // `durable-store` says where a record is KEPT. The same store holds trades
    // ingested from the mock adapter, so it cannot imply operational truth.
    expect(isAuthoritative({ provenance: PROV_DURABLE_STORE })).toBe(false);
  });

  it('rejects fixture, ghost, synthetic and replay provenance', () => {
    for (const p of ['fixture', 'ghost', 'synthetic', 'replay', 'mock', 'absent']) {
      expect(isAuthoritative({ provenance: p }), p).toBe(false);
    }
  });

  it('accepts live_mt5 positions and orders exactly as supplied', () => {
    expect(authoritativeOnly([LIVE_POSITION])).toEqual([LIVE_POSITION]);
    expect(authoritativeOnly([LIVE_ORDER])).toEqual([LIVE_ORDER]);
  });

  it('never merges authoritative with rejected records', () => {
    expect(authoritativeOnly([MOCK_POSITION, LIVE_POSITION])).toEqual([LIVE_POSITION]);
  });

  it('keeps empty distinct from unavailable', () => {
    expect(classify([], { sourceAnswered: true, rejectedCount: 0 })).toBe('empty');
    expect(classify([], { sourceAnswered: true, rejectedCount: 1 })).toBe('unavailable');
    expect(classify([], { sourceAnswered: false })).toBe('unavailable');
  });

  it('keeps stale authoritative records visibly stale', () => {
    const stale = { ...LIVE_POSITION, freshness: { stale: true } };
    expect(classify([stale], { sourceAnswered: true })).toBe('stale');
  });
});

describe('analytics safety boundary', () => {
  it('refuses to treat an unavailable or empty source as observed performance', () => {
    // computeMetrics([]) would return 0 trades / 0% / $0 — a valid-looking
    // report of a flat result that was never observed.
    expect(analyticsInputAdmissible('unavailable')).toBe(false);
    expect(analyticsInputAdmissible('empty')).toBe(false);
  });

  it('admits genuinely available and stale authoritative history', () => {
    expect(analyticsInputAdmissible('available')).toBe(true);
    expect(analyticsInputAdmissible('stale')).toBe(true);
  });
});

/* ── rendering ─────────────────────────────────────────────────────────────── */

const state = vi.hoisted(() => ({ trades: {} as Record<string, unknown>,
                                  analytics: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useOperationalTrades: () => state.trades,
  usePolicyMatrix: () => ({ cells: {}, synthesizedCells: 0 }),
  // M-TRADES-2: AnalyticsView now reads the authoritative backend contract.
  useLedgerAnalytics: () => state.analytics,
}));
vi.mock('react-router-dom', () => ({ useOutletContext: () => ({ pair: 'EURUSD' }) }));

import { AnalyticsView } from '@/views/AnalyticsView';

beforeEach(() => {
  state.trades = { positions: [], orders: [], status: 'unavailable', detail: 'no source' };
  state.analytics = {
    schemaVersion: 1, availability: 'empty', admittedCount: 0, excludedCount: 2,
    exclusions: [{ tradeId: 'a', reason: 'execution_origin_not_admissible' },
                 { tradeId: 'b', reason: 'execution_origin_not_admissible' }],
    accountCurrency: null, currenciesSeen: [], wins: 0, losses: 0, breakEven: 0,
    grossProfit: null, grossLoss: null, grossRealizedPnL: null, winRate: null,
    averageWin: null, averageLoss: null, profitFactor: null, expectancyGross: null,
    maxDrawdownGross: null, equityCurveGross: [],
    netAvailable: false, netRealizedPnL: null, netUnavailableReason: null,
    rAvailable: false, averageR: null, expectancyR: null, rUnavailableReason: null,
  };
});

describe('AnalyticsView under an inadmissible source', () => {
  it('reports no admissible trades rather than a zero-performance result', () => {
    const { container } = render(<AnalyticsView />);
    expect(screen.getByText(/No admissible completed trades/i)).toBeTruthy();
    const html = container.innerHTML;
    // The decisive assertion: an empty admitted set must never render as an
    // OBSERVED flat result. No rate, no currency figure, no flat curve.
    expect(html).not.toMatch(/0\.0\s*%/);
    expect(html).not.toMatch(/\$0/);
    expect(html).not.toMatch(/\b0 trades\b/);
  });

  it('shows the exclusion account so refusals are auditable', () => {
    render(<AnalyticsView />);
    expect(screen.getByTestId('analytics-exclusions').textContent)
      .toMatch(/2 × execution_origin_not_admissible/);
  });
});
