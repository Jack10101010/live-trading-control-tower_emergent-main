# MVP Execution Plan — One Live Strategy Instance

**Priority change:** time-to-first-functioning-live-instance now outranks platform completeness. Broad product development is paused.
**Frozen authority:** `CONTROL-TOWER-ARCHITECTURE-V1.md` (V1.2). This plan implements its §10 build order for exactly one instance — it proposes no new architecture.
**Behavioral authority:** the Lux backtester (`Lux-OB-Backtester/src/`) + the Research Lab deployed policy. The Control Tower UI/prototype backend are reference surfaces only — **verified: the prototype backend imports nothing from Lux and contains no swings/BOS/OB logic. It is not proof of strategy correctness.**
**Audited on:** 2026-07-16, against the actual repositories (Lux `src/`, FX-OB-Research-Lab `deployedPolicy.v1.json`, Control Tower `backend/` + `frontend/`).

---

## A. Current-State Truth Table

Every strategy-scope component: where the authoritative implementation actually lives, what the Control Tower (CT) has today, and what gates paper/live. "CT status" legend: **UI-only** (fixture render), **proto** (prototype backend, fixture math), **absent**.

| Component | Authoritative implementation | CT status | Reusable? | Blocker? | Before paper? | Before live? |
|---|---|---|---|---|---|---|
| Market/session clock (UTC schedule Asia/London/Lull/NY/NY-PM/Outside) | `Lux src/execution.py` `_SESSION_SCHEDULE` (L1668) + `_session_of` | UI labels only | Extract | Core | ✅ | ✅ |
| Detection timeframe (15min from 1m) | `Lux src/resample.py` + config `detection_timeframe` | proto fake (`market_data.py` derives OHLC from regime components) | Extract; discard proto derivation | Core | ✅ | ✅ |
| Execution timeframe (1min walk) | `Lux src/execution.py` simulation loop | absent | Extract | Core | ✅ | ✅ |
| Swings (Pine-parity, right-window confirm) | `Lux src/structure.py` `detect_swings` (43 lines, pure) | absent | **Yes, nearly as-is** | Core | ✅ | ✅ |
| BOS / CHoCH | `Lux src/order_blocks.py` `detect_order_blocks` (structure break branches) | UI badges only | Extract | Core | ✅ | ✅ |
| OB creation / filtering / mitigation / invalidation | `Lux src/order_blocks.py` (`_make_order_block`, mitigation in detect loop, breach helpers in `execution.py`: `_is_fully_breached`, `_price_has_exited_ob_entry_side`) | UI overlay only | Extract | Core | ✅ | ✅ |
| ATR vs cumulative-mean-range filter | `Lux src/order_blocks.py` `_pine_rma(TR,200)` vs `TR.cumsum()/n`; `ob_filter` config | absent | Extract | Core | ✅ | ✅ |
| Min/max OB sizing | `Lux src/order_blocks.py` `_passes_size_filter` (`min_ob_size_pips`=0, `max_ob_size_pips`=100 in production config) | absent | Extract | Core | ✅ | ✅ |
| Delayed-entry threshold (triggered edge @ trigger %) | `Lux src/execution.py` `_triggered_edge_touched`, `_penetration_price`, thresholds `[25]` | absent | Extract | Core | ✅ | ✅ |
| Wait-candle / delay-window logic | `Lux src/execution.py` `_delay_window_init/_update`, `triggered_edge_candle_delays`, same/next-candle modes | absent | Extract | Core | ✅ | ✅ |
| Candidate expiry / cancellation (FFT, retrace, breach, delay validity) | `Lux src/execution.py` (`cancel_on_first_failed_tag`, `cancel_on_retrace` + pips/OB-%, `_fft_*`, `_apply_delay_validity_fields`) | absent | Extract | Core | ✅ | ✅ |
| News blackout (before/after windows, impacts, currencies, pause/cancel/flatten) | `Lux src/execution.py` `prepare_news_cache`, `_news_blackout_match`, `_news_flatten_match`, `_apply_news_blackout`, `_apply_news_touch_cancel` | absent | Extract | Core + **live calendar feed** | ✅ (historical file OK) | ✅ (forward calendar required) |
| Session filters | `Lux src/execution.py` `_apply_session_filter` + `allowed_sessions` (production: filter **off**; sessions used as cohort labels) | UI only | Extract | Core | ✅ | ✅ |
| Stop placement | `Lux src/execution.py` `_planned_trade` (`stop_buffer_pips`, structure-side stop) | proto fake | Extract | Core | ✅ | ✅ |
| Target selection | `Lux src/execution.py` (`rr_multiple`; per-cohort/state targets via `session_strategy_scenario` + deployed policy) | UI matrix only | Extract | Core | ✅ | ✅ |
| Risk sizing | Config `%`-risk convention + pip math (`_to_pips`, pip_size/tick_size); account-level sizing is **driver** responsibility per V1.2 | proto fake (`risk_engine.py` fixture math) | Extract convention; driver implements sizing | Core+driver | ✅ | ✅ |
| Break-even / management | `Lux src/execution.py` BE columns (`be_trigger_basis`, `be_triggered`, arm/stop/exit machinery) + risk-reduction v1 (`move_stop`) | UI timeline only | Extract | Core | ✅ | ✅ |
| Execution eligibility (regime gate, portfolio/policy gate, direction) | `Lux src/regime.py` (6-state, leakage-safe) + `src/portfolio_policy.py` + `execution.py` `_regime_filter_blocks`, `_portfolio_policy_block_row` | proto reimplementation (fixture policy) — **not authoritative** | Extract Lux versions; quarantine proto | Core | ✅ | ✅ |
| Costs (spread/slippage/commission in R) | `Lux src/execution.py` `_cost_r_components` | absent | Extract | Core | ✅ (recorded) | ✅ |
| Production configuration | Lux run configs (`_confirm_nybosshort_*.json`, `_state_target_discovery_config.json`) + **`deployedPolicy.v1.json`** (`policy_version 2026-07-09.te-v1.2-surgical-disable`, sha `86ff709c…`, 48 cohorts) | fixture package (invented v17/v18) — **not the real policy** | Yes (as data) | Scope lock | ✅ | ✅ |
| Historical dataset | `Lux data/candles/EURUSD_1m*.csv` (configs reference `EURUSD_1m_extended_2015_2026.csv`) + `master_economic_calendar_2020_present.csv` | fixture only | Yes | Pin exact file+hash | ✅ | — |
| Event journal | Architecture V1 §2 (contract defined) | proto has event-ish overlay (not per-instance, not V1 envelope) | Build to V1 contract; don't reuse proto storage | M4 | ✅ | ✅ |
| Live market data feed | **absent everywhere** (Lux is CSV; proto derives from fixture) | absent | Build (Data Service: broker-feed or Polygon per V1 §4.2) | **Yes — paper blocker** | ✅ | ✅ |
| Broker adapter (MT5) | **absent** (proto `broker.py` = MockBroker + a clean `Broker` interface seam) | proto seam | Reuse the *interface shape*; build MT5 adapter process per V1 §1 | **Yes — live blocker** | read-only ✅ | write ✅ |
| Risk Guard (daily-loss, drawdown, caps, halt) | V1 §4.4 (spec) ; Lux has no account-level guard | proto `risk_engine.py` fixture math — not authoritative | Build fresh to V1 spec | **Yes — live blocker** | — | ✅ |
| Reconciler | V1 §4.6 (spec) | proto `broker_sync.py` (mock-only) | Build against MT5 read-only | partial ✅ | ✅ |
| Notifier (Telegram/push, dead-man) | V1 §4.8 (spec) | absent | Build | — | ✅ |
| Control Tower UI (fleet, inspector, decision chain, replay, matrix) | — | **Complete, high quality** | **Yes — as observation surface**, repointed at the node's Gateway later | No | optional | optional |

**Blunt summary:** the strategy exists **only** in Lux. The CT prototype backend (≈5,000 lines: `strategy.py`, `market_data.py`, `risk_engine.py`, `portfolio.py`, `execution.py`, `scheduler.py`, `broker_sync.py`) is architecture-*shaped* but strategy-*empty* — useful as interface reference (esp. `broker.py`'s seam), **dangerous if mistaken for the strategy**. It gets **quarantined** (kept running to serve the UI, excluded from the live path). The trading node is built per V1.2 with the Lux-extracted core.

---

## B. Critical Path (ordered; nothing else)

### MILESTONE 1 — Scope lock (≤1 day)
Pin, in a single committed `SCOPE-LOCK.md` in the trading-node repo:
- **Symbol:** EURUSD. **Detection TF:** 15min. **Execution TF:** 1min. (From the production configs — do not invent.)
- **Strategy version:** SMC delayed-entry / triggered-edge, `swing_length 50`, `ob_filter "Atr"`, thresholds `[25]`, `min/max_ob_size 0/100 pips`, news blackout **on** (30/30, high impact), session filter **off**, `reverse_touch_cancel false` — i.e., exactly the values in `_confirm_nybosshort_bullexp_A_rr4.json` / `_state_target_discovery_config.json` (they agree).
- **Policy:** `deployedPolicy.v1.json` `policy_version 2026-07-09.te-v1.2-surgical-disable` (sha `86ff709c…`). **One cohort only** — operator picks from the enabled EURUSD cohorts (top candidates by the policy's own numbers: `EURUSD|asia|bos_short` LABEL n=50 +19.2R; `EURUSD|ny_pm|choch_short` DIRECTION_AWARE HIGH n=66 +7.6R; `EURUSD|outside|choch_long` STATE_ONLY HIGH n=81 +12.0R). Its `decision_policy.regime` + target/risk come from the policy + the confirm-run config (`rr_multiple 2` baseline; RR4 variant only if that is what the Lab confirmed).
- **Dataset:** the exact candle CSV the reference run used (`EURUSD_1m_extended_2015_2026.csv` per configs — locate, pin path + SHA256) + `master_economic_calendar_2020_present.csv` (pin hash).
- **Reference run:** one specific Lux run (config JSON + outputs dir) regenerated fresh and archived immutable. This is Golden Run #1.
- **Account:** one demo/FTMO-style MT5 account (name it).

### MILESTONE 2 — Shared core extraction (V1.2 §10.1)
Create the pure core package (new `core/` in the trading-node repo or a standalone package):
- Move/wrap **as-is**: `structure.py` (already pure), `order_blocks.py` (pure given a DataFrame), `regime.py` (compute-only), `portfolio_policy.py`.
- Split `execution.py`: strategy/candidate/management logic (triggered-edge, delay windows, cancels, news gates, session labels, BE/risk-reduction, cost model) → core; CSV/report/sweep/multiprocessing scaffolding stays behind in Lux as the `BacktestDriver`.
- Core contract per V1.2 §3: events + `Clock` port in, events out; **no I/O, no wall clock, no randomness**. Event names = V1.2 §2.1 families.
- **Behavior-preserving only.** No cleanups, no renames of semantics, no "while we're here."

### MILESTONE 3 — Golden parity (V1.2 §3 migration test)
- Run Golden Run #1 through (a) untouched Lux and (b) the extracted core via a thin BacktestDriver, on the pinned dataset.
- Compare **event-for-event** where the core emits events (swings, BOS/CHoCH, OB lifecycle, candidates, cancels, entries, exits) and **artifact-for-artifact** against Lux's canonical outputs (trades CSV rows: entry/stop/target prices, timestamps, `be_triggered`, outcome, R) — Lux does not emit an event stream today, so its trades/OB records are the comparison anchor; the core's event log must reconcile to them exactly.
- Freeze 3–5 windows as the permanent golden-replay CI suite (I-2/I-3).
- **Hard gate: no paper trading until parity is exact or every diff is written down and operator-approved.**

### MILESTONE 4 — Journal + deterministic replay minimum (V1.2 §10.2)
- Implement the journal **interface** (append/read/tail/snapshot) with one SQLite file per instance; V1.2 §2.2 envelope on every event (`event_time` vs `wall_time`, `account_id`, `config_version`, idempotency key on intents).
- Replay driver = feed journal bars through the core on a virtual clock; assert byte-identical re-emission (I-6).
- **Only** what this one instance needs. No replay UI work.

### MILESTONE 5 — Paper/demo driver (V1.2 §10.3–10.4)
- Data Service minimum: live EURUSD 1m bars (broker feed via MT5 read-only adapter, or Polygon), gap backfill, DST-safe session/calendar service; forward news calendar ingestion (the FF tooling in Lux `data/` is the existing precedent — pin a weekly import procedure if no API).
- Paper driver: same core, live bars, simulated fills; record every bar, decision, veto, intended order, simulated fill, spread/slippage/timing delta, and reconciliation report — all as journal events.
- MT5 adapter **read-only** (positions/account/quotes) + Reconciler running against the demo account.
- Exit only after N sessions of clean operation (see D).

### MILESTONE 6 — Safety gate (V1.2 §4.4–4.6, §6; I-10)
Before any real order, demonstrate each by a forced drill, not by code review: broker-side SL on every order · per-trade risk cap · daily-loss/drawdown halt (Risk Guard in-path) · idempotent order intents (kill/restart mid-submit produces no duplicate) · reconciliation catches an injected mismatch · restart recovery (snapshot+tail replay resumes correct state) · feed-loss → safe state · halt/flatten command works from CT **and** with CT offline · Notifier (Telegram) independent alerts · execution-capability validation against the adapter's declared capabilities.

### MILESTONE 7 — Small live launch
- One instance, minimum practical risk (e.g. fixed fractional ≤0.25% or FTMO-min lot), explicit operator promotion (journal-audited command), automatic halt on unexplained live-vs-paper divergence (any fill, veto, or decision the shadow replay can't reproduce → flatten + halt + notify).
- Daily shadow-replay of the live journal through the core must reproduce the day event-for-event.

---

## C. Deferred Backlog (intentionally frozen — do not touch)

Chart polish/Inspector expansion/drawing tools/overlay cosmetics/multi-chart layouts · multi-symbol beyond EURUSD · generic plugin loading beyond V1.2 §11's static registry · advanced Portfolio Manager behavior (cross-strategy allocation; the Risk Guard seam is reserved) · Historical Explorer refinement · further prototype backend engines (`strategy.py`, `market_data.py`, `portfolio.py`, `risk_engine.py` are **quarantined**: keep serving the UI fixture; no new work; delete or repoint only after the node's Gateway exists) · full config-editor UX (manifests can come later; MVP config is a versioned JSON) · CT UI expansion generally (V1.2 §10.6: "the node never waits for the UI") · fixture-world/Track-B contract evolution · frontend test suites, virtualization, WebSocket work in the CT repo.

---

## D. Exit Criteria (objective pass/fail)

| Milestone | Pass condition |
|---|---|
| M1 | `SCOPE-LOCK.md` committed: symbol/TFs/strategy params/cohort/policy sha/dataset SHA256/reference-run path/account named. Golden Run #1 archived immutable. |
| M2 | Core package imports no I/O/time/network (import-lint passes, I-1); Lux still runs untouched; core compiles + unit smoke on 1k bars. |
| M3 | Golden Run #1: swings, BOS/CHoCH, OB lifecycle, candidates, cancels, entries, stops, targets, BE events, exits, and per-trade R **identical** (zero unexplained diffs) between Lux and core; golden suite wired to run on any core change. |
| M4 | Kill −9 during a replay; restart resumes from snapshot+tail to the identical state; re-replay of any window is byte-identical (I-2, I-6). |
| M5 | ≥10 consecutive trading sessions of paper: zero unexplained decision diffs vs same-day shadow replay; every intended order journaled with spread/slippage/timing; reconciler clean or every diff explained; gap-backfill exercised at least once. |
| M6 | Every drill in M6 list demonstrably passes, each with a journal record; Risk Guard halts on a simulated daily-loss breach; duplicate-order drill produces exactly one broker order. |
| M7 | First live order carries broker-side SL; risk ≤ locked cap; operator promotion event in journal; divergence-halt drill fired once successfully in live-shadow before enabling; instance survives one full week live or halts for an explained reason. |

---

## E. First Implementation Task

**Task: Milestone 1 — produce `SCOPE-LOCK.md` + Golden Run #1.**
Concretely: (1) locate and SHA256-pin `EURUSD_1m_extended_2015_2026.csv` (or the closest existing production candle file — the configs reference it; `data/candles/` must be checked for the exact artifact) and the news calendar; (2) re-run Lux **unmodified** with `_state_target_discovery_config.json` narrowed to the chosen single cohort's window, archiving config + full outputs as `golden/run-001/`; (3) write `SCOPE-LOCK.md` naming every value in Milestone 1, including the one operator decision this plan cannot make alone — **which single enabled cohort from `deployedPolicy 2026-07-09.te-v1.2-surgical-disable` goes live first** (recommendation: `EURUSD|ny_pm|choch_short`, the highest-confidence HIGH/n=66 enabled cohort with a DIRECTION_AWARE policy; `asia|bos_short` has the larger net-R but MEDIUM confidence).
This is one sitting of work, requires no new code beyond a run script, and everything in Milestones 2–7 keys off its pinned artifacts.

---

*This plan adds no architecture. It executes `CONTROL-TOWER-ARCHITECTURE-V1.md` §10 for one instance. PROJECT_STATE.md is intentionally not updated by this audit; update it once, when Milestone 1 lands.*
