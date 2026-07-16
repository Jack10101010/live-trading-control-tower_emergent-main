/**
 * analytics — pure performance math over LiveTrades. No fixture invention:
 * uses `realizedR`, `closedAt`, `scenarioKey`, `lane`, `deploymentId`,
 * `packageHash` — all present on the trade contract.
 */
import type { LiveTrade } from '@/types/domain';
import { parseScenarioKey } from '@/lib/utils';
import { isoToUnix, type LinePoint } from '@/lib/chartData';

export interface PerfMetrics {
  count: number;
  wins: number;
  losses: number;
  winRate: number;
  netR: number;
  avgR: number;
  expectancyR: number;
  profitFactor: number;
  maxDrawdownR: number;
}

export function computeMetrics(trades: LiveTrade[]): PerfMetrics {
  const rs = trades.filter((t) => t.realizedR != null).map((t) => t.realizedR as number);
  const wins = rs.filter((r) => r > 0);
  const losses = rs.filter((r) => r <= 0);
  const netR = rs.reduce((s, r) => s + r, 0);
  const grossWin = wins.reduce((s, r) => s + r, 0);
  const grossLoss = Math.abs(losses.reduce((s, r) => s + r, 0));
  let peak = 0;
  let cum = 0;
  let maxDD = 0;
  for (const r of rs) {
    cum += r;
    peak = Math.max(peak, cum);
    maxDD = Math.min(maxDD, cum - peak);
  }
  return {
    count: rs.length,
    wins: wins.length,
    losses: losses.length,
    winRate: rs.length ? wins.length / rs.length : 0,
    netR,
    avgR: rs.length ? netR / rs.length : 0,
    expectancyR: rs.length ? netR / rs.length : 0,
    profitFactor: grossLoss > 0 ? grossWin / grossLoss : grossWin > 0 ? Infinity : 0,
    maxDrawdownR: maxDD,
  };
}

export function equityCurve(trades: LiveTrade[]): LinePoint[] {
  const closed = trades
    .filter((t) => t.realizedR != null && t.closedAt)
    .slice()
    .sort((a, b) => (a.closedAt! < b.closedAt! ? -1 : 1));
  let cum = 0;
  const pts: LinePoint[] = [];
  for (const t of closed) {
    cum += t.realizedR as number;
    pts.push({ time: isoToUnix(t.closedAt as string), value: Math.round(cum * 100) / 100 });
  }
  return pts;
}

export type Dimension =
  | 'marketState'
  | 'cohort'
  | 'policyCell'
  | 'pair'
  | 'deployment'
  | 'package'
  | 'lane';

export const DIMENSIONS: Array<{ key: Dimension; label: string }> = [
  { key: 'marketState', label: 'Market State' },
  { key: 'cohort', label: 'Cohort' },
  { key: 'policyCell', label: 'Policy Cell' },
  { key: 'pair', label: 'Pair' },
  { key: 'deployment', label: 'Deployment' },
  { key: 'package', label: 'Package' },
  { key: 'lane', label: 'Lane' },
];

export function dimKey(t: LiveTrade, dim: Dimension): string {
  const p = parseScenarioKey(t.scenarioKey);
  switch (dim) {
    case 'marketState':
      return p.marketState;
    case 'cohort':
      return `${p.session}:${p.structure}:${p.direction}`;
    case 'policyCell':
      return t.scenarioKey;
    case 'pair':
      return p.instrument;
    case 'deployment':
      return t.deploymentId;
    case 'package':
      return t.packageHash;
    case 'lane':
      return t.lane;
  }
}

export function groupMetrics(
  trades: LiveTrade[],
  dim: Dimension
): Array<{ group: string; metrics: PerfMetrics }> {
  const map = new Map<string, LiveTrade[]>();
  for (const t of trades) {
    const k = dimKey(t, dim);
    const arr = map.get(k);
    if (arr) arr.push(t);
    else map.set(k, [t]);
  }
  return Array.from(map.entries())
    .map(([group, ts]) => ({ group, metrics: computeMetrics(ts) }))
    .sort((a, b) => b.metrics.netR - a.metrics.netR);
}

export function fmtPF(pf: number): string {
  return Number.isFinite(pf) ? pf.toFixed(2) : '∞';
}
