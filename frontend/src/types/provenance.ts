/**
 * Data provenance (UI-0) — the single vocabulary for "where did this number come
 * from". Every operator-facing value that is not genuinely emitted by a live node
 * or a real production source must carry one of these, and the UI must render it.
 *
 * Rule: `live-node` is reserved for values a running execution node actually
 * emitted. Nothing in the Control Tower may claim it by computing a value itself
 * (Architecture V1 invariant I-7 — "CT displays; the node decides").
 */
export type DataProvenance =
  | 'live-node'      // emitted by a running execution node (never computed here)
  | 'market-feed'    // real market data from a provider/historical store
  | 'fixture'        // the frozen fixture world (world.v1) — a demonstration universe
  | 'synthesized'    // deterministically generated in the Control Tower for design/demo
  | 'placeholder'    // deliberate stand-in; no data source is wired yet
  | 'unavailable'    // a real source exists but returned nothing / failed
  | 'stale';         // real data whose freshness contract has lapsed

/** Short uppercase chip text. Never longer than a few characters on screen. */
export const PROVENANCE_LABEL: Record<DataProvenance, string> = {
  'live-node': 'LIVE',
  'market-feed': 'MARKET DATA',
  fixture: 'FIXTURE',
  synthesized: 'SYNTHESIZED',
  placeholder: 'NOT WIRED',
  unavailable: 'UNAVAILABLE',
  stale: 'STALE',
};

/** One-line explanation shown on hover; keep operator-safe (no stack traces). */
export const PROVENANCE_HINT: Record<DataProvenance, string> = {
  'live-node': 'Emitted by a live execution node.',
  'market-feed': 'Real market data from the configured provider or historical store.',
  fixture: 'From the frozen fixture world — a demonstration universe, not live truth.',
  synthesized: 'Generated in the Control Tower for design/demo. Not strategy-engine output.',
  placeholder: 'No data source is wired yet. Displayed value is a stand-in.',
  unavailable: 'The real source failed or returned nothing.',
  stale: 'Real data, but past its freshness contract.',
};

/** Visual tone. `trusted` is deliberately narrow: only genuinely real sources. */
export type ProvenanceTone = 'trusted' | 'caution' | 'inert' | 'critical';

export const PROVENANCE_TONE: Record<DataProvenance, ProvenanceTone> = {
  'live-node': 'trusted',
  'market-feed': 'trusted',
  fixture: 'caution',
  synthesized: 'caution',
  placeholder: 'inert',
  unavailable: 'critical',
  stale: 'critical',
};

/** True when a value may be read as real operational truth. */
export function isTrustedProvenance(p: DataProvenance): boolean {
  return PROVENANCE_TONE[p] === 'trusted';
}
