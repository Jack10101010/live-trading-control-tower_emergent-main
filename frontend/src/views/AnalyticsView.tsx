import { useOutletContext } from 'react-router-dom';
import { usePolicyMatrix, useTrades } from '@/hooks/useRepository';
import { Panel } from '@/components/structures/Panel';
import { FeatureGate } from '@/components/FeatureGate';
import { ChartPanel } from '@/components/domain/ChartPanel';
import { DataTable, type Column } from '@/components/structures/DataTable';
import { RValue, MetricStat, ProvenanceChip } from '@/components/primitives';
import { fmtPercent, fmtR } from '@/lib/format';
import { useMemo, useState } from 'react';
import {
  computeMetrics,
  equityCurve,
  groupMetrics,
  fmtPF,
  DIMENSIONS,
  type Dimension,
  type PerfMetrics,
} from '@/lib/analytics';

export function AnalyticsView() {
  const { pair } = useOutletContext<{ pair: string }>();
  const matrix = usePolicyMatrix(pair);
  const trades = useTrades({ pair });
  const [dim, setDim] = useState<Dimension>('marketState');

  const metrics = useMemo(() => computeMetrics(trades.live), [trades.live]);
  const equity = useMemo(() => equityCurve(trades.live), [trades.live]);
  const groups = useMemo(() => groupMetrics(trades.live, dim), [trades.live, dim]);

  const cells = Object.values(matrix.cells);
  // UI-0: these two panels aggregate policy cells. When any cell is generated the
  // aggregate is not a research finding and is labelled accordingly.
  const synthesizedCells = matrix.synthesizedCells ?? 0;
  const policyAggregatesSynthesized = synthesizedCells > 0;

  // Aggregations
  const bySession = useMemo(() => {
    const map = new Map<string, { count: number; sumEV: number; sumWr: number; sumN: number }>();
    cells.forEach((c) => {
      const key = c.cohort.session;
      const cur = map.get(key) ?? { count: 0, sumEV: 0, sumWr: 0, sumN: 0 };
      cur.count += 1;
      cur.sumEV += c.evidence.expectancyR;
      cur.sumWr += c.evidence.winRate;
      cur.sumN += c.evidence.sampleSize;
      map.set(key, cur);
    });
    return Array.from(map.entries()).map(([k, v]) => ({
      session: k,
      count: v.count,
      avgEV: v.sumEV / v.count,
      avgWr: v.sumWr / v.count,
      n: v.sumN,
    }));
  }, [cells]);

  const byState = useMemo(() => {
    const map = new Map<string, { count: number; sumEV: number; sumN: number; allowed: number }>();
    cells.forEach((c) => {
      const cur = map.get(c.marketState) ?? { count: 0, sumEV: 0, sumN: 0, allowed: 0 };
      cur.count += 1;
      cur.sumEV += c.evidence.expectancyR;
      cur.sumN += c.evidence.sampleSize;
      if (c.eligibility.resolvedAllowed) cur.allowed += 1;
      map.set(c.marketState, cur);
    });
    return Array.from(map.entries()).map(([k, v]) => ({
      state: k,
      count: v.count,
      avgEV: v.sumEV / v.count,
      allowedPct: (v.allowed / v.count) * 100,
      n: v.sumN,
    }));
  }, [cells]);

  return (
    // Scroll fix: `absolute inset-0` gives the scroll region a DEFINITE height
    // (immune to percentage-height resolution quirks up the flex chain that
    // clipped the bottom panels), `overflow-y-auto` guarantees the scrollbar,
    // and `pb-8` keeps the last row (By Session / By Market State) clear of
    // the viewport edge at full scroll.
    <div className="relative h-full min-h-0">
      <div className="absolute inset-0 overflow-y-auto">
        <div className="grid grid-cols-12 gap-4 p-4 pb-8">
      <Panel provenance="fixture" title="Performance Overview" className="col-span-12">
        <div className="grid grid-cols-7 gap-6">
          <MetricStat label="Net R" value={<RValue value={metrics.netR} />} emphasise />
          <MetricStat label="Trades" value={metrics.count} mono emphasise />
          <MetricStat label="Win rate" value={<span className="mono">{metrics.count ? fmtPercent(metrics.winRate * 100) : '—'}</span>} emphasise />
          <MetricStat label="Expectancy" value={<RValue value={metrics.expectancyR} />} emphasise />
          <MetricStat label="Avg R" value={<RValue value={metrics.avgR} />} emphasise />
          <MetricStat label="Profit factor" value={<span className="mono">{fmtPF(metrics.profitFactor)}</span>} emphasise />
          <MetricStat label="Max DD" value={<RValue value={metrics.maxDrawdownR} />} emphasise />
        </div>
      </Panel>

      <Panel provenance="fixture" title="Equity Curve (cumulative R)" className="col-span-12">
        {equity.length >= 1 ? (
          <FeatureGate flag="charts">
            <ChartPanel kind="line" lineData={equity} height={220} linePrecision={2} className="w-full" />
          </FeatureGate>
        ) : (
          <div className="text-xs text-text-muted italic py-8 text-center">
            No closed trades in scope yet — the equity curve appears once trades settle.
          </div>
        )}
      </Panel>

      <Panel
        provenance="fixture"
        title={
          <span className="flex items-center gap-2">
            Performance by
            <select
              value={dim}
              onChange={(e) => setDim(e.target.value as Dimension)}
              className="h-6 text-2xs rounded-sm bg-[color:var(--panel-2)] border border-[color:var(--border)] text-text px-1.5 outline-none"
              data-testid="analytics-dimension"
            >
              {DIMENSIONS.map((d) => (
                <option key={d.key} value={d.key}>
                  {d.label}
                </option>
              ))}
            </select>
          </span>
        }
        className="col-span-12"
        dense
      >
        <DataTable columns={groupColumns} data={groups} rowKey={(g) => g.group} emptyMessage="No trades in scope" />
      </Panel>

      <Panel
        provenance="fixture"
        title={
          <span className="flex items-center gap-2">
            By Session (policy expectancy)
            {policyAggregatesSynthesized && (
              <ProvenanceChip
                provenance="synthesized"
                detail={`${synthesizedCells} of ${cells.length} policy cells are generated locally.`}
              />
            )}
          </span>
        }
        className="col-span-6"
      >
        <div className="space-y-2">
          {bySession.map((row) => (
            <BarRow
              key={row.session}
              label={row.session}
              value={fmtR(row.avgEV)}
              fillPct={Math.min(100, Math.max(5, ((row.avgEV + 0.3) / 0.6) * 100))}
              color={row.avgEV > 0 ? 'var(--positive)' : 'var(--negative)'}
              sub={`n=${row.n}`}
            />
          ))}
        </div>
      </Panel>

      <Panel
        provenance="fixture"
        title={
          <span className="flex items-center gap-2">
            By Market State (expectancy · allowed %)
            {policyAggregatesSynthesized && (
              <ProvenanceChip
                provenance="synthesized"
                detail={`${synthesizedCells} of ${cells.length} policy cells are generated locally.`}
              />
            )}
          </span>
        }
        className="col-span-6"
      >
        <div className="space-y-2">
          {byState.map((row) => (
            <BarRow
              key={row.state}
              label={row.state}
              value={fmtR(row.avgEV)}
              fillPct={row.allowedPct}
              color={row.avgEV > 0 ? 'var(--positive)' : 'var(--negative)'}
              sub={`allowed ${row.allowedPct.toFixed(0)}%`}
            />
          ))}
        </div>
      </Panel>
        </div>
      </div>
    </div>
  );
}

type GroupRow = { group: string; metrics: PerfMetrics };

function shortenGroup(g: string): string {
  if (g.startsWith('sha256:')) return `${g.slice(7, 15)}…`;
  if (g.startsWith('dpl_') || g.startsWith('mf_')) return `${g.slice(0, 12)}…`;
  const parts = g.split(':');
  return parts.length > 3 ? parts.slice(-3).join(':') : g;
}

const groupColumns: Column<GroupRow>[] = [
  { key: 'group', header: 'Group', cell: (g) => <span className="mono text-xs text-text truncate" title={g.group}>{shortenGroup(g.group)}</span> },
  { key: 'n', header: 'Trades', align: 'right', mono: true, cell: (g) => g.metrics.count },
  { key: 'wr', header: 'Win %', align: 'right', mono: true, cell: (g) => (g.metrics.count ? `${(g.metrics.winRate * 100).toFixed(0)}%` : '—') },
  { key: 'net', header: 'Net R', align: 'right', cell: (g) => <RValue value={g.metrics.netR} /> },
  { key: 'exp', header: 'Expectancy', align: 'right', cell: (g) => <RValue value={g.metrics.expectancyR} /> },
  { key: 'pf', header: 'PF', align: 'right', mono: true, cell: (g) => fmtPF(g.metrics.profitFactor) },
  { key: 'dd', header: 'Max DD', align: 'right', cell: (g) => <RValue value={g.metrics.maxDrawdownR} /> },
];

function BarRow({
  label,
  value,
  fillPct,
  color,
  sub,
}: {
  label: string;
  value: string;
  fillPct: number;
  color: string;
  sub?: string;
}) {
  return (
    <div className="grid grid-cols-[100px_1fr_80px] items-center gap-3 text-xs">
      <span className="text-text-2">{label}</span>
      <span
        className="h-3 rounded-sm overflow-hidden relative"
        style={{ background: 'var(--panel-3)' }}
      >
        <span
          className="block h-full"
          style={{ width: `${Math.max(2, Math.min(100, fillPct))}%`, background: color, opacity: 0.7 }}
        />
      </span>
      <span className="mono text-right text-text">
        {value}
        {sub && <span className="ml-1 text-2xs text-text-muted">{sub}</span>}
      </span>
    </div>
  );
}
