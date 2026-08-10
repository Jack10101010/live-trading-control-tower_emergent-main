/**
 * Card-level data provenance (temporary migration aid).
 *
 * While the fixture→live migration is underway, every significant
 * data-presenting card carries a semantic provenance class. The TEMPORARY
 * visual system collapses these to GREEN (real) / RED (non-real) — but the
 * richer classification is preserved here so the migration can later retire
 * the binary rendering without losing information.
 *
 * Governance rules (docs/governance/CONTROL-TOWER-DATA-DEPENDENCY-AUDIT.md):
 *   - unknown or mixed provenance is RED until proven otherwise;
 *   - client-derived analytics inherit the provenance of their INPUT data;
 *   - a card backed by a real live endpoint that honestly shows an
 *     empty/unavailable state stays GREEN;
 *   - a provenance chip inside a card never makes the card itself green.
 */

/** Semantic card provenance. Do NOT collapse to a boolean. */
export type CardProvenance =
  | 'live'            // durable stores, node telemetry, real runtime state
  | 'runtime-config'  // actual persisted/runtime configuration (operator prefs, security config)
  | 'derived-live'    // computed purely from live/runtime-config inputs
  | 'fixture'         // the WORLD demonstration universe
  | 'synthetic'       // deterministically generated (mock_live, synthetic candles)
  | 'replay'          // replay-generated content
  | 'placeholder'     // honest stand-in awaiting implementation
  | 'mixed'           // real and non-real inseparably combined
  | 'unknown';        // unclassified — treated as non-real by default

/** Panels that are pure chrome (nav, forms without data) opt out explicitly. */
export type PanelProvenance = CardProvenance | 'none';

export type ProvenanceTone = 'green' | 'neutral' | 'red' | 'none';

/** The temporary visual collapse. GREEN only for proven-real classes. */
/**
 * M-PROVENANCE-FINAL — three tones, because absence is not fabrication.
 *
 * The original mapping collapsed `placeholder` into red alongside `fixture` and
 * `synthetic`. That was right while most cards still held invented data: red
 * meant "do not trust this". Now that the fixture content is gone, the same red
 * would tell an operator that an honestly-empty card is as untrustworthy as a
 * fabricated balance — and when everything is red, red stops meaning anything.
 *
 * So `placeholder` becomes NEUTRAL. It is reserved for cards that make no
 * operational claim and say so. Red is now reserved for cards that DO show
 * non-operational data: fixture, synthetic, replay, mixed, and unknown (which
 * fails closed). The semantic taxonomy is unchanged — only the tone mapping —
 * so nothing is collapsed back into a boolean.
 */
export const PROVENANCE_TONE: Record<CardProvenance, ProvenanceTone> = {
  live: 'green',
  'runtime-config': 'green',
  'derived-live': 'green',
  // Neutral: no operational claim is made, and the card says so.
  placeholder: 'neutral',
  // Red: the card genuinely displays non-operational data.
  fixture: 'red',
  // M-CT-RED-STATE-AUDIT-1 — synthetic is NEUTRAL, and only synthetic.
  //
  // Red must mean "an operator has to do something". The local mock adapter
  // requires no action: it is the expected development posture, it is already
  // labelled NON-LIVE on the card, and the node it sits beside is live. Two
  // large permanently-red cards (Live Runtime, Trade Ledger) taught the eye to
  // ignore red on this page — which is the real cost, because the states that
  // DO need action (frozen, identity mismatch, reconciliation fault) are red
  // too and were being read as more of the same.
  //
  // `fixture` stays red: it is the demonstration universe wearing operational
  // clothes, and that IS misleading. `mixed` and `unknown` stay red because
  // neither can be shown to be safe. Only the honestly-labelled deterministic
  // local surface moves.
  synthetic: 'neutral',
  replay: 'red',
  mixed: 'red',
  unknown: 'red',
};

export function provenanceTone(p: PanelProvenance): ProvenanceTone {
  if (p === 'none') return 'none';
  return PROVENANCE_TONE[p];
}

/** Small header badge text. */
export function provenanceBadge(p: PanelProvenance): string | null {
  const tone = provenanceTone(p);
  if (tone === 'none') return null;
  if (tone === 'green') return 'LIVE';
  // NOT WIRED states a missing capability; NON-LIVE warns about shown data.
  return tone === 'neutral' ? 'NOT WIRED' : 'NON-LIVE';
}

/**
 * Dynamic provenance for candle-driven cards (charts and everything derived
 * from candles: order blocks, FVGs, liquidity pools, market structure).
 * GREEN only when candles originate from a genuine live or durable real
 * market-data source; anything Control-Tower-generated is non-real.
 */
export function candleCardProvenance(provider: string | null | undefined): CardProvenance {
  switch (provider) {
    case 'mt5':
      return 'live';
    case 'polygon':
    case 'store':
      return 'derived-live';       // real market data (provider / durable store)
    case 'fixture':
      return 'fixture';
    case 'mock_live':
    case 'synthetic':
      return 'synthetic';
    case 'replay':
    case 'replay-snapshot':
      return 'replay';
    default:
      return 'unknown';            // default-red until proven otherwise
  }
}
