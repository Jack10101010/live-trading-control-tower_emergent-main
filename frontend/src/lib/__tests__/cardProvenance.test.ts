/**
 * Card-level provenance system (temporary migration aid) — behavioural and
 * structural guards.
 *
 * 1. The semantic→visual mapping is exhaustive and GREEN only for proven-real.
 * 2. Chart/candle provenance is dynamic per provider and defaults to non-real.
 * 3. Structural source guards (no-fabrication style): every <Panel> carries an
 *    explicit provenance, live-plane domain components are framed GREEN, and
 *    the known fixture screens stay RED — so a refactor cannot silently strip
 *    or flip the classification.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  PROVENANCE_TONE,
  provenanceTone,
  provenanceBadge,
  candleCardProvenance,
  type CardProvenance,
} from '../cardProvenance';

const SRC = path.resolve(__dirname, '../..');
const read = (rel: string) => readFileSync(path.join(SRC, rel), 'utf8');

describe('provenance → tone mapping', () => {
  it('maps every real class to green and every non-real class to red', () => {
    const green: CardProvenance[] = ['live', 'runtime-config', 'derived-live'];
    const red: CardProvenance[] = ['fixture', 'synthetic', 'replay', 'placeholder', 'mixed', 'unknown'];
    for (const p of green) expect(PROVENANCE_TONE[p], p).toBe('green');
    for (const p of red) expect(PROVENANCE_TONE[p], p).toBe('red');
    // exhaustive: no class exists outside the two lists
    expect(Object.keys(PROVENANCE_TONE).sort()).toEqual([...green, ...red].sort());
  });

  it("chrome ('none') renders no tone and no badge", () => {
    expect(provenanceTone('none')).toBe('none');
    expect(provenanceBadge('none')).toBeNull();
  });

  it('badges say LIVE / NON-LIVE, never anything softer', () => {
    expect(provenanceBadge('live')).toBe('LIVE');
    expect(provenanceBadge('runtime-config')).toBe('LIVE');
    expect(provenanceBadge('fixture')).toBe('NON-LIVE');
    expect(provenanceBadge('mixed')).toBe('NON-LIVE');
    expect(provenanceBadge('unknown')).toBe('NON-LIVE');
  });
});

describe('dynamic candle-card provenance', () => {
  it('is green only for genuine market-data sources', () => {
    expect(provenanceTone(candleCardProvenance('mt5'))).toBe('green');
    expect(provenanceTone(candleCardProvenance('polygon'))).toBe('green');
    expect(provenanceTone(candleCardProvenance('store'))).toBe('green');
  });
  it('is red for every Control-Tower-generated provider', () => {
    for (const p of ['fixture', 'mock_live', 'synthetic', 'replay', 'replay-snapshot']) {
      expect(provenanceTone(candleCardProvenance(p)), p).toBe('red');
    }
  });
  it('defaults to red for unknown/absent providers (never silently green)', () => {
    expect(provenanceTone(candleCardProvenance(undefined))).toBe('red');
    expect(provenanceTone(candleCardProvenance(null))).toBe('red');
    expect(provenanceTone(candleCardProvenance('surprise-provider'))).toBe('red');
  });
});

/* ── structural source guards ────────────────────────────────────────────── */

function allTsx(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      allTsx(full, acc);
    } else if (/\.tsx$/.test(entry) && !/\.test\.tsx$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

describe('structural guards', () => {
  it('every <Panel usage carries an explicit provenance prop', () => {
    // Belt-and-braces beside the required TypeScript prop: catches spreads or
    // any future loosening of the prop type.
    const offenders: string[] = [];
    for (const file of allTsx(SRC)) {
      const text = readFileSync(file, 'utf8');
      const opens = [...text.matchAll(/<Panel[\s>]/g)];
      for (const m of opens) {
        const tag = text.slice(m.index!, text.indexOf('>', m.index!) + 1);
        if (!/provenance(=|\s*=)/.test(tag)) {
          offenders.push(`${path.relative(SRC, file)}: ${tag.slice(0, 80)}`);
        }
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('live-plane domain components are framed in SystemView with honest provenance', () => {
    const s = read('views/SystemView.tsx');
    // Adapter-fed pipelines (projection, runtime loop, ledger) must be DYNAMIC:
    // mock adapter ⇒ synthetic (RED), real adapter ⇒ live (GREEN). A static
    // "live" here would put mock balances inside a GREEN frame.
    for (const comp of ['OperationalDashboard', 'LiveRuntimePanel', 'TradeLedgerPanel']) {
      const mount = s.indexOf(`<${comp}`);
      expect(mount, `${comp} must be mounted`).toBeGreaterThan(-1);
      const before = s.slice(Math.max(0, mount - 250), mount);
      expect(before, `${comp} must use the adapter-dependent frame`)
        .toContain('<ProvenanceFrame provenance={adapterProvenance}');
    }
    // The adapter gate itself must be fail-closed: mock or unknown ⇒ synthetic.
    expect(s).toContain("rt?.broker?.kind && rt.broker.kind !== 'mock'");
    // Operator-owned durable stores and real config remain statically live.
    for (const comp of ['RecommendationPanel', 'SecurityBaselinePanel']) {
      const mount = s.indexOf(`<${comp}`);
      expect(mount, `${comp} must be mounted`).toBeGreaterThan(-1);
      const before = s.slice(Math.max(0, mount - 250), mount);
      expect(before, `${comp} must be framed live`)
        .toContain('<ProvenanceFrame provenance="live"');
    }
  });

  it('fleet fixture deployment tiles are framed RED and Settings flags are not green', () => {
    expect(read('views/FleetOverview.tsx'))
      .toContain('<ProvenanceFrame provenance="fixture"');
    // The Feature Flags settings section must not inherit runtime-config green.
    const gv = read('views/global/GlobalViews.tsx');
    const flags = gv.indexOf('title="Feature Flags"');
    expect(flags).toBeGreaterThan(-1);
    const before = gv.slice(Math.max(0, flags - 200), flags);
    expect(before, 'flags section must be classified placeholder')
      .toContain('provenance="placeholder"');
  });

  it('fixture fleet and fixture trade cards remain RED', () => {
    expect(read('views/FleetOverview.tsx')).toContain('provenance="fixture"');
    const pair = read('views/pair/PairViews.tsx');
    // trades tab + pending orders + fixture panels
    expect((pair.match(/provenance="fixture"/g) ?? []).length).toBeGreaterThanOrEqual(10);
    expect(pair).toContain('provenance="mixed"');            // events feed stays red
  });

  it('risk/accounts, edge monitor, system confidence and broker health stay RED', () => {
    expect(read('views/AccountsProtectionView.tsx')).toContain('provenance="fixture"');
    expect(read('views/BrokerHealthView.tsx')).toContain('provenance="fixture"');
    expect(read('views/EdgeMonitorView.tsx')).toContain('provenance="fixture"');
    const sys = read('views/SystemView.tsx');
    expect(sys).toContain('provenance="fixture"');           // system confidence + fixture panels
    expect(sys).toContain('provenance="placeholder"');       // feature flags card
  });

  it('runtime-config settings sections default GREEN and Storage & Feeds is live', () => {
    const gv = read('views/global/GlobalViews.tsx');
    // SettingsSection defaults to runtime-config; only explicit overrides
    // (e.g. the placeholder flags section) may downgrade it.
    expect(gv).toContain("provenance = 'runtime-config'");
    expect(gv).toContain('provenance={provenance}');
    expect(read('views/SystemView.tsx')).toContain('provenance="live"');
  });

  it('chart cards use dynamic candle provenance, never a static green', () => {
    expect(read('views/global/GlobalViews.tsx')).toContain('candleCardProvenance(');
    expect(read('views/pair/PairViews.tsx')).toContain('candleCardProvenance(');
  });
});
