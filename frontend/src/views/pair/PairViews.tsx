/**
 * Pair-scoped views — all nine tabs of the PairWorkspace.
 * Reusable across any instrument (EURUSD, GBPUSD, XAUUSD, NQ, ES, BTC…).
 * Each view uses <WorkspacePage> for consistent shell + toolbar + inspector.
 */
import { useOutletContext } from 'react-router-dom';
import { useMemo, useState } from 'react';
import {
  usePairWorkspace,
  useMarketState,
  useActivePackage,
  useEdgeMonitor,
  useTrades,
  useEvents,
  usePolicyMatrix,
  useFleet,
} from '@/hooks/useRepository';
import { ActionBar } from '@/components/structures/ActionBar';
import { orderActions, tradeActions } from '@/components/domain/tradeActions';
import { useShellStore } from '@/store/shellStore';
import { Panel } from '@/components/structures/Panel';
import {
  WorkspacePage,
  ToolbarChip,
  ToolbarSpacer,
  ToolbarLabel,
  ToolbarDivider,
  PlaceholderChart,
  PlaceholderTable,
  PlaceholderMetricGrid,
  ComingSoon,
} from '@/components/structures/WorkspacePage';
import { DataTable, type Column } from '@/components/structures/DataTable';
import {
  Badge,
  CohortChip,
  ConfidenceMeter,
  HealthDot,
  KeyValueGrid,
  LaneChip,
  MarketStateBadge,
  MetricStat,
  PLValue,
  RValue,
  SampleSize,
  TimestampUTC,
  ValidationBadgeChip,
  WhyButton,
} from '@/components/primitives';
import { ChartWorkspace } from '@/components/domain/ChartWorkspace';
import { FeatureGate } from '@/components/FeatureGate';
import { fmtPercent, fmtPrice, fmtR, fmtRelative } from '@/lib/format';
import { parseScenarioKey } from '@/lib/utils';
import type { LiveTrade, GhostTrade, BlockedIntent } from '@/types/domain';
import { Filter, Download, Play, Pause, RotateCw } from 'lucide-react';
import { IconButton } from '@/components/primitives/Button';

const usePair = () => useOutletContext<{ pair: string }>().pair;

/* -------------------------------------------------------------------------- */
/*  1. Dashboard — replaces old "Overview". Chart + KPIs + recent decisions.   */
/* -------------------------------------------------------------------------- */

export function PairDashboardView() {
  const pair = usePair();
  const ws = usePairWorkspace(pair);
  const ms = useMarketState(pair);
  const pkg = useActivePackage();
  const edge = useEdgeMonitor();
  const openInspector = useShellStore((s) => s.openInspector);

  const openTrades = ws.trades.filter((t) => t.state !== 'closed');
  const totalRisk = ws.deployments.reduce((s, d) => s + d.riskState.riskTodayPct, 0);
  const worstDd = Math.min(...ws.deployments.map((d) => d.riskState.ddBufferPct), 100);

  return (
    <WorkspacePage
      title={`${pair} · Dashboard`}
      subtitle="Pair health · market state · recent decisions"
      right={
        <div className="text-2xs text-text-muted mono">
          {pkg.label} · v{pkg.version}
        </div>
      }
    >
      <div className="grid grid-cols-12 gap-4">
        <Panel title="Pair Health" className="col-span-12">
          <div className="grid grid-cols-6 gap-6">
            <MetricStat label="Open trades" value={openTrades.length} emphasise mono />
            <MetricStat label="Deployments" value={ws.deployments.length} emphasise mono />
            <MetricStat
              label="Risk today"
              value={<span className="mono">{fmtPercent(totalRisk)}</span>}
              emphasise
            />
            <MetricStat
              label="DD buffer"
              value={
                <span className="mono" style={{ color: worstDd < 40 ? 'var(--warning)' : 'var(--text)' }}>
                  {fmtPercent(worstDd, 0)}
                </span>
              }
              emphasise
            />
            <MetricStat label="Expectancy" value={<RValue value={edge.metrics.expectancyR} />} sub="30d rolling" emphasise />
            <MetricStat
              label="Win rate"
              value={<span className="mono">{fmtPercent(edge.metrics.winRate * 100, 1)}</span>}
              emphasise
            />
          </div>
        </Panel>

        {/* Chart — the canonical ChartWorkspace (same rendering path as Replay).
            Resizable: drag the divider under the chart (persisted height). */}
        <Panel title="Recent Price Action" className="col-span-8" bodyClassName="p-0">
          <FeatureGate flag="charts">
            <ChartWorkspace instrument={pair} mode="live" volume resizable initialHeight={420} />
          </FeatureGate>
        </Panel>

        <Panel title="Market State" className="col-span-4">
          {ms ? (
            <div className="space-y-3">
              <MarketStateBadge state={ms.state} confidence={ms.confidence} />
              <KeyValueGrid
                items={[
                  { label: 'Known at', value: ms.stateKnownAt, mono: true },
                  { label: 'Model', value: ms.modelVersion, mono: true },
                  { label: 'Source', value: ms.source },
                  { label: 'Confidence', value: <ConfidenceMeter value={ms.confidence} width={110} /> },
                  { label: 'Shifted', value: `${ms.shiftedDays}d prior-day` },
                ]}
              />
              <div className="text-2xs text-text-muted pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                Confirmed prior-day state drives execution. Unknown / warmup / unconfirmed states are allowed, not blocked.
              </div>
            </div>
          ) : (
            <div className="text-text-muted text-sm italic">No market state snapshot</div>
          )}
        </Panel>

        <Panel
          title="Recent Decisions"
          className="col-span-8"
          actions={<span className="text-2xs text-text-muted mono">last 24h</span>}
        >
          {ws.decisions.length === 0 ? (
            <div className="text-sm text-text-muted italic py-4 text-center">No decisions in this window</div>
          ) : (
            <ul className="space-y-2">
              {ws.decisions.slice(0, 5).map((d) => {
                const last = d.nodes[d.nodes.length - 1];
                const { session, structure, direction, marketState } = parseScenarioKey(d.scenarioKey);
                return (
                  <li
                    key={d.decisionId}
                    className="rounded-md p-3 border cursor-pointer hover:bg-[color:var(--panel-2)] transition-colors"
                    style={{ borderColor: 'var(--border-subtle)' }}
                    onClick={() => openInspector({ kind: 'decisionChain', decisionId: d.decisionId })}
                  >
                    <div className="flex items-baseline justify-between gap-2">
                      <div className="flex items-center gap-2 text-xs text-text">
                        <CohortChip session={session} structure={structure} direction={direction} />
                        <MarketStateBadge state={marketState} />
                      </div>
                      <TimestampUTC iso={last?.at ?? ''} />
                    </div>
                    <p className="text-sm text-text mt-1 truncate">{last?.value}</p>
                    <p className="text-xs text-text-2 mt-0.5">{last?.why}</p>
                  </li>
                );
              })}
            </ul>
          )}
        </Panel>

        <Panel title="Today's Posture" className="col-span-4">
          <div className="space-y-3">
            <div className="flex items-baseline justify-between">
              <span className="text-2xs uppercase tracking-widest text-text-muted">Blocked intents</span>
              <span className="text-xl font-semibold text-text mono">{ws.blocked.length}</span>
            </div>
            <div className="rounded-md p-2 border" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}>
              {ws.blocked.length > 0 ? (
                <ul className="space-y-1">
                  {ws.blocked.slice(0, 5).map((b) => (
                    <li
                      key={b.blockedIntentId}
                      className="flex items-center gap-2 text-2xs text-text-2 cursor-pointer hover:text-text"
                      onClick={() => openInspector({ kind: 'blocked', blockedIntentId: b.blockedIntentId })}
                    >
                      <Badge variant="live" color="var(--blocked)" size="sm">
                        {b.blockReason.replace(/_/g, ' ')}
                      </Badge>
                      <span className="mono truncate">{b.scenarioKey.replace(`${pair}:`, '')}</span>
                    </li>
                  ))}
                </ul>
              ) : (
                <div className="text-text-muted italic text-2xs">No blocks today</div>
              )}
            </div>
            <div className="pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
              <div className="text-2xs uppercase tracking-widest text-text-muted mb-1">Deployments</div>
              <div className="space-y-1">
                {ws.deployments.map((d) => (
                  <div
                    key={d.deploymentId}
                    className="flex items-center gap-2 text-2xs text-text-2 cursor-pointer hover:text-text"
                    onClick={() => openInspector({ kind: 'deployment', deploymentId: d.deploymentId })}
                  >
                    <LaneChip lane={d.lane} />
                    <span className="mono">{d.status}</span>
                    <span className="ml-auto">
                      <PLValue value={d.riskState.dailyPl} />
                    </span>
                  </div>
                ))}
              </div>
            </div>
          </div>
        </Panel>
      </div>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  2. Orders — pending / execution queue (subset of Trades)                   */
/* -------------------------------------------------------------------------- */

export function PairOrdersView() {
  const pair = usePair();
  const trades = useTrades({ pair });
  const { accounts, brokers, deployments } = useFleet();
  const pkg = useActivePackage();
  const openInspector = useShellStore((s) => s.openInspector);
  const depById = useMemo(() => new Map(deployments.map((d) => [d.deploymentId, d])), [deployments]);
  const pending = trades.live.filter((t) => t.state === 'pending');

  const cols: Column<LiveTrade>[] = [
    { key: 'id', header: 'Order', mono: true, width: 120, cell: (t) => t.brokerOrderId || t.tradeId.slice(-6) },
    {
      key: 'scenario',
      header: 'Policy Cell',
      cell: (t) => {
        const { session, structure, direction, marketState } = parseScenarioKey(t.scenarioKey);
        return (
          <div className="flex items-center gap-2">
            <CohortChip session={session} structure={structure} direction={direction} />
            <MarketStateBadge state={marketState} />
          </div>
        );
      },
    },
    {
      key: 'account',
      header: 'Account',
      cell: (t) => {
        const account = accounts.find((a) => a.accountId === depById.get(t.deploymentId)?.accountId);
        const broker = brokers.find((b) => b.brokerId === account?.brokerId);
        return <span className="text-2xs text-text-2">{broker?.venue ?? '—'} · {account?.type ?? '—'}</span>;
      },
    },
    { key: 'lane', header: 'Lane', cell: (t) => <LaneChip lane={t.lane} /> },
    { key: 'pkg', header: 'Package', cell: () => <Badge variant="status">v{pkg.version}</Badge> },
    { key: 'session', header: 'Session', cell: (t) => <span className="text-2xs text-text-2">{parseScenarioKey(t.scenarioKey).session}</span> },
    { key: 'entry', header: 'Entry', align: 'right', mono: true, cell: (t) => fmtPrice(t.entry) },
    { key: 'sl', header: 'Stop', align: 'right', mono: true, cell: (t) => fmtPrice(t.sl) },
    { key: 'tp', header: 'Target', align: 'right', mono: true, cell: (t) => fmtPrice(t.tp) },
    { key: 'risk', header: 'Risk', align: 'right', mono: true, cell: (t) => `${t.riskPct.toFixed(2)}%` },
    { key: 'er', header: 'Exp R', align: 'right', mono: true, cell: (t) => `${t.originalPlan.rr.toFixed(2)}R` },
    { key: 'age', header: 'Age', cell: (t) => <span className="text-2xs">{fmtRelative(t.openedAt)}</span> },
    { key: 'status', header: 'Status', cell: (t) => <Badge variant="status">{t.state}</Badge> },
    {
      key: 'actions',
      header: '',
      width: 200,
      cell: (t) => (
        <ActionBar
          iconOnly
          actions={orderActions(t, { onInspect: () => openInspector({ kind: 'trade', tradeId: t.tradeId }) })}
        />
      ),
    },
  ];

  return (
    <WorkspacePage
      title={`${pair} · Orders`}
      subtitle="Pending orders · execution queue"
      toolbar={
        <>
          <ToolbarLabel>State</ToolbarLabel>
          <ToolbarChip active count={pending.length}>Pending</ToolbarChip>
          <ToolbarDivider />
          <ToolbarSpacer />
          <span className="text-2xs text-text-muted mono">Package pinned in-flight · atomic hash locked at signal time</span>
        </>
      }
    >
      <Panel title="Pending Orders" bodyClassName="p-0">
        <DataTable
          columns={cols}
          data={pending}
          rowKey={(t) => t.tradeId}
          onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })}
          emptyMessage="No pending orders"
          searchable
          searchAccessor={(t) => `${t.brokerOrderId} ${t.scenarioKey} ${t.state}`}
        />
      </Panel>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  3. Trades — open / closed / ghost / blocked                                */
/* -------------------------------------------------------------------------- */

export function PairTradesView() {
  const pair = usePair();
  const trades = useTrades({ pair });
  const { asOf } = useFleet();
  const currentMs = useMarketState(pair);
  const openInspector = useShellStore((s) => s.openInspector);
  const [tab, setTab] = useState<'open' | 'closed' | 'ghost' | 'blocked'>('open');

  const open = trades.live.filter((t) => t.state !== 'closed' && t.state !== 'pending');
  const closed = trades.live.filter((t) => t.state === 'closed');
  const now = new Date(asOf);

  const liveCols: Column<LiveTrade>[] = [
    {
      key: 'scenario',
      header: 'Scenario',
      width: 260,
      cell: (t) => {
        const { session, structure, direction, marketState } = parseScenarioKey(t.scenarioKey);
        return (
          <div className="flex items-center gap-2">
            <CohortChip session={session} structure={structure} direction={direction} />
            <MarketStateBadge state={marketState} />
          </div>
        );
      },
    },
    { key: 'lane', header: 'Lane', cell: (t) => <LaneChip lane={t.lane} /> },
    { key: 'state', header: 'State', cell: (t) => <Badge variant="status">{t.state}</Badge> },
    { key: 'entry', header: 'Entry', align: 'right', mono: true, cell: (t) => fmtPrice(t.entry) },
    { key: 'sl', header: 'Stop', align: 'right', mono: true, cell: (t) => fmtPrice(t.sl) },
    { key: 'tp', header: 'Target', align: 'right', mono: true, cell: (t) => fmtPrice(t.tp) },
    { key: 'risk', header: 'Risk', align: 'right', mono: true, cell: (t) => `${t.riskPct.toFixed(2)}%` },
    { key: 'currentR', header: 'Current R', align: 'right', cell: (t) => <RValue value={t.currentR} /> },
    { key: 'floating', header: 'Floating', align: 'right', cell: (t) => <PLValue value={t.floatingPl} /> },
    {
      key: 'why',
      header: '',
      width: 60,
      cell: (t) =>
        t.decisionId ? (
          <WhyButton onClick={() => openInspector({ kind: 'decisionChain', decisionId: t.decisionId! })} />
        ) : null,
    },
  ];

  // Active Trades — richer columns + per-row ActionBar.
  const activeCols: Column<LiveTrade>[] = [
    {
      key: 'scenario',
      header: 'Policy Cell',
      width: 240,
      cell: (t) => {
        const { session, structure, direction, marketState } = parseScenarioKey(t.scenarioKey);
        return (
          <div className="flex items-center gap-2">
            <CohortChip session={session} structure={structure} direction={direction} />
            <MarketStateBadge state={marketState} />
          </div>
        );
      },
    },
    { key: 'nowMs', header: 'Current State', cell: () => (currentMs ? <MarketStateBadge state={currentMs.state} confidence={currentMs.confidence} confirmed={currentMs.confirmed} /> : <span className="text-text-muted">—</span>) },
    { key: 'lane', header: 'Lane', cell: (t) => <LaneChip lane={t.lane} /> },
    { key: 'entry', header: 'Entry', align: 'right', mono: true, cell: (t) => fmtPrice(t.entry) },
    { key: 'sl', header: 'Stop', align: 'right', mono: true, cell: (t) => fmtPrice(t.sl) },
    { key: 'tp', header: 'Target', align: 'right', mono: true, cell: (t) => fmtPrice(t.tp) },
    { key: 'currentR', header: 'Floating R', align: 'right', sortable: true, sortAccessor: (t) => t.currentR ?? 0, cell: (t) => <RValue value={t.currentR} /> },
    { key: 'floating', header: 'Floating P/L', align: 'right', sortable: true, sortAccessor: (t) => t.floatingPl, cell: (t) => <PLValue value={t.floatingPl} /> },
    { key: 'tit', header: 'Time in trade', cell: (t) => <span className="text-2xs">{fmtRelative(t.openedAt, now)}</span> },
    { key: 'mgmt', header: 'Management', cell: (t) => <span className="text-2xs text-text-2">{t.protectionStatus}</span> },
    {
      key: 'why',
      header: '',
      width: 60,
      cell: (t) => (t.decisionId ? <WhyButton onClick={() => openInspector({ kind: 'decisionChain', decisionId: t.decisionId! })} /> : null),
    },
    {
      key: 'actions',
      header: '',
      width: 210,
      cell: (t) => <ActionBar iconOnly actions={tradeActions(t, { onInspect: () => openInspector({ kind: 'trade', tradeId: t.tradeId }) })} />,
    },
  ];

  const closedCols: Column<LiveTrade>[] = [
    ...liveCols.slice(0, 3),
    { key: 'realized', header: 'Realized R', align: 'right', cell: (t) => <RValue value={t.realizedR} /> },
    { key: 'close', header: 'Close', align: 'right', mono: true, cell: (t) => (t.closePrice ? fmtPrice(t.closePrice) : '—') },
    { key: 'closedAt', header: 'Closed', cell: (t) => (t.closedAt ? <TimestampUTC iso={t.closedAt} /> : '—') },
  ];

  const ghostCols: Column<GhostTrade>[] = [
    {
      key: 'scenario',
      header: 'Scenario',
      cell: (g) => {
        const { session, structure, direction, marketState } = parseScenarioKey(g.scenarioKey);
        return (
          <div className="flex items-center gap-2">
            <CohortChip session={session} structure={structure} direction={direction} />
            <MarketStateBadge state={marketState} />
          </div>
        );
      },
    },
    { key: 'model', header: 'Model', cell: (g) => <Badge variant="ghost">{g.ghostModelId}</Badge> },
    {
      key: 'outcome',
      header: 'Outcome',
      cell: (g) => (
        <Badge variant={g.ghostOutcome === 'WIN' ? 'validation' : g.ghostOutcome === 'LOSS' ? 'live' : 'neutral'}>
          {g.ghostOutcome}
        </Badge>
      ),
    },
    { key: 'r', header: 'R', align: 'right', cell: (g) => <RValue value={g.ghostR} /> },
    { key: 'mae', header: 'MAE', align: 'right', cell: (g) => <RValue value={g.ghostMae} /> },
    { key: 'mfe', header: 'MFE', align: 'right', cell: (g) => <RValue value={g.ghostMfe} /> },
  ];

  const blockedCols: Column<BlockedIntent>[] = [
    {
      key: 'scenario',
      header: 'Scenario',
      cell: (b) => {
        const { session, structure, direction, marketState } = parseScenarioKey(b.scenarioKey);
        return (
          <div className="flex items-center gap-2">
            <CohortChip session={session} structure={structure} direction={direction} />
            <MarketStateBadge state={marketState} />
          </div>
        );
      },
    },
    { key: 'reason', header: 'Reason', cell: (b) => <Badge variant="live" color="var(--blocked)">{b.blockReason.replace(/_/g, ' ')}</Badge> },
    { key: 'rule', header: 'Rule fired', mono: true, cell: (b) => <span className="text-2xs text-text-muted">{b.ruleFired}</span> },
    { key: 'lane', header: 'Lane', cell: (b) => <LaneChip lane={b.lane} /> },
    { key: 'at', header: 'At', cell: (b) => <TimestampUTC iso={b.at} /> },
  ];

  return (
    <WorkspacePage
      title={`${pair} · Trades`}
      subtitle="Open · closed · ghost · blocked"
      toolbar={
        <>
          <ToolbarLabel>View</ToolbarLabel>
          <ToolbarChip active={tab === 'open'} onClick={() => setTab('open')} count={open.length}>Open</ToolbarChip>
          <ToolbarChip active={tab === 'closed'} onClick={() => setTab('closed')} count={closed.length}>Closed</ToolbarChip>
          <ToolbarChip active={tab === 'ghost'} onClick={() => setTab('ghost')} count={trades.ghost.length}>Ghost</ToolbarChip>
          <ToolbarChip active={tab === 'blocked'} onClick={() => setTab('blocked')} count={trades.blocked.length}>Blocked</ToolbarChip>
          <ToolbarSpacer />
          <IconButton ariaLabel="Filter"><Filter size={13} /></IconButton>
          <IconButton ariaLabel="Export"><Download size={13} /></IconButton>
        </>
      }
    >
      <Panel bodyClassName="p-0" title={`${tab.charAt(0).toUpperCase() + tab.slice(1)} · ${pair}`}>
        {tab === 'open' && (
          <DataTable columns={activeCols} data={open} rowKey={(t) => t.tradeId} onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })} emptyMessage="No open trades" searchable searchAccessor={(t) => `${t.scenarioKey} ${t.state} ${t.protectionStatus}`} />
        )}
        {tab === 'closed' && (
          <DataTable columns={closedCols} data={closed} rowKey={(t) => t.tradeId} onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })} emptyMessage="No closed trades" />
        )}
        {tab === 'ghost' && (
          <DataTable columns={ghostCols} data={trades.ghost} rowKey={(g) => g.ghostTradeId} onRowClick={(g) => openInspector({ kind: 'ghost', ghostTradeId: g.ghostTradeId })} emptyMessage="No ghost trades" />
        )}
        {tab === 'blocked' && (
          <DataTable columns={blockedCols} data={trades.blocked} rowKey={(b) => b.blockedIntentId} onRowClick={(b) => openInspector({ kind: 'blocked', blockedIntentId: b.blockedIntentId })} emptyMessage="No blocked intents" />
        )}
      </Panel>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  4. Edge Monitor (pair-scoped)                                              */
/* -------------------------------------------------------------------------- */

export function PairEdgeMonitorView() {
  const pair = usePair();
  const edge = useEdgeMonitor();

  return (
    <WorkspacePage
      title={`${pair} · Edge Monitor`}
      subtitle="Drift · confidence · research-vs-live · ghost-vs-live · candidates"
    >
      <div className="grid grid-cols-12 gap-4">
        <Panel title="Live Edge Signals" className="col-span-12">
          <div className="grid grid-cols-6 gap-6">
            <MetricStat label="Expectancy" value={<RValue value={edge.metrics.expectancyR} />} emphasise />
            <MetricStat label="Win rate" value={<span className="mono">{fmtPercent(edge.metrics.winRate * 100, 1)}</span>} emphasise />
            <MetricStat label="Edge drift" value={<span className="mono">{edge.metrics.edgeDrift}</span>} emphasise />
            <MetricStat label="Distribution drift" value={<Badge variant="validation" color="var(--positive)">{edge.metrics.distributionDrift}</Badge>} emphasise />
            <MetricStat label="Feature drift" value={<span className="mono">{edge.metrics.featureDrift}</span>} emphasise />
            <MetricStat label="Policy health" value={<Badge variant="health" color="var(--positive)">{edge.metrics.policyHealth}</Badge>} emphasise />
          </div>
        </Panel>

        <Panel title="Expectancy trajectory (30d)" className="col-span-8" bodyClassName="p-3">
          <PlaceholderChart height={220} variant="area" />
        </Panel>

        <Panel title="Comparisons" className="col-span-4">
          <KeyValueGrid
            items={[
              { label: 'Research vs Live', value: <span className="mono">{edge.metrics.researchVsLive}</span> },
              { label: 'Ghost vs Live', value: <span className="mono">{edge.metrics.ghostVsLive}</span> },
              { label: 'Forward-test', value: edge.metrics.forwardTestHealth },
              { label: 'Rec gen', value: edge.metrics.recommendationGeneration, mono: true },
              { label: 'Operator conf.', value: <Badge variant="status">{edge.metrics.operatorConfidence}</Badge> },
              { label: 'As of', value: <TimestampUTC iso={edge.asOf} /> },
            ]}
          />
        </Panel>

        <Panel title="Future Candidates" className="col-span-6">
          {edge.metrics.futureCandidates.length === 0 ? (
            <div className="text-xs text-text-muted italic">No candidates pending</div>
          ) : (
            <ul className="space-y-2">
              {edge.metrics.futureCandidates.map((c, i) => (
                <li key={i} className="flex items-center gap-2 text-xs text-text-2">
                  <Badge variant="recommendation" size="sm">candidate</Badge>
                  <span className="mono">{c}</span>
                </li>
              ))}
            </ul>
          )}
        </Panel>

        <Panel title="Drift by cohort (heat map)" className="col-span-6" bodyClassName="p-3">
          <PlaceholderChart height={200} variant="bar" />
        </Panel>
      </div>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  5. Strategy Health — per-pair readiness / stability / integrity            */
/* -------------------------------------------------------------------------- */

export function PairStrategyHealthView() {
  const pair = usePair();
  const pkg = useActivePackage();
  const matrix = usePolicyMatrix(pair);

  const cells = Object.values(matrix.cells);
  const allowed = cells.filter((c) => c.eligibility.resolvedAllowed).length;
  const native = cells.filter((c) => c.evidence.badge === 'NATIVE').length;
  const insufficient = cells.filter((c) => c.evidence.badge === 'INSUFFICIENT').length;
  const notTested = cells.filter((c) => c.evidence.badge === 'NOT_TESTED').length;

  const healthScore = Math.round(((native / cells.length) * 0.5 + (allowed / cells.length) * 0.5) * 100);

  return (
    <WorkspacePage title={`${pair} · Strategy Health`} subtitle="Coverage · stability · integrity">
      <div className="grid grid-cols-12 gap-4">
        <Panel title="Health Score" className="col-span-4">
          <div className="flex flex-col items-center gap-3 py-2">
            <div
              className="w-28 h-28 rounded-full border-4 flex items-center justify-center"
              style={{
                borderColor: healthScore > 70 ? 'var(--positive)' : healthScore > 50 ? 'var(--warning)' : 'var(--negative)',
                background: 'var(--panel-2)',
              }}
            >
              <div className="text-center">
                <div className="text-3xl font-semibold mono text-text">{healthScore}</div>
                <div className="text-2xs uppercase tracking-widest text-text-muted">/ 100</div>
              </div>
            </div>
            <Badge variant="status" color={healthScore > 70 ? 'var(--positive)' : 'var(--warning)'}>
              {healthScore > 70 ? 'Healthy' : healthScore > 50 ? 'Caution' : 'At risk'}
            </Badge>
          </div>
        </Panel>

        <Panel title="Cell Coverage" className="col-span-8">
          <div className="grid grid-cols-4 gap-4">
            <MetricStat label="Total cells" value={cells.length} mono emphasise />
            <MetricStat label="Allowed" value={<span className="mono text-[color:var(--positive)]">{allowed}</span>} sub={`${((allowed / cells.length) * 100).toFixed(0)}%`} emphasise />
            <MetricStat label="NATIVE" value={<span className="mono text-[color:var(--validated)]">{native}</span>} sub={`${((native / cells.length) * 100).toFixed(0)}%`} emphasise />
            <MetricStat label="Under-tested" value={<span className="mono text-[color:var(--warning)]">{insufficient + notTested}</span>} sub={`${(((insufficient + notTested) / cells.length) * 100).toFixed(0)}%`} emphasise />
          </div>
        </Panel>

        <Panel title="Package Integrity" className="col-span-6">
          <KeyValueGrid
            items={[
              { label: 'Version', value: `v${pkg.version}`, mono: true },
              { label: 'Contract', value: pkg.contractVersion, mono: true },
              { label: 'Domain', value: pkg.domain },
              { label: 'Instruments', value: pkg.instruments.join(', ') },
              { label: 'Hash', value: pkg.packageHash.slice(0, 24) + '…', mono: true },
              { label: 'Validation ID', value: pkg.validation.validationId ?? '—', mono: true },
              { label: 'Δ netR', value: <RValue value={pkg.validation.portfolioDeltas.netR} />, mono: true },
              { label: 'Stability', value: pkg.validation.portfolioDeltas.stability.toFixed(2), mono: true },
            ]}
          />
        </Panel>

        <Panel title="Component versions" className="col-span-6">
          <ul className="space-y-1.5 text-xs">
            {Object.entries(pkg.componentVersions).map(([k, v]) => (
              <li key={k} className="flex items-center gap-2">
                <HealthDot state="ok" />
                <span className="text-text-2">{k}</span>
                <span className="ml-auto mono text-text-muted">{v}</span>
              </li>
            ))}
          </ul>
        </Panel>

        <Panel title="Warning signals" className="col-span-12">
          <ComingSoon
            title="Signal detectors coming online in Phase 2"
            description="Drift-per-cohort, sample-decay, correlated-loss, session-regime-mismatch will surface here as sortable/dismissable alerts."
          />
        </Panel>
      </div>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  6. Activity Timeline — audit stream, scope-filtered                        */
/* -------------------------------------------------------------------------- */

export function PairActivityTimelineView() {
  const pair = usePair();
  const events = useEvents({ pair });
  const [category, setCategory] = useState<string | 'all'>('all');

  const categories = useMemo(() => Array.from(new Set(events.map((e) => e.category))), [events]);
  const filtered = category === 'all' ? events : events.filter((e) => e.category === category);

  return (
    <WorkspacePage
      title={`${pair} · Activity Timeline`}
      subtitle="Append-only audit stream · every decision and mutation lands here"
      toolbar={
        <>
          <ToolbarLabel>Category</ToolbarLabel>
          <ToolbarChip active={category === 'all'} onClick={() => setCategory('all')} count={events.length}>All</ToolbarChip>
          {categories.map((c) => (
            <ToolbarChip key={c} active={category === c} onClick={() => setCategory(c)} count={events.filter((e) => e.category === c).length}>
              {c}
            </ToolbarChip>
          ))}
          <ToolbarSpacer />
          <IconButton ariaLabel="Export"><Download size={13} /></IconButton>
        </>
      }
    >
      <Panel bodyClassName="p-0">
        <ol className="divide-y" style={{ borderColor: 'var(--border-subtle)' }}>
          {filtered.length === 0 ? (
            <li className="p-8 text-center text-text-muted italic">No events in this scope</li>
          ) : (
            filtered.map((ev) => (
              <li
                key={ev.eventId}
                className="grid gap-3 px-4 py-2 hover:bg-[color:var(--panel-2)] transition-colors items-baseline"
                style={{ gridTemplateColumns: '80px 100px 160px 1fr 130px' }}
              >
                <span className="mono text-2xs text-text-muted">#{ev.seq}</span>
                <Badge variant="neutral" size="sm">{ev.category}</Badge>
                <span className="mono text-2xs text-text truncate">{ev.code}</span>
                <span className="text-xs text-text-2 truncate">{ev.humanExplanation}</span>
                <span className="text-2xs text-text-muted mono text-right">
                  <TimestampUTC iso={ev.at} mode="absolute" />
                </span>
              </li>
            ))
          )}
        </ol>
      </Panel>
    </WorkspacePage>
  );
}
