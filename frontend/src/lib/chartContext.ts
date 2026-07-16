/**
 * ChartContext — the SINGLE authoritative model describing what a chart is showing
 * (Phase: Chart Status System). Everything that explains a chart to the operator —
 * the status strip, the future Chart Inspector, screenshot provenance, and audit
 * export — is a *projection of this one model*. No consumer re-derives status; they
 * render `deriveChartContext(...)`.
 *
 * Ownership: this model is derived in ChartWorkspace (which owns mode + the market
 * data hook + timeframe + candles). ChartPanel stays a dumb renderer and never sees
 * any of this — no provider, cache, replay, strategy, or execution concepts leak
 * into it.
 *
 * V1 honesty rule: only DATA is populated, and only with facts that are actually
 * true today. STRATEGY / EXECUTION / SYSTEM are typed extension points, left
 * undefined until the real data exists. We never imply a capability we don't have
 * (e.g. replay is currently live-fetched and non-deterministic — we say exactly that).
 */
import { Activity, History, Award, GitCompareArrows, Compass, type LucideIcon } from 'lucide-react';
import { type Candle } from '@/lib/chartData';
import { type MarketCandles } from '@/lib/api';

// ── Modes ────────────────────────────────────────────────────────────────────
// A proper enum-like union (not a boolean). Not all modes are mounted yet; the
// registry below prepares colour + label + icon for each so new modes are a
// data change, not a UI rewrite.
export type ChartMode =
  | 'live-trading'
  | 'replay'
  | 'golden-replay'
  | 'shadow-replay'
  | 'historical-explorer';

export interface ChartModeMeta {
  label: string;
  /** Identity colour — the mode segment now, a chart frame later. */
  color: string;
  icon: LucideIcon;
  blurb: string;
}

export const CHART_MODES: Record<ChartMode, ChartModeMeta> = {
  'live-trading': {
    label: 'Live Trading',
    color: 'var(--positive)',
    icon: Activity,
    blurb: 'Live operator surface. Data trust and liveness are reported separately below.',
  },
  'replay': {
    label: 'Replay',
    color: '#a855f7',
    icon: History,
    blurb: 'Cursor-driven playback of a recorded session window.',
  },
  'golden-replay': {
    label: 'Golden Replay',
    color: '#d4af37',
    icon: Award,
    blurb: 'Deterministic regression baseline (requires pinned snapshot — not yet available).',
  },
  'shadow-replay': {
    label: 'Shadow Replay',
    color: 'var(--negative)',
    icon: GitCompareArrows,
    blurb: 'Re-runs a live session bar-for-bar (requires pinned snapshot — not yet available).',
  },
  'historical-explorer': {
    label: 'Historical Explorer',
    color: 'var(--primary)',
    icon: Compass,
    blurb: 'Read-only browsing of historical candles. No execution.',
  },
};

/** Map the legacy `mode` prop ('live' | 'replay') to a ChartMode. Call sites may
 *  pass an explicit ChartMode later (e.g. Market Data → historical-explorer). */
export function resolveChartMode(mode: 'live' | 'replay', override?: ChartMode): ChartMode {
  return override ?? (mode === 'replay' ? 'replay' : 'live-trading');
}

// ── Vocabularies (kept small so the strip stays glanceable) ──────────────────
export type DataSourceKind =
  | 'polygon' | 'mt5' | 'store' | 'replay-snapshot' | 'fixture' | 'synthetic' | 'unknown';

/** TRUST = "can I trust what's in the pipe?" — deliberately SEPARATE from liveness. */
export type TrustLevel = 'trusted' | 'delayed' | 'cached' | 'fallback' | 'stale' | 'unavailable';

/** LIVENESS = "is the pipe moving?" — orthogonal to trust. A chart can be live
 *  (polling) while showing fallback data, or static (replay) while perfectly trusted. */
export type Liveness = 'live' | 'idle' | 'static';

export type WarnSeverity = 'info' | 'caution' | 'critical';
export interface ChartWarning {
  id: string;
  severity: WarnSeverity;
  text: string;
}

// ── The model ────────────────────────────────────────────────────────────────
export interface ChartDataContext {
  source: DataSourceKind;
  sourceLabel: string;
  trust: TrustLevel;
  trustLabel: string;
  liveness: Liveness;
  livenessLabel: string;
  updating: boolean;
  timeframe: string;
  barCount: number;
  requestedCount: number;
  complete: boolean;
  firstBarMs: number | null;
  lastBarMs: number | null;
  lastBarAgeS: number | null;
  cache: { cached: boolean; ageS: number | null };
  polygonStatus: string | null;
  loading: boolean;
}

/** Provenance is grouped here (not scattered) so a screenshot stamp / audit export
 *  can consume one object. Snapshot/version fields are extension points — null in V1. */
export interface ChartProvenance {
  mode: ChartMode;
  source: DataSourceKind;
  instrument: string;
  timeframe: string;
  capturedAtMs: number;
  lastBarMs: number | null;
  requestId: string | null;
  replayDataVersion: string | null; // extension point (replay snapshot pinning)
  snapshotHash: string | null;      // extension point (deterministic replay not built)
}

// Extension-point segments — typed but intentionally NOT populated in V1.
export interface ChartStrategyContext {
  strategy: string; version: string; packageLane: string;
  detectionTimeframe: string | null; executionTimeframe: string | null;
  configVersion: string | null;
}
export interface ChartExecutionContext {
  openOrders: number; positionState: string | null; broker: string | null;
}
export interface ChartSystemContext {
  providerAvailable: boolean; connection: string | null; realtime: string | null;
}

export interface ChartContext {
  mode: ChartMode;
  data: ChartDataContext;
  provenance: ChartProvenance;
  warnings: ChartWarning[];
  strategy?: ChartStrategyContext;   // ← reserved
  execution?: ChartExecutionContext; // ← reserved
  system?: ChartSystemContext;       // ← reserved
}

// ── Derivation — the ONE place interpretation lives ──────────────────────────
export interface DeriveChartInput {
  mode: ChartMode;
  instrument: string;
  timeframe: string;
  feed: MarketCandles | undefined; // raw response (carries diagnostics)
  candles: Candle[];               // resolved bars actually handed to ChartPanel
  requestedCount: number;
  isLive: boolean;                 // does this mode poll the live edge?
  nowMs: number;                   // snapshot of wall clock (passed, not read in render)
}

const STEP_S: Record<string, number> = { M1: 60, M5: 300, M15: 900, H1: 3600, H4: 14400, D1: 86400 };

function sourceInfo(feed: MarketCandles | undefined, mode: ChartMode): { kind: DataSourceKind; label: string } {
  const src = feed?.source ?? null;      // 'polygon' | 'store' | null
  const provider = feed?.provider ?? null; // 'replay' | 'fixture' | 'mt5' | ...
  // Replay is currently LIVE-FETCHED, not a pinned snapshot — say so explicitly.
  if (mode === 'replay' || mode === 'golden-replay' || mode === 'shadow-replay') {
    const under = src === 'store' ? 'Store' : src === 'polygon' ? 'Polygon' : '—';
    return { kind: 'polygon', label: `Live-fetched · ${under}` };
  }
  if (provider === 'mt5') return { kind: 'mt5', label: 'MT5' };
  if (src === 'polygon') return { kind: 'polygon', label: 'Polygon' };
  if (src === 'store') return { kind: 'store', label: 'Historical Store' };
  if (provider === 'fixture') return { kind: 'fixture', label: 'Fixture' };
  return { kind: 'unknown', label: '—' };
}

const TRUST_LABEL: Record<TrustLevel, string> = {
  // "Direct" (not "Live") for trusted — trust describes PROVENANCE, and must not
  // collide with the separate LIVENESS word. On the free end-of-day tier the newest
  // bar is legitimately hours old; that's conveyed by liveness (idle) + the shown
  // bar age, NOT by a red "stale" alarm that would cry wolf every session.
  trusted: 'Direct', delayed: 'Delayed', cached: 'Cached',
  fallback: 'Fallback', stale: 'Stale', unavailable: 'No data',
};

export function deriveChartContext(input: DeriveChartInput): ChartContext {
  const { mode, instrument, timeframe, feed, candles, requestedCount, isLive, nowMs } = input;
  const stepS = STEP_S[timeframe] ?? 900;
  const loading = feed === undefined;
  const barCount = candles.length;
  const firstBarMs = barCount ? candles[0].time * 1000 : null;
  const lastBarMs = barCount ? candles[barCount - 1].time * 1000 : null;
  const lastBarAgeS = lastBarMs != null ? Math.max(0, Math.round((nowMs - lastBarMs) / 1000)) : null;

  const { kind, label: sourceLabel } = sourceInfo(feed, mode);
  const fellBack = !!feed?.fellBack;
  const staleLive = !!feed?.staleLive;
  const cacheHit = !!feed?.cacheHit;
  const polygonStatus = feed?.polygonStatus ?? null;
  const cacheAgeS = feed?.cacheAgeSeconds ?? null;

  // ── TRUST (independent of liveness) — precedence: worst wins. Reflects only
  //    concrete provenance flags, never wall-clock age (an EOD feed is legitimately
  //    hours behind; staleness is conveyed by liveness + the shown bar age instead). ──
  let trust: TrustLevel;
  if (loading || barCount === 0) trust = 'unavailable';
  else if (fellBack) trust = 'fallback';
  else if (staleLive) trust = 'cached';           // serving last-good Polygon (rate-limited)
  else if (polygonStatus === 'DELAYED') trust = 'delayed';
  else if (cacheHit) trust = 'cached';
  else trust = 'trusted';

  // ── LIVENESS (independent of trust) ────────────────────────────────────────
  let liveness: Liveness;
  if (!isLive) liveness = 'static';               // replay / historical — no polling
  else if (lastBarAgeS != null && lastBarAgeS <= stepS * 2) liveness = 'live';
  else liveness = 'idle';                          // polling, but nothing fresh arriving
  const updating = liveness === 'live';
  const livenessLabel = liveness === 'live' ? 'Updating' : liveness === 'idle' ? 'Idle' : 'Static';

  // ── WARNINGS — only abnormal conditions; silence = healthy ─────────────────
  const warnings: ChartWarning[] = [];
  if (loading) {
    warnings.push({ id: 'loading', severity: 'info', text: 'Loading market feed' });
  } else if (barCount === 0) {
    warnings.push({ id: 'nodata', severity: 'critical', text: 'No candles from feed' });
  }
  if (fellBack) warnings.push({ id: 'fallback', severity: 'caution', text: 'Historical Store fallback — Polygon unavailable' });
  if (staleLive) warnings.push({ id: 'ratelimited', severity: 'caution', text: 'Polygon rate limited — showing last good candles' });
  if (!loading && barCount > 0 && barCount < requestedCount * 0.75) {
    warnings.push({ id: 'partial', severity: 'caution', text: `Partial history (${barCount} of ${requestedCount} bars)` });
  }
  if (mode === 'replay') {
    warnings.push({ id: 'replay-live', severity: 'caution', text: 'Replay uses live-fetched candles — not deterministic yet' });
  } else if (mode === 'golden-replay' || mode === 'shadow-replay') {
    warnings.push({ id: 'replay-nonpinned', severity: 'critical', text: 'No pinned snapshot — reproducibility not guaranteed' });
  }

  const data: ChartDataContext = {
    source: kind, sourceLabel, trust, trustLabel: TRUST_LABEL[trust],
    liveness, livenessLabel, updating, timeframe, barCount, requestedCount,
    complete: barCount >= requestedCount * 0.9,
    firstBarMs, lastBarMs, lastBarAgeS,
    cache: { cached: cacheHit, ageS: cacheAgeS },
    polygonStatus, loading,
  };

  const provenance: ChartProvenance = {
    mode, source: kind, instrument, timeframe, capturedAtMs: nowMs, lastBarMs,
    requestId: feed?.requestId ?? null,
    replayDataVersion: null, // extension point
    snapshotHash: null,      // extension point (deterministic replay not built)
  };

  return { mode, data, provenance, warnings };
}
