/**
 * LIVE-2 — the minimal execution surface must be gated, confirmed and honest.
 *
 *   - the ONE control is DISABLED unless every backend readiness gate passes;
 *   - nothing is sent without an explicit confirmation step;
 *   - it is unmistakably labelled LIVE EXECUTION / ONE MARKET ORDER / NO AUTOMATION;
 *   - the broker acknowledgement, ticket, lifecycle state, failure and latency
 *     are rendered from the backend response, never invented;
 *   - no OTHER execution control exists in the panel.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ExecutionStateView, MarketOrderResponse } from '@/lib/api';
import { api, MarketOrderError } from '@/lib/api';
import { MarketOrderPanel } from '@/components/domain/MarketOrderPanel';

function view(over: Partial<ExecutionStateView> = {}): ExecutionStateView {
  return {
    schemaVersion: 'ct.execution-telemetry.v1',
    observedAt: '2026-07-27T12:00:00Z',
    readiness: { tradingReady: false, gates: { liveAdapterActive: false, adapterConnected: true } },
    ...over,
  };
}

function readyView(): ExecutionStateView {
  return view({ readiness: { tradingReady: true, gates: { liveAdapterActive: true } } });
}

function response(over: Partial<MarketOrderResponse> = {}): MarketOrderResponse {
  return {
    ok: true, commandId: 'cmd_1', intentId: 'intent_abc', lifecycleState: 'open',
    brokerTicket: '990001', acknowledgement: { status: 'filled' }, deduplicated: false,
    latencyMs: 12.5, timeline: { brokerMs: 12.5 }, acceptedAt: '2026-07-27T12:00:01Z',
    ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MarketOrderPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('MarketOrderPanel', () => {
  it('is unmistakably labelled as live execution with no automation', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    mount();
    const scope = screen.getByTestId('market-order-scope').textContent ?? '';
    expect(scope).toContain('LIVE EXECUTION');
    expect(scope).toContain('ONE MARKET ORDER');
    expect(scope).toContain('NO AUTOMATION');
  });

  it('disables the control unless every readiness gate passes', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    mount();
    await waitFor(() => screen.getByTestId('market-order-failing-gates'));
    const btn = screen.getByTestId('market-order-submit') as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    expect(screen.getByTestId('market-order-failing-gates').textContent)
      .toContain('liveAdapterActive');
  });

  it('requires explicit confirmation before anything is sent', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(readyView());
    const submit = vi.spyOn(api, 'submitMarketOrder').mockResolvedValue(response());
    mount();
    await waitFor(() => {
      expect((screen.getByTestId('market-order-submit') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('market-order-submit'));
    expect(submit).not.toHaveBeenCalled();            // nothing sent yet
    expect(screen.getByTestId('market-order-confirm-dialog')).toBeTruthy();
    fireEvent.click(screen.getByTestId('market-order-confirm'));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(1));
  });

  it('cancelling the confirmation sends nothing', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(readyView());
    const submit = vi.spyOn(api, 'submitMarketOrder').mockResolvedValue(response());
    mount();
    await waitFor(() => {
      expect((screen.getByTestId('market-order-submit') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('market-order-submit'));
    fireEvent.click(screen.getByTestId('market-order-cancel'));
    expect(submit).not.toHaveBeenCalled();
    expect(screen.queryByTestId('market-order-confirm-dialog')).toBeNull();
  });

  it('renders acknowledgement, ticket, lifecycle and latency from the response', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(readyView());
    vi.spyOn(api, 'submitMarketOrder').mockResolvedValue(response());
    mount();
    await waitFor(() => {
      expect((screen.getByTestId('market-order-submit') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('market-order-submit'));
    fireEvent.click(screen.getByTestId('market-order-confirm'));
    await waitFor(() => screen.getByTestId('market-order-result'));
    const result = screen.getByTestId('market-order-result').textContent ?? '';
    expect(result).toContain('open');
    expect(result).toContain('990001');
    expect(result).toContain('filled');
    expect(result).toContain('12.5 ms');
  });

  it('shows a denial with its stage and machine code', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(readyView());
    vi.spyOn(api, 'submitMarketOrder').mockRejectedValue(
      new MarketOrderError('execution_mode_not_active', 'safety', 'mode is observe'));
    mount();
    await waitFor(() => {
      expect((screen.getByTestId('market-order-submit') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('market-order-submit'));
    fireEvent.click(screen.getByTestId('market-order-confirm'));
    await waitFor(() => screen.getByTestId('market-order-denial'));
    const denial = screen.getByTestId('market-order-denial').textContent ?? '';
    expect(denial).toContain('safety');
    expect(denial).toContain('execution_mode_not_active');
  });

  it('labels a deduplicated replay explicitly', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(readyView());
    vi.spyOn(api, 'submitMarketOrder').mockResolvedValue(
      response({ deduplicated: true }));
    mount();
    await waitFor(() => {
      expect((screen.getByTestId('market-order-submit') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('market-order-submit'));
    fireEvent.click(screen.getByTestId('market-order-confirm'));
    await waitFor(() => screen.getByTestId('market-order-deduplicated'));
    expect(screen.getByTestId('market-order-deduplicated').textContent)
      .toContain('no second broker order');
  });

  it('offers exactly one execution control and nothing else', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    mount();
    const buttons = Array.from(document.querySelectorAll('button'));
    expect(buttons.length).toBe(1);                    // Submit Market Order only
    const labels = buttons.map((b) => (b.textContent ?? '').toLowerCase());
    for (const banned of ['kill', 'close', 'pause', 'resume', 'cancel order',
                          'modify', 'flatten', 'pending', 'limit', 'stop order']) {
      expect(labels.some((l) => l.includes(banned))).toBe(false);
    }
  });
});
