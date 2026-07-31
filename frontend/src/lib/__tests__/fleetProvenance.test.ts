/**
 * M-FLEET-2 — the provenance gate, and the structural guards that keep fixture
 * fleet data off ordinary operator routes.
 *
 * The behavioural half tests the gate itself. The structural half is the part
 * that has to survive future edits: a repository-wide assertion that no ordinary
 * component imports the fixture-preview hook or calls `/api/fleet`. One missed
 * import is one invented balance back on an operator's screen.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import {
  PROV_LIVE_MT5,
  PROV_MOCK_FIXTURE,
  PROV_ABSENT,
  isAuthoritative,
  authoritativeOnly,
  classify,
  numericOrNull,
} from '../operationalProvenance';

const SRC = path.resolve(__dirname, '../..');

/** The exact account the mock adapter projects — the record that must be dropped. */
const MOCK_FIXTURE_ACCOUNT = {
  accountFingerprint: 'acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F',
  balance: 100000.0,
  equity: 100412.0,
  provenance: PROV_MOCK_FIXTURE,
  freshness: { available: true, stale: false, status: 'ok' },
};

const LIVE_ACCOUNT = {
  accountFingerprint: 'fp_real',
  balance: 4211.5,
  equity: 4180.25,
  realizedPnLToday: null,
  provenance: PROV_LIVE_MT5,
  freshness: { available: true, stale: false, status: 'ok' },
};

describe('the provenance gate', () => {
  it('rejects mock-fixture records even though they look operational', () => {
    // This is the whole milestone: the record arrives from /api/operations/*
    // with a healthy freshness envelope and the SAME invented balance the
    // fixture serves. Endpoint identity is not provenance.
    expect(isAuthoritative(MOCK_FIXTURE_ACCOUNT)).toBe(false);
    expect(authoritativeOnly([MOCK_FIXTURE_ACCOUNT])).toEqual([]);
  });

  it('accepts live_mt5 records exactly as supplied', () => {
    expect(isAuthoritative(LIVE_ACCOUNT)).toBe(true);
    expect(authoritativeOnly([LIVE_ACCOUNT])).toEqual([LIVE_ACCOUNT]);
  });

  it('fails closed on unknown, absent or missing provenance', () => {
    for (const p of [PROV_ABSENT, 'synthetic', 'replay', 'fixture', 'mock', 'LIVE_MT5', '']) {
      expect(isAuthoritative({ provenance: p }), p).toBe(false);
    }
    expect(isAuthoritative({})).toBe(false);
    expect(isAuthoritative(null)).toBe(false);
    expect(isAuthoritative(undefined)).toBe(false);
  });

  it('never merges authoritative and non-authoritative records', () => {
    const mixed = [MOCK_FIXTURE_ACCOUNT, LIVE_ACCOUNT, { provenance: 'synthetic' }];
    const kept = authoritativeOnly(mixed);
    expect(kept).toEqual([LIVE_ACCOUNT]);
    // A dropped record leaves NO trace — not even a zeroed placeholder.
    expect(JSON.stringify(kept)).not.toContain('100000');
    expect(JSON.stringify(kept)).not.toContain('100412');
  });
});

describe('status classification — empty is not unavailable', () => {
  it('reports unavailable when the source did not answer', () => {
    expect(classify([], { sourceAnswered: false })).toBe('unavailable');
  });

  it('reports empty when the source answered with genuinely nothing', () => {
    expect(classify([], { sourceAnswered: true, rejectedCount: 0 })).toBe('empty');
  });

  it('reports unavailable — not empty — when everything was rejected', () => {
    // Under the mock adapter the source answers, but nothing survives the gate.
    // Saying "no accounts exist" there would be a fabrication of a new kind.
    expect(classify([], { sourceAnswered: true, rejectedCount: 2 })).toBe('unavailable');
  });

  it('distinguishes available from stale', () => {
    expect(classify([LIVE_ACCOUNT], { sourceAnswered: true })).toBe('available');
    const stale = { ...LIVE_ACCOUNT, freshness: { stale: true } };
    expect(classify([stale], { sourceAnswered: true })).toBe('stale');
  });
});

describe('null is not zero', () => {
  it('preserves the absence of a measurement', () => {
    expect(numericOrNull(null)).toBeNull();
    expect(numericOrNull(undefined)).toBeNull();
    expect(numericOrNull(NaN)).toBeNull();
    expect(numericOrNull(Infinity)).toBeNull();
    expect(numericOrNull(0)).toBe(0);          // a real zero survives
    expect(numericOrNull(4211.5)).toBe(4211.5);
  });
});

/* ── structural guards ─────────────────────────────────────────────────────── */

function allSources(dir: string, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'node_modules') continue;
      allSources(full, acc);
    } else if (/\.tsx?$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

const rel = (f: string) => path.relative(SRC, f);

/** Doc comments legitimately NAME the retired hooks to explain why they went. */
const stripComments = (t: string) =>
  t.split('\n').filter((l) => {
    const x = l.trim();
    return !x.startsWith('//') && !x.startsWith('*') && !x.startsWith('/*');
  }).join('\n');

/** The only files allowed to touch fixture fleet data. */
const FIXTURE_ALLOWLIST = new Set([
  'lib/api.ts',                         // defines the fetchers
  'hooks/useRepository.ts',             // defines the hooks
  'views/dev/FixtureFleetPreview.tsx',  // isolated dev-only route (M-FLEET-2)
  'views/dev/FixtureTradesPreview.tsx', // isolated dev-only route (M-TRADES-1)
]);

describe('structural isolation of fixture fleet data', () => {
  it('no ordinary component imports the fixture-preview hook', () => {
    const offenders = allSources(SRC)
      .filter((f) => !FIXTURE_ALLOWLIST.has(rel(f)))
      .filter((f) => /useFixtureFleetPreview|useFixtureTradesPreview/.test(readFileSync(f, 'utf8')))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no ordinary source calls /api/fleet', () => {
    const offenders = allSources(SRC)
      .filter((f) => !FIXTURE_ALLOWLIST.has(rel(f)))
      .filter((f) => {
        const t = readFileSync(f, 'utf8');
        return /apiFetch<[^>]*>\('\/fleet'\)/.test(t) || /fetch\(apiUrl\('\/fleet'\)/.test(t)
          || /apiFetch<[^>]*>\(`?\/trades/.test(t);
      })
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('M-TRADES-1: the old fixture-trades hook name no longer exists anywhere', () => {
    // `useTrades` returned fixture live/ghost/blocked records to nine surfaces,
    // including the chart overlay and the analytics engine.
    const offenders = allSources(SRC)
      .filter((f) => /\buseTrades\b/.test(stripComments(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('M-TRADES-1: no ordinary source reads WORLD trade records directly', () => {
    const offenders: string[] = [];
    for (const f of allSources(SRC)) {
      const t = readFileSync(f, 'utf8');
      const code = t.split('\n').filter((l) => !l.trim().startsWith('//') && !l.trim().startsWith('*')).join('\n');
      if (/world\.(liveTrades|ghostTrades|blockedIntents)\b/.test(stripComments(code))) offenders.push(rel(f));
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('M-TRADES-1: the fixture trades preview route is not linked from navigation', () => {
    const offenders = allSources(SRC)
      .filter((f) => rel(f) !== 'App.tsx' && rel(f) !== 'views/dev/FixtureTradesPreview.tsx')
      .filter((f) => stripComments(readFileSync(f, 'utf8')).includes('/dev/fixture-trades'))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('the old fixture-fleet hook name no longer exists anywhere', () => {
    // `useFleet` returned fixture deployments/brokers/accounts to 13 consumers.
    // Its absence is what makes the removal structural rather than per-component.
    const offenders = allSources(SRC)
      .filter((f) => /\buseFleet\b/.test(stripComments(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('the fixture preview route is not linked from any navigation', () => {
    const offenders = allSources(SRC)
      .filter((f) => rel(f) !== 'App.tsx' && rel(f) !== 'views/dev/FixtureFleetPreview.tsx')
      .filter((f) => stripComments(readFileSync(f, 'utf8')).includes('/dev/fixture-fleet'))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('provenance is compared in ONE module, not scattered across components', () => {
    const offenders = allSources(SRC)
      // `lib/api.ts` DECLARES the provenance union as a type; declaring the
      // vocabulary is not comparing against it, which is what must stay central.
      .filter((f) => rel(f) !== 'lib/operationalProvenance.ts' && rel(f) !== 'lib/api.ts')
      .filter((f) => /['"]live_mt5['"]|['"]mock-fixture['"]/.test(readFileSync(f, 'utf8')))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no ordinary source reads WORLD fleet records directly', () => {
    // The endpoint-level severing did NOT catch `useDeploymentManifest`, which
    // read `world.deployments/accounts/brokers` straight from the fixture world.
    // This guard exists because that bypass was found only by manual review.
    const offenders: string[] = [];
    for (const f of allSources(SRC)) {
      const t = readFileSync(f, 'utf8');
      const code = t.split('\n').filter((l) => !l.trim().startsWith('//') && !l.trim().startsWith('*')).join('\n');
      if (/world\.(deployments|accounts|brokers)\b/.test(code)) offenders.push(rel(f));
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no fabricated fleet sentinel values survive in ordinary source', () => {
    // The fixture's own numbers and identifiers. Their absence outside the
    // allowlist is the blunt, checkable form of "the records are gone".
    const SENTINELS = ['dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ', 'acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F',
                       'brk_01J8Z3E5G7J9M1P3R5T7V9X1Z3B', 'FTMO', 'MT5-Demo',
                       // M-TRADES-1 fixture trade/ghost/blocked identifiers
                       'tr_01J8ZC4H2N8M6K1E3R5T7V9W0XZ', 'gh_01J8ZC9F5P2Q7R4T6V8X0Z1A3BC',
                       'blk_01J8ZCD7K3M9N1P4R6T8V0X2Z4A', 'coid_01J8ZC3F1N5Q8T1W4Z7A0D3G6J',
                       '88213', '88190', 'TE-50'];
    const offenders: string[] = [];
    for (const f of allSources(SRC)) {
      const t = readFileSync(f, 'utf8');
      for (const s of SENTINELS) if (t.includes(s)) offenders.push(`${rel(f)}: ${s}`);
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });
});
