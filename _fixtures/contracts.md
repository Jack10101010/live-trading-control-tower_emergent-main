# Live Trading Control Tower — Track B: Canonical Contracts (v1)

**Status:** the single source of truth for entities, identifiers, commands, formats, and cross-system contracts shared by Claude Design, Claude Code, the backend, replay, and analytics. Architecture is frozen (Master Report · R2 · Design Spec · R3 · Brief V2 · Readiness Review). This document defines *contracts only* — no UI, no code, no visual design.
**Conventions apply document-wide.** Where a field's meaning is defined once (e.g. audit model, ID convention), it is not repeated per entity.
**Companion:** `fixtures/world.v1.json` — one coherent mock universe instantiating every contract here.

---

## 0. Reading guide & mutability legend

Each entity is defined with: **Purpose · Owner · Identifier · Lifecycle · Fields (mutability-tagged) · Relationships · Versioning/Immutability · Audit.**

Mutability tags: **`I`** immutable (set once at creation, never changed) · **`M`** mutable (may change over the entity's life, each change audited) · **`D`** derived (computed, never stored authoritatively; shown for clarity).

Owner = the single service authorised to write the entity. All other services read only. This keeps writes single-sourced (essential for cloud/distributed and for reconciliation integrity).

---

## 1. Global conventions

### 1.1 Identifier convention (frozen)
All surrogate identifiers are **`{prefix}_{ULID}`** — a typed prefix + a 26-char Crockford-base32 ULID. ULIDs are lexicographically sortable by creation time, collision-safe, and independently generatable across distributed services (no central sequence). No entity invents its own id style.

| Prefix | Entity | Example |
|---|---|---|
| `pkg` | StrategyPackage (version) | `pkg_01J8Z6QK3E9R7F2M4C0AB5D6EF` |
| `dpl` | Deployment | `dpl_01J8Z7R2M9K4E1P3T5V7W9X0YZ` |
| `tr` | LiveTrade | `tr_01J8ZC4H2N8M6K1E3R5T7V9W0XZ` |
| `gh` | GhostTrade | `gh_01J8ZC9F5P2Q7R4T6V8X0Z1A3BC` |
| `blk` | BlockedIntent | `blk_01J8ZCD7K3M9N1P4R6T8V0X2Z4A` |
| `dec` | TradeDecisionChain | `dec_01J8ZCH2E5R8T1V4X7Z0A3C6D9F` |
| `sig` | Signal | `sig_01J8ZC1A4D7G0K3N6Q9T2W5Z8B` |
| `rec` | Recommendation | `rec_01J8ZB3C6F9K2N5Q8T1W4Z7A0D` |
| `drf` | Draft | `drf_01J8ZB8H1M4P7S0V3Y6B9E2G5J` |
| `nv` | NativeValidationResult | `nv_01J8ZBK5R8U1X4A7D0G3J6M9P2S` |
| `ev` | Event | `ev_01J8ZD0M3P6S9V2Y5B8E1H4K7N0` |
| `cmd` | Command | `cmd_01J8ZD5R8U1X4A7D0G3J6M9P2ST` |
| `op` | OperatorSession/Operator | `op_01J8Z5A2C4E6G8J0M2P4R6T8V0XZ` |
| `acct` | Account | `acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F` |
| `brk` | Broker (connection) | `brk_01J8Z3E5G7J9M1P3R5T7V9X1Z3B` |
| `rs` | ReplaySession | `rs_01J8ZE2G5J8M1P4R7T0V3X6Z9B2D` |
| `cmp` | PackageComparison | `cmp_01J8ZF4J7M0P3R6T9V2X5Z8B1D4` |

### 1.2 Structured (non-surrogate) keys
Some identifiers are **human-readable composite keys**, deterministic and stable across systems (not ULIDs). **Key-token convention (frozen):** composite keys use the Research Lab token form, not the display enum — sessions `london · lull · newYork · ny_pm · asia · outside`, directions `long · short`, structures keep case `BOS · CHoCH`, market states keep case `BullExpand …`. (Display layers render `London`, `Long`, `Bull / Expand`; the *key* is always the token form.)
- **`cohortKey`** = `{session}:{structure}:{direction}` — e.g. `london:BOS:long` (24 per domain).
- **`policyCellKey`** = `{session}:{structure}:{direction}:{marketState}` — e.g. `london:BOS:long:BullExpand` (144 per instrument matrix). Addresses a cell *within* a package's instrument matrix.
- **`scenarioKey`** = `{instrument}:{policyCellKey}` — e.g. `EURUSD:london:BOS:long:BullExpand`. **The universal cross-system addressing key.** Every LiveTrade, GhostTrade, BlockedIntent, analytics bucket, and replay slice references a `scenarioKey`. There is **no separate "Scenario" entity** — the scenario *is* this key (per Readiness Review P1-1).
- **`packageHash`** = `sha256:{hex}` over the canonical-JSON of the package content (policy matrices + component versions + lineage, excluding the hash field itself). The integrity anchor pinned by every trade.

### 1.3 Timestamps & time
All timestamps are **ISO-8601 UTC** with `Z` (e.g. `2026-07-01T09:14:22Z`), millisecond precision where sub-second matters. Market-state day boundaries use `state_known_at` (start-of-day UTC). Never store local time; presentation derives it.

### 1.4 Versioning philosophy
Two orthogonal version axes (never conflated):
- **Contract version** — the schema/shape version of a contract (`contractVersion: "1.0.0"`). Additive changes bump minor; breaking changes bump major and run side-by-side during migration.
- **Instance version** — a monotonic integer on versioned *content* (e.g. StrategyPackage `version: 18`). A change never edits an existing version; it creates version N+1. Immutable-once-promoted.

### 1.5 Audit model (applies to every mutating action — defined once)
Every state change is produced by a **Command** and emits one or more **Events** (§ Command / Event). Append-only. Each Event carries `who` (operatorId or `system`), `when` (UTC), `causedBy` (commandId or upstream eventId), and, for state changes, `before`/`after`. Immutable and append-only tables are never updated in place. "Audit" per entity below states only what is *additional* to this baseline.

### 1.6 Canonical vocabulary (frozen enums)
Enums are **per-domain data where noted** (future-proofing for crypto/futures), loaded from a `VocabularyBundle`, never hardcoded in a component or service.

| Enum | Values | Per-domain? |
|---|---|---|
| `Domain` | `FX` · `FUTURES`(future) · `CRYPTO`(future) | — |
| `Session` (FX) | `London · Lull · NewYork · NY_PM · Asia · Outside` | **yes** (FUTURES/CRYPTO supply their own axis) |
| `Structure` | `BOS · CHoCH` | extensible per strategy family |
| `Direction` | `Long · Short` | — |
| `MarketState` | `BullExpand · BullCompress · BullChop · BearExpand · BearCompress · BearChop` | model-versioned (see MarketStateSnapshot) |
| `EligibilityAction` | `LABEL · STATE_ONLY · DIRECTION_AWARE · DISABLE` (friendly: `AlwaysAllow · BlockChop · FollowTrend · NeverTrade`) | — |
| `CellMode` | `Inherit · Custom · Block · Research` | — |
| `ValidationBadge` | `NATIVE · RESCORE · BASE · INSUFFICIENT · NOT_TESTED` | — |
| `BlockReason` | `REGIME_BLOCKED · PM_POLICY_BLOCK · COHORT_DISABLED · STATE_BLOCKED · PROTECTION_VETO` | — |
| `Lane` | `live · demo · ghost · experimental · research_forward` | — |
| `PackageStage` | `research · forward_test · demo · small_live · production · archived` | — |
| `PackageStatus` | `draft · validating · validated · active · superseded · rolled_back · archived` | — |
| `DeploymentStatus` | `Armed · Waiting · InTrade · Paused · Locked · Error` | — |
| `ExecutionMode` | `mock · demo · live` | — |
| `RecommendationStatus` | `new · evidence · accepted · modified · rejected · drafted · validating · promoted · deployed` | — |
| `TradeState` | `pending · working · filled · managing · closed · cancelled · rejected` | — |
| `RR_LADDER` | `0.5,0.6,0.7,0.8,0.9,1.0,1.25,1.5,1.75,2.0,2.25,2.5,2.75,3.0,3.25,3.5,3.75,4.0,4.25,4.5,4.75,5.0` | — |

---

## 2. Strategy Package (the deployable, attribution unit)

**Purpose:** the single immutable, versioned, content-hashed, deployable, rollback-able unit the entire runtime pins to. Replaces "Policy Version" as the top-level object (Readiness Review P0-1). It bundles the policy matrices with the *exact versions of every component that participated in the decision*, plus research lineage and validation.
**Owner:** Policy Engine service (authoring/promotion); produced from a Research Lab export.
**Identifier:** `packageId` (`pkg_…`) + `version` (int) + `packageHash` (sha256).
**Lifecycle:** `draft → validating → validated → active → superseded | rolled_back → archived` (`PackageStatus`), moving along `PackageStage` (`research → forward_test → demo → small_live → production`). Promotion/rollback are atomic pointer swaps; a promoted version is never edited.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `packageId` | id | I | stable across versions of the same lineage |
| `version` | int | I | monotonic per `packageId` |
| `packageHash` | hash | I | sha256 of canonical content (the integrity anchor) |
| `contractVersion` | semver | I | schema version of this contract |
| `label` | string | I | human-readable, e.g. `"EURUSD Balanced — NY discipline"` |
| `stage` | PackageStage | M | current ladder stage |
| `status` | PackageStatus | M | lifecycle status |
| `domain` | Domain | I | asset class |
| `instruments` | string[] | I | instruments this package governs (usually 1; may be several) |
| `policy.matrices` | map<instrument, PolicyMatrix> | I | 144-cell matrix per instrument (see §3) |
| `componentVersions` | ComponentVersions | I | the exact versions that define the decision (see below) |
| `researchLineage` | ResearchLineage | I | provenance back to the Lab (see §7) |
| `validation` | ValidationStatus | M | package-level native-validation rollup (cell-level lives on cells) |
| `parentVersion` | int? | I | the version this was derived from (for diffing/rollback) |
| `createdAt` | ts | I | authored time |
| `promotedAt` | ts? | M | when it became `active` |
| `promotedBy` | operatorId? | M | who promoted (human-in-the-loop) |
| `supersededBy` | int? | M | the version that replaced it |
| `notes` | string | M | operator annotations (non-authoritative) |

**ComponentVersions (immutable sub-object) — why the Package exists:**
```
componentVersions {
  marketStateModel   // e.g. "regime@2.3.0"  (EMA200/BBW/ADX model + params)
  policyEngine       // e.g. "policy-engine@1.4.0"
  executionPolicy    // e.g. "exec-policy@2.0.0" (Execution Policy v2 / Target Optimiser methodology)
  protection         // e.g. "protection@1.2.0"
  strategyBrain      // e.g. "strategy-brain@1.1.0"
  configHash         // sha256 of the raw engine config
}
```
**Relationships:** referenced by every `Deployment`, `LiveTrade`, `GhostTrade`, `BlockedIntent` (via `packageHash`), `NativeValidationResult`, `PackageComparison`, `Draft` (as base). **Versioning/Immutability:** content immutable once `status ≥ validated`; only lifecycle fields (`stage/status/promotedAt/promotedBy/supersededBy/notes`) mutate, each audited. **Audit:** promotion/rollback emit `PackagePromoted` / `PackageRolledBack` events with full component-version set.

---

## 3. Policy, matrix & cell

### 3.1 PolicyMatrix
**Purpose:** the 24×6 = 144-cell grid for one instrument; the operational representation of the research. **Owner:** Policy Engine. **Identifier:** addressed by parent package + instrument. **Immutable** within a package version.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `instrument` | string | I | e.g. `EURUSD` |
| `cohortAxis` | {sessions[],structures[],directions[]} | I | the per-domain axis set used (frozen with the package) |
| `cells` | map<policyCellKey, PolicyCell> | I | 144 entries |
| `cohortBaseTargets` | map<cohortKey, rr> | I | the base target a cell may `Inherit` |

### 3.2 PolicyCell (the atom; addressed by `scenarioKey`)
**Purpose:** the fundamental object — everything points back to a cell. Holds eligibility, target, risk, and evidence for one `cohort × marketState`. **Owner:** Policy Engine (values are immutable within a package version; drafts propose changes). **Identifier:** `policyCellKey` within the matrix; `scenarioKey` across systems.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `policyCellKey` | key | I | `session:structure:direction:marketState` |
| `cohort` | {session,structure,direction,instrument} | I | |
| `marketState` | MarketState | I | |
| `eligibility` | {action, mode, resolvedAllowed} | I | action∈EligibilityAction; mode∈CellMode; `resolvedAllowed` derived for this state |
| `target` | {rr, source} | I | rr∈RR_LADDER; source∈`cell·cohortBase·inherited` |
| `risk` | {pct, source} | I | future risk per cell |
| `evidence` | CellEvidence | I | see below |
| `recommendationStatus` | RecommendationStatus | I | snapshot at package build; live recs tracked separately |
| `provenanceRecommendationId` | recId? | I | the recommendation that set this cell, if any |

**CellEvidence:** `{ badge: ValidationBadge, sampleSize, expectancyR, winRate, profitFactor, confidence, inSampleCaveat }`.
**Relationships:** pinned by trades/ghosts/blocked via `scenarioKey`; targeted by `Recommendation`; diffed by `PackageComparison`. **Audit:** cells are immutable in a version; changes occur only by promoting a new package.

---

## 4. Entity Dictionary (runtime objects)

### 4.1 MarketStateSnapshot
**Purpose:** the leakage-safe regime reading that governs execution. **Owner:** Market State Engine. **Identifier:** `{instrument}@{stateKnownAt}` (deterministic; not a ULID). **Lifecycle:** produced daily (shifted), immutable once emitted.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `instrument` | string | I | |
| `state` | MarketState | I | one of six |
| `confidence` | float 0–1 | I | model confidence |
| `confirmed` | bool | I | unconfirmed states are **allowed, not blocked** |
| `stateKnownAt` | ts | I | start-of-day UTC boundary |
| `shiftedDays` | int | I | `1` — no same-day look-ahead |
| `source` | `engine · client` | I | |
| `modelVersion` | string | I | ties to `componentVersions.marketStateModel` |
| `components` | {ema, emaRelation, pxVsEma, bbwValue, bbwThreshold, adxValue} | I | explainability inputs |
| `asOf` | ts | I | emission time |

**Audit:** the exact snapshot that governed a fill is copied onto the trade's decision chain (never referenced by pointer alone) so replay is reproducible even if the panel is recomputed.

### 4.2 PolicyEngineDecision
**Purpose:** the runtime output that eligibility/target/risk/reasoning came from — the bridge between a Signal and a trade/blocked intent. **Owner:** Policy Engine. **Identifier:** embedded in the decision chain (`dec_…` parent). **Immutable.**

| Field | Type | Mut | Notes |
|---|---|---|---|
| `packageHash` | hash | I | which package decided |
| `scenarioKey` | key | I | the addressed cell |
| `marketStateRef` | snapshot (copied) | I | the confirmed state used |
| `eligibilityAction` | EligibilityAction | I | |
| `resolvedAllowed` | bool | I | allow/deny outcome |
| `target` | {rr, source} | I | selected target |
| `risk` | {pct, source} | I | selected risk |
| `reasoning` | string | I | human explanation ("FollowTrend + Bull/Expand ⇒ allowed; target 2.75R from cell") |
| `blockReason` | BlockReason? | I | set when `resolvedAllowed=false` |
| `ruleFired` | string? | I | the exact rule id that allowed/blocked |

### 4.3 TradeDecisionChain
**Purpose:** the defining explainability object; the ordered, per-trade "why". **Owner:** written by the runtime as the trade progresses; append-only per node. **Identifier:** `dec_…`. **Lifecycle:** grows node-by-node; sealed at Outcome.

Ordered nodes (executed): `Signal → MarketState → PolicyCell → Eligibility → Target → Risk → Protection → Execution → Broker → Management → Outcome`.
Blocked: `Signal → MarketState → PolicyCell → ReasonBlocked → RuleFired → NoTrade`.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `decisionId` | id | I | `dec_…` |
| `scenarioKey` | key | I | |
| `packageHash` | hash | I | |
| `nodes[]` | ChainNode[] | M (append) | each: `{node, status: pass·block·na, value, why, ref, at}` |
| `terminal` | `Outcome · NoTrade` | M | set when sealed |
| `downstreamInfluence` | {contributesTo[]} | D | analytics buckets / recommendation evidence this trade feeds — **not** chain nodes (a trade does not produce a recommendation) |

**Audit:** immutable per node once written; `downstreamInfluence` is derived at read time, never authoritative.

### 4.4 LiveTrade
**Purpose:** a real (or demo/mock) executed position on a lane. **Owner:** Execution Engine (mirrors broker truth). **Identifier:** `tr_…` + `clientOrderId` (idempotency) + `brokerOrderId`. **Lifecycle:** `pending → working → filled → managing → closed` (`cancelled/rejected` off-ramps).

| Field | Type | Mut | Notes |
|---|---|---|---|
| `tradeId` | id | I | |
| `clientOrderId` | id | I | idempotency key, generated before broker call |
| `brokerOrderId` | string? | M | set on ack |
| `deploymentId` | id | I | Package×Account×Pair×Lane |
| `packageHash` | hash | I | attribution anchor |
| `scenarioKey` | key | I | universal addressing |
| `lane` | Lane | I | |
| `decisionId` | id | I | → TradeDecisionChain |
| `entry/sl/tp` | price | I (plan) / M (current) | plan frozen at fill; current may move via management |
| `size` | number | M | may reduce (partial close) |
| `riskPct` | float | I | from the cell |
| `state` | TradeState | M | |
| `currentR` | float | D | live |
| `floatingPl` | money | D | live |
| `protectionStatus` | string | M | BE eligible / protected / veto |
| `management[]` | ManagementAction[] | M (append) | the Management node's timeline |
| `sessionEntered` | Session | I | |
| `openedAt/closedAt` | ts | I/M | |
| `realizedR/closePrice` | number | M | on close |
| `originalPlan` | {entry,sl,tp,rr,risk} | I | frozen at fill, for plan-vs-actual |

**ManagementAction:** `{ at, type: SL_TO_BE·MOVE_SL·MOVE_TP·PARTIAL_CLOSE·PROTECTION_EXIT·MANUAL, before, after, actor, reason? }` — append-only.
**Audit:** every management action and manual override captures before/after + actor + reason (reason mandatory for overrides).

### 4.5 GhostTrade
**Purpose:** a hypothetical trade tracked off live market data on a non-live lane — no broker order. **Owner:** Ghost/Sim engine. **Identifier:** `gh_…`. Same shape as LiveTrade **minus** broker fields, **plus** `ghostModelId` (the alternative ruleset) and `sourceSignalId`. Reuses the shared walk semantics (`ghost_outcome/ghost_r/mae/mfe/fill_delay`). Pins `packageHash` + `scenarioKey` like live, so ghost-vs-live is honest.

### 4.6 BlockedIntent
**Purpose:** a first-class record of a trade the system refused — as important as an execution. **Owner:** Policy Engine / Protection (whichever gate fired). **Identifier:** `blk_…`. **Immutable.**

| Field | Type | Mut | Notes |
|---|---|---|---|
| `blockedIntentId` | id | I | |
| `scenarioKey` | key | I | |
| `packageHash` | hash | I | |
| `lane` | Lane | I | |
| `blockReason` | BlockReason | I | typed — never just "blocked" |
| `ruleFired` | string | I | exact rule id |
| `decisionId` | id | I | → chain (blocked variant) |
| `marketStateRef` | snapshot (copied) | I | |
| `at` | ts | I | |

### 4.7 Signal
**Purpose:** the structure-detected trade idea before policy interpretation. **Owner:** Strategy Brain (detection) → handed to Policy Engine. **Identifier:** `sig_…`. **Immutable.** Fields: `{signalId, at, instrument, cohortKey, structureEvent:{type,obId,levels,timeframe}, direction, scenarioKeyCandidate}`.

### 4.8 Recommendation & RecommendationEvidence
**Purpose:** a lifecycle object proposing a Policy Cell change, carrying full research traceability. **Owner:** Edge Monitor / Target Optimiser (generation) → Policy Engine (lifecycle). **Identifier:** `rec_…`. **Lifecycle:** `new → evidence → accepted|modified|rejected → drafted → validating → promoted → deployed`.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `recommendationId` | id | I | |
| `scenarioKey` | key | I | the cell it targets |
| `proposedChange` | {field: `target·eligibility·risk`, from, to} | I | |
| `status` | RecommendationStatus | M | lifecycle |
| `evidence` | RecommendationEvidence | I | see §7 |
| `createdAt` | ts | I | |
| `actor` | operatorId? | M | who advanced it |
| `lineage` | {draftId?, resultingPackageVersion?} | M | where it went |

**RecommendationEvidence (full research traceability — §7):** `{ researchVersion, supportingStats:{expectancyDelta, pBetter, winRate, profitFactor}, sampleSize, confidence, nativeValidation:{badge, portfolioDeltas}, promotionHistory[], deploymentHistory[], evidenceSummary, sourceLinks[], explainabilityText, inSampleCaveat }`.

### 4.9 Draft
**Purpose:** the only place edits happen; never mutates live. **Owner:** Policy Engine. **Identifier:** `drf_…`. **Lifecycle:** `open → validating → promoted | discarded`. Fields: `{draftId, baseVersion, changes:[{policyCellKey, before, after, sourceRecommendationId?}], status, owner, lockedBy?, createdAt, validation?}`. **Concurrency:** `lockedBy`/optimistic version guard (inert single-operator; active multi-operator). **Audit:** promoting a draft creates package N+1 and marks contributing recommendations `deployed`.

### 4.10 NativeValidationResult
**Purpose:** confirmation on the native backtester engine vs client rescore. **Owner:** Validation service. **Identifier:** `nv_…`. **Immutable.** Fields: `{validationId, target:{scope:`cell·package`, ref}, badge:ValidationBadge, nDecided, portfolioDeltas:{netR, drawdown, stability} vs deployed & naiveBase, runRef, engineVersion, at}`.

### 4.11 Deployment
**Purpose:** a running executor = **StrategyPackage × Account × Pair × Lane**. **Owner:** Supervisor. **Identifier:** `dpl_…`. **Lifecycle:** `Armed · Waiting · InTrade · Paused · Locked · Error`.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `deploymentId` | id | I | |
| `packageHash` | hash | M | the active package (atomic swap on promotion) |
| `accountId` | id | I | |
| `pair` | string | I | instrument |
| `lane` | Lane | I | |
| `status` | DeploymentStatus | M | |
| `executionMode` | ExecutionMode | M | mock/demo/live (double-armed for live) |
| `liveEnabled` | bool | M | per-deployment live switch |
| `riskState` | RiskState | D | live |
| `lastAction` | string | M | |
| `pinnedInFlight` | map<tradeId, packageHash> | M | in-flight trades keep their signal-time package |

**Versioning:** a live package change is an **atomic `packageHash` swap**; in-flight trades stay pinned to their creation-time hash. No restart. **Audit:** `PackageDeployed`/`DeploymentPaused/…` events.

### 4.12 Account & FundedAccountRuleSet
**Account** — **Owner:** account service. **Identifier:** `acct_…`. Fields: `{accountId, brokerId, type:`demo·live·funded`, baseCurrency, timezone, balance(M), equity(M), fundedRules}`. **FundedAccountRuleSet (versioned):** `{accountSize, dailyLossLimit, maxDrawdown, ddType:`static·trailing`, trailingAnchor, profitTarget, minTradingDays, consistencyRule?, newsRestrictions, weekendHolding, maxLot, maxRiskExposure, accountTimezone, dailyResetTime}`.

### 4.13 Broker & BrokerHealth
**Broker (connection)** — **Owner:** Broker Adapter. **Identifier:** `brk_…`. Fields: `{brokerId, adapterType, venue, status:`connected·degraded·disconnected`(M), capabilities, lastHeartbeat(M), lastReconcileAt(M), credentialsRef}`. **BrokerHealth (derived, streamed):** `{brokerId, latencyMs, reconnects, executionSpeedMs, orderRejects, spread, slippage, heartbeatAgeMs, reconciliation:{clean, diff?}, syncStatus, timeline[]}`.

### 4.14 Lane (semantics, not a stored entity)
An orthogonal execution dimension on every deployment/trade/ghost/blocked/event: `live` (real broker, real money) · `demo` (real fills, fake money) · `ghost` (no broker, tracked off live data) · `experimental` (sandbox package under test) · `research_forward` (forward-test lane fed by the Lab). Lanes share one signal source + one walk core; only the execution target differs. Lane is a **scope facet and a filter**, never a duplicated screen or entity.

### 4.15 PairWorkspace (scope descriptor)
**Purpose:** the durable per-pair operational context (pair-first architecture). Not runtime state — a **scope descriptor** the UI/services resolve. Fields: `{scopeId, domain, brokerId, accountId, pair, activeDeployments[], activePackageHash, defaultLane}`. Addresses the `Fleet ▸ Broker ▸ Account ▸ Pair` path. Scales to hundreds of pairs (list mechanics are UI concerns; the contract is just the scope path).

### 4.16 OperatorSession
**Purpose:** who is operating, for audit, handoff, and (future) permissions. **Owner:** auth/session service. **Identifier:** `op_…`. Fields: `{operatorId, displayName, startedAt, lastSeenAt(M), permissions[](inert single-operator), handoffNote?(M), activeScope?}`. **Audit:** every Command carries `operatorId`; handoff emits an event. The permission seam is present now, no-op today (Readiness Review P1-5).

### 4.17 Event (append-only audit spine)
**Purpose:** the immutable record of everything that happened and why. **Owner:** Event Log. **Identifier:** `ev_…` + monotonic `seq`. **Immutable, append-only.**

| Field | Type | Mut | Notes |
|---|---|---|---|
| `eventId` | id | I | |
| `seq` | int | I | global monotonic; the WS/stream ordering token |
| `category` | `decision·order·trade·policy·risk·manual·system·error·broker` | I | |
| `code` | string | I | e.g. `TRADE_SL_MOVED`, `PACKAGE_PROMOTED`, `RECONCILE_DIVERGENCE` |
| `humanExplanation` | string | I | operator-readable "why" |
| `scenarioKey` | key? | I | when applicable |
| `packageHash` | hash? | I | when applicable |
| `who` | operatorId · `system` | I | |
| `causedBy` | commandId · eventId? | I | lineage |
| `before/after` | object? | I | for state changes |
| `at` | ts | I | |

### 4.18 Command (the action language)
**Purpose:** the only way to change state; typed, idempotent, permissioned, audited. **Owner:** API gateway → target service. **Identifier:** `cmd_…` + client `idempotencyKey`. See §5 for the full vocabulary.

| Field | Type | Mut | Notes |
|---|---|---|---|
| `commandId` | id | I | |
| `idempotencyKey` | id | I | client-generated; dedups retries |
| `name` | CommandName | I | from §5 |
| `payload` | object | I | per-command |
| `operatorId` | id | I | who issued |
| `confirmationClass` | `silent·confirm·confirm+reason` | I | |
| `reason` | string? | I | required when `confirm+reason` |
| `issuedAt` | ts | I | |
| `resultEvents` | eventId[] | D | the events it produced |

### 4.19 ReplaySession
**Purpose:** deterministic reconstruction of any window across all engines. **Owner:** Replay service. **Identifier:** `rs_…`. Fields: `{replayId, scope:{scenarioKeys[]|pair|deployment}, window:{start,end}, speed, packageHashAtTime, consumedDataHashes:map<scenarioKey,hash>, clock:`replay`, createdBy, createdAt}`. **Determinism:** `consumedDataHashes` proves exactly which market data + which package governed the replay (reproducibility). Emits the same event stream shape as live, flagged `clock:replay`.

---

## 5. Command Vocabulary (canonical action language)

Every command: **payload · permission · audit (events) · rollback**. Confirmation class in brackets. All idempotent on `idempotencyKey`; all re-validated against current safety state at execution time.

| Command | Payload | Permission | Emits (events) | Rollback |
|---|---|---|---|---|
| `DeployPackage` [confirm+reason] | `{deploymentId, packageHash}` | `deploy` | `PackageDeployed` | `DeployPackage(prevHash)` — atomic swap back |
| `PromoteDraft` [confirm+reason] | `{draftId}` | `promote` | `PackageCreated(vN+1)`, `PackagePromoted`, `RecommendationDeployed*` | `RollbackPackage` |
| `RollbackPackage` [confirm+reason] | `{deploymentId, toVersion}` | `promote` | `PackageRolledBack` | forward promote |
| `CreateDraft` [silent] | `{baseVersion, changes?}` | `editPolicy` | `DraftCreated` | `DiscardDraft` |
| `EditDraftCell` [silent] | `{draftId, policyCellKey, change}` | `editPolicy` | `DraftCellEdited` | inverse edit |
| `DiscardDraft` [confirm] | `{draftId}` | `editPolicy` | `DraftDiscarded` | none (draft only) |
| `RunNativeValidation` [silent] | `{target:{scope,ref}}` | `validate` | `NativeValidationStarted/Completed` | n/a |
| `ApproveRecommendation` [confirm] | `{recommendationId, modifiedTo?}` | `editPolicy` | `RecommendationAccepted`→draft | `RejectRecommendation` |
| `RejectRecommendation` [confirm+reason] | `{recommendationId, reason}` | `editPolicy` | `RecommendationRejected` | reopen |
| `ApplyOverride` [confirm+reason] | `{deploymentId, scenarioKey, change, ttl}` | `override` | `OverrideApplied` (auto-expiring) | `RemoveOverride` |
| `RemoveOverride` [confirm] | `{overrideId}` | `override` | `OverrideRemoved` | re-apply |
| `PauseDeployment` [confirm] | `{deploymentId}` | `operate` | `DeploymentPaused` | `ResumeDeployment` |
| `ResumeDeployment` [confirm] | `{deploymentId}` | `operate` | `DeploymentResumed` | pause |
| `KillDeployment` [confirm+reason] | `{deploymentId}` | `operate` | `DeploymentKilled` (cancel orders) | redeploy |
| `FlattenDeployment` [confirm+reason] | `{deploymentId}` | `operate` | `DeploymentFlattened` (close positions) | none (converges to flat) |
| `PausePair` / `PauseAccount` / `PauseBroker` [confirm] | `{scopeRef}` | `operate` | `ScopePaused` | resume equivalent |
| `GlobalKill` [confirm+reason] | `{}` | `safety` | `GlobalKill` (flatten+lock all) | manual re-arm |
| `SetLaneMode` [confirm] | `{deploymentId, executionMode}` | `operate` (live needs `deployLive`) | `ExecutionModeChanged` | revert |
| `MoveTradeSL/TP` [confirm] | `{tradeId, price}` | `manage` | `TRADE_SL_MOVED`/`TP` | inverse move |
| `SLToBE` / `PartialClose` / `CloseTrade` [confirm] | `{tradeId, …}` | `manage` | `TRADE_*` | n/a / none |
| `SetAutoManagement` [confirm] | `{tradeId, enabled}` | `manage` | `AutoMgmtToggled` | toggle |
| `GenerateReplay` [silent] | `{scope, window, speed}` | `read` | `ReplayCreated` | n/a |
| `AcknowledgeAlert` [silent] | `{alertId}` | `read` | `AlertAcknowledged` | n/a |
| `HandoffSession` [confirm] | `{toOperatorId?, note}` | `operate` | `SessionHandoff` | n/a |

Permissions are names on the (currently no-op) permission seam. Live-affecting commands require the double-arm (`executionMode=live` + `liveEnabled` + `deployLive` permission).

---

## 6. Number-Format Canon (freeze — no formatting inside components)

| Kind | Rule | Example | Colour token |
|---|---|---|---|
| R value | signed, 2dp, `R` | `+2.75R` / `-1.00R` | `--positive`/`--negative` by sign |
| Money | currency symbol + thousands + 2dp; negatives in negative token | `$1,240.50` / `-$310.00` | by sign |
| Percent | 1–2dp + `%` | `1.0%` / `96.2%` | contextual |
| Pips | 1dp + ` pips` | `12.5 pips` | neutral |
| Confidence | integer `%` | `96%` | maps to confidence scale |
| Probability | `P=` 2dp | `P=0.94` | neutral |
| Sample size | `n=` int | `n=67` | `--text-2` |
| Latency | int + `ms` (`s` ≥1000ms) | `42ms` / `1.2s` | health scale |
| Duration | compact `Xh Ym` / `Xm Ys` | `2h 14m` | `--text-2` |
| Drawdown | signed % of limit + buffer | `-1.8% (72% buffer)` | `--warning`/`--danger` by buffer |
| Profit / Loss | money rule | `+$412.00` / `-$180.00` | by sign |
| Risk | `%` 1dp | `1.0%` | neutral |
| Price | instrument precision, mono | `1.09420` | neutral |
| Package version | `Package v` + int | `Package v18` | `--primary` |
| Hash | 8-char mono + `…` | `7f3a91c2…` | `--text-muted` |
| UTC timestamp | mono ISO on surface; relative in UI | `2m ago` / hover `2026-07-01T09:14:22Z` | `--text-2` |
| Market state | canonical→display `Bull / Expand` + glyph | `Bull / Expand ▲` | `--ms-*` |

All numeric renders use tabular figures. Components call shared formatters; they never format inline.

---

## 7. Research Traceability (every recommendation answers WHY)

`RecommendationEvidence` (fully specified in §4.8) must always carry the complete lineage so a recommendation is auditable end-to-end:

```
Research (researchVersion, sourceLinks[])
  → Supporting statistics (expectancyDelta, pBetter, winRate, profitFactor)
  → Sample size (nDecided) + Confidence + in-sample caveat
  → Native Validation (badge, portfolioDeltas vs deployed & naive base)
  → Promotion history (when/where this change was promoted before, if ever)
  → Deployment history (lanes/accounts it has run on + realised vs expected)
  → Evidence summary + Explainability text (operator-readable "why")
```

Rules: no recommendation may reach `accepted` without `evidence.nativeValidation.badge ∈ {NATIVE, RESCORE}` **or** an explicit override reason; `INSUFFICIENT`/`NOT_TESTED` are surfaced loudly, never hidden; the thin-sample caveat travels with the number everywhere it is shown. The Recommendation Inspector renders this chain via the shared WhyButton.

---

## 8. System Confidence (operational integrity — always explainable)

**Purpose:** a top-line "can I trust the system right now?" signal for Mission-Control situational awareness. **Owner:** a System Confidence aggregator (read-only over other services). **It is a composite with an always-visible breakdown — never a bare number, and it never suppresses raw safety signals** (broker health / reconciliation remain directly visible regardless).

**Contract:**
```
SystemConfidence {
  score        // 0–100, D (derived — never authoritative)
  band         // healthy ≥85 · caution 60–84 · degraded 40–59 · critical <40
  signals[] {  // each contributing signal, individually explainable
     key,       // brokerHealth·reconciliation·marketStateConfidence·policyValidation·
                // protectionHealth·dataFreshness·executionLatency·edgeHealth·
                // replayVerification·configIntegrity
     state,     // ok · warn · fail · unknown
     weight,    // contribution weight (see philosophy)
     value,     // the underlying metric
     message,   // operator-facing explanation
     since      // when it entered this state
  }
  worstSignal   // D — the lowest-scoring signal (drives the headline)
  explanation   // D — plain-language summary of what is dragging the score
  updatedAt
}
```

**Weighting philosophy (frozen):** **safety-critical signals gate, they do not average.** Reconciliation divergence, protection failure, or a live broker disconnect force the band to **critical** regardless of the weighted average (a hard floor) — you cannot average away a safety failure. Remaining signals contribute a weighted score within the ceiling set by the worst safety-critical signal. **Degradation rules:** any `fail` on a gating signal ⇒ band ≤ critical; any `unknown` on a gating signal ⇒ band ≤ degraded (unknown is not assumed safe). **Operator messaging:** the headline always names the worst signal ("Degraded — reconciliation divergence on FTMO since 09:12"); the score is never shown without its worst-signal cause. **Explainability:** clicking any signal opens its source (Broker Health, Protection, etc.).

---

## 9. Version Comparison (Package A vs Package B)

**Purpose:** make policy evolution legible — what changed and whether it's projected better. **Owner:** Policy Engine (read model). **Identifier:** `cmp_…`. Trivial because packages are immutable + hashed.

```
PackageComparison {
  comparisonId
  a: {packageId, version, packageHash}   // e.g. v17
  b: {packageId, version, packageHash}   // e.g. v18
  cellDiffs[] {                          // only changed cells
     scenarioKey,
     eligibility: {from, to} | null,
     target:      {from, to} | null,
     risk:        {from, to} | null,
     recommendationId?,                  // the rec that drove it
     evidenceDelta: {expectancyR:{from,to}, winRate:{from,to}, badge:{from,to}}
  }
  componentVersionDiffs[] { component, from, to }   // e.g. protection@1.1.0→1.2.0
  validationComparison { a: badge/portfolioDeltas, b: badge/portfolioDeltas }
  projectedImprovement { netR, drawdown, stability, nDecided, inSampleCaveat }
  deploymentHistoryDelta { a: lanes/accounts, b: lanes/accounts }
  generatedAt
}
```

Rules: comparison is computed, never edited; `projectedImprovement` always carries its sample size and in-sample caveat; **componentVersionDiffs are first-class** — a package can differ only in a component version (e.g. a new Market State model) with identical cells, and that must be visible (this is precisely why the Strategy Package exists).

---

## 10. Fixture World (one coherent universe)

The complete instance graph lives in **`fixtures/world.v1.json`** (companion). It is a single internally-consistent universe as of **`2026-07-01T09:14:22Z`**, with realistic ULIDs, hashes, and timestamps, in which **every object references real siblings** (no isolated dummies). Summary of what it contains and how it cross-references:

- **Packages:** `pkg_…` **v18** (EURUSD, `active`, `production`, `NATIVE`, `packageHash 7f3a91c2…`) and its parent **v17** (`superseded`) — enabling a real `PackageComparison` (v17→v18 changed 3 cells: `london:BOS:long:BullExpand` target 2.5R→2.75R, `newYork:BOS:short:BearChop` eligibility FollowTrend→NeverTrade, `asia:CHoCH:long:BullChop` risk 1.0%→0.5%; plus `protection@1.1.0→1.2.0`).
- **Brokers/Accounts:** `brk_` MT5-FTMO (connected) + MT5-Demo; `acct_` FTMO-100k (funded, live) + Demo-50k.
- **Deployments:** EURUSD **live** on FTMO (`InTrade`), EURUSD **ghost**, EURUSD **experimental** (running a draft) — all pinning `packageHash` v18 except experimental (draft hash).
- **Market state:** `EURUSD@2026-07-01T00:00:00Z` = `BullExpand`, confidence 0.96, confirmed, shiftedDays 1, `regime@2.3.0`.
- **Trades:** 2 live (`tr_…`, one filled+managing with a full `management[]` and a sealed `TradeDecisionChain`), 3 ghost (`gh_…`), and **one BlockedIntent per reason** (`blk_…` × 5) each with a blocked decision chain.
- **Recommendations:** `rec_…` #1 `deployed` (drove v18's BullExpand target, full evidence, `NATIVE`, n=67, P=0.94), #2 `validating` (proposes `london:CHoCH:short:BearExpand` 1.75R→2.0R, `RESCORE`, n=22, thin-sample caveat).
- **Draft:** `drf_…` open on base v18 with 2 changed cells (owned by `op_…`).
- **NativeValidation:** `nv_…` for v18 (`NATIVE`, portfolio netR +0.18R vs deployed).
- **Broker reconciliation:** FTMO clean; one historical `RECONCILE_DIVERGENCE` event resolved.
- **Edge Monitor snapshot + SystemConfidence:** score 82 (caution) driven by `worstSignal = dataFreshness (warn)`; all ten signals populated.
- **ReplaySession + Events:** one `rs_…` over the live trade's window; an append-only `ev_…` stream with monotonic `seq`.

---

## 11. Future-Proofing Audit (final check)

Verified the contracts against every scaling axis; where a break was possible, it is fixed here.

- **Hundreds of pairs:** ✅ `scenarioKey` is instrument-qualified; packages are per-instrument (or multi-instrument via `matrices` map); `PairWorkspace` is a scope descriptor. No global enum lists pairs. List mechanics are UI.
- **Futures / Crypto:** ✅ `Session`/`cohortAxis` are **per-domain data** carried on the package (`cohortAxis` is frozen with the matrix), so a 24/7 crypto axis or an exchange-session futures axis is a data addition, not a schema change. `Domain` enum reserves `FUTURES`/`CRYPTO`. Instrument precision is per-instrument.
- **Multiple operators:** ✅ `OperatorSession` + a permission seam on every Command (no-op today) + `Draft.lockedBy` concurrency + `HandoffSession`. Multi-operator is an activation, not a retrofit.
- **Cloud / distributed services:** ✅ ULID ids (no central sequence) + single-writer ownership per entity + append-only Event log with global `seq` + idempotency keys on every command + `packageHash`/`consumedDataHashes` for reproducibility. Reconciliation stays authoritative because broker-owned state is single-sourced.
- **Replay:** ✅ `ReplaySession.consumedDataHashes` + `packageHashAtTime` + copied `marketStateRef` on chains make replay byte-reproducible even as models version forward.
- **Edge monitoring / AI recommendations:** ✅ `Recommendation` is source-agnostic (Target Optimiser today, an AI generator tomorrow) with full `RecommendationEvidence`; nothing assumes a human author.
- **New Policy Engine features / additional Market State models:** ✅ captured by `componentVersions` — a new model or engine feature is a new package version with a visible `componentVersionDiff`, never a silent change. This is the core reason the Strategy Package replaced "Policy Version."
- **Additional strategy families / structures:** ✅ `Structure` is extensible; `cohortAxis` carries the structure set per package, so a new structure family is a wider axis, not a rewrite.

**No breaking change identified that must be made later.** The one change that *had* to happen now — pinning the full Strategy Package (not a bare Policy Version) — is made here, and every trade/ghost/blocked/comparison references `packageHash`. The contracts are cleared for generation.

---

## 12. Handoff

- **Claude Design (Track A already generating):** open the policy-stage gate — the entity dictionary, `scenarioKey` addressing, Strategy Package, matrix cell shape, decision chain, recommendation, and System Confidence are now frozen. Generate the unified matrix single-lens first against `fixtures/world.v1.json`.
- **Claude Code:** wire against these contracts and the fixture world; the ID convention, command vocabulary (payload/permission/events/rollback), and number-format canon are the wiring surface. Never invent a field — if one is missing, flag it against this document.
- **Backend / replay / analytics:** these are the shared shapes; ownership tags define who writes what; the Event log + `seq` is the integration spine.
