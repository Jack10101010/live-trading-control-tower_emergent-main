/**
 * UI-1 — presentation of truthful connection state.
 *
 * Pure derivation, unit-tested without mounting the shell. Two rules govern
 * everything here:
 *
 *   1. The BACKEND dimension is the only one this file decides. A backend cannot
 *      report its own unreachability, so the client judges it from whether the
 *      request succeeded. Every other dimension is passed through from the
 *      backend verbatim — the frontend never re-derives node, bridge or telemetry
 *      state from raw fields.
 *
 *   2. Absence is never upgraded to health. `unknown` (no evidence) and
 *      `unavailable` (positive evidence of a failed read) stay visually and
 *      semantically distinct, and neither is ever rendered in the positive tone.
 */
import type {
  BridgeState,
  ConnectionInstance,
  ConnectionState,
  NodeState,
  SnapshotProblem,
  TelemetryState,
} from '@/lib/api';

export type BackendState = 'starting' | 'available' | 'unavailable';

/** Severity drives colour. `positive` is reserved for observed good news only. */
export type Tone = 'positive' | 'caution' | 'negative' | 'neutral';

export interface DimensionView {
  key: 'backend' | 'telemetry' | 'node' | 'bridge';
  label: string;
  /** The raw state string, shown verbatim so the UI cannot soften it. */
  state: string;
  tone: Tone;
  /** One sentence an operator can act on. Never a stack trace. */
  detail: string;
}

export function deriveBackendState(isLoading: boolean, failed: boolean): BackendState {
  if (failed) return 'unavailable';
  return isLoading ? 'starting' : 'available';
}

const BACKEND_TONE: Record<BackendState, Tone> = {
  starting: 'neutral',
  available: 'positive',
  unavailable: 'negative',
};

const BACKEND_DETAIL: Record<BackendState, string> = {
  starting: 'Contacting the Control Tower backend…',
  // Deliberately narrow: this says the API answered, nothing more.
  available: 'The Control Tower backend is reachable. This says nothing about the execution node.',
  unavailable: 'The Control Tower backend could not be reached. Nothing on this screen is current.',
};

const TELEMETRY_TONE: Record<TelemetryState, Tone> = {
  never_received: 'neutral',
  fresh: 'positive',
  stale: 'caution',
  unavailable: 'negative',
};

const TELEMETRY_DETAIL: Record<TelemetryState, string> = {
  never_received: 'No execution node has ever published telemetry to this Control Tower.',
  fresh: 'The latest snapshot is within its freshness threshold.',
  stale: 'The latest snapshot is older than the freshness threshold. Nothing shown from it is current.',
  unavailable: 'A stored snapshot exists but could not be read or validated.',
};

const NODE_TONE: Record<NodeState, Tone> = {
  unknown: 'caution',
  connected: 'positive',
  disconnected: 'negative',
};

const BRIDGE_TONE: Record<BridgeState, Tone> = {
  unknown: 'caution',
  healthy: 'positive',
  degraded: 'caution',
  unavailable: 'negative',
};

export const PROBLEM_LABEL: Record<SnapshotProblem, string> = {
  malformed_snapshot: 'Malformed snapshot',
  unsupported_schema: 'Unsupported schema version',
  unknown_schema: 'Unknown schema (no version declared)',
};

export const PROBLEM_DETAIL: Record<SnapshotProblem, string> = {
  malformed_snapshot:
    'The stored snapshot failed validation. It is not being displayed as state.',
  unsupported_schema:
    'The node published a schema version this Control Tower does not understand. Upgrade the tower, or the node published a newer contract.',
  unknown_schema:
    'The stored payload declares no schema version, so it cannot be interpreted safely.',
};

/**
 * The four dimensions, in the order an operator should read them: nearest first.
 * When the backend is unreachable every downstream dimension collapses to
 * "unknown" — we cannot see the node through a tower we cannot reach.
 */
export function deriveDimensions(
  backend: BackendState,
  connection: ConnectionState | undefined
): DimensionView[] {
  const backendView: DimensionView = {
    key: 'backend',
    label: 'Control Tower backend',
    state: backend,
    tone: BACKEND_TONE[backend],
    detail: BACKEND_DETAIL[backend],
  };

  if (backend !== 'available' || !connection) {
    const detail =
      backend === 'unavailable'
        ? 'Not observable: the Control Tower backend could not be reached.'
        : 'Not yet observed.';
    return [
      backendView,
      { key: 'telemetry', label: 'Node telemetry', state: 'unknown', tone: 'caution', detail },
      { key: 'node', label: 'Execution node', state: 'unknown', tone: 'caution', detail },
      { key: 'bridge', label: 'MT5 bridge', state: 'unknown', tone: 'caution', detail },
    ];
  }

  // Every dimension below is the backend's own classification, passed through.
  const worstNodeEvidence = connection.instances.find((i) => i.node === connection.node);
  const worstBridgeEvidence = connection.instances.find((i) => i.bridge === connection.bridge);
  return [
    backendView,
    {
      key: 'telemetry',
      label: 'Node telemetry',
      state: connection.telemetry,
      tone: TELEMETRY_TONE[connection.telemetry],
      detail: TELEMETRY_DETAIL[connection.telemetry],
    },
    {
      key: 'node',
      label: 'Execution node',
      state: connection.node,
      tone: NODE_TONE[connection.node],
      detail:
        worstNodeEvidence?.nodeEvidence ??
        'No evidence about the execution node.',
    },
    {
      key: 'bridge',
      label: 'MT5 bridge',
      state: connection.bridge,
      tone: BRIDGE_TONE[connection.bridge],
      detail:
        worstBridgeEvidence?.bridgeEvidence ??
        'No evidence about the MT5 bridge. The Control Tower never contacts MT5 itself.',
    },
  ];
}

/** Human age, or an explicit unknown. Never estimated, never "just now". */
export function formatAge(ageSeconds: number | null): string {
  if (ageSeconds === null) return 'unknown';
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)}s ago`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m ago`;
  if (ageSeconds < 86_400) return `${Math.floor(ageSeconds / 3600)}h ago`;
  return `${Math.floor(ageSeconds / 86_400)}d ago`;
}

/** A timestamp exactly as the node published it, or an explicit unknown. */
export function formatPublishedAt(publishedAt: string | null): string {
  return publishedAt ?? 'unknown';
}

/**
 * Compact summary for the persistent chrome. Deliberately NOT a single word:
 * the shell must not imply one overall health from four separate facts.
 */
export function summarize(
  backend: BackendState,
  connection: ConnectionState | undefined
): { label: string; tone: Tone } {
  if (backend === 'unavailable') return { label: 'backend unavailable', tone: 'negative' };
  if (backend === 'starting') return { label: 'backend starting', tone: 'neutral' };
  if (!connection) return { label: 'node unknown', tone: 'caution' };
  if (connection.firstRun) return { label: 'no node telemetry', tone: 'neutral' };
  if (connection.telemetry === 'unavailable') return { label: 'telemetry unreadable', tone: 'negative' };
  if (connection.telemetry === 'stale') return { label: 'node telemetry stale', tone: 'caution' };
  if (connection.node === 'disconnected') return { label: 'node disconnected', tone: 'negative' };
  if (connection.node !== 'connected') return { label: 'node unknown', tone: 'caution' };
  // Node confirmed; the bridge is a separate fact and is named separately.
  if (connection.bridge === 'healthy') return { label: 'node live · bridge healthy', tone: 'positive' };
  if (connection.bridge === 'unavailable') return { label: 'node live · bridge unavailable', tone: 'negative' };
  if (connection.bridge === 'degraded') return { label: 'node live · bridge degraded', tone: 'caution' };
  return { label: 'node live · bridge unknown', tone: 'caution' };
}

/** Sorted worst-first so a silent instance surfaces above a healthy one. */
export function orderInstances(instances: ConnectionInstance[]): ConnectionInstance[] {
  const rank: Record<TelemetryState, number> = {
    unavailable: 0,
    never_received: 1,
    stale: 2,
    fresh: 3,
  };
  return [...instances].sort(
    (a, b) => rank[a.telemetry] - rank[b.telemetry] || a.instanceId.localeCompare(b.instanceId)
  );
}
