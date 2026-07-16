# Live Trading Control Tower — Master Generation Brief (V2, Final)

**Purpose:** the definitive specification Claude Design consumes to generate the production UI. Written so the first pass lands at ~80–90% of production, minimising expensive regeneration.
**What this is not:** not React, mockups, or implementation. This is the spec Claude Design implements and Claude Code later wires.
**Authoritative & frozen:** Master Architecture Report · UI Blueprint R1/R2 · Design System Spec · Architecture Review R3 · Generation Brief Audit · **Track B Canonical Contracts + Fixture World (delivered)**. **Where any conflict exists, R3 wins.** Do not invent, simplify, or redesign architecture — implement it.
**Revision V2.1:** incorporates the **Deployment Manifest** and **Feature Flags** (§B.1) additively. Track B is now delivered (`contracts/TRACK-B-CANONICAL-CONTRACTS.md` + `contracts/fixtures/world.v1.json`), so the policy-stage gate is **open**.
**How to use this brief:** §A–§N are the spec (§B.1 = Deployment Manifest + Feature Flags). §O is the Lead-Architect final review — scalability, cloud-readiness, reuse, future-module, and final audits, plus verdict. Generate in the staged order in §M against the frozen Track B contracts + Fixture World.

---

## §A. North Star (read first)

This product feels like **Bloomberg Terminal · VS Code · Grafana · Mission Control** — an **institutional operations platform**. It is **not** MetaTrader, TradingView, cTrader, or any broker terminal, and must never borrow their reflexes (chart-centric homes, click-to-trade, discretionary tinkering).

Everything optimises for, in priority order: **situational awareness · explainability · confidence · rapid intervention · operational safety** — never trading convenience. The centre of the application is **Policy · Execution · Safety · Decision-making.** Charts are inspection instruments only.

**The six-verb purpose, reinforced by every screen:** Observe → Understand → Decide → Protect → Execute → Audit.

**One acceptance test for any screen:** *"Would a risk manager trust this at 3am, on hour nine, with real money live?"* Calmer, quieter, denser-but-legible wins.

---

## §B. Pair-First Architecture (navigation & scope)

**Bots are disposable; pairs live forever.** The UI is organised around the durable scope hierarchy, never around bots.

```
Fleet → Broker → Account → Pair → Policy → Execution Lane → Trades
```

The **Pair is the primary operational workspace.** Today only EURUSD exists; the UI must scale to unlimited pairs (GBPUSD, USDJPY, Gold, NAS100, ES, CL, NQ…) and later Futures, with zero redesign — a new pair is a new workspace *instance*, not new screens.

**Two navigation planes (do not collapse them):**
- **Scope Navigator (left):** a drillable tree `Fleet ▸ Broker ▸ Account ▸ Pair`. Selecting a Pair opens its workspace. Multi-select and filtering supported.
- **Global sections (cross-pair):** Fleet Overview · Edge Monitor · Broker Health · System. These are *not* nested under a pair (broker health is per-broker/account; Edge Monitor is cross-pair). Nesting them under a pair would be a scalability mistake.
- **Execution Lane** is an **orthogonal facet** (live / demo / ghost / experimental / research_forward), applied as a filter/compare at any scope level — never a duplicated screen.

**Pair Workspace tabs:** `Overview · Trading · Policy Engine · Analytics · Replay · Logs`. (Policy is scoped to the pair's **instrument**; the cohort axes are shared across instruments, only the policy *values* differ — a pair never invents its own taxonomy.)

**Scope identity (freeze this):** a running deployment is defined by an immutable **Deployment Manifest** (see §B.1) — conceptually *Strategy Package × Broker × Account × Pair × Lane × Environment* plus its data-version, module, and flag pins. The same Strategy Package can run across multiple lanes and environments off one signal source; that is what makes cross-lane and cross-environment comparison coherent.

---

## §B.1 Deployment Manifest & Feature Flags

**Deployment Manifest — the single source of truth for a running deployment.** Every running deployment is defined by one **immutable, content-hashed Deployment Manifest**, so a deployment can be **cloned, exported, restored, or replayed with complete fidelity**. The Manifest **wraps** the Strategy Package — it never replaces it. The Strategy Package stays the immutable *research artifact* (policy matrices + component versions); the Manifest is the immutable *runtime-instance definition*.

```
DeploymentManifest {                        // immutable · content-hashed (manifestHash)
  manifestId · manifestHash · createdAt · createdBy · clonedFrom?
  strategyPackage { packageId, version, packageHash }   // WRAPS the frozen package
  broker        (brokerId)
  account       (accountId)
  pair          (instrument)
  lane          (live | demo | ghost | experimental | research_forward)
  environment   (Local | VPS | Demo | Live | Experimental)
  marketDataVersion        // pins md source/version — fidelity for live & replay
  replayDataVersion        // pins the replay dataset — byte-reproducible replay
  enabledModules[]         // which workspaces/engines are active for this deployment
  featureFlags: map<Flag, bool|variant>
  metadata { label, notes, tags[] }
}
```

The runtime **Deployment** (Track B §4.11) is an *instance* of a Manifest: it references `manifestId · manifestHash` and carries only mutable runtime state (status, executionMode, liveEnabled, riskState, lastAction, pinnedInFlight). Runtime records (trade / ghost / blocked / event) still pin `packageHash` for policy attribution **and additionally carry `manifestHash`** for full-fidelity replay (which market data, which flags, which environment governed them).

> **Conflict & smallest change (vs frozen Track B §4.11).** Track B's `Deployment` inlines `packageHash · accountId · pair · lane`; the Manifest now owns those. **Smallest change:** those four become **derived-from-manifest** (a read cache on `Deployment`), and `Deployment` gains `manifestId · manifestHash`. No entity is removed, no attribution changes (`packageHash` still pins policy), and every existing contract and fixture remains valid. Additive, backward-compatible.

**Feature Flags — modules are configured, not hardcoded.** Module enablement is **data**, resolved from two scopes: **environment/operator flags** (does this install/VPS expose the module at all) and **manifest flags** (is it active for this deployment). The UI computes `visible = environmentEnabled ∧ manifestEnabled` and **adapts automatically** — a disabled module contributes no workspace, nav entry, or affordance; an enabled one mounts them. Adding a future module is *configuration + a workspace + contracts*, never an architectural change.

Canonical flags (extensible): `ghostTrading · replay · edgeMonitor · researchForward · brokerHealth · notifications · analytics · newsIntegration · experimentalFeatures · aiRecommendations`.

Flags are read-only in the UI except through an explicit, audited config command, and **never silently alter safety-critical behaviour** — safety modules (protection, reconciliation, global kill) cannot be flag-disabled on a live lane.

---

## §C. Universal Page Template (freeze one layout)

Every workspace uses the **same skeleton**. Consistency is mandatory; only the Primary Workspace content changes.

```
┌ Command & Safety Bar (global chrome — mode banner, global kill, health, clock, ⌘K)
├ 1. Title
├ 2. Context Bar        (scope breadcrumb: Fleet ▸ Broker ▸ Account ▸ Pair ▸ Policy ▸ Lane)
├ 3. Health Strip       (PM/policy · market state · broker · risk health chips)
├ 4. Primary Workspace  (the workspace's main content)
├ 5. Secondary Panels   (supporting, collapsible)
├ 6. Inspector          (right drawer — selected entity + Decision Chain)
└ 7. Event Dock         (bottom — live audit stream, filtered to scope)
```

**Nuance (Lead-Architect note, binding):** the template is a **consistent skeleton with variable emphasis**, not a procrustean grid. A workspace may expand/collapse bands (Replay promotes the chart into the Primary Workspace and minimises Secondary Panels; the Policy Matrix fills the Primary Workspace edge-to-edge) — but **never removes** the global chrome, Health Strip, Inspector host, or Event Dock. Structure is fixed; proportion flexes.

---

## §D. Design System — IMPLEMENT, do not invent

Claude Design **implements the tokens below exactly.** It does not design a palette, a scale, or a motion system. Any literal colour/size/radius in a component (not referencing a token) is a defect. Themes are token swaps; components never name a theme.

### D1. Colour rules (non-negotiable)
1. **Green/red are reserved for P/L outcome and success/danger only.** Never for market direction or navigation.
2. **Market State uses a distinct hue family** (bull = cool teal/cyan; bear = warm violet) so "bull" never reads as "good."
3. **State is triple-encoded: colour + label + glyph.** Never colour alone (accessibility + calm).
4. **Institutional, not neon.** No glow, no gradients-as-decoration, no glassmorphism.

### D2. Semantic colour tokens (three themes — implement these values)

| Role token | Console Dark | Midnight Navy | Premium Light |
|---|---|---|---|
| `--bg` | `#0E1116` | `#0A0F1E` | `#F5F4F1` |
| `--bg-elevated` | `#12161D` | `#0E1426` | `#FBFAF8` |
| `--panel` | `#161B24` | `#111832` | `#FFFFFF` |
| `--panel-2` | `#1C222D` | `#16203F` | `#F2F1EC` |
| `--panel-3` | `#232A37` | `#1D294C` | `#E9E7E1` |
| `--text` | `#E7EAF0` | `#DBE2F1` | `#1A1F27` |
| `--text-2` | `#A7AFBE` | `#96A2C0` | `#48505E` |
| `--text-muted` | `#6E7683` | `#626E8C` | `#78808E` |
| `--border-subtle` | `#222A35` | `#1A2340` | `#E7E5DF` |
| `--border` | `#2C3542` | `#24304F` | `#D9D6CF` |
| `--focus` | `#4C82F7` | `#5B8DEF` | `#2B6CF0` |
| `--primary` | `#4C82F7` | `#5B8DEF` | `#2B6CF0` |
| `--on-primary` | `#FFFFFF` | `#FFFFFF` | `#FFFFFF` |
| `--selection` | `rgba(76,130,247,.14)` | `rgba(91,141,239,.16)` | `rgba(43,108,240,.10)` |
| `--positive` | `#3FB27F` | `#46B487` | `#1E9E6A` |
| `--negative` | `#E5565B` | `#E86A6E` | `#D23B3B` |
| `--neutral` | `#8B93A1` | `#8790A8` | `#78808E` |
| `--warning` | `#E0A030` | `#E3A63C` | `#B8791E` |
| `--info` | `#4C82F7` | `#5B8DEF` | `#2B6CF0` |
| `--bull` (market) | `#2FB6C9` | `#37AEC0` | `#127C8C` |
| `--bear` (market) | `#C07BE0` | `#B98BE4` | `#7A3FA0` |
| `--draft` | `#E0A030` | `#E3A63C` | `#B8791E` |
| `--validated` | `#35B0A7` | `#3BB0A8` | `#12857C` |
| `--ghost` | `#8E8AC0` | `#8E8AC8` | `#6A66A0` |
| `--blocked` | `#B05867` | `#B7616F` | `#9A3D4C` |
| `--paused` | `#9AA0AB` | `#8790A8` | `#8A8F98` |
| `--live` | `#E5565B` | `#E86A6E` | `#D23B3B` |
| `--healthy` | `#3FB27F` | `#46B487` | `#1E9E6A` |
| `--recommendation` | `#7C6CE4` | `#8577E8` | `#6355C8` |
| `--mode-mock` | `#64748B` | `#5E6C8C` | `#6B7280` |
| `--mode-demo` | `#E0A030` | `#E3A63C` | `#B8791E` |
| `--mode-live` | `#E5565B` | `#E86A6E` | `#D23B3B` |

**Market-state set (categorical; always with label + glyph):** `--ms-bull-expand #22B8A6` · `--ms-bull-compress #2E8FB0` · `--ms-bull-chop #4E6C8C` · `--ms-bear-expand #D98A3A` · `--ms-bear-compress #C56A55` · `--ms-bear-chop #8C6A5E` (Dark; desaturate ~10% for Midnight, darken for Light contrast).
**Eligibility:** `--elig-always-allow`=positive · `--elig-follow-trend`=primary · `--elig-block-chop`=warning · `--elig-never-trade`=text-muted · `--elig-custom`=recommendation.
**Validation badges:** `--valid-native`=validated · `--valid-rescore`=info · `--valid-base`=neutral · `--valid-insufficient`=warning · `--valid-nottested`=text-muted.
**Protection:** `--protect-ok`=positive · `--protect-danger`=warning · `--protect-veto`=negative · `--locked`=`#C33A3F`.
**Chart:** `--grid` = panel-3 tint · `--axis` = border · `--crosshair` = text-muted.

### D3. Non-colour tokens (identical across themes)
- **Spacing (4px base):** `0 · 2 · 4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 · 64`. Default panel padding 16; dense grids 8.
- **Radius:** `sm 4 · md 6 (default) · lg 8 · xl 12 · pill 9999`.
- **Borders:** hairline 1px default; 2px for focus/active. Hierarchy via surface + border, not shadow.
- **Elevation:** `0` flush · `1` panel (1px border + faint 1–2px shadow) · `2` popover · `3` dialog (scrim) · `4` reserved. Low-alpha, neutral, never coloured.
- **Opacity:** disabled `.40` · muted `.62` · hover-overlay `.06` · pressed `.12` · scrim `.55`.
- **Motion:** durations `fast 120ms · base 180ms · slow 260ms`; easing enter `cubic-bezier(.2,0,0,1)`, exit ease-in. Hover = colour/border/opacity only (no scale/bounce). Value-tick = ~400ms subtle bg tint + ▲/▼ glyph. Skeletons for content; spinners only for discrete pending actions. `prefers-reduced-motion` → opacity/instant only.
- **Focus ring:** 2px `--focus` + 2px offset, `:focus-visible` only, high-contrast in all themes.
- **Z-index scale:** base 0 · dropdown 100 · sticky 200 · drawer 300 · dialog 400 · toast 500 · command-palette 600.
- **Grid & sizing:** full-bleed app (no marketing max-width); workspace body fluid 12-col, gutters 16; Inspector drawer 380–460px; control heights 28 (sm) / 32 (md); tables row height 32 (dense) / 40 (comfortable).

### D4. Typography
UI **IBM Plex Sans** (fallback **Inter**); numeric/mono **JetBrains Mono**. Scale `11 · 12 · 13 (table base) · 14 · 16 · 18 · 20 · 24 (KPI) · 30 (rare)`; weights 400/500/600/700; line-height 1.15 numerics / 1.35 UI / 1.5 prose. **All numbers use tabular figures.** R/prices/%/pips/stats → tabular sans; IDs/hashes/timestamps/raw keys → JetBrains Mono.

### D5. Icons
**Lucide only. Never mix.** Sizes 16/20/24, stroke 1.75–2px, currentColor, decorative icons `aria-hidden`. Maintain a fixed semantic map (lock=locked, shield=protection, git-branch=package version, flask=draft, badge-check=validated, activity=live).

---

## §E. Number-Format Canon (freeze; use everywhere)

| Kind | Format | Example |
|---|---|---|
| R value | signed, 2dp, `R` | `+2.75R` · `-1.00R` |
| Money | currency + thousands + 2dp; negatives in `--negative` | `$1,240.50` |
| Percent | 1–2dp + `%` | `1.0%` · `96.2%` |
| Pips | 1dp + `pips` | `12.5 pips` |
| Confidence | integer `%` | `96%` |
| Sample size | `n=` | `n=67` |
| Probability | `P=` 2dp | `P=0.94` |
| Price | instrument precision, mono | `1.09420` |
| Package version | `Package v` + int | `Package v18` |
| Hash | 8-char mono + ellipsis | `7f3a91c2…` |
| Timestamp | UTC ISO (mono) + relative on surface | `2m ago` / hover `2026-07-01T09:14:22Z` |

All numeric renders use `tabular-nums`. Never format ad hoc — components call the shared formatters.

---

## §F. Policy Cell — the fundamental object

Everything ultimately points back to a **Policy Cell**; every trade links to its originating cell; clicking a cell opens its Inspector. Typed shape (subset of the Entity Dictionary; full dictionary is Track B, §N):

```
PolicyCell {
  id                 // stable, e.g. "EURUSD:london:BOS:long:BullExpand"
  cohort { session, structure, direction, instrument }   // session∈6, structure∈{BOS,CHoCH}, direction∈{Long,Short}
  marketState        // ∈ 6: Bull/Expand,Bull/Compress,Bull/Chop,Bear/Expand,Bear/Compress,Bear/Chop
  eligibility { action, mode, resolvedAllowed }
       // action∈{LABEL→AlwaysAllow, STATE_ONLY→BlockChop, DIRECTION_AWARE→FollowTrend, DISABLE→NeverTrade}
       // mode∈{Inherit,Custom,Block,Research}; resolvedAllowed: bool for this state
  target { rr, source }         // rr on ladder 0.5–1.0 by .1 then 1.25–5.0 by .25; source∈{cell,cohortBase,inherited}
  risk   { pct, source }
  evidence { badge, sampleSize, expectancyR, winRate, profitFactor, confidence }
       // badge∈{NATIVE,RESCORE,BASE,INSUFFICIENT,NOT_TESTED}
  recommendationStatus // ∈{none,new,accepted,drafted,validating,promoted,rejected}
  policyVersion        // ref → PolicyVersion
  draftStatus          // {clean | modified: {before,after}}
}
```

**Axes (shared, importable — never hardcoded in a component):** 6 sessions (London, Lull, New York, NY_PM, Asia, Outside) × {BOS, CHoCH} × {Long, Short} = **24 cohorts**; × **6 market states** = **144 cells** per instrument.

---

## §G. Policy Engine (central workspace) & the Unified Matrix

**Runtime decision chain (per R3 — reflect exactly):**
`Signal → Confirmed Market State → Policy Engine → Strategy Brain → Protection → Execution → Broker → Management → Outcome`.
The **Policy Engine decides** eligibility · target · risk · follow-trend · direction-aware · disabled cohorts · market-state interpretation · package version · recommendation status. The **Strategy Brain merely executes** what the Policy Engine decides — it invents nothing.

**Market State is leakage-safe (display this):** execution uses the **confirmed prior-day** state (`state_known_at` = start-of-day, `shifted_days=1`); unknown/warmup/unconfirmed states are **allowed, not blocked.** The Market State panel/badge must show state + confidence + `state_known_at` + source, and must never imply intraday state drives fills.

**Policy Engine sub-views:** Overview · Current Market State · **Unified Policy Matrix** · Decision Flow · Recommendations · Draft Policies · Native Validation · Promotion · History · Live Overrides · Policy Inspector · Version History.

**The Unified Matrix (first-class object, not a settings screen).** ONE 24×6 matrix; a **lens toggle** swaps the value shown — **never separate matrices**:
`Eligibility · Targets · Risk · Recommendations · Validation`.
Interaction (specify to Claude Design; do not let it invent): frozen row/column headers · virtualised · cell = value + evidence badge + draft-diff marker · **cell → hover peek → drill → Inspector** · lens toggle in the Context/Health strip · draft edits highlight the cell, never mutate live.

**Live editing safety (freeze):** all edits go to an immutable **Draft**; a live change is an **atomic version swap**, not a mutation; in-flight trades stay pinned to their signal-time version; **Live Overrides** are time-boxed + reason-mandatory + auto-expiring; promotion is confirm+reason with instant rollback. No affordance may look like editing the live matrix directly.

---

## §H. Decision Chain (defining feature)

Every trade and every blocked intent carries a **Decision Chain** — the backbone of explainability and a single shared component.

**Executed (ends at Outcome — do NOT extend into aggregate nodes):**
`Signal → Market State → Policy Cell → Eligibility → Target → Risk → Protection → Execution → Broker → Management → Outcome`.
**Blocked:** `Signal → Market State → Policy Cell → Reason Blocked (typed) → Rule Fired → No Trade`, where reason ∈ `REGIME_BLOCKED · PM_POLICY_BLOCK · COHORT_DISABLED · STATE_BLOCKED · PROTECTION_VETO`. **Nothing ever just says "Blocked" — it explains why.**

Each node shows its concrete values (cohort, confirmed state + `state_known_at`, package version + hash, matrix cell, rule id, protection verdict, fill, management timeline, outcome-vs-expectation). **Downstream contribution** ("this trade feeds cohort X's expectancy / recommendation R-xx's evidence") is a **separate 'Downstream Influence' panel** — not chain nodes (a single trade does not produce analytics or a recommendation). The universal **`WhyButton`** opens the Decision Chain from anywhere.

---

## §I. Component Strategy — consolidate, don't proliferate

**One `Badge` primitive, variant-driven** — not eight badges. Variants: `status · validation · target · risk · marketState · health · lane · recommendation · eligibility · mode`. Identical shape/behaviour; variant sets colour token + glyph + label source.

**One `Inspector` shell** — not many inspectors. Fixed shell (header: identity + status + package version + WhyButton → body sections → shared `DecisionChain` → related links); only the **entity renderer** changes (`TradeRenderer · OrderRenderer · PolicyCellRenderer · RecommendationRenderer · BlockedIntentRenderer · DeploymentRenderer · DeploymentManifestRenderer`).

**Component catalogue (tiered):**
- **Shell (Tier 0):** AppShell · CommandSafetyBar · ScopeNavigator · GlobalNav · ContextBar · HealthStrip · EventDock · InspectorHost · CommandPalette · NotificationCenter · ModeBanner.
- **Primitives (Tier 1):** Badge (variant-driven) · Chip · Button · IconButton · ConfirmButton · WhyButton · Toggle · Select · Input · SearchInput · FilterBar · Tabs · Tooltip · Skeleton · MetricStat · PLValue · RValue · ConfidenceMeter · SampleSize · PackageVersionChip · CohortChip · MarketStateBadge(=Badge variant) · HealthDot · TimestampUTC.
- **Structures (Tier 2):** Card · Panel · DataTable (virtualised, expandable rows) · Matrix (the unified grid) · Timeline · Drawer · Dialog · Inspector (shell) · KeyValueGrid · DiffView · ChartPanel · StatGroup · EmptyState · ErrorState.
- **Domain (Tier 3):** DeploymentCard · PolicyCell · PolicyMatrix (Matrix + lenses) · DecisionChain · TradeRow · OrderRow · RecommendationCard · RecommendationTimeline · ValidationSummary · MarketStatePanel · BrokerHealthPanel · ReconcileDiff · PromotionLadder · PolicyHistoryTimeline · DraftDiff · LiveOverridesPanel · EdgeMonitorBoard · FleetGrid · PairHeader.
- **Views (Tier 4):** FleetOverview · PairWorkspace(+tabs) · PolicyEngineView · TradingView · EdgeMonitorView · BrokerHealthView · ReplayView · AnalyticsView · AccountsProtectionView · SystemView.

Higher tiers compose lower; views are thin compositions. Before making a primitive, extend an existing one via props/variants.

**Component reuse mandate (configurable variants over duplicates — a defect to violate).** Beyond Badge and Inspector, these families are **one component, variant-driven**; generating siblings is the top consistency-and-cost regression:
- **`ActionBar`** — one quick-action bar; a variant supplies the action set (bot/deployment/trade/order/policy). Not per-entity action bars.
- **`ConfirmDialog`** — one confirmation modal for every `confirm` / `confirm+reason` command (the reason field toggles by confirmation class). Not per-command modals.
- **`HealthIndicator`** — one indicator (dot + label + tooltip) for broker / system / data / policy / protection health. Not per-domain health widgets.
- **`StatusChip`** — a Badge variant, never a separate component.
- **`DataTable`** — one config-driven table (columns + row-renderer + expansion as props) for orders / trades / blocked / events / recommendations. Not per-list tables.
- **`Card`** — one card with slots; DeploymentCard / RecommendationCard are *configurations*, not forks.

**Rule:** if a proposed component differs from an existing one only in data or labels, it is a **variant**, not a component. **Feature-flag awareness:** every View and nav entry renders conditionally on the resolved feature flags (§B.1) — a disabled module produces no component, so the flag system, not new code, controls surface area.

---

## §J. Workspace specs (Primary Workspace content per view)

- **Fleet Overview (global):** FleetGrid of DeploymentCards (pair · package version · lane · **environment** · status · market state · risk buffer · floating P/L · last action; each card resolves from a Deployment Manifest) + attention rail (alerts, stale policy, validation failures, reconciliation divergence). Answers "what needs my attention?" — **not charts.**
- **Pair Workspace › Overview:** pair health, current market state (provenance), active package version, open risk, today's allow/block posture, recent decisions. Sparklines only; no candle chart here.
- **Pair Workspace › Trading:** Pending Orders · Open Trades · Closed Trades tables; per row: Policy Cell · package version · market state · lane · target · risk · protection status · floating perf; row → Inspector (Decision Chain + Management timeline). Execution Queue + lane filter.
- **Pair Workspace › Policy Engine:** the §G workspace (Unified Matrix central).
- **Pair Workspace › Analytics:** performance breakdowns (describes what happened) — dimensional by cohort/state/lane; tagged by package version. *(flag: `analytics`)*
- **Pair Workspace › Replay:** chart-dominant; Trade/Session/Decision/Policy replay; scrub + speed; synchronized Decision Chain + Timeline. Charts inspection-only. Replay resolves the Manifest's `replayDataVersion` for byte-reproducible playback. *(flag: `replay`)*
- **Pair Workspace › Logs:** technical logs (distinct from the business Event Dock).
- **Edge Monitor (global):** EdgeMonitorBoard — expectancy · win rate · edge/distribution/feature drift · policy health · research-vs-live · ghost-vs-live · forward-test health · recommendation generation · operator confidence · future candidates. Predicts; feeds the recommendation pipeline. Not Analytics. *(flag: `edgeMonitor`)*
- **Broker Health (global):** connection · latency · heartbeat · spread · slippage · execution speed · order rejects · synchronization · **reconciliation diff** · health timeline. *(flag: `brokerHealth`)*
- **Accounts & Protection (global):** funded rule sets, RiskState, buffers, lockouts, overrides (reason-gated).
- **System (global):** engine health · technical logs · storage · market-data feeds · **Deployments & Manifests** (clone / export / restore / redeploy) · **Feature Flags** (environment + per-deployment) · settings.

**Feature-flag gating (§B.1):** workspaces marked *(flag: …)* mount only when their flag resolves enabled; disabled ones vanish from nav with no placeholder. Ghost/experimental/research_forward surfaces appear as **lane filters/compares** (flag `ghostTrading` / `researchForward`), never separate screens.

---

## §K. Charts & Execution

**Charts:** TradingView **Lightweight Charts**, **read-only** — no click-to-trade, no drag SL/TP, no discretionary execution. Base price layer + overlay layer (market-state ribbon, cohort/structure markers, eligibility shading, target-by-state levels, execution + manual-action markers, ghost + blocked markers). Charts live on dedicated pages (Replay/Analytics), never the Overview. **TradingView Desktop remains an optional external discretionary companion** — unlinked; the Control Tower never depends on it for UI or execution.
**Execution is broker-first:** intents flow Policy Engine → Strategy Brain → Protection → Execution Engine → Broker. TradingView alerts are **not** the execution architecture; they may *later* be an optional signal source/companion only.

---

## §L. States, interaction, responsive, accessibility

**Per-component relevant states (not all states on all components):**
- Primitives: `default · hover · selected · disabled · loading`.
- Data components: + `empty · error · updating`.
- Policy/domain: + `draft · live · promoted · validation-failed · blocked`.
- System-level (design once): cold start (no policy loaded) · no broker connected · market-state unknown/warmup · reconciliation-divergent (bot locked).
Generate **one primitives states gallery**; do not restate all states on every composite.

**Interaction quality:** hover (colour/border only) · expandable table rows · Inspector slide-in (base 180ms) · panel collapse · selection via `--selection` · skeleton loading · toast notifications. Fast, predictable, quiet.

**Responsive (desktop-first, 3 breakpoints):** `≥1680` dense (all bands visible) · `1280–1680` default (Secondary Panels collapsible) · `<1280` laptop-compact (Inspector → overlay drawer; Matrix → horizontal scroll with frozen headers; multi-panel → stack). Mobile is not a priority but must not break. Never remove the Safety Bar, Health Strip, or Event Dock.

**Accessibility:** WCAG **AA** in all three themes (verify Midnight/Light) · full keyboard (focus-visible, roving-tabindex grids, ⌘K) · rem scaling 90–130% · reduced-motion honoured · **never colour-alone** (triple-encode) · long-session ergonomics (Midnight, no pure white/black, restrained saturated fills).

---

## §M. Staged Generation & Parallel Tracks

**Generate in this order (each frozen after visual approval before the next depends on it):**

1. **Design tokens · themes · typography · spacing · colour system · States Gallery.**
2. **Shell:** navigation · Scope Navigator · Command & Safety Bar · Health Strip · Context Bar · Event Dock · Inspector host.
3. **Primitive components.**
4. **Structural components.**
5. **Market-data workspaces** (contracts already built: `md.bar/tick/news`, BarStore) — Replay/Analytics shells, charts, feed health.
6. **— Entity contracts FROZEN: Track B Canonical Contracts + Fixture World (DELIVERED). Gate OPEN. —**
7. **Policy Engine prototype — the Unified Matrix FIRST** (one lens end-to-end, then extend to five). De-risks the hardest artifact early.
8. **Remaining policy components** (Decision Chain, Recommendations pipeline, Native Validation, Promotion, Drafts, Overrides, Policy Inspector).
9. **Views** (thin compositions).
10. **Claude Code wiring.**

**Two parallel tracks (run simultaneously to compress calendar time):**
- **Track A — Claude Design (contract-independent, start now):** shell · themes · layout · navigation · primitives · market-data UI · replay UI · analytics shell · dashboards · pair workspace · broker health · fleet · charts · event dock. Everything that does **not** depend on the policy entity contracts.
- **Track B — Architecture (DELIVERED · `contracts/TRACK-B-CANONICAL-CONTRACTS.md` + `contracts/fixtures/world.v1.json`):** Strategy Package · **Deployment Manifest** · Policy Cell · Recommendation (+ research traceability) · Decision Chain · **Feature Flags** · System Confidence · Version Comparison · Number-Format Canon · Command Vocabulary · the coherent Fixture World. These are the frozen contracts Claude Code wires and that gate Stages 7–10.

**Track B is delivered; the Stage-6 gate is OPEN.** Stages 1–5 (Track A) proceed immediately; Stages 7–10 generate against the frozen contracts + Fixture World (matrix single-lens first).

---

## §N. Fixtures & documentation requirements

**Fixtures = one coherent world, never Lorem Ipsum.** The delivered `contracts/fixtures/world.v1.json` is that world (as of `2026-07-01T09:14:22Z`): EURUSD on FTMO/MT5, **Package v18** active (validated NATIVE) + superseded v17 for comparison; cohorts across the 6 states with real evidence badges/sample sizes (e.g. *London · BOS · Long · Bull/Expand · Target 2.75R · Risk 1.0% · EV +0.41R · Confidence 96% · n=67 · NATIVE*); live + ghost trades (one with a full Decision Chain + Management timeline); **one blocked intent per reason**; two recommendations at different lifecycle stages; a draft with changed cells; a native-validation result; broker-health + reconciliation; an Edge-Monitor snapshot and System Confidence; three deployments across live/ghost/experimental lanes. Every component pulls from this one world so screens cohere. **Generate against `world.v1.json`; never invent fixture shapes.**

**Documentation (Claude Design must output per component):** its backing entity/contract, props, the relevant-state set, token usage, spacing/layout rules, interaction/motion, responsive behaviour, and relationships to other components — so Claude Code wires without guessing. Rule: **never invent a field; if one is missing from the contract, stop and flag.**

---

## §O. Lead-Architect Final Review (V2.1 — Track B delivered; Manifest + Flags folded in)

### O1. Retained pushback (real risks, mitigations embedded above)
- **Universal template rigidity** — mitigated by "consistent skeleton, variable emphasis" (§C). Let Replay and the Matrix dominate their Primary Workspace; do not force equal bands.
- **Pair-first vs global surfaces** — Edge Monitor / Broker Health / Fleet are cross-pair and must **not** nest under a pair (§B). Most likely scalability drift.
- **Instrument vs pair** — Policy is per **instrument**; axes are shared (§B/§F). Prevents 144-cell duplication.
- **Matrix cost** — five lenses × states × three themes is heavy. **One lens end-to-end first** (§M Stage 7), then extend.
- **Decision Chain scope** — ends at **Outcome**; downstream influence is a separate panel (§H).
- **Badge/Inspector/reuse-mandate** — one component per family (§I). Spawning `XyzBadge`/`XyzInspector`/per-list tables is the top cost regression — forbid it.
- **Lanes are a filter, not screens** (§B/§B.1).
- **Manifest is immutable** — the UI never "edits" a running Manifest; a change is a new Manifest + redeploy (mirrors the Package rule). Live Overrides remain the only fast, time-boxed, audited deviation.

### O2. Scalability verification (refinement #4 — scales on data, not structure)
- **Hundreds of deployments:** each is a Deployment Manifest instance; `FleetGrid` and `ScopeNavigator` virtualise + search + group (by broker/account/domain/environment). No global list caps growth.
- **Multiple brokers / accounts / VPS:** broker · account · environment are Manifest fields + scope facets; a new VPS is a new `environment` value + Manifests — not a schema change.
- **Multiple operators:** `OperatorSession` + permission seam + draft locking (Track B) — activation, not restructure.
- **Multiple domains (FX / Futures / Crypto):** `domain` discriminator + per-domain axes (VocabularyBundle). A new domain is data + packages.
**Conclusion:** only data increases; architecture is unchanged. ✅

### O3. Cloud readiness (refinement #5 — no hidden blockers)
- UI is API-bound, server-authoritative, snapshot+delta → web, remote-monitoring, and a mobile companion are new **clients** on the same contracts.
- ULID ids · single-writer ownership · append-only Event log + global `seq` · idempotent commands (Track B) → distributed/cloud-safe.
- Themes are token swaps; components hold no authoritative state; no `localStorage` of truth → portable to web.
- The Manifest's `environment` field already anticipates VPS/remote hosts; **Manifest export/restore is the migration primitive** — a deployment moves environments with complete fidelity.
- Multi-user collaboration: permission seam + `OperatorSession` present (no-op today).
**Conclusion:** no blocker. Do not build cloud now; nothing prevents it later. ✅

### O4. First-pass optimisation (refinement #6)
- **Merge:** the §I reuse mandate collapses ~15 would-be components into ~8 variant-driven ones.
- **Simplify:** Manifest + Flags make feature-gated workspaces a single conditional mount, not bespoke builds.
- **Delay:** flag-off modules (`aiRecommendations`, `newsIntegration`, `experimentalFeatures`) are not generated first pass — build their shells when the flag + contract land.
- **Freeze first:** token sheet + states gallery + the delivered Fixture World before composites.
- **Matrix single-lens first** (Stage 7). These keep the first pass near production and cheap.

### O5. Component reuse audit (refinement #7)
One each of: **Badge · Inspector · ActionBar · ConfirmDialog · HealthIndicator · StatusChip(=Badge) · DataTable · Card** — see the §I mandate. The rule "differs only in data/labels ⇒ variant, not component" is the single biggest guard against duplication and inconsistent styling.

### O6. Future module expansion (refinement #8 — module = flag + workspace + contracts)
Every future module is **a Feature Flag + a workspace (global or pair tab) + contracts**, mounted by the flag on the universal page template with zero structural change and zero edits to existing modules:
- **Options / Crypto** → new `domain` + VocabularyBundle + packages.
- **AI Copilot · Journal · Journal Assistant · Voice Commands · Mobile Dashboard · Strategy Marketplace · Portfolio Analytics** → each a new flag + workspace + contracts. (Mobile Dashboard is a new *client* over the same API, per O3.)
Adding a module is configuration, never redesign. ✅

### O7. Final architecture audit (refinement #9 — genuine issues, not perfectionism)
- **The unified Matrix is the single hardest artifact** (144 × 5 lenses × 3 themes × states). Prototype-first mitigates; still the top build risk. **(P1)**
- **Manifest ↔ Deployment denormalization** — the derived cache (packageHash/account/pair/lane on `Deployment`) must stay in sync with the Manifest. Enforce "Manifest is truth, cache is derived," or drift appears. **(P1 contract discipline)**
- **Feature-flag combinatorics** — flags multiply UI states; test a capped matrix of combinations and default unknown flags **off**. **(P2)**
- **Fixture World drift** — version `world.v1.json` against the entity-dictionary version so wiring can't silently mismatch. **(P2)**
- **Multi-operator concurrency** — draft locking is specified but untested until multi-operator is enabled; revisit then. **(P2)**
- **Cross-domain axis leakage** — no FX session names may be hardcoded in shared components; axes come only from the VocabularyBundle. **(P1 watch)**
None requires redesign now; all are discipline/sequencing items.

### O8. Verdict — **READY.**
Track B is delivered (Canonical Contracts + Fixture World); the design system is embedded (implement-not-invent); the Deployment Manifest + Feature Flags make the platform **configuration-scalable** (hundreds of deployments, multi-broker/account/VPS/operator/domain, cloud-ready) with **no structural change and no conflict** beyond the one smallest-change note in §B.1; and the reuse mandate + staged order minimise first-pass cost. **Generate now:** Track A Stages 1–5, then open the policy gate at Stage 7 (matrix single-lens first) against the frozen contracts + Fixture World.

**Verdict in one line:** *READY — Manifest + Flags folded in additively, Track B frozen, scalability and cloud paths clear; generate Stages 1–5 now and proceed through the open policy gate against `world.v1.json`.*
