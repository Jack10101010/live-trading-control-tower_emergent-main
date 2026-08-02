/**
 * LIVE-4A — the operational dashboard renders the projection and nothing else.
 *
 *   - ONE query feeds every card (no per-card fetching or aggregation);
 *   - LIVE / MOCK / STALE / UNAVAILABLE / RECONCILIATION REQUIRED badges are
 *     explicit and driven by projection fields, never inferred locally;
 *   - node, account, order and position views render projected values verbatim;
 *   - an unavailable projection shows nothing operational rather than guessing.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type {
  AccountOperationalView,
  NodeOperationalView,
  OperationalSummaryView,
  OrderOperationalView,
  PositionOperationalView,
  ProjectionFreshness,
} from '@/lib/api';
import { api } from '@/lib/api';
import { OperationalDashboard } from '@/components/domain/OperationalDashboard';

function fresh(over: Partial<ProjectionFreshness> = {}): ProjectionFreshness {
  return {
    projectionAt: '2026-07-27T12:00:00Z', sourceAt: '2026-07-27T12:00:00Z',
    ageSeconds: 3, stale: false, available: true, staleAfterSeconds: 120,
    status: 'ok', detail: null, ...over,
  };
}

function node(over: Partial<NodeOperationalView> = {}): NodeOperationalView {
  return {
    nodeId: 'node-1', instanceId: 'node-1', deployment: 'EURUSD',
    // M-NODE-READ-1: the projection now leaves these null on a node view —
    // they were the Control Tower's own state under the node's name. The
    // fixture matches the projection so the test describes what ships.
    adapter: null, broker: null, accountFingerprintMasked: null,
    connectionState: null, heartbeatAgeSeconds: 3, health: 'current',
    executionMode: null, authorizationSummary: null,
    reconciliationState: null, openPositionCount: 1, openOrderCount: null,
    activeScenarioCount: null, lastActivity: '2026-07-27T12:00:00Z',
    telemetryAgeSeconds: 3, warnings: [],
    lifecycleState: 'current', degradedReasons: [],
    deploymentProfile: 'vps-dry-run', strategyFamily: null,
    engineVersion: 'lux@1.4.2', engineVersionExpected: null,
    configFingerprint: null, inputRevision: null,
    symbol: 'EURUSD', timeframe: 'M15', dataSeam: null,
    cycleStatus: 'ok', cycleSequence: 412,
    lastBoundary: '2026-07-27T11:45:00Z', lastBarTime: null, cycleNote: null,
    nodeMode: 'dry_run', submissionDisabled: true, killSwitchActive: false,
    mt5Observation: null,
    publishedAt: '2026-07-27T12:00:00Z', receivedAt: '2026-07-27T12:00:03Z',
    livenessAgeSeconds: 3, livenessStale: false, dataStale: false,
    freshnessBasis: 'received_at', staleAfterSeconds: 900, legacySource: false,
    // A node view carries NODE provenance. `live_mt5` here described a record
    // the projection has never produced.
    provenance: 'node-telemetry',
    freshness: fresh(), ...over,
  };
}

function account(over: Partial<AccountOperationalView> = {}): AccountOperationalView {
  return {
    accountFingerprint: 'acct_1', broker: 'brk_x', server: 'Demo', balance: 100000,
    equity: 100412, margin: 0, marginLevel: 0, leverage: 100, currency: 'USD',
    unrealizedPnL: 12.5, realizedPnLToday: null, openRisk: null,
    connectionState: 'Connected', provenance: 'live_mt5', freshness: fresh(),
    // M-MT5-READ-1 additive fields. Defaulted to "not reported" here so this
    // fixture keeps describing a LOCALLY-read account, not a relayed one.
    freeMargin: null, tradeAllowed: null, tradeExpert: null,
    nodeId: null, observedAt: null,
    ...over,
  };
}

function order(over: Partial<OrderOperationalView> = {}): OrderOperationalView {
  return {
    intentId: 'intent_abc123456789', brokerOrderReference: 'O1', brokerTicket: 'O1',
    instrument: 'EURUSD', side: 'long', orderType: 'market', quantity: 0.2,
    requestedPrice: null, currentState: 'acknowledged', lifecycle: [],
    brokerStatus: 'pending', scenarioId: 'EURUSD:london',
    timestamps: { createdAt: '2026-07-27T12:00:00Z', updatedAt: null },
    reconciliation: { required: false }, nodeId: null, provenance: 'live_mt5',
    ...over,
  };
}

function position(over: Partial<PositionOperationalView> = {}): PositionOperationalView {
  return {
    brokerPositionReference: 'P1', instrument: 'EURUSD', side: 'long', quantity: 0.1,
    entryPrice: 1.1, currentPrice: null, unrealizedPnL: 12.5, stopLoss: 1.095,
    takeProfit: 1.11, ageSeconds: null, lifecycle: [], protectionState: 'protected',
    reconciliation: { required: false, locked: false }, scenarioId: null,
    nodeId: null, accountFingerprint: 'acctfp_live1', provenance: 'live_mt5',
    ...over,
  };
}

function summary(over: Partial<OperationalSummaryView> = {}): OperationalSummaryView {
  return {
    schemaVersion: 'ct.operational-projection.v1',
    projectionTimestamp: '2026-07-27T12:00:00Z',
    nodes: [node()], accounts: [account()], activeOrders: [order()],
    openPositions: [position()], activeScenarios: [],
    reconciliationIssues: [], activeOperations: [], warnings: [],
    counts: { nodes: 1, accounts: 1, activeOrders: 1, openPositions: 1 },
    freshness: fresh(), ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <OperationalDashboard />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('OperationalDashboard', () => {
  it('uses ONE projection query for every card', async () => {
    const spy = vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary());
    // Any per-collection fetch would be a second aggregation source.
    const nodes = vi.spyOn(api, 'operationsNodes');
    const accounts = vi.spyOn(api, 'operationsAccounts');
    const orders = vi.spyOn(api, 'operationsOrders');
    const positions = vi.spyOn(api, 'operationsPositions');
    mount();
    await waitFor(() => screen.getByTestId('operational-dashboard'));
    expect(spy).toHaveBeenCalledTimes(1);
    for (const s of [nodes, accounts, orders, positions]) expect(s).not.toHaveBeenCalled();
  });

  it('renders node, account, order and position cards from the projection', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary());
    mount();
    await waitFor(() => screen.getByTestId('operational-dashboard'));
    const nodeCard = screen.getByTestId('node-card').textContent ?? '';
    expect(nodeCard).toContain('node-1');
    // M-NODE-READ-1: the node card carries the NODE's own facts. The masked
    // account fingerprint and the execution mode are gone — the fingerprint
    // fell back to the Mac broker snapshot's account (under the mock adapter,
    // the FIXTURE account, masked), and `observe` was the Control Tower's own
    // governed execution mode, not the node's.
    expect(nodeCard).toContain('vps-dry-run');   // node's deployment profile
    expect(nodeCard).toContain('lux@1.4.2');     // node's engine version
    expect(nodeCard).toContain('dry_run');       // the NODE's mode
    expect(nodeCard).not.toContain('acct…D6F');
    expect(nodeCard).not.toContain('observe');
    const acct = screen.getByTestId('account-card').textContent ?? '';
    expect(acct).toContain('100000.00 USD');
    expect(acct).toContain('not derivable');     // absent field labelled, not zeroed
    expect(screen.getByTestId('orders-table').textContent).toContain('EURUSD');
    expect(screen.getByTestId('positions-table').textContent).toContain('protected');
  });

  it('M-HONESTY-FINAL: rejects mock-fixture records instead of badging them', async () => {
    // This dashboard rendered account BALANCE and EQUITY from the projection
    // with only a MOCK badge — under the mock adapter that is the $100,000
    // fixture figure on an ordinary route (/system). Badging is M-FLEET-1-era
    // treatment; it now applies the same provenance gate as every other surface.
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary({
      nodes: [], accounts: [account({ provenance: 'mock-fixture', balance: 100000 })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('operational-dashboard-unauthoritative'));
    expect(document.body.innerHTML).not.toContain('100000');
    expect(screen.queryByTestId('account-card')).toBeNull();
  });

  it('badges an admitted live_mt5 account as LIVE', async () => {
    // The MOCK half of this test is gone: a mock-fixture account can no longer
    // reach a card at all, so there is nothing left to badge MOCK.
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(
      summary({ accounts: [account({ provenance: 'live_mt5' })] }));
    mount();
    await waitFor(() => screen.getByTestId('account-card'));
    expect(screen.getByTestId('account-card').textContent).toContain('LIVE');
  });

  it('shows a STALE badge when the projection is stale', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(
      summary({ freshness: fresh({ stale: true, status: 'stale', ageSeconds: 7200 }) }));
    mount();
    await waitFor(() => screen.getByTestId('system-health-card'));
    expect(screen.getByTestId('system-health-card').textContent).toContain('STALE');
  });

  it('shows UNAVAILABLE and no invented values when a source could not be read', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary({
      accounts: [account({
        balance: null, equity: null, currency: null,
        freshness: fresh({ available: false, stale: true, status: 'unavailable',
                           ageSeconds: null, detail: 'account snapshot unavailable' }),
      })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('account-unavailable'));
    const card = screen.getByTestId('account-card').textContent ?? '';
    expect(card).toContain('UNAVAILABLE');
    expect(card).toContain('none were read');
    expect(card).not.toContain('0.00');          // no zeroed balance shown
  });

  it('shows RECONCILIATION REQUIRED on affected orders and positions', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary({
      activeOrders: [order({ reconciliation: { required: true } })],
      openPositions: [position({ reconciliation: { required: true, locked: true } })],
    }));
    mount();
    await waitFor(() => screen.getByTestId('positions-table'));
    const badges = screen.getAllByTestId('reconciliation-badge');
    expect(badges.length).toBeGreaterThanOrEqual(2);
    expect(badges[0].textContent).toContain('RECONCILIATION REQUIRED');
    expect(screen.getByTestId('position-locked').textContent).toContain('LOCKED');
  });

  it('renders node warnings and summary warnings explicitly', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary({
      nodes: [node({ warnings: ['node telemetry stale'] })],
      warnings: ['node telemetry stale', 'broker snapshot unavailable'],
    }));
    mount();
    await waitFor(() => screen.getByTestId('warnings-list'));
    expect(screen.getByTestId('node-warnings').textContent).toContain('node telemetry stale');
    expect(screen.getByTestId('warnings-list').textContent).toContain('broker snapshot unavailable');
  });

  it('reports reconciliation issues and in-flight operations', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary({
      reconciliationIssues: [{ class: 'status_mismatch', entityId: 'P1', critical: true,
                               detail: 'not observed', resolved: false }],
      activeOperations: [{ entityRef: 'P1', intentId: 'intent_a',
                           operation: 'ClosePosition', acquiredAt: '2026-07-27T12:00:00Z' }],
    }));
    mount();
    await waitFor(() => screen.getByTestId('reconciliation-list'));
    expect(screen.getByTestId('reconciliation-card').textContent).toContain('CRITICAL');
    expect(screen.getByTestId('reconciliation-list').textContent).toContain('status_mismatch');
    expect(screen.getByTestId('operations-list').textContent).toContain('ClosePosition');
  });

  it('shows nothing operational when the projection cannot be read', async () => {
    vi.spyOn(api, 'operationsSummary').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() => screen.getByTestId('operational-dashboard-error'));
    const err = screen.getByTestId('operational-dashboard-error').textContent ?? '';
    expect(err).toContain('UNAVAILABLE');
    expect(err).toContain('nothing is inferred locally');
    expect(screen.queryByTestId('node-card')).toBeNull();
    expect(screen.queryByTestId('positions-table')).toBeNull();
  });

  it('offers no execution control — the dashboard is read-only', async () => {
    vi.spyOn(api, 'operationsSummary').mockResolvedValue(summary());
    mount();
    await waitFor(() => screen.getByTestId('operational-dashboard'));
    expect(document.querySelectorAll('button').length).toBe(0);
    expect(document.querySelectorAll('input, select').length).toBe(0);
  });
});
