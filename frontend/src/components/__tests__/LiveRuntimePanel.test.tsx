/**
 * LIVE-5A — the live dashboard renders projected runtime truth and nothing else.
 *
 * Proves: broker / market / execution / runtime cards from ONE query, runtime
 * health badges, unavailable values as "—" (never 0), stale distinguished from
 * unavailable, and no calculation or broker access in the browser.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type {
  LiveBrokerRuntime,
  LiveExecutionRuntime,
  LiveMarketSymbol,
  LiveRuntimeStatus,
  LiveRuntimeView,
} from '@/lib/api';
import { api } from '@/lib/api';
import { LiveRuntimePanel } from '@/components/domain/LiveRuntimePanel';

function runtime(over: Partial<LiveRuntimeStatus> = {}): LiveRuntimeStatus {
  return {
    state: 'CONNECTED', projectionAgeSeconds: 2, brokerAgeSeconds: 3,
    lastTickAt: '2026-07-28T10:00:00Z', lastSuccessAt: '2026-07-28T10:00:00Z',
    tickCount: 12, consecutiveFailures: 0, intervalSeconds: 5, running: true,
    warnings: [], ...over,
  };
}

function broker(over: Partial<LiveBrokerRuntime> = {}): LiveBrokerRuntime {
  return {
    connected: true, connectionState: 'Connected', pingMs: 42, server: 'Demo-1',
    serverTime: '2026-07-28T10:00:00Z', accountFingerprint: 'acct…3456',
    accountCurrency: 'USD', balance: 10000, equity: 10012.5, margin: 120,
    freeMargin: 9892.5, marginLevel: 8343.75, leverage: 100,
    adapterKind: 'mock', executionMode: 'observe',
    lastHeartbeatAt: '2026-07-28T10:00:00Z', heartbeatAgeSeconds: 3,
    availability: 'ok', provenance: 'mock-fixture', freshness: null, ...over,
  };
}

function symbol(over: Partial<LiveMarketSymbol> = {}): LiveMarketSymbol {
  return {
    symbol: 'EURUSD', brokerSymbol: 'EURUSD.r', bid: 1.10045, ask: 1.10055,
    last: 1.1005, spread: 0.0001, quoteAt: '2026-07-28T10:00:00Z',
    quoteAgeSeconds: 2, candleTimeframe: 'M15',
    candleClosedAt: '2026-07-28T10:00:00Z', candleAgeSeconds: 30,
    candleClose: 1.1005, live: true, availability: 'ok',
    candleAvailability: 'ok', provenance: 'mock-fixture', ...over,
  };
}

function execution(over: Partial<LiveExecutionRuntime> = {}): LiveExecutionRuntime {
  return {
    activePositions: 1, pendingOrders: 0, openRecommendations: 2,
    activeScenarios: 3, positionsAvailable: true,
    recommendationsAvailable: true, scenariosAvailable: true, ...over,
  };
}

function view(over: Partial<LiveRuntimeView> = {}): LiveRuntimeView {
  return {
    projectionTimestamp: '2026-07-28T10:00:00Z', runtime: runtime(),
    broker: broker(), symbols: [symbol()], execution: execution(),
    available: true, code: null, warnings: [], ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LiveRuntimePanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('LiveRuntimePanel', () => {
  it('renders every live card from one query', async () => {
    const spy = vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('live-runtime-card'));
    expect(screen.getByTestId('live-broker-card')).toBeTruthy();
    expect(screen.getByTestId('live-market-card')).toBeTruthy();
    expect(screen.getByTestId('live-execution-card')).toBeTruthy();
    // ONE request backs the whole panel.
    expect(spy).toHaveBeenCalledTimes(1);
  });

  it('renders the broker card from projected values', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('live-broker-card'));
    const card = screen.getByTestId('live-broker-card').textContent ?? '';
    expect(screen.getByTestId('broker-connection-badge').textContent).toBe('CONNECTED');
    expect(screen.getByTestId('broker-ping').textContent).toContain('42ms');
    expect(card).toContain('Demo-1');
    expect(card).toContain('acct…3456');
    expect(card).toContain('USD');
    expect(screen.getByTestId('broker-balance').textContent).toContain('10000.00');
    expect(screen.getByTestId('broker-equity').textContent).toContain('10012.50');
    expect(screen.getByTestId('broker-free-margin').textContent).toContain('9892.50');
  });

  it('renders the market card with bid, ask, spread and ages', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('live-market-row'));
    const row = screen.getByTestId('live-market-row').textContent ?? '';
    expect(row).toContain('EURUSD');
    expect(row).toContain('1.10045');      // bid, projected precision
    expect(row).toContain('1.10055');      // ask
    expect(row).toContain('0.00010');      // spread — projected, not computed
    expect(row).toContain('LIVE');
  });

  it('renders the runtime card with health and cadence', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('runtime-state-badge'));
    expect(screen.getByTestId('runtime-state-badge').textContent).toBe('CONNECTED');
    expect(screen.getByTestId('runtime-projection-age').textContent).toContain('2s');
    expect(screen.getByTestId('runtime-broker-age').textContent).toContain('3s');
    const card = screen.getByTestId('live-runtime-card').textContent ?? '';
    expect(card).toContain('5s');          // refresh interval
    expect(card).toContain('12');          // tick count
  });

  it.each([
    ['DEGRADED'], ['STALE'], ['RECONNECTING'], ['STARTING'], ['STOPPED'],
    ['UNKNOWN'],
  ])('badges the %s runtime state', async (state) => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(
      view({ runtime: runtime({ state }) }));
    mount();
    await waitFor(() => screen.getByTestId('runtime-state-badge'));
    expect(screen.getByTestId('runtime-state-badge').textContent).toBe(state);
  });

  it('flags a stopped refresh loop explicitly', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(
      view({ runtime: runtime({ state: 'STOPPED', running: false }) }));
    mount();
    await waitFor(() => screen.getByTestId('runtime-loop-stopped'));
    expect(screen.getByTestId('runtime-loop-stopped').textContent)
      .toContain('LOOP STOPPED');
  });

  it('surfaces runtime warnings verbatim', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(
      view({ runtime: runtime({ warnings: ['broker_not_connected:Disconnected'] }) }));
    mount();
    await waitFor(() => screen.getByTestId('runtime-warnings'));
    expect(screen.getByTestId('runtime-warnings').textContent)
      .toContain('broker_not_connected:Disconnected');
  });

  it('renders unavailable broker figures as dashes, never zero', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view({
      broker: broker({
        connected: false, connectionState: 'Disconnected', pingMs: null,
        server: null, balance: null, equity: null, margin: null,
        freeMargin: null, marginLevel: null, leverage: null,
        accountCurrency: null, heartbeatAgeSeconds: null,
        availability: 'unavailable',
      }),
    }));
    mount();
    await waitFor(() => screen.getByTestId('live-broker-card'));
    const card = screen.getByTestId('live-broker-card').textContent ?? '';
    expect(screen.getByTestId('broker-connection-badge').textContent)
      .toBe('DISCONNECTED');
    expect(card).toContain('—');
    expect(card).not.toContain('0.00');
    expect(screen.getByTestId('broker-balance').textContent).toContain('—');
  });

  it('distinguishes a stale quote from an unavailable one', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view({
      symbols: [
        symbol({ symbol: 'EURUSD', availability: 'stale', live: true }),
        symbol({ symbol: 'GBPUSD', availability: 'unavailable', live: false,
                 bid: null, ask: null, spread: null, quoteAgeSeconds: null }),
      ],
    }));
    mount();
    await waitFor(() => screen.getAllByTestId('live-market-row'));
    const rows = screen.getAllByTestId('live-market-row');
    expect(rows[0].textContent).toContain('STALE');
    expect(rows[1].textContent).toContain('UNAVAILABLE');
    expect(rows[1].textContent).toContain('—');
    expect(rows[1].textContent).not.toContain('0.00000');
  });

  it('renders execution counts and marks unavailable sources', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('exec-positions'));
    expect(screen.getByTestId('exec-positions').textContent).toContain('1');
    expect(screen.getByTestId('exec-recommendations').textContent).toContain('2');
    expect(screen.getByTestId('exec-scenarios').textContent).toContain('3');

    cleanup();
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view({
      execution: execution({ positionsAvailable: false,
                             recommendationsAvailable: false,
                             scenariosAvailable: false }),
    }));
    mount();
    await waitFor(() => screen.getByTestId('exec-positions'));
    expect(screen.getByTestId('exec-positions').textContent).toContain('—');
    expect(screen.getByTestId('exec-scenarios').textContent).toContain('—');
  });

  it('explains a runtime that has not started', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view({
      available: false, code: 'runtime_not_started',
      runtime: runtime({ state: 'STARTING', running: false, tickCount: 0 }),
    }));
    mount();
    await waitFor(() => screen.getByTestId('live-runtime-unavailable'));
    const text = screen.getByTestId('live-runtime-unavailable').textContent ?? '';
    expect(text).toContain('runtime_not_started');
    expect(text).toContain('no tick yet');
    expect(screen.queryByTestId('live-broker-card')).toBeNull();
  });

  it('distinguishes an unreadable projection from an idle market', async () => {
    vi.spyOn(api, 'liveRuntime').mockRejectedValue(new Error('boom'));
    mount();
    await waitFor(() => screen.getByTestId('live-runtime-error'));
    const text = screen.getByTestId('live-runtime-error').textContent ?? '';
    expect(text).toContain('UNAVAILABLE');
    expect(text).toContain('not a claim that the market or broker are idle');
  });

  it('is read-only and reads no broker itself', async () => {
    vi.spyOn(api, 'liveRuntime').mockResolvedValue(view());
    const { container } = mount();
    await waitFor(() => screen.getByTestId('live-runtime-card'));
    expect(container.querySelectorAll('button')).toHaveLength(0);
    expect(container.querySelectorAll('input')).toHaveLength(0);
    expect(container.querySelectorAll('form')).toHaveLength(0);
    expect(screen.getByTestId('live-runtime-scope').textContent)
      .toContain('NO BROKER READS IN THE BROWSER');
    // The client exposes no broker or MT5 access for this panel to reach.
    for (const forbidden of ['readMt5', 'brokerTick', 'mt5Quote',
                             'subscribeSymbol']) {
      expect((api as unknown as Record<string, unknown>)[forbidden])
        .toBeUndefined();
    }
  });
});
