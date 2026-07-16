/**
 * ChartContext — the SINGLE authoritative model describing what a chart is showing.
 * Everything that explains a chart (status strip, future Chart Inspector, screenshot
 * provenance, audit export) is a projection of this one model. No consumer re-derives
 * status; they render `deriveChartContext(...)`.
 *
 * Ownership: derived in ChartWorkspace (which owns mode + market data hook + timeframe
 * + candles). ChartPanel stays a dumb renderer and never sees any of this.
 *
 * Three axes are kept structurally SEPARATE and must never be collapsed:
 *   WORKSPACE MODE  — what this surface is for (monitor / explorer / replay …)
 *   EXECUTION MODE  — what authority exists (disabled / simulation / paper / live-armed)
 *   DATA STATE      — where the candles came from and whether they can be trusted
 * "Live Trading" is NOT a workspace mode: monitoring a delayed Polygon feed with no
 * execution adapter is Market Monitor + Execution:Disabled, and saying otherwise
 * would claim authority the system does not have.
 *
 * Honesty rule: only facts that are true today. STRATEGY/SYSTEM stay undefined until
 * real data exists; provenance snapshot fields stay null until deterministic replay.
 */
import { Activity, History, Award, GitCompareArrows, Compass, type LucideIcon } from 'lucide-react';
import { type Candle } from '@/lib/chartData';
import { type MarketCandles } from '@/lib/api';
import { type WorkspaceMode, type BarWindowPolicy, totalRequestedBars } from '@/lib/chartWindowPolicy';

export type { WorkspaceMode };

export interface WorkspaceModeMeta {
  label: string;
  color: string;
  icon: LucideIcon;
  blurb: string;
}

export const WORKSPACE_MODES: Record<WorkspaceMode, WorkspaceModeMeta> = {
  'market-monitor': {
    label: 'Market Monitor',
    color: 'var(--primary)',
    icon: Activity,
    blurb: 'Monitoring a market feed. No execution authority — see Execution.',
  },
  'historical-explorer': {
    label: 'Historical Explorer',
    color: 'var(--text-2)',
    icon: Compass,
    blurb: 'Read-only browsing of historical candles.',
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
    blurb: 'Deterministic regression baseline (requires pinned snapshot).',
  },
  'shadow-replay': {
    label: 'Shadow Replay',
    color: 'var(--negative)',
    icon: GitCompareArrows,
    blurb: 'Re-runs a live session bar-for-bar (requires pinned snapshot).',
  },
};

/** Map the legacy `mode` prop. 'live' means "not replay" — which is NOT live trading;
 *  it is market monitoring until an execution adapter is bound and armed. */
export function resolveWorkspaceMode(mode: 'live' | 'replay', override?: WorkspaceMode): WorkspaceMode {
  return override ?? (mode === 'replay' ? 'replay' : 'market-monitor');
}

// ── EXECUTION AUTHORITY (separate axis from workspace mode) ──────────────────
export type ExecutionMode = 'disabled' | 'simulation' | 'paper' | 'live-armed';

export interface ExecutionModeMeta { label: string; color: string }
export const EXECUTION_MODES: Record<ExecutionMode, ExecutionModeMeta> = {
  disabled: { label: 'Disabled', color: 'var(--text-muted)' },
  simulation: { label: 'Simulation', color: 'var(--primary)' },
  paper: { label: 'Paper', color: 'var(--warning)' },
  'live-armed': { label: 'Live · Armed', color: 'var(--negative)' },
};

export interface ChartExecutionContext {
  mode: ExecutionMode;
  label: string;
  /** True ONLY when a live strategy instance AND a live execution adapter are both
   *  active and armed. Nothing in the platform can set this yet. */
  armed: boolean;
  note: string | null;
}

/** Honest default: no strategy instance and no execution adapter are bound to any
 *  chart today, so execution authority is Disabled. Extension point: pass a real
 *  ChartExecutionContext once an execution binding exists. */
export const EXECUTION_DISABLED: ChartExecutionContext = {
  mode: 'disabled',
  label: EXECUTION_MODES.disabled.label,
  armed: false,
  note: 'No strategy instance or execution adapter bound',
};

// ── DATA ─────────────────────────────────────────────────────────────────────
export type DataSourceKind =
  | 'polygon' | 'mt5' | 'store' | 'replay-snapshot' | 'fixture' | 'synthetic' | 'unknown';

/** TRUST = "can I trust what's in the pipe?" — SEPARATE from liveness. */
export type TrustLevel = 'trusted' | 'delayed' | 'cached' | 'fallback' | 'stale' | 'unavailable';
/** LIVENESS = "is the pipe moving?" — orthogonal to trust. */
export type Liveness = 'live' | 'idle' | 'static';

export type WarnSeverity = 'info' | 'caution' | 'critical';
export interface ChartWarning { id: string; severity: WarnSeverity; text: string }

/** Everything about the displayed window, so the operator never has to guess whether
 *  a bar count is a deliberate window, all available data, or a truncated response. */
export interface ChartWindowState {
  policyId: string;
  policyName: string;
  requestedBars: number;   // window proper
  warmupBars: number;
  totalRequested: number;  // window + warm-up = what we asked the backend for
  baseBars: number;        // bars the backend returned for that request
  backfilledBars: number;  // extra older bars pulled in by scrolling
  displayedBars: number;   // what ChartPanel is actually rendering
  maxBars: number;
  allowBackfill: boolean;
  partial: boolean;
  partialReason: string | null;
}

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
  firstBarMs: number | null;
  lastBarMs: number | null;
  lastBarAgeS: number | null;
  cache: { cached: boolean; ageS: number | null };
  polygonStatus: string | null;
  lastSuccessAtMs: number | null;
  lastFailedAtMs: number | null;
  providerNote: string | null;
  window: ChartWindowState;
  loading: boolean;
}

export interface ChartProvenance {
  mode: WorkspaceMode;
  executionMode: ExecutionMode;
  source: DataSourceKind;
  instrument: string;
  timeframe: string;
  capturedAtMs: number;
  lastBarMs: number | null;
  requestId: string | null;
  windowPolicy: string;
  replayDataVersion: string | null; // extension point
  snapshotHash: string | null;      // extension point (deterministic replay not built)
}

// Extension points — typed, NOT populated in V1.
export interface ChartStrategyContext {
  strategy: string; version: string; packageLane: string;
  detectionTimeframe: string | null; executionTimeframe: string | null;
  configVersion: string | null;
}
export interface ChartSystemContext {
  providerAvailable: boolean; connection: string | null; realtime: string | null;
}

export interface ChartContext {
  mode: WorkspaceMode;
  execution: ChartExecutionContext;
  data: ChartDataContext;
  provenance: ChartProvenance;
  warnings: ChartWarning[];
  strategy?: ChartStrategyContext; // ← reserved
  system?: ChartSystemContext;     // ← reserved
}

// ── Derivation — the ONE place interpretation lives ──────────────────────────
export interface DeriveChartInput {
  mode: WorkspaceMode;
  instrument: string;
  timeframe: string;
  feed: MarketCandles | undefined;
  candles: Candle[];      // total displayed (base + backfilled)
  baseBars: number;       // bars returned for the base window
  policy: BarWindowPolicy;
  isLive: boolean;
  nowMs: number;
  execution?: ChartExecutionContext;
}

const STEP_S: Record<string, number> = { M1: 60, M5: 300, M15: 900, H1: 3600, H4: 14400, D1: 86400 };

function sourceInfo(feed: MarketCandles | undefined, mode: WorkspaceMode): { kind: DataSourceKind; label: string } {
  const src = feed?.source ?? null;
  const provider = feed?.provider ?? null;
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
  // "Direct" (not "Live") — trust describes PROVENANCE and must not collide with the
  // separate LIVENESS word. The free end-of-day tier is legitimately hours behind;
  // that is conveyed by liveness + the shown bar age, not a false "stale" alarm.
  trusted: 'Direct', delayed: 'Delayed', cached: 'Cached',
  fallback: 'Fallback', stale: 'Stale', unavailable: 'No data',
};

export function deriveChartContext(input: DeriveChartInput): ChartContext {
  const { mode, instrument, timeframe, feed, candles, baseBars, policy, isLive, nowMs } = input;
  const execution = input.execution ?? EXECUTION_DISABLED;
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

  // ── TRUST — concrete provenance flags only, never wall-clock age ────────────
  let trust: TrustLevel;
  if (loading || barCount === 0) trust = 'unavailable';
  else if (fellBack) trust = 'fallback';
  else if (staleLive) trust = 'cached';
  else if (polygonStatus === 'DELAYED') trust = 'delayed';
  else if (cacheHit) trust = 'cached';
  else trust = 'trusted';

  // ── LIVENESS — independent of trust ────────────────────────────────────────
  let liveness: Liveness;
  if (!isLive) liveness = 'static';
  else if (lastBarAgeS != null && lastBarAgeS <= stepS * 2) liveness = 'live';
  else liveness = 'idle';
  const updating = liveness === 'live';
  const livenessLabel = liveness === 'live' ? 'Updating' : liveness === 'idle' ? 'Idle' : 'Static';

  // ── WINDOW — explain the count, always ─────────────────────────────────────
  const totalRequested = totalRequestedBars(policy);
  const backfilledBars = Math.max(0, barCount - baseBars);
  const partial = !loading && baseBars > 0 && baseBars < totalRequested;
  let partialReason: string | null = null;
  if (partial) {
    if (fellBack) partialReason = 'provider fallback returned a shorter range';
    else if (staleLive) partialReason = 'provider rate-limited — serving cached range';
    else partialReason = 'provider returned fewer bars than requested for this window';
  }
  const window: ChartWindowState = {
    policyId: policy.id, policyName: policy.name,
    requestedBars: policy.requestedBars, warmupBars: policy.warmupBars, totalRequested,
    baseBars, backfilledBars, displayedBars: barCount, maxBars: policy.maxBars,
    allowBackfill: policy.allowBackfill, partial, partialReason,
  };

  // ── WARNINGS — abnormal conditions only; silence = healthy ─────────────────
  const warnings: ChartWarning[] = [];
  if (loading) warnings.push({ id: 'loading', severity: 'info', text: 'Loading market feed' });
  else if (barCount === 0) warnings.push({ id: 'nodata', severity: 'critical', text: 'No candles from feed' });
  if (fellBack) warnings.push({ id: 'fallback', severity: 'caution', text: 'Historical Store fallback — Polygon unavailable' });
  if (staleLive) warnings.push({ id: 'ratelimited', severity: 'caution', text: 'Provider rate-limited · showing cached range' });
  if (partial) {
    warnings.push({ id: 'partial', severity: 'caution', text: `${baseBars} of ${totalRequested} requested · Partial` });
  }
  if (mode === 'replay') {
    warnings.push({ id: 'replay-live', severity: 'caution', text: 'Replay uses live-fetched candles — not deterministic yet' });
  } else if (mode === 'golden-replay' || mode === 'shadow-replay') {
    warnings.push({ id: 'replay-nonpinned', severity: 'critical', text: 'No pinned snapshot — reproducibility not guaranteed' });
  }

  const data: ChartDataContext = {
    source: kind, sourceLabel, trust, trustLabel: TRUST_LABEL[trust],
    liveness, livenessLabel, updating, timeframe, barCount,
    firstBarMs, lastBarMs, lastBarAgeS,
    cache: { cached: cacheHit, ageS: feed?.cacheAgeSeconds ?? null },
    polygonStatus,
    lastSuccessAtMs: feed?.lastSuccessAt ? feed.lastSuccessAt * 1000 : null,
    lastFailedAtMs: feed?.lastFailedAt ? feed.lastFailedAt * 1000 : null,
    providerNote: feed?.providerNote ?? null,
    window, loading,
  };

  const provenance: ChartProvenance = {
    mode, executionMode: execution.mode, source: kind, instrument, timeframe,
    capturedAtMs: nowMs, lastBarMs, requestId: feed?.requestId ?? null,
    windowPolicy: policy.name,
    replayDataVersion: null, // extension point
    snapshotHash: null,      // extension point
  };

  return { mode, execution, data, provenance, warnings };
}
