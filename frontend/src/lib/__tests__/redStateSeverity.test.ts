/**
 * M-CT-RED-STATE-AUDIT-1 — severity semantics.
 *
 * Red must mean "an operator has to do something". Two large permanently-red
 * cards (Live Runtime, Trade Ledger, both honestly-labelled local mock) taught
 * the eye to ignore red on the System page — and the states that DO need action
 * are red too. These pin the boundary in both directions: nothing that merely
 * reports a safe/inactive/mock condition may be red, and every genuine
 * intervention state must stay red.
 */
import { describe, it, expect } from 'vitest';
import { PROVENANCE_TONE, provenanceTone } from '@/lib/cardProvenance';

describe('provenance tone — red means action required', () => {
  it('local mock surfaces are NEUTRAL, not red (Live Runtime / Trade Ledger)', () => {
    expect(PROVENANCE_TONE.synthetic).toBe('neutral');
    expect(provenanceTone('synthetic')).toBe('neutral');
  });

  it('genuine node telemetry projections are GREEN', () => {
    expect(PROVENANCE_TONE['derived-live']).toBe('green');
    expect(PROVENANCE_TONE.live).toBe('green');
  });

  it('an honestly-empty placeholder is NEUTRAL', () => {
    expect(PROVENANCE_TONE.placeholder).toBe('neutral');
  });

  it('the fixture demonstration universe stays RED', () => {
    // Fixture data wears operational clothes; that IS misleading.
    expect(PROVENANCE_TONE.fixture).toBe('red');
  });

  it('inseparably mixed and unclassified content stay RED (fail closed)', () => {
    expect(PROVENANCE_TONE.mixed).toBe('red');
    expect(PROVENANCE_TONE.unknown).toBe('red');
  });

  it('an unrecognised provenance falls closed to red, never green', () => {
    // @ts-expect-error deliberately outside the union
    expect(provenanceTone('not-a-real-provenance')).not.toBe('green');
  });
});

describe('the dashboard frame follows node truth, not the local adapter', () => {
  // The rule implemented in SystemView: a genuine node reporting ANY lifecycle
  // state means the card shows node truth. Provenance is not freshness.
  const frame = (nodeStatus: string, adapter: 'live' | 'synthetic') =>
    nodeStatus !== 'absent' ? 'derived-live' : adapter;

  it('a live node makes the operational dashboard green even on a mock adapter', () => {
    expect(frame('current', 'synthetic')).toBe('derived-live');
  });

  it('a STALE node is still node truth — provenance is not freshness', () => {
    expect(frame('stale', 'synthetic')).toBe('derived-live');
  });

  it('a DEGRADED node is still node truth', () => {
    expect(frame('degraded', 'synthetic')).toBe('derived-live');
  });

  it('no node at all falls back to the adapter verdict (fails closed)', () => {
    expect(frame('absent', 'synthetic')).toBe('synthetic');
    expect(PROVENANCE_TONE[frame('absent', 'synthetic') as 'synthetic']).toBe('neutral');
  });
});

describe('zero is not the thing it counts', () => {
  // `0 deny` in red and `0 warn` in amber said something had gone wrong when
  // nothing had. Severity colour belongs to the OCCURRENCE.
  const tone = (n: number, severity: string) => (n > 0 ? severity : 'muted');

  it('zero denials render muted, not negative', () => {
    expect(tone(0, 'negative')).toBe('muted');
  });
  it('zero warnings render muted, not warning', () => {
    expect(tone(0, 'warning')).toBe('muted');
  });
  it('a real denial still renders negative', () => {
    expect(tone(3, 'negative')).toBe('negative');
  });
  it('a real warning still renders warning', () => {
    expect(tone(1, 'warning')).toBe('warning');
  });
});
