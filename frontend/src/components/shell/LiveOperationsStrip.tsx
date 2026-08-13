/**
 * UI-3 — read-only live-operations strip.
 *
 * One compact, persistent row of safety-critical node state. Designed as a
 * caution panel, not a dashboard: dense, text-first, quiet when nothing is wrong.
 *
 * DISPLAY ONLY. There is no button, no toggle, no mutation callback and no
 * command anywhere in this file — by design, and asserted by tests.
 *
 * Accessibility rules honoured here:
 *   - every chip renders LABEL + VALUE as text; colour is never the only signal
 *   - severity is stated in words ("CRITICAL"), not implied by hue alone
 *   - no animation, no pulsing: a caution panel that blinks trains people to
 *     ignore it, and motion is unusable as a safety cue for many operators
 *   - the details disclosure is a native <details>/<summary>, so it is keyboard
 *     operable and screen-reader announced without custom ARIA
 *   - timestamps appear in BOTH absolute and relative form
 *   - tooltips only ever repeat information already present as text
 */
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { api, type RemoteNodeState } from '@/lib/api';
import { isAuthError } from '@/lib/authSession';
import { deriveBackendState } from '@/lib/connectionState';
import {
  deriveLiveOperations,
  formatAbsolute,
  formatRelative,
  orderInstanceOps,
  SEVERITY_LABEL,
  type InstanceOps,
  type OpsChip,
  type ReasonCode,
  type Severity,
} from '@/lib/liveOperations';

const SEVERITY_COLOR: Record<Severity, string> = {
  critical: 'var(--negative)',
  warning: 'var(--caution, var(--warning))',
  unknown: 'var(--text-muted)',
  healthy: 'var(--positive)',
};

/** A non-colour glyph so severity survives greyscale and colour blindness. */
const SEVERITY_GLYPH: Record<Severity, string> = {
  critical: '■',
  warning: '▲',
  unknown: '·',
  healthy: '●',
};

export function useLiveOperations() {
  const connectionQuery = useQuery({
    queryKey: ['live-connection'],
    queryFn: () => api.liveConnection(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });
  const statusQuery = useQuery({
    queryKey: ['live-status'],
    queryFn: () => api.liveStatus(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
    // Never block the strip on the heavier payload: connection truth alone still
    // renders a correct (if less detailed) picture.
    enabled: !connectionQuery.isError,
  });
  const backend = deriveBackendState(connectionQuery.isLoading, connectionQuery.isError,
    isAuthError(connectionQuery.error));
  return deriveLiveOperations(backend, connectionQuery.data, statusQuery.data);
}

/** UI-14 — read-only remote node integration. Separate from the pushed
 *  connection/status so a slow or disabled remote pull never blocks the strip.
 *  `connecting` is the transient loading state; every terminal state comes from
 *  the backend, which is the sole authority for remote truth. */
function useRemoteNode(): { state: RemoteNodeState; provenance: string;
                            publishedAt: string | null; ageSeconds: number | null;
                            reason: string | null } {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['live-remote'],
    queryFn: () => api.liveRemote(),
    refetchInterval: 10_000,
    staleTime: 5_000,
    retry: false,
  });
  if (isError) {
    // The backend itself is unreachable — that is a CONNECTION fact (shown by the
    // Backend chip), so the remote pull is simply unknown, never healthy.
    return { state: 'unreachable', provenance: 'remote-node', publishedAt: null,
             ageSeconds: null, reason: 'backend_unreachable' };
  }
  if (isLoading || !data) {
    return { state: 'connecting', provenance: 'remote-node', publishedAt: null,
             ageSeconds: null, reason: null };
  }
  return {
    state: data.state,
    provenance: data.provenance,
    publishedAt: data.telemetry?.publishedAt ?? null,
    ageSeconds: data.telemetry?.ageSeconds ?? null,
    reason: data.reason,
  };
}

const REMOTE_SEVERITY: Record<RemoteNodeState, Severity> = {
  disabled: 'unknown',
  connecting: 'unknown',
  healthy: 'healthy',
  degraded: 'warning',
  stale: 'warning',
  unauthorized: 'critical',
  unreachable: 'critical',
};

function Chip({ chip }: { chip: OpsChip }) {
  return (
    <span
      className="inline-flex items-baseline gap-1 px-1.5 py-0.5 rounded-sm border text-2xs whitespace-nowrap"
      style={{
        borderColor: `${SEVERITY_COLOR[chip.severity]}55`,
        color: SEVERITY_COLOR[chip.severity],
      }}
      title={chip.detail}
      data-testid={`ops-chip-${chip.key}`}
      data-severity={chip.severity}
    >
      <span aria-hidden>{SEVERITY_GLYPH[chip.severity]}</span>
      <span className="text-text-muted">{chip.label}</span>
      <span className="mono font-medium">{chip.value}</span>
      <span className="sr-only">
        {`${chip.label}: ${chip.value}. ${chip.severity}. ${chip.detail}`}
      </span>
    </span>
  );
}

function ReasonList({ reasons }: { reasons: ReasonCode[] }) {
  if (!reasons.length) return null;
  return (
    <ul className="mt-1 space-y-0.5" data-testid="ops-reasons">
      {reasons.map((reason) => (
        <li key={reason.code} className="text-2xs flex items-baseline gap-2">
          <span aria-hidden style={{ color: SEVERITY_COLOR[reason.severity] }}>
            {SEVERITY_GLYPH[reason.severity]}
          </span>
          {/* The machine code is always preserved beside the readable text. */}
          <code className="mono text-text-muted shrink-0">{reason.code}</code>
          <span className="text-text-2">{reason.text}</span>
          {reason.unmapped && (
            <span className="text-text-muted">(unrecognised code shown verbatim)</span>
          )}
        </li>
      ))}
    </ul>
  );
}

function InstanceDetails({ instance }: { instance: InstanceOps }) {
  return (
    <div className="mt-1" data-testid={`ops-instance-${instance.instanceId}`}>
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-2xs text-text-muted">
        <span className="mono text-text-2">{instance.identity.instanceId}</span>
        <span>
          symbol <span className="mono">{instance.identity.symbol ?? 'unknown'}</span>
        </span>
        <span>
          timeframe <span className="mono">{instance.identity.timeframe ?? 'unknown'}</span>
        </span>
        <span>
          schema <span className="mono">{instance.identity.schemaVersion ?? 'unknown'}</span>
        </span>
        <span>
          last update{' '}
          <time className="mono" dateTime={instance.freshness.publishedAt ?? undefined}>
            {formatAbsolute(instance.freshness.publishedAt)}
          </time>{' '}
          <span className="mono">({formatRelative(instance.freshness.ageSeconds)})</span>
        </span>
        <span>
          pending <span className="mono">{instance.counts.pendingIntents ?? 'unknown'}</span>
        </span>
        <span>
          recent blocks <span className="mono">{instance.counts.recentBlocks ?? 'unknown'}</span>
        </span>
        <span>
          unresolved <span className="mono">{instance.counts.unresolvedRecords ?? 'unknown'}</span>
        </span>
      </div>
      {instance.identity.legacySource && (
        <div className="mt-1 text-2xs text-text-muted">
          Legacy payload — this node predates the versioned telemetry contract, so
          arming, account and bridge state are not reported at all.
        </div>
      )}
      <ReasonList reasons={instance.reasons} />
    </div>
  );
}

export function LiveOperationsStrip() {
  const ops = useLiveOperations();
  const remote = useRemoteNode();
  const [selected, setSelected] = useState<string | null>(null);
  const ordered = orderInstanceOps(ops.instances);
  const multiple = ordered.length > 1;
  // Selection is a local READ filter only — it never changes what fleet severity
  // reports, so a critical instance cannot be hidden by selecting a healthy one.
  const shown = selected ? ordered.filter((i) => i.instanceId === selected) : ordered;
  const primary = shown[0] ?? null;
  const staleShown = ordered.some((i) => i.stale);

  return (
    <section
      className="border-b px-3 py-1 text-2xs"
      style={{ borderColor: 'var(--border-subtle)', background: 'var(--panel)' }}
      aria-label="Live operations"
      data-testid="live-operations-strip"
      data-severity={ops.severity}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        {/* Overall severity — stated as a word, never colour alone. */}
        <span
          className="inline-flex items-baseline gap-1 px-1.5 py-0.5 rounded-sm border font-semibold tracking-wider"
          style={{
            borderColor: SEVERITY_COLOR[ops.severity],
            color: SEVERITY_COLOR[ops.severity],
          }}
          data-testid="ops-severity"
        >
          <span aria-hidden>{SEVERITY_GLYPH[ops.severity]}</span>
          {SEVERITY_LABEL[ops.severity]}
        </span>

        {/* Connection dimensions stay named separately from operational state. */}
        <Chip chip={{ key: 'backend', label: 'Backend', value: ops.backend,
                      severity: ops.backend === 'available' ? 'healthy'
                        : ops.backend === 'unavailable' ? 'critical' : 'unknown',
                      detail: 'Whether this browser can reach the Control Tower backend.' }} />
        <Chip chip={{ key: 'telemetry', label: 'Telemetry', value: ops.telemetry,
                      severity: ops.telemetry === 'fresh' ? 'healthy'
                        : ops.telemetry === 'never_received' ? 'unknown' : 'warning',
                      detail: 'Freshness of the latest validated node snapshot.' }} />
        <Chip chip={{ key: 'node', label: 'Node', value: ops.node,
                      severity: ops.node === 'connected' ? 'healthy'
                        : ops.node === 'disconnected' ? 'critical' : 'unknown',
                      detail: 'Whether the execution node is observable from here.' }} />
        <Chip chip={{ key: 'bridge', label: 'Bridge', value: ops.bridge,
                      severity: ops.bridge === 'healthy' ? 'healthy'
                        : ops.bridge === 'unavailable' ? 'critical'
                        : ops.bridge === 'degraded' ? 'warning' : 'unknown',
                      detail: 'MT5 bridge state as the node itself reported it.' }} />

        {/* UI-14 — read-only remote node pull. Distinct from Node/Bridge (which
            derive from PUSHED telemetry): this is what the tower reads FROM the
            node. Disabled by default; provenance is always remote-node. */}
        <Chip chip={{ key: 'remote', label: 'Remote node', value: remote.state,
                      severity: REMOTE_SEVERITY[remote.state],
                      detail: remote.state === 'disabled'
                        ? 'Remote node integration is disabled (no transport configured).'
                        : `Remote read (provenance: ${remote.provenance})`
                          + (remote.publishedAt ? ` · last update ${remote.publishedAt}` : '')
                          + (remote.reason ? ` · ${remote.reason}` : '') }} />
        {primary?.chips.map((chip) => <Chip key={chip.key} chip={chip} />)}
      </div>

      <div className="mt-0.5 text-text-muted" data-testid="ops-headline">
        {ops.headline}
      </div>

      {/* One explicit stale banner beats subtle per-chip colour shifts. */}
      {staleShown && (
        <div
          className="mt-1 px-1.5 py-0.5 rounded-sm border"
          style={{ borderColor: SEVERITY_COLOR.warning, color: SEVERITY_COLOR.warning }}
          data-testid="ops-stale-banner"
          role="status"
        >
          ▲ Telemetry is stale. Every operational value below is the last observed
          reading, not the current state of the node.
        </div>
      )}

      {ops.firstRun && (
        <div className="mt-1 text-text-muted" data-testid="ops-first-run">
          First run — no node has published telemetry yet. Nothing here reports the
          system as healthy, armed, live or ready, because nothing has been observed.
        </div>
      )}

      {multiple && (
        // A native <select> on purpose: this is a VIEW FILTER, not an operator
        // action. Using a button here would blur the read-only boundary this
        // slice is required to hold (and the tests assert no action buttons).
        <div className="mt-1 flex flex-wrap items-center gap-1" data-testid="ops-instance-selector">
          <label htmlFor="ops-instance-filter" className="text-text-muted">
            Show instance:
          </label>
          <select
            id="ops-instance-filter"
            className="px-1 py-0.5 rounded-sm border mono bg-transparent"
            style={{ borderColor: 'var(--border-subtle)' }}
            value={selected ?? ''}
            onChange={(e) => setSelected(e.target.value === '' ? null : e.target.value)}
          >
            <option value="">all ({ordered.length})</option>
            {ordered.map((instance) => (
              <option key={instance.instanceId} value={instance.instanceId}>
                {`${instance.severity.toUpperCase()} — ${instance.instanceId}`}
              </option>
            ))}
          </select>
          <span className="text-text-muted">
            Filtering the view only; the severity above always reflects the worst
            instance.
          </span>
        </div>
      )}

      {shown.length > 0 && (
        <details className="mt-1" data-testid="ops-details">
          <summary className="cursor-pointer text-text-muted select-none">
            Details, timestamps and reason codes
          </summary>
          {shown.map((instance) => (
            <InstanceDetails key={instance.instanceId} instance={instance} />
          ))}
        </details>
      )}
    </section>
  );
}
