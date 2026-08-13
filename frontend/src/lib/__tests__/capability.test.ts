/**
 * M-FLAGS-1 — capability states stay distinct and unknown fails closed.
 *
 * The old model was 17 hardcoded booleans, 16 of them `true`. Each asserted a
 * module was available; nothing consulted the capability. By the end of the
 * honesty programme they had become actively false — `versionHistory: true` for
 * a page reporting "no strategy-package registry exists".
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';
import { isUsable, stateOf, reasonFor, titleFor } from '../capability';

const SRC = path.resolve(__dirname, '../..');
const cap = (state: string, source = 'none', detail = 'd') => ({ state, source, detail }) as never;

describe('only available is usable', () => {
  it('admits available and refuses every other state', () => {
    expect(isUsable(cap('available'))).toBe(true);
    for (const s of ['disabled', 'unsupported', 'unavailable', 'unknown']) {
      expect(isUsable(cap(s)), s).toBe(false);
    }
  });

  it('fails closed on unknown, malformed or absent capabilities', () => {
    expect(isUsable(undefined)).toBe(false);
    expect(isUsable(null)).toBe(false);
    expect(isUsable({} as never)).toBe(false);
    expect(isUsable(cap('AVAILABLE'))).toBe(false);   // case-sensitive, not coerced
    expect(isUsable(cap('enabled'))).toBe(false);     // unrecognised vocabulary
  });

  it('normalises anything unrecognised to unknown rather than guessing', () => {
    expect(stateOf(cap('enabled'))).toBe('unknown');
    expect(stateOf(undefined)).toBe('unknown');
    expect(stateOf(cap('available'))).toBe('available');
  });
});

describe('the four non-usable states never collapse', () => {
  it('gives each its own title', () => {
    const titles = (['disabled', 'unsupported', 'unavailable', 'unknown'] as const).map(titleFor);
    expect(new Set(titles).size).toBe(4);
    // "disabled" implies someone could turn it on. Never say it for the others.
    expect(titleFor('unsupported')).not.toMatch(/disabled/i);
    expect(titleFor('unavailable')).not.toMatch(/disabled/i);
    expect(titleFor('unknown')).not.toMatch(/disabled/i);
  });

  it('explains an unclassifiable capability without claiming it was switched off', () => {
    const reason = reasonFor(cap('enabled'));
    expect(reason).toMatch(/could not be classified/i);
    expect(reason).not.toMatch(/disabled|turned off/i);
  });

  it('carries the backend reason through verbatim', () => {
    expect(reasonFor(cap('unavailable', 'none', 'no strategy-package registry (M-PKG-1)')))
      .toBe('no strategy-package registry (M-PKG-1)');
  });
});

/* ── structural guards ─────────────────────────────────────────────────────── */

function allSources(dir: string, acc: string[] = []): string[] {
  for (const e of readdirSync(dir)) {
    const full = path.join(dir, e);
    if (statSync(full).isDirectory()) {
      if (e === '__tests__' || e === 'node_modules') continue;
      allSources(full, acc);
    } else if (/\.tsx?$/.test(e) && !/\.test\.tsx?$/.test(e)) acc.push(full);
  }
  return acc;
}
const rel = (f: string) => path.relative(SRC, f);
const strip = (t: string) =>
  t.split('\n').filter((l) => {
    const x = l.trim();
    return !x.startsWith('//') && !x.startsWith('*') && !x.startsWith('/*');
  }).join('\n');

describe('structural: capability state is decided in one place', () => {
  it('no component compares capability states inline', () => {
    const offenders = allSources(SRC)
      .filter((f) => rel(f) !== 'lib/capability.ts')
      // Scoped to CAPABILITY state comparisons. `backend === 'unavailable'` in
      // ConnectionPanel is a connection state and a different domain.
      .filter((f) => /\b(cap|capability)\??\.state\s*===/.test(strip(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('the hardcoded boolean flag dict is gone from the backend', () => {
    const server = readFileSync(path.resolve(SRC, '../../backend/server.py'), 'utf8');
    const body = server.split('async def feature_flags(')[1].split('\n@api_router')[0];
    const code = body.split('"""')[2] ?? body;
    // The old shape was `"name": True`. Capabilities now carry state + source.
    expect(code).not.toMatch(/"\w+":\s*(True|False)\s*,/);
    expect(code).toContain('"state"');
    expect(code).toContain('"source"');
  });

  it('no ordinary source treats a raw boolean flag map as capability truth', () => {
    const offenders = allSources(SRC)
      .filter((f) => /Record<FeatureFlag,\s*boolean>/.test(strip(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });
});
