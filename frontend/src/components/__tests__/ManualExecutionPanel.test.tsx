/**
 * LIVE-3 — the manual-execution surface must be governed, confirmed and honest.
 *
 *   - unmistakable LIVE MANUAL / NO AUTOMATION / LOCAL LOOPBACK ONLY labels;
 *   - every mutation is disabled until its exact gates pass (observe = nothing);
 *   - a confirmation dialog precedes every mutation; cancel sends nothing;
 *   - acknowledgement is rendered DISTINCTLY from the reconciled result;
 *   - no bulk/flatten/pending-order/autonomous control exists;
 *   - no browser storage is touched.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ExecutionStateView, ExecutionModeView, EntityOperationResponse } from '@/lib/api';
import { api } from '@/lib/api';
import { ManualExecutionPanel } from '@/components/domain/ManualExecutionPanel';

function view(mode = 'observe', over: Partial<ExecutionStateView> = {}): ExecutionStateView {
  return {
    schemaVersion: 'ct.execution-telemetry.v1',
    observedAt: '2026-07-27T12:00:00Z',
    readiness: { tradingReady: false, gates: {} },
    governance: {
      executionMode: mode,
      modeProvenance: 'durable-store',
      authorization: { active: false },
      operations: { available: true, entityLocks: [], confirmed: 0 },
      provenance: 'mock-fixture',
    },
    ...over,
  };
}

function modeView(gates: Record<string, boolean> = { g: false }): ExecutionModeView {
  return { mode: 'observe', history: [], manualLiveGates: gates,
           observedAt: '2026-07-27T12:00:00Z' };
}

function opResponse(over: Partial<EntityOperationResponse> = {}): EntityOperationResponse {
  return {
    ok: true, commandId: 'cmd_1', intentId: 'intent_1', lifecycleState: 'acknowledged',
    brokerRef: 'mockop_abc', acknowledgement: { status: 'acknowledged' },
    reconciliationConfirmed: false, deduplicated: false, latencyMs: 3.2,
    acceptedAt: '2026-07-27T12:00:01Z', ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ManualExecutionPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('ManualExecutionPanel', () => {
  it('carries the required labels', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView());
    mount();
    const scope = screen.getByTestId('manual-execution-scope').textContent ?? '';
    expect(scope).toContain('LIVE MANUAL');
    expect(scope).toContain('NO AUTOMATION');
    expect(scope).toContain('LOCAL LOOPBACK ONLY');
  });

  it('disables every mutation in observe mode and manual_live when gates fail', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view('observe'));
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView({ nodeFresh: false }));
    mount();
    await waitFor(() => screen.getByTestId('manual-live-blocked'));
    fireEvent.change(screen.getByTestId('op-position-ref'), { target: { value: 'p1' } });
    fireEvent.change(screen.getByTestId('op-sl'), { target: { value: '1.1' } });
    expect((screen.getByTestId('op-modify') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('op-close') as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId('mode-manual_live') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('manual-live-blocked').textContent).toContain('nodeFresh');
  });

  it('requires confirmation before a mutation and cancel sends nothing', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view('manual_live'));
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView({ g: true }));
    const modify = vi.spyOn(api, 'modifyProtection').mockResolvedValue(opResponse());
    mount();
    await waitFor(() => screen.getByTestId('op-modify'));
    fireEvent.change(screen.getByTestId('op-position-ref'), { target: { value: 'p1' } });
    fireEvent.change(screen.getByTestId('op-sl'), { target: { value: '1.1' } });
    await waitFor(() => {
      expect((screen.getByTestId('op-modify') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('op-modify'));
    expect(modify).not.toHaveBeenCalled();               // dialog first
    expect(screen.getByTestId('manual-confirm-dialog')).toBeTruthy();
    fireEvent.click(screen.getByTestId('manual-cancel'));
    expect(modify).not.toHaveBeenCalled();               // cancelled -> nothing sent
    fireEvent.click(screen.getByTestId('op-modify'));
    fireEvent.click(screen.getByTestId('manual-confirm'));
    await waitFor(() => expect(modify).toHaveBeenCalledTimes(1));
  });

  it('renders acknowledgement DISTINCT from the reconciled result', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view('manual_live'));
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView({ g: true }));
    vi.spyOn(api, 'closePosition').mockResolvedValue(opResponse());
    mount();
    await waitFor(() => screen.getByTestId('op-close'));
    fireEvent.change(screen.getByTestId('op-position-ref'), { target: { value: 'p1' } });
    await waitFor(() => {
      expect((screen.getByTestId('op-close') as HTMLButtonElement).disabled).toBe(false);
    });
    fireEvent.click(screen.getByTestId('op-close'));
    fireEvent.click(screen.getByTestId('manual-confirm'));
    await waitFor(() => screen.getByTestId('manual-result'));
    const result = screen.getByTestId('manual-result').textContent ?? '';
    expect(result).toContain('acknowledged');
    expect(result).toContain('NOT YET — awaiting reconciliation');
  });

  it('offers no bulk, flatten, pending-order or autonomous control', async () => {
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView());
    mount();
    const labels = Array.from(document.querySelectorAll('button'))
      .map((b) => (b.textContent ?? '').toLowerCase());
    for (const banned of ['flatten', 'close all', 'bulk', 'kill', 'place order',
                          'pending order', 'limit', 'auto']) {
      expect(labels.some((l) => l.includes(banned))).toBe(false);
    }
  });

  it('touches no browser storage', async () => {
    const setItem = vi.spyOn(Storage.prototype, 'setItem');
    vi.spyOn(api, 'executionState').mockResolvedValue(view());
    vi.spyOn(api, 'executionMode').mockResolvedValue(modeView());
    mount();
    await waitFor(() => screen.getByTestId('manual-execution-panel'));
    expect(setItem).not.toHaveBeenCalled();
  });
});
