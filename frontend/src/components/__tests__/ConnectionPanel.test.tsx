/**
 * UI-1 — the connection panel must render truth, not reassurance.
 *
 * These tests mount the real component against a stubbed API so the assertions
 * are about what an operator actually SEES. The recurring check is negative: the
 * words "healthy", "connected" and "online" must not appear unless the backend
 * genuinely reported them for the dimension they describe.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ConnectionInstance, ConnectionState } from '@/lib/api';
import { api } from '@/lib/api';
import { ConnectionPanel } from '@/components/domain/ConnectionPanel';

function instance(over: Partial<ConnectionInstance> = {}): ConnectionInstance {
  return {
    instanceId: 'live-eurusd-golden-001',
    telemetry: 'fresh',
    node: 'connected',
    nodeEvidence: 'fresh telemetry received from the node',
    bridge: 'unknown',
    bridgeEvidence: 'node did not sample the bridge on its last cycle',
    problem: null,
    schemaVersion: 'ct.node-telemetry.v1',
    legacySource: false,
    publishedAt: '2026-07-25T12:00:00Z',
    receivedAt: '2026-07-25T12:00:01Z',
    ageSeconds: 12,
    staleAfterSeconds: 120,
    mode: 'dry_run',
    engineVersion: '5bb6372c',
    ...over,
  };
}

function payload(over: Partial<ConnectionState> = {}): ConnectionState {
  const instances = over.instances ?? [instance()];
  return {
    observedAt: '2026-07-25T12:00:12Z',
    staleAfterSeconds: 120,
    firstRun: false,
    telemetry: 'fresh',
    node: 'connected',
    bridge: 'unknown',
    instanceCount: instances.length,
    instances,
    emptyState: null,
    ...over,
  };
}

function mount() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, refetchInterval: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <ConnectionPanel />
    </QueryClientProvider>
  );
}

beforeEach(() => {
  vi.restoreAllMocks();
});

afterEach(() => {
  cleanup();
});

describe('first run', () => {
  it('shows an explicit never-received state instead of health', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(
      payload({
        firstRun: true,
        telemetry: 'never_received',
        node: 'unknown',
        bridge: 'unknown',
        instances: [],
        instanceCount: 0,
        emptyState: 'No live execution node has ever published telemetry to this Control Tower.',
      })
    );
    mount();
    expect(await screen.findByText('No telemetry received yet')).toBeTruthy();
    expect(screen.getByText(/never_received/)).toBeTruthy();
    expect(screen.queryByText('healthy')).toBeNull();
    expect(screen.queryByText('online')).toBeNull();
  });
});

describe('fresh telemetry', () => {
  it('shows node, bridge, timestamps, schema and instance id', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(payload());
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('dim-telemetry').textContent).toContain('fresh')
    );
    expect(screen.getByText('live-eurusd-golden-001')).toBeTruthy();
    expect(screen.getByText('2026-07-25T12:00:00Z')).toBeTruthy();   // last update
    expect(screen.getByText('12s ago')).toBeTruthy();                // telemetry age
    expect(screen.getByText('120s')).toBeTruthy();                   // stale threshold
    expect(screen.getByText('ct.node-telemetry.v1')).toBeTruthy();   // schema version
  });

  it('reports node connected without claiming the bridge is healthy', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(payload());
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('dim-node').textContent).toContain('connected')
    );
    const bridge = screen.getByTestId('dim-bridge');
    expect(bridge.textContent).toContain('unknown');
    expect(bridge.textContent).not.toContain('healthy');
  });
});

describe('stale telemetry', () => {
  it('says stale and stops reporting the node as connected', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(
      payload({
        telemetry: 'stale',
        node: 'unknown',
        instances: [instance({ telemetry: 'stale', node: 'unknown', ageSeconds: 9000,
                               nodeEvidence: 'telemetry is stale; the node may still be running' })],
      })
    );
    mount();
    await waitFor(() =>
      expect(screen.getByTestId('dim-telemetry').textContent).toContain('stale')
    );
    const node = screen.getByTestId('dim-node');
    expect(node.textContent).toContain('unknown');
    expect(node.textContent).not.toContain('connected');
    expect(screen.getByText('2h ago')).toBeTruthy();
  });
});

describe('backend unavailable', () => {
  it('degrades every dimension to unknown and explains the node may still run', async () => {
    vi.spyOn(api, 'liveConnection').mockRejectedValue(new Error('failed to fetch'));
    mount();
    await waitFor(() => {
      expect(screen.getByTestId('dim-backend').textContent).toContain('unavailable');
    });
    for (const key of ['dim-telemetry', 'dim-node', 'dim-bridge']) {
      expect(screen.getByTestId(key).textContent).toContain('unknown');
    }
    expect(screen.getByText(/keeps trading and protecting the account/i)).toBeTruthy();
  });
});

describe('snapshot problems', () => {
  it('distinguishes an unsupported schema from a malformed snapshot', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(
      payload({
        telemetry: 'unavailable',
        node: 'unknown',
        instances: [instance({ telemetry: 'unavailable', node: 'unknown',
                               problem: 'unsupported_schema', schemaVersion: 'ct.node-telemetry.v9',
                               publishedAt: null, ageSeconds: null })],
      })
    );
    mount();
    expect(await screen.findByText('Unsupported schema version')).toBeTruthy();
    expect(screen.queryByText('Malformed snapshot')).toBeNull();
    // No timestamp is invented for a snapshot that could not be read.
    expect(screen.getAllByText('unknown').length).toBeGreaterThan(0);
  });
});

describe('multiple instances', () => {
  it('renders every instance, worst first, without assuming one', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(
      payload({
        telemetry: 'stale',
        node: 'unknown',
        instances: [
          instance({ instanceId: 'node-alpha' }),
          instance({ instanceId: 'node-beta', telemetry: 'stale', node: 'unknown', ageSeconds: 9000 }),
        ],
      })
    );
    mount();
    await waitFor(() => expect(screen.getByTestId('instance-node-alpha')).toBeTruthy());
    expect(screen.getByTestId('instance-node-beta')).toBeTruthy();
    expect(screen.getByText('2 instances')).toBeTruthy();
    const panel = screen.getByTestId('connection-panel');
    const order = Array.from(panel.querySelectorAll('[data-testid^="instance-"]'))
      .map((el) => el.getAttribute('data-testid'));
    expect(order).toEqual(['instance-node-beta', 'instance-node-alpha']);
  });
});

describe('legacy node', () => {
  it('discloses that a legacy payload reports no bridge or account state', async () => {
    vi.spyOn(api, 'liveConnection').mockResolvedValue(
      payload({ instances: [instance({ legacySource: true, bridge: 'unknown' })] })
    );
    mount();
    expect(await screen.findByText(/predates the versioned telemetry contract/i)).toBeTruthy();
  });
});
