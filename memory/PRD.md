# Live Trading Control Tower — PRD

**Started:** 2026-07-14
**Status:** v1 first-pass complete (Phases 1–5 of brief). Presentation-first, fixture-backed.

---

## Original Problem Statement

Web-based institutional operations platform ("Control Tower"). Presentation-first build against
the frozen `world.v1.json` fixture, architected for future backend wiring. Feels like Bloomberg
Terminal / Linear / Raycast — premium, calm, deliberate, confidence-inspiring.

Target users: trading operators, risk managers, quant ops teams running multi-broker /
multi-account / multi-pair deployments. Not retail traders.

Reference documents delivered by user:
- `CLAUDE-DESIGN-MASTER-GENERATION-BRIEF-V2-FINAL.md`
- `TRACK-B-CANONICAL-CONTRACTS.md`
- `world.v1.json` (frozen fixture, 2026-07-01T09:14:22Z)

---

## Architecture (v1)

**Stack:**
- Vite + React 18 + TypeScript + Tailwind (frontend)
- FastAPI + Motor + MongoDB (backend stub — not wired to UI yet)
- Zustand for shell state, TanStack Query for repository, TanStack Table (bundled but not yet used)
- Lucide icons, IBM Plex Sans + JetBrains Mono via Google Fonts

**Data flow:**
- `world.v1.json` bundled as static JSON in `frontend/src/data/`
- `FixtureDataProvider` abstracts it behind typed hooks: `useFleet`, `usePairWorkspace`, `usePolicyMatrix`, `useTrades`, `useBrokerHealth`, `useEdgeMonitor`, `useSystemConfidence`, `useDecisionChain`, `useRecommendations`, `useFeatureFlags`, etc.
- Backend `/api/*` routes serve the SAME shape (`/api/world`, `/api/fleet`, `/api/policy/{instrument}/matrix`, `/api/trades`, `/api/broker-health`, `/api/edge-monitor`, ...). Frontend can flip to `fetch` later with zero component churn.

---

## User Personas

- **Trading Operator (Jack)** — monitors live deployments, needs situational awareness
- **Risk Manager** — needs protection health, buffers, funded ruleset visibility
- **Quant Ops** — reviews policy matrix, drafts, recommendations, promotions

---

## Core Requirements (static)

1. Never borrow retail-terminal reflexes (chart-centric, click-to-trade).
2. Every screen answers: *"Would a risk manager trust this at 3am with real money live?"*
3. Six-verb loop: **Observe → Understand → Decide → Protect → Execute → Audit**
4. Design system: **implement, don't invent**. Tokens from §D2 of the brief exactly.
5. Reuse mandate: **one** Badge, DataTable, Inspector, Matrix, Card, ConfirmDialog.

---

## What's been implemented (2026-07-14)

### Iteration 2 (2026-07-14 · TanStack Query, Read-only Draft/Validation/Promotion, ChartPanel, Version Placeholders)
- **TanStack Query wiring**: `useSuspenseQuery({queryKey:['world'], queryFn: api.world})` in `useRepository.ts`. All 15+ hooks are pure selectors over the cached response. Suspense boundary at App root shows a CT◆ boot fallback. Verified: only 1 `/api/world` request on production cold-load (2 in StrictMode dev — collapses in prod build).
- **Fixture provider deleted**: `/app/frontend/src/data/FixtureDataProvider.tsx` removed. No component imports the JSON directly anymore. Confirmed via grep.
- **Reusable ChartPanel** (`components/domain/ChartPanel.tsx`): TradingView Lightweight Charts v4.2, deterministic synth OHLC candles, price-line markers slot (for future Trade Inspector overlays), no controls / drawing tools / annotations / click-to-trade. Used by ReplayView. Ready for reuse by Trade Inspector.
- **Read-only Draft / Native Validation / Promotion in Policy Engine rail**: 6-card stack — Active Package (Edit disabled), Recommendations, Drafts (Validate + Promote disabled), Native Validation (latest, showing nDecided/Δ netR/stability), Promotion Ladder (5-stage indicator + version history + Deploy disabled), Live Overrides (empty stub). All disabled buttons fire a `toast.info('Read-only preview')` and carry Lock icons + cursor-not-allowed.
- **Version History + Package Comparison placeholders** (`views/VersioningViews.tsx`): nav links visible in ScopeNavigator with lock glyphs. Version History renders timeline of all packages; Package Comparison renders two side-by-side read-only VersionColumns + deferred-flow callout.
- Testing agent: 100% pass, no regressions.

### Iteration 1 (2026-07-14 · MVP)

**Foundations**
- Design tokens (Console Dark + Midnight Navy variants; token swap via `data-theme`)
- Typography: IBM Plex Sans, JetBrains Mono, tabular-nums for all numerics
- Number-Format Canon (`src/lib/format.ts`): fmtR, fmtMoney, fmtPercent, fmtHash, fmtRelative, TimestampUTC
- FixtureDataProvider + 15 typed hooks
- Matrix auto-expansion: 4 seed cells → deterministic 144-cell matrix

**Tier 1 Primitives**
- Badge (variant-driven: 12+ variants — status/validation/target/risk/marketState/health/lane/recommendation/eligibility/mode/ghost/live/draft)
- Button + IconButton (primary/secondary/ghost/outline/danger)
- HealthDot, MetricStat, PLValue, RValue, ConfidenceMeter, SampleSize
- PackageVersionChip, CohortChip, MarketStateBadge, LaneChip, EligibilityBadge, ValidationBadgeChip
- TimestampUTC, KeyValueGrid, WhyButton

**Tier 2 Structures**
- Panel, Card, EmptyState, ErrorState, DiffView, DataTable (config-driven, virtualisable-ready)

**Tier 0 Shell**
- CommandSafetyBar (wordmark "CT◆" · mode banner · system confidence · Global Kill · ⌘K trigger · theme toggle · operator chip)
- ScopeNavigator (Fleet ▸ Broker ▸ Account ▸ Pair drillable tree + Global sections)
- ContextBar (breadcrumb + health chips + market state + package version)
- EventDock (collapsible bottom band, audit stream)
- InspectorHost (right drawer, entity-renderer dispatch)
- CommandPalette (⌘K, grouped commands, keyboard-first)
- AppShell (fixed skeleton, variable emphasis per §C)

**Tier 3 Domain + Inspector Renderers**
- PolicyCellRenderer, TradeRenderer, GhostTradeRenderer, BlockedIntentRenderer, DeploymentRenderer, RecommendationRenderer, DecisionChainRenderer
- DecisionChainView (shared component, vertical timeline, Downstream Influence panel)

**Flagship: Unified Policy Matrix**
- ONE component, 5 lens renderers registered (Eligibility · Targets · Risk · Evidence · Confidence)
- Frozen row/column headers, 24 cohorts × 6 market states = 144 cells
- Cell click → Inspector with PolicyCell renderer
- Lens legend adapts per lens
- Deterministic synthetic fill for cells not in fixture (real fixture-defined cells win)

**Tier 4 Views (all wired)**
- FleetOverview (grid of DeploymentCards + Attention Rail)
- PairWorkspace with tabs: Overview · Trading · Policy Engine · Analytics · Replay · Logs
- EdgeMonitorView, BrokerHealthView, AccountsProtectionView, SystemView

**Backend stub**
- FastAPI serving `/api/world`, `/api/fleet`, `/api/policy/{instrument}/matrix`, `/api/trades`, `/api/broker-health`, `/api/edge-monitor`, `/api/system-confidence`, `/api/recommendations`, `/api/decisions/{id}`, `/api/events`, `/api/feature-flags`, `/api/deployments/{id}`, `/api/packages/active`
- Shape matches the frontend hooks 1:1 — swap-in-place path is ready.

**Feature flags** — 14 flags, defaults-ON per brief; visible in System view.

---

## Prioritized backlog

### P1 (deferred first-pass)
- Wire hooks to `/api/*` via TanStack Query (flip from fixture provider). Currently components read fixture directly through provider.
- Native Validation and Promotion Ladder domain screens (currently only visible in Recommendation inspector)
- Live Overrides panel (stub present, no interaction)
- Draft edit workflow (create draft, edit cell, validate, promote) — currently read-only
- Version History Timeline / PolicyHistoryTimeline
- Package Comparison view (fixture has one but not surfaced)
- TradingView Lightweight Charts on Replay (currently synthetic bar skeleton)
- Manifest export/import UX (Deployment Manifest is described in brief §B.1)

### P2
- Real-time price tick animation on live trade rows
- Confirm+reason dialogs for promotion, override, kill
- Notification Center (toast system exists via sonner, but no center)
- Keyboard-first roving-tabindex in the Matrix grid
- Search + filter inside DataTable / Matrix (Zustand slice exists but no UI)
- Light theme (Premium Light tokens defined but not wired)
- Multi-select in ScopeNavigator for cross-lane compare

### P3
- Real chart integration
- Actual mutation flows (backend endpoints exist as stubs)
- Multi-instrument fleet (fixture is EURUSD-only, but architecture scales)
- WebSocket / SSE feed for Event Dock live updates

---

## Test credentials
No auth in v1 (presentation-first, single operator implicit from fixture).

---

## Next Actions

Awaiting user review of first-pass. Suggested next milestones:
1. Wire the fixture provider to `/api/*` endpoints (10-line change — the shape matches).
2. Implement the Draft / Promotion / Native Validation loop as the second flagship interaction beyond the Matrix.
3. Add real TradingView Lightweight Charts to Replay.
