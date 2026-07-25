/**
 * UI-3 regression guards.
 *
 * Two kinds of guard live here:
 *
 *   CONTRACT — the frontend types still match what the backend actually returns,
 *              and UI-1/UI-2 semantics survive UI-3 unchanged.
 *   ANTI-REGRESSION — the misleading indicators UI-3 scoped or de-emphasised must
 *              not quietly reappear. These read the component sources, because
 *              the failure mode is a future edit reinstating a fixture-derived
 *              claim in persistent chrome, which no behavioural test would catch.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { deriveDimensions, deriveBackendState } from '@/lib/connectionState';
import { deriveDataSourceBadge } from '@/lib/dataSource';
import { deriveLiveOperations } from '@/lib/liveOperations';
import type { BackendHealth, ConnectionState } from '@/lib/api';

const SRC = resolve(__dirname, '..', '..');
const read = (rel: string) => readFileSync(resolve(SRC, rel), 'utf8');

function connection(over: Partial<ConnectionState> = {}): ConnectionState {
  return {
    observedAt: '2026-07-25T12:00:05Z', staleAfterSeconds: 120, firstRun: false,
    telemetry: 'fresh', node: 'connected', bridge: 'healthy',
    instanceCount: 0, instances: [], emptyState: null, ...over,
  };
}

/* ── UI-1 must remain intact ────────────────────────────────────────────────*/

describe('UI-1 connection panel remains truthful', () => {
  it('still collapses every dimension to unknown when the backend is unreachable', () => {
    const dims = deriveDimensions('unavailable', connection());
    for (const key of ['telemetry', 'node', 'bridge']) {
      expect(dims.find((d) => d.key === key)!.state).toBe('unknown');
    }
  });

  it('still returns node and bridge to unknown on stale telemetry', () => {
    // UI-1's rule: a stale reading is not a current one. UI-3 must not have
    // reintroduced last-known-good display through the shared derivation.
    const dims = deriveDimensions(
      'available',
      connection({ telemetry: 'stale', node: 'unknown', bridge: 'unknown' })
    );
    expect(dims.find((d) => d.key === 'node')!.state).toBe('unknown');
    expect(dims.find((d) => d.key === 'bridge')!.state).toBe('unknown');
  });

  it('still derives the backend dimension from the client, not the payload', () => {
    expect(deriveBackendState(true, false)).toBe('starting');
    expect(deriveBackendState(false, true)).toBe('unavailable');
  });
});

/* ── UI-0/UI-1 data-source badge must remain freshness-aware ────────────────*/

describe('data-source badge regression', () => {
  const health = (over: Partial<BackendHealth> = {}): BackendHealth => ({
    status: 'ok', scope: 'process', serverTime: '2026-07-25T12:00:00Z',
    backendMode: 'fixture', brokerKind: 'mock', liveNodeConnected: true,
    tradingReady: false,
    dataSources: { world: 'fixture', broker: 'mock', nodeTelemetry: 'available' },
    nodeTelemetry: { connected: true, instances: ['n'] },
    fixture: { available: true }, ...over,
  });

  it('still downgrades a stale node instead of badging it live', () => {
    expect(deriveDataSourceBadge(health(), false, 'stale').provenance).toBe('stale');
  });

  it('still reports an unreadable snapshot rather than live-node', () => {
    expect(deriveDataSourceBadge(health(), false, 'unavailable').provenance).toBe('stale');
  });

  it('still claims live-node only when telemetry is fresh', () => {
    expect(deriveDataSourceBadge(health(), false, 'fresh').provenance).toBe('live-node');
  });
});

/* ── fixture state must never create operational truth ──────────────────────*/

describe('fixture isolation', () => {
  it('cannot produce a healthy strip without node telemetry', () => {
    const ops = deriveLiveOperations('available', connection({ instances: [] }), undefined);
    expect(ops.severity).not.toBe('healthy');
    expect(ops.instances).toEqual([]);
  });

  it('the operations model imports nothing fixture-derived', () => {
    const source = read('lib/liveOperations.ts');
    for (const forbidden of ['useFleet', 'useWorld', 'systemConfidence', 'brokerHealth',
                             'deployments', 'WorldFixture']) {
      expect(source).not.toContain(forbidden);
    }
  });

  it('the strip reads only the two live endpoints', () => {
    const source = read('components/shell/LiveOperationsStrip.tsx');
    expect(source).toContain('api.liveConnection');
    expect(source).toContain('api.liveStatus');
    for (const forbidden of ['api.world', 'api.fleet', 'api.systemConfidence',
                             'api.brokerHealth', 'useFleet']) {
      expect(source).not.toContain(forbidden);
    }
  });
});

/* ── misleading indicators must not reappear ────────────────────────────────*/

describe('misleading indicator cleanup stays applied', () => {
  it('the fixture execution-mode banner stays scope-labelled and unanimated', () => {
    const source = read('components/shell/CommandSafetyBar.tsx');
    // Label discloses that this is the fixture plan, not the node's runtime mode.
    expect(source).toContain('plan ${execMode}');
    expect(source).toMatch(/NOT the live node's runtime mode/);
    // The pulsing "LIVE" is gone: animation must not be a primary safety signal.
    expect(source).not.toContain("execMode === 'live' ? 'ct-pulse-dot' : ''");
  });

  it('the realtime chip still names the transport it actually describes', () => {
    const source = read('components/shell/CommandSafetyBar.tsx');
    // Every branch of the chip label must name the stream. A bare "live" here read
    // as live TRADING, which is what UI-3 removed.
    for (const label of ['stream ok', 'stream connecting', 'stream offline', 'stream retrying']) {
      expect(source).toContain(`'${label}'`);
    }
    // And no pulse survives on it either.
    expect(source).not.toContain('ct-pulse-dot');
  });

  it('fixture confidence chips stay prefixed so they cannot read as node truth', () => {
    const source = read('components/shell/ContextBar.tsx');
    expect(source).toContain("label: 'fx Reconcile'");
    expect(source).toContain("label: 'fx Broker'");
    expect(source).not.toContain("label: 'Reconcile'");
  });

  it('the fixture deployment "Armed" status stays scope-labelled', () => {
    const source = read('components/shell/ScopeNavigator.tsx');
    expect(source).toMatch(/Fixture deployment status/);
    expect(source).toMatch(/Not live-node arming or execution state/);
  });

  it('only one persistent operational surface exists in the shell', () => {
    // Two competing "live status" surfaces with different semantics is the exact
    // confusion UI-3 removes. The strip is mounted once.
    const shell = read('components/shell/AppShell.tsx');
    expect(shell.match(/<LiveOperationsStrip \/>/g)).toHaveLength(1);
  });
});

/* ── read-only boundary ─────────────────────────────────────────────────────*/

describe('read-only boundary', () => {
  it('neither UI-3 file references the command bus or any mutation', () => {
    for (const rel of ['lib/liveOperations.ts', 'components/shell/LiveOperationsStrip.tsx']) {
      const source = read(rel);
      for (const forbidden of ['useCommand', 'useMutation', 'apiPost', "method: 'POST'",
                               'GlobalKill', 'ConfirmDialog']) {
        expect(source, `${rel} must not reference ${forbidden}`).not.toContain(forbidden);
      }
    }
  });

  it('adds no new backend route or schema version', () => {
    const source = read('lib/api.ts');
    // UI-3 consumes only the endpoints UI-1/UI-2 already exposed.
    expect(source).toContain("apiFetch<ConnectionState>('/live/connection')");
    expect(source).toContain("apiFetch<LiveStatus>('/live/status')");
    expect(source).not.toContain("'/live/command'");
    expect(source).toContain('ct.node-telemetry.v1');   // referenced, never redefined
  });
});

/* ── UI-10: the API client must keep working with credentials disabled ────────*/

describe('UI-10 client regression', () => {
  it('no fetch call opts into credentials', () => {
    const source = read('lib/api.ts');
    expect(source).not.toContain("credentials: 'include'");
    expect(source).not.toContain('credentials: "include"');
    expect(source).not.toContain('withCredentials');
  });

  it('no authentication header is sent', () => {
    const source = read('lib/api.ts');
    expect(source).not.toContain('Authorization');
    expect(source).not.toContain('Bearer ');
    expect(source).not.toContain('X-Api-Key');
  });

  it('no cookie or session dependency exists', () => {
    const source = read('lib/api.ts');
    expect(source).not.toContain('document.cookie');
    expect(source).not.toContain('sessionStorage');
  });

  it('only the request headers the backend policy allows are sent', () => {
    // The backend now allows exactly Accept, Content-Type and Idempotency-Key.
    // A new request header here would be blocked by preflight, so it must be a
    // deliberate change to both sides.
    const source = read('lib/api.ts');
    const headers = [...source.matchAll(/'?([A-Za-z][A-Za-z-]+)'?:\s*(?:'[^']*'|idempotencyKey\(\))/g)]
      .map((m) => m[1])
      .filter((name) => /^(Accept|Content-Type|Idempotency-Key|Authorization|Cookie|X-[A-Za-z-]+)$/.test(name));
    expect(new Set(headers)).toEqual(new Set(['Accept', 'Content-Type', 'Idempotency-Key']));
  });
});
