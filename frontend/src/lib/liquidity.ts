import type { DataProvenance } from '@/types/provenance';
/**
 * liquidity — the Liquidity Pool DATA SOURCE + pure annotation styling (Phase 19).
 *
 * The exact same shape as `orderBlocks.ts` / `fairValueGaps.ts` (copied, generalized):
 *   1. `deriveLiquidityPools(instrument, candles)` — the swappable PROVIDER. A
 *      deterministic, fixture-backed stand-in anchored to recent swing areas (so pools
 *      sit on the visible price). It is NOT a market-structure detector — it picks fixed
 *      recent bars and does no sweep analysis. The real detector drops in by replacing
 *      THIS function behind `useLiquidityPools`; ChartWorkspace and LiquidityLayer never
 *      change.
 *   2. `liquidityAnnotations(pool, ctx)` — turns one pool into an `AnnotationBundle`
 *      slice (a horizontal price line + a shallow zone), or null when the pool has not
 *      yet formed at the replay cursor. The ONLY styling logic; it reuses the existing
 *      `ChartPriceLine` + `ChartZone` primitives — no custom rendering, no second overlay.
 *
 * The layer NEVER calculates sweeps: `sweptAtISO` is DATA supplied here; the layer only
 * thresholds it against replay time (identical to OB mitigation / FVG fill).
 */
import { isoToUnix, type Candle, type ChartZone, type ChartPriceLine } from '@/lib/chartData';

export interface LiquidityPool {
  id: string;
  instrument: string;
  /** buy-side liquidity rests ABOVE highs (red); sell-side rests BELOW lows (green). */
  direction: 'buy-side' | 'sell-side';
  price: number;
  strength: number; // 0..1 — deterministic
  formedAtISO: string;
  sweptAtISO?: string | null;
  active: boolean; // provider flag; true when the pool is not (yet) swept
  /** UI-0 — generated in the Control Tower, NOT detected by the Lux engine. */
  provenance: DataProvenance;
}

const round5 = (x: number) => Math.round(x * 1e5) / 1e5;
const round2 = (x: number) => Math.round(x * 100) / 100;
const unixToISO = (unix: number) => new Date(unix * 1000).toISOString();

/**
 * Deterministic fixture-backed provider (swap point for the real detector). Anchors a
 * small set of pools to recent swing areas (bar highs/lows), alternates buy-side /
 * sell-side, spreads formation across the (replay) window, and deterministically marks
 * one pool as swept. No RNG, fully reproducible.
 */
export function deriveLiquidityPools(instrument: string, candles: Candle[]): LiquidityPool[] {
  if (candles.length < 6) return [];
  const n = candles.length;
  const anchors = [n - 5, n - 4, n - 2]; // recent swing bars → levels sit on the candles
  return anchors.map((idx, k) => {
    const bar = candles[idx];
    const buySide = k % 2 === 0; // alternate buy-side (above highs) / sell-side (below lows)
    const price = round5(buySide ? bar.high + 0.0002 : bar.low - 0.0002);
    const swept = k === 0; // the oldest pool gets swept partway through the window
    return {
      id: `SYN-LQ-${3000 + idx}`,
      provenance: 'synthesized',
      instrument,
      direction: buySide ? 'buy-side' : 'sell-side',
      price,
      strength: round2(0.55 + (idx % 3) * 0.15),
      formedAtISO: unixToISO(bar.time),
      sweptAtISO: swept ? unixToISO(candles[n - 2].time) : null,
      active: !swept,
    };
  });
}

/**
 * One liquidity pool → a price line + a shallow zone (or null if not yet formed by the
 * cursor). Buy-side/sell-side → colour; swept → dashed line + faded zone. Hover title =
 * label. Pure; uses only existing primitives.
 *
 * Visibility + sweep use the LOGICAL time (replay cursor, else now). The zone's right
 * EDGE is clamped to the latest candle (a zone can only be drawn where candles exist);
 * the price line is a full-width level, so it simply appears at formation.
 */
export function liquidityAnnotations(
  pool: LiquidityPool,
  ctx: { cursor?: number; candles: Candle[]; nowISO: string }
): { priceLines: ChartPriceLine[]; zones: ChartZone[] } | null {
  const formed = isoToUnix(pool.formedAtISO);
  const nowTime = ctx.cursor ?? isoToUnix(ctx.nowISO); // logical time (replay/live)
  if (formed > nowTime) return null; // replay hides not-yet-formed liquidity
  let right = formed;
  for (const c of ctx.candles) if (c.time <= nowTime && c.time > right) right = c.time;
  const swept = pool.sweptAtISO != null && isoToUnix(pool.sweptAtISO) <= nowTime;
  // buy-side (above) = red = --negative; sell-side (below) = green = --positive.
  const base = pool.direction === 'buy-side' ? 'var(--negative)' : 'var(--positive)';
  const label = `LIQ ${pool.id} · ${pool.direction} · ${swept ? 'swept' : 'resting'}`;
  // Price line uses a `var()` colour (resolveColor → hex → renders on canvas); swept ⇒ dashed.
  const priceLine: ChartPriceLine = {
    price: pool.price,
    color: base,
    label,
    lineStyle: swept ? 'dashed' : 'solid',
  };
  // Shallow translucent zone around the level (opacity fade encodes swept, like OB/FVG).
  const band = 0.0003 + 0.0004 * pool.strength;
  const zone: ChartZone = {
    time0: formed,
    time1: right,
    price0: round5(pool.price - band),
    price1: round5(pool.price + band),
    color: `color-mix(in srgb, ${base} ${swept ? 4 : 10}%, transparent)`,
    label,
  };
  return { priceLines: [priceLine], zones: [zone] };
}
