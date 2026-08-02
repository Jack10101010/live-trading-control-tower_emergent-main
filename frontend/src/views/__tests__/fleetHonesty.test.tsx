/**
 * M-FLEET-2 — the ordinary operator surfaces render authoritative records only.
 *
 * Supersedes the M-FLEET-1 suite, which asserted that fixture records were
 * correctly LABELLED. Labelling is no longer the contract: the fixture records
 * must not reach these surfaces at all.
 *
 * The decisive case is `mock-fixture`. Those records arrive from
 * `/api/operations/*` — an endpoint that looks authoritative — carrying the same
 * invented balance the fixture serves. If any of these tests can be made to pass
 * by rendering them, the milestone has failed.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const state = vi.hoisted(() => ({ fleet: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useOperationalFleet: () => state.fleet,
  useAccountsProtection: () => state.fleet,
  useConfiguredInstruments: () => ({ symbols: ['EURUSD', 'GBPUSD', 'XAUUSD'], configured: true }),
}));

import { FleetOverview } from '@/views/FleetOverview';
import { AccountsProtectionView } from '@/views/AccountsProtectionView';

/**
 * M-NODE-READ-1 — a node record as the projection now emits it.
 *
 * It previously carried `provenance: 'live_mt5'`, `adapter: 'mt5'`,
 * `broker: 'RealBroker'`, `connectionState`, `executionMode` and
 * `reconciliationState` — a shape the node projection has never produced. Those
 * fields were the Control Tower's own state, and the fixture encoded them as
 * though the node had reported them.
 */
const LIVE_NODE = {
  nodeId: 'node-real-1', provenance: 'node-telemetry',
  lifecycleState: 'current' as const, degradedReasons: [] as string[],
  deploymentProfile: 'vps-dry-run', nodeMode: 'dry_run', cycleStatus: 'ok',
  lastBoundary: '2026-07-27T11:45:00Z', lastBarTime: null,
  engineVersion: 'lux@1.4.2', symbol: 'EURUSD', timeframe: 'M15',
  killSwitchActive: false, submissionDisabled: true,
  openPositionCount: 2, openOrderCount: null,
  livenessAgeSeconds: 12, freshnessBasis: 'received_at',
  mt5Observation: null, legacySource: false, warnings: [] as string[],
  freshness: { available: true, stale: false },
};

const LIVE_ACCOUNT = {
  accountFingerprint: 'fp_real_abc', broker: 'RealBroker', currency: 'USD',
  balance: 4211.5, equity: 4180.25, realizedPnLToday: null, unrealizedPnL: null,
  openRisk: null, margin: null, marginLevel: null, leverage: null,
  connectionState: 'Connected', provenance: 'live_mt5',
};

function setFleet(over: Record<string, unknown>) {
  // M-NODE-READ-1: node and broker admission are separate statuses, so the
  // hook fixture carries both. A test that sets only one is exercising a state
  // the hook cannot produce.
  state.fleet = {
    nodes: [], accounts: [], deployments: [],
    status: 'unavailable', detail: 'no authoritative source',
    nodeStatus: 'absent', nodeDetail: 'No execution node has published telemetry',
    ...over,
  };
}

const renderFleet = () => render(<MemoryRouter><FleetOverview /></MemoryRouter>);

beforeEach(() => setFleet({}));

describe('Fleet Overview — the fixture fleet is gone', () => {
  it('shows an unobserved-node state under the mock adapter, with no counts', () => {
    setFleet({ status: 'unavailable', nodeStatus: 'absent' });
    renderFleet();
    expect(screen.getAllByText(/No execution node observed/i).length).toBeGreaterThan(0);
    // "0 nodes" would assert an empty fleet; nothing has been observed to count.
    expect(screen.getByTestId('fleet-summary').textContent).not.toMatch(/\d/);
  });

  it('renders none of the old fabricated fleet values', () => {
    setFleet({ status: 'unavailable' });
    const { container } = renderFleet();
    const html = container.innerHTML;
    for (const gone of ['412', '100,000', '100000', 'InTrade', 'Armed', 'Waiting',
                        'ghost', 'experimental', 'FIXTURE']) {
      expect(html, `still renders ${gone}`).not.toContain(gone);
    }
    expect(html).not.toMatch(/\$\s?[\d,]/);
  });

  it('separates an unavailable ACCOUNT source from an absent NODE', () => {
    // The decisive case for M-NODE-READ-1: a node IS reporting and the account
    // source is not. Both facts are stated; neither erases the other.
    setFleet({
      status: 'unavailable', nodeStatus: 'current',
      nodes: [LIVE_NODE], accounts: [],
    });
    renderFleet();
    const summary = screen.getByTestId('fleet-summary').textContent!;
    expect(summary).toMatch(/1 node reporting/);
    expect(summary).toMatch(/account source unavailable/);
    // The node card is present — the node did not vanish with the account.
    expect(screen.getByText('node-real-1')).toBeTruthy();
  });

  it('renders genuine nodes exactly as supplied', () => {
    setFleet({
      status: 'available', nodeStatus: 'current',
      nodes: [LIVE_NODE], accounts: [LIVE_ACCOUNT],
    });
    renderFleet();
    expect(screen.getByText('node-real-1')).toBeTruthy();
    expect(screen.getByText('vps-dry-run')).toBeTruthy();
    expect(screen.getByText('lux@1.4.2')).toBeTruthy();
    expect(screen.getByTestId('fleet-summary').textContent).toMatch(/1 node reporting/);
    expect(screen.getByTestId('fleet-summary').textContent).toMatch(/1 account/);
  });

  it('renders "—" for fields the node did not report, never a zero', () => {
    setFleet({
      status: 'unavailable', nodeStatus: 'current',
      nodes: [{ ...LIVE_NODE, engineVersion: null, lastBoundary: null, cycleStatus: null }],
      accounts: [],
    });
    const { container } = renderFleet();
    expect(container.textContent).toContain('—');
    // openPositionCount 2 is a real measurement and must still show.
    expect(container.textContent).toContain('2');
  });

  it('marks a stale node as last-reported rather than current', () => {
    setFleet({
      status: 'unavailable', nodeStatus: 'stale',
      nodes: [{ ...LIVE_NODE, lifecycleState: 'stale',
                warnings: ['node telemetry stale — last reported, not current'] }],
      accounts: [],
    });
    renderFleet();
    expect(screen.getByTestId('fleet-summary').textContent).toMatch(/last reported, not current/);
    // Still a LIVE card: stale is real data that is old, not fixture data.
    expect(screen.getByTestId('node-lifecycle-node-real-1').textContent).toBe('stale');
  });
});

describe('Accounts & Protection — the fixture accounts are gone', () => {
  it('reports no authoritative source instead of fixture balances', () => {
    setFleet({ status: 'unavailable', detail: 'no authoritative source', accounts: [] });
    const { container } = render(<AccountsProtectionView />);
    expect(screen.getByText(/No authoritative account source/i)).toBeTruthy();
    expect(container.innerHTML).not.toContain('100000');
    expect(container.innerHTML).not.toContain('100412');
    expect(container.innerHTML).not.toMatch(/\$\s?[\d,]/);
    expect(container.innerHTML).not.toContain('funded');
  });

  it('distinguishes a genuine empty account list', () => {
    setFleet({ status: 'empty', accounts: [] });
    render(<AccountsProtectionView />);
    expect(screen.getByText(/No accounts reported/i)).toBeTruthy();
  });

  it('renders authoritative accounts exactly as supplied', () => {
    setFleet({ status: 'available', accounts: [LIVE_ACCOUNT] });
    render(<AccountsProtectionView />);
    expect(screen.getByTestId('account-live-badge').textContent).toMatch(/Connected/);
    expect(document.body.textContent).toContain('RealBroker');
  });

  it('never renders zero for an unreported figure', () => {
    setFleet({ status: 'available', accounts: [LIVE_ACCOUNT] });
    render(<AccountsProtectionView />);
    // realizedPnLToday / unrealizedPnL / openRisk / margin / marginLevel / leverage,
    // plus the three M-MT5-READ-1 fields this fixture does not carry at all
    // (freeMargin / tradeAllowed / tradeExpert). A field the record simply
    // OMITS is as unreported as one explicitly null, and must read the same.
    const markers = screen.getAllByTestId('value-not-reported');
    expect(markers.length).toBe(9);
    expect(markers[0].getAttribute('title')).toMatch(/Unknown, not zero/i);
    expect(document.body.innerHTML).not.toMatch(/\$0\.00/);
  });

  it('keeps the M-RISK-1 funded-rule unavailable state', () => {
    setFleet({ status: 'available', accounts: [LIVE_ACCOUNT] });
    render(<AccountsProtectionView />);
    expect(screen.getByTestId('funded-rules-unavailable')).toBeTruthy();
  });
});
