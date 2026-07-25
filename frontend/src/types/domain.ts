/**
 * Core domain types — thin subset of TRACK-B canonical contracts.
 * Not exhaustive; expanded on-demand. Field names match `world.v1.json` exactly.
 */

export type Session = 'London' | 'Lull' | 'NewYork' | 'NY_PM' | 'Asia' | 'Outside';
export type Structure = 'BOS' | 'CHoCH';
export type Direction = 'Long' | 'Short';
export type MarketState =
  | 'BullExpand'
  | 'BullCompress'
  | 'BullChop'
  | 'BearExpand'
  | 'BearCompress'
  | 'BearChop';
export type Lane = 'live' | 'demo' | 'ghost' | 'experimental' | 'research_forward';
import type { DataProvenance } from './provenance';

export type ValidationBadge = 'NATIVE' | 'RESCORE' | 'BASE' | 'INSUFFICIENT' | 'NOT_TESTED';
export type EligibilityAction = 'LABEL' | 'STATE_ONLY' | 'DIRECTION_AWARE' | 'DISABLE';

export interface Cohort {
  session: Session;
  structure: Structure;
  direction: Direction;
  instrument: string;
}

export interface PolicyCell {
  policyCellKey: string;
  cohort: Cohort;
  marketState: MarketState;
  eligibility: {
    action: EligibilityAction;
    mode: 'Inherit' | 'Custom' | 'Block' | 'Research';
    resolvedAllowed: boolean;
  };
  target: { rr: number; source: 'cell' | 'cohortBase' | 'inherited' };
  risk: { pct: number; source: 'cell' | 'cohortBase' | 'inherited' };
  evidence: {
    badge: ValidationBadge;
    sampleSize: number;
    expectancyR: number;
    winRate: number;
    profitFactor: number;
    confidence: number;
    inSampleCaveat: string;
  };
  recommendationStatus:
    | 'none'
    | 'new'
    | 'accepted'
    | 'drafted'
    | 'validating'
    | 'promoted'
    | 'rejected'
    | 'deployed';
  provenanceRecommendationId?: string | null;
  /**
   * UI-0 — where this cell came from. `synthesized` cells are generated in the
   * Control Tower for design/demo and carry NO research evidence; they must never
   * be rendered as strategy-engine output.
   */
  provenance?: DataProvenance;
  draftDelta?: {
    field: 'target' | 'risk' | 'eligibility';
    before: unknown;
    after: unknown;
  } | null;
}

export interface Deployment {
  deploymentId: string;
  packageHash: string;
  accountId: string;
  pair: string;
  lane: Lane;
  status: 'Armed' | 'Waiting' | 'InTrade' | 'Paused' | 'Locked' | 'Error';
  executionMode: 'mock' | 'demo' | 'live';
  liveEnabled: boolean;
  lastAction: string;
  riskState: {
    dailyPl: number;
    floatingPl: number;
    riskTodayPct: number;
    ddBufferPct: number;
    openOrders: number;
    openTrades: number;
  };
  pinnedInFlight: Record<string, string>;
}

export interface Broker {
  brokerId: string;
  adapterType: string;
  venue: string;
  status: 'connected' | 'disconnected' | 'degraded';
  lastHeartbeat: string;
  lastReconcileAt: string;
  capabilities: { trailingStop: boolean; minLot: number; lotStep: number };
}

export interface Account {
  accountId: string;
  brokerId: string;
  type: 'funded' | 'demo' | 'live';
  baseCurrency: string;
  timezone: string;
  balance: number;
  equity: number;
  fundedRules: null | {
    accountSize: number;
    dailyLossLimit: number;
    maxDrawdown: number;
    ddType: string;
    trailingAnchor: string;
    profitTarget: number;
    minTradingDays: number;
    consistencyRule: unknown;
    newsRestrictions: boolean;
    weekendHolding: boolean;
    maxLot: number;
    maxRiskExposure: number;
    accountTimezone: string;
    dailyResetTime: string;
  };
}

export interface Package {
  packageId: string;
  version: number;
  packageHash: string;
  contractVersion: string;
  label: string;
  stage: string;
  status: 'active' | 'superseded' | 'draft' | 'archived';
  domain: string;
  instruments: string[];
  componentVersions: Record<string, string>;
  researchLineage: Record<string, string>;
  validation: {
    badge: ValidationBadge;
    nDecided: number;
    portfolioDeltas: { netR: number; drawdown: number; stability: number };
    validationId: string | null;
  };
  parentVersion: number | null;
  createdAt: string;
  promotedAt: string | null;
  promotedBy: string | null;
  supersededBy: number | null;
  notes: string;
  policy: { matrices: Record<string, PolicyMatrixData> };
}

export interface PolicyMatrixData {
  instrument: string;
  cohortAxis: {
    sessions: Session[];
    structures: Structure[];
    directions: Direction[];
  };
  cohortBaseTargets: Record<string, number>;
  cells: Record<string, PolicyCell>;
  /** UI-0 — worst-case provenance across the grid. */
  provenance?: DataProvenance;
  /** UI-0 — how many cells were generated locally rather than supplied. */
  synthesizedCells?: number;
  /** UI-0 — how many cells came from a real source (fixture today). */
  sourcedCells?: number;
}

export interface LiveTrade {
  tradeId: string;
  clientOrderId: string;
  brokerOrderId: string;
  deploymentId: string;
  packageHash: string;
  scenarioKey: string;
  lane: Lane;
  decisionId: string | null;
  originalPlan: { entry: number; sl: number; tp: number; rr: number; risk: number };
  entry: number;
  sl: number;
  tp: number;
  size: number;
  riskPct: number;
  state: 'pending' | 'open' | 'managing' | 'closed';
  currentR: number | null;
  floatingPl: number;
  protectionStatus: string;
  sessionEntered: Session;
  openedAt: string;
  closedAt: string | null;
  realizedR: number | null;
  closePrice: number | null;
  management: Array<{
    at: string;
    type: string;
    before: Record<string, number>;
    after: Record<string, number>;
    actor: string;
    reason: string;
  }>;
}

export interface GhostTrade {
  ghostTradeId: string;
  ghostModelId: string;
  sourceSignalId: string;
  deploymentId: string;
  packageHash: string;
  scenarioKey: string;
  lane: 'ghost';
  ghostOutcome: 'WIN' | 'LOSS' | 'BE';
  ghostR: number;
  ghostMae: number;
  ghostMfe: number;
  ghostFillDelayCandles: number;
  note: string;
}

export interface BlockedIntent {
  blockedIntentId: string;
  scenarioKey: string;
  packageHash: string;
  lane: Lane;
  blockReason:
    | 'COHORT_DISABLED'
    | 'PM_POLICY_BLOCK'
    | 'REGIME_BLOCKED'
    | 'STATE_BLOCKED'
    | 'PROTECTION_VETO';
  ruleFired: string;
  decisionId: string | null;
  marketStateRef: string;
  at: string;
}

export interface DecisionChain {
  decisionId: string;
  scenarioKey: string;
  packageHash: string;
  terminal: string;
  nodes: Array<{
    node: string;
    status: 'pass' | 'fail' | 'skip';
    value: string;
    why: string;
    ref: string;
    at: string;
  }>;
  downstreamInfluence: { contributesTo: string[] };
}

export interface Recommendation {
  recommendationId: string;
  scenarioKey: string;
  proposedChange: { field: string; from: number | string; to: number | string };
  status: 'new' | 'validating' | 'accepted' | 'deployed' | 'rejected';
  createdAt: string;
  actor: string;
  lineage: { draftId: string | null; resultingPackageVersion: number | null };
  evidence: {
    researchVersion: string;
    supportingStats: Record<string, number>;
    sampleSize: number;
    confidence: number;
    nativeValidation: {
      badge: ValidationBadge;
      portfolioDeltas: { netR: number; drawdown: number; stability: number };
    };
    evidenceSummary: string;
    sourceLinks: string[];
    explainabilityText: string;
    inSampleCaveat: string;
  };
}

export interface BrokerHealth {
  brokerId: string;
  latencyMs: number;
  reconnects: number;
  executionSpeedMs: number;
  orderRejects: number;
  spread: number;
  slippage: number;
  heartbeatAgeMs: number;
  reconciliation: { clean: boolean; diff: unknown };
  syncStatus: string;
  timeline: Array<{ at: string; code: string; detail: string; resolved: boolean }>;
}

export interface EdgeMonitor {
  instrument: string;
  asOf: string;
  metrics: {
    expectancyR: number;
    winRate: number;
    edgeDrift: string;
    distributionDrift: string;
    featureDrift: string;
    policyHealth: string;
    researchVsLive: string;
    ghostVsLive: string;
    forwardTestHealth: string;
    recommendationGeneration: number;
    operatorConfidence: string;
    futureCandidates: string[];
  };
}

export interface SystemConfidence {
  score: number;
  band: 'healthy' | 'caution' | 'degraded' | 'critical';
  worstSignal: string;
  explanation: string;
  updatedAt: string;
  signals: Array<{
    key: string;
    state: 'ok' | 'warn' | 'fail' | 'unknown';
    weight: number;
    value: string;
    message: string;
    since: string;
  }>;
}

export interface EventEntry {
  eventId: string;
  seq: number;
  category: string;
  code: string;
  humanExplanation: string;
  scenarioKey: string | null;
  packageHash: string;
  who: string;
  causedBy: string;
  before: Record<string, unknown> | null;
  after: Record<string, unknown> | null;
  at: string;
}

export interface Draft {
  draftId: string;
  baseVersion: number;
  status: string;
  owner: string;
  lockedBy: string;
  createdAt: string;
  changes: Array<{
    policyCellKey: string;
    before: Record<string, unknown>;
    after: Record<string, unknown>;
    sourceRecommendationId: string | null;
  }>;
  validation: unknown;
}

export interface WorldFixture {
  meta: { contractVersion: string; fixtureVersion: string; asOf: string; note: string };
  vocabulary: {
    domain: string;
    sessions: Session[];
    structures: Structure[];
    directions: Direction[];
    marketStates: MarketState[];
    rrLadder: number[];
  };
  operators: Array<Record<string, unknown>>;
  brokers: Broker[];
  accounts: Account[];
  marketStateSnapshots: Array<{
    instrument: string;
    state: MarketState;
    confidence: number;
    confirmed: boolean;
    stateKnownAt: string;
    shiftedDays: number;
    source: string;
    modelVersion: string;
    components: Record<string, unknown>;
    asOf: string;
  }>;
  packages: Package[];
  drafts: Draft[];
  nativeValidations: unknown[];
  recommendations: Recommendation[];
  deployments: Deployment[];
  signals: Array<Record<string, unknown>>;
  decisionChains: DecisionChain[];
  liveTrades: LiveTrade[];
  ghostTrades: GhostTrade[];
  blockedIntents: BlockedIntent[];
  brokerHealth: BrokerHealth[];
  edgeMonitor: EdgeMonitor;
  systemConfidence: SystemConfidence;
  packageComparisons: PackageComparison[];
  replaySessions: ReplaySession[];
  events: EventEntry[];
}

export interface ReplaySession {
  replayId: string;
  scope: { scenarioKeys: string[]; pair: string; deploymentId: string };
  window: { start: string; end: string };
  speed: number;
  packageHashAtTime: string;
  consumedDataHashes: Record<string, string>;
  clock: string;
  createdBy: string;
  createdAt: string;
}

export interface PackageComparison {
  comparisonId: string;
  a: { packageId: string; version: number; packageHash: string };
  b: { packageId: string; version: number; packageHash: string };
  cellDiffs: Array<{
    scenarioKey: string;
    eligibility: { from: string; to: string } | null;
    target: { from: number; to: number } | null;
    risk: { from: number; to: number } | null;
    recommendationId: string | null;
    evidenceDelta: {
      expectancyR: { from: number; to: number };
      winRate: { from: number; to: number };
      badge: { from: string; to: string };
    };
  }>;
  componentVersionDiffs: Array<{ component: string; from: string; to: string }>;
  validationComparison: {
    a: { badge: string; portfolioDeltas: Record<string, number> };
    b: { badge: string; portfolioDeltas: Record<string, number> };
  };
  projectedImprovement: {
    netR: number;
    drawdown: number;
    stability: number;
    nDecided: number;
    inSampleCaveat: string;
  };
  deploymentHistoryDelta: {
    a: { lanes: string[]; accounts: number };
    b: { lanes: string[]; accounts: number };
  };
  generatedAt: string;
}

export type Environment = 'Local' | 'VPS' | 'Demo' | 'Live' | 'Experimental';

/**
 * DeploymentManifest (Track B / V2.1) — the immutable definition of a running
 * deployment. WRAPS a Strategy Package; never replaces it. Enables clone /
 * export / restore / redeploy with complete fidelity. In v1 this is DERIVED on
 * the client from the fixture (deployment + package + account + broker + flags)
 * — no new fixture data, no invented contract.
 */
export interface DeploymentManifest {
  manifestId: string;
  manifestHash: string;
  derived: boolean;
  createdAt: string;
  createdBy: string;
  clonedFrom: string | null;
  strategyPackage: { packageId: string; version: number; packageHash: string };
  broker: string;
  account: string;
  pair: string;
  lane: Lane;
  environment: Environment;
  marketDataVersion: string;
  replayDataVersion: string;
  enabledModules: string[];
  featureFlags: Record<string, boolean>;
  metadata: { label: string; notes: string; tags: string[] };
}

export type FeatureFlag =
  | 'ghostTrading'
  | 'replay'
  | 'edgeMonitor'
  | 'researchForward'
  | 'brokerHealth'
  | 'notifications'
  | 'analytics'
  | 'newsIntegration'
  | 'experimentalFeatures'
  | 'aiRecommendations'
  | 'commandPalette'
  | 'whyWorkflow'
  | 'decisionChainInspector'
  | 'accountsProtection'
  | 'charts'
  | 'versionHistory'
  | 'packageComparison';
