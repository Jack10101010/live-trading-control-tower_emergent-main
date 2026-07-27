/**
 * API fetchers for TanStack Query.
 *
 * All hooks in `useRepository.ts` derive from `/api/world` today, since the
 * fixture ships as one document. When individual endpoints are wired
 * server-side to MongoDB, each hook can point its query at its own endpoint
 * (`/api/fleet`, `/api/policy/{pair}/matrix`, ...) with zero change to consumers.
 */
import type {
  Account,
  BlockedIntent,
  Broker,
  BrokerHealth,
  DecisionChain,
  Deployment,
  EdgeMonitor,
  EventEntry,
  GhostTrade,
  LiveTrade,
  Package,
  PolicyMatrixData,
  Recommendation,
  SystemConfidence,
  WorldFixture,
} from '@/types/domain';

/** Runtime-layer health (distinct from broker health). Backend `/runtime/health`. */
export interface RuntimeHealth {
  overlayLoaded: boolean;
  runtimeDbHealthy: boolean;
  eventStoreHealthy: boolean;
  commandsEnabled: boolean;
  persistenceEnabled: boolean;
  replayStateAvailable: boolean;
  overlayCount: number;
  eventCount: number;
  checkedAt: string;
  /** Active broker (Phase 6): connection lifecycle + advertised capabilities. */
  broker?: {
    kind: string;
    brokerId: string;
    connection: string;
    capabilities: Record<string, boolean>;
  };
  /** Broker sync/reconciliation status (Phase 7). */
  sync?: {
    lastSync: string | null;
    lastSuccessfulSync: string | null;
    syncDuration: number | null;
    syncStatus: string | null;
    syncSequence: number;
  };
  /** Execution orchestrator status (Phase 8). */
  orchestrator?: {
    orchestratorHealthy: boolean;
    executions: number;
    lastExecution: { command: string; status: string; dryRun: boolean; totalMs: number; at: string } | null;
    averageExecutionMs: number;
    brokerLatencyMs: number;
    dryRunEnabled: boolean;
    validationFailures: number;
    policyFailures: number;
  };
  /** Strategy engine status (Phase 9). */
  strategy?: {
    strategyHealthy: boolean;
    evaluations: number;
    averageEvaluationMs: number;
    lastEvaluationMs: number;
    lastEvaluationAt: string | null;
    lastStrategy: string | null;
  };
  /** Scheduler status (Phase 10). */
  scheduler?: {
    schedulerHealthy: boolean;
    queueDepth: number;
    running: boolean;
    completed: number;
    failed: number;
    averageDurationMs: number;
    lastExecution: SchedulerJob | null;
  };
  /** Market Data Engine status (Phase 11; feed fields added Phase 13). */
  marketData?: {
    marketDataHealthy: boolean;
    provider: string | null;
    snapshotsProduced: number;
    snapshotAgeMs: number | null;
    updateLatencyMs: number;
    averageLatencyMs: number;
    lastSnapshotAt: string | null;
    dataIntegrity: boolean | null;
    currentSymbol: string | null;
    currentTimeframe: string | null;
    /** Active-provider feed status (Phase 13). */
    connection?: string | null;
    symbolCount?: number | null;
    feedLatencyMs?: number | null;
    lastUpdate?: string | null;
  };
  /** Risk Engine status (Phase 12). */
  risk?: {
    riskHealthy: boolean;
    assessments: number;
    allowed: number;
    warnings: number;
    denials: number;
    averageAssessmentMs: number;
    lastAssessmentMs: number;
    lastAssessmentAt: string | null;
    lastSeverity: string | null;
    lastAssessment: {
      deployment: string | null;
      symbol: string | null;
      severity: string;
      allowed: boolean;
      reason: string;
      at: string;
    } | null;
  };
  /** Portfolio Engine status (Phase 14). */
  portfolio?: {
    portfolioHealthy: boolean;
    cycles: number;
    allocations: number;
    deferred: number;
    rejected: number;
    totalCapital: number;
    allocatedCapital: number;
    availableCapital: number;
    utilisationPct: number;
    averageCycleMs: number;
    lastCycleMs: number;
    lastCycleAt: string | null;
  };
}

/** One OHLC bar (Phase 16) — matches the ChartPanel `Candle` shape. */
export interface OhlcBar {
  time: number;
  open: number;
  high: number;
  low: number;
  close: number;
}

/** Candle-series response from the Market Data Engine (Phase 16). */
export interface MarketCandles {
  symbol: string;
  timeframe: string;
  provider: string | null;
  end: string;
  count: number;
  candles: OhlcBar[];
  // Data-provenance diagnostics (Chart Status System). Present on the live/candles
  // path; optional so replay/fixture responses and older callers still typecheck.
  // Consumed only via deriveChartContext — never read ad hoc by components.
  source?: string | null;
  requestId?: string | null;
  cacheHit?: boolean | null;
  fellBack?: boolean | null;
  staleLive?: boolean | null;
  polygonStatus?: string | null;
  cacheAgeSeconds?: number | null;
  lastSuccessAt?: number | null; // unix seconds
  lastFailedAt?: number | null;  // unix seconds
  providerNote?: string | null;  // last provider error class, if any
}

/** Active risk limit set (Phase 12) — engine defaults overlaid with fixture fundedRules. */
export interface RiskLimits {
  maxOpenTrades: number;
  maxExposureLots: number;
  maxDailyLoss: number;
  maxFloatingLoss: number;
  minMarketConfidence: number;
}

/** Immutable market snapshot (Phase 11) — the single owner is the Market Data Engine. */
export interface MarketSnapshot {
  snapshotId: string;
  timestamp: string;
  symbol: string;
  timeframe: string;
  ohlc: { open: number; high: number; low: number; close: number };
  spread: number;
  session: string;
  marketState: { state: string | null; confidence: number; confirmed: boolean; source: string | null };
  volatility: { value: number; threshold: number; regime: string };
  trend: { direction: string; strength: string; adx: number };
  liquidity: { tier: string; score: number };
  integrity: { ok: boolean; checks: Record<string, boolean>; issues: string[] };
  provider: string;
  source: string;
  asOf: string | null;
}

export interface SchedulerJob {
  id: string;
  strategy: string | null;
  deployment: string | null;
  trigger: string;
  scheduledAt: string;
  startedAt: string | null;
  finishedAt: string | null;
  status: string;
  durationMs: number | null;
  result: { evaluated?: number; fired?: number; executed?: number; error?: string } | null;
}

/** Scheduler status view (Phase 10). */
export interface SchedulerStatus {
  schedulerHealthy: boolean;
  queueDepth: number;
  running: boolean;
  completed: number;
  failed: number;
  averageDurationMs: number;
  lastExecution: SchedulerJob | null;
  currentTrigger: string | null;
  triggerTypes: string[];
  activeTriggers: string[];
  schedules: Array<{ id: string; trigger: string; intervalMs: number; enabled: boolean }>;
  recentHistory: SchedulerJob[];
}

/** Scheduler tick result — advances the clock; may run a strategy evaluation. */
export interface SchedulerTick {
  skipped: boolean;
  ran: boolean;
  queueDepth: number;
  strategy: StrategyEvaluation | null;
  scheduler: SchedulerStatus;
}

/** One strategy evaluation result (Phase 9). Read-only Decisions + candidates. */
export interface StrategyEvaluation {
  strategyId: string | null;
  strategyName: string;
  decisions: Array<{
    decisionId: string;
    deploymentId: string;
    pair: string;
    policyCell: string | null;
    marketState: string | null;
    score: number;
    confidence: number;
    signal: string;
    reasoning: string;
    policyFailures: string[];
    candidateCommands: Array<{ name: string; payload: unknown; rationale: string }>;
    evaluatedAt: string;
  }>;
  report: {
    evaluated: number;
    fired: number;
    held: number;
    rejected: number;
    policyFailures: number;
    candidateCommands: number;
    executed: number;
    pipeline: string[];
    durationMs: number;
  };
  metrics: {
    strategyHealthy: boolean;
    evaluations: number;
    averageEvaluationMs: number;
    lastEvaluationMs: number;
    lastEvaluationAt: string | null;
    lastStrategy: string | null;
  };
  at: string;
}

/** One broker reconciliation cycle result (Phase 7). Structured, read-only. */
export interface BrokerReconciliation {
  status: string;
  durationMs: number;
  at: string;
  syncSequence?: number;
  brokerSnapshot: {
    connection: string;
    counts: { positions: number; orders: number; accounts: number };
    faultNotes?: string[];
  };
  runtimeSnapshot: {
    connectionState: string;
    eventSeq: number;
    counts: { positions: number; orders: number; deployments: number; accounts: number };
  };
  reconciliation: {
    findings: Array<{ type: string; severity: string; entityId: string; detail: string }>;
    summary: {
      status: string;
      healthy: boolean;
      warnings: number;
      errors: number;
      brokerPositions: number;
      runtimePositions: number;
      brokerOrders: number;
      runtimeOrders: number;
    };
  };
}

/** One consolidated operator-preferences model, persisted in the runtime overlay. */
export interface OperatorPreferences {
  theme?: string;
  inspectorWidth?: number;
  eventDockOpen?: boolean;
  matrixLens?: string;
  replaySpeed?: number;
}

import { ApiAuthError, authHeader } from '@/lib/authSession';

const BACKEND_URL = (import.meta.env.REACT_APP_BACKEND_URL as string) || '';

if (!BACKEND_URL) {
  // Fail loud in production but keep dev working with an empty prefix
  // (Vite proxy could handle it; we don't rely on that).
  // eslint-disable-next-line no-console
  console.warn('[api] REACT_APP_BACKEND_URL is not set. Requests will hit relative paths.');
}

export const apiUrl = (path: string): string => `${BACKEND_URL}/api${path}`;

function idempotencyKey(): string {
  const c = (globalThis as { crypto?: { randomUUID?: () => string } }).crypto;
  return c?.randomUUID ? c.randomUUID() : `idem_${Date.now()}_${Math.random().toString(36).slice(2)}`;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  // ARCH-3: the operator session header is applied HERE, by the single owner.
  // No component or hook builds its own Authorization header.
  const res = await fetch(apiUrl(path), {
    ...init,
    headers: { Accept: 'application/json', ...authHeader(), ...(init?.headers ?? {}) },
  });
  if (res.status === 401) throw new ApiAuthError(401, path);      // unauthorized ≠ offline
  if (res.status === 503) {
    const body = await res.clone().json().catch(() => null);
    if (body && body.code === 'authentication_misconfigured') throw new ApiAuthError(503, path);
  }
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`API ${res.status} ${path}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

/**
 * Truthful backend PROCESS health (UI-0). `serverTime` is the real clock — the old
 * shape returned the fixture's frozen `meta.asOf` as `asOf`, which read as freshness
 * and was not. `brokerKind: 'mock'` must never be read as MT5 connectivity.
 */
export interface BackendHealth {
  status: string;
  scope: 'process';
  serverTime: string;
  backendMode: string;
  brokerKind: string;
  liveNodeConnected: boolean;
  tradingReady: boolean;
  dataSources: { world: string; broker: string; nodeTelemetry: string };
  nodeTelemetry: { connected: boolean; instances: string[] };
  fixture: { available: boolean; version?: string; contractVersion?: string; asOf?: string };
}

/* ── UI-1: truthful connection state ─────────────────────────────────────────
 * Mirrors backend/connection_state.py. Four INDEPENDENT dimensions — there is no
 * single `connected` boolean, because a reachable backend, a publishing node and
 * a reachable MT5 bridge are three different facts.
 *
 * `unknown` = no evidence either way. `unavailable` = positive evidence that
 * something could not be read or reached. They must never be merged. */
export type TelemetryState = 'never_received' | 'fresh' | 'stale' | 'unavailable';
export type NodeState = 'unknown' | 'connected' | 'disconnected';
export type BridgeState = 'unknown' | 'healthy' | 'degraded' | 'unavailable';
export type SnapshotProblem = 'malformed_snapshot' | 'unsupported_schema' | 'unknown_schema';

export interface ConnectionInstance {
  instanceId: string;
  telemetry: TelemetryState;
  node: NodeState;
  nodeEvidence: string;
  bridge: BridgeState;
  bridgeEvidence: string;
  problem: SnapshotProblem | null;
  schemaVersion: string | null;
  legacySource: boolean;
  /** The NODE's own publish time — the only source of freshness. */
  publishedAt: string | null;
  /** When this tower received it. Never used to compute age. */
  receivedAt: string | null;
  ageSeconds: number | null;
  staleAfterSeconds: number;
  mode: string | null;
  engineVersion: string | null;
}

export interface ConnectionState {
  observedAt: string;
  staleAfterSeconds: number;
  firstRun: boolean;
  telemetry: TelemetryState;
  node: NodeState;
  bridge: BridgeState;
  instanceCount: number;
  instances: ConnectionInstance[];
  emptyState: string | null;
}

/* ── UI-3: the persisted node snapshot, as `GET /api/live/status` returns it ──
 * These mirror `live/telemetry.py` (schema ct.node-telemetry.v1) exactly — every
 * field below was re-derived from a real response. Optional/nullable throughout,
 * because the node publishes `null` for anything it did not observe and the UI
 * must render that as unknown rather than as a default. */
export interface TelemetryOpenEligibility {
  /** Tri-state: never `true` — telemetry cannot authorize an OPEN (UI-1). */
  eligible: boolean | null;
  reasons: string[];
}

export interface TelemetrySnapshot {
  schema_version: string;
  instance_id: string;
  published_at: string;
  runtime?: {
    mode?: string | null;
    submission_disabled?: boolean | null;
    kill_switch_active?: boolean | null;
    open_eligibility?: TelemetryOpenEligibility | null;
  } | null;
  reconciliation?: {
    available?: boolean | null;
    clean?: boolean | null;
    frozen?: boolean | null;
    recovery_required?: boolean | null;
    unresolved_sent_count?: number | null;
    snapshot_status?: string | null;
  } | null;
  account?: {
    identity?: { available?: boolean | null; fingerprint?: string | null } | null;
    health?: {
      available?: boolean | null;
      healthy?: boolean | null;
      trade_allowed?: boolean | null;
      trade_expert?: boolean | null;
      reasons?: string[] | null;
    } | null;
  } | null;
  arming?: {
    status?: string | null;
    armed?: boolean | null;
    expires_at?: string | null;
    attempts_remaining?: number | null;
    fingerprint_matches?: boolean | null;
    reason?: string | null;
  } | null;
  execution?: {
    cycle_frozen?: boolean | null;
    pending_intents?: unknown[] | null;
    blocks?: Array<{ rail?: string | null; intent_id?: string | null }> | null;
    unresolved_sent?: unknown[] | null;
  } | null;
  engine?: { symbol?: string | null; timeframe?: string | null } | null;
  market?: { available?: boolean | null; feed_healthy?: boolean | null } | null;
}

/** One entry of `GET /api/live/status` — the UI-2 read-side envelope. */
export interface LiveStatusEntry {
  instance_id: string;
  schema_version: string;
  legacy_source: boolean;
  published_at: string;
  observed_at: string;
  received_at: string | null;
  age_seconds: number | null;
  stale: boolean;
  stale_after_seconds: number;
  snapshot: TelemetrySnapshot;
}

export interface LiveStatus {
  schemaVersion: string;
  observedAt: string;
  instances: string[];
  statuses: Record<string, LiveStatusEntry>;
  emptyState: string | null;
}

/* ── UI-9: security configuration STATUS (value-free) ────────────────────────
 * Mirrors backend/security_config.py's describe_config. There is deliberately no
 * value field of any kind: the backend reports whether a variable is set, never
 * what it is set to. `active` is always false — UI-9 introduces no connectivity. */
export type SecurityVarStatus = 'configured' | 'missing' | 'invalid';

/* UI-10: the CORS browser boundary, described the same value-free way — counts and
 * classifications, never origin strings. `wildcardEnabled` is structurally false. */
export interface CorsPolicyStatus {
  policyActive: boolean;
  source: 'safe_default' | 'explicit' | 'invalid_fallback';
  originCount: number;
  credentialsEnabled: boolean;
  allowedMethods: string[];
  allowedHeaders: string[];
  localOnly: boolean;
  wildcardEnabled: boolean;
  valid: boolean;
  issueCodes: string[];
}

/* UI-11: authentication STATE only. There is deliberately no token field of any
 * kind — no value, prefix, suffix, hash or length — so the frontend cannot render
 * or store a credential even if the backend were changed to send one. */
export interface AuthPolicyStatus {
  active: boolean;
  configured: boolean;
  defaultState: 'disabled';
  scheme: 'bearer';
  valid: boolean;
  misconfigured: boolean;
  tokenPresent: boolean;
  tokenLengthOk: boolean;
  minTokenLength: number;
  protectedRouteCount: number;
  publicRouteCount: number;
  publicRoutes: string[];
  docsPolicy: string;
  openapiPolicy: string;
  issueCodes: string[];
  remoteActivation: 'not_active';
}

export interface SecurityConfigStatus {
  mode: SecurityVarStatus;
  variables: Record<string, { status: SecurityVarStatus; secret: boolean; path: boolean }>;
  findings: Array<{ severity: 'error' | 'warning'; variable: string | null; message: string }>;
  hasErrors: boolean;
  cors?: CorsPolicyStatus;
  auth?: AuthPolicyStatus;
  /* ARCH-3: explicit truthful dimensions (replaces the removed `active` constant). */
  ingestAuth?: {
    enabled: boolean; enforcing: boolean; misconfigured: boolean;
    tokenPresent: boolean; issueCodes: string[]; routes: string[];
    legacyOperatorTokenAllowed: boolean; degraded: boolean;
  };
  connectivity?: {
    profile: string;
    approvedProfiles: string[];
    remoteApproved: boolean;
    transport: { enabled: boolean; selectedKind: string; misconfigured: boolean; reason: string };
    connectionPolicy: { allowed: boolean; reason: string; profile: string;
                        missingPrerequisites?: string[] };
    outboundNodeAuth: { tokenPresent: boolean };
    missingRemotePrerequisites: string[];
    localOnly: boolean;
  };
}

/* UI-14: read-only Control-Tower -> node integration status. Value-free: no
 * endpoint, no token, only a state classification + the freshness envelope UI-2
 * already exposes. Provenance is always `remote-node`; a failure is a state, never
 * a fixture fallback. */
export type RemoteNodeState =
  | 'disabled' | 'connecting' | 'healthy' | 'degraded' | 'stale'
  | 'unauthorized' | 'unreachable';

export interface RemoteNodeStatus {
  enabled: boolean;
  state: RemoteNodeState;
  provenance: 'remote-node';
  observedAt: string;
  reason: string | null;
  detail: string | null;
  health: { ok: boolean; reason: string } | null;
  telemetry: {
    available: boolean;
    instanceId: string | null;
    schemaVersion: string | null;
    publishedAt: string | null;
    ageSeconds: number | null;
    staleAfterSeconds: number | null;
    stale: boolean;
    problem: string | null;
  } | null;
}

/* ── UI-17: authenticated read-only operator command surface ─────────────────
 * The three permitted read-only command types, and the value-free lifecycle view
 * the backend returns (`command_channel.safe_view` + transport-step metadata). There
 * is deliberately NO token, endpoint or raw-payload field: the browser never handles
 * a credential, and the payload is redacted server-side. `state` is the canonical
 * UI-15 lifecycle; `transport` explains a command that was not acknowledged
 * (disabled / unreachable / timeout). */
export type OperatorCommandType = 'noop' | 'request_health' | 'request_telemetry';
export type OperatorCommandState =
  | 'pending' | 'accepted' | 'rejected' | 'expired' | 'completed' | 'failed';

export interface OperatorCommandView {
  schemaVersion: string;
  commandId: string;
  commandType: string;
  idempotencyKey: string;
  target: string | null;
  operatorRef: string | null;
  requestedAt: string;
  expiresAt: string;
  state: OperatorCommandState;
  createdAt: string;
  updatedAt: string;
  acknowledged: boolean;
  accepted: boolean | null;
  completed: boolean;
  outcomeState: string | null;
  payload: Record<string, unknown>;
  transport: { reason: string; detail: string | null } | null;
  enabled: boolean;
}

/** A stable rejection (HTTP 422): the backend maps a UI-15 CommandError to a fixed
 *  `code` and a redaction-safe `detail`. Thrown by `operatorSubmitCommand` on 422. */
export interface OperatorCommandRejection {
  error: 'rejected';
  code: string;
  detail: string | null;
}

export class OperatorCommandError extends Error {
  constructor(public readonly code: string, public readonly detail: string | null) {
    super(code);
    this.name = 'OperatorCommandError';
  }
}

/* LIVE-1: the canonical execution read model's broker block (read-only). */
export interface ExecutionBrokerState {
  kind: string;
  connection: string;
  provenance: 'live_mt5' | 'mock-fixture' | string;
  readOnly: boolean;
  liveWriteCapable: boolean;
  account: {
    available: boolean;
    login_masked?: string;
    broker_company?: string | null;
    server?: string | null;
    currency?: string | null;
    balance?: number | null;
    equity?: number | null;
    margin?: number | null;
    margin_level?: number | null;
    leverage?: number | null;
    code?: string;
    detail?: string;
  };
  openPositions: number | null;
  openOrders: number | null;
  recentExecutions: number | null;
  reads: { accountSnapshot: string; reconcileSnapshot: string; recentExecutions: string };
  observedAt: string;
}

export interface MarketOrderTelemetry {
  available: boolean;
  code?: string;
  pendingSubmissions?: number;
  awaitingReconciliation?: number;
  activeMarketOrders?: number;
  submissionFailures?: number;
  lastSubmission?: {
    intentId: string | null;
    instrument: string | null;
    side: string | null;
    quantity: number | null;
    state: string | null;
    brokerTicket: string | null;
    ackStatus: string | null;
    finalReason: string | null;
    latencyMs: number | null;
    createdAt: string | null;
    updatedAt: string | null;
  } | null;
  lastBrokerTicket?: string | null;
  provenance: string;
}

export interface OperationView {
  intentId: string | null;
  command: string;
  state: string | null;
  brokerRef: string | null;
  updatedAt: string | null;
  finalReason?: string | null;
  latencyMs?: number | null;
}

export interface GovernanceTelemetry {
  executionMode: string;
  modeProvenance: string;
  authorization: { active: boolean; summary?: Record<string, unknown>; expiresAt?: string };
  operations: {
    available: boolean;
    inFlight?: OperationView[];
    awaitingAcknowledgementConfirmation?: OperationView[];
    reconciliationRequired?: OperationView[];
    confirmed?: number;
    failures?: number;
    lastOperation?: OperationView | null;
    entityLocks?: Array<{ entityRef: string; intentId: string; operation: string; acquiredAt: string }>;
  };
  provenance: string;
}

export interface ExecutionStateView {
  schemaVersion: string;
  observedAt: string;
  broker?: ExecutionBrokerState;
  marketOrder?: MarketOrderTelemetry;
  governance?: GovernanceTelemetry;
  readiness: { tradingReady: boolean; gates: Record<string, boolean> };
}

export interface EntityOperationResponse {
  ok: boolean;
  commandId: string;
  intentId: string | null;
  lifecycleState: string | null;
  brokerRef: string | null;
  acknowledgement: Record<string, unknown> | null;
  reconciliationConfirmed: boolean;
  deduplicated: boolean;
  latencyMs: number | null;
  acceptedAt: string;
}

/* ── LIVE-4A: canonical operational projection read models ────────────────── */
export interface ProjectionFreshness {
  projectionAt: string;
  sourceAt: string | null;
  ageSeconds: number | null;
  stale: boolean;
  available: boolean;
  staleAfterSeconds: number;
  status: 'ok' | 'stale' | 'unavailable' | string;
  detail: string | null;
}

export interface NodeOperationalView {
  nodeId: string;
  instanceId: string | null;
  deployment: string | null;
  adapter: string | null;
  broker: string | null;
  accountFingerprintMasked: string | null;
  connectionState: string | null;
  heartbeatAgeSeconds: number | null;
  health: string | null;
  executionMode: string | null;
  authorizationSummary: Record<string, unknown> | null;
  reconciliationState: string | null;
  openPositionCount: number | null;
  openOrderCount: number | null;
  activeScenarioCount: number | null;
  lastActivity: string | null;
  telemetryAgeSeconds: number | null;
  warnings: string[];
  provenance: string;
  freshness: ProjectionFreshness | null;
}

export interface AccountOperationalView {
  accountFingerprint: string | null;
  broker: string | null;
  server: string | null;
  balance: number | null;
  equity: number | null;
  margin: number | null;
  marginLevel: number | null;
  leverage: number | null;
  currency: string | null;
  unrealizedPnL: number | null;
  realizedPnLToday: number | null;
  openRisk: number | null;
  connectionState: string | null;
  provenance: string;
  freshness: ProjectionFreshness | null;
}

export interface LifecycleStep {
  state: string | null;
  at: string | null;
  reason: string | null;
  brokerRef?: string | null;
}

export interface OrderOperationalView {
  intentId: string | null;
  brokerOrderReference: string | null;
  brokerTicket: string | null;
  instrument: string | null;
  side: string | null;
  orderType: string | null;
  quantity: number | null;
  requestedPrice: number | null;
  currentState: string | null;
  lifecycle: LifecycleStep[];
  brokerStatus: string | null;
  scenarioId: string | null;
  timestamps: { createdAt: string | null; updatedAt: string | null };
  reconciliation: { required?: boolean; criticalUnresolved?: boolean; lastRunId?: string | null } | null;
  nodeId: string | null;
  provenance: string;
}

export interface PositionOperationalView {
  brokerPositionReference: string | null;
  instrument: string | null;
  side: string | null;
  quantity: number | null;
  entryPrice: number | null;
  currentPrice: number | null;
  unrealizedPnL: number | null;
  stopLoss: number | null;
  takeProfit: number | null;
  ageSeconds: number | null;
  lifecycle: LifecycleStep[];
  protectionState: string | null;
  reconciliation: { required?: boolean; criticalUnresolved?: boolean; locked?: boolean; lockOperation?: string | null } | null;
  scenarioId: string | null;
  nodeId: string | null;
  accountFingerprint: string | null;
  provenance: string;
}

export interface ScenarioOperationalView {
  scenarioId: string;
  instrument: string | null;
  direction: string | null;
  session: string | null;
  structure: string | null;
  entryModel: string | null;
  timeframe: string | null;
  status: string | null;
  outcome: string | null;
  nodeId: string | null;
  accountFingerprintMasked: string | null;
  linkedRecommendation: string | null;
  linkedIntentCount: number;
  linkedOrderCount: number;
  linkedPositionCount: number;
  createdAt: string | null;
  updatedAt: string | null;
  expiresAt: string | null;
  ageSeconds: number | null;
  active: boolean;
  tags: string[];
  provenance: string;
  freshness: ProjectionFreshness | null;
}

export interface ScenarioEventView {
  scenarioId: string;
  eventType: string;
  at: string;
  reason: string;
  data: Record<string, unknown>;
  sequence: number | null;
}

export interface ScenarioSummaryView {
  total: number;
  active: number;
  byStatus: Record<string, number>;
  byInstrument: Record<string, number>;
  byOutcome: Record<string, number>;
}

export interface OperationalSummaryView {
  schemaVersion: string;
  projectionTimestamp: string;
  nodes: NodeOperationalView[];
  accounts: AccountOperationalView[];
  activeOrders: OrderOperationalView[];
  openPositions: PositionOperationalView[];
  activeScenarios: ScenarioOperationalView[];
  reconciliationIssues: Array<{ class: string | null; entityId: string | null; critical: boolean; detail: string | null; resolved: boolean }>;
  activeOperations: Array<{ entityRef: string | null; intentId: string | null; operation: string | null; acquiredAt: string | null }>;
  warnings: string[];
  counts: Record<string, number>;
  freshness: ProjectionFreshness | null;
}

/* ── LIVE-4C: canonical Trade Ledger read models ──────────────────────────── */
export interface ClosedTradeOperationalView {
  tradeId: string;
  status: string;
  version: number;
  instrument: string | null;
  side: string | null;
  quantity: number | null;
  averageEntryPrice: number | null;
  averageExitPrice: number | null;
  grossRealizedPnL: number | null;
  totalCosts: number | null;
  netRealizedPnL: number | null;
  realizedR: number | null;
  exitClassification: string | null;
  outcome: string | null;
  accountCurrency: string | null;
  openedAt: string | null;
  closedAt: string | null;
  durationSeconds: number | null;
  scenarioId: string | null;
  origin: string | null;
  costCompleteness: string | null;
  riskCompleteness: string | null;
  warnings: string[];
  conflicts: string[];
  finalized: boolean;
  detail: Record<string, unknown> | null;
  provenance: string;
}

export interface LedgerOperationalSummary {
  finalizedTradeCount: number;
  incompleteTradeCount: number;
  conflictedTradeCount: number;
  amendedTradeCount: number;
  grossRealizedPnL: number | null;
  netRealizedPnL: number | null;
  totalCosts: number | null;
  latestClosedAt: string | null;
  accountCurrency: string | null;
  availability: string;
  provenance: string;
  freshness: ProjectionFreshness | null;
}

export interface TradeLedgerView {
  available: boolean;
  code: string | null;
  summary: LedgerOperationalSummary;
  trades: ClosedTradeOperationalView[];
  totalCount: number;
  provenance: string;
  limit?: number;
  offset?: number;
}

export interface LedgerEventView {
  eventId: string;
  tradeId: string;
  sequence: number;
  eventType: string;
  occurredAt: string;
  recordedAt: string;
  payload: Record<string, unknown>;
  provenance: string;
}

export interface ExecutionModeView {
  mode: string;
  history: Array<Record<string, unknown>>;
  manualLiveGates: Record<string, boolean>;
  observedAt: string;
}

export interface MarketOrderResponse {
  ok: boolean;
  commandId: string;
  intentId: string | null;
  lifecycleState: string | null;
  brokerTicket: string | null;
  acknowledgement: Record<string, unknown> | null;
  deduplicated: boolean;
  latencyMs: number | null;
  timeline: Record<string, number>;
  acceptedAt: string;
}

export interface MarketOrderDenial {
  status: string;
  stage: string;
  reason: string;
  code: string;
}

export class MarketOrderError extends Error {
  constructor(
    public readonly code: string,
    public readonly stage: string,
    public readonly reason: string
  ) {
    super(`market order ${code} at ${stage}`);
    this.name = 'MarketOrderError';
  }
}

export const api = {
  world: () => apiFetch<WorldFixture>('/world'),
  executionState: () => apiFetch<ExecutionStateView>('/execution/state'),
  /* LIVE-2 — submit the ONE executable broker operation. A FRESH idempotency key
   * per intentional operator action: a network retry of the SAME action returns
   * the original durable outcome (never a second broker order); a new action is
   * distinct. A 422 denial surfaces as `MarketOrderError(code, stage, reason)`. */
  submitMarketOrder: async (order: {
    instrument: string;
    side: 'long' | 'short';
    quantity: number;
    stopLoss?: number;
    takeProfit?: number;
    comment?: string;
  }): Promise<MarketOrderResponse> => {
    const res = await fetch(apiUrl('/execution/market-order'), {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey(),
        ...authHeader(),
      },
      body: JSON.stringify(order),
    });
    if (res.status === 422) {
      const body = (await res.json().catch(() => null)) as { detail?: MarketOrderDenial } | null;
      const d = body?.detail;
      throw new MarketOrderError(d?.code ?? 'denied', d?.stage ?? 'unknown', d?.reason ?? '');
    }
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`API ${res.status} /execution/market-order: ${text.slice(0, 200)}`);
    }
    return (await res.json()) as MarketOrderResponse;
  },
  /* LIVE-3 — the three manual-management operations + governance. Each mutation
   * carries a fresh idempotency key and explicit confirmation; denials surface
   * as MarketOrderError(code, stage, reason). */
  entityOperation: async (
    path: string,
    body: Record<string, unknown>
  ): Promise<EntityOperationResponse> => {
    const res = await fetch(apiUrl(path), {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Idempotency-Key': idempotencyKey(),
        ...authHeader(),
      },
      body: JSON.stringify({ ...body, confirm: true }),
    });
    if (res.status === 422) {
      const parsed = (await res.json().catch(() => null)) as { detail?: MarketOrderDenial } | null;
      const d = parsed?.detail;
      throw new MarketOrderError(d?.code ?? 'denied', d?.stage ?? 'unknown', d?.reason ?? '');
    }
    if (!res.ok) {
      const text = await res.text().catch(() => '');
      throw new Error(`API ${res.status} ${path}: ${text.slice(0, 200)}`);
    }
    return (await res.json()) as EntityOperationResponse;
  },
  modifyProtection: (ref: string, body: { stopLoss?: number; takeProfit?: number; reason?: string }) =>
    api.entityOperation(`/execution/positions/${encodeURIComponent(ref)}/protection`, body),
  cancelPendingOrder: (ref: string, body: { reason?: string } = {}) =>
    api.entityOperation(`/execution/orders/${encodeURIComponent(ref)}/cancel`, body),
  closePosition: (ref: string, body: { reason?: string } = {}) =>
    api.entityOperation(`/execution/positions/${encodeURIComponent(ref)}/close`, body),
  executionMode: () => apiFetch<ExecutionModeView>('/execution/mode'),
  /* LIVE-4A — the canonical operational projection. `operationsSummary` is the
   * SINGLE polling source for the dashboard: every card derives from it, so no
   * component re-aggregates operational truth. The per-collection endpoints
   * exist for focused views and parity with the backend surface. */
  operationsSummary: () => apiFetch<OperationalSummaryView>('/operations/summary'),
  /* LIVE-4B — the canonical Scenario read surface. Read-only and derived: the
   * UI never constructs, mutates or infers a scenario. */
  scenarios: (filters: { instrument?: string; session?: string; nodeId?: string; status?: string } = {}) => {
    const q = new URLSearchParams(
      Object.entries(filters).filter(([, v]) => v) as [string, string][]
    ).toString();
    return apiFetch<{ scenarios: ScenarioOperationalView[]; summary: ScenarioSummaryView | null }>(
      `/scenarios${q ? `?${q}` : ''}`
    );
  },
  activeScenarios: () => apiFetch<{ scenarios: ScenarioOperationalView[] }>('/scenarios/active'),
  /* LIVE-4C — the canonical Trade Ledger. Read-only: the UI renders recorded
   * ledger facts and performs NO financial reconstruction of its own. */
  ledgerSummary: () => apiFetch<{ summary: LedgerOperationalSummary }>('/ledger/summary'),
  ledgerTrades: (filters: {
    status?: string; instrument?: string; scenarioId?: string; nodeId?: string;
    limit?: number; offset?: number;
  } = {}) => {
    const q = new URLSearchParams(
      Object.entries(filters).filter(([, v]) => v !== undefined && v !== '')
        .map(([k, v]) => [k, String(v)]) as [string, string][]
    ).toString();
    return apiFetch<TradeLedgerView>(`/ledger/trades${q ? `?${q}` : ''}`);
  },
  ledgerTrade: (tradeId: string) =>
    apiFetch<ClosedTradeOperationalView>(`/ledger/trades/${encodeURIComponent(tradeId)}`),
  ledgerTradeHistory: (tradeId: string) =>
    apiFetch<{ events: LedgerEventView[]; latestVersion: number | null }>(
      `/ledger/trades/${encodeURIComponent(tradeId)}/history`),
  ledgerIncomplete: () => apiFetch<TradeLedgerView>('/ledger/incomplete'),
  ledgerConflicts: () => apiFetch<TradeLedgerView>('/ledger/conflicts'),
  scenarioHistory: (scenarioId?: string) =>
    apiFetch<{ events: ScenarioEventView[] }>(
      `/scenarios/history${scenarioId ? `?scenarioId=${encodeURIComponent(scenarioId)}` : ''}`),
  scenario: (scenarioId: string) =>
    apiFetch<ScenarioOperationalView & { history: ScenarioEventView[] }>(
      `/scenarios/${encodeURIComponent(scenarioId)}`),
  operationsNodes: () => apiFetch<{ nodes: NodeOperationalView[] }>('/operations/nodes'),
  operationsAccounts: () => apiFetch<{ accounts: AccountOperationalView[] }>('/operations/accounts'),
  operationsOrders: () => apiFetch<{ orders: OrderOperationalView[] }>('/operations/orders'),
  operationsPositions: () => apiFetch<{ positions: PositionOperationalView[] }>('/operations/positions'),
  setExecutionMode: (mode: string, reason: string) =>
    apiFetch<{ transitioned: boolean }>('/execution/mode', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode, reason, confirm: true }),
    }),
  authorizationGrants: () =>
    apiFetch<{ grants: Array<Record<string, unknown>> }>('/authorization/grants'),
  issueGrant: (body: Record<string, unknown>) =>
    apiFetch<{ issued: boolean; grant: Record<string, unknown> }>('/authorization/grants', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ...body, confirm: true }),
    }),
  revokeGrant: (id: string, reason: string) =>
    apiFetch<{ revoked: boolean }>(`/authorization/grants/${encodeURIComponent(id)}/revoke`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    }),
  securityConfig: () => apiFetch<SecurityConfigStatus>('/security/config'),
  liveConnection: () => apiFetch<ConnectionState>('/live/connection'),
  liveStatus: () => apiFetch<LiveStatus>('/live/status'),
  liveRemote: () => apiFetch<RemoteNodeStatus>('/live/remote'),
  /* UI-17 — submit ONE permitted read-only operator command. A FRESH idempotency
   * key is generated per call (i.e. per intentional operator action), so a retry of
   * the SAME action de-duplicates while a new action is distinct. No token handling:
   * the browser sends none. A 422 rejection surfaces as `OperatorCommandError(code)`. */
  operatorSubmitCommand: async (
    commandType: OperatorCommandType,
    opts: { ttlSeconds?: number } = {}
  ): Promise<OperatorCommandView> => {
    const res = await fetch(apiUrl('/operator/commands'), {
      method: 'POST',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json', ...authHeader() },
      body: JSON.stringify({
        commandType,
        idempotencyKey: idempotencyKey(),
        ...(opts.ttlSeconds ? { ttlSeconds: opts.ttlSeconds } : {}),
      }),
    });
    if (res.status === 422) {
      const body = (await res.json().catch(() => null)) as OperatorCommandRejection | null;
      throw new OperatorCommandError(body?.code ?? 'rejected', body?.detail ?? null);
    }
    if (!res.ok) {
      const body = await res.text().catch(() => '');
      throw new Error(`API ${res.status} /operator/commands: ${body.slice(0, 200)}`);
    }
    return (await res.json()) as OperatorCommandView;
  },
  operatorCommandStatus: (commandId: string) =>
    apiFetch<OperatorCommandView>(`/operator/commands/${encodeURIComponent(commandId)}`),
  operatorRecentCommands: () =>
    apiFetch<{ enabled: boolean; commands: OperatorCommandView[] }>('/operator/commands'),
  fleet: () =>
    apiFetch<{ deployments: Deployment[]; brokers: Broker[]; accounts: Account[]; asOf: string }>('/fleet'),
  health: () => apiFetch<BackendHealth>('/health'),
  edgeMonitor: () => apiFetch<EdgeMonitor>('/edge-monitor'),
  systemConfidence: () => apiFetch<SystemConfidence>('/system-confidence'),
  recommendations: () => apiFetch<Recommendation[]>('/recommendations'),
  featureFlags: () => apiFetch<Record<string, boolean>>('/feature-flags'),
  brokerHealth: () => apiFetch<{ brokers: Broker[]; health: BrokerHealth[] }>('/broker-health'),
  runtimeHealth: () => apiFetch<RuntimeHealth>('/runtime/health'),
  brokerReconciliation: () => apiFetch<BrokerReconciliation>('/broker/reconciliation'),
  /** Run one reconciliation cycle (appends one BotEvent). Drives the poll loop. */
  brokerSync: () =>
    apiFetch<BrokerReconciliation>('/broker/sync', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }),
  strategyDecisions: () => apiFetch<StrategyEvaluation>('/strategy/decisions'),
  /** Run one read-only strategy evaluation (Decisions + candidates; executes nothing). */
  strategyEvaluate: () =>
    apiFetch<StrategyEvaluation>('/strategy/evaluate', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }),
  schedulerStatus: () => apiFetch<SchedulerStatus>('/scheduler/status'),
  /** Current immutable market snapshot (Phase 11, read-only). */
  marketDataSnapshot: (symbol = 'EURUSD', timeframe = 'M15') =>
    apiFetch<MarketSnapshot>(
      `/market-data/snapshot?symbol=${encodeURIComponent(symbol)}&timeframe=${encodeURIComponent(timeframe)}`
    ),
  /** OHLC candle series from the Market Data Service (Phase 24). The single candle
   *  pipeline: `provider=replay` for replay; `start` makes it a RANGE query
   *  (historical scrolling); otherwise end-anchored `count` bars. */
  marketCandles: (opts: { symbol?: string; timeframe?: string; count?: number; end?: string; start?: string; provider?: string } = {}) => {
    const q = new URLSearchParams();
    q.set('symbol', opts.symbol ?? 'EURUSD');
    q.set('timeframe', opts.timeframe ?? 'M15');
    q.set('count', String(opts.count ?? 220));
    if (opts.end) q.set('end', opts.end);
    if (opts.start) q.set('start', opts.start);
    if (opts.provider) q.set('provider', opts.provider);
    return apiFetch<MarketCandles>(`/market-data/candles?${q.toString()}`);
  },
  /** M1 inspector (Phase 24): canonical M1 bars inside a parent-bar window. */
  m1Window: (symbol: string, startISO: string, endISO: string) =>
    apiFetch<MarketCandles>(
      `/market-data/m1-window?symbol=${encodeURIComponent(symbol)}&start=${encodeURIComponent(startISO)}&end=${encodeURIComponent(endISO)}`
    ),
  /** Active risk limit set (Phase 12, read-only). */
  riskLimits: (account?: string) =>
    apiFetch<RiskLimits>(`/risk/limits${account ? `?account=${encodeURIComponent(account)}` : ''}`),
  /** Advance the scheduler clock — the ONLY driver of WHEN strategy evaluations run. */
  schedulerTick: () =>
    apiFetch<SchedulerTick>('/scheduler/tick', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}',
    }),
  events: (pair?: string) =>
    apiFetch<EventEntry[]>(`/events${pair ? `?pair=${encodeURIComponent(pair)}` : ''}`),
  /** Realtime delta (Phase 5) — long-poll for events with seq > since. */
  eventsLive: (since: number, signal?: AbortSignal) =>
    apiFetch<{ events: EventEntry[]; seq: number; head: number; stale: boolean }>(
      `/events/live?since=${since}`,
      { signal }
    ),
  decision: (id: string) => apiFetch<DecisionChain>(`/decisions/${encodeURIComponent(id)}`),
  deployment: (id: string) => apiFetch<Deployment>(`/deployments/${encodeURIComponent(id)}`),
  activePackage: () => apiFetch<Package>('/packages/active'),
  packages: () => apiFetch<Package[]>('/packages'),
  policyMatrix: (instrument: string, version?: number) =>
    apiFetch<PolicyMatrixData>(`/policy/${encodeURIComponent(instrument)}/matrix${version ? `?version=${version}` : ''}`),
  operatorPreferences: () => apiFetch<OperatorPreferences>('/operator/preferences'),
  putOperatorPreferences: (patch: OperatorPreferences) =>
    apiFetch<OperatorPreferences>('/operator/preferences', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(patch),
    }),
  /** Mutation entry point — broker-free backend appends a BotEvent to the audit log. */
  command: (name: string, payload: unknown) =>
    apiFetch<{ ok: boolean; commandId: string; acceptedAt: string; echo: unknown; events?: EventEntry[] }>(
      `/commands/${encodeURIComponent(name)}`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Idempotency-Key': idempotencyKey() },
        body: JSON.stringify(payload ?? {}),
      }
    ),
  trades: (pair?: string, lane?: string) => {
    const q = new URLSearchParams();
    if (pair) q.set('pair', pair);
    if (lane) q.set('lane', lane);
    const qs = q.toString();
    return apiFetch<{ live: LiveTrade[]; ghost: GhostTrade[]; blocked: BlockedIntent[] }>(
      `/trades${qs ? `?${qs}` : ''}`
    );
  },
};

export const QK = {
  health: ['health'] as const,
  world: ['world'] as const,
  fleet: ['fleet'] as const,
  featureFlags: ['feature-flags'] as const,
  /** Root events key — invalidating this prefix refreshes every scoped events query. */
  events: ['events'] as const,
  eventsFor: (pair?: string) => (pair ? (['events', pair] as const) : (['events'] as const)),
  /** Root trades key — invalidating this prefix refreshes every scoped trades query. */
  trades: ['trades'] as const,
  tradesFor: (pair?: string, lane?: string) => ['trades', pair ?? 'all', lane ?? 'all'] as const,
  /** Root packages key; the list + active-package queries nest under it so one invalidation covers all. */
  packages: ['packages'] as const,
  packagesList: ['packages', 'list'] as const,
  activePackage: ['packages', 'active'] as const,
  brokerHealth: ['broker-health'] as const,
  edgeMonitor: ['edge-monitor'] as const,
  systemConfidence: ['system-confidence'] as const,
  recommendations: ['recommendations'] as const,
  runtimeHealth: ['runtime-health'] as const,
  brokerReconciliation: ['broker-reconciliation'] as const,
  strategyDecisions: ['strategy-decisions'] as const,
  schedulerStatus: ['scheduler-status'] as const,
  marketDataSnapshot: ['market-data-snapshot'] as const,
  marketCandles: (symbol: string, provider: string | undefined, count: number, end: string | undefined, timeframe = 'M15') =>
    ['market-candles', symbol, provider ?? 'active', count, end ?? 'default', timeframe] as const,
  riskLimits: ['risk-limits'] as const,
  decision: (id: string) => ['decision', id] as const,
  policyMatrix: (instrument: string, version?: number) =>
    ['policy-matrix', instrument, version ?? 'active'] as const,
  operatorPreferences: ['operator-preferences'] as const,
};
