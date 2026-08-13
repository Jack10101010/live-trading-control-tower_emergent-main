/**
 * UI-1 — connection-state derivation.
 *
 * The rule under test throughout: absence is never upgraded to health. The UI may
 * say "unknown", "stale" or "unavailable"; it may not say "connected", "healthy"
 * or "online" for anything it has not actually observed.
 */
import { describe, it, expect } from 'vitest';
import type { ConnectionInstance, ConnectionState } from '@/lib/api';
import {
  deriveBackendState,
  deriveDimensions,
  formatAge,
  formatPublishedAt,
  orderInstances,
  summarize,
} from '@/lib/connectionState';

function instance(over: Partial<ConnectionInstance> = {}): ConnectionInstance {
  return {
    instanceId: 'node-1',
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
    ageSeconds: 5,
    staleAfterSeconds: 120,
    mode: 'dry_run',
    engineVersion: '5bb6372c',
    ...over,
  };
}

function state(over: Partial<ConnectionState> = {}): ConnectionState {
  const instances = over.instances ?? [instance()];
  return {
    observedAt: '2026-07-25T12:00:05Z',
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

const stateOf = (dims: ReturnType<typeof deriveDimensions>, key: string) =>
  dims.find((d) => d.key === key)!;

describe('backend dimension', () => {
  it('is starting while the first request is in flight', () => {
    expect(deriveBackendState(true, false)).toBe('starting');
  });

  it('is available once the API answers', () => {
    expect(deriveBackendState(false, false)).toBe('available');
  });

  it('is unavailable when the request failed', () => {
    expect(deriveBackendState(false, true)).toBe('unavailable');
    // A failure outranks a still-loading state.
    expect(deriveBackendState(true, true)).toBe('unavailable');
  });

  it('never claims anything about the node just because the backend answered', () => {
    const dims = deriveDimensions('available', undefined);
    expect(stateOf(dims, 'backend').state).toBe('available');
    expect(stateOf(dims, 'node').state).toBe('unknown');
    expect(stateOf(dims, 'bridge').state).toBe('unknown');
    expect(stateOf(dims, 'backend').detail).toMatch(/says nothing about the execution node/i);
  });
});

describe('first run', () => {
  it('reports an explicit first-run state, not health', () => {
    const s = state({
      firstRun: true,
      telemetry: 'never_received',
      node: 'unknown',
      bridge: 'unknown',
      instances: [],
      instanceCount: 0,
      emptyState: 'No live execution node has ever published telemetry.',
    });
    const dims = deriveDimensions('available', s);
    expect(stateOf(dims, 'telemetry').state).toBe('never_received');
    expect(stateOf(dims, 'node').state).toBe('unknown');
    expect(summarize('available', s)).toEqual({ label: 'no node telemetry', tone: 'neutral' });
  });

  it('uses no optimistic vocabulary anywhere', () => {
    const s = state({ firstRun: true, telemetry: 'never_received', node: 'unknown',
                      bridge: 'unknown', instances: [], instanceCount: 0 });
    const text = JSON.stringify(deriveDimensions('available', s)).toLowerCase();
    for (const word of ['"connected"', '"healthy"', 'online']) {
      expect(text).not.toContain(word);
    }
  });
});

describe('fresh telemetry', () => {
  it('reports the node connected and the bridge separately', () => {
    const dims = deriveDimensions('available', state());
    expect(stateOf(dims, 'telemetry').state).toBe('fresh');
    expect(stateOf(dims, 'node').state).toBe('connected');
    // The bridge is a DIFFERENT fact and stays unknown until observed.
    expect(stateOf(dims, 'bridge').state).toBe('unknown');
  });

  it('never lets node health imply MT5 health', () => {
    const s = state({ node: 'connected', bridge: 'unknown' });
    expect(summarize('available', s).label).toBe('node live · bridge unknown');
    expect(summarize('available', s).tone).toBe('caution');
  });

  it('reports a healthy bridge only when the backend says so', () => {
    const s = state({ bridge: 'healthy', instances: [instance({ bridge: 'healthy' })] });
    expect(summarize('available', s)).toEqual({
      label: 'node live · bridge healthy',
      tone: 'positive',
    });
  });
});

describe('stale telemetry', () => {
  it('is explicit and never keeps showing connected', () => {
    const s = state({
      telemetry: 'stale',
      node: 'unknown',
      instances: [instance({ telemetry: 'stale', node: 'unknown', ageSeconds: 9000 })],
    });
    const dims = deriveDimensions('available', s);
    expect(stateOf(dims, 'telemetry').state).toBe('stale');
    expect(stateOf(dims, 'telemetry').tone).toBe('caution');
    expect(stateOf(dims, 'node').state).not.toBe('connected');
    expect(summarize('available', s)).toEqual({ label: 'node telemetry stale', tone: 'caution' });
  });

  it('does not accuse the node of being down', () => {
    const s = state({ telemetry: 'stale', node: 'unknown' });
    expect(stateOf(deriveDimensions('available', s), 'node').state).toBe('unknown');
    expect(stateOf(deriveDimensions('available', s), 'node').state).not.toBe('disconnected');
  });
});

describe('backend unavailable', () => {
  it('collapses every downstream dimension to unknown, not to a fault', () => {
    const dims = deriveDimensions('unavailable', state());
    expect(stateOf(dims, 'backend').state).toBe('unavailable');
    expect(stateOf(dims, 'backend').tone).toBe('negative');
    for (const key of ['telemetry', 'node', 'bridge']) {
      expect(stateOf(dims, key).state).toBe('unknown');
      expect(stateOf(dims, key).detail).toMatch(/could not be reached/i);
    }
  });

  it('ignores any stale cached connection payload', () => {
    // Even holding a "connected" payload, an unreachable backend cannot vouch for it.
    const dims = deriveDimensions('unavailable', state({ node: 'connected' }));
    expect(stateOf(dims, 'node').state).toBe('unknown');
  });

  it('summarizes as backend unavailable rather than node state', () => {
    expect(summarize('unavailable', state())).toEqual({
      label: 'backend unavailable',
      tone: 'negative',
    });
  });
});

describe('unknown node and bridge are distinct from unavailable', () => {
  it('keeps unknown and unavailable separate for the bridge', () => {
    const unknown = deriveDimensions('available', state({ bridge: 'unknown' }));
    const unavailable = deriveDimensions(
      'available',
      state({ bridge: 'unavailable', instances: [instance({ bridge: 'unavailable',
        bridgeEvidence: 'node could not read broker positions' })] })
    );
    expect(stateOf(unknown, 'bridge').state).toBe('unknown');
    expect(stateOf(unknown, 'bridge').tone).toBe('caution');
    expect(stateOf(unavailable, 'bridge').state).toBe('unavailable');
    expect(stateOf(unavailable, 'bridge').tone).toBe('negative');
  });

  it('surfaces the backend evidence sentence for the reported state', () => {
    const s = state({
      bridge: 'degraded',
      instances: [instance({ bridge: 'degraded', bridgeEvidence: 'node reported an unhealthy price feed' })],
    });
    expect(stateOf(deriveDimensions('available', s), 'bridge').detail).toBe(
      'node reported an unhealthy price feed'
    );
  });

  it('reports an unknown node without implying disconnection', () => {
    const s = state({ node: 'unknown', instances: [instance({ node: 'unknown',
      nodeEvidence: 'telemetry is stale; the node may still be running' })] });
    const dim = stateOf(deriveDimensions('available', s), 'node');
    expect(dim.state).toBe('unknown');
    expect(dim.detail).toMatch(/may still be running/i);
  });

  it('reports a disconnected node only as the backend classified it', () => {
    const s = state({ node: 'disconnected', instances: [instance({ node: 'disconnected' })] });
    expect(stateOf(deriveDimensions('available', s), 'node').tone).toBe('negative');
    expect(summarize('available', s).label).toBe('node disconnected');
  });
});

describe('multiple instances', () => {
  it('preserves every instance without assuming one', () => {
    const s = state({
      telemetry: 'stale',
      node: 'unknown',
      instances: [
        instance({ instanceId: 'node-a' }),
        instance({ instanceId: 'node-b', telemetry: 'stale', node: 'unknown', ageSeconds: 9000 }),
      ],
    });
    expect(s.instances).toHaveLength(2);
    expect(orderInstances(s.instances).map((i) => i.instanceId)).toEqual(['node-b', 'node-a']);
  });

  it('orders worst-first so a silent instance is not hidden below a healthy one', () => {
    const ordered = orderInstances([
      instance({ instanceId: 'good', telemetry: 'fresh' }),
      instance({ instanceId: 'broken', telemetry: 'unavailable' }),
      instance({ instanceId: 'quiet', telemetry: 'stale' }),
    ]);
    expect(ordered.map((i) => i.instanceId)).toEqual(['broken', 'quiet', 'good']);
  });

  it('shows the worst rollup the backend computed, never the best', () => {
    const s = state({ telemetry: 'stale', node: 'unknown', bridge: 'unknown' });
    const dims = deriveDimensions('available', s);
    expect(stateOf(dims, 'telemetry').state).toBe('stale');
    expect(stateOf(dims, 'node').state).toBe('unknown');
  });
});

describe('snapshot problems are distinguishable', () => {
  it('reports telemetry unavailable in the negative tone', () => {
    const s = state({
      telemetry: 'unavailable',
      node: 'unknown',
      instances: [instance({ telemetry: 'unavailable', node: 'unknown',
                             problem: 'malformed_snapshot', publishedAt: null, ageSeconds: null })],
    });
    const dim = stateOf(deriveDimensions('available', s), 'telemetry');
    expect(dim.state).toBe('unavailable');
    expect(dim.tone).toBe('negative');
    expect(summarize('available', s)).toEqual({ label: 'telemetry unreadable', tone: 'negative' });
  });
});

describe('freshness formatting', () => {
  it('never estimates a timestamp it does not have', () => {
    expect(formatAge(null)).toBe('unknown');
    expect(formatPublishedAt(null)).toBe('unknown');
  });

  it('shows the node timestamp verbatim', () => {
    expect(formatPublishedAt('2026-07-25T12:00:00Z')).toBe('2026-07-25T12:00:00Z');
  });

  it('formats age in plain units', () => {
    expect(formatAge(5)).toBe('5s ago');
    expect(formatAge(125)).toBe('2m ago');
    expect(formatAge(7200)).toBe('2h ago');
    expect(formatAge(172_800)).toBe('2d ago');
  });
});
