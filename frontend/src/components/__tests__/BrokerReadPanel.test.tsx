/**
 * LIVE-1 — the read-only broker panel must render truth and offer no control.
 *
 *   - it is unmistakably LIVE READ ONLY / NO EXECUTION;
 *   - provenance (live MT5 vs mock fixture) is explicit;
 *   - an unavailable account read is shown as such, never as zeros or health;
 *   - there is NO button, no toggle, no execution control anywhere.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ExecutionStateView, ExecutionBrokerState } from '@/lib/api';
import { api } from '@/lib/api';
import { BrokerReadPanel } from '@/components/domain/BrokerReadPanel';

function broker(over: Partial<ExecutionBrokerState> = {}): ExecutionBrokerState {
  return {
    kind: 'mt5', connection: 'Connected', provenance: 'live_mt5',
    readOnly: true, liveWriteCapable: false,
    account: {
      available: true, login_masked: 'mt5_****0001', broker_company: 'Demo Broker',
      server: 'Demo-Server', currency: 'EUR', balance: 10_000, equity: 10_000,
      margin: 0, margin_level: 0, leverage: 100,
    },
    openPositions: 0, openOrders: 0, recentExecutions: 0,
    reads: { accountSnapshot: 'ok', reconcileSnapshot: 'ok', recentExecutions: 'ok' },
    observedAt: '2026-07-27T00:00:00Z', ...over,
  };
}

function view(b?: ExecutionBrokerState): ExecutionStateView {
  return {
    schemaVersion: 'ct.execution-state.v1', observedAt: '2026-07-27T00:00:00Z',
    broker: b, readiness: { tradingReady: false, gates: {} },
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <BrokerReadPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('BrokerReadPanel', () => {
  it('is unmistakably read-only with no execution', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view(broker()));
    mount();
    expect(screen.getByTestId('broker-read-scope').textContent)
      .toContain('LIVE READ ONLY');
    expect(screen.getByTestId('broker-read-scope').textContent)
      .toContain('NO EXECUTION');
    await waitFor(() => screen.getByTestId('broker-read-body'));
  });

  it('shows a LIVE provenance badge and masked account for a live MT5 read', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view(broker()));
    mount();
    await waitFor(() => screen.getByTestId('broker-read-body'));
    expect(screen.getByTestId('broker-provenance').textContent).toBe('LIVE (MT5)');
    expect(screen.getByTestId('broker-read-body').getAttribute('data-provenance'))
      .toBe('live_mt5');
    expect(screen.getByText('mt5_****0001')).toBeTruthy();
    // never renders a raw numeric login
    expect(document.body.innerHTML).not.toContain('1000001');
  });

  it('labels a mock fixture read as such', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(
      view(broker({ kind: 'mock', provenance: 'mock-fixture' })));
    mount();
    await waitFor(() => screen.getByTestId('broker-read-body'));
    expect(screen.getByTestId('broker-provenance').textContent).toBe('MOCK FIXTURE');
  });

  it('shows an unavailable account read explicitly, inventing no values', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(
      view(broker({ account: { available: false, code: 'unavailable' } })));
    mount();
    await waitFor(() => screen.getByTestId('broker-account-unavailable'));
    const body = screen.getByTestId('broker-read-body');
    expect(body.textContent).toContain('unavailable');
    // no fabricated balance/equity rows
    expect(body.textContent).not.toContain('Balance');
    expect(body.textContent).not.toContain('Equity');
  });

  it('offers no execution controls of any kind', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view(broker()));
    mount();
    await waitFor(() => screen.getByTestId('broker-read-body'));
    expect(document.querySelectorAll('button').length).toBe(0);
    expect(document.querySelectorAll('input, select, [role="button"]').length).toBe(0);
  });

  it('degrades to an explicit message when the backend cannot be reached', async () => {
    vi.spyOn(api, 'executionState').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() => screen.getByTestId('broker-read-error'));
    expect(screen.getByTestId('broker-read-error').textContent)
      .toContain('unavailable');
  });
});
