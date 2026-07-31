import { useFleet, useRepository } from '@/hooks/useRepository';
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
  const { deployments, brokers, accounts, activePackage, asOf, available, provenance, provenanceDetail } =
    useFleet();
  const { world } = useRepository();
  const msByPair = Object.fromEntries(world.marketStateSnapshots.map((m) => [m.instrument, m]));
  const navigate = useNavigate();
  const setActivePair = useShellStore((s) => s.setActivePair);
  const openInspector = useShellStore((s) => s.openInspector);


  return (
    <div className="grid grid-cols-[1fr_320px] h-full min-h-0">
      {/* Main — FleetGrid */}
      <div className="flex flex-col overflow-hidden">
        <header className="px-6 pt-6 pb-4 shrink-0">
          <div className="flex items-baseline justify-between gap-4">
            <div>
              <h1 className="text-2xl font-semibold text-text tracking-tight">Fleet Overview</h1>
              {/* M-FLEET-1: counts are DERIVED from the records actually
                  returned, and the word before them states what those records
                  are. With no source there is nothing to count, so no number is
                  shown — "0 deployments" would assert an empty fleet, which is a
                  different fact from "this surface has no production source". */}
              <p className="text-xs text-text-muted mt-1" data-testid="fleet-summary">
                {available ? (
                  <>
                    {deployments.length} {provenance === 'fixture' ? 'fixture' : ''} deployment
                    {deployments.length === 1 ? '' : 's'} · {brokers.length} broker
                    {brokers.length === 1 ? '' : 's'} · {accounts.length} account
                    {accounts.length === 1 ? '' : 's'} · Package {activePackage.label}
                  </>
                ) : (
                  <>Fleet composition unavailable — no production source</>
                )}
              </p>
            </div>
            <div className="flex items-center gap-3">
              {provenance === 'fixture' && (
                <span
                  data-testid="fleet-provenance-banner"
                  title={provenanceDetail}
                  className="text-2xs uppercase tracking-widest px-2 py-0.5 rounded border mono"
                  style={{ borderColor: 'var(--mode-mock)', color: 'var(--mode-mock)' }}
                >
                  Fixture data — not live
                </span>
              )}
              <PackageVersionChip version={activePackage.version} hash={activePackage.packageHash} />
              {available && asOf ? (
                <span className="text-2xs text-text-muted mono">
                  as of <TimestampUTC iso={asOf} mode="absolute" />
                </span>
              ) : null}
            </div>
          </div>
        </header>

        <div className="flex-1 overflow-auto px-6 pb-6">
          <ProvenanceFrame provenance={available ? 'fixture' : 'placeholder'} className="p-2">
          {/* M-FLEET-1: three DISTINCT states, never conflated.
              no source  -> say so; do not render an empty grid that reads as "no deployments"
              source, 0  -> a genuine empty fleet
              source, n  -> render exactly the n records returned, labelled */}
          {!available ? (
            <EmptyState
              title="Fleet composition unavailable"
              description={provenanceDetail ||
                'Deployment, broker and account records have no production source in this process.'}
              icon={<Layers size={16} />}
            />
          ) : deployments.length === 0 ? (
            <EmptyState
              title="No deployments"
              description="The fleet source returned no deployment records. This is a real empty fleet, not missing data."
              icon={<Layers size={16} />}
            />
          ) : (
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
                    {/* M-FLEET-1: the single most dangerous element on this page
                        was a FIXTURE record wearing a green `live` badge. The
                        record's own mode is still shown — hiding it would be its
                        own dishonesty — but a fixture deployment never gets the
                        live/demo colour, and carries an explicit tag. */}
                    {provenance === 'fixture' && (
                      <span data-testid={`deployment-fixture-tag-${d.deploymentId}`}>
                        <Badge variant="mode" color="var(--mode-mock)">FIXTURE</Badge>
                      </span>
                    )}
                    <Badge variant="status">{d.status}</Badge>
                    <Badge
                      variant="mode"
                      color={
                        provenance !== 'fixture' && d.executionMode === 'live'
                          ? 'var(--mode-live)'
                          : provenance !== 'fixture' && d.executionMode === 'demo'
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
          )}
          </ProvenanceFrame>
        </div>
      </div>

      {/* Attention rail */}
      <aside
        className="border-l overflow-y-auto"
        style={{ borderColor: 'var(--border-subtle)', background: 'var(--bg-elevated)' }}
      >
        <Panel
          provenance="placeholder"
          title={
            <span className="flex items-center gap-2">
              <AlertTriangle size={12} className="text-[color:var(--warning)]" />
              Attention Rail
            </span>
          }
          className="border-0 rounded-none h-full"
        >
          {/* M-CONF-1: the fixture confidence signals (attention list +
              "healthy signals" grid with invented values) are GONE. No
              confidence model exists; this rail is honest about that until
              real node telemetry can populate it. */}
          <EmptyState
            title="No attention model"
            description="System confidence is not computed — no confidence model exists. Attention signals will populate from genuine node telemetry when implemented."
            icon={<AlertTriangle size={16} />}
          />
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
