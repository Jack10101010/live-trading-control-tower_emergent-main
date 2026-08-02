# Control Tower — Mock-Data Eradication Plan (canonical)

**Status:** Plan of record. Target state: **ZERO PRODUCTION MOCK DATA**.
**Baseline:** provenance borders + M-RISK-1 + M-CONF-1 + M-TEL-1 + M-EDGE-1 complete.
**M-ENV-1 complete** — the production boundary now exists; every remaining
milestone drains a domain behind it.

---

## 1. Executive summary

Four honesty milestones removed fabricated *judgments* one endpoint at a time. That
approach is correct but cannot reach zero, because the remaining problem is not a list
of cards — it is **two fail-open defaults and one shared fixture object**:

1. **`broker_adapter.active_kind()` defaults to `"mock"`** when
   `CONTROL_TOWER_BROKER_ADAPTER` is unset (`broker_adapter.py:357,406-412`). A
   production deploy that forgets one env var silently runs the **mock broker**.
2. **`_MD_PROVIDER` defaults to `"fixture"`** when `MARKET_DATA_PROVIDER` is unset
   (`server.py:1606`). Same failure shape for market data.
3. **`WORLD` is a process-global read by 14 production endpoints**, and `/api/events`
   *merges* fixture records with real ones in a single response (`server.py:296-299`).

Everything else is downstream of those three. The plan therefore inverts the usual
order: **build the production boundary first (fail-closed), then drain WORLD
domain-by-domain, then delete the fixture from the production import graph.**

Encouraging findings: the frontend has **no** fixture import or fallback left (the
bundled `world.v1.json` was deleted); `fixture_surfaces.py` already *derives* which
surfaces are fixture-backed rather than hand-listing them — that machinery is the
natural enforcement seam; and the durable plane (`/operations/*`, `/ledger/*`,
`/scenarios*`, execution/auth stores) is already authoritative.

---

## 2–4. Inventory, producer→UI trace, classification

`WORLD` = `fixture_world.load(FIXTURE_SEARCH_PATHS)` → `backend/fixtures/world.v1.json`.
14 endpoints read it (2 further hits are M-RISK-1/M-EDGE-1 *docstrings* only — code clean).

| # | Producer (symbol) | Shape | API | Hook | UI consumer | Operator claim | Merges real? | Authoritative replacement | Class |
|---|---|---|---|---|---|---|---|---|---|
| 1 | `WORLD.deployments` `.brokers` `.accounts` | 3 deployments, 2 brokers, 2 accounts w/ balances | `/fleet`, `/deployments`, `/world` | `useFleet` | FleetOverview tiles, DeploymentsView, ContextBar, CommandSafetyBar, AccountsProtection | "3 active deployments · $100,000 balance" | no | `/operations/{nodes,accounts,positions}` | **C** |
| 2 | `WORLD.liveTrades` `.ghostTrades` | trades w/ R values, entry/SL/TP | `/trades`, `/world` | `useTrades` | Pair trades/orders tabs, Pair Health counts | "+$412 daily P/L, 1 open" | no | `/ledger/*` + `/operations/{orders,positions}` | **C** |
| 3 | `computeMetrics(useTrades)` (`lib/analytics.ts`) | expectancy, equity curve, PF | — (client) | `useTrades` | AnalyticsView ×5 panels | performance history | derived-from-fixture | recompute from ledger closes | **C** (after #2) |
| 4 | `WORLD.blockedIntents` | 5 intents | `/world` | `useTrades` | Blocked-intent panels | "why we didn't trade" | no | node telemetry | **B** |
| 5 | `WORLD.marketStateSnapshots` | state, confidence 96%, model `regime@2.3.0` | `/world` | `useMarketState` | Market State cards (pair + market-data), ContextBar badge | "Bull/Expand 96%" | no | node-published market state | **B** |
| 6 | `WORLD.brokerHealth` | latency/health rows | `/broker-health`, `/world` | `useBrokerHealth` | BrokerHealthView | broker connectivity | no | real MT5 health | **B** |
| 7 | `WORLD.recommendations` `.drafts` `.decisionChains` `.nativeValidations` | recs w/ evidence, p-values, sample sizes | `/recommendations`, `/decisions/{id}`, `/world` | `useRecommendations`, `useDecisionChain`, `useDrafts` | EdgeMonitor pipeline panel, Recommendation inspector, Decision-chain inspector | "n=67, P=0.94, NATIVE" | no | durable `/trade-recommendations*` (exists) | **C/D** |
| 8 | `WORLD.packages` `.packageComparisons` | package registry, cells, versions | `/packages`, `/policy/{i}/matrix`, `/world` | `usePackages`, `usePolicyMatrix`, `useActivePackage` | PolicyEngine ×6, Versioning ×5, StrategyPackages, SystemView, package tabs | policy truth, cell evidence | no | real package registry (unbuilt) | **B** |
| 9 | `WORLD.events` | 3 seed events | `/events` (**merged**) | `useEvents` | Journal feed, Event dock | audit stream | **YES — merged with events.db** | runtime events only | **D** |
| 10 | `WORLD.replaySessions` | replay sessions | `/world` | `useReplaySessions` | ReplayView ×3 | replay state | no | real replay engine | **E/F** |
| 11 | `WORLD.operators` `.vocabulary` | operator profile, enums | `/world` | `useOperator`, `useVocabulary` | Settings identity, label vocab | identity/labels | no | operator prefs (real) / static config | **C** (operator) / **E** (vocabulary) |
| 12 | `WORLD.signals` `.decisionChains` seeds | signals | `/world` | — | Decision inspector | decision lineage | no | node decisions | **B** |
| 13 | `MockBroker` (`broker.py:105`) | positions/orders/account | `/broker/*`, `/operations/*`, `/execution/*` via adapter | many | Operational Dashboard, LiveRuntime, TradeLedger, execution routes | **entire operational plane when mock is active** | no | real MT5 adapter | **F + boundary** |
| 14 | `market_data` `FixtureProvider`/`MockLiveProvider`/`ReplayProvider` | synthetic candles | `/market/candles`, `/market/snapshot` | `useMarketCandles` | Charts + derived overlays (OB/FVG/liquidity/structure) | price history | no | mt5/polygon/store | **F + boundary** |
| 15 | `strategy.PolicyAlignmentStrategy` | decisions w/ blended score over fixture cells | `/strategy/evaluate` | `useStrategyEvaluation` | SystemView Strategy Engine panel | "3 fired · 2 hold" | fixture-fed | node decides (I-7) | **A/B** |
| 16 | `_MOCK_AUTHORIZATION` (`server.py:1343`) | mock authz provider | command routes | — | command surfaces | authorization | no | `DurableOperatorAuthorizationProvider` (exists) | **E** |
| 17 | `/feature-flags` hardcoded dict (`server.py:4574`) | 17 booleans | `/feature-flags` | `useFeatureFlags` | FeatureGate, Settings, System | module availability | no | env/manifest config | **B** |
| 18 | `broker_sync` fault injection | ghost positions, mismatches | `/broker/sync` | — | reconciliation drills | drill data | no | test-only | **E** |
| 19 | `PlaceholderChart` / `PlaceholderMetricGrid` | shaped voids | — | — | latency histogram, snapshot integrity, replay pinning | honestly labelled | no | node telemetry | **B (already honest)** |

**Class totals:** A=1 · B=7 · C=5 · D=2 · E=4 · F=2.

---

## 5. Final production-boundary design

A single, explicit mode gate — **the linchpin of the whole plan**.

```
CONTROL_TOWER_ENVIRONMENT = development (default) | production (explicit)
```

**Environment controls SAFETY. It is not a mode, and it adds no mode enum.**
`CONTROL_TOWER_MODE` is already taken (`security_config.py:42` — the tower's
operating mode), and `execution_mode.py` owns a third, durable vocabulary
(`observe` / `manual_live` / `halted`, operator-changed at runtime). Introducing a
fourth "mode" would repeat the naming collision this project already rejected once
(`clock_skew_seconds`, M-TEL-1).

Behaviour selection therefore stays exactly where it already lives — the existing
**per-capability** variables `CONTROL_TOWER_BROKER_ADAPTER` and
`MARKET_DATA_PROVIDER`. Environment does one thing: it declares which of their
values are **admissible**, following the `connection_policy.APPROVED_PROFILES`
idiom already proven in this repository (deny-by-default, unknown values denied
verbatim, nothing falls back).

| Question | Required answer |
|---|---|
| How is production selected? | Explicit `CONTROL_TOWER_ENVIRONMENT=production`. **Unset ⇒ `development`** (ergonomic). Production is never reached by accident because it is never the default; once selected, every capability must be explicitly real or startup aborts. |
| Can production ACTIVATE the fixture world? | **No.** `fixture_world.load()` raises `EnvironmentViolation(fixture_world_activation_forbidden)` under production, and `server.py` never calls it there — no fixture bytes enter the process. |
| Can production routes access WORLD? | **No.** `WORLD` is an *unavailable* `FixtureWorld` in production, so the existing FIX-2 boundary refuses all 16 fixture-backed paths with an honest `501 fixture_world_unavailable`. (`None` was rejected during implementation: it would raise `AttributeError` inside handlers — a 500, not an honest answer.) |
| Can frontend hooks fall back to fixtures? | **No** — already true (no fixture in the bundle); enforced by a build-time guard. |
| Can real and mock events merge? | **No.** `/events` splits: runtime events only in production. |
| Can a missing adapter silently activate mock? | **No.** `ACTIVE_KIND` default `"mock"` is removed; production requires an explicit real adapter or fails closed. |
| Can replay data appear in live UI? | **No.** Replay/mock market providers are refused under production mode. |
| Can demo accounts appear in live fleet? | **No.** Fleet reads only `/operations/*` after M-FLEET. |
| Can build-time env accidentally expose fixtures? | **No.** Vite build asserts no fixture module in the graph; CI greps the built bundle for sentinels. |
| What happens when authoritative data is unavailable? | The established pattern: `{schemaVersion, computed/configured:false, reason, detail}` or an explicit `unavailable` surface — never zeros, never fixtures. |

**Rule: production fails CLOSED to explicit unavailable state; it never falls OPEN to fixture data.**

---

## 6. Dependency graph & 7. Roadmap

```
M-ENV-1 (environment boundary)    ─┬─► M-EVENTS-1 (split)        [no external dep]
                                   ├─► M-STRAT-1 (CT engine)     [no external dep]
                                   ├─► M-FLAGS-1 (config)        [no external dep]
                                   ├─► M-PKG-1  (policy→unavail) [no external dep]
                                   └─► M-STATE-1 (mkt state/broker health→unavail)
                                                 │
   demo MT5 ──► M-MT5-READ-1 ──► M-FLEET-1 ──► M-TRADES-1 ──► M-TRADES-2 (analytics)
                     │                             │
   real feed ──► M-FEED-1                          └─► M-REC-1 (durable recs)
                                                            │
                                        all above ──► M-WORLD-0 (delete fixture)
```

**Critical path to zero:** `M-ENV-1 → M-MT5-READ-1 → M-FLEET-1 → M-TRADES-1 → M-TRADES-2 → M-WORLD-0`. Everything else parallelises.

| ID | Objective | Producers removed | Replacement / unavailable behaviour | Files | Prereq | Risk |
|---|---|---|---|---|---|---|
| **M-ENV-1 ☑** *(supersedes the M-MODE-1 id)* | Environment-gated admissibility; fail-closed production | fail-open defaults (`ACTIVE_KIND="mock"`, `_MD_PROVIDER="fixture"`) | `CONTROL_TOWER_ENVIRONMENT`; development default preserved; production requires explicit real capabilities or aborts; WORLD=None in production | `broker_adapter.py`, `market_data.py`, `server.py` boot, new `environment.py`, tests | none | **Low–Med** — development path byte-unchanged, so tests/CI/`--reload` are unaffected |
| **M-EVENTS-1** | Un-merge the audit stream | `WORLD.events` seeds in `_all_events` | runtime `events.db` only; empty = "no events yet" | `server.py`, tests | M-ENV-1 | Low |
| **M-STRAT-1** | Remove CT-side decision engine from operator view | `PolicyAlignmentStrategy` output | honest unavailable; node decides (I-7) | `server.py`, `SystemView.tsx`, `strategy.py` (retain lib, drop route surface) | M-ENV-1 | Low |
| **M-FLAGS-1** | Flags from real config | hardcoded 17-key dict | env/manifest-sourced; FeatureGate copy already corrected | `server.py`, tests | M-ENV-1 | Low |
| **M-STATE-1** | Market state + broker health honest | `WORLD.marketStateSnapshots`, `.brokerHealth` | unavailable until node/MT5 publishes | `server.py`, `GlobalViews`, `PairViews`, `BrokerHealthView` | M-ENV-1 | Low |
| **M-PKG-1** | Policy/packages honest | `WORLD.packages`, `.packageComparisons` | unavailable; gated views state absence | `server.py`, `PolicyEngineView`, `VersioningViews` | M-ENV-1 | Med (widest UI surface) |
| **M-MT5-READ-1** | Real read-only adapter | MockBroker as production source | real MT5 reads; DYNAMIC panels flip GREEN | adapter wiring, `.env` | demo MT5 | Med |
| **M-FEED-1** | Real market data | fixture/mock_live/replay providers | mt5/store; charts flip GREEN | `market_data.py`, config | feed access | Med |
| **M-FLEET-1** | Fleet from operations plane | `WORLD.deployments/brokers/accounts` | `/operations/*`; empty = "no node published" | `server.py`, `useRepository`, Fleet/Deployments/Accounts views | M-MT5-READ-1 | **High** (most consumers) |
| **M-TRADES-1** | Trades/orders from ledger | `WORLD.liveTrades/ghostTrades/blockedIntents` | `/ledger/*` + `/operations/*` | `server.py`, `useTrades`, Pair tabs | broker history | High |
| **M-TRADES-2** | Analytics from real closes | `computeMetrics` over fixture trades | ledger-derived or unavailable | `AnalyticsView`, `lib/analytics.ts` | M-TRADES-1 | Med |
| **M-REC-1** | Recommendations from durable store | `WORLD.recommendations/drafts/decisionChains` | existing durable store | `server.py`, inspectors, EdgeMonitor pipeline panel | M-ENV-1 | Med |
| **M-WORLD-0** ☑ | Delete the fixture from production | `WORLD` itself | moved to `_fixtures/` + dev-only lazy loader | ☑ `backend/fixtures/world.v1.json` deleted · ☑ `/api/world` → `/api/dev/fixture-world` · ☑ `WORLD` global gone · ◐ `fixture_world.py` still imported for the `unavailable()` response contract (see note) | all above | **done** |

> **M-WORLD-0 note.** Three of the four stated conditions are met outright. The fourth — *`fixture_world.py` → dev/test only* — is met in EFFECT but not literally: the module owns both the dev-only loader AND `unavailable()`, the canonical honest-refusal body that ordinary routes return when a fixture-backed surface is asked for. That contract is genuine runtime behaviour, so the module legitimately remains imported. Splitting it would move one function for the sake of a sentence. The loader itself is unreachable from any ordinary path, which is what the condition was protecting.

Each milestone: one coherent domain, one reviewable diff, tests + honest-contract guards, one clean commit. Commit-message template follows the M-EDGE-1 form (producer severed · consumers updated · replacement or unavailable · tests · scoreboard ☑).

---

## 8. Enforcement strategy (CI gate)

1. **Import-graph guard (backend):** under `CONTROL_TOWER_MODE=production`, importing `fixture_world`/`fixture_surfaces` raises; a test asserts the production import graph excludes them.
2. **Route source guards:** extend the proven M-RISK-1/M-EDGE-1 pattern — per-route AST/source assertions that no production handler references `WORLD` (narrow, docstring-stripped).
3. **Sentinel sweep:** one test hits every production route with the fixture *present* and asserts no known sentinel appears (`acct_01J8Z…`, `100000.00`, `+$412`, `0.39`, `0.335`, `regime@2.3.0`, `brk_01J8Z…`, `tr_01J8Z…`). This is the single highest-value guard.
4. **Merge guard:** assert no production response contains records from both a durable store and the fixture (`/events` regression test).
5. **Mode guards:** production + unset adapter ⇒ startup refusal; production + `provider=fixture|mock_live|replay` ⇒ refusal; `active_kind()` never returns `mock` under production.
6. **Frontend build guard:** vitest asserts no `src/**` import of a fixture module; CI greps `dist/` for sentinels post-build.
7. **Unavailable-contract conformance:** shared test that every honest endpoint returns `{schemaVersion, computed|configured:false, reason, detail}` and no numeric metric fields.
8. **CI gate:** `validate.sh` grows a `mock-data` phase running 1–7; failure blocks merge.

---

## 9. Deletion list (final state)

`backend/fixtures/world.v1.json` (moved to tests), `fixture_world.py` + `fixture_surfaces.py` (dev/test-only), `/api/world` route, `WORLD` global; `MockBroker` retained **test/dev-only**; fixture market providers retained behind mode; frontend dead types (`Deployment`/`Package`/`Recommendation` fixture shapes once replaced), dead hooks (`useRepository`'s `useWorld` selectors), `computeMetrics` over fixture trades, `PlaceholderChart` instances that are replaced by real data, `strategy.PolicyAlignmentStrategy` operator surface.

---

## 10. Acceptance criteria — ZERO PRODUCTION MOCK DATA

1. No production endpoint reads `WORLD` (source guards, all routes).
2. No operator-facing frontend path consumes fixture state (import guard + bundle grep).
3. No response merges real and fixture records.
4. Every unavailable subsystem returns an explicit honest contract — no zeros, no defaults resembling truth.
5. Demo/replay/test data reachable **only** under an explicit non-production mode that production refuses to select.
6. `CONTROL_TOWER_ENVIRONMENT=production` makes every fixture/mock/replay capability value **inadmissible** (startup aborts with a named reason); unset environment yields `development`, which is loudly disclosed and cannot reach a real broker.
7. Sentinel sweep clean across every production route with the fixture present on disk.
8. Production build artifact contains no fixture data.
9. CI gate enforces 1–8; reintroduction fails the build.
10. Provenance borders show **no RED** on production surfaces (the temporary system can then be retired).

---

## 11. Recommended first milestone

**M-ENV-1 — environment-gated admissibility (supersedes M-MODE-1).** It is the only
milestone that *prevents* regression rather than cleaning one instance, it unblocks five
parallel milestones, needs no external infrastructure, and it fixes the two genuinely
dangerous defects found in this audit (mock broker and fixture market data as **silent
defaults**). Until it lands, every other milestone can be undone by one unset variable.

### Revised M-ENV-1 definition (replaces the earlier M-MODE-1 design)

**Environment model.** One new variable, one axis, two values:
`CONTROL_TOWER_ENVIRONMENT = development` (default when unset) `| production` (explicit).
It is a *safety posture*, not a mode. No new mode enum is introduced: the tower already
has `CONTROL_TOWER_MODE` (`security_config.py:42`) and the durable, operator-governed
`execution_mode` (`observe`/`manual_live`/`halted`). Behaviour continues to be selected
by the existing per-capability variables.

**Mode model (unchanged).** `CONTROL_TOWER_BROKER_ADAPTER` and `MARKET_DATA_PROVIDER`
keep their current meaning and values. `execution_mode` is untouched. Environment only
declares which of their values are **admissible** — the `APPROVED_PROFILES` idiom from
`connection_policy.py`.

| | development (default) | production (explicit) |
|---|---|---|
| Broker adapter | `mock` allowed; current default preserved | must be explicitly real; `mock` **inadmissible** |
| Market provider | `fixture`/`mock_live`/`replay` allowed; current default preserved | must be `mt5` — the **only** production-admissible provider registered in the engine; fixture/mock_live/replay **inadmissible**. (`store` and `polygon` are `DataService` candle *sources*, not engine providers, and are therefore *unknown* provider values.) |
| WORLD | loaded and available | not loaded; an *unavailable* world, so fixture-backed routes answer honest `501` |
| Replay | allowed | forbidden |
| Startup with nothing set | works (`uvicorn --reload`, tests, CI unchanged) | **aborts** with a named reason per missing/inadmissible capability |

**Startup semantics.** Resolve environment → if `development`, behave exactly as today
(zero behavioural change). If `production`, validate every capability against the
admissibility policy and **abort with an explicit, named reason** on the first
violation (`broker_adapter_inadmissible: mock`, `market_provider_inadmissible: fixture`,
`broker_adapter_unset`, …). Environment is disclosed on `/api/health` so the UI can
render it prominently, and logged once, loudly, at boot.

**Why unset ⇒ development is safe.** Production is never reached by accident because it
is never a default: forgetting the variable does not yield production-with-fixtures, it
yields a development instance that is *visibly* non-live (RED provenance borders, honest
`computed:false` contracts, adapter kind on `/health`). The failure is visible rather
than silent — the opposite of today's defect, where a production deploy silently gets a
mock broker. Fail-closed is preserved *within* production, which is where it matters.

**M-ENV-1 acceptance criteria.**
1. `CONTROL_TOWER_ENVIRONMENT` unset ⇒ `development`; every existing workflow
   (`uvicorn --reload`, full test suite, `validate.sh`, CI) passes unchanged.
2. `production` + unset broker adapter ⇒ startup abort, named reason.
3. `production` + `CONTROL_TOWER_BROKER_ADAPTER=mock` ⇒ abort (mock inadmissible).
4. `production` + `MARKET_DATA_PROVIDER` in {fixture, mock_live, replay} or unset ⇒ abort.
5. `production` ⇒ the fixture world is never loaded (`WORLD.available is False`), every fixture-backed route answers `501 fixture_world_unavailable`, and any call to `fixture_world.load()` raises.
6. `active_kind()` can never return `mock` under production; no silent fallback anywhere.
7. Environment is reported on `/api/health`; a development instance says so explicitly.
8. Admissibility is data (a frozenset per environment), not scattered `if` branches, so
   later milestones extend it without touching boot logic.
9. Tests cover every cell of the table above, including "development is byte-unchanged".
10. `execution_mode`, `connection_policy` and `CONTROL_TOWER_MODE` semantics are untouched.

**Why "mode" was rejected as the naming axis.** `CONTROL_TOWER_MODE` already exists
(`security_config.py:42`) and means the tower's own operating mode; `execution_mode`
already owns the durable, operator-governed execution state (`observe` / `manual_live` /
`halted`) derived from an append-only transition log; `connection_policy` owns the
outbound profile. A fourth "mode" would have collided with all three — the same
vocabulary collision this project rejected in M-TEL-1. Environment is an
**admissibility policy**, not a behaviour selector: the existing per-capability
selectors keep choosing behaviour, and environment only declares which of their values
may be used.

**Production behaviour while WORLD-backed endpoints still exist.** A production process
starts, serves `/api/health` with `environment: production`, and answers every one of the
16 derived fixture-backed paths with `501 fixture_world_unavailable` — the honest
"no production source" contract, not fabricated data and not an empty 200. Each of those
domains is retired by its own downstream milestone; none of them is widened here.

**Downstream milestone ordering is unchanged.** M-ENV-1 remains the prerequisite for
M-EVENTS-1, M-STRAT-1, M-FLAGS-1, M-STATE-1, M-PKG-1 and M-REC-1, in the order already
recorded in §7. No downstream milestone was started.

### M-ENV-1 — as implemented

| | development (unset default) | production (explicit) |
|---|---|---|
| Broker adapters | `mock`, `mt5` | `mt5` only |
| Market-data providers | `fixture`, `replay`, `mock_live`, `mt5` | `mt5` only |
| Fixture world | loaded | never loaded; `available == False` |
| Replay | permitted | forbidden |

Canonical authority: `backend/environment.py` — the only module that parses
`CONTROL_TOWER_ENVIRONMENT` (enforced by a test). Startup validation runs at
`server.py` module scope before the Mongo connection, before the fixture world and
before any provider registration, so a rejected configuration exits import with a
named `EnvironmentViolation` and never mounts a route or starts a loop.

Two capability seams provide additive defence behind that gate:
`broker_adapter.get_adapter()` refuses an inadmissible kind at the single construction
path (so an explicit `get_adapter("mock")` cannot smuggle a mock broker into
production), and `fixture_world.load()` raises `fixture_world_activation_forbidden`
so a future call site cannot reintroduce fixture data through a side door.

Reason codes: `environment_invalid: <value>`, `broker_adapter_unset`,
`broker_adapter_inadmissible: <value>`, `broker_adapter_unknown: <value>`,
`market_provider_unset`, `market_provider_inadmissible: <value>`,
`market_provider_unknown: <value>`, `fixture_world_activation_forbidden`.

Health reports `environment` (authenticated body only, per ARCH-3), and `backendMode`
/ `dataSources.world` became DERIVED rather than the constant `"fixture"` — asserting
"fixture" in a process where the fixture world is not active would itself be a
fabrication.


---

## M-PREVIEW-DELETE-1 ☑

Eighteen fixture-backed routes → **eight**, every one under `/api/dev/` and
named `fixture`. Six deleted (no consumer), five renamed, five severed from the
fixture and kept as the operational endpoints they always were. No alias, no
redirect, no hidden query-parameter switch; negative contract tests assert the
deleted paths stay 404.

The remaining asset question is now isolated from runtime architecture: see
`RUNTIME-SOURCE-BOUNDARY.md`.
