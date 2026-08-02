import { useEffect, useMemo } from 'react';
import { useQuery, useSuspenseQueries, useSuspenseQuery, keepPreviousData } from '@tanstack/react-query';
import { deriveOrderBlocks, type OrderBlock } from '@/lib/orderBlocks';
import { deriveFairValueGaps, type FairValueGap } from '@/lib/fairValueGaps';
import { deriveLiquidityPools, type LiquidityPool } from '@/lib/liquidity';
import { deriveMarketStructure, type MarketStructure } from '@/lib/marketStructure';
import { type Candle } from '@/lib/chartData';
import { api, QK, type BackendHealth, type BrokerReconciliation, type OperatorPreferences, type RuntimeHealth, type StrategyEvaluation, type SchedulerStatus, type MarketSnapshot, type RiskLimits, type MarketCandles, type FleetProvenance, type NodeOperationalView, type AccountOperationalView, type PositionOperationalView, type OrderOperationalView, type RecommendationOperationalView, type Capability, type LedgerAnalytics, type OperatorIdentity } from '@/lib/api';
import {
  authoritativeOnly,
  admissionRejectionCopy,
  classify,
  UNAVAILABLE_DETAIL,
  TRADES_UNAVAILABLE_DETAIL,
  PACKAGES_UNAVAILABLE_DETAIL,
  RECOMMENDATIONS_UNAVAILABLE_DETAIL,
  type OperationalStatus,
  type ProvenancedRecord,
} from '@/lib/operationalProvenance';
import {
  authoritativeNodesOnly,
  classifyNodeObservation,
  NODE_ABSENT_DETAIL,
  type NodeObservationStatus,
  type NodeRecord,
} from '@/lib/nodeProvenance';
import { isUsable } from '@/lib/capability';
import { expandMatrix } from '@/lib/matrixExpand';
import { queryClient } from '@/lib/queryClient';
import { applyEvents, resetLastAppliedSeq, seedLastAppliedSeq } from '@/lib/realtime';
import { useShellStore } from '@/store/shellStore';
import type {
  Deployment,
  LiveTrade,
  GhostTrade,
  BlockedIntent,
  Broker,
  Account,
  BrokerHealth,
  DecisionChain,
  Recommendation,
  Package,
  EdgeMonitor,
  SystemConfidence,
  EventEntry,
  MarketState,
  Draft,
  FeatureFlag,
  PolicyMatrixData,
  WorldFixture,
  DeploymentManifest,
  ReplaySession,
  PackageComparison,
} from '@/types/domain';

/**
/**
 * M-WORLD-ORDINARY-1 — RENAMED, and restricted to `/dev/*` routes.
 *
 * This was `useWorld()`, and the application SHELL called it on every ordinary
 * page: `useOperator()` read `world.operators[0]` to render a name in the
 * chrome, so every route fetched all 22 fixture collections in order to display
 * an authored person. The name gave no hint that it served authored records.
 *
 * The name now says what it is, the endpoint is `/api/dev/fixture-world`, and a
 * source guard rejects any import of it outside `views/dev/`.
 */
/**
 * The UI's own domain taxonomy. Not system state, and not an observation —
 * these are the labels this application uses to talk about FX structure, so it
 * owns them. Served from `world.vocabulary` before M-WORLD-ORDINARY-1, which
 * meant Settings could not render without the authored fixture file.
 */
export const DOMAIN_VOCABULARY = {
  domain: 'FX',
  sessions: ['London', 'Lull', 'NewYork', 'NY_PM', 'Asia', 'Outside'],
  structures: ['BOS', 'CHoCH'],
  directions: ['Long', 'Short'],
  marketStates: ['BullExpand', 'BullCompress', 'BullChop',
                 'BearExpand', 'BearCompress', 'BearChop'],
  rrLadder: [0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.25, 1.5, 1.75, 2.0, 2.25, 2.5,
             2.75, 3.0, 3.25, 3.5, 3.75, 4.0, 4.25, 4.5, 4.75, 5.0],
} as const;

function useFixtureWorldPreview(): WorldFixture {
  const { data } = useSuspenseQuery({
    queryKey: QK.world,
    queryFn: api.fixtureWorldPreview,
    staleTime: Infinity,
  });
  return data;
}

/**
 * M-WORLD-ORDINARY-1 — `useRepository()` is GONE.
 *
 * It returned the whole fixture world to any caller. Its last consumer,
 * `TradeRenderer`, destructured `world` and never read a field from it —
 * M-TRADES-1 had already emptied every collection it used — so an ordinary
 * inspector was fetching 22 authored collections to use none of them.
 */

/**
 * M-FLEET-2 — the AUTHORITATIVE operational fleet.
 *
 * This hook used to return `/api/fleet`: authored fixture deployments, brokers
 * and accounts. Thirteen surfaces consumed it, so every one of them showed
 * invented financial entities on an ordinary operator route.
 *
 * It now reads the operational projection and passes it through the single
 * provenance gate in `lib/operationalProvenance`. Only `live_mt5` records
 * survive; `mock-fixture` (the development default) and anything unrecognised
 * are dropped. Under the mock adapter that means this hook returns NOTHING and
 * reports `unavailable` — which is the honest answer, and is why the operator
 * UI is deliberately empty in development.
 *
 * `deployments` is permanently empty: there is no authoritative deployment
 * concept. `/api/operations/nodes` projects execution NODES, not deployments,
 * and inventing deployments from nodes would recreate the fabrication this
 * milestone removes. Surfaces render an unavailable state instead.
 */
export function useOperationalFleet(): {
  nodes: NodeOperationalView[];
  accounts: AccountOperationalView[];
  deployments: Deployment[];
  /** BROKER admission status. Governs accounts only. */
  status: OperationalStatus;
  detail: string;
  /** NODE admission status. Independent of `status` in both directions. */
  nodeStatus: NodeObservationStatus;
  nodeDetail: string;
  /** M-ACTIVATE-READINESS-1 — why genuine records were refused, in operator
   *  words. Empty unless a record was rejected for a NAMED reason; a record
   *  dropped for provenance alone contributes nothing here, because "the mock
   *  adapter is running" is not news to anyone. */
  rejections: string[];
} {
  const [{ data: nodesRes }, { data: acctRes }] = useSuspenseQueries({
    queries: [
      { queryKey: QK.operationsNodes, queryFn: api.operationsNodes },
      { queryKey: QK.operationsAccounts, queryFn: api.operationsAccounts },
    ],
  });
  return useMemo(() => {
    const rawNodes = nodesRes?.nodes ?? [];
    const rawAccounts = acctRes?.accounts ?? [];
    // M-NODE-READ-1 — TWO GATES, DELIBERATELY NOT ONE.
    //
    // Nodes were passed through `authoritativeOnly`, the BROKER gate, which
    // does not admit `node-telemetry` — correctly, since a heartbeat is not an
    // account. The consequence was that a genuinely reporting node was dropped
    // and every fleet surface reported "no authoritative source".
    //
    // Each collection now goes through the gate that matches its authority, and
    // the two statuses are reported separately. They must stay separate: a node
    // reporting perfectly with an unreadable terminal is a real and important
    // state, and one combined status could not express it.
    const nodes = authoritativeNodesOnly(rawNodes as unknown as NodeRecord[]) as unknown as NodeOperationalView[];
    const accounts = authoritativeOnly(rawAccounts as unknown as ProvenancedRecord[]) as unknown as AccountOperationalView[];
    const status = classify(accounts as unknown as ProvenancedRecord[], {
      sourceAnswered: Boolean(acctRes),
      rejectedCount: rawAccounts.length - accounts.length,
    });
    const nodeStatus = classifyNodeObservation(nodes as unknown as NodeRecord[], {
      sourceAnswered: Boolean(nodesRes),
    });
    return {
      nodes,
      accounts,
      deployments: [],          // no authoritative deployment concept exists
      status,
      detail: status === 'available' || status === 'stale' ? '' : UNAVAILABLE_DETAIL,
      nodeStatus,
      nodeDetail: nodeStatus === 'absent' ? NODE_ABSENT_DETAIL : '',
      rejections: admissionRejectionCopy(rawAccounts as unknown as ProvenancedRecord[]),
    };
  }, [nodesRes, acctRes]);
}

/**
 * M-FLEET-2 — the CONFIGURED instrument universe (`/api/instruments`).
 *
 * Navigation needs instrument identity; it previously took that from fixture
 * deployments, so the pair menu was derived from invented entities. This is
 * genuine configuration and asserts nothing operational about any symbol.
 */
/**
 * M-EVENTS-1 — DEVELOPMENT FIXTURE EVENTS ONLY. NOT THE AUDIT STREAM.
 * The three authored seeds that used to be merged into `/api/events`.
 */
export function useFixtureEventsPreview(): { events: EventEntry[]; detail: string } {
  const { data } = useSuspenseQuery({
    queryKey: QK.fixtureEventsPreview,
    queryFn: api.fixtureEventsPreview,
    staleTime: Infinity,
  });
  return { events: data?.events ?? [], detail: data?.detail ?? '' };
}

/** M-TRADES-2 — the ONLY analytics source. No client-side metric derivation. */
export function useLedgerAnalytics(): LedgerAnalytics {
  const { data } = useSuspenseQuery({
    queryKey: QK.ledgerAnalytics,
    queryFn: api.ledgerAnalytics,
    staleTime: Infinity,
  });
  return data;
}

export function useConfiguredInstruments(): { symbols: string[]; configured: boolean } {
  const { data } = useSuspenseQuery({
    queryKey: QK.instruments,
    queryFn: api.instruments,
    staleTime: Infinity,
  });
  return useMemo(
    () => ({
      symbols: (data?.instruments ?? []).map((i) => i.symbol),
      configured: Boolean(data),
    }),
    [data]
  );
}

/**
 * M-FLEET-2 — DEVELOPMENT FIXTURE PREVIEW ONLY. NOT FOR ORDINARY SURFACES.
 *
 * Returns the authored fixture fleet from `/api/fleet`. Every ordinary operator
 * component is forbidden from importing this (enforced by a source guard); it
 * exists for tests and the dedicated development preview route. In production
 * M-ENV-1 never loads the fixture world, so the endpoint answers 501.
 */
export function useFixtureFleetPreview(): {
  deployments: Deployment[];
  brokers: Broker[];
  accounts: Account[];
  available: boolean;
  provenance: FleetProvenance;
  provenanceDetail: string;
  asOf: string;
} {
  const { data: fleet } = useSuspenseQuery({
    queryKey: QK.fixtureFleetPreview,
    queryFn: api.fixtureFleetPreview,
    staleTime: Infinity,
  });
  return useMemo(
    () => ({
      deployments: fleet.deployments,
      brokers: fleet.brokers,
      accounts: fleet.accounts,
      available: fleet.available,
      provenance: fleet.provenance,
      provenanceDetail: fleet.detail,
      asOf: fleet.asOf ?? '',
    }),
    [fleet]
  );
}

/**
 * Single deployment read through `/api/fleet` composition (migrated off
 * `/api/world`, Phase 4 Step 4). Reads the runtime-overlaid deployment list —
 * so the inspector reflects live runtime status — via the shared `QK.fleet`
 * key, which deployment commands already invalidate. Signature unchanged.
 */
export function useDeployment(_id: string): Deployment | undefined {
  // M-FLEET-2: there is no authoritative deployment record. This previously
  // resolved against the fixture fleet, so any inspector opened on an ordinary
  // route rendered an invented deployment. It now resolves to nothing until a
  // real deployment concept exists, and callers render an unavailable state.
  return undefined;
}

/**
 * Derive the immutable Deployment Manifest for a deployment from real fixture
 * data (deployment + package it wraps + account + broker + active flags).
 * No new fixture data, no invented contract — the manifest is a view over the
 * frozen world. When a backend manifest store lands, point this at `/api/...`.
 */
export function useDeploymentManifest(_deploymentId: string): DeploymentManifest | undefined {
  // M-FLEET-2: this synthesised a "deployment manifest" from the FIXTURE world's
  // deployments, packages, accounts and brokers — reading WORLD directly, so it
  // bypassed /api/fleet entirely and survived the endpoint-level severing. Every
  // field it produced (broker, account, environment "Live", manifest hash) was
  // derived from invented records. There is no authoritative manifest source, so
  // it resolves to nothing and its consumers render an unavailable state.
  return undefined;
}

export function usePairWorkspace(pairId: string) {
  // M-WORLD-ORDINARY-1: the `useWorld()` call here was DEAD — every collection
  // it once read had already been emptied by M-TRADES-1, M-FLEET-2, M-NODE-TEL-1
  // and M-EVENTS-1, so an ordinary pair route fetched the whole fixture world
  // and discarded all of it.
  return useMemo(() => {
    // M-TRADES-1: fixture trades, ghosts and blocked intents no longer reach a
    // pair workspace on an ordinary route.
    const trades: LiveTrade[] = [];
    const ghosts: GhostTrade[] = [];
    const blocked: BlockedIntent[] = [];
    // M-FLEET-2: fixture deployments no longer reach a pair workspace.
    const deployments: Deployment[] = [];
    // M-NODE-TEL-1: no authoritative market state exists for a pair workspace.
    const marketState = undefined;
    // M-REC-1: fixture decision chains no longer reach a pair workspace.
    const decisions: DecisionChain[] = [];
    // M-EVENTS-1: the pair workspace no longer surfaces fixture events.
    const events: EventEntry[] = [];
    return { pair: pairId, trades, ghosts, blocked, deployments, marketState, decisions, events };
  }, [pairId]);
}

// Module-level cache of expanded matrices, keyed by instrument@version. Replaces
// the old per-world WeakMap now that the source comes from `/api/policy/...`.
const matrixCache = new Map<string, PolicyMatrixData>();

/**
 * Policy matrix via `/api/policy/{instrument}/matrix` (migrated off `/api/world`,
 * Phase 4.5). The endpoint returns the representative cells; `expandMatrix`
 * builds the full 24×6 grid client-side (unchanged), memoised by instrument@version.
 */
export function usePolicyMatrix(_instrument: string, _packageVersion?: number): PolicyMatrixData {
  // M-PKG-1 / M-REC-1: `/api/policy/{i}/matrix` derives its cells from
  // `WORLD["packages"]`, so every eligibility verdict, sample size and p-value
  // in the 144-cell grid was authored. M-PKG-1 removed the views that displayed
  // the grid but left this hook fetching it; the recommendation-evidence guard
  // caught the remainder. No package registry exists, so there is no matrix.
  return { cells: {}, synthesizedCells: 0 } as PolicyMatrixData;
}

/**
 * Trades read through `/api/trades` (migrated off `/api/world`, Phase 4
 * Step 2). Pair/lane filtering happens server-side with the exact semantics
 * the old client-side selector had (lane filters live + blocked only — ghost
 * is itself a lane). Trade/order commands invalidate `QK.trades`.
 */
/**
 * M-TRADES-1 — the AUTHORITATIVE operational trade surface.
 *
 * `useTrades` previously returned `/api/trades`: two authored live trades with
 * entry/SL/TP and R values, three ghost trades with outcomes, five blocked
 * intents. Nine surfaces consumed it, including the chart overlay and the
 * analytics engine.
 *
 * It now reads open POSITIONS and open ORDERS from the operational projection
 * through the same provenance gate M-FLEET-2 established. The laundering risk
 * is live here: under the mock adapter `/api/operations/positions` returns a
 * position complete with `entryPrice` and `currentPrice`, stamped
 * `mock-fixture`. It is rejected, and this reports `unavailable`.
 *
 * Positions and orders are kept as SEPARATE collections. They are different
 * domain facts and merging them into one "trades" list would blur an open
 * exposure with a resting instruction.
 */
export function useOperationalTrades(pair?: string): {
  positions: PositionOperationalView[];
  orders: OrderOperationalView[];
  status: OperationalStatus;
  detail: string;
} {
  const [{ data: posRes }, { data: ordRes }] = useSuspenseQueries({
    queries: [
      { queryKey: QK.operationsPositions, queryFn: api.operationsPositions },
      { queryKey: QK.operationsOrders, queryFn: api.operationsOrders },
    ],
  });
  return useMemo(() => {
    const rawPos = posRes?.positions ?? [];
    const rawOrd = ordRes?.orders ?? [];
    const keep = (rs: unknown[]) => authoritativeOnly(rs as ProvenancedRecord[]);
    let positions = keep(rawPos) as unknown as PositionOperationalView[];
    let orders = keep(rawOrd) as unknown as OrderOperationalView[];
    if (pair) {
      positions = positions.filter((p) => p.instrument === pair);
      orders = orders.filter((o) => o.instrument === pair);
    }
    const rejected = (rawPos.length - keep(rawPos).length) + (rawOrd.length - keep(rawOrd).length);
    const status = classify([...positions, ...orders] as unknown as ProvenancedRecord[], {
      sourceAnswered: Boolean(posRes || ordRes),
      rejectedCount: rejected,
    });
    return {
      positions,
      orders,
      status,
      detail: status === 'available' || status === 'stale' ? '' : TRADES_UNAVAILABLE_DETAIL,
    };
  }, [posRes, ordRes, pair]);
}

/**
 * M-TRADES-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. NOT FOR ORDINARY SURFACES.
 * Guarded by a repository-wide source check; reachable from `/dev/fixture-trades`.
 */
export function useFixtureTradesPreview(scope?: { pair?: string; lane?: string }): {
  live: LiveTrade[];
  ghost: GhostTrade[];
  blocked: BlockedIntent[];
} {
  const { data } = useSuspenseQuery({
    queryKey: QK.fixtureTradesPreview,
    queryFn: () => api.fixtureTradesPreview(scope?.pair, scope?.lane),
    staleTime: Infinity,
  });
  return data;
}

/** Broker health via `/api/broker-health` (migrated off `/api/world`, Step 4). */
export function useBrokerHealth(): { available: boolean; detail: string } {
  // M-HONESTY-FINAL: returned fixture broker records with invented latency and
  // health. No broker-health telemetry source exists.
  const { data } = useSuspenseQuery({
    queryKey: QK.brokerHealth,
    queryFn: api.brokerHealth,
    staleTime: Infinity,
  });
  return {
    available: Boolean(data?.available),
    detail: data?.detail ?? 'No broker-health telemetry source exists.',
  };
}

/** Edge monitor via `/api/edge-monitor` (migrated off `/api/world`, Step 4). */
export function useEdgeMonitor(): EdgeMonitor {
  const { data } = useSuspenseQuery({
    queryKey: QK.edgeMonitor,
    queryFn: api.edgeMonitor,
    staleTime: Infinity,
  });
  return data;
}

/** Runtime-layer health via `/api/runtime/health` (distinct from broker health). */
export function useRuntimeHealth(): RuntimeHealth {
  const { data } = useSuspenseQuery({
    queryKey: QK.runtimeHealth,
    queryFn: api.runtimeHealth,
    staleTime: Infinity,
  });
  return data;
}

/** Last broker reconciliation result via `/api/broker/reconciliation` (Phase 7). */
export function useBrokerReconciliation(): BrokerReconciliation {
  const { data } = useSuspenseQuery({
    queryKey: QK.brokerReconciliation,
    queryFn: api.brokerReconciliation,
    staleTime: Infinity,
  });
  return data;
}

/** Last strategy evaluation via `/api/strategy/decisions` (Phase 9, read-only). */
/**
 * M-WORLD-ORDINARY-1 — the strategy engine's inputs were all authored.
 *
 * `_strategy_env` injected fixture DEPLOYMENTS, a policy matrix derived from
 * fixture PACKAGES, and fixture RECOMMENDATIONS. The engine is real code, but
 * every input was invented, so SystemView rendered a synthetic evaluation of a
 * fabricated world as the live engine's operational record: a decision count,
 * a fired/held/rejected split, an evaluation latency and a `strategyHealthy`
 * dot.
 *
 * The backend now reports `available: false` with a reason rather than
 * evaluating nothing and returning zeros — `0 fired · 0 held` with a latency
 * asserts that an evaluation ran, which is a claim about the strategy's
 * behaviour that nothing supports.
 */
export function useStrategyEvaluation(): StrategyEvaluation & {
  available: boolean; detail: string | null;
} {
  const { data } = useSuspenseQuery({
    queryKey: QK.strategyDecisions,
    queryFn: api.strategyDecisions,
    staleTime: Infinity,
  });
  return data as StrategyEvaluation & { available: boolean; detail: string | null };
}

/** Scheduler status via `/api/scheduler/status` (Phase 10, read-only). */
export function useSchedulerStatus(): SchedulerStatus {
  const { data } = useSuspenseQuery({
    queryKey: QK.schedulerStatus,
    queryFn: api.schedulerStatus,
    staleTime: Infinity,
  });
  return data;
}

/** Current immutable market snapshot via `/api/market-data/snapshot` (Phase 11, read-only). */
export function useMarketSnapshot(): MarketSnapshot {
  const { data } = useSuspenseQuery({
    queryKey: QK.marketDataSnapshot,
    queryFn: () => api.marketDataSnapshot(),
    staleTime: Infinity,
  });
  return data;
}

/**
 * Candle series from the Market Data Engine via `/api/market-data/candles` (Phase 16).
 * The SINGLE candle source for every chart: `provider='replay'` for replay, undefined
 * for the active (live) provider. Non-suspending so the chart area loads independently.
 */
/**
 * Backend PROCESS health (UI-0). Deliberately NON-suspending and never retried into
 * a throw: the shell renders a truthful "backend unreachable" indicator instead of
 * being torn down. Never falls back to fixture values — `undefined` means unknown.
 */
export function useBackendHealth(): { health: BackendHealth | undefined; failed: boolean } {
  const { data, isError } = useQuery({
    queryKey: QK.health,
    queryFn: () => api.health(),
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: false,
  });
  return { health: data, failed: isError };
}

export function useMarketCandles(
  symbol: string,
  opts: { provider?: string; count?: number; endISO?: string; timeframe?: string; live?: boolean } = {}
): MarketCandles | undefined {
  const { provider, count = 220, endISO, timeframe = 'M15', live = false } = opts;
  const { data } = useQuery({
    queryKey: QK.marketCandles(symbol, provider, count, endISO, timeframe),
    queryFn: async () => {
      const r = await api.marketCandles({ symbol, provider, count, end: endISO, timeframe });
      const meta = r as { requestId?: string; source?: string; cacheHit?: boolean; fellBack?: boolean };
      console.debug('[md] response', {
        requestId: meta.requestId, tf: timeframe, source: meta.source,
        cacheHit: meta.cacheHit, fellBack: meta.fellBack, count: r.candles.length,
        first: r.candles[0]?.time, last: r.candles[r.candles.length - 1]?.time,
      });
      return r;
    },
    // Keep the prior timeframe's bars on screen while the new key fetches. Without
    // this, switching TF changed the query key → data=undefined → candles=[] →
    // ChartPanel fully UNMOUNTED and remounted (chart teardown + createChart,
    // doubled by StrictMode). That churn (a) flashed the chart black on rapid
    // switches and (b) emitted a burst of synchronous setState that starved the
    // router's startTransition-wrapped navigation. keepPreviousData keeps the
    // chart mounted so the data effect swaps series in place (its reset branch).
    placeholderData: keepPreviousData,
    // Live charts poll the service (Phase 24 — polling now, WebSocket later);
    // replay/historical windows are immutable → never stale.
    staleTime: live ? 12_000 : Infinity,
    refetchInterval: live ? 15_000 : false, // 15s: within Polygon free-tier 5 req/min
  });
  return data;
}

/**
 * Order blocks for a chart (Phase 17) — the SWAP SEAM. Today it returns a deterministic
 * fixture-backed provider derived from the candle series; later, point it at the real
 * OB detector endpoint here WITHOUT touching ChartWorkspace or the OrderBlockLayer.
 */
export function useOrderBlocks(instrument: string, candles: Candle[]): OrderBlock[] {
  return useMemo(() => deriveOrderBlocks(instrument, candles), [instrument, candles]);
}

/**
 * Fair value gaps for a chart (Phase 18) — the SWAP SEAM, identical to `useOrderBlocks`.
 * Today it returns a deterministic fixture-backed provider derived from the candle
 * series; later, point it at the real FVG detector endpoint here WITHOUT touching
 * ChartWorkspace or the FairValueGapLayer.
 */
export function useFairValueGaps(instrument: string, candles: Candle[]): FairValueGap[] {
  return useMemo(() => deriveFairValueGaps(instrument, candles), [instrument, candles]);
}

/**
 * Liquidity pools for a chart (Phase 19) — the SWAP SEAM, identical to the OB/FVG hooks.
 * Today it returns a deterministic fixture-backed provider derived from the candle
 * series; later, point it at the real liquidity detector endpoint here WITHOUT touching
 * ChartWorkspace or the LiquidityLayer.
 */
export function useLiquidityPools(instrument: string, candles: Candle[]): LiquidityPool[] {
  return useMemo(() => deriveLiquidityPools(instrument, candles), [instrument, candles]);
}

/**
 * Market structure — swings + BOS/CHOCH (Phase 20) — the SWAP SEAM. ONE hook, ONE
 * provider, feeding BOTH the Swing and Structure layers. Today a deterministic
 * fixture-backed provider derived from the candle series; later, point it at the real
 * market-structure detector endpoint here WITHOUT touching ChartWorkspace or the layers.
 */
export function useMarketStructure(instrument: string, candles: Candle[]): MarketStructure {
  return useMemo(() => deriveMarketStructure(instrument, candles), [instrument, candles]);
}

/** Active risk limit set via `/api/risk/limits` (Phase 12, read-only). */
export function useRiskLimits(): RiskLimits {
  const { data } = useSuspenseQuery({
    queryKey: QK.riskLimits,
    queryFn: () => api.riskLimits(),
    staleTime: Infinity,
  });
  return data;
}

/** Accounts + deployments via `/api/fleet` composition (migrated off `/api/world`,
 *  Phase 4.5). Deployments are runtime-overlaid; single `QK.fleet` key. */
export function useAccountsProtection(): {
  accounts: AccountOperationalView[];
  status: OperationalStatus;
  detail: string;
  rejections: string[];
} {
  // M-FLEET-2: authoritative accounts only. Under the mock adapter the
  // projection's records carry `mock-fixture` provenance and the same invented
  // balances the fixture serves, so they are rejected and this reports
  // `unavailable` rather than rendering them.
  const { accounts, status, detail, rejections } = useOperationalFleet();
  return { accounts, status, detail, rejections };
}


/** Decision chain via `/api/decisions/{id}` (migrated off `/api/world`, Phase 4.5).
 *  Per-id query key; returns undefined on 404. */
export function useDecisionChain(_decisionId: string): DecisionChain | undefined {
  // M-REC-1: `/api/decisions/{id}` reads `WORLD["decisionChains"]` — authored
  // narrative chains describing reasoning that never occurred. Genuine operator
  // decisions are recorded per recommendation at
  // `/api/trade-recommendations/{id}/decisions`; there is no authoritative
  // chain-by-decision-id lookup, so this resolves to nothing.
  return undefined;
}

/**
 * M-REC-1 — recommendations from the DURABLE OPERATOR STORE.
 *
 * `useRecommendations` read `/api/recommendations`, which returns
 * `WORLD["recommendations"]`: authored proposals carrying p-values, sample
 * sizes and NATIVE validation badges. Those numbers are the most persuasive
 * fabrication in the application — a recommendation with "n=67, P=0.94" reads
 * as a research finding.
 *
 * A genuine durable store already exists (`/api/trade-recommendations`), and
 * critically it is NOT seeded from the fixture: `recommendation_store.py`
 * contains no WORLD read of any kind, so durable and fixture records have never
 * been able to mix. That is what makes this a migration rather than a removal.
 *
 * The two authorities stay separate. Nothing merges them, and no evidence field
 * is carried over — the durable record has its own contract, and a field the
 * old card expected does not become a reason to invent one.
 */
export function useRecommendations(): {
  recommendations: RecommendationOperationalView[];
  status: OperationalStatus;
  detail: string;
} {
  const { data } = useSuspenseQuery({
    queryKey: QK.tradeRecommendations,
    queryFn: () => api.tradeRecommendations({}),
    staleTime: Infinity,
  });
  return useMemo(() => {
    const recs = data?.recommendations ?? [];
    return {
      recommendations: recs,
      status: data ? (recs.length ? 'available' : 'empty') : 'unavailable',
      detail: data ? '' : RECOMMENDATIONS_UNAVAILABLE_DETAIL,
    };
  }, [data]);
}

/** M-REC-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. */
export function useFixtureRecommendationsPreview(): Recommendation[] {
  const { data } = useSuspenseQuery({
    queryKey: QK.recommendations,
    queryFn: api.recommendations,
    staleTime: Infinity,
  });
  return data;
}

export function useDrafts(): { drafts: Draft[]; status: OperationalStatus } {
  // M-REC-1: drafts came from `WORLD["drafts"]`. No durable draft store exists,
  // and transforming durable recommendations into "drafts" would invent a
  // lifecycle state the system does not implement.
  return { drafts: [], status: 'unavailable' };
}

/**
 * Events read through their own endpoint + query key (first hook migrated off
 * the monolithic `/api/world`, per the Phase-4 seam plan). The backend merges
 * frozen fixture events with appended operator events; `executeCommand`
 * invalidates `QK.events` so the Event Dock reflects every dispatched command.
 */
export function useEvents(scope?: { pair?: string }): EventEntry[] {
  const { data } = useSuspenseQuery({
    queryKey: QK.eventsFor(scope?.pair),
    queryFn: () => api.events(scope?.pair),
    staleTime: Infinity,
  });
  return data;
}

/** System confidence via `/api/system-confidence` (migrated off `/api/world`, Step 4). */
export function useSystemConfidence(): SystemConfidence {
  const { data } = useSuspenseQuery({
    queryKey: QK.systemConfidence,
    queryFn: api.systemConfidence,
    staleTime: Infinity,
  });
  return data;
}

/**
 * M-PKG-1 — the AUTHORITATIVE package registry. There isn't one.
 *
 * `/api/packages` reads `WORLD["packages"]`: authored strategy packages with
 * version numbers, promotion timestamps, component versions and policy
 * matrices. No package registry module exists anywhere in the backend — it was
 * never built. So every version number, hash and promotion date an operator
 * has ever seen on these surfaces was written by hand.
 *
 * This resolves to UNAVAILABLE, not to replacement data. Synthesising a version
 * from configuration would be the same fabrication in a new coat: a package
 * hash asserts that a specific strategy build is deployed and running, which is
 * a claim no part of this system can currently support.
 *
 * Fixture packages remain available at `useFixturePackagesPreview` for the
 * development route.
 */
export function usePackageRegistry(): { packages: Package[]; status: OperationalStatus; detail: string } {
  return { packages: [], status: 'unavailable', detail: PACKAGES_UNAVAILABLE_DETAIL };
}

/** M-PKG-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. NOT AN OPERATIONAL SOURCE. */
export function useFixturePackagesPreview(): Package[] {
  const { data } = useSuspenseQuery({
    queryKey: QK.packagesList,
    queryFn: api.packages,
    staleTime: Infinity,
  });
  return data;
}

export function useReplaySessions(): ReplaySession[] {
  return useFixtureWorldPreview().replaySessions;
}

export function useReplaySessionForPair(pair: string): ReplaySession | undefined {
  const world = useFixtureWorldPreview();
  return world.replaySessions.find((r) => r.scope.pair === pair) ?? world.replaySessions[0];
}

/**
 * M-PKG-1: package comparisons were fixture diffs between authored packages —
 * cell-level changes that never happened between builds that never existed.
 */
export function usePackageComparisons(): { comparisons: PackageComparison[]; status: OperationalStatus } {
  return { comparisons: [], status: 'unavailable' };
}

/** M-PKG-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. */
export function useFixturePackageComparisonsPreview(): PackageComparison[] {
  return useFixtureWorldPreview().packageComparisons;
}

export function useDeploymentsForPackage(packageHash: string): Deployment[] {
  // M-FLEET-2: authoritative deployments only — of which there are none, so
  // this is empty rather than a list of fixture deployments.
  void packageHash;
  return useOperationalFleet().deployments;
}

/**
 * M-PKG-1: the "active package" is the sharpest claim on these surfaces — it
 * asserts a specific strategy build is deployed and governing decisions right
 * now. No registry exists to support it, so it resolves to undefined and every
 * consumer renders an explicit unavailable state.
 */
export function useActivePackage(): Package | undefined {
  return undefined;
}

/** M-PKG-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. */
export function useFixtureActivePackagePreview(): Package {
  const { data } = useSuspenseQuery({
    queryKey: QK.activePackage,
    queryFn: api.activePackage,
    staleTime: Infinity,
  });
  return data;
}

/**
 * M-NODE-TEL-1 — market state has NO authoritative source.
 *
 * This returned `WORLD.marketStateSnapshots`: a single authored record claiming
 * `BullExpand` at 96% confidence, confirmed, from model `regime@2.3.0`. Every
 * part of that is invented — the regime, the confidence, the model identity and
 * the timestamp — and it drove the shell badge, the pair header, chart
 * annotations and policy-cell context.
 *
 * Audited against the node contract: `ct.node-telemetry.v1` publishes NO
 * market-state, regime or confidence field of any kind. The fixture was the
 * sole source, so this resolves to undefined and every consumer reports
 * unavailable.
 *
 * Deliberately NOT replaced with a client-side heuristic. Deriving a regime
 * from candles — a moving average, a volatility band — would produce a label
 * that looks like a model output and is not one, which is a worse fabrication
 * than the fixture because it would appear computed. A market-state model must
 * publish its own explicit contract before this surface can claim anything.
 */
export interface MarketStateSnapshot {
  state: MarketState;
  confidence: number;
  confirmed: boolean;
  stateKnownAt: string;
  shiftedDays: number;
  source: string;
  modelVersion: string;
  asOf: string;
}

export function useMarketState(_instrument: string): MarketStateSnapshot | undefined {
  return undefined;
}

/** M-NODE-TEL-1 — DEVELOPMENT FIXTURE PREVIEW ONLY. Authored regime data. */
export function useFixtureMarketStatePreview(): Array<Record<string, unknown>> {
  return useFixtureWorldPreview().marketStateSnapshots as unknown as Array<Record<string, unknown>>;
}

/**
 * Feature flags — v1 defaults ON per brief. Fetched from `/api/feature-flags`
 * via TanStack Query; backend returns the same defaults so behaviour is
 * identical to hardcoded flags today but flippable at the edge later.
 */
export function useCapabilities(): Record<string, Capability> {
  const { data } = useSuspenseQuery({
    queryKey: QK.featureFlags,
    queryFn: api.featureFlags,
    staleTime: Infinity,
  });
  return data?.capabilities ?? {};
}

/** M-FLAGS-1: the capability record, or undefined when the seam has no entry. */
export function useCapability(name: string): Capability | undefined {
  return useCapabilities()[name];
}

/**
 * M-FLAGS-1: usable ONLY when `available`. Everything else — disabled,
 * unsupported, unavailable, unknown, or absent from the response — fails closed.
 */
export function useFeatureFlag(flag: FeatureFlag): boolean {
  return isUsable(useCapabilities()[flag]);
}

/** Back-compat for nav gating: a usable-only boolean map. */
export function useFeatureFlags(): Record<string, boolean> {
  const caps = useCapabilities();
  return useMemo(
    () => Object.fromEntries(Object.entries(caps).map(([k, v]) => [k, isUsable(v)])),
    [caps]
  );
}

/**
 * Operator-preferences sync (Phase 4.5). Hydrates the consolidated preference
 * model from the runtime overlay on mount (backend is the durable, cross-session
 * source), then debounce-pushes any change back. One connection, cleaned up on
 * unmount. Mounted once in AppShell. Preferences are view state (no audit event).
 */
export function usePreferenceSync(): void {
  const hydratePreferences = useShellStore((s) => s.hydratePreferences);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let last = JSON.stringify(useShellStore.getState().preferences());

    // Debounced push on any genuine preference change (non-pref store changes
    // compare equal and are ignored).
    const unsub = useShellStore.subscribe((state) => {
      const snap: OperatorPreferences = {
        theme: state.theme,
        inspectorWidth: state.inspectorWidth,
        eventDockOpen: state.eventDockOpen,
        matrixLens: state.matrixLens,
        replaySpeed: state.replay.speed,
      };
      const json = JSON.stringify(snap);
      if (json === last) return;
      last = json;
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void api.putOperatorPreferences(snap).catch(() => {}), 600);
    });

    // Hydrate from the durable backend on mount (cross-session source of truth).
    api
      .operatorPreferences()
      .then((prefs) => {
        if (cancelled) return;
        if (prefs && Object.keys(prefs).length > 0) {
          hydratePreferences(prefs as unknown as Record<string, unknown>);
          // Re-baseline so the hydrate isn't echoed straight back as a PUT.
          last = JSON.stringify(useShellStore.getState().preferences());
          if (timer) {
            clearTimeout(timer);
            timer = null;
          }
        } else {
          void api.putOperatorPreferences(useShellStore.getState().preferences()).catch(() => {});
        }
      })
      .catch(() => {
        /* offline: keep the localStorage-hydrated preferences */
      });

    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
      unsub();
    };
  }, [hydratePreferences]);
}

/**
 * Realtime read-side sync (Phase 5). One long-poll loop distributes runtime
 * changes to every client via the event log's monotonic `seq`. On each delta it
 * patches the cache (`applyEvents`) — no full world refresh. `seq` is the source
 * of truth: reconnects re-request from the last applied seq (replaying missed
 * events); a stale cursor (server head < ours, e.g. a log reset) triggers a
 * one-time full resync. Mounted once in AppShell. Transport = plain HTTP long-poll
 * (no WebSocket, no broker, no auth).
 */
export function useRealtimeSync(): void {
  const setRealtime = useShellStore((s) => s.setRealtime);
  useEffect(() => {
    let cancelled = false;
    let controller: AbortController | null = null;

    // Seed the cursor from the initial snapshot the Event Dock already loaded.
    const initial = queryClient.getQueryData<EventEntry[]>(QK.events);
    let since = initial && initial.length ? Math.max(...initial.map((e) => e.seq ?? 0)) : 0;
    seedLastAppliedSeq(since);
    setRealtime({ state: 'connecting', lastSeq: since });

    const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

    async function loop() {
      while (!cancelled) {
        controller = new AbortController();
        try {
          // We hold a snapshot and are actively polling → connected. (Errors below
          // downgrade to reconnecting/offline.)
          setRealtime({ state: 'live' });
          const res = await api.eventsLive(since, controller.signal);
          if (cancelled) break;
          if (res.stale || res.head < since) {
            // Cursor ahead of server head → the log was reset. Resync once.
            await queryClient.invalidateQueries();
            since = res.head;
            resetLastAppliedSeq(res.head);
            setRealtime({ state: 'live', lastSeq: since });
            continue;
          }
          const applied = applyEvents(res.events);
          since = Math.max(since, res.seq);
          setRealtime({
            state: 'live',
            lastSeq: since,
            ...(applied ? { lastEventAt: Date.now() } : {}),
          });
        } catch {
          if (cancelled) break;
          setRealtime({ state: typeof navigator !== 'undefined' && !navigator.onLine ? 'offline' : 'reconnecting' });
          await sleep(2000); // backoff, then retry from the same `since` → replay
        }
      }
    }
    void loop();
    return () => {
      cancelled = true;
      controller?.abort();
    };
  }, [setRealtime]);
}

/**
 * M-WORLD-ORDINARY-1 — the display taxonomy is FRONTEND-OWNED.
 *
 * This fetched `world.vocabulary` from the backend: sessions, structures,
 * directions, market states and an RR ladder. None of it is system state — it
 * is the UI's own domain vocabulary — but serving it from the fixture meant
 * Settings could not render without the authored file.
 */
export function useVocabulary(): typeof DOMAIN_VOCABULARY {
  return DOMAIN_VOCABULARY;
}

/**
 * M-WORLD-ORDINARY-1 — the acting operator, from CONFIGURATION.
 *
 * This returned `world.operators[0]`: the fixture's authored operator, rendered
 * as a display name in the shell of every page and as an id, role and timezone
 * on Settings — the last two with invented `?? 'Lead'` / `?? 'UTC'` fallbacks
 * layered on top. An asserted human identity in an operator console is not
 * decoration.
 *
 * `/api/operator/identity` reports `operator_identity.resolve()` — the same
 * source every audit record and safety `operator_ref` already uses. It is
 * configuration or the explicit `UNATTRIBUTED`, with an assurance level, and
 * it carries NO display name, role, timezone, email or avatar, because this
 * deployment has no per-user authentication and none of those has a source.
 */
export function useOperator(): OperatorIdentity {
  const { data } = useSuspenseQuery({
    queryKey: QK.operatorIdentity,
    queryFn: api.operatorIdentity,
    staleTime: Infinity,
  });
  return data;
}
