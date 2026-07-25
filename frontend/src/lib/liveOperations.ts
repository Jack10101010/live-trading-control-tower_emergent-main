/**
 * UI-3 — live-operations model (pure derivation).
 *
 * Turns UI-1 connection truth and UI-2 telemetry into the compact safety picture
 * the operations strip renders. It is a PROJECTION, never a decision: every value
 * traces to something the node published, to the absence of one, or to the
 * client's own inability to reach the backend. The frontend never recomputes an
 * execution decision (invariant I-7 / architecture rule 4).
 *
 * Three rules govern everything below.
 *
 *   1. CRITICAL WINS. Severity is the worst contributing signal, never an average
 *      and never the most recent. A healthy field can never mask a critical one.
 *
 *   2. STALE ERASES CURRENCY. Past the freshness threshold the node's last
 *      readings stop being statements about now. Arming, bridge, account,
 *      reconciliation and runtime all fall back to unknown, and the observed
 *      values survive only as explicitly labelled historical evidence.
 *
 *   3. ABSENCE IS NOT HEALTH. `unknown` (no evidence) and `unhealthy` (evidence of
 *      a problem) stay distinct, and neither is ever rendered as healthy.
 *      `healthy` requires affirmative, fresh evidence on every safety dimension.
 */
import type {
  BridgeState,
  ConnectionInstance,
  ConnectionState,
  LiveStatus,
  LiveStatusEntry,
  NodeState,
  TelemetryState,
} from '@/lib/api';
import type { BackendState } from '@/lib/connectionState';

export type Severity = 'critical' | 'warning' | 'unknown' | 'healthy';

/** Worst-first. Index 0 is the most alarming state that can be displayed. */
const SEVERITY_ORDER: Severity[] = ['critical', 'warning', 'unknown', 'healthy'];

export function worstSeverity(values: Severity[]): Severity {
  if (!values.length) return 'unknown';
  return values.reduce((worst, s) =>
    SEVERITY_ORDER.indexOf(s) < SEVERITY_ORDER.indexOf(worst) ? s : worst
  );
}

/** A single readable cell of the strip. Text is mandatory — never colour alone. */
export interface OpsChip {
  key: string;
  /** Short label, always rendered. */
  label: string;
  /** The state word, always rendered next to the label. */
  value: string;
  severity: Severity;
  /** One sentence of context. Supplemental, never the sole carrier of meaning. */
  detail: string;
  /** True when the value is historical rather than current (stale telemetry). */
  historical?: boolean;
  /**
   * Whether this chip contributes to the aggregate severity. Default true.
   *
   * Two chips carry no safety VERDICT and are excluded, or `healthy` could never
   * be reached at all:
   *   - `mode` is context. Neither dry_run nor live is good or bad by itself.
   *   - `open` when "not evaluated" is the permanent, correct state — UI-1
   *     established that telemetry can never authorize an OPEN, so treating it as
   *     an unknown verdict would pin every instance at `unknown` forever.
   * Both still render their own `unknown` styling; they simply do not drag the
   * headline down. An OPEN that is explicitly BLOCKED does contribute.
   */
  contributes?: boolean;
}

export interface ReasonCode {
  /** The original machine code, always preserved. */
  code: string;
  /** Concise readable text, or the raw code when unrecognised. */
  text: string;
  severity: Severity;
  /** True when no mapping existed and `text` falls back to the raw code. */
  unmapped: boolean;
}

export interface InstanceOps {
  instanceId: string;
  severity: Severity;
  /** True when telemetry is stale: nothing below describes the present. */
  stale: boolean;
  /** True when there is no usable snapshot at all. */
  unavailable: boolean;
  chips: OpsChip[];
  reasons: ReasonCode[];
  identity: {
    instanceId: string;
    symbol: string | null;
    timeframe: string | null;
    schemaVersion: string | null;
    legacySource: boolean;
  };
  freshness: {
    publishedAt: string | null;
    ageSeconds: number | null;
    staleAfterSeconds: number | null;
    state: TelemetryState;
  };
  counts: {
    pendingIntents: number | null;
    recentBlocks: number | null;
    unresolvedRecords: number | null;
  };
}

export interface LiveOperations {
  /** Fleet severity: the WORST instance, never the selected one. */
  severity: Severity;
  backend: BackendState;
  telemetry: TelemetryState;
  node: NodeState;
  bridge: BridgeState;
  firstRun: boolean;
  /** Present when nothing can be evaluated at all. */
  headline: string;
  instances: InstanceOps[];
}

/* ── reason codes ───────────────────────────────────────────────────────────
 * The vocabulary is the node's own (live/telemetry.py) plus the arming and
 * account-health reason strings it emits. An unrecognised code is rendered
 * verbatim at warning severity — never dropped, and never given a more
 * optimistic explanation than the code itself supports. */
const REASON_TEXT: Record<string, { text: string; severity: Severity }> = {
  kill_switch_active: { text: 'Kill switch is engaged — new entries blocked', severity: 'critical' },
  reconciliation_frozen: { text: 'Reconciliation is frozen against broker state', severity: 'critical' },
  unresolved_sent: { text: 'Submitted orders remain unresolved', severity: 'critical' },
  account_health_blocked: { text: 'Account health blocks trading', severity: 'critical' },
  runtime_identity_mismatch: { text: 'Broker account does not match the armed account', severity: 'critical' },
  submission_disabled: { text: 'Order submission is disabled on the node', severity: 'warning' },
  not_armed: { text: 'Node is not armed — live OPEN blocked', severity: 'warning' },
  arm_expired: { text: 'Arm session has expired', severity: 'warning' },
  arm_disarmed: { text: 'Arm session was disarmed', severity: 'warning' },
  arm_session_expired: { text: 'Arm session has expired', severity: 'warning' },
  arm_session_disarmed: { text: 'Arm session was disarmed', severity: 'warning' },
  probation_exhausted: { text: 'Probation allowance is exhausted', severity: 'warning' },
  probation_open_limit_reached: { text: 'Probation allowance is exhausted', severity: 'warning' },
  account_health_unavailable: { text: 'Account health was not observed', severity: 'unknown' },
  runtime_identity_not_evaluated: { text: 'Account identity not checked this cycle', severity: 'unknown' },
  runtime_identity_unavailable: { text: 'Broker account identity unavailable', severity: 'warning' },
  arm_context_missing: { text: 'No arm session installed', severity: 'warning' },
  mode_not_live: { text: 'Node is not in live mode', severity: 'unknown' },
  authorization_not_evaluated_by_node_rails: {
    text: 'OPEN authorization not evaluated — the node decides at order time',
    severity: 'unknown',
  },
};

export function mapReasonCodes(codes: Array<string | null | undefined>): ReasonCode[] {
  const seen = new Set<string>();
  const out: ReasonCode[] = [];
  // Stable source order is preserved within each severity band, so a code's
  // position still reflects where the node listed it.
  codes.forEach((raw) => {
    if (typeof raw !== 'string' || !raw.trim()) return;
    const code = raw.trim();
    if (seen.has(code)) return;                       // deduplicate, never drop
    seen.add(code);
    const known = REASON_TEXT[code];
    out.push({
      code,
      text: known?.text ?? code,
      severity: known?.severity ?? 'warning',
      unmapped: !known,
    });
  });
  return out.sort(
    (a, b) => SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity)
  );
}

/* ── chip helpers ───────────────────────────────────────────────────────────*/

const UNKNOWN_CHIP = (key: string, label: string, detail: string): OpsChip => ({
  key,
  label,
  value: 'unknown',
  severity: 'unknown',
  detail,
});

const STALE_DETAIL = 'Telemetry is stale — this is the last observed value, not the current one.';

function bool3(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function count(list: unknown): number | null {
  return Array.isArray(list) ? list.length : null;
}

/* ── per-instance derivation ────────────────────────────────────────────────*/

function instanceIdentity(entry: LiveStatusEntry) {
  const engine = entry.snapshot?.engine ?? null;
  return {
    instanceId: entry.instance_id,
    symbol: engine?.symbol ?? null,
    timeframe: engine?.timeframe ?? null,
    schemaVersion: entry.schema_version ?? null,
    legacySource: Boolean(entry.legacy_source),
  };
}

/**
 * Build the chips for one instance.
 *
 * `stale` is passed in rather than re-derived so freshness has exactly one
 * definition (the backend's). When stale, every operational chip degrades to
 * unknown and carries the observed value as clearly-labelled history.
 */
function buildChips(entry: LiveStatusEntry, stale: boolean): OpsChip[] {
  const snap = entry.snapshot ?? ({} as LiveStatusEntry['snapshot']);
  const runtime = snap.runtime ?? null;
  const recon = snap.reconciliation ?? null;
  const health = snap.account?.health ?? null;
  const arming = snap.arming ?? null;
  const eligibility = runtime?.open_eligibility ?? null;
  const execution = snap.execution ?? null;
  const chips: OpsChip[] = [];

  /** Stale telemetry can never yield a current reading; the observed value is
   *  preserved as history so the operator can still see what it WAS. */
  const push = (chip: OpsChip) => {
    if (!stale || chip.severity === 'unknown') {
      chips.push(stale ? { ...chip, historical: true } : chip);
      return;
    }
    chips.push({
      ...chip,
      value: `${chip.value} (stale)`,
      severity: 'unknown',
      detail: `${STALE_DETAIL} Last observed: ${chip.value}.`,
      historical: true,
    });
  };

  // ── runtime mode. Neither dry_run nor live is healthy or unhealthy by itself.
  const mode = runtime?.mode ?? null;
  push(
    mode
      ? {
          key: 'mode',
          label: 'Mode',
          value: mode,
          severity: 'unknown',
          contributes: false,
          detail:
            mode === 'live'
              ? 'Node is running in live mode. Mode alone is not a safety verdict.'
              : `Node is running in ${mode} mode. Mode alone is not a safety verdict.`,
        }
      : { ...UNKNOWN_CHIP('mode', 'Mode', 'The node did not report a runtime mode.'),
          contributes: false }
  );

  // ── submission
  const submissionDisabled = bool3(runtime?.submission_disabled);
  push(
    submissionDisabled === null
      ? UNKNOWN_CHIP('submission', 'Submission', 'The node did not report submission state.')
      : {
          key: 'submission',
          label: 'Submission',
          value: submissionDisabled ? 'disabled' : 'enabled',
          severity: submissionDisabled ? 'warning' : 'healthy',
          detail: submissionDisabled
            ? 'Order submission is disabled on the node — no order will reach the broker.'
            : 'The node would submit orders if every other rail allowed it.',
        }
  );

  // ── kill switch
  const kill = bool3(runtime?.kill_switch_active);
  push(
    kill === null
      ? UNKNOWN_CHIP('kill', 'Kill switch', 'The node did not report kill-switch state.')
      : {
          key: 'kill',
          label: 'Kill switch',
          value: kill ? 'ENGAGED' : 'clear',
          severity: kill ? 'critical' : 'healthy',
          detail: kill
            ? 'The operator kill file is engaged — new entries are blocked.'
            : 'The kill file is not present.',
        }
  );

  // ── reconciliation
  if (recon?.available !== true) {
    push(UNKNOWN_CHIP('reconciliation', 'Reconcile', 'The node reported no reconciliation result.'));
  } else if (recon.frozen === true || recon.recovery_required === true) {
    push({
      key: 'reconciliation',
      label: 'Reconcile',
      value: recon.frozen === true ? 'FROZEN' : 'recovery required',
      severity: 'critical',
      detail:
        recon.frozen === true
          ? 'The node froze reconciliation against broker state — it will not act until resolved.'
          : 'The node requires recovery before it can act.',
    });
  } else if (recon.clean === true) {
    push({
      key: 'reconciliation',
      label: 'Reconcile',
      value: 'clean',
      severity: 'healthy',
      detail: `The node reconciled cleanly (snapshot ${recon.snapshot_status ?? 'ok'}).`,
    });
  } else {
    push(
      UNKNOWN_CHIP(
        'reconciliation',
        'Reconcile',
        `The node did not report a clean reconciliation (snapshot ${recon.snapshot_status ?? 'unknown'}).`
      )
    );
  }

  // ── unresolved SENT records (critical: an order may be live at the broker)
  const unresolved = recon?.unresolved_sent_count ?? count(execution?.unresolved_sent);
  push(
    unresolved === null || unresolved === undefined
      ? UNKNOWN_CHIP('unresolved', 'Unresolved', 'The node did not report unresolved records.')
      : {
          key: 'unresolved',
          label: 'Unresolved',
          value: String(unresolved),
          severity: unresolved > 0 ? 'critical' : 'healthy',
          detail:
            unresolved > 0
              ? `${unresolved} submitted order(s) remain unresolved — an order may exist at the broker.`
              : 'No submitted order is awaiting resolution.',
        }
  );

  // ── account health
  if (health?.available !== true) {
    push(UNKNOWN_CHIP('account', 'Account', 'The node did not sample account health this cycle.'));
  } else if (health.healthy === false) {
    push({
      key: 'account',
      label: 'Account',
      value: 'blocked',
      severity: 'critical',
      detail: 'The node evaluated account health and it blocks trading.',
    });
  } else if (health.trade_allowed === false) {
    push({
      key: 'account',
      label: 'Account',
      value: 'trade not allowed',
      severity: 'critical',
      detail: 'The broker reports trading is not allowed on this account.',
    });
  } else if (health.trade_expert === false) {
    push({
      key: 'account',
      label: 'Account',
      value: 'expert trading off',
      severity: 'critical',
      detail: 'The terminal reports automated (expert) trading is disabled.',
    });
  } else if (health.healthy === true) {
    push({
      key: 'account',
      label: 'Account',
      value: 'healthy',
      severity: 'healthy',
      detail: 'The node evaluated account health and it permits trading.',
    });
  } else {
    push(
      UNKNOWN_CHIP(
        'account',
        'Account',
        'Account values were observed but the node published no health verdict.'
      )
    );
  }

  // ── arming
  const armStatus = arming?.status ?? null;
  if (!armStatus) {
    push(UNKNOWN_CHIP('arming', 'Arming', 'The node did not report arming state.'));
  } else if (arming?.fingerprint_matches === false) {
    push({
      key: 'arming',
      label: 'Arming',
      value: 'account mismatch',
      severity: 'critical',
      detail: 'The observed broker account does not match the armed account.',
    });
  } else if (armStatus === 'armed') {
    const attempts = arming?.attempts_remaining;
    push({
      key: 'arming',
      label: 'Arming',
      value: 'armed',
      severity: 'healthy',
      detail:
        `An arm session is installed${typeof attempts === 'number' ? ` with ${attempts} attempt(s) remaining` : ''}` +
        `${arming?.expires_at ? `, expiring ${arming.expires_at}` : ''}. ` +
        'Authorization is still decided by the node at order time.',
    });
  } else {
    push({
      key: 'arming',
      label: 'Arming',
      value: armStatus,
      severity: 'warning',
      detail:
        armStatus === 'unarmed'
          ? 'No arm session is installed — every live OPEN is blocked.'
          : `The arm session is ${armStatus} — live OPEN is blocked.`,
    });
  }

  // ── OPEN eligibility. Never "allowed": telemetry cannot authorize (UI-1).
  if (!eligibility) {
    push(UNKNOWN_CHIP('open', 'OPEN', 'The node did not report OPEN eligibility.'));
  } else if (eligibility.eligible === false) {
    push({
      key: 'open',
      label: 'OPEN',
      value: 'blocked',
      severity: 'warning',
      detail: `Blocked by ${eligibility.reasons?.length ?? 0} observed condition(s).`,
    });
  } else {
    push({
      key: 'open',
      label: 'OPEN',
      value: 'not evaluated',
      severity: 'unknown',
      contributes: false,
      detail:
        'No blocker was observed, but authorization was not evaluated — the node’s rails decide at order time.',
    });
  }

  return chips;
}

function instanceOps(
  entry: LiveStatusEntry,
  connectionInstance: ConnectionInstance | undefined
): InstanceOps {
  const telemetryState: TelemetryState = connectionInstance?.telemetry ?? (entry.stale ? 'stale' : 'fresh');
  const unusable = telemetryState === 'unavailable' || telemetryState === 'never_received';
  const stale = telemetryState === 'stale';
  const execution = entry.snapshot?.execution ?? null;
  const recon = entry.snapshot?.reconciliation ?? null;

  if (unusable) {
    // No usable snapshot: report the fact, never a stale interpretation of it.
    const problem = connectionInstance?.problem ?? null;
    return {
      instanceId: entry.instance_id,
      severity: 'unknown',
      stale: false,
      unavailable: true,
      chips: [
        UNKNOWN_CHIP(
          'snapshot',
          'Snapshot',
          problem
            ? `The stored snapshot could not be used (${problem}).`
            : 'No usable snapshot is available for this instance.'
        ),
      ],
      reasons: mapReasonCodes(problem ? [problem] : []),
      identity: instanceIdentity(entry),
      freshness: {
        publishedAt: null,
        ageSeconds: null,
        staleAfterSeconds: entry.stale_after_seconds ?? null,
        state: telemetryState,
      },
      counts: { pendingIntents: null, recentBlocks: null, unresolvedRecords: null },
    };
  }

  const chips = buildChips(entry, stale);
  const eligibility = entry.snapshot?.runtime?.open_eligibility ?? null;
  const blockRails = (execution?.blocks ?? []).map((b) => b?.rail ?? null);
  const reasons = mapReasonCodes([
    ...(eligibility?.reasons ?? []),
    entry.snapshot?.arming?.reason ?? null,
    ...(entry.snapshot?.account?.health?.reasons ?? []),
    ...blockRails,
  ]);

  // A recent execution block is a warning in its own right, even when the chips
  // above are otherwise quiet.
  const recentBlocks = count(execution?.blocks);
  const chipSeverities = chips
    .filter((c) => c.contributes !== false)
    .map((c) => c.severity);
  if (recentBlocks !== null && recentBlocks > 0 && !stale) chipSeverities.push('warning');
  // Stale telemetry can never produce a healthy instance.
  if (stale) chipSeverities.push('unknown');

  return {
    instanceId: entry.instance_id,
    severity: worstSeverity(chipSeverities),
    stale,
    unavailable: false,
    chips,
    reasons,
    identity: instanceIdentity(entry),
    freshness: {
      publishedAt: entry.published_at ?? null,
      ageSeconds: entry.age_seconds ?? null,
      staleAfterSeconds: entry.stale_after_seconds ?? null,
      state: telemetryState,
    },
    counts: {
      pendingIntents: count(execution?.pending_intents),
      recentBlocks,
      unresolvedRecords: recon?.unresolved_sent_count ?? count(execution?.unresolved_sent),
    },
  };
}

/* ── connection dimensions contribute their own severity ────────────────────*/

const TELEMETRY_SEVERITY: Record<TelemetryState, Severity> = {
  never_received: 'unknown',
  fresh: 'healthy',
  stale: 'warning',
  unavailable: 'warning',
};

const NODE_SEVERITY: Record<NodeState, Severity> = {
  unknown: 'unknown',
  connected: 'healthy',
  disconnected: 'critical',
};

const BRIDGE_SEVERITY: Record<BridgeState, Severity> = {
  unknown: 'unknown',
  healthy: 'healthy',
  degraded: 'warning',
  unavailable: 'critical',
};

/**
 * Build the whole strip model.
 *
 * `bridge unavailable` and `node disconnected` are critical ONLY while telemetry
 * is fresh: without current evidence they are merely unknown, and an unreachable
 * Control Tower must never be presented as a node fault — the node stays
 * autonomous when the tower is down (invariant I-10).
 */
export function deriveLiveOperations(
  backend: BackendState,
  connection: ConnectionState | undefined,
  status: LiveStatus | undefined
): LiveOperations {
  if (backend !== 'available' || !connection) {
    return {
      severity: 'unknown',
      backend,
      telemetry: 'never_received',
      node: 'unknown',
      bridge: 'unknown',
      firstRun: false,
      headline:
        backend === 'unavailable'
          ? 'Control Tower backend unreachable — node state cannot be observed. The node keeps running without it.'
          : 'Contacting the Control Tower backend…',
      instances: [],
    };
  }

  if (connection.firstRun) {
    return {
      severity: 'unknown',
      backend,
      telemetry: 'never_received',
      node: 'unknown',
      bridge: 'unknown',
      firstRun: true,
      headline:
        connection.emptyState ??
        'No execution node has ever published telemetry to this Control Tower.',
      instances: [],
    };
  }

  const byId = new Map(connection.instances.map((i) => [i.instanceId, i]));
  const entries = connection.instances
    .map((i) => status?.statuses?.[i.instanceId])
    .filter((e): e is LiveStatusEntry => Boolean(e));
  const instances = entries.map((entry) => instanceOps(entry, byId.get(entry.instance_id)));

  const fresh = connection.telemetry === 'fresh';
  const dimensionSeverities: Severity[] = [
    TELEMETRY_SEVERITY[connection.telemetry],
    fresh ? NODE_SEVERITY[connection.node] : 'unknown',
    fresh ? BRIDGE_SEVERITY[connection.bridge] : 'unknown',
  ];

  // Fleet severity is the WORST across every instance AND every connection
  // dimension. A critical instance can never hide behind a healthy one.
  const severity = worstSeverity([
    ...dimensionSeverities,
    ...instances.map((i) => i.severity),
    ...(instances.length ? [] : (['unknown'] as Severity[])),
  ]);

  return {
    severity,
    backend,
    telemetry: connection.telemetry,
    node: connection.node,
    bridge: connection.bridge,
    firstRun: false,
    headline: HEADLINE[severity],
    instances,
  };
}

const HEADLINE: Record<Severity, string> = {
  critical: 'Active danger — node reports a blocking condition',
  warning: 'Attention — the node is not in a fully permissive state',
  unknown: 'Not observable — insufficient current evidence',
  healthy: 'All observed safety dimensions report affirmative, fresh evidence',
};

export const SEVERITY_LABEL: Record<Severity, string> = {
  critical: 'CRITICAL',
  warning: 'WARNING',
  unknown: 'UNKNOWN',
  healthy: 'NOMINAL',
};

/** Absolute + relative rendering. Both are always available (accessibility). */
export function formatAbsolute(iso: string | null): string {
  return iso ?? 'none';
}

export function formatRelative(ageSeconds: number | null): string {
  if (ageSeconds === null) return 'unknown';
  if (ageSeconds < 60) return `${Math.floor(ageSeconds)}s ago`;
  if (ageSeconds < 3600) return `${Math.floor(ageSeconds / 60)}m ago`;
  if (ageSeconds < 86_400) return `${Math.floor(ageSeconds / 3600)}h ago`;
  return `${Math.floor(ageSeconds / 86_400)}d ago`;
}

/** Worst-first ordering so a critical instance is never below a healthy one. */
export function orderInstanceOps(instances: InstanceOps[]): InstanceOps[] {
  return [...instances].sort(
    (a, b) =>
      SEVERITY_ORDER.indexOf(a.severity) - SEVERITY_ORDER.indexOf(b.severity) ||
      a.instanceId.localeCompare(b.instanceId)
  );
}
