import { useEffect, useMemo } from 'react';
import { useQuery, useSuspenseQueries, useSuspenseQuery, keepPreviousData } from '@tanstack/react-query';
import { deriveOrderBlocks, type OrderBlock } from '@/lib/orderBlocks';
import { deriveFairValueGaps, type FairValueGap } from '@/lib/fairValueGaps';
import { deriveLiquidityPools, type LiquidityPool } from '@/lib/liquidity';
import { deriveMarketStructure, type MarketStructure } from '@/lib/marketStructure';
import { type Candle } from '@/lib/chartData';
import { api, QK, type BackendHealth, type BrokerReconciliation, type OperatorPreferences, type RuntimeHealth, type StrategyEvaluation, type SchedulerStatus, type MarketSnapshot, type RiskLimits, type MarketCandles, type FleetProvenance, type NodeOperationalView, type AccountOperationalView, type PositionOperationalView, type OrderOperationalView } from '@/lib/api';
import {
  authoritativeOnly,
  classify,
  UNAVAILABLE_DETAIL,
  TRADES_UNAVAILABLE_DETAIL,
  type OperationalStatus,
  type ProvenancedRecord,
} from '@/lib/operationalProvenance';
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
 * The single source of truth: fetch `/api/world` once via TanStack Query.
 * All other hooks are pure selectors over the cached response. The component
 * tree cannot tell whether data comes from the fixture or a live backend.
 *
 * When individual endpoints are wired later, replace this hook's implementation
 * with per-hook `useSuspenseQuery` calls — no consumer needs to change.
 */
function useWorld(): WorldFixture {
  const { data } = useSuspenseQuery({
    queryKey: QK.world,
    queryFn: api.world,
    staleTime: Infinity,
  });
  return data;
}

/** Back-compat alias — several renderers still call `useRepository()` */
export function useRepository(): { world: WorldFixture } {
  return { world: useWorld() };
}

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
  status: OperationalStatus;
  detail: string;
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
    const nodes = authoritativeOnly(rawNodes as unknown as ProvenancedRecord[]) as unknown as NodeOperationalView[];
    const accounts = authoritativeOnly(rawAccounts as unknown as ProvenancedRecord[]) as unknown as AccountOperationalView[];
    const rejected = (rawNodes.length - nodes.length) + (rawAccounts.length - accounts.length);
    const status = classify([...nodes, ...accounts] as unknown as ProvenancedRecord[], {
      sourceAnswered: Boolean(nodesRes || acctRes),
      rejectedCount: rejected,
    });
    return {
      nodes,
      accounts,
      deployments: [],          // no authoritative deployment concept exists
      status,
      detail: status === 'available' || status === 'stale' ? '' : UNAVAILABLE_DETAIL,
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
  const world = useWorld();
  return useMemo(() => {
    // M-TRADES-1: fixture trades, ghosts and blocked intents no longer reach a
    // pair workspace on an ordinary route.
    const trades: LiveTrade[] = [];
    const ghosts: GhostTrade[] = [];
    const blocked: BlockedIntent[] = [];
    // M-FLEET-2: fixture deployments no longer reach a pair workspace.
    const deployments: Deployment[] = [];
    const marketState = world.marketStateSnapshots.find((m) => m.instrument === pairId);
    const decisions = world.decisionChains.filter((d) => d.scenarioKey.startsWith(`${pairId}:`));
    const events = world.events.filter((e) => e.scenarioKey?.startsWith(`${pairId}:`) ?? true);
    return { pair: pairId, trades, ghosts, blocked, deployments, marketState, decisions, events };
  }, [world, pairId]);
}

// Module-level cache of expanded matrices, keyed by instrument@version. Replaces
// the old per-world WeakMap now that the source comes from `/api/policy/...`.
const matrixCache = new Map<string, PolicyMatrixData>();

/**
 * Policy matrix via `/api/policy/{instrument}/matrix` (migrated off `/api/world`,
 * Phase 4.5). The endpoint returns the representative cells; `expandMatrix`
 * builds the full 24×6 grid client-side (unchanged), memoised by instrument@version.
 */
export function usePolicyMatrix(instrument: string, packageVersion?: number): PolicyMatrixData {
  const { data: source } = useSuspenseQuery({
    // UI-0: NO silent fallback. This previously swallowed every failure
    // (`.catch(() => null)`) and then synthesized all 144 cells — complete with
    // NATIVE badges and sample sizes — so a dead endpoint rendered as a confident
    // policy grid. The error now propagates to the route boundary and the operator
    // sees an explicit unavailable state instead of invented policy.
    queryKey: QK.policyMatrix(instrument, packageVersion),
    queryFn: () => api.policyMatrix(instrument, packageVersion),
    staleTime: Infinity,
    retry: false,
  });
  return useMemo(() => {
    const cacheKey = `${instrument}@${packageVersion ?? 'active'}`;
    const hit = matrixCache.get(cacheKey);
    if (hit) return hit;
    const built = expandMatrix(instrument, source);
    matrixCache.set(cacheKey, built);
    return built;
  }, [source, instrument, packageVersion]);
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
export function useBrokerHealth(): { brokers: Broker[]; health: BrokerHealth[] } {
  const { data } = useSuspenseQuery({
    queryKey: QK.brokerHealth,
    queryFn: api.brokerHealth,
    staleTime: Infinity,
  });
  return data;
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
export function useStrategyEvaluation(): StrategyEvaluation {
  const { data } = useSuspenseQuery({
    queryKey: QK.strategyDecisions,
    queryFn: api.strategyDecisions,
    staleTime: Infinity,
  });
  return data;
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
} {
  // M-FLEET-2: authoritative accounts only. Under the mock adapter the
  // projection's records carry `mock-fixture` provenance and the same invented
  // balances the fixture serves, so they are rejected and this reports
  // `unavailable` rather than rendering them.
  const { accounts, status, detail } = useOperationalFleet();
  return { accounts, status, detail };
}


/** Decision chain via `/api/decisions/{id}` (migrated off `/api/world`, Phase 4.5).
 *  Per-id query key; returns undefined on 404. */
export function useDecisionChain(decisionId: string): DecisionChain | undefined {
  const { data } = useSuspenseQuery({
    queryKey: QK.decision(decisionId),
    queryFn: () => api.decision(decisionId).catch(() => null),
    staleTime: Infinity,
  });
  return data ?? undefined;
}

/** Recommendations via `/api/recommendations` (migrated off `/api/world`, Step 4). */
export function useRecommendations(): Recommendation[] {
  const { data } = useSuspenseQuery({
    queryKey: QK.recommendations,
    queryFn: api.recommendations,
    staleTime: Infinity,
  });
  return data;
}

export function useDrafts(): Draft[] {
  return useWorld().drafts;
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

/** Packages list via `/api/packages` (migrated off `/api/world`, Step 4). Nests
 *  under the `packages` key so package commands' `invalidates:['packages']` refresh it. */
export function usePackages(): Package[] {
  const { data } = useSuspenseQuery({
    queryKey: QK.packagesList,
    queryFn: api.packages,
    staleTime: Infinity,
  });
  return data;
}

export function useReplaySessions(): ReplaySession[] {
  return useWorld().replaySessions;
}

export function useReplaySessionForPair(pair: string): ReplaySession | undefined {
  const world = useWorld();
  return world.replaySessions.find((r) => r.scope.pair === pair) ?? world.replaySessions[0];
}

export function usePackageComparisons(): PackageComparison[] {
  return useWorld().packageComparisons;
}

export function useDeploymentsForPackage(packageHash: string): Deployment[] {
  // M-FLEET-2: authoritative deployments only — of which there are none, so
  // this is empty rather than a list of fixture deployments.
  void packageHash;
  return useOperationalFleet().deployments;
}

/** Active package through `/api/packages/active`. */
export function useActivePackage(): Package {
  const { data } = useSuspenseQuery({
    queryKey: QK.activePackage,
    queryFn: api.activePackage,
    staleTime: Infinity,
  });
  return data;
}

export function useMarketState(instrument: string):
  | {
      state: MarketState;
      confidence: number;
      confirmed: boolean;
      stateKnownAt: string;
      shiftedDays: number;
      source: string;
      modelVersion: string;
      components: Record<string, unknown>;
      asOf: string;
    }
  | undefined {
  const world = useWorld();
  return world.marketStateSnapshots.find((m) => m.instrument === instrument);
}

/**
 * Feature flags — v1 defaults ON per brief. Fetched from `/api/feature-flags`
 * via TanStack Query; backend returns the same defaults so behaviour is
 * identical to hardcoded flags today but flippable at the edge later.
 */
export function useFeatureFlags(): Record<FeatureFlag, boolean> {
  const { data } = useSuspenseQuery({
    queryKey: QK.featureFlags,
    queryFn: api.featureFlags,
    staleTime: Infinity,
  });
  return data as Record<FeatureFlag, boolean>;
}

export function useFeatureFlag(flag: FeatureFlag): boolean {
  return useFeatureFlags()[flag];
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

export function useVocabulary() {
  return useWorld().vocabulary;
}

export function useOperator() {
  return useWorld().operators[0];
}
