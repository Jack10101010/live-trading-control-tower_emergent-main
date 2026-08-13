/**
 * M-WORLD-ORDINARY-1 — static enforcement of the ordinary/preview boundary.
 *
 * The behavioural tests prove today's components are clean. These prove that a
 * future edit cannot quietly reconnect them, which is the failure mode that
 * actually happens: `useOperator()` was severed from the fixture in the BACKEND
 * by HARDEN-1, and the frontend kept reading `world.operators[0]` for months
 * afterwards because nothing checked.
 *
 * Import-aware where string matching would produce false positives: a comment
 * naming a retired hook in order to explain its removal is not a call site, and
 * a guard that cannot tell the difference gets weakened the first time it fires
 * on prose.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(__dirname, '../..');

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

/** Strip line and block comments so prose cannot trip a guard about code. */
function codeOnly(text: string): string {
  return text
    .replace(/\/\*[\s\S]*?\*\//g, '')
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

/** The only modules permitted to name fixture-preview machinery. */
const PREVIEW_ALLOWLIST = new Set([
  'lib/api.ts',                          // defines the fetchers
  'hooks/useRepository.ts',              // defines the preview hooks
  'views/dev/FixtureFleetPreview.tsx',
  'views/dev/FixtureTradesPreview.tsx',
]);

describe('no ordinary source reaches the fixture world', () => {
  it('nothing imports or calls the retired `useWorld` / `useRepository`', () => {
    const offenders = allSources(SRC)
      .filter((f) => /\b(useWorld|useRepository)\s*\(/.test(codeOnly(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no source calls `api.world` or names the retired `/world` path', () => {
    const offenders = allSources(SRC)
      .filter((f) => {
        const code = codeOnly(readFileSync(f, 'utf8'));
        return /\bapi\.world\b/.test(code) || /['"`]\/world['"`]/.test(code);
      })
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('only the preview seam may name `/dev/fixture-world`', () => {
    const offenders = allSources(SRC)
      .filter((f) => !PREVIEW_ALLOWLIST.has(rel(f)))
      .filter((f) => /dev\/fixture-world/.test(codeOnly(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no ordinary source reads `world.operators`', () => {
    // The specific expression that put an authored person in the chrome of
    // every page. It must not return under any name.
    const offenders = allSources(SRC)
      .filter((f) => /world\.operators|operators\[0\]/.test(codeOnly(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('every fixture-preview hook is named as one', () => {
    // A preview hook whose name does not say "Fixture" or "Preview" is exactly
    // how `useWorld()` ended up in the application shell.
    const hooks = readFileSync(path.join(SRC, 'hooks/useRepository.ts'), 'utf8');
    const previewFetchers = ['api.fixtureWorldPreview', 'api.fleet', 'api.packages',
                             'api.activePackage', 'api.recommendations',
                             'api.fixtureEventsPreview'];
    const offenders: string[] = [];
    const fnRe = /(?:export )?function (\w+)\(/g;
    let match: RegExpExecArray | null;
    const starts: Array<{ name: string; at: number }> = [];
    while ((match = fnRe.exec(hooks)) !== null) {
      starts.push({ name: match[1], at: match.index });
    }
    starts.forEach((fn, i) => {
      const body = hooks.slice(fn.at, starts[i + 1]?.at ?? hooks.length);
      const used = previewFetchers.filter((p) => body.includes(p));
      if (used.length && !/Fixture|Preview/i.test(fn.name)) {
        offenders.push(`${fn.name} fetches ${used.join(', ')}`);
      }
    });
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('preview hooks are imported only by dev route modules', () => {
    const offenders: string[] = [];
    for (const file of allSources(SRC)) {
      const name = rel(file);
      if (name.startsWith('views/dev/') || PREVIEW_ALLOWLIST.has(name)) continue;
      const code = codeOnly(readFileSync(file, 'utf8'));
      if (/\buseFixture\w+Preview\b/.test(code)) offenders.push(name);
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });
});

describe('no fixture sentinel survives in ordinary shipped source', () => {
  it('no fixture operator identity appears anywhere', () => {
    const offenders: string[] = [];
    for (const file of allSources(SRC)) {
      const text = readFileSync(file, 'utf8');
      for (const sentinel of ['Jack (Lead)', 'op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ']) {
        if (text.includes(sentinel)) offenders.push(`${rel(file)}: ${sentinel}`);
      }
    }
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('no invented role or timezone fallback returns', () => {
    // `?? 'Lead'` and `?? 'UTC'` were invented DEFAULTS layered on top of
    // fixture data — a fabrication built on a fabrication.
    const offenders = allSources(SRC)
      .filter((f) => /\?\?\s*'(Lead|UTC|Admin|Owner)'/.test(codeOnly(readFileSync(f, 'utf8'))))
      .map(rel);
    expect(offenders, offenders.join('\n')).toEqual([]);
  });

  it('the shell renders no avatar derived from a name it does not have', () => {
    const chip = readFileSync(
      path.join(SRC, 'components/shell/OperatorIdentityChip.tsx'), 'utf8');
    expect(codeOnly(chip)).not.toMatch(/charAt\(0\)/);
    expect(codeOnly(chip)).not.toMatch(/displayName/);
  });
});
