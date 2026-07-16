/**
 * marketStructure — ONE shared Market Structure DATA SOURCE feeding TWO independent
 * presentation layers (Phase 20). Unlike OB / FVG / Liquidity (one provider per layer),
 * swings AND structure events (BOS / CHOCH) come from a SINGLE provider — the provider
 * owns the data, each layer owns only presentation.
 *
 *   1. `deriveMarketStructure(instrument, candles)` — the swappable PROVIDER. A
 *      deterministic, fixture-backed stand-in anchored to recent bars (so points sit on
 *      the visible price). NOT a real detector — it picks fixed bars and does no ZigZag /
 *      structure analysis. The real detector drops in by replacing THIS function behind
 *      `useMarketStructure`; ChartWorkspace and both layers never change.
 *   2. `swingMarkers(ms, ctx)` — SwingLayer presentation: swings → `ChartMarker[]`.
 *   3. `structureAnnotations(ms, ctx)` — StructureLayer presentation: BOS / CHOCH →
 *      `{ zones, markers }` using existing primitives.
 *
 * Layers NEVER detect structure; they only threshold `formedAtISO` against replay time.
 * Type-only imports keep this module self-contained (node-testable, no `@/` runtime dep).
 */
import type { Candle, ChartMarker, ChartZone } from '@/lib/chartData';

const toUnix = (iso: string) => Math.floor(new Date(iso).getTime() / 1000);
const isoOf = (unix: number) => new Date(unix * 1000).toISOString();
const round5 = (x: number) => Math.round(x * 1e5) / 1e5;
const round2 = (x: number) => Math.round(x * 100) / 100;

export interface Swing {
  id: string;
  type: 'high' | 'low';
  price: number;
  strength: number; // 0..1
  formedAtISO: string;
}

export interface StructureEvent {
  id: string;
  type: 'BOS' | 'CHOCH';
  direction: 'bullish' | 'bearish';
  fromSwing: string; // swing id
  toSwing: string; // swing id
  formedAtISO: string;
}

export interface MarketStructure {
  swings: Swing[];
  structureEvents: StructureEvent[];
}

type Ctx = { cursor?: number; candles: Candle[]; nowISO: string };

/**
 * Deterministic fixture-backed provider (swap point for the real detector). Anchors five
 * alternating high/low swings to recent bars, then connects them into one BOS (recent
 * higher-high) and one CHOCH (recent lower-low), with formation spread across the (replay)
 * window so replay reveals them over time. No RNG, fully reproducible.
 */
export function deriveMarketStructure(instrument: string, candles: Candle[]): MarketStructure {
  if (candles.length < 8) return { swings: [], structureEvents: [] };
  const n = candles.length;
  const anchors = [n - 5, n - 4, n - 3, n - 2, n - 1]; // recent bars → points sit on candles
  const swings: Swing[] = anchors.map((idx, k) => {
    const bar = candles[idx];
    const isHigh = k % 2 === 0; // alternate high / low
    return {
      id: `SW-${4000 + idx}`,
      type: isHigh ? 'high' : 'low',
      price: round5(isHigh ? bar.high : bar.low),
      strength: round2(0.55 + (idx % 3) * 0.15),
      formedAtISO: isoOf(bar.time),
    };
  });

  const highs = swings.filter((s) => s.type === 'high');
  const lows = swings.filter((s) => s.type === 'low');
  const structureEvents: StructureEvent[] = [];
  if (highs.length >= 3) {
    // BOS: the most recent higher-high breaks the prior high (bullish). Forms late.
    const from = highs[1];
    const to = highs[2];
    structureEvents.push({
      id: 'STR-BOS-1', type: 'BOS', direction: 'bullish',
      fromSwing: from.id, toSwing: to.id, formedAtISO: to.formedAtISO,
    });
  }
  if (lows.length >= 2) {
    // CHOCH: a lower-low breaks structure (bearish). Forms partway through the window.
    const from = lows[0];
    const to = lows[1];
    structureEvents.push({
      id: 'STR-CHOCH-1', type: 'CHOCH', direction: 'bearish',
      fromSwing: from.id, toSwing: to.id, formedAtISO: to.formedAtISO,
    });
  }
  return { swings, structureEvents };
}

/** Latest candle time at/under the logical cursor — a real bar coordinate. */
function rightEdge(nowTime: number, candles: Candle[], floor: number): number {
  let right = floor;
  for (const c of candles) if (c.time <= nowTime && c.time > right) right = c.time;
  return right;
}

/**
 * SwingLayer presentation — swings → markers only. Swing highs sit above the bar (red),
 * swing lows below (green). Hides not-yet-formed swings vs the cursor. Pure.
 */
export function swingMarkers(ms: MarketStructure | undefined, ctx: Ctx): ChartMarker[] {
  const nowTime = ctx.cursor ?? toUnix(ctx.nowISO);
  return (ms?.swings ?? [])
    .filter((s) => toUnix(s.formedAtISO) <= nowTime) // replay hides future swings
    .map((s) => ({
      time: toUnix(s.formedAtISO),
      position: s.type === 'high' ? ('aboveBar' as const) : ('belowBar' as const),
      color: s.type === 'high' ? 'var(--negative)' : 'var(--positive)',
      shape: 'circle' as const,
      text: s.type === 'high' ? 'SH' : 'SL',
    }));
}

/**
 * StructureLayer presentation — BOS / CHOCH → a zone (at the broken swing level) plus a
 * break marker, using existing primitives only. Hides not-yet-formed events vs the cursor.
 * The zone right edge is clamped to the latest candle. Pure; never detects structure.
 */
export function structureAnnotations(
  ms: MarketStructure | undefined,
  ctx: Ctx
): { zones: ChartZone[]; markers: ChartMarker[] } {
  const nowTime = ctx.cursor ?? toUnix(ctx.nowISO);
  const byId = new Map((ms?.swings ?? []).map((s) => [s.id, s]));
  const zones: ChartZone[] = [];
  const markers: ChartMarker[] = [];
  for (const ev of ms?.structureEvents ?? []) {
    const formed = toUnix(ev.formedAtISO);
    if (formed > nowTime) continue; // replay hides not-yet-formed BOS / CHOCH
    const from = byId.get(ev.fromSwing);
    const to = byId.get(ev.toSwing);
    if (!from || !to) continue;
    const t0 = toUnix(from.formedAtISO);
    const right = rightEdge(nowTime, ctx.candles, formed);
    const base = ev.direction === 'bullish' ? 'var(--positive)' : 'var(--negative)';
    const label = `${ev.type} ${ev.id} · ${ev.direction}`;
    const band = 0.0002;
    zones.push({
      time0: Math.min(t0, formed),
      time1: Math.max(right, formed),
      price0: round5(to.price - band),
      price1: round5(to.price + band),
      color: `color-mix(in srgb, ${base} ${ev.type === 'BOS' ? 18 : 12}%, transparent)`,
      label,
    });
    markers.push({
      time: formed,
      position: ev.direction === 'bullish' ? ('belowBar' as const) : ('aboveBar' as const),
      color: base,
      shape: ev.direction === 'bullish' ? ('arrowUp' as const) : ('arrowDown' as const),
      text: ev.type,
    });
  }
  return { zones, markers };
}
