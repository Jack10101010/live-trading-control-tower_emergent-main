import { NavLink, useLocation } from 'react-router-dom';
import { cn } from '@/lib/utils';
import { useFleet, useFeatureFlags } from '@/hooks/useRepository';
import type { FeatureFlag } from '@/types/domain';
import { useShellStore } from '@/store/shellStore';
import {
  LayoutGrid,
  Wifi,
  Shield,
  Server,
  ChevronRight,
  ChevronDown,
  CircleDot,
  Activity,
  Radio,
  Database,
  Package as PackageIcon,
  Settings as SettingsIcon,
  GitBranch,
  GitCompare,
  Lock,
  Gauge,
  Grid3x3,
  PlayCircle,
  ListOrdered,
  TrendingUp,
  BarChart3,
  HeartPulse,
  Clock,
  type LucideIcon,
} from 'lucide-react';
import { useState, type ReactNode } from 'react';
import { HealthDot } from '@/components/primitives';

/**
 * ScopeNavigator — left rail. Full application structure exposed here.
 * Instruments (currently: EURUSD; ready for GBPUSD, XAUUSD, NQ, ES, BTC…)
 * expand into their PairWorkspace tab tree so users can jump directly to
 * any tab of any pair without visiting the pair dashboard first.
 */
export function ScopeNavigator() {
  const { deployments, brokers, accounts, pairs } = useFleet();
  const flags = useFeatureFlags();
  const location = useLocation();
  const activePair = useShellStore((s) => s.activePair);
  const setActivePair = useShellStore((s) => s.setActivePair);
  const [expanded, setExpanded] = useState<Set<string>>(new Set(['pair:EURUSD', 'fleet']));

  const toggle = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <nav
      className="flex flex-col h-full shrink-0 overflow-hidden"
      style={{ width: 260, background: 'var(--bg-elevated)', borderRight: '1px solid var(--border)' }}
      data-testid="scope-navigator"
    >
      <div className="flex-1 overflow-auto py-3">
        {/* GLOBAL */}
        <SectionLabel>Global</SectionLabel>
        <NavItem to="/fleet" icon={<LayoutGrid size={14} />}>Fleet Overview</NavItem>
        {flags.brokerHealth && <NavItem to="/broker-health" icon={<Wifi size={14} />}>Broker Health</NavItem>}
        {flags.accountsProtection && <NavItem to="/accounts" icon={<Shield size={14} />}>Accounts &amp; Protection</NavItem>}
        <NavItem to="/market-data" icon={<Radio size={14} />}>Market Data</NavItem>
        <NavItem to="/deployments" icon={<Database size={14} />}>Deployments</NavItem>
        <NavItem to="/strategy-packages" icon={<PackageIcon size={14} />}>Strategy Packages</NavItem>
        {flags.edgeMonitor && <NavItem to="/edge-monitor" icon={<Activity size={14} />}>Edge Monitor</NavItem>}
        <NavItem to="/system" icon={<Server size={14} />}>System</NavItem>
        <NavItem to="/settings" icon={<SettingsIcon size={14} />}>Settings</NavItem>

        {/* Deferred placeholders */}
        <div className="mt-2 pt-2 border-t mx-2" style={{ borderColor: 'var(--border-subtle)' }}>
          {flags.versionHistory && <NavItem to="/version-history" icon={<GitBranch size={14} />} trailing={<Lock size={9} className="text-text-muted" />}>Version History</NavItem>}
          {flags.packageComparison && <NavItem to="/package-comparison" icon={<GitCompare size={14} />} trailing={<Lock size={9} className="text-text-muted" />}>Package Comparison</NavItem>}
        </div>

        {/* SCOPE — fleet drill */}
        <div className="mt-3 pt-3 border-t mx-2" style={{ borderColor: 'var(--border-subtle)' }} />
        <SectionLabel>Scope</SectionLabel>
        <div className="px-2">
          <button
            onClick={() => toggle('fleet')}
            className="flex items-center gap-1 w-full text-left h-7 px-1.5 text-xs text-text hover:bg-[color:var(--panel-2)] rounded-sm"
          >
            {expanded.has('fleet') ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
            <LayoutGrid size={12} className="ml-0.5" />
            <span className="ml-1 font-medium">Fleet</span>
            <span className="ml-auto text-2xs text-text-muted mono">{deployments.length} dep</span>
          </button>

          {expanded.has('fleet') && (
            <div className="pl-3 mt-0.5 space-y-0.5">
              {brokers.map((broker) => {
                const brokerAccounts = accounts.filter((a) => a.brokerId === broker.brokerId);
                const brokerId = `b:${broker.brokerId}`;
                return (
                  <div key={broker.brokerId}>
                    <button
                      onClick={() => toggle(brokerId)}
                      className="flex items-center gap-1 w-full text-left h-6 px-1.5 text-2xs text-text-2 hover:bg-[color:var(--panel-2)] rounded-sm"
                    >
                      {expanded.has(brokerId) ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
                      <HealthDot state={broker.status === 'connected' ? 'ok' : 'critical'} />
                      <span className="ml-1 truncate">{broker.venue}</span>
                    </button>
                    {expanded.has(brokerId) && (
                      <div className="pl-4 space-y-0.5">
                        {brokerAccounts.map((acct) => {
                          const deps = deployments.filter((d) => d.accountId === acct.accountId);
                          return (
                            <div key={acct.accountId} className="text-2xs text-text-muted">
                              <div className="uppercase tracking-wider py-1">{acct.type}</div>
                              {deps.map((d) => (
                                <button
                                  key={d.deploymentId}
                                  onClick={() => setActivePair(d.pair)}
                                  className={cn(
                                    'flex items-center gap-2 w-full text-left h-6 px-1.5 rounded-sm text-2xs hover:bg-[color:var(--panel-2)]',
                                    activePair === d.pair && 'bg-[color:var(--selection)] text-text'
                                  )}
                                >
                                  <CircleDot
                                    size={9}
                                    style={{
                                      color:
                                        d.status === 'InTrade'
                                          ? 'var(--live)'
                                          : d.status === 'Armed'
                                          ? 'var(--warning)'
                                          : 'var(--paused)',
                                    }}
                                  />
                                  <span className="font-medium mono">{d.pair}</span>
                                  <span className="ml-auto text-text-muted uppercase">{d.lane}</span>
                                </button>
                              ))}
                            </div>
                          );
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}
        </div>

        {/* PAIRS — every pair expandable to its tab tree */}
        <div className="mt-3 pt-3 border-t mx-2" style={{ borderColor: 'var(--border-subtle)' }} />
        <SectionLabel>Pairs</SectionLabel>
        <div className="px-2 space-y-0.5">
          {pairs.map((pair) => (
            <PairNode
              key={pair}
              pair={pair}
              expanded={expanded.has(`pair:${pair}`)}
              onToggle={() => toggle(`pair:${pair}`)}
              active={activePair === pair}
              locationPath={location.pathname}
            />
          ))}

          {/* UI-0: layout placeholders ONLY. These instruments are not configured
              deployments and must never read as such — they stay non-interactive and
              are explicitly labelled. Creating a deployment is out of scope here. */}
          <div
            className="mt-2 px-2 text-[9px] uppercase tracking-widest text-text-muted"
            data-testid="placeholder-pairs-heading"
          >
            Placeholders · not configured
          </div>
          {['GBPUSD', 'XAUUSD', 'NQ', 'ES', 'BTC'].map((p) => (
            <div
              key={`ghost-${p}`}
              className="flex items-center gap-2 h-7 px-2 rounded-sm text-2xs text-text-muted opacity-50 cursor-not-allowed"
              title="Layout placeholder — no deployment exists for this instrument and none can be created here."
              aria-disabled="true"
              data-testid={`placeholder-pair-${p}`}
              data-provenance="placeholder"
            >
              <CircleDot size={9} className="text-text-muted" />
              <span className="mono">{p}</span>
              <span className="ml-auto flex items-center gap-1">
                <span className="text-[8px] uppercase tracking-wider">placeholder</span>
                <Lock size={9} className="text-text-muted" />
              </span>
            </div>
          ))}
        </div>
      </div>
    </nav>
  );
}

const PAIR_TABS: Array<{ path: string; label: string; icon: LucideIcon; flag?: FeatureFlag }> = [
  { path: 'dashboard', label: 'Dashboard', icon: Gauge },
  { path: 'policy', label: 'Policy Engine', icon: Grid3x3 },
  { path: 'replay', label: 'Replay', icon: PlayCircle, flag: 'replay' },
  { path: 'orders', label: 'Orders', icon: ListOrdered },
  { path: 'trades', label: 'Trades', icon: TrendingUp },
  { path: 'analytics', label: 'Analytics', icon: BarChart3, flag: 'analytics' },
  { path: 'edge-monitor', label: 'Edge Monitor', icon: Activity, flag: 'edgeMonitor' },
  { path: 'strategy-health', label: 'Strategy Health', icon: HeartPulse },
  { path: 'activity', label: 'Activity Timeline', icon: Clock },
];

function PairNode({
  pair,
  expanded,
  onToggle,
  active,
  locationPath,
}: {
  pair: string;
  expanded: boolean;
  onToggle: () => void;
  active: boolean;
  locationPath: string;
}) {
  const flags = useFeatureFlags();
  const isCurrent = locationPath.startsWith(`/pair/${pair}`);
  return (
    <div>
      <div
        className={cn(
          'flex items-center gap-1 h-7 rounded-sm text-xs hover:bg-[color:var(--panel-2)]',
          (active || isCurrent) && !expanded ? 'bg-[color:var(--selection)] text-text' : 'text-text-2'
        )}
      >
        <button onClick={onToggle} className="flex items-center pl-1.5 pr-1 h-full">
          {expanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        </button>
        <NavLink
          to={`/pair/${pair}/dashboard`}
          className={({ isActive }) =>
            cn(
              'flex-1 inline-flex items-center gap-1.5 h-full',
              isActive ? 'text-text' : ''
            )
          }
        >
          <CircleDot size={9} className="text-[color:var(--live)]" />
          <span className="mono font-medium">{pair}</span>
        </NavLink>
      </div>
      {expanded && (
        <div className="pl-4 space-y-0.5 mt-0.5">
          {PAIR_TABS.filter((t) => !t.flag || flags[t.flag]).map((t) => (
            <NavLink
              key={t.path}
              to={`/pair/${pair}/${t.path}`}
              className={({ isActive }) =>
                cn(
                  'flex items-center gap-2 h-6 px-2 rounded-sm text-2xs hover:bg-[color:var(--panel-2)]',
                  isActive ? 'bg-[color:var(--selection)] text-text' : 'text-text-muted hover:text-text-2'
                )
              }
            >
              <t.icon size={11} />
              <span>{t.label}</span>
            </NavLink>
          ))}
        </div>
      )}
    </div>
  );
}

function SectionLabel({ children }: { children: ReactNode }) {
  return (
    <div className="px-3 pb-1 pt-1 text-[9px] uppercase tracking-widest text-text-muted font-medium">
      {children}
    </div>
  );
}

function NavItem({
  to,
  icon,
  trailing,
  children,
}: {
  to: string;
  icon: ReactNode;
  trailing?: ReactNode;
  children: ReactNode;
}) {
  return (
    <div className="px-2">
      <NavLink
        to={to}
        end
        className={({ isActive }) =>
          cn(
            'flex items-center gap-2 h-7 px-2 rounded-sm text-xs hover:bg-[color:var(--panel-2)]',
            isActive ? 'bg-[color:var(--selection)] text-text' : 'text-text-2'
          )
        }
      >
        {icon}
        <span className="flex-1 truncate">{children}</span>
        {trailing}
      </NavLink>
    </div>
  );
}
