import { Outlet, NavLink, useParams, useLocation, Navigate } from 'react-router-dom';
import { cn } from '@/lib/utils';
import { usePairWorkspace, useActivePackage, useMarketState } from '@/hooks/useRepository';
import { useShellStore } from '@/store/shellStore';
import { useEffect } from 'react';
import {
  MarketStateBadge,
  PackageVersionChip,
  PLValue,
} from '@/components/primitives';
import {
  Gauge,
  Grid3x3,
  PlayCircle,
  ListOrdered,
  TrendingUp,
  BarChart3,
  Activity,
  HeartPulse,
  Clock,
} from 'lucide-react';

/**
 * PairWorkspace — reusable template. Instrument-agnostic: swap `:pairId`
 * to see any pair (EURUSD, GBPUSD, XAUUSD, NQ, ES, BTC…) without redesign.
 * 9 tabs; every one has a shell + real or placeholder content.
 */

const TABS = [
  { path: 'dashboard', label: 'Dashboard', icon: Gauge },
  { path: 'policy', label: 'Policy Engine', icon: Grid3x3 },
  { path: 'replay', label: 'Replay', icon: PlayCircle },
  { path: 'orders', label: 'Orders', icon: ListOrdered },
  { path: 'trades', label: 'Trades', icon: TrendingUp },
  { path: 'analytics', label: 'Analytics', icon: BarChart3 },
  { path: 'edge-monitor', label: 'Edge Monitor', icon: Activity },
  { path: 'strategy-health', label: 'Strategy Health', icon: HeartPulse },
  { path: 'activity', label: 'Activity Timeline', icon: Clock },
] as const;

export function PairWorkspace() {
  const { pairId } = useParams<{ pairId: string }>();
  const location = useLocation();
  const setActivePair = useShellStore((s) => s.setActivePair);
  const pair = pairId ?? 'EURUSD';

  useEffect(() => {
    setActivePair(pair);
  }, [pair, setActivePair]);

  const ws = usePairWorkspace(pair);
  const pkg = useActivePackage();
  const ms = useMarketState(pair);

  // Redirect to dashboard by default when hitting /pair/:pairId
  if (location.pathname === `/pair/${pair}` || location.pathname === `/pair/${pair}/`) {
    return <Navigate to={`/pair/${pair}/dashboard`} replace />;
  }

  const openLive = ws.trades.filter((t) => t.state !== 'closed');
  const dailyPl = ws.deployments.reduce((sum, d) => sum + d.riskState.dailyPl, 0);
  const floating = ws.deployments.reduce((sum, d) => sum + d.riskState.floatingPl, 0);

  return (
    <div className="flex flex-col h-full min-h-0">
      {/* Pair header */}
      <header
        className="flex items-center gap-6 px-6 h-14 border-b shrink-0"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
      >
        <div className="flex items-center gap-3">
          <div
            className="w-9 h-9 rounded-md flex items-center justify-center text-sm font-bold mono border"
            style={{ background: 'var(--panel-2)', borderColor: 'var(--border)', color: 'var(--text)' }}
          >
            {pair.slice(0, 3)}
          </div>
          <div>
            <h1 className="text-lg font-semibold text-text mono">{pair}</h1>
            <div className="text-2xs text-text-muted">{assetClassOf(pair)}</div>
          </div>
        </div>

        <div className="h-8 w-px" style={{ background: 'var(--border-subtle)' }} />

        {ms && <MarketStateBadge state={ms.state} confidence={ms.confidence} confirmed={ms.confirmed} />}
        <PackageVersionChip version={pkg.version} hash={pkg.packageHash} />

        <div className="ml-auto flex items-center gap-5 text-xs">
          <HeaderStat label="Deployments" value={ws.deployments.length} mono />
          <HeaderStat label="Open" value={openLive.length} mono />
          <HeaderStat label="Daily P/L" value={<PLValue value={dailyPl} />} />
          <HeaderStat label="Floating" value={<PLValue value={floating} />} />
        </div>
      </header>

      {/* Tabs */}
      <nav
        className="flex items-center gap-0.5 px-4 h-10 border-b shrink-0 overflow-x-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
        data-testid="pair-workspace-tabs"
      >
        {TABS.map((tab) => (
          <PairTab key={tab.path} to={`/pair/${pair}/${tab.path}`} icon={<tab.icon size={12} />}>
            {tab.label}
          </PairTab>
        ))}
      </nav>

      <div className="flex-1 min-h-0 overflow-hidden">
        <Outlet context={{ pair }} />
      </div>
    </div>
  );
}

function assetClassOf(pair: string): string {
  if (['EURUSD', 'GBPUSD', 'USDJPY', 'AUDUSD', 'USDCHF'].includes(pair)) return 'FX Major';
  if (['XAUUSD', 'XAGUSD'].includes(pair)) return 'Precious metal';
  if (['NQ', 'ES', 'YM', 'RTY'].includes(pair)) return 'Index future';
  if (['BTC', 'ETH', 'SOL'].includes(pair)) return 'Digital asset';
  return 'Institutional';
}

function PairTab({
  to,
  icon,
  children,
}: {
  to: string;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <NavLink
      to={to}
      className={({ isActive }) =>
        cn(
          'inline-flex items-center gap-1.5 h-8 px-3 rounded-sm text-xs font-medium transition-colors duration-fast whitespace-nowrap',
          isActive
            ? 'bg-[color:var(--panel)] text-text border border-[color:var(--border)]'
            : 'text-text-2 border border-transparent hover:text-text hover:bg-[color:var(--panel)]'
        )
      }
    >
      {icon}
      {children}
    </NavLink>
  );
}

function HeaderStat({
  label,
  value,
  mono,
}: {
  label: string;
  value: React.ReactNode;
  mono?: boolean;
}) {
  return (
    <div className="flex flex-col leading-tight">
      <span className="text-2xs uppercase tracking-widest text-text-muted">{label}</span>
      <span className={cn('text-sm font-semibold text-text tabular', mono && 'mono')}>{value}</span>
    </div>
  );
}
