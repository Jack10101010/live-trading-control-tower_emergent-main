/**
 * fairValueGaps — the Fair Value Gap DATA SOURCE + pure zone styling (Phase 18).
 *
 * The exact same shape as `orderBlocks.ts` (copied, generalized, adapted for FVGs):
 *   1. `deriveFairValueGaps(instrument, candles)` — the swappable PROVIDER. A
 *      deterministic, fixture-backed stand-in anchored to the engine's candle series
 *      (so gaps sit on the visible price). It is NOT a market-structure detector — it
 *      picks fixed recent bars and does no 3-candle imbalance scanning. The real
 *      detector drops in by replacing THIS function behind `useFairValueGaps`;
 *      ChartWorkspace and FairValueGapLayer never change.
 *   2. `fairValueGapZone(fvg, ctx)` — turns one gap into a `ChartZone` (or null when the
 *      gap has not yet formed at the replay cursor). The ONLY styling logic; it reuses
 *      the existing zone primitive — no custom rendering, no second overlay system.
 */
import { isoToUnix, type Candle, type ChartZone } from '@/lib/chartData';

export interface FairValueGap {
  id: string;
  instrument: string;
  direction: 'bullish' | 'bearish';
  top: number;
  bottom: number;
  formedAtISO: string;
  /** When price traded back into the gap (supplied by the source; the layer only
   *  thresholds it against replay time — it computes no fill detection itself). */
  filledAtISO?: string | null;
}

const round5 = (x: number) => Math.round(x * 1e5) / 1e5;
const unixToISO = (unix: number) => new Date(unix * 1000).toISOString();

/**
 * Deterministic fixture-backed provider (swap point for the real detector). Anchors a
 * small set of gaps to recent bars so they render on the current feed, with formation
 * times spread across the (replay) window so replay reveals them over time, alternating
 * direction, and one deterministic fill. No RNG, fully reproducible. Gaps sit around the
 * bar body midpoint — distinct from Order Blocks (which sit at the bar high/low).
 */
export function deriveFairValueGaps(instrument: string, candles: Candle[]): FairValueGap[] {
  if (candles.length < 6) return [];
  const n = candles.length;
  const anchors = [n - 5, n - 4, n - 2]; // recent bars → gaps sit on the visible candles
  return anchors.map((idx, k) => {
    const bar = candles[idx];
    const bullish = k % 2 === 0; // alternate so both bullish + bearish are demonstrated
    const gap = Math.max(0.0003, Math.abs(bar.close - bar.open) * 0.5);
    const mid = (bar.open + bar.close) / 2;
    // Thin imbalance band around the body midpoint (bullish leans up, bearish down).
    const top = round5(bullish ? mid + gap : mid + gap * 0.4);
    const bottom = round5(bullish ? mid - gap * 0.4 : mid - gap);
    // The oldest gap fills partway through the window (deterministic).
    const filledAtISO = k === 0 ? unixToISO(candles[n - 2].time) : null;
    return {
      id: `FVG-${2000 + idx}`,
      instrument,
      direction: bullish ? 'bullish' : 'bearish',
      top,
      bottom,
      formedAtISO: unixToISO(bar.time),
      filledAtISO,
    };
  });
}

/**
 * One fair value gap → one `ChartZone`, or null if it has not formed by the cursor.
 * Bullish/bearish → colour; filled → faded (lower opacity). Hover title = label.
 * Pure; uses only the existing zone primitive.
 *
 * Visibility + fill use the LOGICAL time (replay cursor, else now), so replay
 * reveals/fills gaps over time. The box's right EDGE is clamped to the latest candle,
 * since a zone can only be drawn where candle data exists (`timeToCoordinate` returns
 * null between bars).
 */
export function fairValueGapZone(
  fvg: FairValueGap,
  ctx: { cursor?: number; candles: Candle[]; nowISO: string }
): ChartZone | null {
  const formed = isoToUnix(fvg.formedAtISO);
  const nowTime = ctx.cursor ?? isoToUnix(ctx.nowISO); // logical time (replay/live)
  if (formed > nowTime) return null; // replay hides not-yet-formed gaps
  let right = formed;
  for (const c of ctx.candles) if (c.time <= nowTime && c.time > right) right = c.time;
  const filled = fvg.filledAtISO != null && isoToUnix(fvg.filledAtISO) <= nowTime;
  const base = fvg.direction === 'bullish' ? 'var(--positive)' : 'var(--negative)';
  const opacityPct = filled ? 6 : 14; // filled gaps fade
  return {
    time0: formed,
    time1: right,
    price0: fvg.bottom,
    price1: fvg.top,
    color: `color-mix(in srgb, ${base} ${opacityPct}%, transparent)`,
    label: `FVG ${fvg.id} · ${fvg.direction} · ${filled ? 'filled' : 'open'}`,
  };
}
