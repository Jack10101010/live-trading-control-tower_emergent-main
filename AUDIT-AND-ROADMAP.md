# Live Trading Control Tower — Emergent First-Pass Audit & Roadmap

**Scope:** comprehensive architecture + implementation audit of this repository (the Emergent-generated first-pass UI), read in full. **No code was modified.**
**Verdict up front:** this is a **high-quality, faithful, well-architected first pass** — the design system, component consolidation, data-seam, and routing are genuinely strong. It is **not yet production-ready**, which is expected: it is a read-only presentation skeleton with **no command/mutation layer, no real-time, and several contract gaps** (most notably the Deployment Manifest and the third theme). The path to production is clear and low-risk because the foundation is clean.
**Overall production readiness: ~35–40%** (visual/structural ~75%, read-only data-binding ~70%, interactivity/commands ~10%, backend integration ~25%).

---

## 1. Overall architecture

**Stack:** Vite + React 18 + TypeScript · React Router v6 · TanStack Query (server cache) · Zustand (`shellStore`, UI-only state) · Tailwind (mapped to CSS-variable design tokens) · lightweight-charts · lucide-react · sonner (toasts). Path alias `@ → src`. Backend: FastAPI stub serving `world.v1.json` (Motor/Mongo imported but unused).

**Shape (excellent):** a clean, server-authoritative-ready presentation architecture.
- **Single data seam:** `hooks/useRepository.ts` fetches `/api/world` once via `useSuspenseQuery`; every other hook (`useFleet`, `usePolicyMatrix`, `useTrades`, `useSystemConfidence`…) is a **pure selector** over the cached world. Components cannot tell fixture from live backend. `lib/api.ts` already defines per-endpoint fetchers (`/fleet`, `/policy/{instrument}/matrix`, `/trades`…) so hooks can flip fixture → API with **zero consumer change**. This is exactly the "presentational, props-in/callbacks-out, no data logic in components" discipline the brief asked for.
- **UI state vs server state cleanly split:** Zustand holds only shell/UI state (scope, theme, inspector payload, matrix lens, palette, event dock); server data lives in Query. No `localStorage` of authoritative state.
- **Design tokens** are real CSS variables in `styles/tokens.css`, themed by `data-theme`, mirrored into Tailwind's `colors`. Format canon centralised in `lib/format.ts`.

**Assessment:** the architecture is faithful to the frozen brief and is the strongest part of the deliverable. It is the right skeleton to build production on.

---

## 2. Routing and navigation

- **React Router v6**, declared in `App.tsx`. `/` → `/fleet`; catch-all → `/fleet` (no dead ends).
- **Global routes:** `/fleet · /broker-health · /accounts · /market-data · /deployments · /strategy-packages · /edge-monitor · /system · /settings · /version-history · /package-comparison`.
- **Pair workspace (reusable across instruments):** `/pair/:pairId` with nested tabs `dashboard · policy · replay · orders · trades · analytics · edge-monitor · strategy-health · activity`. This parametric design is the pair-first architecture done correctly — adding GBPUSD/NQ/BTC is data, not new routes.
- **ScopeNavigator** (left rail) exposes the full `Fleet ▸ Broker ▸ Account ▸ Deployment` drill plus per-pair expandable tab trees, and shows dimmed "instrument-slot-ready" ghosts (GBPUSD/XAUUSD/NQ/ES/BTC).

**Gaps:** Version History and Package Comparison are routed but render placeholders (marked with a lock). Navigation is otherwise complete and resolves everywhere.

---

## 3. Workspace / page inventory

| Area | Route | State |
|---|---|---|
| Fleet Overview | `/fleet` | **Real** (deployment grid + attention rail from SystemConfidence) |
| Broker Health | `/broker-health` | **Real-ish** (health metrics bound) |
| Accounts & Protection | `/accounts` | **Real-ish** (funded rules/risk bound) |
| Market Data | `/market-data` | **Partial** (market-state real; chart placeholder) |
| Deployments | `/deployments` | **Real** (DataTable of deployments) |
| Strategy Packages | `/strategy-packages` | **Real** (packages table; chart placeholder) |
| Edge Monitor (global) | `/edge-monitor` | **Real-ish** (metrics bound) |
| System | `/system` | **Partial** |
| Settings | `/settings` | **Read-only** (flags/vocab/theme shown; no mutation) |
| Version History | `/version-history` | **Placeholder** |
| Package Comparison | `/package-comparison` | **Placeholder** (fixture has the data — could be real) |
| Pair › Dashboard | `/pair/:id/dashboard` | **Partial** (real market state/decisions; placeholder metrics) |
| Pair › Policy Engine | `/pair/:id/policy` | **Real** (unified matrix + rails; read-only actions) |
| Pair › Replay | `/pair/:id/replay` | **Shell only** (transport controls no-op) |
| Pair › Orders | `/pair/:id/orders` | **Real** (DataTable) |
| Pair › Trades | `/pair/:id/trades` | **Real** (open/closed/ghost/blocked tables → inspector) |
| Pair › Analytics | `/pair/:id/analytics` | **Placeholder** (charts) |
| Pair › Edge Monitor | `/pair/:id/edge-monitor` | **Partial** (metrics real; charts placeholder) |
| Pair › Strategy Health | `/pair/:id/strategy-health` | **Placeholder** |
| Pair › Activity Timeline | `/pair/:id/activity` | **Real** (events filtered to pair) |

**~20 workspaces exist; roughly half are fully data-bound, half are "shaped but placeholder."**

---

## 4. Component inventory

~40 components across a clean tier structure:
- **Shell (7):** AppShell, CommandSafetyBar, ScopeNavigator, ContextBar, EventDock, CommandPalette, InspectorHost.
- **Primitives (~16 in `index.tsx` + Badge + Button):** Badge, Button/IconButton, HealthDot, MetricStat, PLValue, RValue, ConfidenceMeter, SampleSize, PackageVersionChip, CohortChip, MarketStateBadge, ValidationBadgeChip, EligibilityBadge, LaneChip, TimestampUTC, KeyValueGrid, WhyButton.
- **Structures (in 3 files):** DataTable, Panel/Card/EmptyState/ErrorState/DiffView, WorkspacePage + Toolbar* + Placeholder* + ComingSoon/SkeletonRows.
- **Matrix (1):** PolicyMatrix (the flagship).
- **Domain (2):** DecisionChain, ChartPanel.
- **Inspector (8):** InspectorHost + 7 entity renderers (PolicyCell, Trade, GhostTrade, BlockedIntent, Deployment, Recommendation, DecisionChain).
- **Views (10 files, ~25 view functions).**

---

## 5. Reusable components (the standout success)

The reuse mandate was followed almost exactly:
- **One `Badge`** — variant-driven (`status/validation/target/risk/marketState/health/lane/recommendation/eligibility/mode/…`); all badges/chips derive from it. No `XyzBadge` proliferation.
- **One `DataTable`** — config-driven columns/rowKey/onRowClick; used for orders, trades, ghosts, blocked, deployments, packages, events.
- **One `Inspector` shell** — fixed host, swappable entity renderers. No per-entity inspector components.
- **One `Card`, one `Panel`, one `WorkspacePage`** skeleton.
- Shared primitives everywhere; `lib/format.ts` is the single formatting source; `lib/utils.ts` centralises colour/label mappers.

This is exactly the low-component-count, config-driven outcome the brief optimised for.

---

## 6. Duplicate components

**Effectively none** — the consolidation held. Minor, trivial repetition only:
- Small inline helpers repeated across views (`MetricRow` in FleetOverview; `ToolbarChip` used pervasively but it's a shared export). Not worth refactoring now.
- `ContextBar` merges the brief's separate **Context Bar + Health Strip** into one band (intentional, documented "variable emphasis"). Acceptable, flagged as a deviation in §13, not a duplicate.

---

## 7. Placeholder components

A deliberate placeholder system in `WorkspacePage.tsx`: `PlaceholderTable · PlaceholderChart · PlaceholderMetricGrid · PlaceholderPanel · ComingSoon · SkeletonRows` ("data lands in a later phase"). Consumed by: pair Analytics, Strategy Health, Edge-Monitor charts, Dashboard metrics; global Market Data chart, Strategy Packages chart; and the two fully-placeholder pages **Version History** and **Package Comparison**. Charts across the app are placeholders even though `ChartPanel` (Lightweight Charts) exists and is unused on most pages.

---

## 8. Broken or incomplete navigation

- **No broken links / no unresolved routes.** Every nav target renders something.
- **Incomplete (by design):** Version History and Package Comparison are placeholder destinations; several global pages (Market Data, System, Settings) are shaped-but-partial.
- **Interaction dead-ends:** Replay transport buttons are `onClick={() => {}}`; Global Kill is a native `alert()`; Policy Engine Edit/Validate/Promote/Deploy buttons fire a "Read-only preview" toast; the `WhyButton` inside `PolicyCellRenderer` is a no-op comment (`/* would drill to decision chain */`). These are navigation-complete but action-incomplete.

---

## 9. Design system consistency

**Very high.** Token-only colour via CSS vars + Tailwind mapping; spacing/radius/typography/shadow scales all tokenised; `lib/format.ts` enforces the Number-Format Canon (R, money, %, pips, confidence, `n=`, `P=`, hash, UTC, relative). Triple-encoded state (colour + label + glyph) in Badge/MarketStateBadge. Focus-visible ring, reduced-motion, custom scrollbars, tabular numerics — all present and consistent.

**Minor inconsistencies:** a few hardcoded literals — the boot/wordmark gradient (`#4C82F7→#2FB6C9` in `main.tsx` and `CommandSafetyBar`), and rgba tick-animation literals in `tokens.css` (acceptable, they live in the token file). Nothing systemic.

---

## 10. Theme implementation

- **Two themes implemented:** `console-dark` (default) and `midnight-navy`, both as token swaps via `data-theme`, values matching §D2 exactly.
- **`premium-light` is MISSING.** The brief requires three themes; the store's `theme` union is only `'console-dark' | 'midnight-navy'` and the toggle is binary. **This is a hard-requirement deviation** (§13).

---

## 11. Fixture usage

**Exemplary.** `world.v1.json` is the single design dataset and is **byte-identical across `frontend/src/data/`, `backend/fixtures/`, and the canonical contract fixture** (verified). The matrix's representative cells are expanded to the full 24×6=144 grid by `lib/matrixExpand.ts`. No Lorem Ipsum, no fabricated values, every screen renders from one coherent world (Package v18 active + v17 superseded, three lanes, five block reasons, System Confidence, etc.).

---

## 12. Current implementation percentage

| Layer | Est. complete | Notes |
|---|---|---|
| Design system / tokens / themes | **~80%** | 2 of 3 themes; otherwise complete |
| Shell / navigation | **~85%** | complete; health-strip merged, some hardcoded chips |
| Primitives | **~90%** | comprehensive, production-grade |
| Structures | **~70%** | DataTable/Panel/Inspector strong; **ConfirmDialog/ActionBar/Timeline missing**; no virtualization |
| Read-only data binding | **~70%** | core views bound; analytics/charts placeholder |
| Policy matrix (flagship) | **~75%** | renders + lenses + inspector; lens naming deviates; drafts read-only |
| Interactivity / commands | **~10%** | no mutation layer at all |
| Real-time (snapshot+delta/WS) | **~0%** | absent; Query staleTime Infinity |
| Backend integration | **~25%** | GET routes + seam ready; no mutations, no persistence, no WS |
| **Overall production readiness** | **~35–40%** | strong skeleton, not operational |

---

## 13. Deviations from the frozen architecture

Ordered by materiality:

1. **Deployment Manifest is entirely absent (highest).** V2.1's central addition — no `DeploymentManifest` type, renderer, or panel. `Deployment` still inlines `packageHash/accountId/pair/lane` with **no `manifestId/manifestHash`**. Clone/export/restore/redeploy and full-fidelity replay have no representation.
2. **Third theme (`premium-light`) missing.**
3. **Matrix lenses deviate:** implemented `eligibility · targets · risk · evidence · confidence` vs canonical `eligibility · targets · risk · recommendations · validation`. "Recommendations" and "Validation" lenses are not present as lenses.
4. **No command/confirmation layer:** Global Kill uses `alert()`; there is **no `ConfirmDialog`** (the brief's only-modal) and **no command callbacks** (`onPromoteDraft`, `onPauseDeployment`, …). Mutations are toasts.
5. **Feature flags fetched but not enforced:** `useFeatureFlags` exists and Settings displays them, but **Views/nav do not self-gate** (`visible = environmentEnabled ∧ manifestEnabled`). `newsIntegration:false` still shows nothing conditionally; nothing hides.
6. **Enum drift vs Track B:** `executionMode` = `live|mock` (no `demo`); `operationMode` = `live|mock|replay` (no `demo`); SystemConfidence `band` = `green|caution|red` (Track B: `healthy|caution|degraded|critical`); signal states `ok|warn|critical` (Track B: `ok|warn|fail|unknown`); Deployment status adds `Idle`, lacks `Error`.
7. **Health Strip merged into Context Bar** and several health chips are **hardcoded** ("MD feed ok", "Reconcile ok", "Data 40s lag") rather than data-bound.
8. **Mode banner hardcoded to "Live"** (not driven by `operationMode`/deployment mode).
9. **No real-time layer** (brief specified snapshot + delta / WebSocket). Query is static (`staleTime: Infinity`).
10. **Virtualization absent** though `@tanstack/react-virtual` + `react-table` are installed — DataTable and ScopeNavigator map arrays directly.

---

## 14. Technical debt

- **Hardcoded data that should be bound:** Fleet `DeploymentCard` renders `MarketStateBadge state="BullExpand" confidence={0.96}` **literally** (not per-deployment); ContextBar breadcrumb hardcodes `FTMO / Funded USD`; ContextBar health chips hardcoded; mode banner hardcoded.
- **Thin/loose types:** `types/domain.ts` is a self-described "thin subset" — `signals`, `operators`, `nativeValidations`, `packageComparisons`, `replaySessions` are `unknown[]`; `Recommendation.evidence` omits `promotionHistory`/`deploymentHistory` (research-traceability §7); no `Command`/`DeploymentManifest`/`PackageComparison`/`ReplaySession` types.
- **Backend brittleness:** `server.py` does `mongo_url = os.environ['MONGO_URL']` at import → **hard crash if unset**; Mongo client created but unused. CORS wildcard.
- **Unused dependencies:** `@tanstack/react-table`, `@tanstack/react-virtual` installed, not used.
- **No frontend tests** (only backend contract tests, GET-only).
- **`alert()`** in a trading kill path.
- **No auth/permission seam** (brief wanted a no-op seam now).
- **No error boundaries** around Suspense beyond the boot fallback.

---

## 15. Missing workspaces

Relative to the brief/inventory:
- **Deployment Manifest management** (clone / export / restore / redeploy) — none.
- **Real Version History & Package Comparison** — placeholders (yet `packageComparisons` fixture data exists and is ready to bind).
- **Native Validation run / Promotion / Draft-edit surfaces** — present as read-only panels inside Policy Engine, but not as actionable workspaces.
- **News** (flag off) — no view, acceptable while flagged.
- No dedicated global **Blocked/Vetoed** surface (blocked intents appear as a Trades sub-tab — acceptable).

---

## 16. Missing functionality

- **The entire command/mutation surface:** deploy · pause · resume · kill · flatten · close trade · move SL/TP · SL→BE · approve/reject recommendation · create/promote/discard draft · run native validation · apply/remove override · set lane mode. **None are wired** (no `useMutation`, no POST in `api.ts`, no backend mutation routes).
- **Confirmation system** (`ConfirmDialog`, confirm+reason) — absent.
- **Third theme.**
- **Feature-flag gating** of views/nav.
- **Real charts** (ChartPanel unused on pages; placeholders instead) — Replay/Analytics especially.
- **Real-time updates** (WS / snapshot+delta) — absent.
- **Decision-chain drill** from cells/rows in several places (WhyButton no-ops).
- **Virtualization** for scale.
- **Deployment Manifest** lifecycle.

---

## 17. Backend integration readiness

**Reads: strong and low-risk.** The hook seam + typed selectors + `api.ts` per-endpoint fetchers + matching FastAPI GET routes + identical fixture mean flipping fixture → live for **read** data is a near-mechanical change (`useWorld` → per-hook `useSuspenseQuery` at the endpoints already declared). `QK` query keys and route shapes are in place.

**Writes & real-time: not started.**
- Backend exposes **zero mutation endpoints** beyond the mock `POST /api/commands/{name}` acknowledger; no real command handlers, idempotency persistence, or event emission.
- No WebSocket / SSE; the brief's snapshot+delta streaming is entirely absent.
- No persistence (Mongo optional and unused; world is a static file).
- No auth.

**Net:** the frontend is architected to accept a real backend for reads with minimal effort; the **backend itself is a read-only fixture echo** and the **command + real-time half of the contract is unbuilt.**

**Local runnability (resolved).** The application now runs locally from a clean
clone with standard tooling only — see [README.md](README.md) → *Local
Development*. `emergentintegrations` was removed from `requirements.txt` (it is
not on public PyPI and nothing imports it); `server.py` treats Mongo as optional
and **serves the fixture instead of crashing** when `MONGO_URL` is unset or the
database is unreachable; and Vite proxies `/api/*` to the backend so
`npm run dev` + `uvicorn server:app --reload` is the entire setup. Emergent
platform artifacts (`.emergent/`, `.gitconfig`, hardcoded HMR `clientPort`) have
been removed.

---

## 18. Production readiness assessment

**Not production-ready — and correctly so for a first-pass design generation.** It is an excellent, faithful, visually production-grade **read-only skeleton** with a clean architecture and near-perfect component discipline. It **cannot yet operate a live trading system**: there are no commands, no confirmations, no real data, no real-time, no Deployment Manifest, no third theme, and analytics/charts are placeholders. Crucially, none of these are *architectural* problems — the seams exist; the work is *building through them*. The foundation materially de-risks the road to production.

---

# Prioritised Roadmap to Production (highest value, lowest risk first)

Each item: **value · risk · why**. Grouped into phases; within a phase, do top-down.

### Phase 0 — Fidelity & cheap wins (days; very low risk, high value)
1. **Bind the hardcoded values to data** (Fleet card market state, mode banner, ContextBar breadcrumb + health chips). *High value (correctness/trust), near-zero risk.*
2. **Add the `premium-light` theme** (token set + widen the `theme` union + 3-way toggle). *Closes a hard requirement; pure token work.*
3. **Correct the matrix lens set** to `eligibility · targets · risk · recommendations · validation` (rename evidence→validation, add recommendations lens; keep confidence as a sub-display). *Trivial; restores contract fidelity.*
4. **Align enums to Track B** (`executionMode`/`operationMode` add `demo`; SystemConfidence band `healthy/caution/degraded/critical`; signal `fail/unknown`; Deployment `Error`). *Low risk; prevents downstream mismatch.*
5. **Harden `server.py`** (make `MONGO_URL` optional; don't crash without it). *Removes a boot footgun.*

### Phase 1 — Explainability + the command scaffold (1–2 weeks; low–medium risk, highest functional value)
6. **Wire the universal `WhyButton` → DecisionChain drill everywhere** (inspector renderer exists; just connect). *High explainability value, low risk.*
7. **Build `ConfirmDialog` (the only modal) + a safe command layer:** typed command callbacks named per the Command Vocabulary → `useMutation` → `api` POST stubs that **no-op/echo in mock mode** and require confirm(+reason) for destructive ones. Replace `alert()`/toasts. *This unlocks every action surface; keep it inert against live until Phase 4 rails exist.*
8. **Enforce feature-flag gating** of views + nav (`visible = env ∧ manifest`). *Cheap, high scalability value; flags already fetched.*
9. **Add the `DeploymentManifest` type + renderer + a minimal Manifest panel** (clone/export/restore/redeploy as commands from #7). *Closes the top architectural deviation; additive.*

### Phase 2 — Real analytics & charts (1–2 weeks; medium risk)
10. **Replace `PlaceholderChart` with real `ChartPanel`** (Lightweight Charts, read-only, overlay slots) on Replay first, then Analytics/Edge/Market-Data. *ChartPanel already exists; data is in the fixture.*
11. **Make Version History & Package Comparison real** (bind `packageComparisons`/package history — data is present). *Low risk; removes two placeholder pages.*
12. **Flesh out Analytics/Strategy-Health** dimensional breakdowns from trades/decisions. *Medium.*

### Phase 3 — Live backend: mutations, persistence, real-time (3–5 weeks; higher risk)
13. **Backend mutation endpoints + command handlers** (idempotency keys, event emission to an append-only log) matching the Command Vocabulary. *The other half of the contract.*
14. **Real-time layer** (WebSocket snapshot + delta; move Query off `staleTime: Infinity` for live entities). *Medium/high; the brief's live spine.*
15. **Persistence** (wire Mongo/SQLite behind the existing routes) + **flip read hooks** to per-endpoint queries. *Low risk given the seam; do incrementally per hook.*
16. **Auth/permission seam** (no-op now, structural for multi-operator). *Cheap now, expensive later.*

### Phase 4 — Scale & safety hardening (2–4 weeks; medium risk)
17. **Virtualize** DataTable + ScopeNavigator + FleetGrid (use the already-installed react-virtual). *Scalability; do before many pairs/rows.*
18. **Live-trading safety rails** around the command layer: double-arm for live, reconciliation-lock, protection vetoes surfaced, mode banner truthful. *Gate live execution on these.*
19. **Frontend tests + error boundaries + states gallery** (per-component relevant states). *Quality/maintenance.*
20. **Remove/retire unused deps; type-tighten** (`unknown[]` → real Track B types incl. Command, ReplaySession, NativeValidationResult). *Debt paydown.*

### Sequencing logic
- Phases 0–2 are **frontend-only, low-risk, high-visibility** — they take the read-only skeleton to a *complete, explainable, actionable-in-mock* product without touching live money.
- Phase 3 builds the **live backend** behind the seams that already exist.
- Phase 4 is the **hardening gate** that must pass before any real-money lane is enabled.

**Recommended first action:** Phase 0 items 1–3 (a single, low-risk fidelity pass) — they visibly raise quality and correct the clearest contract deviations before any deeper work begins.

---

## Appendix — What the generation got right (worth preserving)
The single-`Badge`/single-`DataTable`/single-`Inspector` consolidation; the `useWorld` selector seam with fixture↔API parity; token-only theming; the Number-Format Canon in one place; the parametric pair-workspace routing; and byte-identical fixtures across FE/BE/contract. **Do not refactor these — build on them.**
