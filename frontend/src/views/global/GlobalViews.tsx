/**
 * Global workspaces: Market Data · Deployments · Strategy Packages · Settings.
 * Every page ships a complete shell; where fixture data exists we use it,
 * otherwise we render placeholder-but-shaped content.
 */
import { useState } from 'react';
import {
  useFleet,
  usePackages,
  useFeatureFlags,
  useVocabulary,
  useMarketState,
  useOperator,
  useActivePackage,
} from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import { Panel } from '@/components/structures/Panel';
import {
  WorkspacePage,
  ToolbarLabel,
  ToolbarChip,
  ToolbarSpacer,
  ToolbarDivider,
  PlaceholderChart,
  PlaceholderMetricGrid,
  ComingSoon,
} from '@/components/structures/WorkspacePage';
import {
  Badge,
  HealthDot,
  KeyValueGrid,
  LaneChip,
  MarketStateBadge,
  MetricStat,
  PLValue,
  PackageVersionChip,
  RValue,
  TimestampUTC,
  ValidationBadgeChip,
} from '@/components/primitives';
import { ChartWorkspace } from '@/components/domain/ChartWorkspace';
import { FeatureGate } from '@/components/FeatureGate';
import { DataTable, type Column } from '@/components/structures/DataTable';
import type { Deployment, Package } from '@/types/domain';
import { fmtHash, fmtPercent, fmtProbability } from '@/lib/format';
import { NavLink } from 'react-router-dom';
import { cn } from '@/lib/utils';
import {
  Radio,
  Cpu,
  Sun,
  Moon,
  Bell,
  User,
  Keyboard,
  Palette,
  Shield,
  Database,
  Wifi,
} from 'lucide-react';
import { toast } from 'sonner';

/* -------------------------------------------------------------------------- */
/*  Market Data                                                                */
/* -------------------------------------------------------------------------- */

export function MarketDataView() {
  const { pairs } = useFleet();
  const [pair, setPair] = useState(pairs[0] ?? 'EURUSD');
  const ms = useMarketState(pair);

  return (
    <WorkspacePage
      title="Market Data"
      subtitle="Feeds · latency · quality · reconciliation"
      toolbar={
        <>
          <ToolbarLabel>Instrument</ToolbarLabel>
          {pairs.map((p) => (
            <ToolbarChip key={p} active={pair === p} onClick={() => setPair(p)}>
              {p}
            </ToolbarChip>
          ))}
          <ToolbarSpacer />
          <span className="text-2xs text-text-muted mono">All feeds via MT5 · reconciled every 30s</span>
        </>
      }
    >
      <div className="grid grid-cols-12 gap-4">
        <Panel title={`${pair} · Live Feed`} className="col-span-12" bodyClassName="p-0">
          <FeatureGate flag="charts">
            <ChartWorkspace instrument={pair} mode="live" volume resizable initialHeight={460} />
          </FeatureGate>
        </Panel>

        <Panel title="Feed Health" className="col-span-4">
          <ul className="space-y-2 text-xs">
            <FeedRow name="Primary tick feed" state="ok" info="42 ms" />
            <FeedRow name="Aggregated bar feed" state="warn" info="40s lag" />
            <FeedRow name="News feed" state="critical" info="disconnected" />
            <FeedRow name="Reconciler" state="ok" info="clean" />
          </ul>
        </Panel>

        <Panel title="Market State (Provenance)" className="col-span-4">
          {ms ? (
            <div className="space-y-2">
              <MarketStateBadge state={ms.state} confidence={ms.confidence} />
              <KeyValueGrid
                items={[
                  { label: 'Known at', value: ms.stateKnownAt, mono: true },
                  { label: 'Model', value: ms.modelVersion, mono: true },
                  { label: 'Source', value: ms.source },
                  { label: 'Shifted', value: `${ms.shiftedDays}d prior` },
                ]}
              />
            </div>
          ) : (
            <div className="text-sm text-text-muted italic">No snapshot</div>
          )}
        </Panel>

        <Panel title="Latency histogram (24h)" className="col-span-4" bodyClassName="p-3">
          <PlaceholderChart height={180} variant="bar" />
        </Panel>

        <Panel title="Snapshot integrity" className="col-span-12">
          <PlaceholderMetricGrid
            items={[
              { label: 'Snapshots today', value: '1,382', sub: 'append-only' },
              { label: 'Divergences', value: '0', sub: 'MT5 vs internal' },
              { label: 'Reconcile clean', value: '100%', sub: 'last 24h' },
              { label: 'Storage', value: '4.2 GB', sub: 'this week' },
            ]}
          />
        </Panel>
      </div>
    </WorkspacePage>
  );
}

function FeedRow({ name, state, info }: { name: string; state: 'ok' | 'warn' | 'critical'; info: string }) {
  return (
    <li className="flex items-center gap-2">
      <HealthDot state={state} />
      <span className="text-text">{name}</span>
      <span className="ml-auto mono text-2xs text-text-muted">{info}</span>
    </li>
  );
}

/* -------------------------------------------------------------------------- */
/*  Deployments                                                                */
/* -------------------------------------------------------------------------- */

export function DeploymentsView() {
  const { deployments, brokers, accounts } = useFleet();
  const openInspector = useShellStore((s) => s.openInspector);

  const cols: Column<Deployment>[] = [
    { key: 'id', header: 'Deployment', mono: true, cell: (d) => d.deploymentId.slice(0, 20) + '…' },
    { key: 'pair', header: 'Pair', mono: true, cell: (d) => d.pair },
    { key: 'lane', header: 'Lane', cell: (d) => <LaneChip lane={d.lane} /> },
    { key: 'status', header: 'Status', cell: (d) => <Badge variant="status">{d.status}</Badge> },
    {
      key: 'mode',
      header: 'Mode',
      cell: (d) => (
        <Badge variant="mode" color={d.executionMode === 'live' ? 'var(--mode-live)' : 'var(--mode-mock)'}>
          {d.executionMode}
        </Badge>
      ),
    },
    {
      key: 'broker',
      header: 'Broker',
      cell: (d) => {
        const acct = accounts.find((a) => a.accountId === d.accountId);
        const br = brokers.find((b) => b.brokerId === acct?.brokerId);
        return <span className="text-xs text-text-2">{br?.venue ?? '—'}</span>;
      },
    },
    { key: 'package', header: 'Package', mono: true, cell: (d) => fmtHash(d.packageHash, 12) },
    { key: 'pl', header: 'Daily P/L', align: 'right', cell: (d) => <PLValue value={d.riskState.dailyPl} /> },
    { key: 'risk', header: 'Risk today', align: 'right', mono: true, cell: (d) => fmtPercent(d.riskState.riskTodayPct) },
    { key: 'dd', header: 'DD buffer', align: 'right', mono: true, cell: (d) => fmtPercent(d.riskState.ddBufferPct, 0) },
    { key: 'action', header: 'Last action', cell: (d) => <span className="text-2xs text-text-muted truncate">{d.lastAction}</span> },
  ];

  const byLane = deployments.reduce<Record<string, number>>((acc, d) => {
    acc[d.lane] = (acc[d.lane] ?? 0) + 1;
    return acc;
  }, {});

  return (
    <WorkspacePage
      title="Deployments"
      subtitle="Every running package × pair × account × lane"
      toolbar={
        <>
          <ToolbarLabel>Lane</ToolbarLabel>
          <ToolbarChip active count={deployments.length}>All</ToolbarChip>
          {Object.entries(byLane).map(([lane, n]) => (
            <ToolbarChip key={lane} count={n}>{lane}</ToolbarChip>
          ))}
          <ToolbarSpacer />
          <span className="text-2xs text-text-muted mono">Atomic version pinning · rollback via manifest</span>
        </>
      }
    >
      <div className="space-y-4">
        <div className="grid grid-cols-4 gap-4">
          <PLKpi label="Deployments" value={deployments.length} />
          <PLKpi label="Live lanes" value={deployments.filter((d) => d.lane === 'live').length} />
          <PLKpi label="Total daily P/L" value={<PLValue value={deployments.reduce((s, d) => s + d.riskState.dailyPl, 0)} />} />
          <PLKpi label="Total floating" value={<PLValue value={deployments.reduce((s, d) => s + d.riskState.floatingPl, 0)} />} />
        </div>

        <Panel title="All Deployments" bodyClassName="p-0">
          <DataTable
            columns={cols}
            data={deployments}
            rowKey={(d) => d.deploymentId}
            onRowClick={(d) => openInspector({ kind: 'deployment', deploymentId: d.deploymentId })}
          />
        </Panel>

        <ComingSoon title="Deployment Manifest editor" description="Create · promote · roll back manifests once mutation flows land." />
      </div>
    </WorkspacePage>
  );
}

function PLKpi({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-md border p-3" style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}>
      <div className="text-[10px] uppercase tracking-widest text-text-muted">{label}</div>
      <div className="text-lg font-semibold text-text mono mt-1">{value}</div>
    </div>
  );
}

/* -------------------------------------------------------------------------- */
/*  Strategy Packages                                                          */
/* -------------------------------------------------------------------------- */

export function StrategyPackagesView() {
  const packages = usePackages();
  const active = useActivePackage();
  const openInspector = useShellStore((s) => s.openInspector);

  const cols: Column<Package>[] = [
    { key: 'version', header: 'Version', cell: (p) => <PackageVersionChip version={p.version} hash={p.packageHash} /> },
    { key: 'label', header: 'Label', cell: (p) => <span className="text-xs text-text">{p.label}</span> },
    { key: 'stage', header: 'Stage', cell: (p) => p.stage },
    { key: 'status', header: 'Status', cell: (p) => <Badge variant="status">{p.status}</Badge> },
    { key: 'validation', header: 'Validation', cell: (p) => <ValidationBadgeChip badge={p.validation.badge} /> },
    { key: 'nD', header: 'nDecided', align: 'right', mono: true, cell: (p) => p.validation.nDecided },
    { key: 'dn', header: 'Δ netR', align: 'right', cell: (p) => <RValue value={p.validation.portfolioDeltas.netR} /> },
    { key: 'stab', header: 'Stability', align: 'right', mono: true, cell: (p) => p.validation.portfolioDeltas.stability.toFixed(2) },
    { key: 'promoted', header: 'Promoted', cell: (p) => (p.promotedAt ? <TimestampUTC iso={p.promotedAt} /> : '—') },
  ];

  return (
    <WorkspacePage
      title="Strategy Packages"
      subtitle="Every package version · immutable · atomic promotion chain"
      toolbar={
        <>
          <ToolbarLabel>Filter</ToolbarLabel>
          <ToolbarChip active count={packages.length}>All</ToolbarChip>
          <ToolbarChip count={packages.filter((p) => p.status === 'active').length}>Active</ToolbarChip>
          <ToolbarChip count={packages.filter((p) => p.status === 'superseded').length}>Superseded</ToolbarChip>
          <ToolbarChip count={packages.filter((p) => p.status === 'draft').length}>Drafts</ToolbarChip>
          <ToolbarSpacer />
          <NavLink to="/package-comparison" className="text-2xs text-text-2 hover:text-text underline underline-offset-4">
            Compare versions →
          </NavLink>
        </>
      }
    >
      <div className="grid grid-cols-12 gap-4">
        <Panel title="Active Package" className="col-span-4">
          <KeyValueGrid
            items={[
              { label: 'Label', value: active.label },
              { label: 'Version', value: `v${active.version}`, mono: true },
              { label: 'Stage', value: active.stage },
              { label: 'Domain', value: active.domain },
              { label: 'Instruments', value: active.instruments.join(', ') },
              { label: 'Hash', value: active.packageHash.slice(0, 20) + '…', mono: true },
              { label: 'Validation', value: <ValidationBadgeChip badge={active.validation.badge} /> },
              { label: 'Promoted by', value: active.promotedBy ?? '—' },
            ]}
          />
        </Panel>

        <Panel title="Portfolio delta trajectory" className="col-span-8" bodyClassName="p-3">
          <PlaceholderChart height={260} variant="area" />
        </Panel>

        <Panel title="All Packages" className="col-span-12" bodyClassName="p-0">
          <DataTable columns={cols} data={packages} rowKey={(p) => `${p.packageId}-${p.version}`} />
        </Panel>
      </div>
    </WorkspacePage>
  );
}

/* -------------------------------------------------------------------------- */
/*  Settings                                                                   */
/* -------------------------------------------------------------------------- */

export function SettingsView() {
  const flags = useFeatureFlags();
  const operator = useOperator();
  const vocab = useVocabulary();
  const theme = useShellStore((s) => s.theme);
  const setTheme = useShellStore((s) => s.setTheme);

  return (
    <WorkspacePage
      title="Settings"
      subtitle="Operator profile · appearance · notifications · feature flags · shortcuts"
    >
      <div className="grid grid-cols-12 gap-4">
        <SettingsSection
          className="col-span-6"
          icon={<User size={13} />}
          title="Operator Profile"
        >
          <KeyValueGrid
            items={[
              { label: 'Display name', value: String(operator.displayName) },
              { label: 'Operator ID', value: String(operator.operatorId), mono: true },
              { label: 'Role', value: <Badge variant="status">{String(operator.role ?? 'Lead')}</Badge> },
              { label: 'Timezone', value: String(operator.timezone ?? 'UTC') },
            ]}
          />
        </SettingsSection>

        <SettingsSection
          className="col-span-6"
          icon={<Palette size={13} />}
          title="Appearance"
        >
          <div className="space-y-3">
            <div>
              <div className="text-2xs uppercase tracking-widest text-text-muted mb-1.5">Theme</div>
              <div className="flex items-center gap-2">
                <ToggleRow
                  active={theme === 'console-dark'}
                  onClick={() => setTheme('console-dark')}
                  icon={<Moon size={12} />}
                  label="Console Dark"
                />
                <ToggleRow
                  active={theme === 'midnight-navy'}
                  onClick={() => setTheme('midnight-navy')}
                  icon={<Moon size={12} />}
                  label="Midnight Navy"
                />
                <ToggleRow
                  active={theme === 'premium-light'}
                  onClick={() => setTheme('premium-light')}
                  icon={<Sun size={12} />}
                  label="Premium Light"
                />
              </div>
            </div>
            <div>
              <div className="text-2xs uppercase tracking-widest text-text-muted mb-1.5">Density</div>
              <div className="flex items-center gap-2">
                <ToggleRow active label="Compact (default)" />
                <ToggleRow disabled label="Comfortable" />
              </div>
            </div>
          </div>
        </SettingsSection>

        <SettingsSection
          className="col-span-6"
          icon={<Bell size={13} />}
          title="Notifications"
        >
          <ul className="space-y-1.5 text-xs">
            {[
              ['Recommendation available', true],
              ['DD buffer breach', true],
              ['Broker reconnect failure', true],
              ['Package promotion', true],
              ['Ghost model outperforms live', false],
            ].map(([name, on]) => (
              <li key={String(name)} className="flex items-center gap-2">
                <span className="w-1.5 h-1.5 rounded-full" style={{ background: on ? 'var(--positive)' : 'var(--text-muted)' }} />
                <span className="text-text">{String(name)}</span>
                <span
                  className="ml-auto text-2xs mono uppercase"
                  style={{ color: on ? 'var(--positive)' : 'var(--text-muted)' }}
                >
                  {on ? 'on' : 'off'}
                </span>
              </li>
            ))}
          </ul>
        </SettingsSection>

        <SettingsSection
          className="col-span-6"
          icon={<Keyboard size={13} />}
          title="Keyboard Shortcuts"
        >
          <ul className="space-y-1.5 text-xs">
            <ShortcutRow keys={['⌘', 'K']} label="Command palette" />
            <ShortcutRow keys={['G', 'F']} label="Go to Fleet Overview" />
            <ShortcutRow keys={['G', 'P']} label="Go to Policy Engine" />
            <ShortcutRow keys={['?']} label="Show all shortcuts" />
            <ShortcutRow keys={['ESC']} label="Close Inspector / Palette" />
          </ul>
        </SettingsSection>

        <SettingsSection
          className="col-span-12"
          icon={<Cpu size={13} />}
          title="Feature Flags"
        >
          <div className="grid grid-cols-3 gap-2">
            {Object.entries(flags).map(([k, v]) => (
              <div
                key={k}
                className="flex items-center gap-2 rounded-sm px-2 py-1.5 border text-xs"
                style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
              >
                <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: v ? 'var(--positive)' : 'var(--text-muted)' }} />
                <span className="text-text-2 truncate">{k}</span>
                <span className="ml-auto text-2xs mono uppercase" style={{ color: v ? 'var(--positive)' : 'var(--text-muted)' }}>
                  {v ? 'on' : 'off'}
                </span>
              </div>
            ))}
          </div>
        </SettingsSection>

        <SettingsSection
          className="col-span-12"
          icon={<Database size={13} />}
          title="Vocabulary (read-only · from contract)"
        >
          <KeyValueGrid
            items={[
              { label: 'Domain', value: vocab.domain },
              { label: 'Sessions', value: vocab.sessions.join(' · '), mono: true },
              { label: 'Structures', value: vocab.structures.join(' · '), mono: true },
              { label: 'Directions', value: vocab.directions.join(' · '), mono: true },
              { label: 'Market states', value: vocab.marketStates.join(' · '), mono: true },
              { label: 'RR ladder', value: vocab.rrLadder.join(' · '), mono: true },
            ]}
          />
        </SettingsSection>
      </div>
    </WorkspacePage>
  );
}

function SettingsSection({
  icon,
  title,
  children,
  className,
}: {
  icon: React.ReactNode;
  title: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={className}>
      <Panel
        title={
          <span className="flex items-center gap-2">
            {icon}
            {title}
          </span>
        }
      >
        {children}
      </Panel>
    </div>
  );
}

function ToggleRow({
  active,
  disabled,
  onClick,
  icon,
  label,
}: {
  active?: boolean;
  disabled?: boolean;
  onClick?: () => void;
  icon?: React.ReactNode;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        'inline-flex items-center gap-1.5 h-7 px-2.5 rounded-sm text-xs font-medium border transition-colors',
        active
          ? 'bg-[color:var(--panel)] text-text border-[color:var(--border)]'
          : 'bg-transparent text-text-2 border-[color:var(--border-subtle)] hover:text-text',
        disabled && 'opacity-40 cursor-not-allowed'
      )}
    >
      {icon}
      {label}
    </button>
  );
}

function ShortcutRow({ keys, label }: { keys: string[]; label: string }) {
  return (
    <li className="flex items-center gap-2">
      <span className="text-text-2">{label}</span>
      <span className="ml-auto flex items-center gap-1">
        {keys.map((k, i) => (
          <kbd
            key={i}
            className="mono text-2xs px-1.5 py-0.5 rounded-sm border"
            style={{ background: 'var(--panel-2)', borderColor: 'var(--border)', color: 'var(--text)' }}
          >
            {k}
          </kbd>
        ))}
      </span>
    </li>
  );
}
