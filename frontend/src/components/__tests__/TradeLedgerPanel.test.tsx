/**
 * LIVE-4C — the Trade Ledger UI renders recorded ledger facts and nothing else.
 *
 *   - the table and detail view print backend-recorded values verbatim;
 *   - an unavailable figure renders as "—", NEVER as 0 (no frontend accounting);
 *   - completeness / conflict / amendment states appear as explicit badges;
 *   - lineage, deals, management history and ledger events are shown as recorded;
 *   - there are no edit, delete or PnL-adjustment controls.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type {
  ClosedTradeOperationalView,
  LedgerOperationalSummary,
  TradeLedgerView,
} from '@/lib/api';
import { api } from '@/lib/api';
import { TradeLedgerPanel } from '@/components/domain/TradeLedgerPanel';

function summary(over: Partial<LedgerOperationalSummary> = {}): LedgerOperationalSummary {
  return {
    finalizedTradeCount: 1, incompleteTradeCount: 0, conflictedTradeCount: 0,
    amendedTradeCount: 0, grossRealizedPnL: 50, netRealizedPnL: 47.5,
    totalCosts: -2.5, latestClosedAt: '2026-07-01T03:00:00Z', accountCurrency: 'USD',
    availability: 'ok', provenance: 'durable-store', freshness: null, ...over,
  };
}

function trade(over: Partial<ClosedTradeOperationalView> = {}): ClosedTradeOperationalView {
  return {
    tradeId: 'trd_aaaaaaaaaaaaaaaa', status: 'FINALIZED', version: 1,
    instrument: 'EURUSD', side: 'long', quantity: 1, averageEntryPrice: 1.1,
    averageExitPrice: 1.105, grossRealizedPnL: 50, totalCosts: -2.5,
    netRealizedPnL: 47.5, realizedR: 2.5, exitClassification: 'TAKE_PROFIT',
    outcome: 'WIN', accountCurrency: 'USD', openedAt: '2026-07-01T01:00:00Z',
    closedAt: '2026-07-01T03:00:00Z', durationSeconds: 7200,
    scenarioId: 'scn_bbbbbbbbbbbbbbbb', origin: 'CONTROL_TOWER',
    costCompleteness: 'complete', riskCompleteness: 'complete',
    warnings: [], conflicts: [], finalized: true,
    detail: {
      trade: {
        lineage: {
          recommendationId: 'rec_1', intentIds: ['intent_1'],
          internalOperationIds: ['cmd_1'], brokerOrderIds: ['O1'],
          brokerPositionIds: ['P1'], brokerDealIds: ['D1', 'D2'],
          nodeId: 'node-1', accountFingerprint: 'acctfp_1',
        },
        commission: -2, fees: 0, swap: -0.5, initialStopPrice: 1.095,
        initialRiskAmount: 20, realizedRBasis: 'gross',
        requestedEntryPrice: 1.1, averageFillPrice: 1.1, entrySlippage: 0,
        executionQualityCompleteness: 'partial',
      },
      managementHistory: [{ intentId: 'intent_1', command: 'ModifyPositionProtection',
                            state: 'acknowledged', at: '2026-07-01T02:00:00Z',
                            reason: 'broker_acknowledged' }],
      reconciliationFindings: [],
    },
    provenance: 'mock-fixture', ...over,
  };
}

function view(over: Partial<TradeLedgerView> = {}): TradeLedgerView {
  return {
    available: true, code: null, summary: summary(), trades: [trade()],
    totalCount: 1, provenance: 'durable-store', limit: 50, offset: 0, ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <TradeLedgerPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('TradeLedgerPanel', () => {
  it('renders the ledger table from recorded values', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    const row = screen.getByTestId('ledger-row').textContent ?? '';
    expect(row).toContain('EURUSD');
    expect(row).toContain('long');
    expect(row).toContain('1.10000');       // avg entry, recorded precision
    expect(row).toContain('50.00');         // gross, verbatim
    expect(row).toContain('-2.50');         // costs, verbatim
    expect(row).toContain('47.50');         // net, verbatim — NOT recomputed
    expect(row).toContain('2.50R');
    expect(row).toContain('TAKE_PROFIT');
  });

  it('marks the panel read-only historical accounting', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('ledger-scope'));
    const scope = screen.getByTestId('ledger-scope').textContent ?? '';
    expect(scope).toContain('READ ONLY');
    expect(scope).toContain('HISTORICAL ACCOUNTING');
  });

  it('renders ledger totals without analytics', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('ledger-summary'));
    const s = screen.getByTestId('ledger-summary').textContent ?? '';
    expect(s).toContain('Finalized');
    expect(s).toContain('Gross realized');
    expect(s).toContain('Net realized');
    for (const banned of ['Win rate', 'Expectancy', 'Drawdown', 'Equity']) {
      expect(s).not.toContain(banned);
    }
  });

  it('shows unavailable figures as "—" and never as zero', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view({
      trades: [trade({
        status: 'READY_TO_FINALIZE', finalized: false,
        totalCosts: null, netRealizedPnL: null, realizedR: null,
        costCompleteness: 'unavailable', riskCompleteness: 'unavailable',
      })],
      summary: summary({ netRealizedPnL: null, totalCosts: null }),
    }));
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    const row = screen.getByTestId('ledger-row').textContent ?? '';
    expect(row).toContain('—');
    expect(row).not.toContain('0.00R');      // absent R is not rendered as 0R
    const s = screen.getByTestId('ledger-summary').textContent ?? '';
    expect(s).toContain('—');
  });

  it('shows completeness and status badges explicitly', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view({
      trades: [trade({
        status: 'INCOMPLETE', finalized: false, scenarioId: null,
        costCompleteness: 'unavailable', riskCompleteness: 'unavailable',
        warnings: ['unresolved_material_reconciliation_finding'],
      })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('ledger-status-badges'));
    const badges = screen.getByTestId('ledger-status-badges').textContent ?? '';
    expect(badges).toContain('INCOMPLETE');
    expect(badges).toContain('COSTS PENDING');
    expect(badges).toContain('RISK UNAVAILABLE');
    expect(badges).toContain('SCENARIO UNLINKED');
    expect(badges).toContain('RECONCILIATION REQUIRED');
  });

  it('shows AMENDED and CONFLICTED states', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view({
      trades: [trade({ status: 'AMENDED', version: 2 }),
               trade({ tradeId: 'trd_cccccccccccccccc', status: 'CONFLICTED',
                       finalized: false, conflicts: ['conflicting_scenario_lineage'] })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    const all = screen.getAllByTestId('ledger-status-badges').map((b) => b.textContent).join(' ');
    expect(all).toContain('AMENDED');
    expect(all).toContain('CONFLICTED');
  });

  it('opens a detail view with lineage, management history and events', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view());
    vi.spyOn(api, 'ledgerTradeHistory').mockResolvedValue({
      events: [
        { eventId: 'evt_1', tradeId: 'trd_aaaaaaaaaaaaaaaa', sequence: 1,
          eventType: 'BrokerHistoryObserved', occurredAt: '2026-07-01T03:05:00Z',
          recordedAt: '2026-07-01T03:05:00Z', payload: {}, provenance: 'mock-fixture' },
        { eventId: 'evt_2', tradeId: 'trd_aaaaaaaaaaaaaaaa', sequence: 2,
          eventType: 'TradeFinalized', occurredAt: '2026-07-01T03:06:00Z',
          recordedAt: '2026-07-01T03:06:00Z', payload: {}, provenance: 'mock-fixture' },
      ],
      latestVersion: 1,
    });
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    fireEvent.click(screen.getByTestId('ledger-row'));
    await waitFor(() => screen.getByTestId('ledger-trade-detail'));
    const lineage = screen.getByTestId('ledger-lineage').textContent ?? '';
    expect(lineage).toContain('scn_bbbbbbbbbbbbbbbb');
    expect(lineage).toContain('rec_1');
    expect(lineage).toContain('intent_1');
    expect(lineage).toContain('O1');
    expect(lineage).toContain('P1');
    expect(lineage).toContain('D1, D2');          // full deal list
    expect(lineage).toContain('CONTROL_TOWER');
    expect(screen.getByTestId('ledger-management-history').textContent)
      .toContain('ModifyPositionProtection');
    await waitFor(() => {
      expect(screen.getByTestId('ledger-event-history').textContent)
        .toContain('TradeFinalized');
    });
  });

  it('filters by status and instrument', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view({
      trades: [trade(),
               trade({ tradeId: 'trd_dddddddddddddddd', instrument: 'GBPUSD',
                       status: 'INCOMPLETE', finalized: false })],
      summary: summary({ finalizedTradeCount: 1, incompleteTradeCount: 1 }),
    }));
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    expect(screen.getAllByTestId('ledger-row')).toHaveLength(2);
    fireEvent.change(screen.getByTestId('ledger-filter-instrument'),
                     { target: { value: 'GBPUSD' } });
    expect(screen.getAllByTestId('ledger-row')).toHaveLength(1);
    fireEvent.change(screen.getByTestId('ledger-filter-instrument'), { target: { value: '' } });
    fireEvent.change(screen.getByTestId('ledger-filter-status'),
                     { target: { value: 'FINALIZED' } });
    expect(screen.getAllByTestId('ledger-row')).toHaveLength(1);
  });

  it('shows UNAVAILABLE when the ledger store cannot be read', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view({
      available: false, code: 'ledger_store_unavailable', trades: [], totalCount: 0,
    }));
    mount();
    await waitFor(() => screen.getByTestId('ledger-unavailable'));
    expect(screen.getByTestId('ledger-unavailable').textContent)
      .toContain('ledger_store_unavailable');
    expect(screen.queryByTestId('ledger-table')).toBeNull();
  });

  it('shows an explicit error without reconstructing anything locally', async () => {
    vi.spyOn(api, 'ledgerTrades').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() => screen.getByTestId('ledger-error'));
    const err = screen.getByTestId('ledger-error').textContent ?? '';
    expect(err).toContain('UNAVAILABLE');
    expect(err).toContain('nothing is reconstructed locally');
  });

  it('offers no edit, delete or adjustment controls', async () => {
    vi.spyOn(api, 'ledgerTrades').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('ledger-table'));
    expect(document.querySelectorAll('button').length).toBe(0);
    expect(document.querySelectorAll('input, textarea').length).toBe(0);
    // Only the two read-only filter selects exist.
    expect(document.querySelectorAll('select').length).toBe(2);
  });
});
