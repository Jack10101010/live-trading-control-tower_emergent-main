/**
 * UI-17 — the read-only operator controls panel.
 *
 * The panel exposes exactly three read-only actions and must never present an
 * optimistic success, never fire a duplicate submission, and never handle a token.
 * These tests pin the loading, success, rejection, failure and disabled/unreachable
 * states, plus the negative safety properties.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { api, OperatorCommandError, type OperatorCommandView } from '@/lib/api';
import { OperatorCommandPanel } from '@/components/domain/OperatorCommandPanel';

function view(over: Partial<OperatorCommandView> = {}): OperatorCommandView {
  return {
    schemaVersion: 'ct.command.v1',
    commandId: 'cmd_abc',
    commandType: 'request_health',
    idempotencyKey: 'k-1',
    target: null,
    operatorRef: null,
    requestedAt: '2026-07-26T12:00:00Z',
    expiresAt: '2026-07-26T12:00:30Z',
    state: 'completed',
    createdAt: '2026-07-26T12:00:00Z',
    updatedAt: '2026-07-26T12:00:00Z',
    acknowledged: true,
    accepted: true,
    completed: true,
    outcomeState: 'completed',
    payload: {},
    transport: { reason: 'delivered', detail: null },
    enabled: true,
    ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <OperatorCommandPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

describe('read-only surface', () => {
  it('offers only the three permitted read-only actions and no execution control', () => {
    mount();
    // Exactly three action buttons, and they are exactly the permitted trio.
    const buttons = screen.getAllByRole('button');
    expect(buttons).toHaveLength(3);
    expect(buttons.map((b) => b.getAttribute('data-testid')).sort()).toEqual([
      'operator-btn-noop', 'operator-btn-request_health', 'operator-btn-request_telemetry',
    ]);
    // No button is labelled with an execution verb.
    for (const b of buttons) {
      const label = (b.textContent ?? '').toLowerCase();
      for (const banned of ['pause', 'resume', 'arm', 'order', 'cancel', 'kill', 'flatten']) {
        expect(label.includes(banned)).toBe(false);
      }
    }
    expect(screen.getByTestId('operator-scope').textContent).toBe('READ-ONLY');
  });
});

describe('loading state', () => {
  it('shows an accessible loading state and disables buttons while submitting', async () => {
    let resolve!: (v: OperatorCommandView) => void;
    const pending = new Promise<OperatorCommandView>((r) => { resolve = r; });
    vi.spyOn(api, 'operatorSubmitCommand').mockReturnValue(pending);
    mount();

    fireEvent.click(screen.getByTestId('operator-btn-request_health'));
    expect(await screen.findByTestId('operator-loading')).toBeTruthy();
    // Every button is disabled while a submission is in flight.
    expect((screen.getByTestId('operator-btn-request_telemetry') as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId('operator-btn-request_health').getAttribute('aria-busy')).toBe('true');
    // No optimistic result yet.
    expect(screen.queryByTestId('operator-command-result')).toBeNull();

    resolve(view());
    await waitFor(() => expect(screen.getByTestId('operator-command-result')).toBeTruthy());
  });
});

describe('success state', () => {
  it('shows completed with acknowledgement and completion as distinct facts', async () => {
    vi.spyOn(api, 'operatorSubmitCommand').mockResolvedValue(view());
    mount();
    fireEvent.click(screen.getByTestId('operator-btn-request_health'));
    const result = await screen.findByTestId('operator-command-result');
    expect(result.getAttribute('data-state')).toBe('completed');
    expect(result.textContent).toContain('ack accepted');
    expect(result.textContent).toContain('completed yes');
  });

  it('never presents optimistic success — a pending disabled result is shown as pending', async () => {
    vi.spyOn(api, 'operatorSubmitCommand').mockResolvedValue(
      view({ state: 'pending', enabled: false, acknowledged: false, accepted: null,
              completed: false, outcomeState: null,
              transport: { reason: 'command_transport_disabled', detail: null } })
    );
    mount();
    fireEvent.click(screen.getByTestId('operator-btn-request_telemetry'));
    const result = await screen.findByTestId('operator-command-result');
    expect(result.getAttribute('data-state')).toBe('pending');
    expect(screen.getByTestId('operator-disabled-note')).toBeTruthy();
  });
});

describe('rejection and failure states', () => {
  it('renders a stable rejection code for a 422 rejection', async () => {
    vi.spyOn(api, 'operatorSubmitCommand').mockRejectedValue(
      new OperatorCommandError('already_expired', null)
    );
    mount();
    fireEvent.click(screen.getByTestId('operator-btn-request_health'));
    const rej = await screen.findByTestId('operator-command-rejection');
    expect(rej.getAttribute('role')).toBe('alert');
    expect(rej.textContent).toContain('already_expired');
  });

  it('renders a failure alert when the backend is unreachable', async () => {
    vi.spyOn(api, 'operatorSubmitCommand').mockRejectedValue(new Error('API 500'));
    mount();
    fireEvent.click(screen.getByTestId('operator-btn-request_telemetry'));
    const err = await screen.findByTestId('operator-command-error');
    expect(err.getAttribute('role')).toBe('alert');
    expect(err.textContent).toContain('not sent');
  });

  it('maps a remote rejection state to a REJECTED result', async () => {
    vi.spyOn(api, 'operatorSubmitCommand').mockResolvedValue(
      view({ state: 'rejected', acknowledged: true, accepted: false, completed: false,
             outcomeState: null, transport: { reason: 'remote_rejected', detail: 'HTTP 403' } })
    );
    mount();
    fireEvent.click(screen.getByTestId('operator-btn-request_telemetry'));
    const result = await screen.findByTestId('operator-command-result');
    expect(result.getAttribute('data-state')).toBe('rejected');
    expect(result.textContent).toContain('remote_rejected');
  });
});

describe('duplicate-click prevention', () => {
  it('does not fire a second submission while one is in flight', async () => {
    let resolve!: (v: OperatorCommandView) => void;
    const pending = new Promise<OperatorCommandView>((r) => { resolve = r; });
    const spy = vi.spyOn(api, 'operatorSubmitCommand').mockReturnValue(pending);
    mount();
    const btn = screen.getByTestId('operator-btn-request_health');
    fireEvent.click(btn);
    // Wait until the submission is actually in flight (buttons disabled)…
    await screen.findByTestId('operator-loading');
    fireEvent.click(btn);            // disabled now; must not fire again
    fireEvent.click(screen.getByTestId('operator-btn-noop'));  // also disabled
    expect(spy).toHaveBeenCalledTimes(1);
    resolve(view());
    await waitFor(() => expect(screen.getByTestId('operator-command-result')).toBeTruthy());
  });
});

describe('idempotency and no-token handling (api layer)', () => {
  it('generates a fresh idempotency key per intentional action', async () => {
    const keys: string[] = [];
    const stub = vi.fn(async (_url: string, init: RequestInit) => {
      keys.push(JSON.parse(init.body as string).idempotencyKey);
      return { ok: true, status: 200, json: async () => view() } as unknown as Response;
    });
    vi.stubGlobal('fetch', stub);
    await api.operatorSubmitCommand('request_health');
    await api.operatorSubmitCommand('request_health');
    expect(keys).toHaveLength(2);
    expect(keys[0]).not.toBe(keys[1]);     // a new key per action
    vi.unstubAllGlobals();
  });

  it('sends no Authorization header or token from the browser', async () => {
    let seenInit: RequestInit | undefined;
    const stub = vi.fn(async (_url: string, init: RequestInit) => {
      seenInit = init;
      return { ok: true, status: 200, json: async () => view() } as unknown as Response;
    });
    vi.stubGlobal('fetch', stub);
    await api.operatorSubmitCommand('noop');
    const headers = (seenInit?.headers ?? {}) as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('authorization');
    expect(JSON.stringify(seenInit)).not.toContain('token');
    vi.unstubAllGlobals();
  });
});
