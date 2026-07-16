/**
 * orderBlocks — the Order Block DATA SOURCE + pure zone styling (Phase 17).
 *
 * Two concerns, both pure and read-only:
 *   1. `deriveOrderBlocks(instrument, candles)` — the swappable PROVIDER. Today it is
 *      a deterministic, fixture-backed stand-in (anchored to the engine's candle
 *      series so blocks sit on the visible price). It is NOT a market-structure
 *      detector — it picks fixed recent bars and does no swing/BOS analysis. The real
 *      detector (the fixture already carries one real block in `signals[].structureEvent`,
 *      obId `OB-2261`) drops in by replacing THIS function behind `useOrderBlocks`;
 *      ChartWorkspace and OrderBlockLayer never change. The `OrderBlock` shape mirrors
 *      the fixture `structureEvent` (obId / levels / direction / time) so that swap is 1:1.
 *   2. `orderBlockZone(ob, ctx)` — turns one block into a `ChartZone` (or null when the
 *      block has not yet formed at the replay cursor). This is the ONLY styling logic;
 *      it reuses the existing zone primitive — no custom rendering, no second overlay.
 */
import { isoToUnix, type Candle, type ChartZone } from '@/lib/chartData';

export interface OrderBlock {
  id: string;
  instrument: string;
  direction: 'bullish' | 'bearish';
  top: number;
  bottom: number;
  formedAtISO: string;
  /** When price returned into the block (supplied by the source; the layer only
   *  thresholds it against replay time — it computes no mitigation itself). */
  mitigatedAtISO?: string | null;
}

const round5 = (x: number) => Math.round(x * 1e5) / 1e5;
const unixToISO = (unix: number) => new Date(unix * 1000).toISOString();

/**
 * Deterministic fixture-backed provider (swap point for the real detector). Anchors a
 * small set of blocks to the most recent bars so they render on the current feed, with
 * formation times spread across the (replay) window so replay reveals them over time,
 * alternating direction, and one deterministic mitigation. No RNG, fully reproducible.
 */
export function deriveOrderBlocks(instrument: string, candles: Candle[]): OrderBlock[] {
  if (candles.length < 6) return [];
  const n = candles.length;
  const anchors = [n - 4, n - 3, n - 2]; // recent bars → boxes sit on the visible candles
  return anchors.map((idx, k) => {
    const bar = candles[idx];
    const bullish = k % 2 === 0; // alternate so both bullish + bearish are demonstrated
    const pad = Math.max(0.0004, Math.abs(bar.high - bar.low) * 0.5);
    // bullish = demand zone at the bar low; bearish = supply zone at the bar high.
    const top = round5(bullish ? bar.low + pad * 0.4 : bar.high + pad);
    const bottom = round5(bullish ? bar.low - pad : bar.high - pad * 0.4);
    // The oldest block mitigates partway through the window (deterministic).
    const mitigatedAtISO = k === 0 ? unixToISO(candles[n - 2].time) : null;
    return {
      id: `OB-${1000 + idx}`,
      instrument,
      direction: bullish ? 'bullish' : 'bearish',
      top,
      bottom,
      formedAtISO: unixToISO(bar.time),
      mitigatedAtISO,
    };
  });
}

/**
 * One order block → one `ChartZone`, or null if it has not formed by the cursor.
 * Bullish/bearish → colour; mitigated → faded (lower opacity). Hover title = label.
 * Pure; uses only the existing zone primitive.
 *
 * Visibility + mitigation use the LOGICAL time (replay cursor, else now), so replay
 * reveals/mitigates blocks over time. The box's right EDGE is clamped to the last
 * candle, since a zone can only be drawn where candle data exists.
 */
export function orderBlockZone(
  ob: OrderBlock,
  ctx: { cursor?: number; candles: Candle[]; nowISO: string }
): ChartZone | null {
  const formed = isoToUnix(ob.formedAtISO);
  const nowTime = ctx.cursor ?? isoToUnix(ctx.nowISO); // logical time (replay/live)
  if (formed > nowTime) return null; // replay hides not-yet-formed order blocks
  // Right edge = the latest candle at/under the logical time. It MUST be a real bar
  // time — `timeToCoordinate` returns null between bars — so the box always renders.
  let right = formed;
  for (const c of ctx.candles) if (c.time <= nowTime && c.time > right) right = c.time;
  const mitigated = ob.mitigatedAtISO != null && isoToUnix(ob.mitigatedAtISO) <= nowTime;
  const base = ob.direction === 'bullish' ? 'var(--positive)' : 'var(--negative)';
  const opacityPct = mitigated ? 7 : 17; // mitigated blocks fade
  return {
    time0: formed,
    time1: right,
    price0: ob.bottom,
    price1: ob.top,
    color: `color-mix(in srgb, ${base} ${opacityPct}%, transparent)`,
    label: `OB ${ob.id} · ${ob.direction} · ${mitigated ? 'mitigated' : 'active'}`,
  };
}
