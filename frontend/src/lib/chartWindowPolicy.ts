/**
 * chartWindowPolicy — the ONE place chart history windows are named and owned.
 *
 * Before this module, three views hardcoded three unexplained counts (Replay 220,
 * Market Data 240, Dashboard 180) and the *displayed* total additionally depended on
 * how far the operator had back-scrolled — so 220 / 300 / 648 appeared with nothing
 * telling the operator whether a count was a deliberate window, all available data,
 * a partial response, or a fallback.
 *
 * Different workspaces legitimately want different windows; the rule is only that the
 * defaults are NAMED and live here, never as magic numbers in view components.
 */

/** WORKSPACE MODE — what this surface is FOR. Deliberately does not include a
 *  "live trading" value: live-ness of *execution* is a separate axis (ExecutionMode),
 *  because a chart can monitor a delayed feed with no execution authority at all. */
export type WorkspaceMode =
  | 'market-monitor'
  | 'historical-explorer'
  | 'replay'
  | 'golden-replay'
  | 'shadow-replay';

export interface BarWindowPolicy {
  id: string;
  /** Shown verbatim in the status strip, e.g. "Monitor window". */
  name: string;
  purpose: string;
  /** Bars of the window proper. */
  requestedBars: number;
  /** Extra bars fetched BEFORE the window so indicators/state can prime. */
  warmupBars: number;
  /** Hard ceiling including any back-scrolled history. */
  maxBars: number;
  /** May this workspace load older bars by scrolling? Replay windows are exact. */
  allowBackfill: boolean;
}

export const BAR_WINDOW_POLICIES: Record<WorkspaceMode, BarWindowPolicy> = {
  'market-monitor': {
    id: 'monitor',
    name: 'Monitor window',
    purpose: 'Recent operational context',
    requestedBars: 300,
    warmupBars: 0,
    maxBars: 3000,
    allowBackfill: true,
  },
  'historical-explorer': {
    id: 'explorer',
    name: 'Explorer window',
    purpose: 'Wide inspection range',
    requestedBars: 600,
    warmupBars: 0,
    maxBars: 5000,
    allowBackfill: true,
  },
  'replay': {
    id: 'replay',
    name: 'Replay window',
    purpose: 'Exact replay window plus declared warm-up',
    requestedBars: 220,
    warmupBars: 40,
    maxBars: 260,
    allowBackfill: false, // the replay window is exact — never silently widened
  },
  'golden-replay': {
    id: 'golden',
    name: 'Golden replay window',
    purpose: 'Deterministic regression baseline (requires pinned snapshot)',
    requestedBars: 220,
    warmupBars: 40,
    maxBars: 260,
    allowBackfill: false,
  },
  'shadow-replay': {
    id: 'shadow',
    name: 'Shadow replay window',
    purpose: 'Bar-for-bar re-run of a live session (requires pinned snapshot)',
    requestedBars: 220,
    warmupBars: 40,
    maxBars: 260,
    allowBackfill: false,
  },
};

export function resolveBarWindow(mode: WorkspaceMode): BarWindowPolicy {
  return BAR_WINDOW_POLICIES[mode];
}

/** Bars actually asked of the backend = window + warm-up. */
export function totalRequestedBars(p: BarWindowPolicy): number {
  return p.requestedBars + p.warmupBars;
}
