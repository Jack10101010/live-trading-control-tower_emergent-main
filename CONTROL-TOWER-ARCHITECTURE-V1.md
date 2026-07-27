# Control Tower Platform — Architecture V1

**Status:** FROZEN BASELINE (V1.2 — final hygiene pass: dependency rule + invariants, §13). All future architectural changes are recorded as ADRs; this document is amended only by accepted ADRs.
**Date:** 2026-07-16
**Scope:** Long-term architecture for the Control Tower platform, trading node, and shared strategy core.

---

## 0. Locked decisions

| Decision | Choice | Consequence |
|---|---|---|
| Code reuse | One shared core across research / backtest / replay / paper / live | Analyzer + strategy logic is extracted into a pure library; Lux Backtester and the live node become *drivers* of the same core |
| Decision clock | Bar-close driven | Deterministic replay is achievable; entries/exits are expressed as resting orders managed by the Execution Engine |
| Process model | Modular monolith "Trading Node" | One process hosts all instances with strict internal boundaries; MT5 Adapter is a separate process |
| Target environment | Prop-firm accounts (FTMO-style) first | Risk Guard, reconciliation, idempotency, and health monitoring are safety-critical from day one, not later hardening |

---

## 1. Top-level architecture

```
                          MAC (optional, can be offline)
  ┌────────────────────────────────────────────────────────────┐
  │  CONTROL TOWER (thin client)                               │
  │  Charts · Analytics · Config editor · Replay UI · Logs     │
  └───────────────┬────────────────────────────▲───────────────┘
        commands  │ (control plane, req/resp)  │  telemetry
                  │                            │ (event stream + queries)
  ════════════════╪════════════════════════════╪══ VPN (WireGuard/Tailscale)
                  ▼                            │
  ┌────────────────────────────────────────────┴───────────────┐
  │  GATEWAY API            VPS (runs headless, 24/5)          │
  │  auth · command audit · event fan-out · journal queries    │
  ├────────────────────────────────────────────────────────────┤
  │  TRADING NODE (modular monolith)                           │
  │                                                            │
  │   DATA SERVICE ──► INSTANCE SUPERVISOR                     │
  │   Polygon adapter      │                                   │
  │   Broker-feed adapter  ├─ Instance: SMC-DE-v2 EURUSD M15   │
  │   Historical store     │    Analyzer → Strategy Engine     │
  │   Gap backfill         ├─ Instance: SMC-DE-v2 GBPUSD M15   │
  │                        └─ Instance: ... (isolated state)   │
  │   SESSION/CALENDAR          │ TradeCandidate               │
  │   service (DST-aware)       ▼                              │
  │                        RISK GUARD  ◄── account equity feed │
  │                             │ OrderIntent (or veto)        │
  │                             ▼                              │
  │                        EXECUTION ENGINE                    │
  │                        order lifecycle · idempotency ·     │
  │                        SL/TP mgmt · retries                │
  │                             │                              │
  │   RECONCILER ◄──────────────┤                              │
  │   NOTIFIER (Telegram/push)  │                              │
  │                             ▼                              │
  │              ┌─ EVENT JOURNAL (append-only, the spine) ─┐  │
  └──────────────┴──────────────┬───────────────────────────┴──┘
                                ▼  local RPC
                  ┌──────────────────────────┐
                  │  MT5 ADAPTER (separate   │   ← the ONLY
                  │  process, Windows-bound) │     Windows-bound
                  │  + MT5 Terminal          │     component
                  └────────────┬─────────────┘
                               ▼
                            BROKER
```

Key properties: the Control Tower is a client of the Gateway, never a hop in the data plane. Everything on the VPS runs and protects the account with the Mac switched off. The Windows dependency is quarantined to one adapter process.

---

## 2. The event journal is the spine

Every input and output of the system is an immutable, timestamped event appended to a per-instance journal. This single mechanism provides replay, crash recovery, audit, debugging, analytics, and chart rendering. It is the most important structural decision in the platform — everything else hangs off it.

### 2.1 Canonical event families

- **MarketData** — `BarClosed` (primary trigger), `TickBatch` (optional later, additive)
- **Analyzer** — `SwingFormed`, `BosDetected`, `ChochDetected`, `DirectionChanged`, `MarketStateChanged`, `ObCreated`, `ObMitigated`, `ObInvalidated`, `SessionOpened`, `SessionClosed`
- **Strategy** — `SetupArmed`, `TradeCandidate`, `CandidateExpired`, `CandidateCancelled`
- **Risk** — `CandidateApproved`, `CandidateVetoed`, `LimitBreached`, `TradingHalted`, `TradingResumed`
- **Execution** — `OrderIntent`, `OrderSubmitted`, `OrderAccepted`, `OrderRejected`, `OrderFilled`, `PartialFill`, `OrderModified`, `OrderCancelled`, `PositionClosed`
- **System** — `InstanceStarted/Stopped/Paused`, `ConfigApplied`, `Heartbeat`, `FeedLost/FeedRestored`, `ReconciliationReport`, `CommandReceived` (audit of every CT command)

### 2.2 Mandatory envelope on every event

```
event_id · instance_id · account_id · seq · schema_version
event_time (market time, UTC) · wall_time (processing time, UTC)
config_version · correlation_id · causation_id
```

Rules that pay off for years:

1. `event_time` vs `wall_time` are always separate. Replay runs on `event_time` with a virtual clock; nothing in the core may read the wall clock.
2. `account_id` exists from day one even with one account — retrofitting multi-account into an event schema is brutal.
3. `config_version` on every event makes analytics honest: every trade is traceable to the exact config that produced it.
4. `schema_version` on every event, with a "new fields are optional, never repurpose a field" evolution rule.
5. Order intents carry a deterministic **idempotency key** (`instance_id + candidate_id + attempt`) so a crash/retry can never produce a duplicate order — an FTMO account-killer.

### 2.3 Storage

**The journal *interface* is the architectural commitment; its storage is a replaceable implementation.** The contract: `append(events)` (atomic, ordered per instance), `read(instance, seq_range)`, `tail(instance)` subscription, and snapshot save/load. Producers and consumers depend only on this interface — nothing outside the journal module may know what backs it.

First implementation: one SQLite journal file per instance plus periodic state snapshots (fast restart = load snapshot, replay tail). Historical candles in Parquet. Snapshots are tagged with the engine version that produced them and are discarded on engine upgrade — state is rebuilt from the journal, never migrated (§12). No Kafka, no message broker — a single-operator monolith does not need one, and the event *schema* plus the journal interface (not the transport or storage engine) are what preserve the option to move to NATS/Redpanda or another store later.

---

## 3. The shared core (hexagonal)

The core library — Market Analyzer, Strategy Engines, instance state — is **pure and deterministic**: no I/O, no network, no threads, no wall clock, no randomness without a seeded generator. It consumes events and a `Clock` port; it emits events. That's the whole contract.

```
                    ┌────────────────────────────────┐
   BacktestDriver ─►│          SHARED CORE           │
   ReplayDriver   ─►│  Market Analyzer               │──► events out
   PaperDriver    ─►│  Strategy Engine(s)            │
   LiveDriver     ─►│  Instance State                │
                    └────────────────────────────────┘
        (drivers own I/O, time, persistence, adapters)
```

Consequences:

- **Backtest = replay = paper = live** for the decision logic, by construction. The Lux Backtester becomes the `BacktestDriver` around this core; the research lab imports the same core. Divergence between research and live becomes structurally impossible rather than a discipline problem.
- **Migration test:** the extraction is done when the shared core reproduces a known Lux backtest run event-for-event. Keep 3–5 of these runs as **golden replays** — the permanent regression suite for every future change to analyzer or strategy code.
- **Testability:** the core is testable with plain unit tests feeding event lists. No mocks of brokers or sockets anywhere near strategy logic.

---

## 4. Components

### 4.1 Naming (resolves the "Portfolio Manager" problem)

| Term | Meaning |
|---|---|
| **Strategy** | A blueprint/plugin, e.g. *SMC Delayed Entry v2* |
| **Strategy Engine** | The per-instance decision component (the thing misnamed "Portfolio Manager"). Interface: `StrategyEngine`; concrete: `SmcDelayedEntryV2Engine` |
| **Instance State** | The engine's owned state: market state, direction, BOS/CHOCH, session state, daily permissions, OB lifecycle, eligibility — derivable from the event stream |
| **Strategy Instance** | Strategy + config + symbol + timeframe + account/broker binding + Instance State, supervised as an isolated unit |
| **Risk Guard** | In-path pre-trade checks + autonomous account protection (this is *not* the future Portfolio Manager) |
| **Portfolio Manager** (future) | Cross-instance capital allocation, correlation, exposure netting. Plugs in between Risk Guard's instance checks and account checks. Do not build yet; the seam exists |

"Strategy State Engine" was close but undersells it — the component owns decisions, not just state. Keep "SMC" out of the generic component name; it belongs only on the concrete class.

### 4.2 Data Service

Provider adapters behind one interface (`Polygon`, `BrokerFeed(MT5)`), an owned historical store, gap detection and backfill, and bar construction with explicit session/DST handling.

**Feed-divergence decision (flagged, not fully resolved):** Polygon FX is aggregated indicative pricing; fills happen on the broker's feed with its spread. For a structure-level strategy (OBs, BOS) the two feeds *will* disagree at the pip level. Recommendation: each instance declares one **primary decision feed** (for live FTMO instances, the broker feed); record both feeds; add a divergence monitor that alerts when they drift beyond a threshold. Polygon remains the research/chart/history workhorse.

### 4.3 Session / Calendar Service

Session-driven strategies make this core infrastructure, not a helper: DST-aware London/NY sessions, holiday calendars, symbol trading hours, futures roll (for the NQ future). All timestamps UTC internally; exchange-local time only at the edges. DST transition weeks are a recurring correctness bug class — this service is the single place that logic lives.

### 4.4 Risk Guard (safety-critical, in-path)

Two layers, both server-side on the VPS:

1. **Pre-trade gate** (synchronous, in the execution path): `TradeCandidate → CandidateApproved | CandidateVetoed`. Checks: per-instance risk %, max concurrent positions, daily trade count, instrument allow-list, trading-halted flag, and prop-firm limits — projected daily loss including floating P&L, total drawdown headroom, equity floor.
2. **Autonomous guard** (continuous): monitors account equity and open positions independently of any candidate. Can emit `TradingHalted` and command flatten-all on its own authority. Includes the **dead-man's switch**: feed or broker connection lost beyond a threshold with open positions → execute the configured policy (alert → tighten stops → flatten), and a hard equity floor above the FTMO breach level with margin for slippage.

The Control Tower can *trigger* halt/flatten; it never *implements* them. The guard functions with the Mac off.

### 4.5 Execution Engine

Owns the full order lifecycle as an explicit state machine: `Intent → Submitted → Accepted → Working → Filled/PartialFill/Rejected/Cancelled → Closed`. Responsibilities: idempotent submission (client order IDs from the intent's idempotency key), bounded retries with backoff, SL/TP placement and modification, trailing management, partial-fill handling, slippage recording (intended vs actual price on every fill — this data is gold for analytics later).

**Execution Adapter interface** (each adapter replaceable): `submit / modify / cancel / list_orders / list_positions / account_info / stream_updates`, plus a **capability declaration** — a formal contract, not a convention:

- Declared capabilities include (extensible enum): hedging vs netting, partial fills, server-side trailing stops, server-side OCO, SL/TP modify support, stop-distance rules, min/step lot size, symbol conventions.
- For every capability an instance's configuration touches, the Execution Engine resolves exactly one of three responses: **native** (use the broker feature), **emulate** (engine-side fallback, permitted only where node downtime cannot cause unbounded loss — engine-side trailing is acceptable; engine-side stop-loss is never acceptable, per §6's broker-side-stops rule), or **refuse** (instance fails validation at start with an explicit reason).
- Resolution happens at **instance start**: manifest requirements (§11.2) × adapter capabilities → validated or refused. Never discovered at order time.
- Hard rule: **broker identity may not be inspected outside its own adapter.** No `if broker == MT5` anywhere in the engine, guard, or strategies — a violation of this rule is an architecture bug, not a style issue.

First adapter: MT5. The adapter also normalizes **symbols** — `EURUSD` vs `EURUSD.r` vs exchange-specific futures codes — via a symbol-mapping table owned by config, never hardcoded.

### 4.6 Reconciler

On every connect and on a periodic timer: fetch broker truth (orders, positions, account), diff against journal-derived expected state, emit `ReconciliationReport`. Policies for each mismatch class: orphaned broker position (adopt or alert), missing expected position (mark closed externally, alert), unknown working order (cancel or adopt). Manual intervention in the MT5 terminal *will* happen; the system must converge afterward rather than trade on a false belief.

### 4.7 Gateway API + Control Tower

**Control plane (commands):** authenticated request/response with acknowledgment and audit (`CommandReceived` journaled): start/stop/pause instance, flatten instance/account, halt/resume, apply config, launch replay. Commands are *requests* — the node can refuse (e.g. config apply while position open, if so configured).

**Telemetry plane:** live event stream over WebSocket (fan-out of the journal tail) plus a query API over journal history and the candle store.

**Control Tower is a thin client.** It renders events and issues commands. The moment CT computes anything trading-relevant (a P&L number, an eligibility flag) that the node doesn't also compute, there are two sources of truth. Rule: **CT displays; the node decides.** Charts render analyzer/strategy/execution events as overlays on candles from the query API. Cosmetic overlays (FVGs, liquidity) live in a separate **Visual Analyzer** plugin category — render-only modules whose outputs are never consumed by strategy engines, keeping the core-logic vs visual-overlay boundary structural rather than conventional.

### 4.8 Notifier

Independent alerting path (Telegram/push/email) wired to Risk, System, and Reconciliation events. This must not depend on CT being open — with the Mac asleep, a `LimitBreached` or `FeedLost` at 3am still reaches the phone.

---

## 5. Communication patterns (summary)

| Path | Pattern | Rationale |
|---|---|---|
| Within Trading Node | In-process pub/sub over the journal (write, then fan out) | No broker infra; journal-first guarantees nothing observable was unrecorded |
| Strategy → Risk → Execution | Synchronous in-process calls, journaled at each hop | The data plane needs deterministic ordering, not queues |
| Node ↔ MT5 Adapter | Local RPC (gRPC or ZeroMQ) with heartbeats | Process isolation from MT5 terminal instability |
| CT ↔ Gateway commands | Request/response with ack + audit | Commands need refusal semantics and an audit trail |
| Gateway → CT telemetry | WebSocket event stream + query API | Fire-and-forget fan-out; CT reconnect = re-query + resubscribe |
| Alerts | Push (Telegram etc.) direct from node | Must work with CT offline |

Anti-recommendation: do not introduce Kafka/RabbitMQ/Redis-streams now. The event schema preserves that option; the infrastructure would only add failure modes for a single-operator system.

---

## 6. Deployment

**VPS (Windows, initially):** Trading Node + Gateway + journal + Notifier as one deployable service (auto-restart on crash, starts on boot); MT5 Adapter + MT5 Terminal as a separate supervised process pair. Keep the node platform-neutral (it's Python — enforce no Windows-only dependencies outside the adapter) so it can move to a Linux box + small Windows MT5 host later, or drop Windows entirely when a non-MT5 broker arrives.

**Mac:** Control Tower only. Nothing trading-critical.

**Network:** WireGuard/Tailscale between Mac and VPS. No publicly exposed ports; the Gateway binds to the VPN interface only.

**Backups:** journals, snapshots, and config history shipped off-VPS daily (object storage). The journal is the audit trail for every trade ever taken — treat it accordingly.

**Failure model (explicit policies, configured not coded):**

| Failure | Behavior |
|---|---|
| Node crash | Supervisor restarts → load snapshot → replay journal tail → reconcile against broker → resume or hold-and-alert |
| MT5 adapter/terminal crash | Node marks execution degraded, halts new entries, alerts; positions remain broker-side with server-side SL/TP (always place SL/TP broker-side, never rely on the node to exit) |
| Data feed loss | `FeedLost` → no new decisions; dead-man's policy governs open positions |
| VPS loss | Broker-side stops are the last line of defense; Notifier heartbeat-missing alert from an external uptime monitor |
| Mac off | Zero impact — by construction |

---

## 7. Configuration management

Config is trading-critical data, not files on disk:

1. **Schema per strategy version** — validated on write; CT's config editor is generated from the schema.
2. **Versioned + immutable** — every apply creates a new `config_version`; history is queryable; every event carries the version (see §2.2).
3. **Apply semantics per parameter** — declared in the schema: `hot` (applies next bar), `restart` (instance restart required), `flat-only` (refused while a position is open). The node enforces; CT merely displays which is which.
4. **Instance config layering** — strategy defaults → instance overrides, resolved and frozen at apply time into the immutable version (no runtime inheritance surprises).
5. **Experimental parameters (the feature-flag mechanism)** — experimental features (new BOS detection, experimental BE logic, trial filters, OB-logic variants) are ordinary parameters in the strategy's config schema, marked `experimental: true`, default **off**, enabled per instance. There is deliberately **no separate feature-flag system**: a flag store parallel to config would create a second source of behavioral truth and break the guarantee that `config_version` fully determines behavior — replays would no longer reproduce live decisions. As config parameters, experiments get versioning, audit, per-instance scoping, apply semantics, and replay fidelity for free. Discipline rule: every experimental parameter has a retirement path — it is either promoted to a permanent parameter (or the next strategy version) or deleted; long-lived flags multiply the test surface combinatorially and rot the golden-replay suite.

---

## 8. Observability and debugging

- **The journal is the primary observability tool.** "Why did instance X skip this setup?" is answered by replaying that instance's exact event stream in the CT replay view — not by grepping logs.
- **Shadow replay:** because live decisions are journaled and the core is deterministic, any live incident can be re-run bar-for-bar with full state inspection. This is the payoff of §2 + §3 and is something most retail platforms never achieve.
- **Structured logs** (JSON, with `instance_id`/`correlation_id`) for operational plumbing only — connection chatter, retries, performance. Decisions live in events, never only in logs.
- **Health model:** every component emits `Heartbeat` with status; the supervisor aggregates into a node health snapshot that CT displays and the Notifier watches. Include feed lag, adapter round-trip time, and last-reconciliation age.
- **Metrics worth recording from day one:** intended vs filled price (slippage), candidate→fill latency, veto reasons histogram, feed divergence.

---

## 9. What will hurt in 2–5 years if skipped now

1. **Event schema versioning discipline** (§2.2) — an unversioned journal becomes unreadable by future code, killing replay history.
2. **`account_id` in the envelope** — multi-account (several FTMO accounts + personal) is the most likely near-term growth axis.
3. **Symbol normalization service** — broker-suffixed FX symbols and futures roll will otherwise leak `if broker == ...` through the codebase.
4. **UTC-only core** — one exchange-local timestamp inside the core seeds years of DST bugs in a session-driven strategy.
5. **Golden replays as CI** — without them, every analyzer refactor silently risks changing live behavior.
6. **CT thinness** — every convenience calculation added to the UI is future divergence; enforce "CT displays, node decides" in review.
7. **Adapter capability declarations** — otherwise broker #2 (cTrader/IBKR) forces execution-engine surgery instead of a new adapter.
8. **The Portfolio Manager seam** — Risk Guard's split between instance checks and account checks is exactly where cross-strategy allocation plugs in later; don't fuse the layers.

---

## 10. Build order

1. **Extract the shared core** from the research lab + Lux code; define the event schema and envelope. *Done when golden Lux runs reproduce event-for-event through the core.*
2. **Journal + Replay driver** — journal writer, snapshots, virtual-clock replay; CT replay view can come later, correctness first.
3. **Data Service + Session/Calendar** — Polygon adapter, historical store, backfill, DST-safe sessions.
4. **Paper driver + MT5 Adapter (read-only)** — live data in, simulated fills, reconciler running against a demo account.
5. **Risk Guard + Execution Engine + MT5 write path** — demo account first, then FTMO. Dead-man's switch and Notifier before first real order.
6. **Control Tower UI incrementally** — health + logs first, then charts/overlays, then config editor, then replay UI. The node never waits for the UI.

Replay, paper, and live arrive in that order *because they share the architecture* — each stage validates the same core the next stage trades with.

---

## 11. Extension model

**Principle: the platform grows by adding implementations of declared extension points, never by modifying the core.** This generalizes what §3–§4 already establish for individual components into an explicit architectural rule.

### 11.1 Extension points and their contracts

| Extension point | Contract | Loaded by |
|---|---|---|
| Strategy | `StrategyEngine` interface + manifest (§11.2) | Instance Supervisor |
| Execution adapter | Adapter interface + capability declaration (§4.5) | Execution Engine |
| Market data provider | Provider interface (subscribe, history, backfill) | Data Service |
| Visual overlay | Render-only module: events/candles in, drawables out; outputs never consumable by strategy engines (§4.7) | Control Tower |
| Notification channel | `notify(alert)` sink | Notifier |
| Analytics / reporting | Journal consumer — reads the journal interface (§2.3), writes nothing to it | Standalone or CT |

Analytics and reporting need no special machinery: the journal is an open, versioned read interface, so any journal consumer is a plugin by construction.

**Anti-goal:** this is *not* a runtime plugin framework. No dynamic discovery, no third-party sandboxing, no plugin marketplace machinery. Extensions are modules registered in a static registry at build/startup time. The architectural value is in the declared contracts and the "core never changes" rule — not in loading mechanics, which would add failure modes a single-operator trading system cannot afford.

### 11.2 Strategy manifests

Each strategy ships a declarative manifest — data, never code inspection:

```
name · version · description
config schema (drives CT's generated config editor, §7.1)
supported symbols / symbol classes · supported timeframes
requirements: required data feeds, required adapter capabilities
              (e.g. "needs SL/TP modify", "needs pending orders")
```

The Instance Supervisor validates at instance creation: manifest requirements × chosen adapter's capability declaration × data-service feeds → start or refuse with an explicit reason. The Control Tower builds its strategy catalog and config editors from manifests alone. A strategy whose manifest omits a requirement it actually has is a bug caught by paper trading, not a runtime surprise in live.

---

## 12. Engine upgrades and state (rebuild, not migrate)

There is deliberately **no state-migration framework.** Because Instance State is derivable from the event stream (§2, §4.1), the upgrade path for a strategy engine (e.g. SMC-DE v2 → v2.1) is:

1. Snapshots are tagged with the engine version that produced them (§2.3). On engine upgrade, the snapshot is discarded.
2. Instance State is **rebuilt by replaying the journal** (or its relevant tail) through the *new* engine version. The new logic's view of history — possibly different OB lifecycles, different market state — is the correct post-upgrade state, not a defect.
3. **Upgrades are flat-only by default:** an instance restarts on a new engine version only with no open position, enforced by the supervisor (same `flat-only` semantics as config, §7.3).
4. If an upgrade must occur with an open position, the position is explicitly **adopted through the Reconciler** (§4.6) — broker truth in, alert raised — never silently carried across engine versions.

Migrating snapshot structs version-to-version would be permanent maintenance machinery solving a problem the event-sourced design already solved. If a rebuild is ever too slow, that is a performance problem for the journal/snapshot layer — not a reason to introduce migration code.

---

## 13. Dependency rule and invariants

Architectural boundaries outrank convenience. This section is the enforcement layer for everything above it: the boundaries in §1–§12 remain real only as long as the rules below are honored on every commit.

### 13.1 The dependency rule

Three testable rules replace all case-by-case judgment:

1. **All dependencies point inward.** Layering, outermost to innermost: `UI / CT → drivers & infrastructure (data, execution, journal, gateway) → contracts (ports, event schema, manifests, capability declarations) → shared core`. A module may depend only on layers at or inside its own.
2. **Concretes depend on contracts, never on other concretes.** The MT5 adapter and the Polygon adapter implement interfaces; nothing imports them by name except the registry (§11). Two implementations of anything never depend on each other.
3. **The core depends on nothing outside itself** — no I/O library, no adapter, no UI, no clock, no journal implementation. It sees events in, events out, and injected ports.

Everything follows from these: strategies cannot know brokers, MT5, Polygon, or the Control Tower; execution cannot know BOS/CHOCH; charts cannot contain trading logic; adapters cannot know strategies — not by convention, but because the imports are illegal.

**Enforcement:** import-linting in CI (e.g. per-package allowed-imports rules). A dependency-rule violation fails the build; it is never a review discussion.

### 13.2 Permanent invariants

| # | Invariant | Enforced by |
|---|---|---|
| I-1 | The core performs no I/O and never reads the wall clock; all time comes from the injected `Clock` | CI: import lint (no io/net/time modules in core) + core test harness |
| I-2 | The core is deterministic: same event sequence + config ⇒ same outputs, always | CI: golden replays (§3) run on every change |
| I-3 | Every analyzer or strategy change preserves golden-replay parity, or the golden set is deliberately re-baselined in the same commit with a written justification | CI: golden replays; re-baseline requires explicit commit annotation |
| I-4 | The journal is append-only; history is never mutated or deleted (corrections are new events) | Journal interface exposes no update/delete; review |
| I-5 | Every action that affects trading behavior — or that would be needed to explain a trade after the fact — is an event in the journal | Review; reconciliation and replay gaps surface violations |
| I-6 | Replay determinism is never sacrificed: no code path may behave differently under replay vs live except inside drivers | Review + shadow-replay spot checks (§8) |
| I-7 | CT displays; the node decides. No trading-relevant value is computed in the Control Tower that the node does not also compute and emit | Review; any CT-side derivation of trading state is rejected |
| I-8 | Broker identity never escapes its adapter (§4.5) | CI: import lint + grep gate for broker identifiers outside adapters |
| I-9 | `config_version` fully determines configurable behavior — no runtime toggles outside versioned config (§7.5) | Review; replay mismatches surface violations |
| I-10 | Safety functions (Risk Guard, dead-man's switch, broker-side stops) live server-side and operate with CT offline (§4.4, §6) | Review + failure-drill testing |

An invariant with no enforcement column entry would be an aspiration; every entry above names the mechanism that catches its violation. When a future change genuinely requires breaking one of these, that is by definition an architectural change — it goes through an ADR, never through code review alone.

---

## 14. Architecture Decision Records (ADRs)

This frozen baseline is amended only by accepted ADRs (see the Status line at the top). Each ADR records an architectural change made after V1.2 was frozen.

### ADR-1 — Unify execution authority (ARCH-1, 2026-07-27)

**Context.** Architecture Audit A found two parallel execution architectures in the live-prep backend. The *wired* path (`POST /api/commands/{name}` → `execution.ExecutionOrchestrator` → `broker`) enforced a fail-**open** policy: 27 of 33 commands reached broker dispatch through `PolicyResult(True, "none")`, i.e. no authorization gate. The *audited* deny-by-default policy engine (`execution_safety.evaluate()`, UI-18) was imported by nothing. The command vocabulary was declared five times, in two non-intersecting spellings (`CloseTrade` vs `close_position`), so naively wiring the safety gate would have classified every real command as unknown. This violated invariant I-7's spirit: two sources of truth for "may this command execute".

**Decision.** Establish exactly one execution authority with one canonical pipeline. Nothing may reach broker dispatch without passing it.

```
Operator Command
      ↓
Command Contract              (command_channel — read-only operator surface)
      ↓
Execution Safety Policy       (execution_safety.evaluate — deny-by-default gate)
      ↓
Execution Orchestrator        (execution.ExecutionOrchestrator — the single authority)
      ↓
Broker Adapter                (broker — MockBroker active; MT5 inert)
      ↓
Broker
```

The orchestrator's stages are explicit and unskippable:

```
Validate → Safety → Resolve → Broker Dispatch → Broker Result → Audit
```

- **Validate** — command is known to the registry (unknown ⇒ reject) + structural pre-checks.
- **Safety** — `execution_safety.evaluate()` is consulted for **every** command; a DENY ends the pipeline before dispatch. This is the anti-fail-open gate.
- **Resolve** — per-command feasibility (locked deployment, already-closed trade, capability). A command with no extra rule resolves to an *explicit named* `no_additional_feasibility_constraint`, never a silent allow.
- **Broker Dispatch / Broker Result / Audit** — effect through the mock broker; before/after captured; immutable BotEvent appended.

**Single command registry.** `backend/command_registry.py` is the one authoritative catalogue. It owns command identity, canonical name, aliases, risk classification, audit category (lifecycle metadata), broker-dispatch flag and required broker capability, and the submission surface. The snake_case safety names are **aliases** of their canonical PascalCase commands (`close_position` → `CloseTrade`), so the two former vocabularies now resolve to one entry. Import-time guards reject duplicate canonical names and alias collisions.

**Removed duplicate authorities** (each now has exactly one owner):

| Concept | Before (duplicated) | After (single owner) |
|---|---|---|
| Command identity / vocabulary | `server.KNOWN_COMMANDS`, `execution.COMMAND_SPEC` keys, `broker.BROKER_COMMANDS`, `execution_safety._RISK_BY_COMMAND`, `server._COMMAND_CATEGORY` | `command_registry` (all derive from it) |
| Risk classification | `execution_safety._RISK_BY_COMMAND` | `command_registry` (constants owned by `execution_safety`) |
| Policy / authorization | `execution._policy` fail-open + `execution_safety` (unwired) | `execution_safety.evaluate()` — the sole gate, always consulted |
| Broker-dispatch routing | `broker.BROKER_COMMANDS` literal | `command_registry.broker_dispatched_names()` |
| Broker capability requirement | hard-coded in feasibility policies | `command_registry` `broker_capability` field |
| Execution mode vocabulary | `execution_safety` modes vs `security_config.KNOWN_MODES` | (unchanged this slice; `execution_safety` owns execution mode) |

**Broker isolation (unchanged).** `_ACTIVE` remains `"mock"`; MT5 stays inert (order operations are no-ops, no gateway, no socket); no transport, UI or live-trading change. The permissive `SafetyContext` that lets the fixture world keep working is built **only** while the active broker is the mock; against any non-mock broker the pipeline reverts to deny-by-default, so a real broker can never be dispatched to on the strength of the mock context.

**Consequences.** Fail-open is structurally impossible: unknown commands deny, every dispatch requires a prior safety allow, and the "no additional feasibility rule" case is an explicit named outcome. When live execution is built, the only change required is to supply a *real* `SafetyContext` (armed, identified, confirmed, healthy node) for the real broker — the pipeline and registry do not change.

### ADR-2 — Establish the pre-live execution core (ARCH-2, 2026-07-27)

**Context.** ARCH-1 unified the execution authority but left the pre-live core incomplete: adapters were constructed at import (the MT5 adapter probed for a live gateway on `import broker`), execution lifecycle state existed only in process memory, there was no order-intent model, no durable order lifecycle, no canonical reconciliation over execution state, and readiness fields were constants. Audit A additionally flagged the unauthenticated fault-injection route as able to permanently corrupt reconciliation truth.

**Decision.** Build the complete mock-only execution core that Live-1 will activate rather than construct:

```
Operator (or future strategy) intent
        ↓
command_registry            (canonical identity, risk class, intent kind, risk_reducing)
        ↓
execution_safety.evaluate   (deny-by-default; + reconciliation gate)
        ↓
execution_context           (ONE immutable context assembled at the boundary)
        ↓
execution.ExecutionOrchestrator
        ↓
broker_adapter.BrokerAdapter (canonical contract; lazy, centralized, fail-closed factory)
        ↓
MockBroker (active) / MT5Adapter (inert)
        ↓
BrokerResult                 (canonical envelope; inert ops answer `unavailable`)
        ↓
order_lifecycle + execution_store   (durable intents + append-only transitions)
        ↓
reconciliation               (canonical authority; discrepancies feed safety)
        ↓
execution_telemetry + BotEvent audit (derived read model; /api/execution/state)
```

**Ownership after ARCH-2 (single writable owner per fact):**

| Fact | Owner |
|---|---|
| Adapter contract, capability model, connection-state vocabulary, result/error envelope | `broker_adapter.py` |
| Adapter construction + selection (lazy, cached, fail-closed) | `broker_adapter.get_adapter` |
| Execution context assembly | `server._execution_context` (one assembly; safety + orchestration consume it) |
| Order intent model + `intent_`/`recon_` identity | `order_lifecycle.py` |
| Lifecycle states + allowed transitions | `order_lifecycle.py` (explicit table; terminal protection; evidence rules) |
| Durable lifecycle + reconciliation records | `execution_store.py` (`backend/execution_state.db`, schema v1, append-only transitions, fail-closed) |
| Reconciliation classification + safety posture | `reconciliation.py` (`broker_sync` remains the fixture-VIEW sync and delegates canonical runs) |
| Execution read model + readiness gates | `execution_telemetry.py` (derived only; `tradingReady` is a gate conjunction, never a constant) |

**Key semantics.**
- *Arming ambiguity resolved:* the node stays authoritative for live arming and broker/account safety (I-7); the tower-side window is named **command authorization** (`ExecutionContext.command_authorization`) and is never populated from node telemetry.
- *Durability gate:* a broker-dispatched command is denied (`execution_store_unavailable`) immediately before dispatch if the durable lifecycle cannot record it. The store therefore contains exactly the authorized intents; safety/feasibility denials are refused earlier and never dispatch. An audit-append failure after dispatch surfaces loudly while the durable lifecycle already holds the evidence — no silent mutation.
- *Restart recovery:* deterministic and fail-closed — pre-dispatch intents → `failed(restart_before_dispatch)`; possibly-dispatched intents → `unknown` → `reconciliation_required`. Completion is never fabricated; `unknown` resolves only through reconciliation with evidence.
- *Reconciliation feeds safety:* unresolved CRITICAL discrepancies deny new risk-increasing execution (`reconciliation_unresolved`); risk-reducing commands (close/cancel/de-risk, flagged in the registry) and emergency stops stay available so an operator can always de-risk. Account-identity mismatch is a hard failure; stale input can never reconcile clean.
- *Fault injection:* `/api/broker/faults` is test-only and disabled by default (`BROKER_FAULT_INJECTION_ENABLED`).

**Boundary: ARCH-2 does NOT enable live execution.** The mock is the only constructible-active adapter; MT5 is inert (lazy gateway, no order operations, no sockets — proven by subprocess import tests); no live market data, no strategy behaviour, no frontend controls were added. **Live-1 may begin only after the ARCH-2 acceptance criteria pass**, and will consist of supplying real context facts (node-derived health, real command authorization, real account identity) and a real adapter behind the SAME boundary — not of changing the pipeline.
