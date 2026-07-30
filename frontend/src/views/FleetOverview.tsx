import { useFleet, useSystemConfidence, useRepository } from '@/hooks/useRepository';
import { useNavigate } from 'react-router-dom';
import { useShellStore } from '@/store/shellStore';
import { Card, Panel, EmptyState, ProvenanceFrame } from '@/components/structures/Panel';
import {
  Badge,
  HealthDot,
  LaneChip,
  MarketStateBadge,
  PackageVersionChip,
  PLValue,
  RValue,
  TimestampUTC,
} from '@/components/primitives';
import { AlertTriangle, ChevronRight, Radio, Layers } from 'lucide-react';
import { fmtPercent } from '@/lib/format';

/**
 * FleetOverview — global. FleetGrid of DeploymentCards + attention rail.
 * Answers "what needs my attention?" — not charts (§J).
 */
export function FleetOverview() {
  const { deployments, brokers, accounts, activePackage, asOf } = useFleet();
  const confidence = useSystemConfidence();
  const { world } = useRepository();
  const msByPair = Object.fromEntries(world.marketStateSnapshots.map((m) => [m.instrument, m]));
  const navigate = useNavigate();
  const setActivePair = useShellStore((s) => s.setActivePair);
  const openInspector = useShellStore((s) => s.openInspector);

  const attention = confidence.signals.filter((s) => s.state !== 'ok');

  return (
    <div className="grid grid-cols-[1fr_320px] h-full min-h-0">
      {/* Main — FleetGrid */}
      <div className="flex flex-col overflow-hidden">
        <header className="px-6 pt-6 pb-4 shrink-0">
          <div className="flex items-baseline justify-between gap-4">
            <div>
              <h1 className="text-2xl font-semibold text-text tracking-tight">Fleet Overview</h1>
              <p className="text-xs text-text-muted mt-1">
                {deployments.length} active deployments · {brokers.length} broker{brokers.length > 1 ? 's' : ''} · {accounts.length} account{accounts.length > 1 ? 's' : ''} · Package {activePackage.label}
              </p>
            </div>
            <div className="flex items-center gap-3">
              <PackageVersionChip version={activePackage.version} hash={activePackage.packageHash} />
              <span className="text-2xs text-text-muted mono">
                as of <TimestampUTC iso={asOf} mode="absolute" />
              </span>
            </div>
          </div>
        </header>

        <div className="flex-1 overflow-auto px-6 pb-6">
          <ProvenanceFrame provenance="fixture" className="p-2">
          <div className="grid gap-3 grid-cols-1 xl:grid-cols-2 2xl:grid-cols-3">
            {deployments.map((d) => {
              const account = accounts.find((a) => a.accountId === d.accountId);
              const broker = brokers.find((b) => b.brokerId === account?.brokerId);
              return (
                <Card
                  key={d.deploymentId}
                  interactive
                  onClick={() => {
                    setActivePair(d.pair);
                    navigate(`/pair/${d.pair}/dashboard`);
                  }}
                  className="p-4"
                  data-testid={`deployment-card-${d.deploymentId}`}
                >
                  <div className="flex items-center justify-between gap-2 mb-3">
                    <div className="flex items-center gap-2 min-w-0">
                      <div
                        className="w-9 h-9 rounded-md flex items-center justify-center text-xs font-bold mono border shrink-0"
                        style={{
                          background: 'var(--panel-2)',
                          borderColor: 'var(--border)',
                          color: 'var(--text)',
                        }}
                      >
                        {d.pair.slice(0, 3)}
                      </div>
                      <div className="min-w-0">
                        <div className="text-sm font-semibold text-text mono">{d.pair}</div>
                        <div className="text-2xs text-text-muted truncate">
                          {broker?.venue} · {account?.type}
                        </div>
                      </div>
                    </div>
                    <LaneChip lane={d.lane} />
                  </div>

                  <div className="flex items-center gap-2 mb-3 flex-wrap">
                    <Badge variant="status">{d.status}</Badge>
                    <Badge
                      variant="mode"
                      color={
                        d.executionMode === 'live'
                          ? 'var(--mode-live)'
                          : d.executionMode === 'demo'
                          ? 'var(--mode-demo)'
                          : 'var(--mode-mock)'
                      }
                    >
                      {d.executionMode}
                    </Badge>
                    {msByPair[d.pair] && (
                      <MarketStateBadge
                        state={msByPair[d.pair].state}
                        confidence={msByPair[d.pair].confidence}
                        confirmed={msByPair[d.pair].confirmed}
                      />
                    )}
                  </div>

                  <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 mb-3 text-xs">
                    <MetricRow label="Daily P/L" value={<PLValue value={d.riskState.dailyPl} />} />
                    <MetricRow label="Floating" value={<PLValue value={d.riskState.floatingPl} />} />
                    <MetricRow label="Risk today" value={<span className="mono">{fmtPercent(d.riskState.riskTodayPct)}</span>} />
                    <MetricRow
                      label="DD buffer"
                      value={
                        <span className="mono" style={{ color: d.riskState.ddBufferPct < 40 ? 'var(--warning)' : 'var(--text)' }}>
                          {fmtPercent(d.riskState.ddBufferPct, 0)}
                        </span>
                      }
                    />
                    <MetricRow label="Orders" value={<span className="mono">{d.riskState.openOrders}</span>} />
                    <MetricRow label="Trades" value={<span className="mono">{d.riskState.openTrades}</span>} />
                  </div>

                  <div
                    className="text-xs text-text-2 border-t pt-2 mt-2 truncate"
                    style={{ borderColor: 'var(--border-subtle)' }}
                    title={d.lastAction}
                  >
                    {d.lastAction}
                  </div>

                  <button
                    onClick={(e) => {
                      e.stopPropagation();
                      openInspector({ kind: 'deployment', deploymentId: d.deploymentId });
                    }}
                    className="mt-2 w-full flex items-center justify-center gap-1 text-2xs uppercase tracking-widest text-text-muted hover:text-text py-1"
                  >
                    Inspect
                    <ChevronRight size={11} />
                  </button>
                </Card>
              );
            })}
          </div>
          </ProvenanceFrame>
        </div>
      </div>

      {/* Attention rail */}
      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <Panel
          provenance="fixture"
          title={
            <span className="flex items-center gap-2">
              <AlertTriangle size={12} className="text-[color:var(--warning)]" />
              Attention Rail
            </span>
          }
          className="border-0 rounded-none h-full"
        >
          {attention.length === 0 ? (
            <EmptyState
              title="All systems nominal"
              description="No warnings or critical alerts across the fleet."
              icon={<HealthDot state="ok" size="md" />}
            />
          ) : (
            <ul className="space-y-2">
              {attention.map((s) => (
                <li
                  key={s.key}
                  className="rounded-md p-2.5 border"
                  style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
                >
                  <div className="flex items-center gap-2">
                    <HealthDot state={s.state === 'warn' ? 'warn' : 'critical'} />
                    <span className="text-2xs uppercase tracking-widest text-text-muted">
                      {s.key}
                    </span>
                    <span className="ml-auto mono text-2xs text-text-muted">{s.value}</span>
                  </div>
                  <p className="text-xs text-text mt-1.5">{s.message}</p>
                  <p className="text-2xs text-text-muted mt-1">
                    <TimestampUTC iso={s.since} />
                  </p>
                </li>
              ))}
              <li className="mt-4 pt-2 border-t" style={{ borderColor: 'var(--border-subtle)' }}>
                <div className="text-2xs uppercase tracking-widest text-text-muted mb-2">Healthy signals</div>
                <div className="grid grid-cols-2 gap-1.5">
                  {confidence.signals
                    .filter((s) => s.state === 'ok')
                    .map((s) => (
                      <div key={s.key} className="flex items-center gap-1.5 text-2xs text-text-2">
                        <HealthDot state="ok" size="sm" />
                        <span className="truncate">{s.key}</span>
                      </div>
                    ))}
                </div>
              </li>
            </ul>
          )}
        </Panel>
      </aside>
    </div>
  );
}

function MetricRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-2xs uppercase tracking-widest text-text-muted">{label}</span>
      <span className="text-xs font-medium">{value}</span>
    </div>
  );
}
