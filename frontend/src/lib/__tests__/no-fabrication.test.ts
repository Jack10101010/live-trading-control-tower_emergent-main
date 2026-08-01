/**
 * UI-0 — regression guards against fabricated operational truth reappearing.
 *
 * These read source files deliberately. The claims being guarded were hardcoded
 * JSX literals and a silent `.catch()` fallback, so a behavioural test cannot see
 * them; a source assertion is the honest way to keep them from returning.
 */
import { describe, it, expect } from 'vitest';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import path from 'node:path';

const SRC = path.resolve(__dirname, '../..');
const read = (rel: string) => readFileSync(path.join(SRC, rel), 'utf8');

function allSourceFiles(dir = SRC, acc: string[] = []): string[] {
  for (const entry of readdirSync(dir)) {
    const full = path.join(dir, entry);
    if (statSync(full).isDirectory()) {
      if (entry === '__tests__' || entry === 'data') continue;
      allSourceFiles(full, acc);
    } else if (/\.(ts|tsx)$/.test(entry) && !/\.test\.tsx?$/.test(entry)) {
      acc.push(full);
    }
  }
  return acc;
}

describe('hardcoded operational claims are gone', () => {
  it('no longer claims MongoDB is connected (there is no MongoDB client)', () => {
    const s = read('views/SystemView.tsx');
    expect(s).not.toContain('MongoDB');
  });

  it('no longer hardcodes engine component versions', () => {
    const s = read('views/SystemView.tsx');
    for (const lit of ['regime@2.3.0', 'policy-engine@1.4.0', 'exec-policy@2.0.0', 'protection@1.2.0', 'strategy-brain@1.1.0']) {
      expect(s).not.toContain(lit);
    }
  });

  it('no longer hardcodes feed latency, lag or connection claims', () => {
    const s = read('views/global/GlobalViews.tsx');
    for (const lit of ['42 ms', '40s lag', 'All feeds via MT5']) {
      expect(s).not.toContain(lit);
    }
    // SystemView carried the same fabricated "40s lag" row.
    expect(read('views/SystemView.tsx')).not.toContain('40s lag');
  });

  it('no longer hardcodes snapshot-integrity statistics', () => {
    const s = read('views/global/GlobalViews.tsx');
    for (const lit of ["'1,382'", "'4.2 GB'", "'100%'"]) {
      expect(s).not.toContain(lit);
    }
  });

  it('no longer shows a hardcoded application version banner', () => {
    expect(read('components/shell/CommandSafetyBar.tsx')).not.toContain('v1.0.0');
  });

  it('shows a persistent global data-source badge instead', () => {
    const s = read('components/shell/CommandSafetyBar.tsx');
    expect(s).toContain('data-source-badge');
    expect(s).toContain('deriveDataSourceBadge');
  });
});

describe('no silent fallbacks', () => {
  it('does not swallow policy-matrix failures into a fabricated grid', () => {
    const s = read('hooks/useRepository.ts');
    // M-PKG-1/M-REC-1: the hook no longer fetches the matrix at all. Its cells
    // were derived from WORLD packages and no package registry exists, so not
    // fetching is strictly stronger than not swallowing a failure.
    expect(s).not.toContain('api.policyMatrix(');
    expect(s).toContain('there is no matrix');
  });

  it('does not derive the health timestamp from the fixture world', () => {
    // The backend contract test covers the server side; this guards the client type.
    const s = read('lib/api.ts');
    expect(s).toContain('serverTime');
    expect(s).toContain('liveNodeConnected');
  });
});

describe('placeholder instruments stay inert', () => {
  it('labels the future-pair slots as placeholders and keeps them non-interactive', () => {
    const s = read('components/shell/ScopeNavigator.tsx');
    expect(s).toContain('Placeholders · not configured');
    expect(s).toContain('aria-disabled="true"');
    expect(s).toContain('cursor-not-allowed');
    expect(s).not.toContain('No deployment yet — instrument slot ready');
    // No creation affordance may appear here.
    expect(s).not.toMatch(/onClick=\{[^}]*create/i);
  });
});

describe('UI-0 introduces no live-execution path', () => {
  it('never references arming, order submission or MT5 credentials anywhere in the frontend', () => {
    const forbidden = [
      'ArmRuntime',
      'arm_request',
      'arm_runtime',
      'verify_and_arm',
      'order_send',
      'open_position',
      'MT5_PASSWORD',
      'MT5_LOGIN',
      'LIVE_SUBMIT_DISABLED',
    ];
    const offenders: string[] = [];
    for (const file of allSourceFiles()) {
      const text = readFileSync(file, 'utf8');
      for (const needle of forbidden) {
        if (text.includes(needle)) offenders.push(`${path.relative(SRC, file)} → ${needle}`);
      }
    }
    expect(offenders).toEqual([]);
  });
});
