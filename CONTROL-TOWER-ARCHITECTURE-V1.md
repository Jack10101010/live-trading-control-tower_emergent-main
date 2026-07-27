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

### ADR-3 — Complete secure pre-live activation plane (ARCH-3, 2026-07-27)

**Context.** After ARCH-2, four activation blockers remained: no in-repo client could send the UI-11 credential (enabling auth disabled the platform); the node would have needed the operator token (full API surface) just to publish telemetry; the declared `ConnectionPolicy` gate was unimplemented; and the security surface still reported `active: false` with text claiming transport/auth did not exist. The tower/node arming ambiguity and the permissive mock execution context also needed hard structural boundaries before any Live-1 work.

**Decision.**

1. **Three authentication principals**, each with its own variables, policy object, scope and fail-closed path — no cross-principal reuse by default:
   - *Operator* (`CONTROL_TOWER_AUTH_ENABLED`/`_API_TOKEN`) — every protected route; applied by the SPA's single header owner (`lib/authSession.ts`, memory-only token, reload clears, 401 ≠ offline).
   - *Node ingest* (`CONTROL_TOWER_INGEST_AUTH_ENABLED`/`_INGEST_TOKEN`) — governs `POST /api/live/ingest` exclusively (`CLASS_INGEST` route scope). The operator token does NOT authenticate ingest unless the deprecated `CONTROL_TOWER_INGEST_ALLOW_OPERATOR_TOKEN` compat flag is explicitly enabled, which the security surface reports as DEGRADED. The publisher sends the dedicated token and distinguishes 401/403 (`unauthorized: true`) from network failure (`HTTPError` caught before `URLError`); publishing failures never affect trading.
   - *Tower→node read* (`NODE_API_TOKEN`) — the outbound GET-only transport, unchanged in direction, now policy-gated.
2. **Canonical `ConnectionPolicy`** (`connection_policy.py`): consulted immediately before EVERY outbound socket (the transport's single I/O method; structural test pins policy-before-open and exactly one opener call). Deny-by-default; immutable decisions with machine reason codes and host-free audit views. **Operating profiles**: `local_loopback` (the ONLY approved profile; loopback hosts only, plain HTTP tolerated locally), `remote_pre_live` and `remote_live` (DENY, naming their missing prerequisites — TLS trust, private network, secrets management, explicit activation attestation — none of which is faked). Transport selection and connection approval remain separate decisions; enabled-but-misconfigured is reported distinctly from disabled (`transport.selection_status`).
3. **Operator command authorization** (`command_authorization.py`): immutable, bounded, scope-exact `AuthorizationGrant` — no generic "armed" boolean. The ONLY provider is the explicitly-mock provider, which refuses to issue for any non-mock adapter, so the permissive fixture authorization structurally cannot survive adapter activation. Revocation and malformed data fail closed. Node arming remains node-authoritative and observed-only; a grant can never substitute for node health, account identity, reconciliation or broker state (each gate runs independently), and node arming cannot substitute for a grant.
4. **Execution context with provenance** (`execution_context.py`): tower / node / broker / reconciliation fact groups each labelled (`mock-synthetic`, `node-telemetry`, `durable-store`, `absent`). Node facts derive from real validated telemetry when present (staleness derived, health never invented); the **account-identity gate** (`NODE_EXPECTED_ACCOUNT_FINGERPRINT`) denies all non-read-only commands on MISMATCH and denies execution-affecting commands on UNKNOWN or stale; stale node telemetry maps healthy→stale and denies.
5. **Truthful surfaces**: `/api/security/config` lost the `active:false` constant and its "nothing exists yet" reason — replaced by explicit dimensions (`auth`, `ingestAuth`, `connectivity` = profile/transport-selection/ConnectionPolicy-decision/outbound-auth/missing-prerequisites). `/api/health` returns a MINIMAL body (`status`,`scope`,`serverTime`) to unauthenticated callers when operator auth is enforcing — no node ids, broker kind or fixture versions. `Cache-Control: no-store` is applied uniformly to every `/api` response by middleware. Readiness gained the activation-plane gates (`operatorAuthenticated`, `commandAuthorizationActive`, `accountIdentityMatch`, `nodeArmingObserved`, `approvedConnectionProfile`) — still a named-gate conjunction, never one unexplained boolean.
6. **Activation runbook**: 13 ordered, observable steps with rollback (SECURITY-BASELINE.md); transport is enabled last, after its auth and policy prerequisites; remote profiles are verified to still deny as the final step.

**Boundary.** ARCH-3 does not enable remote or live execution: mock remains the only active adapter, MT5 stays inert, no real order path exists, `local_loopback` is the only approved profile, and remote activation remains explicitly prohibited. Completion of ARCH-3 permits work on a mock-validated Live-1 adapter vertical slice; it does not itself authorize any real broker connection or order submission.

### ADR-LIVE-1 — Read-only MT5 adapter vertical slice (LIVE-1, 2026-07-27)

**Context.** ARCH-3 permitted a mock-validated Live-1 adapter slice. Until now every broker read was simulated by `MockBroker`; no in-repo code read a real terminal. The first genuine step toward live operation is to read a real MT5 terminal — account, positions, orders, history, server time — while keeping execution structurally impossible. **LIVE-1 introduces live broker reads only. Execution remains structurally impossible.**

**Decision.**

1. **Genuine read-only MT5 adapter** (`broker.py` `MT5Adapter` over `live/mt5_gateway.py`): supports connection/terminal status, account information + identity fingerprint, broker information, symbol specs, open positions, open orders, recent executions (history deals), and server time. Every write verb — `submit_order`, `close_position`, `submit_command`, `cancel_order`, `modify_order`, `flatten` — is inert and returns a canonical unavailable/denied `BrokerResult` (never touches the terminal). The MT5 SDK is injectable (`MT5Gateway(config, sdk=…)`) so the whole slice is provable against a fake SDK with no terminal present.

2. **Adapter activation is centralized and fail-closed** (`broker_adapter.py`): `active_kind()` reads the single selection variable `CONTROL_TOWER_BROKER_ADAPTER` (blank → `mock` default; unknown value returned verbatim then rejected at construction). `get_adapter()` is the ONLY factory: lazy, cached-after-success, constructs nothing for an unknown kind (`UnknownAdapterError`), and — for `mt5` — calls `ConnectionPolicy.evaluate_local_broker("mt5")` and constructs the adapter only on approval (`AdapterDeniedError` + `AUDIT adapter_denied` otherwise). Importing the backend performs no MT5 import side effects, initialization or connection (pinned by a subprocess test that traps `socket.connect`).

3. **ConnectionPolicy gates all MT5 access** (`connection_policy.py` `evaluate_local_broker`): deny-by-default; `local_loopback` is the only approved profile; `remote_pre_live`/`remote_live` deny naming missing prerequisites; a non-`{mock,mt5}` kind denies (`adapter_unknown`). A denied policy makes zero MT5 API calls — the gateway property short-circuits to `connection_denied` before loading the SDK — with a machine-readable reason and an `AUDIT mt5_gateway_denied` event.

4. **Canonical broker models** (`BrokerAccountInfo`, canonical positions/orders, `BrokerDeal`, `SymbolSpec`, `TerminalInfo`, broker info): immutable, broker-neutral field names, redaction-safe (`login_masked = mt5_****NNNN`; raw login never leaves the adapter), deterministic serialization (`as_dict()` returns sorted keys). No MT5 SDK object (`SimpleNamespace`/named tuple) escapes the adapter boundary — pinned by tests asserting no MT5 type names or SDK objects appear in returned data.

5. **Capabilities derive from the adapter, write stays absent**: `BrokerCapability` gained `supportsLiveWrite` (default `False`). Mock = read ✓ / write ✗; MT5 = read ✓ / write ✗. Execution safety continues to deny every execution command; a non-mock adapter has no permissive execution context, so a live read opens no execution path.

6. **Telemetry integration** (`/api/execution/state` `broker` block): exposes adapter kind, connection state, terminal-connected, account identity + broker identity, equity/balance/margin/margin-level/leverage, open positions/orders counts, recent-execution count, server time, telemetry timestamp, and `provenance` (`live_mt5` vs `mock-fixture`). Unavailable/partial reads are reported with explicit codes (`reads.*`) and never invented — an unavailable account read carries `available:false` + `code`, with no fabricated balances.

7. **Reconciliation consumes live reads, still read-only** (`reconcile_snapshot` → `run_reconciliation`): no corrective action, account-identity mismatch remains a hard failure, `recon_` ids and discrepancy classes preserved.

8. **Minimal read-only UI** (`BrokerReadPanel.tsx`): displays connection, masked account, broker, server, balance, equity, margin level, open positions/orders, adapter kind; labelled **LIVE READ ONLY · NO EXECUTION**; provenance badge (LIVE (MT5) vs MOCK FIXTURE); unavailable reads shown explicitly; no button, input or execution control of any kind.

9. **Graceful failure** (`_guarded`): package missing, terminal absent/closed, login failure, account/symbol unavailable, timeout, or malformed data each map to a canonical `BrokerResult` — no crash, no traceback leakage, machine-readable code, `AUDIT mt5_read_failed` event — and execution remains unavailable throughout.

**Boundary.** LIVE-1 introduces live broker reads only. **Execution remains structurally impossible**: no order submission, modification, cancellation, position close, or account mutation path exists in the live adapter (all write verbs are inert), the MT5 write capability is absent, and `local_loopback` remains the only approved connection profile. LIVE-1 does not enable trade execution, remote profiles, or any account mutation. *(Superseded in exactly one respect by ADR-LIVE-2: market-order submission.)*

### ADR-LIVE-2 — First market-order execution slice (LIVE-2, 2026-07-27)

**Context.** LIVE-1 delivered genuine read-only MT5 access with execution structurally impossible. The first execution-capable step must prove the complete architecture ARCH-1/2/3 built — authentication → authorization → context → safety → adapter → acknowledgement → lifecycle → durable store → reconciliation → telemetry → UI — by allowing exactly one broker operation, with everything else remaining read-only.

**LIVE-2 enables exactly one executable broker operation: `submit_market_order()`. All other broker mutations remain structurally unavailable.**

**Decision.**

1. **Canonical market-order models** (`broker_adapter.py`): immutable, broker-neutral `MarketOrderRequest` (intent id, instrument, side, quantity, optional stop loss / take profit, optional comment, correlation id, idempotency key, execution mode — no strategy fields; validated at construction so nothing malformed can exist) and `MarketOrderAck` (status ∈ {filled, partially_filled, rejected, not_submitted, timeout, communication_failed}, broker order/deal tickets, volumes, price, machine reason, provenance). Both serialize deterministically (`as_dict()` → sorted keys). No broker-native object crosses the boundary.

2. **One executable operation, one surface** (`command_registry.py`): new canonical command `SubmitMarketOrder` on the dedicated `SURFACE_EXECUTION` — submit-able ONLY through `POST /api/execution/market-order`; the fixture control plane (`/api/commands/{name}`) rejects it as unknown, and `execution_command_names()` proves the surface contains exactly one command. Risk class `execution_affecting`, `broker_dispatched`, required capability `supportsMarketExecution`, intent kind `submit`.

3. **Adapters** (`broker.py`): `MockBroker.submit_market_order` — deterministic fixture execution (ticket derived from the intent id; no invented market price; provenance `mock-fixture`). `MT5Adapter.submit_market_order` — the ONE live write, delegating to the gateway's proven typed write path (`live/mt5_gateway.open_position` → `live/mt5_results` conservative classification) and mapping the typed result into the canonical acknowledgement (FILLED/PARTIAL → ok; REJECT retcodes → `rejected`; pre-submit normalization refusal → `not_submitted` with zero `order_send` calls; AMBIGUOUS retcodes → `timeout`; exceptions → `communication_failed`, type name only — no message leakage). MT5 capabilities now declare exactly `supportsLiveWrite=True` + `supportsMarketExecution=True`; every other write capability stays False and every other write verb stays inert.

4. **Execution safety remains authoritative.** The market order flows exclusively through the canonical `ExecutionOrchestrator` pipeline; no alternate path exists. Denial matrix (each independently proven): execution mode not active; operator unidentified; unconfirmed; node not healthy (stale/unknown each deny); account identity mismatch/unknown; unresolved critical reconciliation; disarmed / expired authorization; duplicate command; expired command; unknown command; missing market-execution capability; broker disconnected; unapproved connection profile; execution store unavailable (denied BEFORE dispatch). The mock authorization provider still refuses non-mock adapters, so a permissive fixture grant structurally cannot authorize the live adapter.

5. **Lifecycle** (`order_lifecycle.py`): new terminal state `open`. The required flow maps onto the canonical vocabulary as `created` (intent_created) → `validated` → `ready` (reason `safety_allowed`) → `submitting` → `submitted` (order_send accepted) → `acknowledged` (reason `broker_acknowledged`) → `open` (position confirmed). Failures: `rejected` (broker_rejected), `failed` (submission_failed — order_send never ran), `unknown` → `reconciliation_required` (timeout / communication_failed — the outcome is never guessed). Transitions stay table-validated, append-only, monotonic, evidence-carrying; the broker ticket persists as `broker_ref`.

6. **Persistence** (`execution_store.py`, schema v1 unchanged): the intent row persists the full canonical request facts (instrument/side/quantity/SL/TP); the acknowledgement JSON (+ measured broker latency) persists as transition evidence; the ticket as `broker_ref`; all writes atomic and append-only; restart recovery unchanged (pre-dispatch → failed; in-flight → unknown → reconciliation_required).

7. **Idempotency** — a duplicated submission can never create two broker orders: the route REQUIRES an `Idempotency-Key`; the orchestrator consults the durable store (new `intent_by_idempotency_key`) before creating an intent and replays the original intent's durable outcome (`deduplicated: true`, zero adapter calls) — restart-safe because the store is durable; duplicated broker acknowledgements (two intents, one ticket) are a `duplicate_broker_reference` reconciliation fault.

8. **Reconciliation** (`reconciliation.py`, still read-only, no corrective action): OPEN market orders join the broker-reference integrity scan (an open order without a ticket = `missing_broker_reference`) and contribute their tickets as POSITION lineage (a broker position matching an open intent is expected; one without lineage remains `broker_only_position`); all discrepancy classes preserved; account mismatch remains a hard failure.

9. **Telemetry** (`/api/execution/state` `marketOrder` block): derived entirely from the durable store (provenance `durable-store`) — pending submissions, awaiting-reconciliation count, active market orders, submission failures, last submission (state, ticket, ack status, final reason, measured latency), last broker ticket; store unavailable is reported explicitly. The broker block's `readOnly` now DERIVES from `supportsLiveWrite` instead of being asserted.

10. **UI** (`MarketOrderPanel.tsx`): exactly one control — *Submit Market Order* — disabled unless every backend readiness gate passes, behind an explicit confirmation dialog, labelled **LIVE EXECUTION · ONE MARKET ORDER · NO AUTOMATION**; renders acknowledgement, ticket, lifecycle state, denials (stage + machine code), deduplicated replays and latency. No other execution control exists in the SPA; the read panel's obsolete "execution is structurally impossible" caption was corrected.

**Boundary.** LIVE-2 enables exactly one executable broker operation: `submit_market_order()`. All other broker mutations — pending orders, stop/limit orders, order modification, SL/TP modification, partial close, position close, order cancellation — remain structurally unavailable (inert verbs, absent capabilities). No strategy automation, no scheduling, no autonomous execution exists. Live MT5 submission additionally remains gated behind facts that do not yet exist in this repository: a real (non-mock) authorization provider and grant-issuance flow, a configured expected account fingerprint with healthy fresh node telemetry, and an active execution mode — under the shipped configuration, every live market order still denies at the safety gate.

### ADR-LIVE-3 — Controlled position and order management (LIVE-3, 2026-07-27)

**Context.** LIVE-2 enabled exactly one executable operation (market-order submission) but left no way to manage the resulting risk: no durable operator authorization, no execution-mode governance, and no modify/cancel/close path. LIVE-3 completes the manual-management vertical: **manual, individually confirmed position and order management only. It does not enable autonomous execution, strategy-triggered execution, pending-order creation, bulk mutation, remote activation or unattended trading.**

**Decision.**

1. **Durable operator authorization** (`command_authorization.DurableOperatorAuthorizationProvider`, store schema v2 `auth_grants` + append-only `auth_grant_events`): immutable grants carrying operator identity, issued/expiry timestamps, deployment scopes, an EXACT account-fingerprint scope (no wildcard account scope for live), optional exact command list, optional max quantity, confirmation, revocation state, reason and provider — never a credential. Issuance requires confirmation, an approved (local_loopback) profile, the MT5 adapter, and a VERIFIED account-identity match; every requirement denies with a machine reason. Selection (`applicable_grant`) is deterministic: active, adapter-exact, account-exact, scope/command-covering grants ordered by (expires_at, id) — earliest-expiring wins; expired/revoked/malformed rows never authorize. The mock provider remains for the fixture world and still refuses every non-mock adapter. Minimal operator API: `GET/POST /api/authorization/grants`, `POST /api/authorization/grants/{id}/revoke` (operator principal, no-store). A grant continues to override nothing — node health, arming, account identity, broker connection, execution mode, reconciliation, ConnectionPolicy and store health all gate independently.

2. **Execution-mode governance** (`execution_mode.ExecutionModeOwner`, append-only `mode_transitions`): ONE durable owner; modes observe (DEFAULT — all mutations deny) / manual_live (explicitly authorized manual operations only) / halted (only the registry-flagged emergency de-risking operations). Transitions require operator, reason and confirmation; entering manual_live requires EVERY activation gate healthy (approved profile, MT5 write-capable connected adapter, account match, fresh node telemetry, clean reconciliation, a valid applicable grant, available store); observe/halted are always reachable. **Restart never fabricates activation**: a durable manual_live found at process start is automatically downgraded to observe with an audited `process_restart_requires_reactivation` transition; halted survives restart. No environment variable and no strategy code path can set the mode (test-pinned). API: `GET/POST /api/execution/mode`.

3. **Exactly four executable operations** (registry SURFACE_EXECUTION): `SubmitMarketOrder` (LIVE-2) + `ModifyPositionProtection`, `CancelPendingOrder`, `ClosePosition`. Canonical immutable requests (`ModifyPositionProtectionRequest` — at least one level, levels are finite positive prices, None = UNCHANGED so a stop can never be removed; `CancelPendingOrderRequest`; `ClosePositionRequest` — a supplied quantity is rejected at construction: FULL close only, partial close denies explicitly). No generic broker-command payload exists. Cancel/close are risk-reducing and are the only halted-available commands; modify is not risk-reducing and requires clean reconciliation.

4. **Safety policy** — no route-local logic: structural validation + FRESH canonical snapshot verification (`stale_broker_snapshot` denies; entity must exist in the fresh snapshot; instrument mismatch denies; direction unverifiable denies; a worsened stop denies `risk_increasing_stop`; SL/TP ordering contradictions deny) live in the orchestrator's validators; the safety engine gained MODE_MANUAL_LIVE/MODE_HALTED (halted permits ONLY the flagged commands, all other gates still applying); capability/profile/connection checks per command; entity discrepancy + lock gates in the orchestrator before dispatch; the durable store is required before any dispatch. Market-order submission and protection modification remain denied in halted mode.

5. **Lifecycle** — acknowledgement is NOT final: `created → validated → ready(safety_allowed) → modify_pending|cancel_pending|close_pending → acknowledged(broker_acknowledged) → modified|cancelled|closed` where the terminal transition is recorded ONLY by `reconciliation.confirm_operations` observing the change in a fresh snapshot (`reconciliation_confirmed`). Failures: rejected (broker_rejected), failed (submission_failed — order_send never ran), unknown → reconciliation_required (timeout/communication_failed — the entity freezes). Restart recovery unchanged (in-flight → unknown → reconciliation_required).

6. **Concurrency** (`entity_locks`, durable): ONE in-flight mutation per broker entity — the lock is claimed atomically before dispatch (`entity_locked` denies a second claimant), survives restart, is released only by a terminal outcome or reconciliation evidence (never expiry), and stays held through acknowledged/ambiguous states — a close-vs-modify race cannot double-dispatch, and duplicate idempotent replays do not contend (they short-circuit before locking).

7. **Persistence & idempotency** (schema v1→v2 additive migration, explicit and fail-closed): grants, revocations, mode transitions, operation requests (intent rows), acknowledgements + measured latency (transition evidence), entity references (`broker_ref`), locks — all append-only/atomic. A duplicated idempotency key replays the durable outcome with ZERO broker calls (restart-safe); a CHANGED payload under the same key denies `idempotency_conflict`.

8. **Reconciliation** (still read-only, zero corrective operations): confirms modifications (requested vs observed SL/TP), cancellations (order absent) and closes (position absent); an acknowledgement without an observed change is a persisted `status_mismatch` discrepancy and the entity stays locked; ambiguous (frozen) operations resolve with evidence to confirmed or `operation_not_applied`→failed; a stale snapshot confirms NOTHING; an unresolved same-entity discrepancy denies further mutation (`entity_discrepancy_unresolved`); account mismatch remains a hard failure blocking everything.

9. **Telemetry** (`/api/execution/state` `governance` block, derived-only): current mode (+ provenance durable-store), active grant summary + expiry (redacted), in-flight operations, acknowledged-awaiting-confirmation (DISTINCT from confirmed), reconciliation-required operations, failures, last operation with final reason + latency, active entity locks.

10. **UI** (`ManualExecutionPanel`): mode selector, grant issue/revoke, per-entity modify SL/TP / cancel / close — every mutation gate-disabled and individually confirmed; labels **LIVE MANUAL · NO AUTOMATION · LOCAL LOOPBACK ONLY**; masked account; acknowledgement rendered separately from the reconciled result; no bulk close, no flatten-all, no pending-order creation, no quantity increase; token memory-only, no browser persistence.

**Activation & rollback.** Activation: issue a scoped grant (`POST /api/authorization/grants` — requires verified account identity) → verify gates (`GET /api/execution/mode`) → enter manual_live (confirmed, reasoned) → operate. Rollback at any point: `POST /api/execution/mode {mode: observe|halted}` (always available), revoke grants, or restart the process (manual_live never survives restart).

**Boundary.** LIVE-3 enables manual, individually confirmed position and order management only: exactly four executable operations through the one canonical pipeline. Pending/limit/stop order creation, SL removal, risk-increasing modification, partial close, bulk mutation, strategy/autonomous execution and remote profiles remain structurally unavailable. Under the shipped configuration every live mutation still denies until an operator explicitly issues a grant and activates manual_live with all gates healthy.

*The audited post-LIVE-3 state of the system — ownership map, pipeline, state machines, failure matrix and LIVE-4 entry criteria — is recorded in `LIVE-3-ARCHITECTURE-CHECKPOINT.md`, which is the canonical reference for that material.*

### ADR-LIVE-4A — Operational read model and Control Tower projection (LIVE-4A, 2026-07-27)

**Context.** After LIVE-3 the Control Tower answered a dozen independent endpoints, and each UI panel re-derived its own version of "what is happening" — joining broker reads, node telemetry, execution state and reconciliation locally. Operational truth therefore had no single owner on the read side, and two panels could disagree. **LIVE-4A introduces the canonical operational read model. It introduces no new execution capability.**

**Decision.**

1. **One projection owner** — new `backend/operational_projection.py`. It consumes broker snapshots, node telemetry, execution-store lifecycle, reconciliation posture, execution mode, authorization state and entity locks, and produces immutable read models. It holds no adapter, issues no command, owns no persistence and caches no mutable state. Structural tests pin that it references no `get_broker`, no `order_send`, no store write, no socket, and imports no runtime owner.

2. **Immutable canonical read models** — `NodeOperationalView`, `AccountOperationalView`, `OrderOperationalView`, `PositionOperationalView`, `ScenarioProjection`, `OperationalSummary`. All frozen dataclasses with tuple collections and deterministic `as_dict()` (sorted keys at every level, JSON-safe). **Orders and positions are separate concepts and are never merged** — distinct types with distinct field vocabularies.

3. **Deterministic, injected-clock builder** — every `build_*` function takes an explicit `ProjectionSources` bundle of read-only callables plus `now`. It reads no clock, environment or global, so identical sources at an identical `now` produce byte-identical output. Restart-safe by construction: the projection holds nothing between calls. Projection order is Broker → Execution Store → Lifecycle → Reconciliation → Telemetry → Mode → Authorization → Projection.

4. **Explicit freshness** — every time-sensitive model carries a `Freshness` (`projectionAt`, `sourceAt`, `ageSeconds`, `stale`, `available`, `status`). **UNAVAILABLE (the source could not be read) and STALE (read but too old) are distinct states** and are never collapsed into a zero or a healthy-looking default; an unparseable timestamp is stale, never fresh.

5. **Explicit provenance** — every model carries `provenance` (`live_mt5` / `mock-fixture` / `node-telemetry` / `durable-store` / `absent`), so a fixture value can never be mistaken for live broker truth.

6. **Nothing is invented** — a missing broker snapshot yields no positions (not an empty "healthy" list); a missing account read yields an explicitly unavailable account (not zeroed balances); non-derivable fields (`realizedPnLToday`, `openRisk`, position `currentPrice`, position age) are reported absent rather than estimated; absent node telemetry yields one explicitly-unavailable node view rather than an empty list.

7. **Unified read-only API** — `GET /api/operations/{summary,nodes,accounts,orders,positions}` and `GET /api/operations/{node,order,position}/{id}`. All operator-authenticated by the deny-by-default classifier, all `Cache-Control: no-store`, all derived. **No handler aggregates anything** — each delegates to the projection owner (structurally pinned).

8. **Scenario foundation (placeholder only)** — no scenario entity exists in this repository; the only evidence is a `scenarioKey` string echoed in stored command payloads. `ScenarioProjection` projects exactly that and nothing else. No strategy logic and no scenario generation was added.

9. **Trade-ledger foundation (interfaces only)** — `TradeLedgerProjection`, `ClosedTradeProjection`, `LedgerSummary` pin the shape a future ledger slice must satisfy. No persistence, no analytics: constructing the ledger today reports `available: false`, `ledger_not_implemented`.

10. **Projection-driven UI** — new `OperationalDashboard` with a **single** polling source (`api.operationsSummary`). Node, account, order, position, operations, reconciliation, warnings and system-health cards each receive an already-projected model; formatting lives in one set of shared helpers. Badges are explicit: **LIVE / MOCK / NODE / STALE / UNAVAILABLE / RECONCILIATION REQUIRED**. The dashboard is read-only — it renders zero buttons and zero inputs.

**Boundary.** LIVE-4A is a READ MODEL. It adds no broker execution, no strategy logic, no autonomous behaviour and no change to the execution pipeline: the execution surface remains exactly the four LIVE-2/LIVE-3 operations (test-pinned), and the projection cannot write, execute or persist. The UI no longer reconstructs operational truth — it reflects the projection.

### ADR-LIVE-4B — Canonical Scenario domain (LIVE-4B, 2026-07-27)

**Context.** The trading lineage had no parent. Recommendations, intents, orders and positions each existed independently, joined only by a `scenarioKey` STRING (`instrument:session:structure:direction:entryModel`) carried on fixture records — a convention, not an entity. LIVE-4A could therefore only project that string as a placeholder. Without a canonical parent, no future trade can be attributed, and no ledger or analytics slice can aggregate honestly. **LIVE-4B introduces the canonical Scenario domain. It introduces no strategy engine. It introduces no autonomous trading.**

**Decision.**

1. **The Scenario is the canonical parent object** — new `backend/scenario_domain.py`. Target lineage: `Scenario → Recommendation → Intent → Order → Position → Deals → Ledger → Analytics`. Every future trade must be traceable to exactly one Scenario.

2. **Immutable, validated models** — `Scenario` (frozen; natural key, timeframe, status, node, account fingerprint, timestamps, expiry, explicit link tuples, tags, bounded JSON-safe metadata, provenance), `ScenarioId`, `ScenarioStatus`, `ScenarioOutcome`, `ScenarioEvent`, `ScenarioSummary`. All serialize with sorted keys and are JSON-safe; invalid natural keys, directions, statuses, oversized metadata and excess tags are rejected at construction.

3. **Deterministic identity** — `ScenarioId.derive()` hashes the natural key plus an explicit discriminator, so the same setup always yields the same `scn_<16 hex>` id (replay- and rebuild-stable). `ScenarioId.from_key()` parses the existing fixture convention; malformed keys raise rather than being partially guessed.

4. **Explicit lifecycle** — 15 statuses (`CREATED … REJECTED`) with an explicit forward table plus abandonment (`INVALIDATED`/`EXPIRED`/`CANCELLED`) reachable from every non-terminal status. Terminal statuses are immutable. Transitions require a reason and monotonic time, and are PURE — `transition()` returns a new Scenario and never mutates its input. Illegal pairs raise `ScenarioError("invalid_transition")`.

5. **Append-only durable store** — new `backend/scenario_store.py`, its OWN database file and schema version. `scenario_events` is append-only (no update/delete surface); the `scenarios` table is a snapshot that can be discarded and rebuilt deterministically from events alone (`rebuild_scenario`). Writes are atomic and idempotent on `(scenario_id, sequence)`; creating the same scenario twice appends nothing. Nothing is created or updated automatically. The store owns scenarios ONLY — it never touches execution, reconciliation, authorization, mode or lock tables (structurally pinned).

6. **Explicit relationships** — links are identifier tuples on the Scenario (`linked_recommendation_id`, `linked_intent_ids`, `linked_order_ids`, `linked_position_ids`), recorded through `link()`: idempotent, sorted, conflict-detecting. Nothing is inferred and nothing is reverse-reconstructed — an unrecorded link does not exist.

7. **Projection integration** — the LIVE-4A placeholder is REPLACED. `ScenarioOperationalView` is built from the scenario store with linkage counts, age, masked account fingerprint, freshness and `durable-store` provenance. An unreadable store yields an empty projection AND an explicit `scenarios_available() == False` — an empty list never claims "no scenarios exist".

8. **Read-only API** — `GET /api/scenarios` (with `instrument`/`session`/`nodeId`/`status` filters and a deterministic summary), `/api/scenarios/active`, `/api/scenarios/history`, `/api/scenarios/{id}` (view + append-only history). Operator-authenticated, `no-store`, deterministically ordered, projection-owned, honest 404/503.

9. **Execution independence** — the execution pipeline, broker behaviour, routing, authorization and reconciliation are UNCHANGED. `OrderIntent` gains one OPTIONAL `scenario_id` for lineage, persisted via an additive nullable column (execution store schema 2 → 3, explicit fail-closed migration). The orchestrator CAPTURES it verbatim at intent creation and nothing else: no validator, gate, policy or adapter reads it, and no execution module imports the Scenario domain (all structurally pinned).

10. **Ledger preparation** — `TradeLedgerProjection`, `ClosedTradeProjection` and `LedgerSummary` now carry the scenario dimension (`scenarioId`, `scenarioIds`, `byScenario`). Interfaces only: still no persistence, no analytics, and the ledger still reports `available: false`.

11. **UI** — a read-only Scenario section (`ScenarioPanel`) listing identity, status, natural key, node, linkage counts, age and freshness, with status/instrument/session/node filters. No editing, no mutation, no strategy control (zero buttons, zero inputs; only the four filter selects).

**Boundary.** LIVE-4B introduces the canonical Scenario domain: the parent entity, its lifecycle, its append-only store, its projection and its read-only surfaces. It introduces NO strategy engine, NO signal generation, NO scenario generation and NO autonomous trading — every scenario and every transition is an explicit, caller-supplied fact. The four executable operations and every ARCH-1…LIVE-3 safety guarantee are untouched.

### ADR-LIVE-4C — Canonical Trade Ledger and closed-trade reconstruction (LIVE-4C, 2026-07-27)

**Context.** LIVE-4A/4B delivered the operational read model and the Scenario parent, but the historical economic result of a trade had no owner: the ledger existed only as pinned interfaces. **LIVE-4C introduces the canonical historical Trade Ledger. It introduces no new broker execution capability. It introduces no strategy engine. It introduces no performance analytics beyond ledger totals.**

**Broker-history audit (grounded, and the reason PART 1 was necessary).** Before this slice the broker abstraction did NOT expose enough evidence to reconstruct a closed trade: `history_deals_get` was reached only via `getattr` inside `broker.recent_executions` (optional, 1-day window, 50-deal cap); `history_orders_get` was never called; the canonical `BrokerDeal` carried no position id and no `DEAL_ENTRY` direction; commission / fee / swap appeared nowhere in production; account margin mode was never read; and `MockBroker` had no deal evidence at all.

**Decision.**

1. **Read-only broker-history contract** — new `backend/broker_history.py`: `BrokerDealRecord` (position id + `DEAL_ENTRY` + per-deal `BrokerCostEvidence`), `BrokerHistoricalOrder`, `BrokerClosedPositionEvidence`, `BrokerHistorySnapshot`, plus an explicit account-mode vocabulary. It maps ONLY fields the MetaTrader5 SDK genuinely provides, each probed defensively, and reports capability gaps as `deals_available` / `orders_available` / `costs_available` flags. A failed read is `unavailable`, never an empty history. There is no write method, and no execution import (structurally pinned). Both an MT5 reader and a deterministic mock reader are implemented; the mock reports costs as UNAVAILABLE because the fixture world records none.

2. **Deterministic trade identity** — `TradeId.derive()` hashes ONLY immutable broker lineage (account fingerprint, position id, instrument, opening deal id). Financial values are deliberately excluded, so identity is stable across restart, ledger rebuild, reconciliation rerun, repeated history reads and **late-arriving cost evidence**.

3. **Deterministic reconstruction owner** — new `backend/trade_reconstruction.py`: a pure function of its inputs (no I/O, no clock, no persistence, no broker call). Deals group by broker POSITION ID; `DEAL_ENTRY` splits entries from exits, so one order producing several deals, several entry orders on one position, several closing deals, partial close, scale-in and scale-out are all handled by construction, with volume-weighted entry and exit prices.

4. **Account-mode honesty (fail closed)** — grouping by position id is only sound when a position id identifies one economic trade, which holds on HEDGING accounts. On `netting`, `exchange` or `unknown` the trade is still fully reconstructed and VISIBLE, but a blocking reason prevents finalization. Nothing is silently reconstructed under hedging assumptions.

5. **Ledger lifecycle** — `OBSERVED → RECONSTRUCTING → {INCOMPLETE | READY_TO_FINALIZE | CONFLICTED} → FINALIZED → AMENDED`. Settled truth is never silently overwritten: `FINALIZED` may only move to `AMENDED` (a recorded amendment) or `CONFLICTED`; a refresh of a finalized entry is a no-op. Finalization requires sufficient closing evidence and raises otherwise.

6. **Append-only store** — new `backend/trade_ledger_store.py`, own file and schema version. `ledger_events` has no update or delete surface and no generic CRUD; entries rebuild deterministically from events alone; ingestion is idempotent on `(trade_id, sequence)` with deterministic event ids; writes are atomic; a newer schema fails closed. It owns ledger facts only — never execution, scenario or broker state.

7. **Financial accounting: zero ≠ unavailable** — every group carries a completeness marker (`complete` / `partial` / `pending` / `unavailable` / `not_applicable` / `conflicted`) and every value is `None` when absent. Net PnL stays unavailable while costs are unavailable; a measured zero cost is preserved as `0.0`.

8. **Realized-R policy (canonical, tested)** — R is computed from **GROSS** realized PnL divided by the initial risk amount, and every record states `realizedRBasis: "gross"`. Gross is canonical because cost evidence frequently arrives late; an R that silently changed when a swap settled would not be comparable. Initial risk comes ONLY from the ORIGINAL submit intent's recorded stop — a later protective modification is never used as the initial stop. Without grounded initial-risk evidence, `realizedR` is unavailable and `riskCompleteness` is incomplete.

9. **Exit classification** — explicit broker reason (MT5 `DEAL_REASON_*`) always wins; otherwise proximity to a recorded protective level within a documented instrument-precision tolerance (default 5 points); a partial close is classified by quantity; otherwise `UNKNOWN`, which is preferred to invented certainty.

10. **Scenario lineage and reconciliation gates** — lineage resolves from EXPLICIT identifiers only (intent `scenario_id`, and Scenario-side linked intent/order/position ids). Multiple distinct scenarios is a CONFLICT that blocks finalization; absent linkage does not fabricate a Scenario and, by documented policy, does not block finalization (legacy/manual/external trades remain visible with `scenarioId` unavailable and an explicit `origin`). Unresolved MATERIAL reconciliation findings (account identity, duplicate/missing broker reference, quantity mismatch, unknown) block finalization; non-material findings are recorded and visible but do not gate.

11. **Projection, API and UI** — the placeholder ledger interfaces are replaced by `TradeLedgerOperationalView`, `ClosedTradeOperationalView` and `LedgerOperationalSummary`. Six read-only endpoints (`/api/ledger/summary`, `/trades`, `/trades/{id}`, `/trades/{id}/history`, `/incomplete`, `/conflicts`) are operator-authenticated, `no-store`, deterministically ordered, filtered, paginated with a bounded page size, and honest about 404/unavailable; no handler performs accounting. Ingestion is an internal read-side service (`refresh_trade_ledger`), NOT an HTTP mutation endpoint and NOT an autonomous loop. The read-only UI renders recorded values verbatim — an unavailable figure shows as "—", never zero — with FINALIZED / INCOMPLETE / AMENDED / CONFLICTED / COSTS PENDING / RISK UNAVAILABLE / SCENARIO UNLINKED / RECONCILIATION REQUIRED badges, and no edit, delete or adjustment control.

**Non-goals (explicit).** No win rate, expectancy, drawdown, equity curve, or grouping by strategy/session/instrument. No new broker execution capability, no new execution command, no change to execution authorization, safety, routing or reconciliation write behaviour, and no automated Scenario creation. Backfill is provided as a bounded, idempotent interface shape only — an unbounded full-account history scan is never run.
