# Live Trading Control Tower — Engineering Handoff (for Claude Code)

> Read this document in full before writing any code. It is self-contained: you do not need any prior conversation. It tells you what the system is, what is frozen, what is built, what remains, and the exact next task. When in doubt, **extend, do not redesign.**

---

## 1. Executive Summary

**What this is.** The Live Trading Control Tower is an **institutional trading-operations platform** — a supervisory "mission control" for autonomously executing algorithmic FX strategies discovered by a separate research system (FX-OB-Research-Lab). It is **not** a charting app, not MetaTrader, not a broker terminal. Its centre of gravity is **Policy · Execution · Safety · Decision-making** — the operator observes, understands, decides, protects, executes, and audits. Charts exist only for inspection.

**Current maturity.** The frontend is a **production-quality, fully data-bound operator UI running against a fixture backend**. Phases 0–3 of the post-generation roadmap are complete: enum/theme/lens fidelity, the command layer + ConfirmDialog, feature-flag gating, the Deployment Manifest, charts (TradingView Lightweight Charts), Replay, Version History, Package Comparison, Analytics, and the full operator workflow (Pending Orders, Active Trades, Trade Inspector, Decision Chain with per-node "Why?", Management Timeline). TypeScript compiles with **0 errors**; the production build succeeds with code-splitting. **What is NOT built:** a real backend (persistence, real command execution, event sourcing), a real-time layer (WebSocket snapshot+delta), authentication, and any broker integration. Overall production readiness is roughly **55–60%** for the UI, **~20%** for backend/runtime.

**Architectural philosophy.** Server-authoritative, presentational components (props in, callbacks out), one source of truth per concern, aggressive component reuse (single Badge / DataTable / Inspector / ConfirmDialog / ActionBar / ChartPanel), token-only styling, contracts-first (never invent fields), and explainability everywhere (every decision-bearing object answers "Why?"). The UI is a thin, swappable view over a data seam; the fixture can be replaced by a live API with zero component changes.

**What is already complete.** The entire operator-facing UI: shell, pair-first navigation, unified Policy Matrix (5 lenses), Decision Chain, Deployment Manifest, command layer (typed, confirmed, mock-executed), feature flags, three themes, charts + replay, analytics, versioning, and the orders/trades/inspector workflow.

**What remains.** Backend realness (Phase 4: mutations + event log + persistence), real-time (Phase 5), auth (Phase 6), broker integration (Phase 7), scale/safety hardening (Phase 8), and a handful of still-placeholder UI panels (pair Dashboard metrics, Strategy Health, Market Data chart, Orders queue-depth charts).

---

## 2. Architecture (FROZEN)

The architecture is **frozen**. It was designed across a long series of authoritative documents, audited, and locked. Your job is to build *through* it, never to change it.

### 2.1 Authoritative documents

Two of the frozen source documents are embedded in this repo for you:
- **`_fixtures/brief.md`** — the Master Generation Brief (V2.1): north star, design tokens, page template, component strategy, workspace specs, staged generation.
- **`_fixtures/contracts.md`** — Track B Canonical Contracts: every entity, identifier, command vocabulary, and the number-format canon.
- **`_fixtures/world.v1.json`** / **`frontend/src/data/world.v1.json`** / **`backend/fixtures/world.v1.json`** — the coherent Fixture World (must stay byte-identical across all three copies).
- **`AUDIT-AND-ROADMAP.md`** (repo root) — the first-pass audit and the phased roadmap this work follows.
- **`memory/PRD.md`** — product requirements memory.

The full authoritative set (Master Report, UI Blueprint R1/R2, Design System Spec, R3 Architecture Review, Master Generation Brief V2.1, Track B Canonical Contracts, Fixture World, Claude Design Prompt) lives in the **separate** `FX-OB-Research-Lab/live-tower/` repository. `_fixtures/brief.md` and `_fixtures/contracts.md` are the two you need day-to-day.

### 2.2 What must never change

- The **frozen contracts** (Track B): entity shapes, identifiers, the command vocabulary, the number-format canon.
- **Pair-first navigation** and routing (`/pair/:pairId/...` with reusable tabs).
- The **design token system** (CSS variables in `styles/tokens.css`, `data-theme` switching, mirrored into Tailwind).
- The **single-implementation components**: Badge, DataTable, Inspector (InspectorHost + entity renderers), ConfirmDialog, ActionBar, ChartPanel, Panel, Card, WorkspacePage.
- The **command layer** (typed `Command` union → `useCommand` dispatcher → ConfirmDialog → mock backend).
- The **data seam** (`useRepository`/`useWorld` selectors over `/api/world`).
- **Strategy Package** and **Deployment Manifest** semantics (the Manifest wraps the Package; the Package is immutable).

### 2.3 Architectural principles

1. **Server-authoritative.** Components never compute trading truth; they render snapshots and emit intents.
2. **Presentational components.** Props in, typed callbacks out. No data fetching, no business logic, no `localStorage` of authoritative state (view prefs like theme/inspector-width are fine).
3. **Contracts-first.** Every field a component renders comes from Track B / the fixture. Never invent a field — if one is missing, add a `// FLAG: CONTRACT REQUIRED` comment and stop.
4. **One source of truth.** One formatter module (`lib/format.ts`), one colour/label mapper (`lib/utils.ts`), one command vocabulary (`lib/commands.ts`), one design-token file.
5. **Reuse over proliferation.** A new component that differs from an existing one only in data/labels is a **variant**, not a new component.
6. **Explainability everywhere.** Every trade, blocked intent, and policy decision exposes a one-click "Why?" backed by the Decision Chain.

### 2.4 Deployment model

Local-first today: a Vite dev server + a FastAPI backend serving `world.v1.json`. MongoDB is **optional** (the backend runs fixture-only without `MONGO_URL`). The intended production path is a VPS/cloud backend with persistence + a real-time channel; the frontend is already API-bound and needs no structural change to point at it.

### 2.5 The core domain objects

- **Strategy Package** — the immutable, content-hashed, versioned unit of *policy*. Contains the Cohort×MarketState policy matrix (144 cells for one instrument), component versions (Market State model, Policy Engine, Protection, Strategy Brain, Execution Policy), research lineage, and validation. Every trade pins a `packageHash`. Immutable once promoted; a change is a new version.
- **Deployment Manifest** — the immutable definition of a *running deployment*. It **wraps** a Strategy Package (never replaces it) and adds: broker, account, pair, lane, environment (Local/VPS/Demo/Live/Experimental), market-data version, replay-data version, enabled modules, feature flags, metadata. Enables clone/export/restore/redeploy with complete fidelity. In this codebase it is **derived on the client** from real fixture data (`useDeploymentManifest`) — see Technical Debt.
- **Policy Engine** — the runtime interpreter. Given confirmed market state × cohort × direction × active package, it returns eligibility, target, risk, reasoning, and package version *before* the Strategy Brain forms an intent. In the UI it is the Policy Engine workspace + the unified matrix.
- **Pair-first navigation** — the operator drills `Fleet ▸ Broker ▸ Account ▸ Pair`; each pair is a reusable workspace with tabs (Dashboard, Policy Engine, Replay, Orders, Trades, Analytics, Edge Monitor, Strategy Health, Activity). Adding an instrument is data, not new routes.
- **Unified Policy Matrix** — ONE 24-cohort × 6-market-state grid with a **lens toggle** (Eligibility · Targets · Risk · Recommendations · Validation). Never separate matrices. Cell click → Inspector.
- **Decision Chain** — the explainability backbone: `Signal → Market State → Policy Cell → Eligibility → Target → Risk → Protection → Execution → Broker → Management → Outcome`. Each node has a one-click "Why?". Blocked intents get a typed reason. Downstream influence is a **separate panel**, not a chain node.
- **Inspector architecture** — ONE `InspectorHost` (a right-side, resizable drawer) with swappable **entity renderers** (Trade, Order, PolicyCell, Recommendation, BlockedIntent, Deployment, DeploymentManifest, DecisionChain, GhostTrade). Never build a second inspector.
- **Command architecture** — a typed `Command` discriminated union (`lib/commands.ts`) + `commandMeta` (label, confirmation class, destructive flag, summary). `useCommand()` dispatches: `silent` executes immediately; `confirm`/`confirm+reason` route to the single `ConfirmDialog`; execution posts to the mock `POST /api/commands/{name}`. No component executes a command directly.
- **Feature flags** — a `FeatureFlag` union + `useFeatureFlags()` (from `/api/feature-flags`) + a single `FeatureGate` component. Views and nav entries render only when their flag is enabled. Modules disappear cleanly when disabled.
- **Themes** — three: `console-dark` (default), `midnight-navy`, `premium-light`. All share every spacing/type/behaviour token; only colours differ. Switched via `data-theme` on `<html>`.
- **Token system** — CSS custom properties in `styles/tokens.css`, mirrored into Tailwind (`tailwind.config.js`). No literal hex/px in components. Green/red reserved for P/L & success/danger; market-state uses its own hue family; state is triple-encoded (colour + label + glyph).

---

## 3. Repository Overview

```
live-trading-control-tower_emergent-main/
├── ENGINEERING-HANDOFF.md         ← this document
├── AUDIT-AND-ROADMAP.md           ← first-pass audit + phased roadmap
├── README.md
├── _fixtures/                     ← frozen source-of-truth copies
│   ├── brief.md                   ← Master Generation Brief V2.1
│   ├── contracts.md               ← Track B Canonical Contracts
│   └── world.v1.json              ← Fixture World (canonical copy)
├── memory/
│   └── PRD.md                     ← product requirements memory
├── backend/                       ← FastAPI (fixture server, mock commands)
│   ├── server.py                  ← ALL routes (GET reads + POST /commands mock)
│   ├── fixtures/world.v1.json     ← backend copy of the fixture (keep in sync)
│   ├── requirements.txt
│   ├── pytest.ini
│   └── tests/test_control_tower_api.py   ← contract tests over the public URL
├── tests/                         ← (root pytest scaffolding)
└── frontend/                      ← Vite + React + TS app
    ├── package.json  vite.config.ts  tailwind.config.js  tsconfig.json
    └── src/
        ├── main.tsx               ← app bootstrap: QueryClient, Router, Toaster, Suspense
        ├── App.tsx                ← ALL routes; lazy-loads heavy workspaces
        ├── vite-env.d.ts
        ├── styles/tokens.css      ← THE design tokens + 3 themes + base CSS
        ├── data/world.v1.json     ← frontend copy of the fixture (keep in sync)
        ├── types/domain.ts        ← TypeScript entity contracts (subset of Track B)
        ├── store/
        │   ├── shellStore.ts      ← UI state (scope, theme, inspector, matrix lens, dock, inspector width)
        │   └── commandStore.ts    ← the single pending-command holder for ConfirmDialog
        ├── hooks/
        │   ├── useRepository.ts   ← the DATA SEAM: useWorld() + all selector hooks
        │   └── useCommand.ts      ← the command dispatcher + executeCommand()
        ├── lib/
        │   ├── api.ts             ← fetchers (GET reads + command POST); per-endpoint ready
        │   ├── commands.ts        ← the typed Command vocabulary + commandMeta
        │   ├── format.ts          ← Number-Format Canon (the ONLY formatters)
        │   ├── utils.ts           ← cn() + colour/label mappers + parseScenarioKey
        │   ├── matrixExpand.ts    ← expands representative cells → 144; synthCandles()
        │   ├── chartData.ts       ← chart adapters (candles/markers/zones/session bands)
        │   ├── comparePackages.ts ← package diff (find precomputed or compute)
        │   └── analytics.ts       ← performance metrics, equity curve, group-by
        ├── components/
        │   ├── FeatureGate.tsx    ← the ONLY flag gate
        │   ├── primitives/        ← Badge, Button/IconButton, index.tsx (HealthDot, MetricStat,
        │   │                         PLValue, RValue, ConfidenceMeter, PackageVersionChip,
        │   │                         CohortChip, MarketStateBadge, ValidationBadgeChip,
        │   │                         EligibilityBadge, LaneChip, TimestampUTC, KeyValueGrid, WhyButton…)
        │   ├── structures/        ← DataTable (sort+search), Panel/Card/EmptyState/ErrorState/DiffView,
        │   │                         WorkspacePage + Toolbar*/Placeholder*, ConfirmDialog, ActionBar
        │   ├── matrix/PolicyMatrix.tsx        ← the unified 5-lens matrix
        │   ├── domain/            ← ChartPanel, DecisionChain, DeploymentManifestPanel, tradeActions
        │   └── inspector/         ← InspectorHost + entity renderers (Trade/Order/PolicyCell/
        │                             Recommendation/BlockedIntent/Deployment/DeploymentManifest/
        │                             DecisionChain/GhostTrade)
        └── views/
            ├── FleetOverview.tsx  PairWorkspace.tsx  PolicyEngineView.tsx
            ├── ReplayView.tsx  AnalyticsView.tsx  EdgeMonitorView.tsx
            ├── BrokerHealthView.tsx  AccountsProtectionView.tsx  SystemView.tsx  VersioningViews.tsx
            ├── global/GlobalViews.tsx   ← MarketData/Deployments/StrategyPackages/Settings
            └── pair/PairViews.tsx       ← the 9 pair tabs (Dashboard/Orders/Trades/EdgeMonitor/Health/Activity)
```

**Where to find things:** routes → `App.tsx`. Data access → `hooks/useRepository.ts`. Commands → `lib/commands.ts` + `hooks/useCommand.ts`. Formatting → `lib/format.ts`. Tokens → `styles/tokens.css`. Contracts → `types/domain.ts` (+ `_fixtures/contracts.md`). Backend routes → `backend/server.py`.

---

## 4. Current Implementation Status (honest)

Legend: ✅ complete · 🟡 partial · ⬜ missing.

| Subsystem | Status | Notes |
|---|---|---|
| **Shell** (Safety Bar, ScopeNavigator, ContextBar, EventDock, CommandPalette, InspectorHost) | ✅ | Data-bound; mode banner + system-confidence + health chips from fixture; resizable inspector. |
| **Navigation / routing** | ✅ | Pair-first, reusable tabs, flag-gated, deep-linkable, catch-all → `/fleet`. |
| **Themes / tokens** | ✅ | 3 themes (console-dark/midnight-navy/premium-light), token-only, 3-way toggle + Settings. |
| **Command Layer** | ✅ (mock) | Typed vocabulary, `useCommand`, single ConfirmDialog, confirm+reason, mock `POST /commands`. **No real execution/persistence.** |
| **Feature Flags** | ✅ | `FeatureGate` gates routes/nav/panels; flags from backend; modules vanish cleanly when off. |
| **Deployment Manifest** | 🟡 | Type + renderer + panel + inspector case + clone/export/restore/redeploy commands. **Derived client-side** (no fixture/backend manifest object yet). |
| **Policy Engine + Unified Matrix** | ✅ | 5 canonical lenses, cell → inspector, recommendations/drafts/validation/promotion rails, real commands. |
| **Decision Chain** | ✅ | Full node chain + per-node one-click "Why?" with governing context; downstream-influence panel. |
| **Trade Inspector** | ✅ | Overview, Commands (ActionBar), Manifest/PolicyCell/Chain/VersionHistory links, current+original market state, plan-vs-execution, lifecycle Management Timeline, recommendations, validation, events. |
| **Pending Orders** | ✅ | Full columns + per-row ActionBar (Inspect/Cancel/ReduceRisk/ConvertGhost/Pause/Resume). Empty in current fixture (no `pending` trades). |
| **Active Trades** | ✅ | Floating R/PL, current+original state, time-in-trade, management status, per-row ActionBar, sort+search. |
| **Charts (ChartPanel)** | ✅ (synthetic data) | Lightweight Charts, read-only: candles, volume, session shading, markers, zones, price lines, replay cursor, line mode. **Candles are synthetic** (no OHLC in fixture — `// FLAG: CONTRACT REQUIRED`). |
| **Replay** | ✅ | Play/pause/step/speed/scrubber/jump-to-trade/jump-to-date; drives chart cursor, active trade, timeline, policy state; `?t=` deep-link. Cursor is per-visit local state. |
| **Analytics** | ✅ | Net R, win rate, expectancy, avg R, profit factor, drawdown; equity curve (reuses ChartPanel); dimensional group-by (state/cohort/cell/pair/deployment/package/lane). Low-N honest. |
| **Version History** | ✅ | Real DataTable (status/validation/created/promoted/superseded/deps/hash) + Inspect/Compare/Promote/Rollback. |
| **Package Comparison** | ✅ | Real diff from fixture `packageComparisons` + client-side fallback; changed-only; DiffView, no raw JSON. |
| **Edge Monitor** | 🟡 | Metrics bound; some charts still placeholder. |
| **Pair Dashboard / Strategy Health / Market Data chart / Orders queue charts** | 🟡 | Shaped with real headers but `PlaceholderChart`/metrics remain. |
| **Backend** | 🟡 | FastAPI GET reads for all entities + one **mock** `POST /commands`. Mongo optional. |
| **Persistence** | ⬜ | No database writes; world is a static file; commands don't persist. |
| **Realtime** | ⬜ | No WebSocket/SSE; TanStack Query `staleTime: Infinity`. |
| **Authentication** | ⬜ | No auth/permission seam (operator is `world.operators[0]`). |
| **Broker Integration** | ⬜ | None. No live/demo broker. Commands never touch a broker (by design at this phase). |
| **Testing** | 🟡 | Backend contract tests exist (`backend/tests`). **No frontend tests.** |
| **Performance** | 🟡 | Lazy-loading works; tables are **not virtualized** (`@tanstack/react-virtual` installed, unused). |

---

## 5. Current Roadmap (priority order)

Phases 0–3 are **done**. Remaining, in priority order:

**Phase 4 — Real backend: mutations + event log + persistence.** *Why:* the command layer exists but is fire-and-forget against a mock; operator actions must become durable, auditable state. *Depends on:* nothing new (seam + command vocabulary already exist). *Scope:* make `POST /api/commands/{name}` validate + append an append-only `BotEvent`, expose `GET /api/events`, add persistence (SQLite is sufficient locally; Mongo optional), and flip the read hooks from `/api/world` to per-endpoint queries incrementally. Close the **command → event → audit** loop so the Event Dock reflects actions.

**Phase 5 — Real-time (snapshot + delta).** *Why:* a live control tower needs pushed updates (fills, P/L, risk, reconciliation). *Depends on:* Phase 4 event log. *Scope:* WebSocket/SSE emitting typed frames (`bot.state`, `trade.update`, `event.log`, `risk.update`, `reconcile.diff`), a client subscription that patches the Query cache by `seq`, and re-snapshot on gap. Move relevant queries off `staleTime: Infinity`.

**Phase 6 — Auth / permission seam + multi-operator.** *Why:* promotion/override/go-live must be gated; multi-operator handoff. *Depends on:* Phase 4. *Scope:* a no-op-today `AuthProvider` + `can(action)` wrapping the command dispatch, `OperatorSession`, handoff. Cheap now, expensive later.

**Phase 7 — Broker adapter (demo first) behind hard rails.** *Why:* execute for real, smallest possible, double-armed. *Depends on:* Phases 4–6. *Scope:* a `BrokerAdapter` interface, a MockBroker → DemoBroker, reconciliation lock, the double-arm (`EXECUTION_MODE=live` + `liveEnabled` + permission). **Do not start before the safety rails exist.**

**Phase 8 — Scale + safety hardening.** *Why:* production robustness. *Scope:* table virtualization (react-virtual), error boundaries, frontend tests + a states gallery, live-trading safety rails, finish the remaining placeholder panels (Dashboard metrics, Strategy Health, Market Data chart, Orders queue charts), remove/replace synthetic candles when real bars land.

---

## 6. Technical Debt (known)

- **Contract gaps (`// FLAG: CONTRACT REQUIRED`):**
  - No `md.bar.v1` OHLC series in the fixture → charts use a **deterministic synthetic candle** series (`lib/chartData.ts deriveCandles` → `lib/matrixExpand.ts synthCandles`), anchored to real trade timestamps. Replace `deriveCandles` when real bars exist; every chart consumer is unchanged.
  - **Deployment Manifest is derived client-side** (`useDeploymentManifest`) from deployment + package + account + broker + flags. There is no fixture/backend manifest object. When a real manifest store lands, point the selector at it — the renderer/panel/inspector are unchanged.
- **Thin/loose types:** `types/domain.ts` is a documented subset; `signals`, `operators`, `nativeValidations` are `unknown[]`. Tighten as needed against `_fixtures/contracts.md`, never invent.
- **Fixture limitations:** low sample size (few closed trades → thin analytics/equity), **no pending orders** (Orders workspace is empty but complete). The fixture is coherent but small.
- **Backend limitations:** GET-only reads + one mock command endpoint; no persistence, no event emission, no WS, no auth, no broker. `server.py` holds the world in memory from `world.v1.json`.
- **Performance concerns:** tables map arrays directly (fine at fixture scale; virtualize before large row counts). The main JS bundle is ~600 kB (heavy workspaces are already code-split into separate chunks — this is acceptable, not urgent).
- **Lazy-loading risks:** heavy views are `React.lazy` + a local `Lazy` Suspense wrapper in `App.tsx`. Validated: single stores, single QueryClient, deduped queries, clean chart/interval unmount, inspector state survives navigation. **Any new lazy view must preserve these invariants** (see §11).
- **Known compromises:** Replay cursor is per-visit local state (resets on tab change); Move Stop/Target/Reduce/Partial commands dispatch without a numeric-price input UI (ConfirmDialog collects a reason, not a price) — a price-input variant is a future enhancement, not a contract change.
- **Fixture sync:** `frontend/src/data/world.v1.json`, `backend/fixtures/world.v1.json`, and `_fixtures/world.v1.json` must stay identical. Changing one requires changing all three.
- **Environment note:** in this sandbox the `dist/` folder is filesystem-locked; verification builds went to a temp dir. On a normal machine `npm run build` overwrites `dist/` cleanly.

---

## 7. Lessons Learned (READ THIS)

This section exists to prevent repeating mistakes. It is the most important part of the handoff.

- **The architecture is frozen because it was expensive to converge and cheap to violate.** It went through a Master Report, two UI blueprints, a design-system spec, an R3 review, a generation brief (V1→V2.1), Track B contracts, a first-pass audit, and three implementation phases. Every "small redesign" risks a cascade. **Extend; do not redesign.**
- **Component reuse is the single biggest cost lever.** The first generation (Emergent) succeeded largely because it produced **one** Badge, **one** DataTable, **one** Inspector shell. Every phase since has preserved that. When we needed order/trade actions, we added **one** `ActionBar` + **one** `tradeActions` source rather than buttons-per-view. This avoided regenerating dozens of near-duplicate components.
- **Why the Inspector exists as one shell with entity renderers:** so any entity (trade, order, cell, recommendation, manifest, chain…) gets identical chrome, resize, escape-to-close, and "Why?" behaviour for free. Spawning `TradeInspector`, `OrderInspector`, etc. would have multiplied bugs and styling drift. **Never create a second inspector — add a renderer + a `shellStore` payload kind + an `InspectorHost` case.**
- **Why the ActionBar exists:** to guarantee every operator action flows through the **one** command dispatcher (→ ConfirmDialog → mock backend), so command logic is defined once. Inline action buttons would duplicate confirmation/dispatch logic and drift. **All actions go through ActionBar + `lib/commands.ts`.**
- **Why Badge / DataTable / ConfirmDialog must stay single implementations:** they are the highest-fan-in components. A second implementation means divergent padding, tokens, and behaviour, and doubles the regeneration surface. If you think you need a new one, you need a **variant/prop** on the existing one.
- **Why feature flags exist:** modules must enable/disable by configuration (environment + manifest), not by code changes. This is how the platform scales to new modules without redesign and how disabled surfaces vanish cleanly. **Gate new modules with `FeatureGate`; add the flag to the `FeatureFlag` union and the backend.**
- **Why the Deployment Manifest wraps the Strategy Package (and does not replace it):** the Package is the immutable *research artifact* (policy + component versions + hashes); the Manifest is the immutable *runtime instance* (Package + broker + account + pair + lane + environment + flags). Wrapping preserves attribution (`packageHash` on every trade) while enabling clone/export/restore/redeploy. **Never fold Package fields into the Manifest or mutate a running Manifest — a change is a new version + redeploy.**
- **Why "Scenario" is NOT a separate entity:** the scenario *is* the `scenarioKey` = `instrument:session:structure:direction:marketState` = the Policy Cell coordinate. A separate Scenario object would duplicate the Policy Cell and split addressing. Everything (trades, ghosts, blocked, analytics, replay) keys off `scenarioKey`. **Use the key; do not mint a Scenario type.**
- **Why lazy loading must be carefully validated:** lazy chunks can silently reintroduce duplicate stores, duplicate query clients, stale state, or leaked chart/interval handles. We validated: **2 distinct singleton Zustand stores, exactly 1 QueryClient/Provider, deduped `useSuspenseQuery`, `chart.remove()` and `clearInterval` on unmount, inspector state in the store (survives navigation).** Re-run these checks whenever you add a lazy route or a chart/timer.
- **What worked:** the data seam (fixture↔API parity), token-only theming, contracts-first typing, and the staged (freeze-each-layer) approach. Each phase typechecked to 0 and production-built before moving on.
- **What did not / traps hit:** synthetic candle timestamps initially did not overlap real trade times (fixed by anchoring `deriveCandles` to an end ISO); an over-clever account lookup was simplified to a `deploymentId→deployment→account` map; lucide icon typing needed `LucideIcon`; canvas colours must be **resolved** from CSS variables (Lightweight Charts can't read `var(--x)`) — see `resolveColor` in `ChartPanel`.
- **Regenerations avoided:** by embedding tokens (implement, don't invent), fencing the component inventory, and closing contract gaps before generating policy screens, we kept the first Claude Design pass and every subsequent phase from being torn up.

---

## 8. Coding Standards

- **Reuse philosophy:** extend existing components via props/variants before creating anything new. If a "new" component differs only in data/labels, it is a variant.
- **Component philosophy:** presentational only. **Props in, callbacks out.** No data fetching, no business logic, no global-store writes of trading truth inside leaf components.
- **Contracts first / never invent fields:** every rendered value comes from `types/domain.ts` (backed by `_fixtures/contracts.md`) or the fixture. Missing field → `// FLAG: CONTRACT REQUIRED`, stop, ask.
- **Typed commands:** all mutations are members of the `Command` union in `lib/commands.ts`, dispatched via `useCommand`. Never call `api.command` or `fetch` for a mutation from a component; never `alert()`; never a bare placeholder toast for an action.
- **Token-only styling:** consume CSS variables / Tailwind token classes. No literal hex/rgb/px. Lucide icons only. Green/red reserved for P/L & success/danger; triple-encode state.
- **One source of truth:** formatting → `lib/format.ts`; colour/label maps → `lib/utils.ts`; chart adapters → `lib/chartData.ts`; analytics math → `lib/analytics.ts`. Do not inline these.
- **No duplicate components / no duplicated business logic:** one Badge, one DataTable, one Inspector, one ConfirmDialog, one ActionBar, one ChartPanel, one DecisionChainView.
- **Always verify:** `npx tsc --noEmit` must be **0 errors** and `npm run build` must succeed before you consider a task done. Keep the three `world.v1.json` copies in sync.

---

## 9. Current State of the Codebase

Implementation stopped at the **end of Phase 3 (operator workflow)**. The last completed work:

- **Phase 0** — enums aligned to Track B; Premium Light theme + 3-way toggle; canonical 5-lens matrix; hardcoded values bound to fixture; `server.py` Mongo made optional; pre-existing type errors fixed.
- **Phase 1** — the command layer (`commands.ts`, `commandStore.ts`, `useCommand.ts`, `ConfirmDialog`, `api.command`, mock `POST /commands`); feature-flag gating (`FeatureGate` + route/nav gates); Deployment Manifest (type, `useDeploymentManifest`, renderer, inspector case, `DeploymentManifestPanel`, manifest commands).
- **Phase 2** — ChartPanel enhanced (read-only, full overlay set) + `chartData.ts`; Replay completed with a real clock; Version History + Package Comparison made real (`comparePackages.ts`); Analytics expanded (`analytics.ts`); heavy workspaces lazy-loaded.
- **Phase 3** — Pending Orders + Active Trades workspaces (full columns + per-row `ActionBar`); expanded Trade Inspector; per-node "Why?" in the Decision Chain; lifecycle Management Timeline; `ActionBar` + `tradeActions`; `DataTable` sort + search; resizable/persistent Inspector.

**Verification at stop:** `tsc --noEmit` = 0 errors; production build succeeds with code-split chunks (PolicyEngine, Versioning, Replay, Analytics, EdgeMonitor); single-implementation components confirmed (1 def each); backend `py_compile` OK.

**Still needs implementation:** everything in §5 (Phases 4–8). The frontend is complete enough to operate a *fixture* world end-to-end; nothing is *durable* or *live*.

---

## 10. Immediate Next Task

**Task: Phase 4, Step 1 — make commands durable and auditable (the command → event → audit loop).**

Do exactly this, and nothing beyond it:

1. **Backend (`backend/server.py`):** give `POST /api/commands/{name}` a real (still broker-free) effect — validate the command name against the known set, generate a `commandId`, and **append an immutable `BotEvent`** (`{eventId, seq, category, code, humanExplanation, scenarioKey?, packageHash?, who, causedBy, before?, after?, at}`) to an **append-only store**. Use SQLite (create the file lazily; no Mongo requirement) or, if you must stay zero-dependency first, an in-memory list persisted to a JSONL file. Return the created event(s) in the response, not just an ack.
2. **Backend reads:** update `GET /api/events` to return the fixture events **plus** any appended events, newest `seq` last, filterable by `pair` (already supported).
3. **Frontend (`hooks/useCommand.ts` + `lib/api.ts`):** after a successful `executeCommand`, **invalidate the events query** (`queryClient.invalidateQueries` for the events key) so the **Event Dock updates** to show the operator's action. Keep the success toast.
4. **Frontend (`EventDock`/`useEvents`):** ensure the dock reads through a query key that can be invalidated (introduce `QK.events` if needed) rather than only the monolithic world.
5. **Verify:** `tsc --noEmit` = 0, `npm run build` succeeds, backend test still passes; manually confirm a dispatched command (e.g. `PauseDeployment`) appends an event the dock shows.

**Why this is the single highest-value next step:** the entire command layer, ConfirmDialog, and ActionBar were just built but are currently *fire-and-forget* — actions vanish. Closing the command→event→audit loop makes every operator action **durable, visible, and explainable**, which is the platform's core promise (auditability/explainability). It is **low-risk** (no broker, no live money, no persistence of trading positions — just an event log), it exercises the Phase-4 persistence seam on the safest possible surface, and it is the **prerequisite** for the real-time layer (Phase 5) and everything after. Do not jump to WebSockets, auth, or broker work first — this loop must exist and be trusted before anything pushes or executes.

---

## 11. Risks (remaining)

- **Lazy-loading regressions:** adding a lazy route can silently duplicate a store or QueryClient, or leak a chart/interval. **Mitigation / mandatory check:** confirm exactly 1 `new QueryClient`, N *distinct* Zustand stores (not duplicates), deduped query keys, `chart.remove()` + `clearInterval` on unmount, and that inspector/scope state lives in the store (survives navigation).
- **Future WebSocket layer:** introducing real-time risks double subscriptions, cache races, and memory leaks. **Mitigation:** one connection at the shell level, patch the Query cache by monotonic `seq`, re-snapshot on gap, tear down on unmount; keep `staleTime` decisions explicit per entity.
- **State duplication:** two stores exist by design (`shellStore`, `commandStore`); do not add a third for the same concern. UI state → Zustand; server state → Query. Never both.
- **Query duplication:** every read must go through a shared query key (`QK`) so it dedupes and can be invalidated. Do not `fetch` ad hoc in components.
- **Backend persistence:** when you add SQLite/Mongo, keep the read hooks' *shape* identical so the frontend is unchanged; migrate hooks off `/api/world` **incrementally**, one endpoint at a time.
- **Large tables:** virtualize (`@tanstack/react-virtual`, already installed) before row counts grow; the current non-virtual tables are fine at fixture scale only.
- **Fixture drift:** the three `world.v1.json` copies must stay identical; a drift silently breaks reads or tests.
- **Contract drift:** if you tighten a `unknown[]` type or add a field, verify it against `_fixtures/contracts.md`. Never invent; flag instead.
- **Design drift:** any literal colour/px, a second Badge/Table/Inspector, or an un-tokened style is drift. Review for it.

---

## 12. Working Rules (always)

1. **Architecture is frozen. Never redesign.** Extend the existing structure.
2. **Prefer extending over creating.** New need → new prop/variant/renderer, not a new component.
3. **Reuse existing components. Never duplicate.** One Badge, DataTable, Inspector, ConfirmDialog, ActionBar, ChartPanel, DecisionChainView.
4. **Never invent contracts or fields.** Use `types/domain.ts` / the fixture; missing → `// FLAG: CONTRACT REQUIRED`.
5. **All mutations are typed Commands** dispatched via `useCommand` → ConfirmDialog. No `alert()`, no placeholder-toast actions, no ad-hoc mutation fetches.
6. **Presentational components only.** Props in, callbacks out.
7. **Token-only styling.** CSS variables / Tailwind tokens; Lucide icons; three themes; triple-encoded state.
8. **Build incrementally**, one coherent task at a time; do not jump ahead.
9. **Always `npx tsc --noEmit` (0 errors) and `npm run build` (success)** before finishing.
10. **Preserve routing and pair-first navigation.** New pair capability is a tab, not a new route tree.
11. **Preserve the command layer, Deployment Manifest, and Strategy Package semantics.** Manifest wraps Package; Package is immutable; every trade pins `packageHash`.
12. **Preserve explainability.** Every decision-bearing object keeps its one-click "Why?" and Decision Chain.
13. **Preserve the data seam.** Reads through `useRepository` selectors and shared query keys; keep the fixture↔API swap seamless.
14. **Keep the three `world.v1.json` copies in sync.**
15. **Validate lazy-loading invariants** whenever you touch routing, stores, charts, or timers.

Welcome aboard. Start with §10.
