/**
 * UI-3 — the operations strip must render truth and offer no control.
 *
 * Two classes of assertion dominate:
 *   - what an operator SEES (text, not colour; absolute + relative timestamps;
 *     reason codes preserved)
 *   - what an operator CANNOT DO (no action button, no mutation callback) — the
 *     read-only boundary is a safety property, so it is tested, not assumed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type {
  ConnectionInstance,
  ConnectionState,
  LiveStatus,
  LiveStatusEntry,
  TelemetrySnapshot,
} from '@/lib/api';
import { api } from '@/lib/api';
import { LiveOperationsStrip } from '@/components/shell/LiveOperationsStrip';

function snapshot(over: Partial<TelemetrySnapshot> = {}): TelemetrySnapshot {
  return {
    schema_version: 'ct.node-telemetry.v1',
    instance_id: 'live-eurusd-golden-001',
    published_at: '2026-07-25T12:00:00Z',
    runtime: {
      mode: 'dry_run', submission_disabled: false, kill_switch_active: false,
      open_eligibility: { eligible: null, reasons: ['authorization_not_evaluated_by_node_rails'] },
    },
    reconciliation: { available: true, clean: true, frozen: false, recovery_required: false,
                      unresolved_sent_count: 0, snapshot_status: 'ok' },
    account: { identity: { available: true },
               health: { available: true, healthy: true, trade_allowed: true, trade_expert: true } },
    arming: { status: 'armed', armed: true, expires_at: '2099-01-01T00:00:00Z',
              attempts_remaining: 1, fingerprint_matches: true },
    execution: { cycle_frozen: false, pending_intents: [], blocks: [], unresolved_sent: [] },
    engine: { symbol: 'EURUSD', timeframe: 'M15' },
    market: { available: true, feed_healthy: true },
    ...over,
  };
}

function entry(over: Partial<LiveStatusEntry> = {}): LiveStatusEntry {
  return {
    instance_id: 'live-eurusd-golden-001', schema_version: 'ct.node-telemetry.v1',
    legacy_source: false, published_at: '2026-07-25T12:00:00Z',
    observed_at: '2026-07-25T12:00:05Z', received_at: '2026-07-25T12:00:01Z',
    age_seconds: 12, stale: false, stale_after_seconds: 120, snapshot: snapshot(),
    ...over,
  };
}

function connInstance(over: Partial<ConnectionInstance> = {}): ConnectionInstance {
  return {
    instanceId: 'live-eurusd-golden-001', telemetry: 'fresh', node: 'connected',
    nodeEvidence: 'fresh telemetry received from the node', bridge: 'healthy',
    bridgeEvidence: 'node observed a healthy price feed', problem: null,
    schemaVersion: 'ct.node-telemetry.v1', legacySource: false,
    publishedAt: '2026-07-25T12:00:00Z', receivedAt: '2026-07-25T12:00:01Z',
    ageSeconds: 12, staleAfterSeconds: 120, mode: 'dry_run', engineVersion: '5bb6372c',
    ...over,
  };
}

function connection(over: Partial<ConnectionState> = {}): ConnectionState {
  const instances = over.instances ?? [connInstance()];
  return {
    observedAt: '2026-07-25T12:00:12Z', staleAfterSeconds: 120, firstRun: false,
    telemetry: 'fresh', node: 'connected', bridge: 'healthy',
    instanceCount: instances.length, instances, emptyState: null, ...over,
  };
}

function status(entries: LiveStatusEntry[]): LiveStatus {
  return {
    schemaVersion: 'ct.node-telemetry.v1', observedAt: '2026-07-25T12:00:12Z',
    instances: entries.map((e) => e.instance_id),
    statuses: Object.fromEntries(entries.map((e) => [e.instance_id, e])),
    emptyState: null,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <LiveOperationsStrip />
    </QueryClientProvider>
  );
}

function stub(conn: ConnectionState, st?: LiveStatus) {
  vi.spyOn(api, 'liveConnection').mockResolvedValue(conn);
  vi.spyOn(api, 'liveStatus').mockResolvedValue(st ?? status([]));
}

beforeEach(() => vi.restoreAllMocks());
afterEach(() => cleanup());

const severity = () => screen.getByTestId('ops-severity').textContent ?? '';

describe('first run', () => {
  it('is explicit and offers no optimistic words', async () => {
    stub(connection({ firstRun: true, telemetry: 'never_received', node: 'unknown',
                      bridge: 'unknown', instances: [], instanceCount: 0,
                      emptyState: 'No execution node has ever published telemetry.' }));
    mount();
    expect(await screen.findByTestId('ops-first-run')).toBeTruthy();
    await waitFor(() => expect(severity()).toContain('UNKNOWN'));
    // The overall verdict must never read healthy...
    expect(severity()).not.toContain('NOMINAL');
    // ...and no OPERATIONAL chip may exist at all, because none was observed.
    for (const key of ['mode', 'submission', 'kill', 'reconciliation', 'unresolved',
                       'account', 'arming', 'open']) {
      expect(screen.queryByTestId(`ops-chip-${key}`)).toBeNull();
    }
    // Telemetry/node/bridge must all say unknown or never_received.
    expect(screen.getByTestId('ops-chip-telemetry').textContent).toContain('never_received');
    expect(screen.getByTestId('ops-chip-node').textContent).toContain('unknown');
    expect(screen.getByTestId('ops-chip-bridge').textContent).toContain('unknown');
    // A crude word-ban is the wrong tool here: the honest first-run disclaimer
    // NAMES the forbidden words in order to deny them ("...not healthy, armed,
    // live or ready..."). The structural assertions above are stronger, so this
    // just pins the disclaimer itself.
    expect(screen.getByTestId('ops-first-run').textContent).toMatch(
      /nothing here reports the system as healthy, armed, live or ready/i
    );
  });
});

describe('fresh healthy strip', () => {
  it('shows every minimum safety field as readable text', async () => {
    stub(connection(), status([entry()]));
    mount();
    await waitFor(() => expect(severity()).toContain('NOMINAL'));
    for (const key of ['backend', 'telemetry', 'node', 'bridge', 'mode', 'submission',
                       'kill', 'reconciliation', 'unresolved', 'account', 'arming', 'open']) {
      const el = screen.getByTestId(`ops-chip-${key}`);
      // Label AND value are both present as text — colour is never the signal.
      expect(el.textContent?.trim().length).toBeGreaterThan(0);
    }
    expect(screen.getByTestId('ops-chip-kill').textContent).toContain('clear');
    expect(screen.getByTestId('ops-chip-arming').textContent).toContain('armed');
    expect(screen.getByTestId('ops-chip-open').textContent).toContain('not evaluated');
  });

  it('states severity in words, not only in colour', async () => {
    stub(connection(), status([entry()]));
    mount();
    await waitFor(() => expect(severity()).toMatch(/NOMINAL|CRITICAL|WARNING|UNKNOWN/));
  });
});

describe('critical strip', () => {
  it('shows CRITICAL for an engaged kill switch', async () => {
    stub(
      connection(),
      status([entry({ snapshot: snapshot({
        runtime: { mode: 'live', submission_disabled: false, kill_switch_active: true,
                   open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } } }) })])
    );
    mount();
    await waitFor(() => expect(severity()).toContain('CRITICAL'));
    expect(screen.getByTestId('ops-chip-kill').textContent).toContain('ENGAGED');
    expect(screen.getByTestId('ops-chip-kill').getAttribute('data-severity')).toBe('critical');
  });
});

describe('warning strip', () => {
  it('shows WARNING when the node is unarmed', async () => {
    stub(connection(), status([entry({ snapshot: snapshot({
      arming: { status: 'unarmed', armed: false } }) })]));
    mount();
    await waitFor(() => expect(severity()).toContain('WARNING'));
    expect(screen.getByTestId('ops-chip-arming').textContent).toContain('unarmed');
  });
});

describe('stale strip', () => {
  it('shows one explicit stale banner and qualifies armed', async () => {
    stub(
      connection({ telemetry: 'stale', node: 'unknown', bridge: 'unknown',
                   instances: [connInstance({ telemetry: 'stale', node: 'unknown',
                                              bridge: 'unknown', ageSeconds: 9000 })] }),
      status([entry({ stale: true, age_seconds: 9000 })])
    );
    mount();
    expect(await screen.findByTestId('ops-stale-banner')).toBeTruthy();
    expect(screen.getByTestId('ops-stale-banner').textContent).toMatch(/last observed reading/i);
    expect(screen.getByTestId('ops-chip-arming').textContent).toContain('stale');
    expect(severity()).not.toContain('NOMINAL');
  });
});

describe('backend unavailable', () => {
  it('degrades to unknown and says the node keeps running', async () => {
    vi.spyOn(api, 'liveConnection').mockRejectedValue(new Error('failed to fetch'));
    vi.spyOn(api, 'liveStatus').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('ops-chip-backend').textContent).toContain('unavailable')
    );
    expect(severity()).toContain('UNKNOWN');
    expect(screen.getByTestId('ops-headline').textContent).toMatch(/keeps running without it/i);
  });
});

describe('multiple instances', () => {
  const many = () => {
    const bad = entry({ instance_id: 'node-bad', snapshot: snapshot({
      instance_id: 'node-bad',
      runtime: { mode: 'live', submission_disabled: false, kill_switch_active: true,
                 open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } } }) });
    stub(
      connection({ instances: [connInstance(), connInstance({ instanceId: 'node-bad' })],
                   instanceCount: 2 }),
      status([entry(), bad])
    );
  };

  it('offers a read-only filter and keeps fleet severity at the worst', async () => {
    many();
    mount();
    await waitFor(() => expect(screen.getByTestId('ops-instance-selector')).toBeTruthy());
    expect(severity()).toContain('CRITICAL');
    // Filtering to the HEALTHY instance must not soften the fleet severity.
    fireEvent.change(screen.getByLabelText('Show instance:'), {
      target: { value: 'live-eurusd-golden-001' },
    });
    expect(severity()).toContain('CRITICAL');
  });

  it('is a <select> filter, not an action control', async () => {
    many();
    mount();
    await waitFor(() => expect(screen.getByTestId('ops-instance-selector')).toBeTruthy());
    expect(screen.getByLabelText('Show instance:').tagName).toBe('SELECT');
  });
});

describe('details disclosure', () => {
  it('is a keyboard-operable native details element', async () => {
    stub(connection(), status([entry()]));
    mount();
    const details = await screen.findByTestId('ops-details');
    expect(details.tagName).toBe('DETAILS');
    const summary = details.querySelector('summary');
    expect(summary).toBeTruthy();
    // <summary> is focusable and toggles on Enter/Space natively — no custom ARIA
    // or click handler needed, which is why it was chosen.
    expect(summary?.getAttribute('onclick')).toBeNull();
  });

  it('shows timestamps in both absolute and relative form', async () => {
    stub(connection(), status([entry()]));
    mount();
    await screen.findByTestId('ops-details');
    const details = screen.getByTestId('ops-details');
    expect(details.textContent).toContain('2026-07-25T12:00:00Z');   // absolute
    expect(details.textContent).toContain('12s ago');                 // relative
    expect(details.querySelector('time')?.getAttribute('datetime')).toBe('2026-07-25T12:00:00Z');
  });

  it('shows identity, symbol, timeframe and schema version', async () => {
    stub(connection(), status([entry()]));
    mount();
    await screen.findByTestId('ops-details');
    const text = screen.getByTestId('ops-details').textContent ?? '';
    expect(text).toContain('live-eurusd-golden-001');
    expect(text).toContain('EURUSD');
    expect(text).toContain('M15');
    expect(text).toContain('ct.node-telemetry.v1');
  });
});

describe('reason codes', () => {
  it('renders the machine code alongside readable text', async () => {
    stub(connection(), status([entry({ snapshot: snapshot({
      runtime: { mode: 'live', submission_disabled: true, kill_switch_active: false,
                 open_eligibility: { eligible: false,
                                     reasons: ['submission_disabled', 'not_armed'] } } }) })]));
    mount();
    await waitFor(() => expect(screen.getByTestId('ops-reasons')).toBeTruthy());
    const text = screen.getByTestId('ops-reasons').textContent ?? '';
    expect(text).toContain('submission_disabled');                    // raw code
    expect(text).toMatch(/Order submission is disabled/i);            // readable
    expect(text).toContain('not_armed');
  });

  it('renders an unrecognised code verbatim and flags it', async () => {
    stub(connection(), status([entry({ snapshot: snapshot({
      runtime: { mode: 'live', submission_disabled: false, kill_switch_active: false,
                 open_eligibility: { eligible: false, reasons: ['brand_new_rail_code'] } } }) })]));
    mount();
    await waitFor(() => expect(screen.getByTestId('ops-reasons')).toBeTruthy());
    const text = screen.getByTestId('ops-reasons').textContent ?? '';
    expect(text).toContain('brand_new_rail_code');
    expect(text).toMatch(/unrecognised code shown verbatim/i);
  });
});

describe('read-only boundary', () => {
  it('renders no action button anywhere in the strip', async () => {
    stub(connection(), status([entry()]));
    mount();
    await waitFor(() => expect(severity()).toContain('NOMINAL'));
    const strip = screen.getByTestId('live-operations-strip');
    expect(strip.querySelectorAll('button')).toHaveLength(0);
    expect(strip.querySelectorAll('input[type="checkbox"]')).toHaveLength(0);
    expect(strip.querySelectorAll('form')).toHaveLength(0);
  });

  it('exposes no mutation callback in its source', async () => {
    // A structural guard: the component must not import or invoke the command bus.
    const source = LiveOperationsStrip.toString();
    for (const forbidden of ['useCommand', 'dispatch', 'mutate']) {
      expect(source).not.toContain(forbidden);
    }
  });

  it('uses no animation as a safety signal', async () => {
    stub(connection(), status([entry({ snapshot: snapshot({
      runtime: { mode: 'live', submission_disabled: false, kill_switch_active: true,
                 open_eligibility: { eligible: false, reasons: ['kill_switch_active'] } } }) })]));
    mount();
    await waitFor(() => expect(severity()).toContain('CRITICAL'));
    const html = screen.getByTestId('live-operations-strip').innerHTML;
    expect(html).not.toContain('ct-pulse-dot');
    expect(html).not.toContain('animate-');
  });
});
