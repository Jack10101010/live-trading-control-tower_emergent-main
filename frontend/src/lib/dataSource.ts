/**
 * Global data-source truth (UI-0).
 *
 * Pure derivation of the persistent badge the shell shows so an operator never has
 * to infer where the screen's data came from. Kept out of the component so it can
 * be unit-tested without mounting the whole shell.
 *
 * Hard rule: `live-node` is claimed ONLY when a real execution node has published
 * telemetry to the backend. A mock broker never implies MT5 connectivity, and a
 * responding backend never implies trading readiness.
 */
import type { BackendHealth, TelemetryState } from '@/lib/api';
import { PROVENANCE_HINT, type DataProvenance } from '@/types/provenance';

export interface DataSourceBadge {
  provenance: DataProvenance;
  /** Short lower-case text rendered in the safety bar. */
  label: string;
  /** Tooltip; safe for operators (no stack traces, no secrets). */
  hint: string;
}

export function deriveDataSourceBadge(
  health: BackendHealth | undefined,
  failed: boolean,
  /** UI-1: the backend's telemetry classification, when known. Optional so the
   *  UI-0 contract keeps working for callers that do not have it. */
  telemetry?: TelemetryState
): DataSourceBadge {
  if (failed) {
    return {
      provenance: 'unavailable',
      label: 'backend offline',
      hint: 'Backend health check failed — no data source confirmed.',
    };
  }
  if (!health) {
    return {
      provenance: 'placeholder',
      label: 'source unknown',
      hint: 'Backend health not yet known.',
    };
  }
  // UI-1: `liveNodeConnected` means "a node has published to this process at some
  // point" — it is not freshness-aware, so on its own it would badge a node that
  // died days ago as `live-node`. When the caller supplies the connection state we
  // downgrade a stale/unreadable node to `stale`, which is what an operator needs
  // to see. Without it we fall back to UI-0 behaviour rather than guessing.
  if (health.liveNodeConnected && telemetry && telemetry !== 'fresh') {
    return {
      provenance: 'stale',
      label: telemetry === 'unavailable' ? 'node telemetry unreadable' : 'live node · stale',
      hint:
        `${PROVENANCE_HINT.stale} Node telemetry is ${telemetry}; nothing from it is current. ` +
        `Backend mode ${health.backendMode}; broker ${health.brokerKind}.`,
    };
  }
  const provenance: DataProvenance = health.liveNodeConnected ? 'live-node' : 'fixture';
  const label = provenance === 'live-node' ? 'live node' : `fixture · ${health.brokerKind} broker`;
  const hint =
    `${PROVENANCE_HINT[provenance]} Backend mode ${health.backendMode}; broker ${health.brokerKind}; ` +
    `node telemetry ${health.dataSources.nodeTelemetry}; trading ready: ${health.tradingReady ? 'yes' : 'no'}.`;
  return { provenance, label, hint };
}
