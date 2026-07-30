import { useBrokerHealth } from '@/hooks/useRepository';
import { Panel } from '@/components/structures/Panel';
import { HealthDot, MetricStat, Badge, TimestampUTC } from '@/components/primitives';
import { CheckCircle2 } from 'lucide-react';

export function BrokerHealthView() {
  const { brokers, health } = useBrokerHealth();

  return (
    <div className="p-6 h-full min-h-0 overflow-auto space-y-4">
      <div>
        <h1 className="text-2xl font-semibold text-text tracking-tight">Broker Health</h1>
        <p className="text-xs text-text-muted mt-1">
          Connection · latency · heartbeat · spread · slippage · rejects · reconciliation diff · timeline
        </p>
      </div>

      {brokers.map((broker) => {
        const h = health.find((x) => x.brokerId === broker.brokerId);
        return (
          <Panel
            provenance="fixture"
            key={broker.brokerId}
            title={
              <span className="flex items-center gap-2">
                <HealthDot state={broker.status === 'connected' ? 'ok' : 'critical'} />
                {broker.venue}
                <span className="text-text-muted mono ml-2">· {broker.adapterType}</span>
              </span>
            }
            actions={
              <Badge variant="health" color="var(--positive)">
                {broker.status}
              </Badge>
            }
          >
            <div className="grid grid-cols-4 gap-6 mb-4">
              <MetricStat label="Latency" value={<span className="mono">{h?.latencyMs ?? '—'}ms</span>} emphasise />
              <MetricStat label="Execution" value={<span className="mono">{h?.executionSpeedMs ?? '—'}ms</span>} emphasise />
              <MetricStat label="Heartbeat" value={<span className="mono">{h?.heartbeatAgeMs ?? '—'}ms</span>} emphasise />
              <MetricStat label="Reconnects" value={<span className="mono">{h?.reconnects ?? 0}</span>} emphasise />
              <MetricStat label="Spread" value={<span className="mono">{h?.spread?.toFixed(5) ?? '—'}</span>} />
              <MetricStat label="Slippage" value={<span className="mono">{h?.slippage?.toFixed(5) ?? '—'}</span>} />
              <MetricStat label="Rejects" value={<span className="mono">{h?.orderRejects ?? 0}</span>} />
              <MetricStat
                label="Reconciliation"
                value={
                  h?.reconciliation?.clean ? (
                    <span className="flex items-center gap-1.5 text-[color:var(--positive)]">
                      <CheckCircle2 size={13} /> Clean
                    </span>
                  ) : (
                    <span className="text-[color:var(--negative)]">Divergent</span>
                  )
                }
              />
            </div>

            {h?.timeline?.length ? (
              <div>
                <div className="text-2xs uppercase tracking-widest text-text-muted mb-2">
                  Health Timeline
                </div>
                <ul className="space-y-1.5">
                  {h.timeline.map((ev, i) => (
                    <li
                      key={i}
                      className="rounded-md border p-2 flex items-center gap-3 text-xs"
                      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel-2)' }}
                    >
                      <Badge variant={ev.resolved ? 'validation' : 'live'} size="sm">
                        {ev.resolved ? 'resolved' : 'active'}
                      </Badge>
                      <span className="mono text-text">{ev.code}</span>
                      <span className="text-text-2 flex-1 truncate">{ev.detail}</span>
                      <TimestampUTC iso={ev.at} />
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </Panel>
        );
      })}
    </div>
  );
}
