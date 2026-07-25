import type { DataProvenance } from '@/types/provenance';
/**
 * chartData — pure adapters that turn domain objects (trades, ghosts, sessions)
 * into ChartPanel props. Keeps ChartPanel dumb (props-only) and avoids
 * duplicating conversion logic across Replay / Trades / Inspector / Analytics.
 *
 * Candle OHLC is NOT generated here (Phase 16): every chart sources its candle
 * series from the Market Data Engine via `useMarketCandles` → ChartWorkspace. This
 * module only produces overlay primitives (markers/zones/priceLines) from candles
 * + domain data. There is exactly one candle pipeline, and it is the backend engine.
 */
import { parseScenarioKey } from '@/lib/utils';
import type { LiveTrade, GhostTrade } from '@/types/domain';

export interface Candle {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}
export interface ChartMarker {
  time: number;
  position: 'aboveBar' | 'belowBar' | 'inBar';
  color: string;
  shape: 'arrowUp' | 'arrowDown' | 'circle' | 'square';
  text?: string;
}
export interface ChartZone {
  time0: number;
  time1: number;
  price0?: number;
  price1?: number;
  color: string;
  label?: string;
  fullHeight?: boolean;
}
export interface ChartPriceLine {
  price: number;
  color: string;
  label: string;
  lineStyle?: 'solid' | 'dashed';
}
export interface LinePoint {
  time: number;
  value: number;
}

export function isoToUnix(iso: string): number {
  return Math.floor(new Date(iso).getTime() / 1000);
}

/**
 * UI-0 — SYNTHETIC "range activity", NOT feed volume. The market-data contract
 * carries no volume field, so this is derived from (high - low). It must never be
 * presented as genuine traded volume: it is off by default and, where a caller
 * opts in, the series is titled with SYNTHETIC_VOLUME_LABEL.
 */
export const SYNTHETIC_VOLUME_PROVENANCE: DataProvenance = 'synthesized';
export const SYNTHETIC_VOLUME_LABEL = 'Synthetic range activity';

export function deriveVolume(candles: Candle[]): Array<{ time: number; value: number; color: string }> {
  return candles.map((c) => ({
    time: c.time,
    value: Math.round((c.high - c.low) * 1e6),
    color: c.close >= c.open ? 'rgba(63,178,127,0.35)' : 'rgba(229,86,91,0.35)',
  }));
}

function directionOf(scenarioKey: string): 'long' | 'short' {
  return parseScenarioKey(scenarioKey).direction === 'short' ? 'short' : 'long';
}

/** Entry / Stop / Target price lines for a trade. */
export function tradePriceLines(trade: LiveTrade): ChartPriceLine[] {
  const lines: ChartPriceLine[] = [
    { price: trade.entry, color: 'var(--primary)', label: 'Entry', lineStyle: 'solid' },
    { price: trade.sl, color: 'var(--negative)', label: 'Stop', lineStyle: 'dashed' },
    { price: trade.tp, color: 'var(--positive)', label: 'Target', lineStyle: 'dashed' },
  ];
  return lines;
}

/** Entry + Exit markers for a set of trades. */
export function tradeMarkers(trades: LiveTrade[]): ChartMarker[] {
  const markers: ChartMarker[] = [];
  for (const t of trades) {
    const long = directionOf(t.scenarioKey) === 'long';
    markers.push({
      time: isoToUnix(t.openedAt),
      position: long ? 'belowBar' : 'aboveBar',
      color: long ? 'var(--positive)' : 'var(--negative)',
      shape: long ? 'arrowUp' : 'arrowDown',
      text: `Entry ${t.entry}`,
    });
    if (t.closedAt && t.closePrice != null) {
      markers.push({
        time: isoToUnix(t.closedAt),
        position: 'inBar',
        color: 'var(--text-2)',
        shape: 'square',
        text: t.realizedR != null ? `Exit ${t.realizedR >= 0 ? '+' : ''}${t.realizedR}R` : 'Exit',
      });
    }
  }
  return markers;
}

/** Ghost-trade markers (visually distinct — hollow circles). */
export function ghostMarkers(ghosts: GhostTrade[], atISO: string): ChartMarker[] {
  return ghosts.map((g) => ({
    time: isoToUnix(atISO),
    position: 'aboveBar' as const,
    color: 'var(--ghost)',
    shape: 'circle' as const,
    text: `Ghost ${g.ghostModelId} ${g.ghostOutcome}`,
  }));
}

/** Active-trade highlight zone: SL→TP band across the trade's life. */
export function activeTradeZone(trade: LiveTrade, nowISO: string): ChartZone {
  return {
    time0: isoToUnix(trade.openedAt),
    time1: isoToUnix(trade.closedAt ?? nowISO),
    price0: Math.min(trade.sl, trade.tp),
    price1: Math.max(trade.sl, trade.tp),
    color: 'color-mix(in srgb, var(--primary) 10%, transparent)',
    label: 'Active',
  };
}

const SESSION_BOUNDS: Array<[number, number, string]> = [
  [0, 3, 'Asia'],
  [3, 8, 'London'],
  [8, 10, 'Lull'],
  [10, 17, 'NewYork'],
  [17, 24, 'Outside'],
];
function sessionOf(unix: number): string {
  const h = new Date(unix * 1000).getUTCHours();
  for (const [a, b, s] of SESSION_BOUNDS) if (h >= a && h < b) return s;
  return 'Outside';
}
const SESSION_TINT: Record<string, string> = {
  Asia: 'rgba(142,138,192,0.05)',
  London: 'rgba(76,130,247,0.05)',
  Lull: 'rgba(110,118,131,0.04)',
  NewYork: 'rgba(63,178,127,0.05)',
  Outside: 'rgba(110,118,131,0.02)',
};

/** Contiguous session bands (full-height zones) over the candle series. */
export function sessionBands(candles: Candle[]): ChartZone[] {
  if (candles.length === 0) return [];
  const zones: ChartZone[] = [];
  let start = candles[0].time;
  let cur = sessionOf(candles[0].time);
  for (let i = 1; i < candles.length; i++) {
    const s = sessionOf(candles[i].time);
    if (s !== cur) {
      zones.push({ time0: start, time1: candles[i].time, color: SESSION_TINT[cur], label: cur, fullHeight: true });
      start = candles[i].time;
      cur = s;
    }
  }
  zones.push({ time0: start, time1: candles[candles.length - 1].time, color: SESSION_TINT[cur], label: cur, fullHeight: true });
  return zones;
}
