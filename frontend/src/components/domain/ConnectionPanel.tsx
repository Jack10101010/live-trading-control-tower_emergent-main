/**
 * UI-1 — the truthful connection panel.
 *
 * Shows four INDEPENDENT dimensions rather than one health light, because the
 * facts are independent: the Control Tower being reachable says nothing about the
 * execution node, and the node publishing says nothing about MT5. Every value is
 * backend-derived; this component performs no inference of its own beyond
 * deciding whether it could reach the backend at all.
 *
 * Read-only. No control affordance of any kind.
 */
import { useQuery } from '@tanstack/react-query';
import { api, type ConnectionInstance, type ConnectionState } from '@/lib/api';
import {
  deriveBackendState,
  deriveDimensions,
  formatAge,
  formatPublishedAt,
  orderInstances,
  PROBLEM_DETAIL,
  PROBLEM_LABEL,
  type DimensionView,
  type Tone,
} from '@/lib/connectionState';

const TONE_COLOR: Record<Tone, string> = {
  positive: 'var(--positive)',
  caution: 'var(--caution, var(--warning))',
  negative: 'var(--negative)',
  neutral: 'var(--text-muted)',
};

export function useConnectionState() {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['live-connection'],
    queryFn: () => api.liveConnection(),
    // Telemetry ages continuously, so the panel re-reads rather than letting a
    // "fresh" badge quietly rot on screen.
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });
  return {
    connection: data,
    backend: deriveBackendState(isLoading, isError),
  };
}

function Dot({ tone }: { tone: Tone }) {
  return (
    <span
      aria-hidden
      className="inline-block rounded-full"
      style={{ width: 8, height: 8, background: TONE_COLOR[tone] }}
    />
  );
}

function DimensionRow({ dim }: { dim: DimensionView }) {
  return (
    <div className="flex items-start gap-2 py-1.5" data-testid={`dim-${dim.key}`}>
      <span className="mt-1.5">
        <Dot tone={dim.tone} />
      </span>
      <div className="min-w-0">
        <div className="flex items-baseline gap-2">
          <span className="text-xs text-text-2">{dim.label}</span>
          <span className="text-xs mono" style={{ color: TONE_COLOR[dim.tone] }}>
            {dim.state}
          </span>
        </div>
        <div className="text-2xs text-text-muted">{dim.detail}</div>
      </div>
    </div>
  );
}

function InstanceRow({ instance }: { instance: ConnectionInstance }) {
  return (
    <div
      className="border-t border-[color:var(--border)] pt-2 mt-2 text-2xs"
      data-testid={`instance-${instance.instanceId}`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="mono text-text-2">{instance.instanceId}</span>
        <span className="mono text-text-muted">{instance.telemetry}</span>
      </div>
      <dl className="grid grid-cols-2 gap-x-3 gap-y-0.5 mt-1 text-text-muted">
        <dt>Last update</dt>
        <dd className="mono">{formatPublishedAt(instance.publishedAt)}</dd>
        <dt>Telemetry age</dt>
        <dd className="mono">{formatAge(instance.ageSeconds)}</dd>
        <dt>Stale after</dt>
        <dd className="mono">{instance.staleAfterSeconds}s</dd>
        <dt>Schema version</dt>
        <dd className="mono">{instance.schemaVersion ?? 'unknown'}</dd>
        <dt>Execution node</dt>
        <dd className="mono">{instance.node}</dd>
        <dt>MT5 bridge</dt>
        <dd className="mono">{instance.bridge}</dd>
        <dt>Mode</dt>
        <dd className="mono">{instance.mode ?? 'unknown'}</dd>
        <dt>Engine</dt>
        <dd className="mono">{instance.engineVersion ?? 'unknown'}</dd>
      </dl>
      {instance.legacySource && (
        <div className="mt-1 text-text-muted">
          Legacy payload — this node predates the versioned telemetry contract, so
          identity, health, arming and market state are not reported.
        </div>
      )}
      {instance.problem && (
        <div className="mt-1" style={{ color: TONE_COLOR.negative }}>
          <div className="font-medium">{PROBLEM_LABEL[instance.problem]}</div>
          <div className="text-text-muted">{PROBLEM_DETAIL[instance.problem]}</div>
        </div>
      )}
    </div>
  );
}

function FirstRun({ connection }: { connection: ConnectionState }) {
  return (
    <div className="mt-2 pt-2 border-t border-[color:var(--border)] text-2xs text-text-muted">
      <div className="text-text-2 mb-0.5">No telemetry received yet</div>
      <div>{connection.emptyState}</div>
      <div className="mt-1">
        This is a first-run state, not a fault. Nothing here reports the execution
        node as healthy, connected or online, because nothing has been observed.
      </div>
    </div>
  );
}

export function ConnectionPanel() {
  const { connection, backend } = useConnectionState();
  const dimensions = deriveDimensions(backend, connection);

  return (
    <section
      className="rounded-md border border-[color:var(--border)] bg-[color:var(--panel)] p-3"
      data-testid="connection-panel"
      aria-label="Connection state"
    >
      <header className="flex items-baseline justify-between gap-2 mb-1">
        <h3 className="text-xs uppercase tracking-wider text-text-muted">Connection</h3>
        {connection && (
          <span className="text-2xs text-text-muted mono">
            observed {connection.observedAt}
          </span>
        )}
      </header>

      {dimensions.map((dim) => (
        <DimensionRow key={dim.key} dim={dim} />
      ))}

      {connection && connection.firstRun && <FirstRun connection={connection} />}

      {connection && !connection.firstRun && (
        <div className="mt-1">
          <div className="text-2xs text-text-muted">
            {connection.instanceCount === 1
              ? '1 instance'
              : `${connection.instanceCount} instances`}
          </div>
          {orderInstances(connection.instances).map((instance) => (
            <InstanceRow key={instance.instanceId} instance={instance} />
          ))}
        </div>
      )}

      {backend === 'unavailable' && (
        <div className="mt-2 pt-2 border-t border-[color:var(--border)] text-2xs"
             style={{ color: TONE_COLOR.negative }}>
          The Control Tower backend is unreachable, so the execution node cannot be
          observed from here. This does not mean the node has stopped — it keeps
          trading and protecting the account without the Control Tower.
        </div>
      )}
    </section>
  );
}
