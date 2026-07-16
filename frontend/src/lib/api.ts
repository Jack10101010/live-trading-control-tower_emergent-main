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
  const res = await fetch(apiUrl(path), {
    ...init,
    headers: { Accept: 'application/json', ...(init?.headers ?? {}) },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => '');
    throw new Error(`API ${res.status} ${path}: ${body.slice(0, 200)}`);
  }
  return (await res.json()) as T;
}

export const api = {
  world: () => apiFetch<WorldFixture>('/world'),
  fleet: () =>
    apiFetch<{ deployments: Deployment[]; brokers: Broker[]; accounts: Account[]; asOf: string }>('/fleet'),
  health: () => apiFetch<{ status: string; asOf: string }>('/health'),
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
