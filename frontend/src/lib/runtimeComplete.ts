/**
 * M-CT-RUNTIME-COMPLETE-UX-1 — CURRENT RUNTIME and LAST COMPLETE CYCLE are two
 * different facts with two different clocks, and conflating them is what made
 * the System page lie in both directions.
 *
 * The node publishes a transition payload the moment a recompute starts, then a
 * complete payload at cycle end. Only the complete one carries `news` and
 * `decisions`. So during a recompute — measured at ~25 minutes on the live node —
 * the newest runtime publication legitimately contains no strategy context at
 * all. Reading that as "news unknown" is wrong (we know what the last complete
 * cycle said), and reading it as current is worse (it is not).
 *
 * Hence: two ages, never substituted for one another, and a severity model where
 * a healthy recompute is INFORMATIONAL rather than a fault.
 */

export type RuntimeSeverity =
  | 'healthy'        // current, positively verified
  | 'recomputing'    // node is deliberately working; informational
  | 'delayed'        // complete cycle older than expected; runtime still fine
  | 'degraded'       // transport degraded; execution unaffected
  | 'stale'          // runtime beyond its phase budget — genuine attention
  | 'unknown';       // nothing observed yet — never "healthy"

export type Tone = 'green' | 'blue' | 'neutral' | 'amber' | 'red';

/** Red is reserved for states an operator must act on. */
export const SEVERITY_TONE: Record<RuntimeSeverity, Tone> = {
  healthy: 'green',
  recomputing: 'blue',      // deliberate work in progress, not a problem
  delayed: 'amber',
  degraded: 'amber',        // CT delivery — never implies execution is unhealthy
  stale: 'red',
  unknown: 'amber',         // absence is not health, but it is not a fault either
};

export interface StrategyView {
  current?: {
    cycleStatus?: string | null;
    cycleNote?: string | null;
    strategyBlocksPresent?: boolean;
    publishedAt?: string | null;
  };
  lastComplete?: {
    available?: boolean;
    publishedAt?: string | null;
    boundary?: string | null;
    sequence?: number | null;
    ageSeconds?: number | null;
    isCurrent?: boolean;
    news?: Record<string, unknown> | null;
    decisions?: Record<string, unknown> | null;
  };
  delivery?: {
    healthy?: boolean;
    ageSeconds?: number | null;
    lastDeliveryAt?: string | null;
    degradedAfterSeconds?: number | null;
  };
}

/** Node cycle statuses that mean "deliberately busy", not "broken". */
const WORKING = new Set(['recomputing', 'bootstrap', 'recovering']);

/**
 * How old a complete cycle may be before it deserves operator awareness.
 * A recompute plus a full bar interval: beyond that, cycles are not landing.
 * Deliberately NOT the runtime freshness budget — different question.
 */
export const COMPLETE_DELAYED_AFTER_S = 1800;

export function isRecomputing(v: StrategyView): boolean {
  const s = v?.current?.cycleStatus;
  return typeof s === 'string' && WORKING.has(s);
}

/**
 * CURRENT RUNTIME severity. Answers only "what is the node doing now".
 * `runtimeStale` comes from the existing phase-aware freshness authority — this
 * function never re-derives staleness, it consumes the verdict.
 */
export function runtimeSeverity(v: StrategyView, runtimeStale: boolean): RuntimeSeverity {
  if (runtimeStale) return 'stale';                 // fails closed, stays red
  if (!v?.current?.cycleStatus) return 'unknown';
  if (isRecomputing(v)) return 'recomputing';
  return 'healthy';
}

/**
 * LAST COMPLETE severity. Answers only "how current is the strategy context".
 *
 * A healthy recompute in progress must NOT turn this red: the whole point of
 * retaining the last complete cycle is that its age is expected to grow while
 * the node works. It goes amber when cycles genuinely stop landing.
 */
export function completeSeverity(
  v: StrategyView,
  { delayedAfterS = COMPLETE_DELAYED_AFTER_S }: { delayedAfterS?: number } = {}
): RuntimeSeverity {
  const lc = v?.lastComplete;
  if (!lc?.available) return 'unknown';             // never green from absence
  if (lc.isCurrent) return 'healthy';               // this payload IS the complete one
  const age = lc.ageSeconds;
  if (typeof age === 'number' && age > delayedAfterS) return 'delayed';
  return 'healthy';
}

/** CT DELIVERY severity — transport only. Never speaks about execution. */
export function deliverySeverity(v: StrategyView): RuntimeSeverity {
  const d = v?.delivery;
  if (!d || d.ageSeconds == null) return 'unknown';
  return d.healthy ? 'healthy' : 'degraded';
}

/**
 * Provenance label for a strategy block. The card must always say WHICH cycle
 * it is showing — a retained block presented without its age reads as current.
 */
export function strategyProvenance(v: StrategyView): {
  label: string; isCurrent: boolean; refreshing: boolean; ageSeconds: number | null;
} {
  const lc = v?.lastComplete;
  const refreshing = isRecomputing(v) && !v?.current?.strategyBlocksPresent;
  if (!lc?.available) {
    return { label: 'no complete cycle observed', isCurrent: false, refreshing, ageSeconds: null };
  }
  return {
    label: lc.isCurrent ? 'from this cycle' : 'from last complete cycle',
    isCurrent: Boolean(lc.isCurrent),
    refreshing,
    ageSeconds: typeof lc.ageSeconds === 'number' ? lc.ageSeconds : null,
  };
}

/** Compact age, e.g. "38s", "7m", "2h 5m". Null stays null — never "0". */
export function humanAge(seconds: number | null | undefined): string | null {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return null;
  const s = Math.max(0, Math.round(seconds));
  if (s < 90) return `${s}s`;
  const m = Math.round(s / 60);
  if (m < 90) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h ${m % 60}m`;
}

/**
 * `ct.node-decisions.v1` ships missing values as the STRING "nan", not null —
 * verified on a real cycle-end payload. Rendered naively the table shows "nan"
 * in every unresolved cell, so absence is normalised once, here.
 */
export function cleanField(value: unknown): string | null {
  if (value == null) return null;
  const s = String(value).trim();
  if (s === '' || s.toLowerCase() === 'nan' || s.toLowerCase() === 'none') return null;
  return s;
}

/**
 * Is this decision's execution context RESOLVED?
 *
 * Session, market state, cohort and final RR are resolved at FILL/TRIGGER time,
 * not at OB detection. A record with no `fill_time` carries detection-time
 * PROVISIONAL values — its `cohort_key` reads `…|unknown|…` and its state fields
 * are "nan". Presenting those as the executed decision would be a lie, so the
 * table must mark them.
 */
export function isResolved(rec: Record<string, unknown>): boolean {
  return cleanField(rec?.fill_time) != null;
}
