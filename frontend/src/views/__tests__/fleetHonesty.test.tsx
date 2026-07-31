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

const LIVE_NODE = {
  nodeId: 'node-real-1', adapter: 'mt5', broker: 'RealBroker', connectionState: 'Connected',
  health: 'healthy', executionMode: 'observe', reconciliationState: 'clean',
  openPositionCount: 2, openOrderCount: 0, provenance: 'live_mt5',
};

const LIVE_ACCOUNT = {
  accountFingerprint: 'fp_real_abc', broker: 'RealBroker', currency: 'USD',
  balance: 4211.5, equity: 4180.25, realizedPnLToday: null, unrealizedPnL: null,
  openRisk: null, margin: null, marginLevel: null, leverage: null,
  connectionState: 'Connected', provenance: 'live_mt5',
};

function setFleet(over: Record<string, unknown>) {
  state.fleet = { nodes: [], accounts: [], deployments: [], status: 'unavailable', detail: 'no authoritative source', ...over };
}

const renderFleet = () => render(<MemoryRouter><FleetOverview /></MemoryRouter>);

beforeEach(() => setFleet({}));

describe('Fleet Overview — the fixture fleet is gone', () => {
  it('shows an unavailable state under the mock adapter, with no counts', () => {
    setFleet({ status: 'unavailable', detail: 'no authoritative operational source' });
    renderFleet();
    expect(screen.getAllByText(/No authoritative operational source/i).length).toBeGreaterThan(0);
    // "0 nodes" would assert an empty fleet; there is no fleet view at all.
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

  it('distinguishes a genuine empty result from an unavailable source', () => {
    setFleet({ status: 'empty', nodes: [], accounts: [] });
    renderFleet();
    expect(screen.getByText(/No nodes or accounts reported/i)).toBeTruthy();
    expect(screen.getByText(/genuine empty result, not missing data/i)).toBeTruthy();
  });

  it('renders authoritative nodes exactly as supplied', () => {
    setFleet({ status: 'available', nodes: [LIVE_NODE], accounts: [LIVE_ACCOUNT] });
    renderFleet();
    expect(screen.getByText('node-real-1')).toBeTruthy();
    expect(screen.getByText('RealBroker')).toBeTruthy();
    expect(screen.getByTestId('fleet-summary').textContent).toMatch(/1 node · 1 account/);
  });

  it('renders "—" for fields the source did not report, never a zero', () => {
    setFleet({
      status: 'available',
      nodes: [{ ...LIVE_NODE, broker: null, health: null, openOrderCount: null }],
      accounts: [],
    });
    const { container } = renderFleet();
    expect(container.textContent).toContain('—');
    // openPositionCount 2 is a real measurement and must still show.
    expect(container.textContent).toContain('2');
  });

  it('marks stale records as last-reported rather than current', () => {
    setFleet({ status: 'stale', nodes: [LIVE_NODE], accounts: [] });
    renderFleet();
    expect(screen.getByTestId('fleet-summary').textContent).toMatch(/last reported, not current/);
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
    // realizedPnLToday / unrealizedPnL / openRisk / margin / marginLevel / leverage are null
    const markers = screen.getAllByTestId('value-not-reported');
    expect(markers.length).toBe(6);
    expect(markers[0].getAttribute('title')).toMatch(/Unknown, not zero/i);
    expect(document.body.innerHTML).not.toMatch(/\$0\.00/);
  });

  it('keeps the M-RISK-1 funded-rule unavailable state', () => {
    setFleet({ status: 'available', accounts: [LIVE_ACCOUNT] });
    render(<AccountsProtectionView />);
    expect(screen.getByTestId('funded-rules-unavailable')).toBeTruthy();
  });
});
