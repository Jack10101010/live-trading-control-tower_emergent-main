# Control Tower — Data Dependency & Provenance Audit (Canonical)

**Status:** Canonical inventory for the fixture→live migration. Card-level GREEN/RED
borders (temporary visual system) are implemented per this table.
**Provenance architecture:** `frontend/src/lib/cardProvenance.ts` (semantic
`CardProvenance` union → temporary GREEN/RED collapse) + required `provenance`
prop on the shared `Panel` primitive (TypeScript-enforced) + `ProvenanceFrame`
wrapper for self-contained domain components + `candleCardProvenance()` for
provider-dynamic chart cards.
**Rule set:** unknown/mixed ⇒ RED; derived analytics inherit input provenance;
live-backed cards with honest empty states stay GREEN; a provenance chip inside
a card never greens the card.

## Route-by-route card inventory

| Route | Card / container | Component | Hook → API → backend | True source | Class | Border | Operator risk | Migration action |
|---|---|---|---|---|---|---|---|---|
| /fleet | Fleet entities (deployments/brokers/accounts) | `FleetOverview` | `useFleet` → `/api/fleet` → `WORLD` | WORLD fixture | fixture | RED | High (financial entities) | replace source (`/operations/*`) |
| /broker-health | Broker health panel | `BrokerHealthView` | `useBrokerHealth` → `/api/world` → `WORLD.brokerHealth` | WORLD fixture | fixture | RED | High | await MT5 telemetry |
| /accounts | Accounts + protection | `AccountsProtectionView` | `useAccountsProtection`, `useRiskLimits` → `/api/risk/limits` | fixture accounts + fixture-overlaid limits | fixture | RED | **Highest** (risk numbers) | replace source; never green while fundedRules overlay remains |
| /market-data | Live Feed chart | `MarketDataView` (`GlobalViews`) | `ChartWorkspace` ← market-data engine | provider-dependent | dynamic | **DYNAMIC** (`candleCardProvenance`) | Medium | green only for mt5/polygon/store |
| /market-data | Feed Health | same | `useRuntimeHealth` (provider row) + placeholder rows | mixed (live provider fact + unwired feeds) | mixed | RED | Low (chips honest) | **SPLIT** candidate |
| /market-data | Market State (Provenance) | same | `useMarketState` → `WORLD.marketStateSnapshots` | WORLD fixture | fixture | RED | Medium | await node telemetry |
| /market-data | Latency histogram | same | `PlaceholderChart` | placeholder | placeholder | RED | Low | await implementation |
| /market-data | Snapshot integrity | same | placeholder grid | placeholder | placeholder | RED | Low | await node reconciliation |
| /deployments | All Deployments | `DeploymentsView` | `useFleet` → `WORLD` | WORLD fixture | fixture | RED | High | replace source |
| /strategy-packages | Active Package / delta / All Packages | `StrategyPackagesView` | `usePackages` → `WORLD.packages` | WORLD fixture | fixture | RED | Medium | replace or gate |
| /edge-monitor | 4 panels (signals/comparisons/candidates/pipeline) | `EdgeMonitorView` | `useEdgeMonitor` → `/api/edge-monitor` → `WORLD` | WORLD fixture | fixture | RED | High (fake analytics) | gate or remove |
| /system | Engine Components | `SystemView` | active package (fixture) | WORLD fixture | fixture | RED | Medium | await node manifest |
| /system | Feature Flags | `SystemView` | `useFeatureFlags` → `/api/feature-flags` → **hardcoded dict** (`server.py:4510`) | constants | placeholder | RED | Low | move to real config; FeatureGate copy corrected |
| /system | Storage & Feeds | `SystemView` | `useRuntimeHealth` → `/runtime/health` | real runtime | live | GREEN | — | retain |
| /system | System Confidence | `SystemView` | `/api/system-confidence` → `WORLD` | WORLD fixture | fixture | RED | **High** (fake confidence) | remove |
| /system | Operational Dashboard | `OperationalDashboard` (framed) | `/api/operations/*` → projection over telemetry + durable stores | **adapter-fed** (mock ⇒ synthetic) | dynamic | **DYNAMIC** (adapter kind) | Medium | retain; green only with a real adapter |
| /system | Live Runtime | `LiveRuntimePanel` (framed) | `/api/live-runtime` → backend runtime loop (adapter-fed) | **adapter-fed** | dynamic | **DYNAMIC** (adapter kind) | Medium | retain; green only with a real adapter |
| /system | Trade Ledger | `TradeLedgerPanel` (framed) | `/api/ledger/*` → TradeLedgerStore (ingests broker history — mock in mock mode) | **adapter-fed durable** | dynamic | **DYNAMIC** (adapter kind) | Medium | retain; green only with a real adapter |
| /system | Recommendations (durable) | `RecommendationPanel` (framed) | `/api/trade-recommendations*` → durable store | durable store | live | GREEN | — | retain |
| /system | Security Baseline | `SecurityBaselinePanel` (framed) | `/api/security/config` | real config | live | GREEN | — | retain |
| /system | Deployments & Manifests / Packages / Brokers | `SystemView` | `WORLD` | WORLD fixture | fixture | RED | Medium | replace source |
| /settings | Preferences / Appearance / Notifications / Shortcuts | `SettingsView` (`SettingsSection`) | `/api/operator/preferences` → runtime overlay | persisted config | runtime-config | GREEN | — | retain |
| /settings | Feature Flags section | `SettingsView` | `/api/feature-flags` → hardcoded dict | constants | placeholder | RED | Low | move to real config |
| /fleet | Deployment tile grid (Cards) | `FleetOverview` (ProvenanceFrame) | `useFleet` → `WORLD` | WORLD fixture | fixture | RED | High | replace source |
| /pair/:id/dashboard | Pair Health / Market State / Recent Decisions / Today's Posture | `PairViews` | `usePairWorkspace` etc. → `WORLD` | WORLD fixture | fixture | RED | High | replace source |
| /pair/:id/dashboard | Recent Price Action chart | `PairViews` | `ChartWorkspace` | provider-dependent | dynamic | **DYNAMIC** | Medium | as market-data chart |
| /pair/:id (trades/orders tabs) | Live/ghost trades, Pending Orders, tab tables | `PairViews` | `useTrades` → `WORLD.liveTrades/ghostTrades` | WORLD fixture | fixture | RED | **High** (fake R values) | replace with ledger/operations |
| /pair/:id (edge/package tabs) | Edge signals, expectancy, drift, health score, coverage, integrity, versions, warnings | `PairViews` | fixture cluster | WORLD fixture | fixture | RED | High | gate or replace |
| /pair/:id/journal | Events feed | `PairViews` | `useEvents` → `WORLD.events` ⊕ runtime events.db | inseparably mixed | mixed | RED | Medium | **SPLIT** (fixture seeds vs runtime events) |
| /pair/:id/policy | Policy panels ×6 | `PolicyEngineView` | `usePolicyMatrix` → fixture packages (+ synthesized cells zeroed) | WORLD fixture | fixture | RED | Medium | replace source |
| /pair/:id/replay | Replay panels ×3 | `ReplayView` | replay sessions → `WORLD.replaySessions` | replay | replay | RED | Low (gated) | retain gated |
| /pair/:id/analytics | Performance / equity / 3 more | `AnalyticsView` | `computeMetrics(useTrades)` — derived from fixture trades | derived-from-fixture | fixture | RED | High (fake performance) | replace input source |
| /version-history, /package-comparison | Packages, comparisons ×5 panels | `VersioningViews` | `WORLD.packages/packageComparisons` | WORLD fixture | fixture | RED | Medium | retain gated |
| (shell) | ChartDataStatusStrip | in `ChartWorkspace` | chart feed status | derived (labels its own source) | chip-level | — (chip) | Low | retain |
| (any) | `PlaceholderPanel` sections | `WorkspacePage` | none | placeholder | placeholder | RED | Low | await implementation |

## Manual-inspection corrections (post-launch review)

Four findings from live inspection, all fixed: (1) fleet deployment tile Cards
were unclassified → framed RED; (2) the ProvenanceFrame badge overlapped card
content → now straddles the frame edge; (3) OperationalDashboard /
LiveRuntimePanel / TradeLedgerPanel displayed mock-adapter content inside a
GREEN frame → frames are now adapter-dependent (mock/unknown ⇒ synthetic RED);
(4) the Settings "Feature Flags" section inherited runtime-config GREEN from a
shared mapped Panel → per-section provenance, flags ⇒ placeholder RED.
Residual (documented, not in card scope): the shell header's SYSTEM CONFIDENCE
chip renders fixture data as chrome — retired with the confidence milestone.

## Consumer graphs

**WORLD fixture consumers:** `/api/world` → `useRepository` selectors → FleetOverview, PairViews (health/trades/orders/decisions/posture/edge/package tabs), PolicyEngineView, AnalyticsView (via derived metrics), EdgeMonitorView, SystemView (engine components, confidence, deployments/packages/brokers), GlobalViews (deployments, packages, market-state), VersioningViews, ReplayView, BrokerHealthView, AccountsProtectionView, DeploymentManifestPanel. Plus direct fixture routes: `/api/fleet`, `/api/edge-monitor`, `/api/system-confidence`, `/api/recommendations`, `/api/decisions/{id}`.

**Synthetic market-data consumers:** `market_data.py` providers (`fixture`, `mock_live`, `replay`; `mt5` real) → `/api/market/*` → `useMarketCandles`/`useMarketSnapshot` → `ChartWorkspace` (both chart cards) → client-derived `deriveOrderBlocks`/`deriveFairValueGaps`/`deriveLiquidityPools`/`deriveMarketStructure` (inherit candle provenance) → chart overlays.

**Hardcoded feature-flag consumers:** `server.py:4510` dict → `/api/feature-flags` → `useFeatureFlags`/`useFeatureFlag` → `FeatureGate` (routes: broker-health, accounts, edge-monitor, replay, version-history, package-comparison, charts) + SystemView flags card.

**Fixture risk-limit dependencies:** fixture `accounts[].fundedRules` → `_RISK_ENGINE.limits()` (`server.py:4236`) → `/api/risk/limits` → `useRiskLimits` → AccountsProtectionView. No green until this overlay is severed.

**Mixed-source cards:** Feed Health (live provider row + placeholder rows); Events feed (fixture seeds ⊕ runtime events). Both RED; both split candidates.

**Provider/mode-dependent cards:** both `ChartWorkspace` panels (market-data + pair dashboard) via `candleCardProvenance`.

**Live-backed but currently empty:** OperationalDashboard, LiveRuntimePanel, TradeLedgerPanel, RecommendationPanel (no node has published; honest UNAVAILABLE states) — GREEN by rule.

## Uncertain provenance (flagged, defaulted RED)

None remaining at card level — every card is explicitly classified. Sub-card
uncertainty: individual rows inside GREEN operations views inherit backend
projection provenance tags; trust chips there summarize per-row truth.

## Migration milestones (recommended order)

1. Sever fixture fundedRules from `/api/risk/limits` (highest risk).
2. Remove System Confidence fixture panel.
3. Fleet/accounts/brokers → `/operations/*`.
4. Trades/orders tabs → ledger + operations.
5. Edge monitor: gate off until research pipeline is real.
6. Split events feed (fixture seeds vs runtime).
7. Feature flags → real config; then reclassify the flags card GREEN.
8. Charts: wire MT5/store as default provider.
9. Retire this temporary border system once all cards are GREEN or removed.
