import type { DataProvenance } from '@/types/provenance';
/**
 * chartLayers — the canonical CHART ANNOTATION MODEL and layer registry (Phase 15).
 *
 * This is the single extension point that makes ChartPanel the canonical rendering
 * surface. Every current AND future overlay — order blocks, FVGs, liquidity,
 * BOS/CHOCH, swing points, session shading, trade markers, SL/TP/BE lines, RR tool,
 * execution/broker markers, strategy markers, recommendations, risk & portfolio
 * overlays — is expressed as a `ChartLayer`: a pure producer that turns a shared
 * `LayerContext` into an `AnnotationBundle` (markers / zones / priceLines).
 *
 * Adding an overlay later = add one `ChartLayer` here (and, if it needs new data,
 * one field on `LayerContext` populated in ChartWorkspace). No ChartPanel change,
 * no rendering-pipeline change, no new store. ChartWorkspace merges the enabled
 * layers and hands ONE bundle to ONE ChartPanel — replay and live share the path.
 *
 * Layers are PURE and READ-ONLY. They never fetch, never mutate, never execute.
 * They reuse the existing `chartData` adapters — no duplicate overlay logic.
 */
import {
  tradeMarkers,
  tradePriceLines,
  activeTradeZone,
  ghostMarkers,
  sessionBands,
  isoToUnix,
  type Candle,
  type ChartMarker,
  type ChartZone,
  type ChartPriceLine,
} from '@/lib/chartData';
import { orderBlockZone, type OrderBlock } from '@/lib/orderBlocks';
import { fairValueGapZone, type FairValueGap } from '@/lib/fairValueGaps';
import { liquidityAnnotations, type LiquidityPool } from '@/lib/liquidity';
import { swingMarkers, structureAnnotations, type MarketStructure } from '@/lib/marketStructure';
import type { LiveTrade, GhostTrade } from '@/types/domain';
import type { MarketSnapshot } from '@/lib/api';

/** The three primitive kinds ChartPanel already renders. Every layer emits these. */
export interface AnnotationBundle {
  markers?: ChartMarker[];
  zones?: ChartZone[];
  priceLines?: ChartPriceLine[];
}

/**
 * Everything a layer may read. ChartWorkspace assembles this ONCE from the shared
 * hooks/candle source; layers consume it purely. Extend this (populated in one
 * place) when a future overlay needs a new engine output — that is the additive seam.
 */
export interface LayerContext {
  instrument: string;
  candles: Candle[];
  nowISO: string;
  /** Replay virtual time (unix seconds). Undefined ⇒ live. Same path either way. */
  cursor?: number;
  mode: 'live' | 'replay';
  trades: LiveTrade[];
  ghosts: GhostTrade[];
  /** Current Market Data snapshot (Phase 11) — reserved for future engine layers. */
  snapshot?: MarketSnapshot | null;
  /** Order blocks for this instrument (Phase 17) — supplied by the swappable provider. */
  orderBlocks?: OrderBlock[];
  /** Fair value gaps for this instrument (Phase 18) — supplied by the swappable provider. */
  fairValueGaps?: FairValueGap[];
  /** Liquidity pools for this instrument (Phase 19) — supplied by the swappable provider. */
  liquidityPools?: LiquidityPool[];
  /** Market structure — swings + BOS/CHOCH (Phase 20). ONE provider, TWO layers read it. */
  marketStructure?: MarketStructure;
}

/** Grouping for the layer-toggle UI. Future engine overlays slot under `engine`. */
export type ChartLayerGroup = 'market' | 'trades' | 'engine';

export interface ChartLayer {
  id: string;
  label: string;
  group: ChartLayerGroup;
  defaultOn: boolean;
  /**
   * UI-0 — provenance of what this layer draws. `synthesized` layers are generated
   * in the Control Tower for design/demo and are NOT Lux engine detections; they
   * stay off by default and are labelled in the layer control.
   */
  provenance?: DataProvenance;
  /** Pure: (shared context) → annotation primitives. No side effects. */
  build: (ctx: LayerContext) => AnnotationBundle;
}

// ---------------------------------------------------------------------------
// Shared helpers.
// ---------------------------------------------------------------------------

/** Trades opened by the cursor (replay) or all currently-open trades (live). */
function visibleTrades(ctx: LayerContext): LiveTrade[] {
  if (ctx.cursor == null) return ctx.trades.filter((t) => t.state !== 'closed');
  return ctx.trades.filter((t) => isoToUnix(t.openedAt) <= ctx.cursor!);
}

/** The single trade still open at the cursor / now — drives SL/TP/BE lines + zone. */
function activeTrade(ctx: LayerContext): LiveTrade | undefined {
  const visible = visibleTrades(ctx);
  if (ctx.cursor == null) return visible[0];
  return visible.find((t) => !t.closedAt || isoToUnix(t.closedAt) > ctx.cursor!);
}

// ---------------------------------------------------------------------------
// Layer registry. The CURRENT foundation ships the two overlays that already
// existed (session shading + trades), re-expressed as layers to prove the model.
// Future overlays are appended here — additive, never a chart redesign.
// ---------------------------------------------------------------------------

const sessionLayer: ChartLayer = {
  id: 'session',
  label: 'Sessions',
  group: 'market',
  defaultOn: false,
  build: (ctx) => ({ zones: sessionBands(ctx.candles) }),
};

const tradesLayer: ChartLayer = {
  id: 'trades',
  label: 'Trades',
  group: 'trades',
  defaultOn: false,
  build: (ctx) => {
    const visible = visibleTrades(ctx);
    const active = activeTrade(ctx);
    return {
      markers: tradeMarkers(visible),
      priceLines: active ? tradePriceLines(active) : [],
      zones: active ? [activeTradeZone(active, ctx.nowISO)] : [],
    };
  },
};

const ghostLayer: ChartLayer = {
  id: 'ghosts',
  label: 'Ghosts',
  group: 'trades',
  defaultOn: false,
  build: (ctx) => ({ markers: ghostMarkers(ctx.ghosts, ctx.nowISO) }),
};

/**
 * Order Block layer (Phase 17) — the FIRST production ChartLayer and the reference
 * pattern for every future market-structure overlay (FVG, Liquidity, BOS, CHOCH,
 * Swing …). It is PURE: it reads `ctx.orderBlocks` (supplied by the swappable
 * provider), thresholds each block against the replay cursor (hiding not-yet-formed
 * blocks and fading mitigated ones), and emits zones via the existing primitive.
 * No fetching, no rendering, no mitigation computation of its own.
 */
const orderBlockLayer: ChartLayer = {
  id: 'orderBlocks',
  label: 'Order Blocks (synthetic)',
  provenance: 'synthesized',
  group: 'market',
  defaultOn: false,
  build: (ctx) => ({
    zones: (ctx.orderBlocks ?? [])
      .map((ob) => orderBlockZone(ob, ctx))
      .filter((z): z is ChartZone => z !== null),
  }),
};

/**
 * Fair Value Gap layer (Phase 18) — the SECOND production ChartLayer, an exact copy of
 * `orderBlockLayer` adapted for FVGs. Pure: reads `ctx.fairValueGaps` (swappable
 * provider), hides not-yet-formed gaps vs the replay cursor, fades filled gaps, and
 * emits zones via the existing primitive. No fetching, no rendering, no fill detection.
 */
const fairValueGapLayer: ChartLayer = {
  id: 'fairValueGaps',
  label: 'Fair Value Gaps (synthetic)',
  provenance: 'synthesized',
  group: 'market',
  defaultOn: false,
  build: (ctx) => ({
    zones: (ctx.fairValueGaps ?? [])
      .map((fvg) => fairValueGapZone(fvg, ctx))
      .filter((z): z is ChartZone => z !== null),
  }),
};

/**
 * Liquidity Pool layer (Phase 19) — the THIRD production ChartLayer, the same pattern as
 * Order Blocks / FVGs but emitting BOTH a price line and a shallow zone per pool. Pure:
 * reads `ctx.liquidityPools` (swappable provider), hides not-yet-formed pools vs the
 * replay cursor, dashes + fades swept pools, and emits existing primitives. It NEVER
 * calculates sweeps — `sweptAtISO` is data; the layer only thresholds it against time.
 */
const liquidityLayer: ChartLayer = {
  id: 'liquidity',
  label: 'Liquidity (synthetic)',
  provenance: 'synthesized',
  group: 'market',
  defaultOn: false,
  build: (ctx) => {
    const priceLines: ChartPriceLine[] = [];
    const zones: ChartZone[] = [];
    for (const pool of ctx.liquidityPools ?? []) {
      const b = liquidityAnnotations(pool, ctx);
      if (!b) continue;
      priceLines.push(...b.priceLines);
      zones.push(...b.zones);
    }
    return { priceLines, zones };
  },
};

/**
 * Swing layer (Phase 20) — swing highs/lows as markers only. Reads the SHARED
 * `ctx.marketStructure` (owned by one provider) and hides not-yet-formed swings vs the
 * replay cursor. Pure; no structure detection of its own.
 */
const swingLayer: ChartLayer = {
  id: 'swings',
  label: 'Swings (synthetic)',
  provenance: 'synthesized',
  group: 'market',
  defaultOn: false,
  build: (ctx) => ({ markers: swingMarkers(ctx.marketStructure, ctx) }),
};

/**
 * Structure layer (Phase 20) — BOS / CHOCH as a zone (at the broken level) + a break
 * marker, using existing primitives only. Reads the SAME `ctx.marketStructure` as the
 * swing layer (shared provider, independent presentation). Hides not-yet-formed events.
 */
const structureLayer: ChartLayer = {
  id: 'structure',
  label: 'BOS / CHOCH (synthetic)',
  provenance: 'synthesized',
  group: 'market',
  defaultOn: false,
  build: (ctx) => {
    const a = structureAnnotations(ctx.marketStructure, ctx);
    return { zones: a.zones, markers: a.markers };
  },
};

/**
 * The canonical layer registry. Order = render order (later overlays draw on top):
 * sessions → fair value gaps → order blocks → liquidity → swings → BOS/CHOCH → trades →
 * ghosts. FUTURE (additive, same pattern): execution/broker fills, strategy markers
 * (group 'trades'); risk, portfolio, recommendations, decisions (group 'engine').
 */
export const CHART_LAYERS: ChartLayer[] = [
  sessionLayer,
  fairValueGapLayer,
  orderBlockLayer,
  liquidityLayer,
  swingLayer,
  structureLayer,
  tradesLayer,
  ghostLayer,
];

export const DEFAULT_LAYER_IDS: string[] = CHART_LAYERS.filter((l) => l.defaultOn).map((l) => l.id);

/** Merge the enabled layers' bundles into one bundle for ChartPanel. Pure. */
export function buildAnnotations(enabled: ReadonlySet<string>, ctx: LayerContext): Required<AnnotationBundle> {
  const markers: ChartMarker[] = [];
  const zones: ChartZone[] = [];
  const priceLines: ChartPriceLine[] = [];
  for (const layer of CHART_LAYERS) {
    if (!enabled.has(layer.id)) continue;
    const b = layer.build(ctx);
    if (b.markers) markers.push(...b.markers);
    if (b.zones) zones.push(...b.zones);
    if (b.priceLines) priceLines.push(...b.priceLines);
  }
  return { markers, zones, priceLines };
}
