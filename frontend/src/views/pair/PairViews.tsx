/**
 * Pair-scoped views — all nine tabs of the PairWorkspace.
 * Reusable across any instrument (EURUSD, GBPUSD, XAUUSD, NQ, ES, BTC…).
 * Each view uses <WorkspacePage> for consistent shell + toolbar + inspector.
 */
import { useOutletContext } from 'react-router-dom';
import { PACKAGES_UNAVAILABLE_DETAIL } from '@/lib/operationalProvenance';
import { useRuntimeHealth } from '@/hooks/useRepository';
import { candleCardProvenance } from '@/lib/cardProvenance';
import { useMemo, useState } from 'react';
import {
  usePairWorkspace,
  useMarketState,
  useActivePackage,
  useOperationalTrades,
  useEvents,
  usePolicyMatrix,
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
  ProvenanceChip,
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
  const rtHealth = useRuntimeHealth();
  const pkg = useActivePackage();
  const openInspector = useShellStore((s) => s.openInspector);

  // M-TRADES-1: was a fixture-derived open-trade count.
  const openTrades = ws.trades.filter((t) => t.state !== 'closed');
  const totalRisk = ws.deployments.reduce((s, d) => s + d.riskState.riskTodayPct, 0);
  const worstDd = Math.min(...ws.deployments.map((d) => d.riskState.ddBufferPct), 100);

  return (
    <WorkspacePage
      title={`${pair} · Dashboard`}
      subtitle="Pair health · market state · recent decisions"
      right={
        <div className="text-2xs text-text-muted mono">
          {pkg?.label} · v{pkg?.version}
        </div>
      }
    >
      <div className="grid grid-cols-12 gap-4">
        {/* M-EDGE-1: the fixture-fed Expectancy and Win rate tiles are removed
            (no edge/performance model exists). The remaining tiles are pair
            operational content and rebalance to four columns. */}
        <Panel provenance="fixture" title="Pair Health" className="col-span-12">
          <div className="grid grid-cols-4 gap-6">
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
          </div>
        </Panel>

        {/* Chart — the canonical ChartWorkspace (same rendering path as Replay).
            Resizable: drag the divider under the chart (persisted height). */}
        <Panel provenance={candleCardProvenance(rtHealth?.marketData?.provider)} title="Recent Price Action" className="col-span-8" bodyClassName="p-0">
          <FeatureGate flag="charts">
            <ChartWorkspace instrument={pair} mode="live" resizable initialHeight={420} />
          </FeatureGate>
        </Panel>

        <Panel provenance="fixture" title="Market State" className="col-span-4">
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
          provenance="fixture"
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

        <Panel provenance="fixture" title="Today's Posture" className="col-span-4">
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
  // M-TRADES-1: fixture trades severed. Authoritative positions/orders only.
  const { positions, orders, status: tradesStatus } = useOperationalTrades(pair);
  const trades = { live: [] as LiveTrade[], ghost: [] as GhostTrade[], blocked: [] as BlockedIntent[] };

  const pkg = useActivePackage();
  const openInspector = useShellStore((s) => s.openInspector);

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
      // M-FLEET-2: resolved a FIXTURE account and broker for each order row.
      // No authoritative account mapping exists, so the column reports absence.
      cell: () => <span className="text-2xs text-text-muted">—</span>,
    },
    { key: 'lane', header: 'Lane', cell: (t) => <LaneChip lane={t.lane} /> },
    { key: 'pkg', header: 'Package', cell: () => <Badge variant="status">v{pkg?.version}</Badge> },
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
      {/* M-TRADES-1: authoritative orders render here. Under the mock adapter
          the projection's records are rejected by the provenance gate, so this
          list is empty and the table below states which source is missing. */}
      {orders.length > 0 && (
        <Panel provenance="live" title={`Authoritative orders (${orders.length})`} bodyClassName="p-3">
          <ul className="text-xs mono space-y-1" data-testid="authoritative-orders">
            {orders.map((o, i) => (
              <li key={o.brokerOrderReference ?? `order-${i}`} data-testid="authoritative-order-row">
                {o.instrument ?? '—'} · {o.side ?? '—'} · {o.brokerOrderReference ?? '—'}
              </li>
            ))}
          </ul>
        </Panel>
      )}
      <Panel provenance="live" title="Pending Orders" bodyClassName="p-0">
        <DataTable
          columns={cols}
          data={pending}
          rowKey={(t) => t.tradeId}
          onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })}
          emptyMessage={tradesStatus === 'unavailable' ? 'No authoritative order source — orders cannot be reported' : 'No open orders reported'}
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
  // M-TRADES-1: fixture trades severed. Authoritative positions/orders only.
  const { positions, orders, status: tradesStatus } = useOperationalTrades(pair);
  const trades = { live: [] as LiveTrade[], ghost: [] as GhostTrade[], blocked: [] as BlockedIntent[] };

  const currentMs = useMarketState(pair);
  const openInspector = useShellStore((s) => s.openInspector);
  const [tab, setTab] = useState<'open' | 'closed' | 'ghost' | 'blocked'>('open');

  const open = trades.live.filter((t) => t.state !== 'closed' && t.state !== 'pending');
  const closed = trades.live.filter((t) => t.state === 'closed');
  // M-FLEET-2: was `new Date(asOf)` — the FIXTURE world's frozen timestamp used
  // as "now" for age calculations, which made stale rows look fresh. The real
  // clock is the only honest reference here.
  const now = new Date();

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
          <ToolbarChip active={tab === 'ghost'} onClick={() => setTab('ghost')} count={undefined}>Ghost</ToolbarChip>
          <ToolbarChip active={tab === 'blocked'} onClick={() => setTab('blocked')} count={undefined}>Blocked</ToolbarChip>
          <ToolbarSpacer />
          <IconButton ariaLabel="Filter"><Filter size={13} /></IconButton>
          <IconButton ariaLabel="Export"><Download size={13} /></IconButton>
        </>
      }
    >
      {/* M-TRADES-1: authoritative open positions. Rejected under the mock
          adapter, so this is empty and the tabs below say what is missing. */}
      {positions.length > 0 && (
        <Panel provenance="live" title={`Authoritative open positions (${positions.length})`} bodyClassName="p-3">
          <ul className="text-xs mono space-y-1" data-testid="authoritative-positions">
            {positions.map((p, i) => (
              <li key={p.brokerPositionReference ?? `pos-${i}`} data-testid="authoritative-position-row">
                {p.instrument ?? '—'} · {p.side ?? '—'} · qty {p.quantity ?? '—'} · entry {p.entryPrice ?? '—'}
              </li>
            ))}
          </ul>
        </Panel>
      )}
      {tradesStatus === 'stale' && (
        <p className="text-2xs text-[color:var(--warning)] mb-2" data-testid="trades-stale">
          Last reported, not current — these records are past their freshness budget.
        </p>
      )}
      <Panel provenance="live" bodyClassName="p-0" title={`${tab.charAt(0).toUpperCase() + tab.slice(1)} · ${pair}`}>
        {tab === 'open' && (
          <DataTable columns={activeCols} data={open} rowKey={(t) => t.tradeId} onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })} emptyMessage={tradesStatus === 'unavailable' ? 'No authoritative position source — open trades cannot be reported' : 'No open positions reported'} searchable searchAccessor={(t) => `${t.scenarioKey} ${t.state} ${t.protectionStatus}`} />
        )}
        {tab === 'closed' && (
          <DataTable columns={closedCols} data={closed} rowKey={(t) => t.tradeId} onRowClick={(t) => openInspector({ kind: 'trade', tradeId: t.tradeId })} emptyMessage={tradesStatus === 'unavailable' ? 'No authoritative trade history — the durable ledger does not yet state record origin' : 'No closed trades reported'} />
        )}
        {tab === 'ghost' && (
          <DataTable columns={ghostCols} data={trades.ghost} rowKey={(g) => g.ghostTradeId} onRowClick={(g) => openInspector({ kind: 'ghost', ghostTradeId: g.ghostTradeId })} emptyMessage="No authoritative ghost-execution source — fixture ghost trades removed" />
        )}
        {tab === 'blocked' && (
          <DataTable columns={blockedCols} data={trades.blocked} rowKey={(b) => b.blockedIntentId} onRowClick={(b) => openInspector({ kind: 'blocked', blockedIntentId: b.blockedIntentId })} emptyMessage="No authoritative blocked-intent source — fixture blocked intents removed" />
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

  return (
    <WorkspacePage
      title={`${pair} · Edge Monitor`}
      subtitle="Edge performance metrics are not computed for any pair."
    >
      <div className="grid grid-cols-12 gap-4">
        {/* M-EDGE-1: the pair-level fabricated edge metrics, comparisons,
            candidates and the expectancy/drift placeholder charts are GONE. The
            TAB IS DELIBERATELY KEPT: the absence of an edge model is itself
            operationally relevant, and a genuine ledger-backed model can later
            inhabit this surface without another navigation migration. No
            pair-specific claim is made, because no pair-specific model exists. */}
        <Panel provenance="placeholder" title="Edge Performance" className="col-span-12">
          <div className="py-6 px-2 max-w-2xl" data-testid="pair-edge-monitor-not-computed">
            <div className="text-sm text-text font-medium">Edge performance is not computed.</div>
            <p className="text-xs text-text-muted mt-2">
              No edge or performance model is implemented — for this pair or any
              other. Genuine expectancy, win rate and drift must derive from the
              authoritative trade ledger and real broker history. Nothing here is
              estimated from demonstration data.
            </p>
          </div>
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

  // UI-0: this score is derived from cell badges/eligibility. When the grid is
  // partly synthesized the score is NOT a research finding and must say so.
  const synthesizedCells = matrix.synthesizedCells ?? 0;
  const scoreIsSynthesized = synthesizedCells > 0;
  const healthScore = Math.round(((native / cells.length) * 0.5 + (allowed / cells.length) * 0.5) * 100);

  return (
    <WorkspacePage title={`${pair} · Strategy Health`} subtitle="Coverage · stability · integrity">
      <div className="grid grid-cols-12 gap-4">
        <Panel provenance="fixture" title="Health Score" className="col-span-4">
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
            {scoreIsSynthesized && (
              <div className="flex flex-col items-center gap-1" data-testid="health-score-provenance">
                <ProvenanceChip
                  provenance="synthesized"
                  detail={`${synthesizedCells} of ${cells.length} cells are generated — this score is not a research finding.`}
                />
                <span className="text-2xs text-text-muted text-center leading-snug">
                  {synthesizedCells} of {cells.length} cells generated locally
                </span>
              </div>
            )}
          </div>
        </Panel>

        <Panel provenance="fixture" title="Cell Coverage" className="col-span-8">
          <div className="grid grid-cols-4 gap-4">
            <MetricStat label="Total cells" value={cells.length} mono emphasise />
            <MetricStat label="Allowed" value={<span className="mono text-[color:var(--positive)]">{allowed}</span>} sub={`${((allowed / cells.length) * 100).toFixed(0)}%`} emphasise />
            <MetricStat label="NATIVE" value={<span className="mono text-[color:var(--validated)]">{native}</span>} sub={`${((native / cells.length) * 100).toFixed(0)}%`} emphasise />
            <MetricStat label="Under-tested" value={<span className="mono text-[color:var(--warning)]">{insufficient + notTested}</span>} sub={`${(((insufficient + notTested) / cells.length) * 100).toFixed(0)}%`} emphasise />
          </div>
        </Panel>

        <Panel provenance="placeholder" title="Strategy package" className="col-span-6">
          {/* M-PKG-1: rendered the fixture package's domain, instruments, hash,
              validation id, portfolio deltas and component versions. No package
              registry exists, so none of it had a source. */}
          <div className="text-xs text-text-muted" data-testid="pair-package-unavailable">
            {PACKAGES_UNAVAILABLE_DETAIL}
          </div>
        </Panel>

        <Panel provenance="fixture" title="Warning signals" className="col-span-12">
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
      <Panel provenance="mixed" bodyClassName="p-0">
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
