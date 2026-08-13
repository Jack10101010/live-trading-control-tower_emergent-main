/**
 * UI-0 — provenance & no-silent-fallback guarantees (pure logic).
 *
 * These assert SEMANTICS: that generated data is marked as generated, that no
 * fabricated research evidence is emitted, and that the global data-source badge
 * never claims a live node without one.
 */
import { describe, it, expect } from 'vitest';
import { expandMatrix } from '@/lib/matrixExpand';
import { deriveDataSourceBadge } from '@/lib/dataSource';
import { deriveMarketStructure } from '@/lib/marketStructure';
import { deriveOrderBlocks } from '@/lib/orderBlocks';
import { deriveFairValueGaps } from '@/lib/fairValueGaps';
import { deriveLiquidityPools } from '@/lib/liquidity';
import { SYNTHETIC_VOLUME_LABEL, SYNTHETIC_VOLUME_PROVENANCE, type Candle } from '@/lib/chartData';
import { CHART_LAYERS, DEFAULT_LAYER_IDS } from '@/lib/chartLayers';
import { isTrustedProvenance } from '@/types/provenance';
import type { BackendHealth } from '@/lib/api';

const candles: Candle[] = Array.from({ length: 30 }, (_, i) => ({
  time: 1_800_000_000 + i * 900,
  open: 1.1 + i * 1e-4,
  high: 1.1005 + i * 1e-4,
  low: 1.0995 + i * 1e-4,
  close: 1.1002 + i * 1e-4,
}));

const healthyFixture: BackendHealth = {
  status: 'ok',
  scope: 'process',
  serverTime: '2026-07-25T10:00:00Z',
  backendMode: 'fixture',
  brokerKind: 'mock',
  liveNodeConnected: false,
  tradingReady: false,
  dataSources: { world: 'fixture', broker: 'mock', nodeTelemetry: 'unavailable' },
  nodeTelemetry: { connected: false, instances: [] },
  fixture: { available: true, version: 'world.v1', asOf: '2026-07-01T09:14:22Z' },
};

/* ── policy matrix: synthesized cells carry provenance, never fake evidence ── */

describe('policy matrix provenance', () => {
  it('marks every locally generated cell as synthesized', () => {
    const m = expandMatrix('EURUSD', undefined);
    const cells = Object.values(m.cells);
    expect(cells.length).toBe(144);
    expect(cells.every((c) => c.provenance === 'synthesized')).toBe(true);
    expect(m.synthesizedCells).toBe(144);
    expect(m.provenance).toBe('synthesized');
  });

  it('never emits a NATIVE badge for a generated cell', () => {
    const cells = Object.values(expandMatrix('EURUSD', undefined).cells);
    expect(cells.some((c) => c.evidence.badge === 'NATIVE')).toBe(false);
    expect(cells.every((c) => c.evidence.badge === 'NOT_TESTED')).toBe(true);
  });

  it('never invents research evidence (sample size, win rate, expectancy, confidence)', () => {
    const cells = Object.values(expandMatrix('EURUSD', undefined).cells);
    expect(cells.every((c) => c.evidence.sampleSize === 0)).toBe(true);
    expect(cells.every((c) => c.evidence.winRate === 0)).toBe(true);
    expect(cells.every((c) => c.evidence.expectancyR === 0)).toBe(true);
    expect(cells.every((c) => c.evidence.profitFactor === 0)).toBe(true);
    expect(cells.every((c) => c.evidence.confidence === 0)).toBe(true);
    expect(cells.every((c) => c.evidence.inSampleCaveat.includes('synthesized'))).toBe(true);
  });

  it('never invents a recommendation status for a generated cell', () => {
    const cells = Object.values(expandMatrix('EURUSD', undefined).cells);
    expect(cells.every((c) => c.recommendationStatus === 'none')).toBe(true);
  });

  it('keeps supplied cells distinguishable from generated ones', () => {
    const supplied = {
      instrument: 'EURUSD',
      cohortAxis: { sessions: [], structures: [], directions: [] },
      cohortBaseTargets: {},
      cells: {
        'london:BOS:long:BullExpand': {
          policyCellKey: 'london:BOS:long:BullExpand',
          marketState: 'BullExpand',
          eligibility: { action: 'LABEL', mode: 'Custom', resolvedAllowed: true },
          target: { rr: 2.5, source: 'cell' },
          risk: { pct: 0.5, source: 'cell' },
          evidence: {
            badge: 'NATIVE',
            sampleSize: 42,
            expectancyR: 0.31,
            winRate: 0.44,
            profitFactor: 1.6,
            confidence: 0.9,
            inSampleCaveat: '',
          },
          recommendationStatus: 'none',
        },
      },
    } as unknown as Parameters<typeof expandMatrix>[1];
    const m = expandMatrix('EURUSD', supplied);
    const real = m.cells['london:BOS:long:BullExpand'];
    expect(real.provenance).toBe('fixture');
    expect(real.evidence.badge).toBe('NATIVE'); // a genuinely supplied badge survives
    expect(m.synthesizedCells).toBe(143);
    expect(m.sourcedCells).toBe(1);
    expect(m.provenance).toBe('synthesized'); // worst-case across the grid
  });
});

/* ── chart overlays: cannot be mistaken for Lux engine detections ── */

describe('synthetic chart overlays', () => {
  it('labels market structure as synthetic and never uses engine-style ids', () => {
    const ms = deriveMarketStructure('EURUSD', candles);
    expect(ms.structureEvents.length).toBeGreaterThan(0);
    for (const ev of ms.structureEvents) {
      expect(ev.provenance).toBe('synthesized');
      expect(ev.id.startsWith('SYN-')).toBe(true);
      expect(ev.id.startsWith('STR-')).toBe(false);
    }
    for (const s of ms.swings) {
      expect(s.provenance).toBe('synthesized');
      expect(s.id.startsWith('SYN-')).toBe(true);
    }
  });

  it('marks order blocks, FVGs and liquidity as synthesized', () => {
    for (const ob of deriveOrderBlocks('EURUSD', candles)) {
      expect(ob.provenance).toBe('synthesized');
      expect(ob.id.startsWith('SYN-')).toBe(true);
    }
    for (const g of deriveFairValueGaps('EURUSD', candles)) {
      expect(g.provenance).toBe('synthesized');
      expect(g.id.startsWith('SYN-')).toBe(true);
    }
    for (const l of deriveLiquidityPools('EURUSD', candles)) {
      expect(l.provenance).toBe('synthesized');
      expect(l.id.startsWith('SYN-')).toBe(true);
    }
  });

  it('keeps every synthetic layer off by default and labels it in the control', () => {
    const synthetic = CHART_LAYERS.filter((l) => l.provenance === 'synthesized');
    expect(synthetic.map((l) => l.id).sort()).toEqual(
      ['fairValueGaps', 'liquidity', 'orderBlocks', 'structure', 'swings'].sort()
    );
    for (const l of synthetic) {
      expect(l.defaultOn).toBe(false);
      expect(l.label.toLowerCase()).toContain('synthetic');
      expect(DEFAULT_LAYER_IDS).not.toContain(l.id);
    }
  });
});

/* ── synthetic volume ── */

describe('synthetic chart volume', () => {
  it('is declared synthesized and carries an explicit label', () => {
    expect(SYNTHETIC_VOLUME_PROVENANCE).toBe('synthesized');
    expect(SYNTHETIC_VOLUME_LABEL).toMatch(/synthetic/i);
    expect(isTrustedProvenance(SYNTHETIC_VOLUME_PROVENANCE)).toBe(false);
  });
});

/* ── global data-source badge ── */

describe('global data-source badge', () => {
  it('reports fixture + mock broker truthfully when no node is connected', () => {
    const b = deriveDataSourceBadge(healthyFixture, false);
    expect(b.provenance).toBe('fixture');
    expect(b.label).toContain('fixture');
    expect(b.label).toContain('mock');
    expect(isTrustedProvenance(b.provenance)).toBe(false);
    expect(b.hint).toContain('trading ready: no');
  });

  it('never claims a live node just because a mock broker is connected', () => {
    const b = deriveDataSourceBadge({ ...healthyFixture, brokerKind: 'mock' }, false);
    expect(b.provenance).not.toBe('live-node');
  });

  it('claims live-node only when node telemetry exists', () => {
    const b = deriveDataSourceBadge(
      {
        ...healthyFixture,
        liveNodeConnected: true,
        dataSources: { ...healthyFixture.dataSources, nodeTelemetry: 'available' },
        nodeTelemetry: { connected: true, instances: ['some-instance'] },
      },
      false
    );
    expect(b.provenance).toBe('live-node');
  });

  it('reports an explicit offline state when the health check fails', () => {
    const b = deriveDataSourceBadge(undefined, true);
    expect(b.provenance).toBe('unavailable');
    expect(b.label).toContain('offline');
  });

  it('says "unknown" rather than guessing before health arrives', () => {
    const b = deriveDataSourceBadge(undefined, false);
    expect(b.provenance).toBe('placeholder');
  });
});
