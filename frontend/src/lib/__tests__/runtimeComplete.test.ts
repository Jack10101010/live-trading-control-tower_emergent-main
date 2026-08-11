/**
 * M-CT-RUNTIME-COMPLETE-UX-1 — two clocks, two verdicts, no substitution.
 *
 * The failure these prevent: a healthy 25-minute recompute making the page
 * scream, or a retained strategy block being shown as if it were current.
 */
import { describe, it, expect } from 'vitest';
import {
  runtimeSeverity, completeSeverity, deliverySeverity, strategyProvenance,
  isRecomputing, humanAge, cleanField, isResolved,
  SEVERITY_TONE, COMPLETE_DELAYED_AFTER_S,
} from '@/lib/runtimeComplete';

const complete = (over = {}) => ({
  current: { cycleStatus: 'ok', strategyBlocksPresent: true, publishedAt: 'T' },
  lastComplete: { available: true, isCurrent: true, ageSeconds: 5, news: {}, decisions: {} },
  delivery: { healthy: true, ageSeconds: 20 },
  ...over,
});
const recomputing = (ageSeconds = 420) => ({
  current: { cycleStatus: 'recomputing', strategyBlocksPresent: false, publishedAt: 'T' },
  lastComplete: { available: true, isCurrent: false, ageSeconds, news: { health: 'ok' }, decisions: { count: 50 } },
  delivery: { healthy: true, ageSeconds: 30 },
});

describe('runtime vs complete are independent', () => {
  it('a completed cycle is healthy on both', () => {
    expect(runtimeSeverity(complete(), false)).toBe('healthy');
    expect(completeSeverity(complete())).toBe('healthy');
  });

  it('a recompute is INFORMATIONAL, not a fault', () => {
    expect(runtimeSeverity(recomputing(), false)).toBe('recomputing');
    expect(SEVERITY_TONE.recomputing).toBe('blue');
    expect(SEVERITY_TONE.recomputing).not.toBe('red');
  });

  it('a healthy recompute does NOT make the complete cycle red', () => {
    // The whole point: the complete age grows while the node legitimately works.
    expect(completeSeverity(recomputing(600))).toBe('healthy');
    expect(SEVERITY_TONE[completeSeverity(recomputing(600))]).not.toBe('red');
  });

  it('a genuinely delayed complete cycle is AMBER while runtime stays healthy', () => {
    const v = recomputing(COMPLETE_DELAYED_AFTER_S + 60);
    expect(completeSeverity(v)).toBe('delayed');
    expect(SEVERITY_TONE.delayed).toBe('amber');
    expect(runtimeSeverity(v, false)).toBe('recomputing');   // runtime unaffected
  });

  it('stale runtime fails closed to RED regardless of the complete slot', () => {
    expect(runtimeSeverity(complete(), true)).toBe('stale');
    expect(SEVERITY_TONE.stale).toBe('red');
  });

  it('the two ages are never substituted for one another', () => {
    const v = recomputing(900);
    expect(v.lastComplete.ageSeconds).toBe(900);
    expect(v.delivery.ageSeconds).toBe(30);      // delivery is its own clock
  });
});

describe('absence is never health', () => {
  it('no complete cycle observed -> unknown, not healthy', () => {
    expect(completeSeverity({ lastComplete: { available: false } })).toBe('unknown');
    expect(SEVERITY_TONE.unknown).not.toBe('green');
  });
  it('no cycle status -> unknown', () => {
    expect(runtimeSeverity({}, false)).toBe('unknown');
  });
});

describe('CT delivery is transport only', () => {
  it('degraded delivery is amber and says nothing about execution', () => {
    const v = { ...complete(), delivery: { healthy: false, ageSeconds: 1200 } };
    expect(deliverySeverity(v)).toBe('degraded');
    expect(SEVERITY_TONE.degraded).toBe('amber');
    expect(runtimeSeverity(v, false)).toBe('healthy');   // execution unaffected
  });
  it('unobserved delivery is unknown, not healthy', () => {
    expect(deliverySeverity({})).toBe('unknown');
  });
});

describe('strategy provenance is always stated', () => {
  it('during recompute the blocks are labelled as the LAST COMPLETE cycle', () => {
    const p = strategyProvenance(recomputing(420));
    expect(p.label).toBe('from last complete cycle');
    expect(p.isCurrent).toBe(false);
    expect(p.refreshing).toBe(true);
    expect(p.ageSeconds).toBe(420);
  });
  it('at cycle end the blocks are labelled as current', () => {
    const p = strategyProvenance(complete());
    expect(p.label).toBe('from this cycle');
    expect(p.isCurrent).toBe(true);
    expect(p.refreshing).toBe(false);
  });
  it('with nothing observed it says so rather than implying freshness', () => {
    expect(strategyProvenance({ lastComplete: { available: false } }).label)
      .toBe('no complete cycle observed');
  });
  it('bootstrap and recovering also count as working, not broken', () => {
    expect(isRecomputing({ current: { cycleStatus: 'bootstrap' } })).toBe(true);
    expect(isRecomputing({ current: { cycleStatus: 'ok' } })).toBe(false);
  });
});

describe('decision record hygiene (real ct.node-decisions.v1 quirks)', () => {
  it('the string "nan" is absence, not a value', () => {
    expect(cleanField('nan')).toBeNull();
    expect(cleanField('NaN')).toBeNull();
    expect(cleanField('')).toBeNull();
    expect(cleanField(null)).toBeNull();
    expect(cleanField('Bear/Chop')).toBe('Bear/Chop');
  });

  it('an unfilled decision is NOT treated as resolved', () => {
    // Real record: detection-time session present, cohort "unknown", no fill.
    const unfilled = { fill_time: null, cohort_key: 'EURUSD|unknown|bos_long', target_rr: '2.0' };
    expect(isResolved(unfilled)).toBe(false);
  });

  it('a filled decision is resolved', () => {
    expect(isResolved({ fill_time: '2026-08-11 09:00:00+00:00' })).toBe(true);
  });

  it('"nan" fill_time is unresolved, not a timestamp', () => {
    expect(isResolved({ fill_time: 'nan' })).toBe(false);
  });
});

describe('age formatting', () => {
  it('formats compactly and never invents a zero', () => {
    expect(humanAge(38)).toBe('38s');
    expect(humanAge(420)).toBe('7m');
    expect(humanAge(7500)).toBe('2h 5m');
    expect(humanAge(null)).toBeNull();
    expect(humanAge(undefined)).toBeNull();
  });
});
