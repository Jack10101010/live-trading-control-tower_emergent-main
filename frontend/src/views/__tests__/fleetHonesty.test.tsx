/**
 * M-FLEET-1 — Fleet Overview and Accounts & Protection tell the truth.
 *
 * These render the real components against a mocked `useFleet` /
 * `useAccountsProtection`, because the properties under test are about what the
 * OPERATOR sees: whether an empty set becomes a zero, whether a fixture record
 * can pass for live, and whether "no source" can pass for "no deployments".
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

const fleetState = vi.hoisted(() => ({ current: {} as Record<string, unknown> }));

vi.mock('@/hooks/useRepository', () => ({
  useFleet: () => fleetState.current,
  useAccountsProtection: () => fleetState.current,
  useRepository: () => ({ world: { marketStateSnapshots: [] } }),
}));

import { FleetOverview } from '@/views/FleetOverview';
import { AccountsProtectionView } from '@/views/AccountsProtectionView';

const ACCOUNT = {
  accountId: 'acct_TEST_0001', brokerId: 'brk_1', type: 'funded' as const,
  baseCurrency: 'USD', timezone: 'UTC', balance: 100000, equity: 100412, fundedRules: null,
};

const DEPLOYMENT = {
  deploymentId: 'dpl_TEST_0001', packageHash: 'sha256:abc', accountId: 'acct_TEST_0001',
  pair: 'EURUSD', lane: 'live' as const, status: 'InTrade' as const,
  executionMode: 'live' as const, liveEnabled: true, lastAction: 'entered long',
  riskState: { dailyPl: 412, floatingPl: 96.4, riskTodayPct: 1, ddBufferPct: 72, openOrders: 1, openTrades: 1 },
  pinnedInFlight: {},
};

const BROKER = {
  brokerId: 'brk_1', adapterType: 'mt5', venue: 'FTMO', status: 'connected' as const,
  lastHeartbeat: '2026-01-01T00:00:00Z', lastReconcileAt: '2026-01-01T00:00:00Z',
  capabilities: { trailingStop: true, minLot: 0.01, lotStep: 0.01 },
};

const PACKAGE = { label: 'P1', version: '1.0.0', packageHash: 'sha256:abc' };

function setFleet(over: Record<string, unknown>) {
  fleetState.current = {
    deployments: [], brokers: [], accounts: [], pairs: [],
    activePackage: PACKAGE, asOf: '2026-01-01T00:00:00Z',
    available: true, provenance: 'fixture', provenanceDetail: 'fixture detail text',
    ...over,
  };
}

const renderFleet = () =>
  render(<MemoryRouter><FleetOverview /></MemoryRouter>);

beforeEach(() => setFleet({}));

describe('Fleet Overview — no source vs empty set', () => {
  it('says the source is unavailable rather than showing an empty fleet', () => {
    setFleet({ available: false, provenance: 'unavailable', provenanceDetail: 'no production source' });
    renderFleet();
    // Stated in BOTH the summary line and the grid's empty state — an operator
    // must not have to scroll to learn the fleet has no source.
    expect(screen.getAllByText(/Fleet composition unavailable/i).length).toBe(2);
    expect(screen.getAllByText(/no production source/i).length).toBeGreaterThan(0);
    // Crucially it must NOT claim a count — "0 deployments" asserts an empty fleet.
    expect(screen.getByTestId('fleet-summary').textContent).not.toMatch(/\d/);
    expect(screen.queryByText(/No deployments/i)).toBeNull();
  });

  it('renders an honest empty state when the source genuinely returns nothing', () => {
    setFleet({ deployments: [] });
    renderFleet();
    expect(screen.getByText(/^No deployments$/i)).toBeTruthy();
    expect(screen.getByText(/real empty fleet, not missing data/i)).toBeTruthy();
  });
});

describe('Fleet Overview — records and counts', () => {
  it('renders exactly the records supplied, and counts that match them', () => {
    setFleet({ deployments: [DEPLOYMENT], brokers: [BROKER], accounts: [ACCOUNT] });
    renderFleet();
    expect(screen.getByTestId(`deployment-card-${DEPLOYMENT.deploymentId}`)).toBeTruthy();
    const summary = screen.getByTestId('fleet-summary').textContent ?? '';
    expect(summary).toMatch(/1 fixture deployment\b/);
    expect(summary).toMatch(/1 broker\b/);
    expect(summary).toMatch(/1 account\b/);
  });

  it('summary counts track the supplied records, never a hard-coded 3/2/2', () => {
    setFleet({
      deployments: [DEPLOYMENT, { ...DEPLOYMENT, deploymentId: 'dpl_TEST_0002' }],
      brokers: [BROKER], accounts: [ACCOUNT],
    });
    renderFleet();
    const summary = screen.getByTestId('fleet-summary').textContent ?? '';
    expect(summary).toMatch(/2 fixture deployments/);
    expect(summary).not.toMatch(/3 /);
  });
});

describe('Fleet Overview — fixture records cannot pass for live', () => {
  it('tags every fixture deployment and banners the page', () => {
    setFleet({ deployments: [DEPLOYMENT], brokers: [BROKER], accounts: [ACCOUNT] });
    renderFleet();
    expect(screen.getByTestId(`deployment-fixture-tag-${DEPLOYMENT.deploymentId}`).textContent)
      .toMatch(/FIXTURE/);
    expect(screen.getByTestId('fleet-provenance-banner').textContent).toMatch(/not live/i);
  });

  it('never paints a fixture deployment with the live mode colour', () => {
    setFleet({ deployments: [DEPLOYMENT], brokers: [BROKER], accounts: [ACCOUNT] });
    const { container } = renderFleet();
    // The record is executionMode "live"; as fixture data it must not be
    // rendered in the live colour, which is what makes it read as operational.
    expect(container.innerHTML).not.toContain('var(--mode-live)');
    expect(container.innerHTML).toContain('var(--mode-mock)');
  });
});

describe('Accounts & Protection — missing values never become zero', () => {
  it('shows "not derivable" rather than $0.00 and a full drawdown buffer', () => {
    // An account with NO deployments: P/L and buffer are unknown, not zero/100%.
    setFleet({ accounts: [ACCOUNT], deployments: [] });
    render(<AccountsProtectionView />);
    const markers = screen.getAllByTestId('aggregate-not-derivable');
    expect(markers.length).toBe(4);              // daily P/L, floating, risk, DD buffer
    const html = document.body.innerHTML;
    expect(html).not.toMatch(/\$0\.00/);
    expect(html).not.toMatch(/100\s*%/);         // the invented full buffer is gone
    expect(markers[0].getAttribute('title')).toMatch(/unknown, not zero/i);
  });

  it('does derive the aggregates when deployments genuinely exist', () => {
    setFleet({ accounts: [ACCOUNT], deployments: [DEPLOYMENT] });
    render(<AccountsProtectionView />);
    expect(screen.queryByTestId('aggregate-not-derivable')).toBeNull();
  });
});

describe('Accounts & Protection — sources and empty states', () => {
  it('renders an unavailable state that invents no balance', () => {
    setFleet({ available: false, provenance: 'unavailable', accounts: [], deployments: [],
               provenanceDetail: 'no production source' });
    render(<AccountsProtectionView />);
    expect(screen.getByText(/Accounts unavailable/i)).toBeTruthy();
    expect(document.body.innerHTML).not.toMatch(/\$[\d,]/);   // no figure of any kind
  });

  it('renders an honest empty state when there are genuinely no accounts', () => {
    setFleet({ accounts: [], deployments: [] });
    render(<AccountsProtectionView />);
    expect(screen.getByText(/^No accounts$/i)).toBeTruthy();
    expect(screen.getByText(/genuine empty result, not missing data/i)).toBeTruthy();
  });

  it('labels fixture accounts and does not paint them live', () => {
    setFleet({ accounts: [ACCOUNT], deployments: [DEPLOYMENT] });
    const { container } = render(<AccountsProtectionView />);
    expect(screen.getByTestId(`account-fixture-tag-${ACCOUNT.accountId}`).textContent)
      .toMatch(/FIXTURE/);
    expect(within(screen.getByTestId(`account-fixture-note-${ACCOUNT.accountId}`)).getByText(
      /development fixture values/i)).toBeTruthy();
    expect(container.innerHTML).not.toContain('var(--mode-live)');
  });

  it('renders exactly the accounts supplied', () => {
    setFleet({
      accounts: [ACCOUNT, { ...ACCOUNT, accountId: 'acct_TEST_0002', type: 'demo' as const }],
      deployments: [DEPLOYMENT],
    });
    render(<AccountsProtectionView />);
    expect(screen.getByTestId('account-fixture-tag-acct_TEST_0001')).toBeTruthy();
    expect(screen.getByTestId('account-fixture-tag-acct_TEST_0002')).toBeTruthy();
  });
});
