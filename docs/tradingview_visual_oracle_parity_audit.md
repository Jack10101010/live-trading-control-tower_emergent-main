# TradingView Visual Oracle — Phase 0 Production Parity Audit

**Status:** Phase 0 complete (audit only). No Pine Script written. No production behaviour changed.
**Audit date:** 2026-08-03
**Auditor scope:** the Windows VPS production deployment only. The Mac-side Control Tower
checkout, research-lab backtests, and the pre-existing `.pine` files in the Lux repo were
deliberately **excluded** as non-authoritative (see §2.4).

---

## 1. Purpose and authority

### 1.1 What this document is

A verified map of the **decision process actually executed by the live trading node on this
VPS**, produced so that a later Pine Script v6 indicator can mirror its *visible* behaviour for
debugging, historical replay, parity verification and sanity testing.

The Pine indicator is **not** a strategy, execution engine, automation system, or alternative
implementation. Where Python and Pine disagree, **Python wins by definition** — the Pine artefact
is wrong, not the engine.

### 1.2 Evidence standard applied

Every claim below is grounded in code read on this machine at the commits recorded in §2. Where
something is inferred rather than read, it is labelled **[INFERRED]**. Where evidence is missing,
it is listed in §19 rather than guessed. Documentation in the repositories was **not** treated as
evidence: several repo documents describe intentions that the pinned code does not implement
(§19.4).

### 1.3 The single most important finding

**The production engine is not a streaming/incremental SMC engine.** It is a *full historical
recompute* of the entire 2015→frontier backtest, executed once per closed 15-minute boundary,
followed by a **row-level diff of the resulting trades table** against the previous cycle's table.
Broker actions are derived from that diff, not from any live event stream.

Evidence: `live/runner.py:118-156` (`golden_pipeline`), `live/runner.py:185-237` (`run_once`),
`live/intents.py:73-125` (`diff_frontier`).

This has three consequences that dominate the entire Pine design:

1. **The engine has no incremental state to mirror.** Order-block lifecycle, candidate state and
   trade state are all *rebuilt from zero* every cycle inside `simulate_trades`. There is no
   persisted OB collection, no persisted candidate collection, and no persisted lifecycle state.
   The only persisted engine artefact is the previous trades DataFrame (`live/state.py`, consumed
   at `live/runner.py:217`).
2. **The engine can and does retroactively change history.** Because every cycle recomputes from
   2015, a trade row's fields can differ between cycles. `diff_frontier` only inspects
   `fill_time`, `outcome` and `stop`, so most retroactive changes are invisible to execution — but
   they are *not* invisible to a visual oracle. Pine, which is forward-only, cannot reproduce this.
3. **The 15-minute boundary is the only decision clock**, but fills are evaluated on **1-minute**
   candles inside the recompute. A fill and its exit occurring inside the same 15m window are
   detected but explicitly **never sent to the broker** (`SKIP_INTRA_WINDOW`,
   `live/intents.py:70,97-100`).

### 1.4 Authority ranking used in this audit

| Rank | Artefact | Why |
|---|---|---|
| 1 | The 30 files in `live/engine_manifest.json`, verified by hash | Gated at startup; a mismatch refuses to trade |
| 2 | `live/*.py` on the running branch | The live wrapper |
| 3 | The launcher's process environment (`live_state/ops/start-live-shadow.ps1`) | Determines all env-driven config |
| 4 | `generated_configs/d6cdae589b1e4c37a67763253c466067.json` | The pinned Golden config |
| 5 | Repository markdown | **Not evidence.** Used only for orientation |

---

## 2. Canonical production paths

### 2.1 Verified runtime

| Property | Verified value | How verified |
|---|---|---|
| Live process | `python -m live.main`, PID 3404 | `Win32_Process` query, running at audit time |
| Interpreter | `live-trading-control-tower_emergent-main\.venv\Scripts\python.exe` | process command line |
| Control Tower repo | `C:\Users\Administrator\Projects\live-trading-control-tower_emergent-main` | process command line + launcher `$CT` |
| Branch | `hardening/m3-p1-production-fixes` | `git rev-parse --abbrev-ref HEAD` |
| Commit | `cca0120a842ee10e2af5f52ce14887ca55f3242b` (2026-08-03 10:26:25 +0200) | `git log -1` |
| Working tree | Clean except untracked `artifacts/` | `git status --porcelain` |
| Engine repo (`LUX_ROOT`) | `C:\Users\Administrator\Projects\Lux-OB-Backtester` | launcher line 78 |
| Engine branch / commit | `golden-run-001-engine` @ `d978074a2ee938fb4803e50d419788c584d6ef57` | `git log -1` |
| Engine working tree | Clean except untracked `release_assets/` | `git status --porcelain` |
| Engine manifest id | `6cb6cbcd…9854b` — **computed == expected** | executed `live.engine_identity.verify` against `LUX_ROOT` |
| Governed engine surface | Exactly **30 files** | `live/engine_manifest.json` `file_count: 30` |

The engine pin was verified by *executing* the production verifier, not by reading it:
`verify(LUX_ROOT, load_manifest(ENGINE_MANIFEST_PATH))` returned
`ok=True, "30 governed files match the approved manifest"` and the computed
`engine_manifest_id` equalled `live/config.py:ENGINE_MANIFEST_ID_EXPECTED`.

### 2.2 Deployed configuration (process environment)

From `live_state/ops/start-live-shadow.ps1` (git-ignored; the Task Scheduler launcher):

| Variable | Value |
|---|---|
| `LUX_ROOT` | `C:\Users\Administrator\Projects\Lux-OB-Backtester` |
| `LIVE_MODE` | `dry_run` (guard at launcher L102; a second guard at `live/main.py:211` refuses `live`) |
| `LIVE_STATE_DIR` | `…\live-trading-control-tower_emergent-main\live_state` |
| `MARKET_DATA_DIR` | `…\live_state\market_data` |
| `MT5_LOGIN` / `MT5_SERVER` | `<MT5_LOGIN>` / `<MT5_SERVER>` (identity guard at launcher L96-101) |
| `MT5_PASSWORD` | **deliberately unset** — attaches read-only to the logged-in terminal |
| `CT_BASE_URL` | `<CONTROL_TOWER_URL>` |

Defaults taken from `live/config.py` because the launcher does not set them:
`LIVE_FIXED_RISK_LOTS=0.01`, `LIVE_MAX_OPEN_POSITIONS=6`, `LIVE_DAILY_LOSS_LIMIT_R=5.0`,
`LIVE_MT5_MAGIC=77001`, `MT5_SERVER_BASE_UTC_OFFSET_HOURS=2`, `MT5_SERVER_DST_RULE=us`.

**The node is in shadow/dry-run.** `Executor._execute` short-circuits to `"simulated"` whenever
`config.mode != "live"` (`live/executor.py:163-175`). No broker orders are being sent.

### 2.3 Canonical module inventory (live path only)

**Control Tower wrapper** — `live-trading-control-tower_emergent-main`:

| Module | Role on the live path |
|---|---|
| `live/main.py` | Entrypoint; lifecycle; the one cycle loop (`cycle()`, L73-170) |
| `live/config.py` | `LiveConfig`; frozen constants; engine pins |
| `live/world.py` | `World.CURRENT`; `instance_id="live-eurusd-golden-001"` — keys intent identity |
| `live/mt5_gateway.py` | MT5 terminal IPC; server-clock → UTC conversion |
| `live/mt5_bridge.py` | Closed M1 bars → `market_data/EURUSD_1m_live.csv` |
| `live/runner.py` | `LuxSession` (engine load + pin verify), `LiveRunner`, `golden_pipeline` |
| `live/intents.py` | `diff_frontier` → `OrderIntent` list |
| `live/executor.py` | Reconcile; rails; apply intents |
| `live/safety.py` | `RailVerdict` hard rails |
| `live/state.py` | `RunnerState`: prev frame, ledger, mirror, realised-R counter |
| `live/lifecycle.py` | State machine + OS process lock |
| `live/publisher.py`, `telemetry.py`, `ops_log.py` | Control Tower telemetry + `cycles.jsonl` |
| `live/account_observation.py` | Read-only account telemetry (outside the intent path) |

Present in `live/` but **not on the cycle path** — standalone operator tools, none imported by
`live/main.py`: `rehearsal.py` (P0 rehearsal; also calls `diff_frontier`, so a `diff_frontier`
caller search returns it), `deploy_check.py`, `shadow_report.py`, `status.py`, `engine_identity.py`
(imported by `runner.py` at startup only, not per cycle).

**Pinned engine** — `Lux-OB-Backtester`, the 30 manifest files. Of those, the modules the live
path actually executes:

| Module | Role |
|---|---|
| `scripts/run_backtest.py` | Driver (impure layer): config load, date filter, news load, resample, OB news-tag, structure filters, `execute_scenario_job` |
| `strategy_core/order_blocks.py` | Swing legs + BOS/CHoCH + OB construction (`detect_order_blocks`) |
| `strategy_core/execution.py` | `prepare_candles_for_simulation`, `prepare_order_blocks_for_simulation`, **`simulate_trades`** (the walk), `summarize_trades` |
| `strategy_core/sessions.py` | UTC-hour session schedule; label + cohort key |
| `strategy_core/regime.py` | Daily market-state panel (EMA200 / BBW / ADX → 6 states) |
| `strategy_core/scenario.py` | `_build_cohort_index` — cohort rule compilation |
| `strategy_core/policy.py` | Portfolio Manager decision table |
| `strategy_core/news.py` | Blackout / flatten window matching |
| `strategy_core/ghost_tracker.py` | Annotation-only (see §2.5) |
| `src/execution.py` | `prepare_news_cache` (deliberate `src`-resident seam) |
| `src/resample.py` | `resample_candles` (M1 → 15min) |
| `src/run_outputs.py` | `engine_version()` |
| `src/portfolio_policy.py` | Filesystem shim for `load_policy` |

### 2.4 Legacy / duplicate paths explicitly EXCLUDED

These exist on this machine and must **not** be mirrored:

| Path | Why excluded |
|---|---|
| `Projects\live-trading-control-tower_emergent` (no `-main`) | Different remote, `main` @ `b6ffc09`, last commit 2026-07-14 "Auto-generated changes". Not the running repo. |
| `ct-capacity-worktree` (`incremental/m-incremental-1`) | Git worktree of the CT repo, different branch. Not running. |
| `ct-live-prep-worktree` (`freshness/server-observed-phase-aware`) | Worktree, 31 commits behind its upstream. Not running. |
| `lux-capacity-worktree`, `lux-stage1-worktree` | Lux worktrees on other branches. `LUX_ROOT` points at the main checkout, not these. |
| `Projects\FX-OB-Backtester` | Separate older clone of the Lux remote, `main` @ 2026-06-26. Not `LUX_ROOT`. |
| `Lux-OB-Backtester\*.pine` (22 files incl. `lux_style_swing_ob_backtester_v1.pine` and 20 `_BACKUP_PRE_*` variants) | Pre-existing Pine. Excluded by explicit instruction; also predates the Python baseline (`…_PRE_PYTHON_BASELINE.pine`). **Must not be used as a starting point.** |
| `strategy_core/swings.py::detect_swings` | **Dead on the live path.** See §2.5. |
| `src/retest_tracker.py` (766 lines) | Only reachable from `run_backtest.py:4407` inside `main()`'s report emission and `scripts/export_ob_retests.py`. **Not** called by `execute_scenario_job`, therefore not on the live path. |
| `src/fair_baseline.py`, `src/portfolio_replay.py`, `src/sweeps.py` | Report/validator paths in `main()`, not in `execute_scenario_job`. |
| `backend/strategy.py`, `backend/risk_engine.py`, `backend/execution.py` | Control Tower **console** backend (FastAPI fixture server). Does not participate in trading. |

### 2.5 Two duplicate-implementation traps

**Trap 1 — swing detection exists twice.** `strategy_core/swings.py::detect_swings`
(swings.py:12-50) and an **inlined copy of the same loop** inside
`strategy_core/order_blocks.py::detect_order_blocks` (order_blocks.py:144-171). The live pipeline
calls only `core.detect_order_blocks` (`live/runner.py:131`); `detect_swings` is never invoked on
the live path — it is only re-exported by `strategy_core/__init__.py:74` and `src/structure.py:6`.

The two differ in **what they retain**: `detect_swings` writes `swing_high`/`swing_low`/
`swing_level`/`swing_confirmed_at` columns and keeps every pivot; the inlined copy keeps only the
**most recent uncrossed** swing high and swing low in two dicts, plus a `crossed` flag. **Pine must
mirror the inlined version**, not `detect_swings`.

**Trap 2 — the ghost tracker is inert under the deployed config.** `GhostTracker` is constructed
and ticked every candle (`execution.py:2305,3046,3049`) and its 10 fields are merged into every
row (`execution.py:3070-3072`), but `add_candidate` is only called from the `retrace_cancel`
(L2987) and `first_failed_tag` (L3030) paths. The Golden config sets
`triggered_edge_cancel_on_retrace=false` and `triggered_edge_cancel_on_first_failed_tag=false`, so
**no ghost candidate is ever registered in production**. All 10 ghost fields are empty. Pine must
not display them.

---

## 3. Runtime execution order

### 3.1 Numbered execution trace — one live cycle

Startup (once, `live/main.py:209-282`) precedes this: `ProcessLock` → `VALIDATING`
(`config.validate()`, `LuxSession` load + `verify_engine()`) → `CONNECTING`
(`gateway.connect()`, `gateway.verify_time_base()`, `bridge.verify_time_base()`) → `RECONCILING`
(startup reconcile; **freeze ⇒ refuse to start**) → `READY` → `RUNNING`.

Then, every ~10 s (`live/main.py:299-302`):

| # | Step | Source | Notes |
|---|---|---|---|
| 1 | `ops.cycle_start()` | `main.py:75` | Outside the trading try/except |
| 2 | `gateway.ensure_connected()` | `main.py:84` | **Guard:** failure raises → cycle error, loop continues |
| 3 | `executor.reconcile()` | `main.py:94` | **Broker truth leads the cycle.** Moved here from inside `apply()` |
| 4 | `bridge.poll_once()` | `main.py:95` | Pull closed M1 bars from MT5, append to live segment CSV |
| 5 | `runner.run_once(defer_commit=True, on_work_start=…)` | `main.py:127` | See §3.2 |
| 6 | **Early return:** if `last_boundary == boundary` → `{"status":"no_new_bar"}` | `runner.py:206-207` | Nothing else in step 5 runs |
| 7 | `on_work_start(boundary)` → early telemetry publish | `runner.py:209-210`, `main.py:97-125` | Only when real work starts |
| 8 | `golden_pipeline(candles, frontier_date)` | `runner.py:118-156` | **The full recompute.** ~1119 s warm median |
| 9 | `diff_frontier(prev, trades_str, frontier_bar)` | `runner.py:220` | Empty on first run (`prev is None`) |
| 10 | Stage commit `(trades_str, boundary_str)` — **not written yet** | `runner.py:224-225` | LR-1 deferral |
| 11 | `executor.apply(intents, report=reconcile_report)` — only if `status=="ok"` **and** intents non-empty | `main.py:129-131` | See §3.3 |
| 12 | `runner.commit_cycle()` — **LR-1 commit point** | `main.py:135`, `runner.py:159-182` | Boundary + frame advance only after execution is durable |
| 13 | `_observe(observer)` → account telemetry | `main.py:141-147` | Outside the intent path; own cadence; never raises |
| 14 | `publisher.build_payload(...)` → `publisher.publish(...)` | `main.py:141-149` | |
| 15 | `ops.cycle_end(...)` → `cycles.jsonl` + `heartbeat.json` | `main.py:164-169` | Own try/except; a write failure must not kill the process |

**Crash semantics (LR-1).** A crash between steps 10 and 12 replays the whole cycle. The pipeline
is deterministic, so identical `intent_id`s are regenerated and the ledger's duplicate rail
suppresses whatever already executed (`runner.py:159-175`, `safety.py:52-55`).

### 3.2 `golden_pipeline` — the recompute, stage by stage

All of `live/runner.py:118-156`. **Every stage runs over the ENTIRE history, every cycle.**

| # | Call | Source | Output |
|---|---|---|---|
| 1 | `assemble_candles(frozen_csv, live_segment_csv)` | `runner.py:85-95` | Frozen M1 2015→2026-06-19 + live M1 after it |
| 2 | `s.golden_config(golden_config_path, end_date=frontier_date)` | `runner.py:77-82` | Golden JSON over `rb.ACTIVE_CONFIG`; **`end_date` is the only live delta** |
| 3 | `rb.filter_date_range(candles_raw, config)` | `run_backtest.py:623-637` | `[start_date, end_date + 1 day)` |
| 4 | `core.prepare_candles_for_simulation(candles)` | `execution.py:660-665` | `to_datetime`, **sort by time**, add `_session` |
| 5 | `rb.load_news_calendar_events(config)` | `run_backtest.py:1833-1883` | Filtered events + `window_start/end` |
| 6 | `rb.load_news_events(config)` | `run_backtest.py:1886-1889` | Same, `require_file=True` |
| 7 | `s.prepare_news_cache(news_events, candles, 5)` | `src/execution.py:21+` | Per-candle blackout/flatten lookup dicts |
| 8 | `rb.resample_candles(candles, "15min")` | `src/resample.py:4-19` | **M1 → 15m detection frame** |
| 9 | `core.detect_order_blocks(detection, swing_length=50, ob_filter="Atr", pip_size=1e-4, min=0, max=100)` | `order_blocks.py:127-222` | The OB universe + `ob_id` |
| 10 | `rb.tag_order_blocks_with_news(order_blocks, calendar_events)` | `run_backtest.py:2104` | Annotation |
| 11 | `rb.filter_order_blocks_by_structure(obs, "both")` | `run_backtest.py:474-479` | **No-op** under `"both"` |
| 12 | `rb.filter_order_blocks_by_structure_direction(obs, [bos_long,bos_short,choch_long,choch_short])` | `run_backtest.py:499-526` | **No-op** — all four allowed |
| 13 | `core.prepare_order_blocks_for_simulation(order_blocks)` | `execution.py:668-673` | Sort by `detection_time` |
| 14 | `rb.entry_scenarios(config)` → filter `mode=="triggered_edge"` → **must be exactly one** | `runner.py:141-143` | Hard `RuntimeError` otherwise |
| 15 | `rb.execute_scenario_job(job, …)` → `simulate_trades(...)` | `run_backtest.py:2573-2610` | See §3.3 |
| 16 | `result["trades"]` | `runner.py:156` | The trades DataFrame |

Inside step 15, `execute_scenario_job` first builds three per-run lookups
(`run_backtest.py:2534-2540`): `regime_emit`, `portfolio_emit` (mode `enforce` — `annotate_only`
is False for `kind=="entry"`), and `state_policy_emit`.

### 3.3 `simulate_trades` — the per-candle walk

`strategy_core/execution.py:2121-3098`. Single pass over **1-minute** candles.

Per candle (`execution.py:2327`):

| # | Sub-step | Source |
|---|---|---|
| 1 | **Admit OBs** whose `detection_time <= candle.time` → build `plan` → append to `pending` | L2329-2374 |
| 2 | Resolve `candle_blackout_event` (only if `pending`) and `candle_flatten_event` (only if `news_flatten_active_trades` and `active_trades`) | L2376-2396 |
| 3 | **Active-trade loop**, in order: metrics update → news-flatten → protection → BE/move-stop arm → BE stop → `_exit_outcome` | L2398-2477 |
| 4 | **Pending loop**, in order (each `continue` is a terminal decision for that candidate): | L2479-3044 |
| 4a | news blackout pause / touch-cancel | L2493-2508 |
| 4b | un-pause (`rearmed_at`) | L2510-2513 |
| 4c | **trigger arm** (`_triggered_edge_touched`) else mark tap | L2515-2525 |
| 4d | delay-window observation (observational only) | L2528-2529 |
| 4e | research cancels (`cancel_if_exits_ob_before_arm`, `require_revisit_confirmation`) — **both off in production** | L2535-2598 |
| 4f | reverse-touch-cancel — **off in production** | L2600-2631 |
| 4g | capacity check (`allow_multi_position` ⇒ always `True`) | L2634-2641 |
| 4h | **fill gate** (compound condition) | L2644 |
| 4h.1 | session filter — **disabled in production** | L2647-2654 |
| 4h.2 | regime filter (`_regime_filter_block_row`) — **inert**, no filter mode configured | L2662-2664 |
| 4h.3 | **portfolio-policy gate (`enforce`)** → `REGIME_BLOCKED` | L2671-2673 |
| 4h.4 | **cohort eligibility** → `COHORT_DISABLED` / `STATE_BLOCKED` | L2711-2757 |
| 4h.5 | state/cohort **target RR** rewrite of `plan["tp"]` | L2763-2779 |
| 4h.6 | BE / risk-reduction resolution | L2782-2826 |
| 4h.7 | risk-amount weight | L2832-2837 |
| 4h.8 | **news blackout blocks new fills** → `NEWS_BLACKOUT` | L2840-2850 |
| 4h.9 | `_apply_fill_metrics` → **FILLED** | L2852 |
| 4h.10 | fill-candle protection → fill-candle BE → `_fill_candle_exit_outcome` → else push to `active_trades` | L2874-2954 |
| 4i | **invalidation** (price beyond the far OB edge) → `INVALID` | L2957-2968 |
| 4j | retrace-cancel / first-failed-tag — **both off in production** | L2970-3041 |
| 4k | otherwise remain pending | L3043-3044 |
| 5 | `ghost_tracker.tick_all` | L3046 |

After the loop: leftovers → `OPEN` (L3051-3055) and `UNFILLED` (L3057-3066); then costs
(L3068-3072), stop-anchored excursions (L3076), market-state / portfolio enrichment
(L3078-3080), BE annotation (L3083-3092), frame build (L3094).

### 3.4 Timing semantics — the answers that matter for Pine

| Question | Production answer | Source |
|---|---|---|
| Intrabar or bar-close? | **Bar close only, at two granularities.** Structure/OB on closed 15m bars; fills/exits on closed 1m bars. Nothing is evaluated intrabar. | `runner.py:130`, `execution.py:2327` |
| Which bar closes a 15m boundary? | `[B, B+15)` is confirmed once M1 reaches `B+15`. `latest_closed_boundary = floor(last_m1 + 1min, 15min) − 15min` | `runner.py:98-103` |
| Current or previous bar? | The **current** closed bar in both loops. BOS/CHoCH compares `close[i-1]` vs `close[i]` — a two-bar test. | `order_blocks.py:173-177` |
| Timezone | UTC everywhere above the gateway (`TIME_BASE="utc-v2"`). The gateway converts MT5 server clock → UTC. | `config.py:19`, `mt5_bridge.py` |
| Session boundaries | UTC **hour-of-day only**, `[start, end)`. No weekday logic, no DST logic. | `sessions.py:23-38` |
| Date range | `[start_date, end_date + 1 day)` — end inclusive by day | `run_backtest.py:626-637` |
| News windows | `[event − 3min, event + 3min]` **inclusive both ends**; flatten from `window_start − 5min` | `run_backtest.py:1872-1875`, `news.py:31,47` |
| Market state | Daily panel, **shifted one day** (`shiftedDays: 1`); day *D*'s row uses data through *D−1* close; `knownAt = D T00:00:00Z` | `regime.py:264,291` |

### 3.5 Boundary example

Last stored M1 bar = `10:44` (covering `[10:44, 10:45)`).
`next_open = 10:45` → `floor(10:45, 15m) = 10:45` → `boundary = 10:30`.
So the 15m bar `[10:30, 10:45)` is the newest confirmed-closed detection bar.
`frontier_date = boundary.date()`; `frontier_bar = str(boundary)` tz-localized to UTC
(`runner.py:240-242`).

---

## 4. Dependency graph

```
live.main.main()
├─ ProcessLock ──────────────── live/lifecycle.py
├─ build()
│  ├─ LiveConfig ────────────── live/config.py   (env + frozen constants)
│  ├─ MT5Gateway ────────────── live/mt5_gateway.py
│  ├─ MT5BarBridge ─────────┬── live/mt5_bridge.py
│  │                        └── reads/writes market_data/EURUSD_1m_live.csv
│  ├─ LuxSession ───────────┬── sys.path += LUX_ROOT ; os.chdir(LUX_ROOT)
│  │                        ├── import scripts.run_backtest        (driver)
│  │                        ├── import strategy_core               (pure core)
│  │                        ├── import src.execution.prepare_news_cache
│  │                        └── verify_engine()  ── engine_identity.verify
│  │                                                 ├─ engine_version (3-file digest)
│  │                                                 ├─ manifest_id   (30-file digest)
│  │                                                 └─ verify_loaded_modules
│  ├─ LiveRunner ───────────┬── RunnerState (live/state.py)
│  │                        └── golden_pipeline  ── see below
│  ├─ Executor ─────────────┬── SafetyRails (live/safety.py)
│  │                        ├── RunnerState (ledger / mirror / realised R)
│  │                        └── MT5Gateway
│  ├─ CTPublisher ──────────── live/publisher.py → CT_BASE_URL
│  ├─ AccountObserver ──────── live/account_observation.py  (read-only, off-path)
│  └─ OpsLog ───────────────── live/ops_log.py → live_state/ops/cycles.jsonl
└─ cycle() loop
   ├─ gateway.ensure_connected
   ├─ executor.reconcile ────── broker truth (leads)
   ├─ bridge.poll_once ──────── closed M1 bars
   ├─ runner.run_once
   │  └─ golden_pipeline
   │     ├─ rb.filter_date_range
   │     ├─ core.prepare_candles_for_simulation ── strategy_core/execution.py
   │     ├─ rb.load_news_calendar_events / load_news_events
   │     ├─ src.execution.prepare_news_cache
   │     ├─ rb.resample_candles ───────────────── src/resample.py  (M1→15min)
   │     ├─ core.detect_order_blocks ──────────── strategy_core/order_blocks.py
   │     │     └─ inlined swing loop  +  BOS/CHoCH  +  _make_order_block
   │     ├─ rb.tag_order_blocks_with_news
   │     ├─ rb.filter_order_blocks_by_structure(_direction)   [both no-ops]
   │     ├─ core.prepare_order_blocks_for_simulation
   │     └─ rb.execute_scenario_job
   │        ├─ regime_emit_for_run ───────────── strategy_core/regime.py
   │        ├─ portfolio_emit_for_run ────────── strategy_core/policy.py
   │        ├─ state_policy_emit_for_run ─────── strategy_core/regime.py
   │        └─ simulate_trades ───────────────── strategy_core/execution.py
   │           ├─ _planned_trade
   │           ├─ _build_cohort_index ────────── strategy_core/scenario.py
   │           ├─ _triggered_edge_touched / _is_filled
   │           ├─ _portfolio_policy_block_row ── policy.decide + regime predicates
   │           ├─ _regime_state_for_candle ───── regime panel index
   │           ├─ _protection_trigger  [baseline ⇒ inert]
   │           ├─ _exit_outcome / _fill_candle_exit_outcome
   │           ├─ GhostTracker  [inert in production]
   │           └─ _apply_execution_costs
   ├─ intents.diff_frontier ──── prev frame vs cur frame
   ├─ executor.apply ─────────── rails → ledger → (dry_run ⇒ simulated)
   ├─ runner.commit_cycle ────── LR-1 commit
   └─ publisher.publish
```

**Direction of dependency, for Pine's benefit:** `strategy_core` is a pure library — no `src`
imports, no filesystem, no clock (`strategy_core/__init__.py:9-15`). Everything impure (paths,
config loading, news CSV, resampling) lives in `scripts/run_backtest.py` and `src/`. **The
Pine-reproducible surface is essentially `strategy_core` plus `src/resample.py`.**

**Purity verified empirically during this audit.** An audit-hook probe (`sys.addaudithook`) was
run against this venv: `import pandas` alone emits `socket.gethostname`; `import strategy_core`
adds **zero** additional `socket.*` / `subprocess.*` / `os.system` events and loads **zero** `src.*`
or `scripts.*` modules. The purity contract holds; see §16.8 for why the repo's own purity test
nevertheless reports a failure here.

---

## 5. State inventory

### 5.1 Notation

- **Pine: display** — must be shown by the oracle.
- **Pine: reproduce** — Pine must maintain this internally.
- **Pine: impossible** — cannot be derived from chart data alone.
- Names are **exact production identifiers**. Human descriptions are separate.

### 5.2 Swing / structure state — owner: `detect_order_blocks` locals

| Field | Type / values | Default | Created | Mutated | Reset | Persisted | Consumers | Pine |
|---|---|---|---|---|---|---|---|---|
| `current_leg` | `0` \| `BULLISH_LEG=1` \| `BEARISH_LEG=0` | `0` | `order_blocks.py:138` | L152-171 per 15m bar | never (per-run) | no | leg-change detection | reproduce |
| `swing_high` | `{"level": float\|None, "index": int\|None, "crossed": bool}` | `{None,None,False}` | L140 | replaced on `leg_change == -1` (L165-170); `crossed=True` on break (L180) | replaced by next swing high | no | BOS/CHoCH, OB pivot | reproduce + display |
| `swing_low` | same shape | `{None,None,False}` | L141 | replaced on `leg_change == 1` (L159-164); `crossed=True` on break (L203) | replaced by next swing low | no | BOS/CHoCH, OB pivot | reproduce + display |
| `swing_trend_bias` | `0` \| `BULLISH=1` \| `BEARISH=-1` | `0` | L139 | `BULLISH` on bull break (L181), `BEARISH` on bear break (L204) | never | no | **BOS vs CHoCH tag** | reproduce + display |
| `ob_id` | `int`, monotonic from 1 | `1` | L142 | `+= 1` **only when an OB is appended** (L197, L220) | never | no | `trade_id`, ghost key | reproduce + display |
| `next_leg`, `leg_change` | per-bar temporaries | — | L152-158 | — | per bar | no | — | reproduce |

**Critical:** there is **no separate "internal" and "external" structure** in the production
engine. There is exactly **one** swing scale, `swing_length=50`, one leg state machine, and one
`swing_trend_bias`. The conventional SMC internal/external distinction **does not exist here** and
must not be invented (§19.1).

### 5.3 Order-block record — owner: `_make_order_block`, `order_blocks.py:85-106`

| Field | Type | Source | Pine |
|---|---|---|---|
| `ob_id` | int | run-local counter | display (see §6.6 caveat) |
| `direction` | `"bullish"` \| `"bearish"` | break side | display |
| `structure_tag` | `"BOS"` \| `"CHoCH"` | `swing_trend_bias` at break | display |
| `origin_time`, `origin_index` | ts / int | extreme-`parsed_*` bar in `[pivot_index, detection_index-1]` | reproduce |
| `detection_time`, `detection_index` | ts / int | the breaking 15m bar | reproduce + display |
| `pivot_time`, `pivot_index` | ts / int | the swing bar | display |
| `top`, `bottom` | float | `origin.parsed_high` / `origin.parsed_low` | reproduce + display |
| `break_level` | float | the crossed swing level | display |
| `origin_open/high/low/close`, `parsed_high`, `parsed_low` | float | origin bar | debug |
| `swing_length`, `ob_filter` | 50, `"Atr"` | config echo | debug |
| `width_pips` | float | **mutated in place** by `_passes_size_filter` (L119) before append | debug |

Lifecycle fields (`mitigated`, `breached`, taps) are documented in `strategy_core/types.py:36` as
"mutate in place" — **but no such mutation exists in the pinned `detect_order_blocks` or
`simulate_trades`.** The type-alias comment is stale. See §19.2.

### 5.4 Candidate (pending) record — owner: `simulate_trades`, `execution.py:2345-2372`

| Field | Values | Default | Mutated at | Pine |
|---|---|---|---|---|
| `ob`, `plan` | shared **by reference** | — | `plan["tp"]`/`["rr_multiple"]` rewritten at fill (L2776-2777) | reproduce |
| `created_time`, `created_candle_index` | ts / int | admission candle | — | display |
| `same_candle_entry_allowed` | bool | `effective_delay == 0` ⇒ **`False`** (delay 3) | — | debug |
| `trigger_delay_candles` | int | **3** | — | display |
| `triggered_edge_armed` | bool | absent ⇒ False | `True` at L2517 | **display** |
| `trigger_time`, `trigger_candle_index` | ts / int | — | L2518-2519 | display |
| `armed_at`, `armed_on_trigger_candle`, `armed_same_candle` | ts / bool | — | L2520-2522 | debug |
| `tapped_before_trigger`, `tapped_time`, `tapped_candle_index` | bool / ts / int | absent | `_triggered_edge_mark_tap` L1023-1028 (once) | debug |
| `news_paused`, `rearmed_at` | bool / ts | absent | L2494-2495, L2510-2513 | display |
| `metrics.max_distance_away_before_fill_price` | float | `0.0` | `_update_pending_metrics` | debug |
| `_dw` | dict | `_delay_window_init` | `_delay_window_update` | debug |
| `cancel_on_first_failed_tag`, `cancel_on_retrace`, `cancel_if_exits_ob_before_arm`, `require_revisit_confirmation` | bool | **all `False` in production** | — | omit |
| `revisit_*`, `_cr_exit_seen` | — | inert | — | omit |
| `reverse_paused` | bool | inert (`reverse_touch_cancel_enabled=False`) | — | omit |

### 5.5 Plan record — owner: `_planned_trade`, `execution.py:714-728`

| Field | Formula (bullish / bearish) | Pine |
|---|---|---|
| `entry` | `top − depth·(0/100)` = **`top`** / `bottom + 0` = **`bottom`** (entry_level_pct = 0) | reproduce + display |
| `stop` | `bottom − stop_buffer` / `top + stop_buffer`; `stop_buffer = 1 pip = 0.0001` | reproduce + display |
| `risk` | `entry − stop` / `stop − entry`; **plan rejected if `risk <= 0`** (L706-707) | reproduce + display |
| `tp` | `entry + risk·rr` / `entry − risk·rr`; base `rr_multiple = 2` | reproduce + display |
| `rr_multiple` | 2, **overwritten at fill** by cohort/state RR (L2777) | display |
| `trigger_penetration_pct` | `25` (drives the arm test) | display |
| `entry_level_pct`, `ob_entry_depth_pct`, `effective_entry_depth_pct` | `0.0` | debug |
| `original_edge_entry`, `adjusted_entry` | both = `entry` here | debug |

### 5.6 Active-trade record — `execution.py:2943-2953`

`ob`, `plan`, `row`, `metrics`, `_be_arm_price`, `_be_stop_price`, `_be_stop_active_from`,
`_be_trigger_basis`. **All BE fields are `None` in production** (`be_enabled=false`, no global
risk reduction, no cohort `be`/`risk_reduction` in the Golden scenario). Pine: reproduce `ob`,
`plan`, `row`; omit BE.

### 5.7 Market-state (regime) row — `regime.py:279-292`

| Field | Values | Pine |
|---|---|---|
| `date` | `"YYYY-MM-DD"` UTC | display |
| `marketState` | one of `Bull/Expand`, `Bull/Compress`, `Bull/Chop`, `Bear/Expand`, `Bear/Compress`, `Bear/Chop`, or `None` | **display** |
| `trendState` | `"Bull"` \| `"Bear"` \| `None` | display |
| `volatilityState` | `"Expand"` \| `"Compress"` \| `None` | display |
| `chopState` | `"Chop"` \| `"Trend"` \| `None` | display |
| `ema`, `pxVsEma`, `bbw`, `adx` | float \| `None` | debug |
| `confirmed` | bool — `True` when `emaConfirmDays <= 0` (production default) | display |
| `knownAt` | `"{date}T00:00:00Z"` | debug |
| `shiftedDays` | `1` | debug |

Threshold: `bbwThresholdMode="fixed"` ⇒ `BBW_THRESHOLD_BY_SYMBOL["EURUSD"] = 2.342`
(`regime.py:43,139-144`).

### 5.8 Session state — `sessions.py:23-30`

| Key | Label | UTC window |
|---|---|---|
| `asia` | `Asia` | `[0, 7)` |
| `london` | `London` | `[7, 10)` |
| `lull` | `London Lull` | `[10, 12)` |
| `newYork` | `New York` | `[12, 15)` |
| `ny_pm` | `NY PM` | `[15, 17)` |
| `outside` | `Outside` | else |

`fill_session` (the label) comes from `_candle_session` → cached `_session` column
(`execution.py:664`); the cohort gate key comes from `_cohort_session_key` (`sessions.py:56-57`).
Both derive from the same table, so they cannot disagree.

### 5.9 Cohort rule — `_build_cohort_index`, `scenario.py:136-148`

Key: `(session_key, structure, direction)` where structure ∈ {`"BOS"`,`"CHoCH"`}, direction ∈
{`"Long"`,`"Short"`}. Value fields: `enabled`, `target_rr`, `be_arm_r`, `be_trigger`,
`rr_move_stop`, `rr_trigger_r`, `rr_stop_r`, `rr_unsupported_kind`, `risk_amount`,
`state_overrides`, `elig_states`.

A cohort is only indexed if it is **actionable**. Under the Golden config all 24 cohorts carry
`eligibility.states` and/or `state_overrides`, so all 24 are indexed. Pine: reproduce as a
constant table (§10.4).

### 5.10 Live-wrapper state — `live/state.py` (`RunnerState`)

| Field | Purpose | Pine |
|---|---|---|
| `last_boundary` | dedupe guard for `run_once` | impossible |
| previous frame | the trades DataFrame from the last committed cycle | impossible |
| ledger `intent_id → status` | duplicate suppression (`simulated`/`sent`/`confirmed`/`failed`/`frozen`) | impossible |
| mirror `trade_id → ticket` | broker position mirror | impossible |
| realised-R by day | daily-loss counter | impossible |
| `broker_closed_ticket` | server-side close detection | impossible |

---

## 6. Algorithm parity map

### 6.1 Detection-frame construction (M1 → 15m)

**Production behaviour.** `src/resample.py:4-19`. `set_index("time")`, `resample("15min")`,
`agg(open=first, high=max, low=min, close=last, volume=sum)`, then
`dropna(subset=["open","high","low","close"])`, then `reset_index()`.

Pandas `resample("15min")` is **left-labelled, left-closed** by default: bar `10:30` covers
`[10:30, 10:45)`. Empty intervals produce all-NaN rows which `dropna` removes — so **missing 15m
bars are silently dropped, not forward-filled**, and `candle_index` is therefore *not* a uniform
time grid.

Upstream, `prepare_candles_for_simulation` (`execution.py:660-665`) sorts M1 by time but **does
not de-duplicate**. `assemble_candles` (`runner.py:85-95`) prevents interleaving by taking frozen
rows `<= frozen_end` and live rows `> frozen_end`; `MT5BarBridge.append_bars`
(`mt5_bridge.py:132-162`) dedupes within a batch (first wins) and drops any bar `<= last stored`.

**Pine reproduction requirements.** Chart timeframe must be **15m** for structure/OB and **1m**
for the walk. Pine's own bar series is the exchange's; it cannot be rebuilt from the CSV.

**Mismatch risks.** TradingView's 15m aggregation of *its* feed will not equal a pandas resample
of Dukascopy-frozen + FTMO-live M1. Weekend/holiday gaps and TradingView's session filtering
produce different bar counts, which shifts every index-based value. See §12 L-01, L-02, L-09.

### 6.2 Swing detection (the inlined loop)

**Production behaviour.** `order_blocks.py:144-171`.

```
for index in range(50, len(candles)):
    pivot_index = index - 50
    right_highs = candles.loc[pivot_index+1 : index, "high"]     # 50 bars, INCLUSIVE both ends
    right_lows  = candles.loc[pivot_index+1 : index, "low"]
    new_leg_high = candles.at[pivot_index,"high"] >  right_highs.max()   # STRICT >
    new_leg_low  = candles.at[pivot_index,"low"]  <  right_lows.min()    # STRICT <
    next_leg = BEARISH_LEG if new_leg_high else (BULLISH_LEG if new_leg_low else current_leg)
    leg_change = next_leg - current_leg
    if leg_change == 1:  swing_low  = {level: low[pivot],  index: pivot, crossed: False}
    elif leg_change == -1: swing_high = {level: high[pivot], index: pivot, crossed: False}
    current_leg = next_leg
```

- **Right-side only.** There is no left-side comparison. This is *not* a symmetric fractal.
- **Confirmation lag is exactly 50 bars.** A pivot at `pivot_index` is confirmed at
  `index = pivot_index + 50`.
- **`elif` ordering:** if both `new_leg_high` and `new_leg_low` are true on the same bar, the
  **high wins** (`order_blocks.py:153-156`).
- **`leg_change` gate:** a swing is recorded **only on a transition**, never on a repeat. Since
  `BEARISH_LEG=0` and `BULLISH_LEG=1`, `leg_change ∈ {−1, 0, +1}`.
- **Warm-up:** the first 50 bars produce nothing; `current_leg` starts at `0` (== `BEARISH_LEG`),
  so the very first `new_leg_low` produces `leg_change == +1` and a swing low, while an initial
  `new_leg_high` produces `leg_change == 0` and **records nothing**. This asymmetric cold start is
  real and must be reproduced.
- **Equal highs/lows:** strict `>` / `<` mean a pivot equal to any bar in its right window is
  **not** a swing.
- **`.loc[a:b]` is inclusive of `b`** — the window is 50 bars: `pivot+1 … pivot+50`.

**Pine reproduction requirements.** A rolling 50-bar right-window max/min compared against the
value 50 bars back — i.e. `high[50] > ta.highest(high, 50)` evaluated on the current bar, with a
manually maintained `current_leg` var. Arrays are not required for the leg machine itself.

**Mismatch risks.** Any difference in bar count (§6.1) shifts `pivot_index` and can change which
bar is the pivot. Pine's `ta.highest(50)` on the current bar spans `[bar-49, bar]`, so the offset
must be checked against `[pivot+1, pivot+50]` — off-by-one here silently changes every swing.

### 6.3 Parsed prices, ATR and the volatility flip

**Production behaviour.** `_add_parsed_prices`, `order_blocks.py:44-56`.

```
true_range = max(H−L, |H−prev_close|, |L−prev_close|)          # prev_close = close.shift(1) → NaN on bar 0
atr = _pine_rma(true_range, 200)
cumulative_mean_range = true_range.cumsum() / [max(i,1) for i in range(n)]
volatility_measure = atr                                      # ob_filter == "Atr"
high_volatility = (H − L) >= 2.0 * volatility_measure         # >= , not >
parsed_high = H where not high_volatility else L              # FLIPPED
parsed_low  = L where not high_volatility else H              # FLIPPED
```

`_pine_rma` (`order_blocks.py:32-41`) seeds with **the first value** (`previous = value`), then
`prev = (prev·199 + v)/200`.

Two things are load-bearing and easy to get wrong:

1. **Seeding.** Pine's `ta.rma` seeds with an SMA of the first `length` values. This
   implementation seeds with `x[0]`. Over 200 periods the difference decays but is **never
   exactly zero**, and it is evaluated against a `>=` threshold — so near-threshold bars can flip.
2. **Bar 0 TR is NaN.** `close.shift(1)` is NaN at index 0, so `high_close`/`low_close` are NaN
   and `pd.concat(...).max(axis=1)` returns `H−L` (pandas `max` skips NaN). The RMA then seeds
   from that. **[INFERRED from pandas semantics — not separately executed.]**
3. **The flip is real.** On a high-volatility bar the OB box is built from an *inverted*
   high/low, i.e. `top < bottom` for that origin. `_ob_depth = top − bottom` then goes **negative**,
   which cascades: `_triggered_edge_touched` returns `False` when `depth <= 0` (L1003-1004), and
   `_planned_trade` yields `risk <= 0` → the plan is **rejected** (L706-707). So a high-volatility
   origin bar generally produces no tradeable candidate. This is the pinned behaviour and must be
   reproduced, not "fixed".

**Pine reproduction requirements.** A hand-rolled RMA seeded with the first TR value (not
`ta.rma`), and a cumulative-mean fallback if `ob_filter` is ever changed. `ta.atr` must **not** be
used.

**Mismatch risks.** Highest-severity numeric divergence in the whole system (§12 L-03). The RMA
is seeded at the very first bar of the loaded series, so **the ATR value at any bar depends on
where the chart's history begins.** Pine and Python will only agree if both start from the same
first bar.

### 6.4 BOS / CHoCH detection

**Production behaviour.** `order_blocks.py:173-220`, evaluated on every 15m bar *after* the swing
update.

```
previous_close = close[index-1]  if index > 0 else None
current_close  = close[index]

if swing_high.level is not None and not swing_high.crossed:
    bull_break = previous_close <= swing_high.level and current_close > swing_high.level
    if bull_break:
        structure_tag = "CHoCH" if swing_trend_bias == BEARISH else "BOS"
        swing_high.crossed = True
        swing_trend_bias = BULLISH
        → build bullish OB

if swing_low.level is not None and not swing_low.crossed:
    bear_break = previous_close >= swing_low.level and current_close < swing_low.level
    if bear_break:
        structure_tag = "CHoCH" if swing_trend_bias == BULLISH else "BOS"
        swing_low.crossed = True
        swing_trend_bias = BEARISH
        → build bearish OB
```

- **Close-based, two-bar confirmation.** Not a wick break. Requires `prev_close` on the
  non-breaking side and `cur_close` strictly through.
- **Comparison operators are asymmetric on purpose:** `prev <= level` and `cur > level` for bulls;
  `prev >= level` and `cur < level` for bears. A `prev_close` exactly *at* the level still permits
  a break.
- **`crossed` latches.** A swing can be broken exactly once.
- **Tagging:** `BOS` when `swing_trend_bias` is `0` (initial) or already the same side; `CHoCH`
  only when flipping from the opposite bias. **The very first break of a run is always `BOS`**,
  because `swing_trend_bias` starts at `0` which equals neither `BULLISH` nor `BEARISH`.
- **Both blocks can fire on the same bar** — they are independent `if`s, not `elif`. A bar that
  closes above the swing high *and* below the swing low would emit a bullish OB then a bearish OB,
  with the bearish tag computed from the bias the bullish break just set. `ob_id` increments
  between them. (Geometrically near-impossible on a single bar, but the code permits it and the
  ordering is deterministic: **bull first**.)

**Pine reproduction requirements.** Two persistent `var` records with a `crossed` flag, a
persistent `swing_trend_bias`, and strict adherence to the bull-then-bear evaluation order.

**Mismatch risks.** `swing_trend_bias` is **never reset** and is seeded by whatever the first
break in the loaded history was. A Pine chart with less history will start from a different bias
and can tag BOS where Python tags CHoCH — permanently, for the rest of the series. This is
Limitation **L-04** and is the single biggest structural-parity risk after §6.3.

### 6.5 Order-block construction

**Production behaviour.** `_make_order_block`, `order_blocks.py:59-106`.

- Bullish: search `parsed_low` over `[pivot_index, detection_index − 1]`, take
  `search[search == search.min()].index[0]` — the **first** (earliest) minimum. Bearish: **first**
  maximum of `parsed_high`. This is the tie-break rule for equal extremes: **earliest wins.**
- `top = origin.parsed_high`, `bottom = origin.parsed_low` — the **parsed** values, which are
  flipped on high-volatility bars (§6.3).
- The search window **excludes** the detection bar (`detection_index − 1`).
- If the slice is empty → `None` → no OB, **and `ob_id` is not incremented** (L193-197).

**Size filter.** `_passes_size_filter`, `order_blocks.py:115-124`:
`width_pips = |top − bottom| / pip_size`; reject if `< min_ob_size_pips` (0) or `> max_ob_size_pips`
(100). With `min = 0` the lower gate is `width < 0`, which is never true — so **only the 100-pip
upper gate is active**. Note `width_pips` is written **into the dict** (L119) before the
accept/reject decision, and the same dict object is what gets appended.

**`ob_id` semantics.** Incremented **only on successful append** (L197, L220). A rejected or
`None` OB leaves the counter untouched. So `ob_id` is a dense 1..N sequence over *accepted* OBs, in
detection order, across both directions.

**Pine reproduction requirements.** Arrays of OB records; a per-OB search over the pivot→detection
window (bounded — the window can be long); a monotonic counter incremented only on append.

**Mismatch risks.** `ob_id` is history-start-dependent (§6.6). The pivot→detection window can span
hundreds of bars, so the search cost is real (§12 L-08).

### 6.6 Order-block identity

`ob_id` is a **run-local monotonic counter** over the whole recomputed history, and `trade_id =
("L" if bullish else "S") + "_" + str(int(ob_id))` (`execution.py:764-765`).

The Control Tower explicitly treats this as a public identifier — the excluded worktree branch
`incremental/m-incremental-1` carries the commit *"M-LIVE-STATE-6 Phase 1/7: ob_id is a PUBLIC
identifier, not a run-local label"*, which is **not** on the running branch. On the running branch
`ob_id` remains run-local.

**Pine can reproduce `ob_id` exactly only if it processes the identical bar series from the
identical first bar.** In practice that means: same symbol, same feed, same 15m aggregation, and
chart history reaching back to 2015-01-01. This is not achievable on a normal TradingView chart
(§12 L-05). Pine must therefore display a **locally-derived id** and label it as such — never
claim it is the Python `ob_id` unless a full-history parity fixture proves it.

### 6.7 Candidate admission and the triggered-edge entry model

**Admission.** `execution.py:2329-2374`. OBs are admitted when
`ob_detection_times[cursor] <= candle["time"]` on the **1-minute** walk. `ob_cursor` only advances
forward, so admission is O(n) total. `_direction_allowed(direction, "both")` is always `True`
(`execution.py:1743-1746`).

**Entry price.** `triggered_edge_entry_level_pct = 0` ⇒ entry is exactly the OB edge:
`top` for bullish, `bottom` for bearish (`_planned_trade`, L689-704).

**Arming (`_triggered_edge_touched`, L998-1005).**

```
threshold = 25.0
depth = top − bottom
if depth <= 0: return False
penetration = max(0, top − candle.low)      bullish
              max(0, candle.high − bottom)  bearish
return penetration >= depth * 0.25          # >= , wick-based
```

Arming is a **wick** test on a 1m candle, at 25 % penetration into the block.

**Fill (`execution.py:2644`).** Once armed, the fill test is:

```
armed AND candle_index >= trigger_candle_index + 3
      AND candle.low <= entry <= candle.high        # in-range, BOTH sides
```

Note the fill test for an armed triggered-edge order is **`low <= entry <= high`**, not the
one-sided `low <= entry` used by non-triggered models (the ternary at L2644 selects between them).

**The 3-candle delay.** `trigger_delay_candles = 3`, so the earliest fillable 1m candle is
`trigger_candle_index + 3`. `same_candle_entry_allowed` is `False`.
`triggered_edge_same_candle_modes: ["same_candle","next_candle"]` in the config is **not consumed**
by this path — the live job uses `triggered_edge_candle_delays: [3]` via
`scenario["triggered_edge_delay_candles"]` (`run_backtest.py:2585`).

**Pre-trigger tap.** If not armed and `_is_filled` is true (`low <= entry` / `high >= entry`,
L1661-1664), the candidate is marked `tapped_before_trigger` once (L2524-2525). Under the
production config this only feeds diagnostics, since both tap-driven cancels are disabled.

**Pine reproduction requirements.** Per-candidate state on a 1m chart: `armed`,
`trigger_candle_index`, and a bar-index comparison. Because `candle_index` is a **position in the
loaded array**, not a timestamp, Pine's `bar_index` is equivalent **only if no bars are missing**
(§12 L-09).

**Mismatch risks.** Wick-precision differences between feeds change arming; the `>=` at exactly
25 % is a knife-edge.

### 6.8 Invalidation

**Production behaviour.** `execution.py:2957-2968`, evaluated **only after** the fill gate did not
fire on this candle:

```
bullish: candle.low  < ob.bottom     # STRICT
bearish: candle.high > ob.top        # STRICT
→ outcome "INVALID", missed_reason "invalidated_before_fill"
```

Wick-based, strict inequality, evaluated on the 1m walk. **Order matters:** a candle that both
fills and invalidates is a **FILL**, because the fill branch `continue`s at L2955 before the
invalidation check is reached.

**Pine reproduction.** Straightforward, but the fill-before-invalidation precedence must be
preserved exactly.

### 6.9 Exit resolution

**Subsequent candles** — `_exit_outcome`, `execution.py:1667-1680`:

```
bullish: hit_stop = low  <= stop ; hit_tp = high >= tp
bearish: hit_stop = high >= stop ; hit_tp = low  <= tp
if hit_stop: return "LOSS", -1.0        # STOP WINS same-candle ambiguity
if hit_tp:   return "WIN",  rr_multiple
```

**The fill candle** — `_fill_candle_exit_outcome`, `execution.py:1702-1740`, using
`_fill_candle_bookable_extreme` (L1683-1699):

```
bullish: ext = high if open <= entry else close
bearish: ext = low  if open >= entry else close
hit_stop = (low <= stop) / (high >= stop)      # unchanged — the stop is on the fill leg
hit_tp   = ext >= tp     / ext <= tp
stop still wins.
```

The rationale is explicit in the docstring: a limit fills on the move *toward* entry, so the stop
is provably post-fill, but the favourable extreme may be pre-fill. A same-candle TP is only booked
on a **gap-fill** (open at/through the limit) or a **confirming close**.

**Pine reproduction.** Both branches, with the fill-candle special case. `pnl_r` is a fixed
`-1.0` / `rr_multiple` — **not** computed from actual exit price.

**Mismatch risks.** Same-candle stop/target ambiguity resolves to LOSS in Python; TradingView's
`strategy` engine has its own convention — but this is an *indicator*, so the rule must be coded
explicitly (§12 L-11).

### 6.10 Market state (regime)

**Production behaviour.** `regime.py:181-293`, built once per run from **daily** bars aggregated
from the same M1 series (`run_backtest.py:2430-2437` — `resample("1D")`, `dropna`).

- `ema = ewm(adjust=False, alpha = 2/201)` seeded with `close[0]`; `pxVsEma = (close−ema)/ema·100`
- `bbw = (2·2·sd_20)/ma_20·100`, `sd` with **`ddof=1`**
- Wilder ADX(14) via `ewm_adjust_false(alpha = 1/14)` on TR, +DM, −DM, then DX, then ADX
- `classify_state` (L154-164): `trend = "Bull" if pxVsEma > 0 else "Bear"`;
  `volatility = "Expand" if bbw > 2.342 else "Compress"`; **`adx < 18` overrides volatility → `/Chop`**
- **One-day shift:** row for day *i* uses features from day *i−1* (L264). Day 0 is all-`None`.
- `confirmed = True` when `emaConfirmDays <= 0` — the production default.

**Pine reproduction requirements.** `request.security` on a daily timeframe with explicit
`lookahead_off` and a manual one-day shift, plus hand-rolled EWM (seeded at first value, not
`ta.ema`'s SMA seed) and Wilder ADX. This is a **second history-start-dependent indicator family**
(same class of risk as §6.3).

**Mismatch risks.** Daily aggregation boundaries: Python uses **UTC** calendar days
(`resample("1D")` over UTC-naive timestamps). TradingView's daily bars follow the *exchange*
session, which for FX is typically 17:00 New York. These are **different days**. This is
Limitation **L-06** and it directly changes cohort eligibility, because eligibility is keyed on
`marketState`.

### 6.11 Session assignment

`_session_for_hour(pd.Timestamp(t).hour)` — pure UTC hour-of-day, `[start, end)`
(`sessions.py:33-38`). No weekday, no DST, no holiday logic anywhere in the pinned engine
(explicitly stated at `sessions.py:8-9`). Pine reproduces this with `hour(time, "UTC")` — it must
**not** use `session.ismarket` or any exchange-session helper.

### 6.12 Cohort resolution and eligibility

**Production behaviour.** `execution.py:2711-2757`.

```
_c_struct = "CHoCH" if "choch" in str(ob.structure_tag).lower() else "BOS"
_c_dir    = "Long" if ob.direction == "bullish" else "Short"
_c_rule   = _cohort_index[(_cohort_session_key(candle), _c_struct, _c_dir)]
```

The session key comes from the **fill candle** (the 1m candle), not the OB's detection candle.

If a rule exists and carries any state rule, the daily market state is resolved for that candle
(`_regime_state_for_candle`), then:

```
_eff_allowed = _c_rule["enabled"]                       # base
if state and confirmed and elig_states:
    if elig_states[state] == "allow": _eff_allowed = True    # RESCUE
    if elig_states[state] == "block": _eff_allowed = False   # BLOCK
```

- **Unknown / warm-up / unconfirmed state ⇒ base eligibility is used.** Labels are never invented.
- A state-level `allow` inside a disabled cohort sets `row["state_eligibility"] = "rescued"`.
- Block outcome depends on *why*: `STATE_BLOCKED` when the base was enabled and a state blocked it;
  `COHORT_DISABLED` otherwise (L2746-2755).

**Target override** (L2763-2779): a confirmed state with a `custom` override replaces the cohort
base RR; the new `tp` is recomputed from the **unchanged** entry and risk. Only the take-profit
moves — entry, stop and risk are never touched.

### 6.13 Portfolio Manager gate

**Production behaviour.** `_portfolio_policy_block_row`, `execution.py:527-572`, mode
**`enforce`** on the live path.

1. `decide(table, "EURUSD", cohort_session_key(candle), row.structure_tag, row.direction)` →
   `Decision` (`policy.py:333-347`). A **missing** cohort fails **safe**: `ENABLED/LABEL/unknown_cohort`.
2. Stamp decision + market-state provenance on the row (even when allowed).
3. `LABEL` ⇒ never blocks.
4. Otherwise `_portfolio_regime_gate` (`execution.py:485-502`):
   - `DISABLE` → blocked, `portfolio_disabled`
   - `STATE_ONLY` → blocked if state ∉ non-Chop states, `state_not_allowed`
   - `DIRECTION_AWARE` → state gate over *all* states (never blocks), then direction gate
     (`bull_allows=long`, `bear_allows=short`, `chop_allows=both`) → `direction_mismatch`
5. Blocked ⇒ `outcome = "REGIME_BLOCKED"`, `cancel_reason = "regime_blocked"`,
   `regime_block_reason` = one of the three above.

**Critical interaction — `portfolio_include_disabled_cohorts = true`.** Before the table is used,
`with_disabled_as_label(table, "EURUSD")` rewrites **every `DISABLE` cohort to `LABEL` in memory**
(`run_backtest.py:2458-2459`, `policy.py:274-307`). The deployed JSON is untouched.

The deployed `deployed_policy.v1.json` (`policy_version: 2026-07-09.te-v1.2-surgical-disable`, 48
cohorts, 24 for EURUSD) contains **12 `DISABLE`** EURUSD cohorts — *all of which are neutralised
to `LABEL` at runtime*. The effective EURUSD table on the live path is therefore:

| Session | BOS Long | BOS Short | CHoCH Long | CHoCH Short |
|---|---|---|---|---|
| `london` | LABEL | ~~DISABLE~~→LABEL | ~~DISABLE~~→LABEL | LABEL |
| `lull` | ~~DISABLE~~→LABEL | LABEL | ~~DISABLE~~→LABEL | ~~DISABLE~~→LABEL |
| `newYork` | ~~DISABLE~~→LABEL | LABEL | ~~DISABLE~~→LABEL | **DIRECTION_AWARE** |
| `ny_pm` | ~~DISABLE~~→LABEL | ~~DISABLE~~→LABEL | LABEL | **DIRECTION_AWARE** |
| `asia` | ~~DISABLE~~→LABEL | LABEL | LABEL | ~~DISABLE~~→LABEL |
| `outside` | **DIRECTION_AWARE** | ~~DISABLE~~→LABEL | **STATE_ONLY** | **DIRECTION_AWARE** |

So in production the PM gate can block via only **5 cohorts**: four `DIRECTION_AWARE` and one
`STATE_ONLY`. `portfolio_disabled` is **unreachable**. Pine must encode the *effective* table, and
the HUD should show both the deployed regime and the effective one so the override is visible.

### 6.14 News filtering

**Windows.** `run_backtest.py:1872-1875`: `window_start = time − 3min`, `window_end = time + 3min`.
Filtered to `impact ∈ {"high"}`; `news_blackout_currencies` is empty so
`relevant_news_currencies(config)` decides the currency set. **[INFERRED — `relevant_news_currencies`
was not read; for EURUSD it is expected to yield `{EUR, USD}`. Recorded as evidence gap §19.3.]**

**Matching.** `news.py:31` — `window_start <= t <= window_end`, **inclusive both ends**.
`news.py:47` — flatten window `[window_start − 5min, window_end]`, earliest `(flatten_target,
window_start)` wins.

**Effects, in walk order:**

| Effect | Config | Behaviour | Source |
|---|---|---|---|
| Pause pending | `news_pause_pending_orders=true` | Mark `news_paused`; candidate survives the candle | L2493-2508 |
| Cancel on touch during blackout | `news_cancel_if_touched_during_blackout=true` | Touch during blackout ⇒ terminal row, `cancel_reason="news_touch_cancel"` | L2497-2506 |
| Block new fills | `news_block_new_fills=true` | At the fill point ⇒ `NEWS_BLACKOUT`, `news_action="BLOCKED_FILL"` | L2840-2850 |
| Flatten active | `news_flatten_active_trades=true`, 5 min before | Active trade closed ⇒ `NEWS_FLATTEN` | L2402-2409 |

**Pine reproduction: impossible.** There is no economic calendar in Pine. See §12 L-07.

### 6.15 Protection, break-even, risk reduction — all inert

| Feature | Config | Production effect |
|---|---|---|
| Protection | `protection_modes: ["baseline"]` | `_protection_trigger` returns `None` immediately (`execution.py:1577-1578`). **Dead.** |
| Break-even | `be_enabled: false`; no cohort `be` | `be_arm_level_r is None` ⇒ `_be_enabled = False`; `_be_arm_price = None`. **Dead.** |
| Risk reduction | no `risk_reduction` in scenario or global | `_rr_global_move_stop = False`; no cohort `rr_move_stop`. **Dead.** |
| Reverse touch cancel | `reverse_touch_cancel_enabled: false` | **Dead.** |
| Session filter | `session_filter_enabled: false` | **Dead.** |
| FFT / retrace cancel | both `false` | **Dead**, and with them the ghost tracker. |

**Consequence: the production stop never moves.** `diff_frontier`'s `MODIFY_STOP` branch
(`intents.py:117-124`) is therefore **unreachable under the current configuration**. Pine must not
draw a break-even line, and the HUD's "protection / BE" fields must read *disabled*, not *pending*.

### 6.16 Costs and R accounting

`_apply_execution_costs` (`execution.py:1476-1506`) runs over **every** row post-loop, using
`spread_pips=0.2`, `slippage_pips=0.2`, `commission_r_per_trade=0`. `pnl_r` is the gross
`-1.0 / rr_multiple`; `net_r` is cost-inclusive and is what `diff_frontier` carries on
`CLOSE_POSITION` (`intents.py:114`) and what feeds the daily-loss rail. Pine can reproduce this
arithmetic but the spread is a *config constant*, not the live spread (§12 L-10).

---

## 7. Entry blocking matrix

Every condition that can stop a candidate progressing, in **production evaluation order**. "Live?"
records whether the branch is reachable under the deployed configuration.

| # | Stage | Production condition | Reason / outcome code | Source | State inspected | Trigger semantics | Live? | Pine | Display |
|---|---|---|---|---|---|---|---|---|---|
| B01 | Detection | `risk <= 0` (from a flipped high-volatility origin) | *(no code — `plan is None`)* | `execution.py:706-707` | `plan` | 15m, silent | yes | reproduce | **yes — silent no-trade** |
| B02 | Detection | `_make_order_block` search slice empty | *(no code — OB is `None`)* | `order_blocks.py:73-79` | pivot window | 15m, silent | rare | reproduce | debug |
| B03 | Detection | `width_pips > 100` | *(no code — OB dropped)* | `order_blocks.py:122-123` | `top`,`bottom` | 15m, silent | yes | reproduce | debug |
| B04 | Detection | `width_pips < 0` | *(no code)* | `order_blocks.py:120-121` | — | never true (`min=0`) | **no** | omit | — |
| B05 | Admission | `_direction_allowed` false | *(no code)* | `execution.py:1743-1746` | `trade_direction` | `"both"` ⇒ always allowed | **no** | omit | — |
| B06 | Pending | Blackout active + `news_pause_pending_orders` | `news_paused` (not terminal) | `execution.py:2493-2508` | blackout event | 1m, inclusive window | yes | **impossible** | yes |
| B07 | Pending | Entry touched during blackout | `news_touch_cancel` / row appended | `execution.py:2497-2506` | blackout + touch | 1m | yes | **impossible** | yes |
| B08 | Pending | `cancel_if_exits_ob_before_arm` | `EXITED_OB_BEFORE_ARM` | `execution.py:2535-2556` | `_dw.exited` | flag off | **no** | omit | — |
| B09 | Pending | `require_revisit_confirmation` unmet | *(fill gate AND)* | `execution.py:2644` | `revisit_confirmed` | flag off | **no** | omit | — |
| B10 | Pending | Reverse conflict | `REVERSE_TOUCH_CANCEL` | `execution.py:2600-2631` | `active_trades` | flag off | **no** | omit | — |
| B11 | Fill gate | Not armed | *(no row emitted)* | `execution.py:2644` | `triggered_edge_armed` | 1m wick, 25 % | yes | reproduce | **yes** |
| B12 | Fill gate | `candle_index < trigger_candle_index + 3` | *(no row)* | `execution.py:2644` | indices | 1m count | yes | reproduce | **yes** |
| B13 | Fill gate | `entry` not in `[low, high]` | *(no row)* | `execution.py:2644` | candle range | 1m | yes | reproduce | yes |
| B14 | Fill gate | Capacity (`one_per_direction`/`single_position`) | *(no row)* | `execution.py:2634-2641` | `active_trades` | `allow_multi_position` ⇒ always fillable | **no** | omit | — |
| B15 | Post-gate | `fill_session ∉ allowed_sessions` | `session_filter_cancel` | `execution.py:2647-2654` | `_session` | disabled | **no** | omit | — |
| B16 | Post-gate | Regime **filter** mode blocks | `REGIME_FILTER_BLOCKED` *(see §19.3)* | `execution.py:2662-2664` | `regime_emit` | no filter mode configured | **no** | omit | — |
| B17 | Post-gate | PM `DISABLE` | `REGIME_BLOCKED` / `portfolio_disabled` | `execution.py:490-491` | policy table | **neutralised by include-disabled override** | **no** | omit | debug only |
| B18 | Post-gate | PM `STATE_ONLY` and state is `*/Chop` | `REGIME_BLOCKED` / `state_not_allowed` | `execution.py:492-495` | daily state | 1 cohort (`outside`/CHoCH/Long) | **yes** | reproduce | **yes** |
| B19 | Post-gate | PM `DIRECTION_AWARE` direction mismatch | `REGIME_BLOCKED` / `direction_mismatch` | `execution.py:496-501` | daily state + side | 4 cohorts | **yes** | reproduce | **yes** |
| B20 | Post-gate | Cohort base disabled (no rescue) | `COHORT_DISABLED` / `cohort_disabled` | `execution.py:2752-2755` | `_c_rule.enabled` | all 24 cohorts have `base:"allow"` | **no** | reproduce | debug |
| B21 | Post-gate | Confirmed state has `elig_states[state] == "block"` | `STATE_BLOCKED` / `state_target_block` | `execution.py:2746-2750` | daily state | **62 state-blocks configured** | **yes — the dominant filter** | reproduce | **yes** |
| B22 | Post-gate | Blackout active + `news_block_new_fills` | `NEWS_BLACKOUT` / `news_blackout_cancel` | `execution.py:2840-2850` | blackout event | 1m inclusive | yes | **impossible** | yes |
| B23 | Pending | Invalidation beyond far edge | `INVALID` / `invalidated_before_fill` | `execution.py:2957-2968` | candle vs OB | 1m wick, strict | yes | reproduce | **yes** |
| B24 | Pending | Retrace cancel | `USED_OB_RETRACE_CANCEL` | `execution.py:2970-2997` | `tapped_before_trigger` | flag off | **no** | omit | — |
| B25 | Pending | First-failed-tag cancel | `FIRST_FAILED_TAG_CANCEL` | `execution.py:2999-3041` | `tapped_before_trigger` | flag off | **no** | omit | — |
| B26 | End of run | Still pending | `UNFILLED` + `never_triggered` \| `never_filled_after_trigger` | `execution.py:3057-3066` | `triggered_edge_armed` | terminal | yes | reproduce | yes |
| B27 | Active | News flatten | `NEWS_FLATTEN` | `execution.py:2402-2409` | flatten event | 1m | yes | **impossible** | yes |
| B28 | Live | Reconcile freeze | *(all intents `frozen`)* | `executor.py:123-128` | broker snapshot | per cycle | yes | **impossible** | yes |
| B29 | Live rail | KILL file present | `kill_switch` | `safety.py:47` | filesystem | per intent | yes | **impossible** | yes |
| B30 | Live rail | Symbol not `EURUSD` | `symbol_whitelist` | `safety.py:50` | intent | per intent | yes | **impossible** | debug |
| B31 | Live rail | Duplicate `intent_id` in ledger | `duplicate_intent` | `safety.py:52-55` | ledger | per intent | yes | **impossible** | yes |
| B32 | Live rail | Realised R `<= −5.0` today | `daily_loss_limit` | `safety.py:57-61` | daily counter | per intent | yes | **impossible** | yes |
| B33 | Live rail | Open mirrors `>= 6` | `max_open_positions` | `safety.py:62-64` | mirror | per intent | yes | **impossible** | yes |
| B34 | Live rail | Unknown broker position | `unknown_position` | `safety.py:71-78` | broker | per intent | yes | **impossible** | yes |
| B35 | Live | Fill **and** exit inside one 15m window | `SKIP_INTRA_WINDOW` | `intents.py:70,95-100` | frame diff | per boundary | yes | **impossible** | **yes** |
| B36 | Live | `LIVE_MODE != "live"` | `"simulated"` | `executor.py:164-175` | config | **always true today** | yes | **impossible** | **yes** |

### 7.1 The five distinctions the matrix must preserve

| Distinction | Which rows | How to tell them apart |
|---|---|---|
| **Candidate never created** | B01–B05 | No trade row exists at all. Invisible in the trades CSV — Pine must derive it from the OB universe. |
| **Candidate created but never eligible** | B11–B13, B26 | Row exists with `outcome ∈ {UNFILLED}` and a `missed_reason`. |
| **Candidate blocked at the fill point** | B15–B22 | Row exists, `fill_time == ""`, specific `outcome` + `cancel_reason`. |
| **Candidate invalidated** | B23 | `outcome = "INVALID"`. |
| **Order suppressed / prevented downstream** | B28–B36 | The engine *did* produce a fill; the live layer refused to mirror it. **No engine row reflects this** — it exists only in `cycles.jsonl` and the ledger. |

That last row is the one most likely to mislead an operator: **the chart can legitimately show a
fill that never reached the broker.** The HUD must carry a permanent "Python-parity /
execution-state unknown" warning (§13.1).

---

## 8. Order-block lifecycle

### 8.1 What the state machine actually is

There is **no persisted order-block state machine.** OBs are constructed in a single 15m pass and
are thereafter **immutable**; all "lifecycle" is the state of the *candidate* wrapping the OB
inside one `simulate_trades` run, and it is discarded when the run ends.

The `strategy_core/types.py:36` comment claiming "lifecycle fields mutate in place: mitigation,
breach, taps" describes a design that is **not present** in the pinned code. The only in-place
mutation of an OB dict is `width_pips` (`order_blocks.py:119`). Recorded as §19.2.

### 8.2 The real transition table

| State | Trigger | Source | Prev | Next | Timestamp recorded | Bar identity | Prices | Reason code | Entry eligibility | Stored? |
|---|---|---|---|---|---|---|---|---|---|---|
| *(none)* | swing high/low crossed by close | `order_blocks.py:176-220` | — | **CREATED** | `detection_time`, `origin_time`, `pivot_time` | `detection_index`, `origin_index`, `pivot_index` | `top`,`bottom`,`break_level` | `structure_tag` | becomes admissible | in the OB frame |
| CREATED | `width_pips > 100` | `order_blocks.py:122` | CREATED | **REJECTED** | — | — | — | *(none)* | never admitted | **no — dropped, `ob_id` not consumed** |
| CREATED | `detection_time <= candle.time` on the 1m walk | `execution.py:2329` | CREATED | **ADMITTED (pending)** | `created_time` | `created_candle_index` | `entry`,`stop`,`tp` | — | eligible to arm | `pending` list |
| ADMITTED | `risk <= 0` | `execution.py:706` | ADMITTED | **NOT ADMITTED** | — | — | — | *(silent)* | never | **no row at all** |
| ADMITTED | `penetration >= 25 % depth` (wick) | `execution.py:2516-2523` | ADMITTED | **ARMED** | `trigger_time`, `armed_at` | `trigger_candle_index` | — | — | fillable after +3 | `pending` |
| ADMITTED | `_is_filled` while unarmed | `execution.py:2524-2525` | ADMITTED | **TAPPED** (sub-state) | `tapped_time` | `tapped_candle_index` | — | — | unchanged | `pending` |
| ARMED | `idx >= trigger_idx+3` **and** `low <= entry <= high` | `execution.py:2644` | ARMED | **FILLED** | `fill_time` | fill index | `entry` | — | consumed | `active_trades` |
| ARMED/ADMITTED | blackout + touch | `execution.py:2497-2506` | — | **TERMINAL** | candle time | index | — | `news_touch_cancel` | dead | trades list |
| ARMED | blackout at fill | `execution.py:2840-2850` | ARMED | **TERMINAL** | candle time | index | — | `news_blackout_cancel` | dead | trades list |
| ARMED | PM gate blocks | `execution.py:2671-2673` | ARMED | **TERMINAL** | candle time | index | — | `regime_blocked` (+`regime_block_reason`) | dead | trades list |
| ARMED | cohort/state eligibility blocks | `execution.py:2742-2757` | ARMED | **TERMINAL** | candle time | index | — | `cohort_disabled` \| `state_target_block` | dead | trades list |
| ADMITTED/ARMED | price beyond far edge | `execution.py:2957-2968` | — | **INVALID** | candle time | index | — | `invalidated_before_fill` | dead | trades list |
| ADMITTED/ARMED | end of candle series | `execution.py:3057-3066` | — | **UNFILLED** | — | — | — | `never_triggered` \| `never_filled_after_trigger` | dead | trades list |

### 8.3 Transitions that do NOT exist

Named in the Phase-0 brief but **absent from the pinned engine** — Pine must not invent them:

| Concept | Finding |
|---|---|
| **Mitigation** | No mitigation logic. No `mitigated` field is ever written. The nearest analogue is "first touch" (`tapped_before_trigger`), which only marks a diagnostic. |
| **Partial interaction** | Not supported. `partial_close` is explicitly recorded-but-never-executed (`scenario.py:99`, `rr_unsupported_kind`). |
| **Expiration** | No time-based OB expiry exists. An OB stays pending until filled, invalidated, or the series ends. |
| **Retirement / archival** | No such state. |
| **Replacement / supersession** | None. Overlapping OBs coexist independently; `allow_multi_position` means they never compete. |
| **Session transition effects on OBs** | None. Session only affects the *cohort key at the fill candle*. |
| **Structure transition effects on OBs** | None. A later BOS/CHoCH does not retire an earlier OB. |
| **Trade-entry effects on other OBs** | None under `allow_multi_position` (`execution.py:2634-2635` — `can_fill = True` unconditionally). |

### 8.4 ID determinism

See §6.6. Summary: `ob_id` is deterministic **given the identical bar array from index 0**, and not
otherwise. Pine cannot guarantee it.

---

## 9. Candidate / trade lifecycle

### 9.1 Production outcome vocabulary

> **Corrected in Phase 0.5.** The list below was originally assembled by reading; it is now
> machine-extracted by `tools/oracle/enums.py` (see §21.4). The extractor found **two outcome codes
> this section had omitted**: `NEWS_TOUCH_CANCEL` and `SESSION_FILTERED`. Both are corrected below.
> `NEWS_TOUCH_CANCEL` is **live in production** — the committed golden run contains 120 of them —
> so the omission was material.

Full vocabulary (18 values):

`WIN`, `LOSS`, `OPEN`, `UNFILLED`, `INVALID`, `NEWS_BLACKOUT`, `NEWS_FLATTEN`,
**`NEWS_TOUCH_CANCEL`**, **`SESSION_FILTERED`**, `PROTECTION_EXIT`, `BE_EXIT`,
`REVERSE_TOUCH_CANCEL`, `EXITED_OB_BEFORE_ARM`, `USED_OB_RETRACE_CANCEL`,
`FIRST_FAILED_TAG_CANCEL`, `COHORT_DISABLED`, `STATE_BLOCKED`, `REGIME_BLOCKED`.

`NEWS_TOUCH_CANCEL` is set by `_apply_news_touch_cancel` (`execution.py:1415`);
`SESSION_FILTERED` by `_apply_session_filter` (`execution.py:1383`).

Reachable under the deployed config: **`WIN`, `LOSS`, `OPEN`, `UNFILLED`, `INVALID`,
`NEWS_BLACKOUT`, `NEWS_FLATTEN`, `NEWS_TOUCH_CANCEL`, `STATE_BLOCKED`, `REGIME_BLOCKED`,
`COHORT_DISABLED`** (the last only via a state rescue path that cannot currently trigger, since all
24 cohorts have `base: "allow"`). `SESSION_FILTERED` is **not** reachable
(`session_filter_enabled: false`).

**Note for the extractor's own limits:** `WIN`, `LOSS` and `PROTECTION_EXIT` are assigned from
*variables* (`row["outcome"] = outcome`), not string literals, so a literal scan cannot see them.
They reach Pine through the live-sourced `engine_exited_outcome` enum instead
(`live/intents.py::_EXITED`). This asymmetry is asserted by
`test_win_loss_are_absent_from_the_scan_and_that_is_expected`, so a future refactor that turns them
into literals fails a test rather than silently changing the contract.

The live layer classifies exits with `_EXITED = {WIN, LOSS, NEWS_FLATTEN, PROTECTION_EXIT,
BE_EXIT}` and `_DECIDED = {WIN, LOSS}` (`intents.py:24-25`). **`_DECIDED` is defined but never
used** — dead code in the live wrapper.

### 9.2 Transition table

| Current state | Event / condition | Guard | Next state | Side effects | Reason | Source | Pine sees it? |
|---|---|---|---|---|---|---|---|
| — | OB detection_time reached | `_direction_allowed`, `risk > 0` | PENDING | append to `pending` | — | `execution.py:2329-2374` | **yes** |
| PENDING | 25 % wick penetration | `depth > 0` | ARMED | `trigger_time`, `_delay_window_init` | — | `execution.py:2515-2523` | **yes** |
| PENDING | `low<=entry`/`high>=entry` while unarmed | once only | PENDING (tapped) | `tapped_*` | — | `execution.py:2524-2525` | **yes** |
| PENDING | blackout active | `news_pause_pending_orders` | PENDING (paused) | `news_paused` | — | `execution.py:2493-2495` | **no** |
| PENDING(paused) | blackout ends | — | PENDING | `rearmed_at` | — | `execution.py:2510-2513` | **no** |
| PENDING/ARMED | entry touched in blackout | `news_cancel_if_touched…` | **TERMINAL** | row appended | `news_touch_cancel` | `execution.py:2497-2506` | **no** |
| ARMED | `idx >= trig+3` ∧ in-range | all gates pass | FILLED | `_apply_fill_metrics`, push to `active_trades` | — | `execution.py:2644-2954` | **yes** |
| ARMED | gate: PM | `enforce` | **TERMINAL** | `REGIME_BLOCKED` | `regime_blocked` | `execution.py:2671-2673` | **yes** |
| ARMED | gate: eligibility | confirmed state | **TERMINAL** | `STATE_BLOCKED`/`COHORT_DISABLED` | `state_target_block`/`cohort_disabled` | `execution.py:2742-2757` | **yes** |
| ARMED | gate: news | `news_block_new_fills` | **TERMINAL** | `NEWS_BLACKOUT` | `news_blackout_cancel` | `execution.py:2840-2850` | **no** |
| PENDING/ARMED | far edge breached | strict | **TERMINAL** | `INVALID` | `invalidated_before_fill` | `execution.py:2957-2968` | **yes** |
| FILLED (fill candle) | `_fill_candle_exit_outcome` | gap-fill or close-confirm for TP | **TERMINAL** | `WIN`/`LOSS` | — | `execution.py:2928-2934` | **yes** |
| FILLED | subsequent candle stop/TP | stop wins ties | **TERMINAL** | `WIN`/`LOSS` | — | `execution.py:2467-2474` | **yes** |
| FILLED | flatten window | `news_flatten_active_trades` | **TERMINAL** | `NEWS_FLATTEN` | — | `execution.py:2402-2409` | **no** |
| FILLED | end of series | — | **TERMINAL** | `OPEN` | — | `execution.py:3051-3055` | **yes** |
| PENDING/ARMED | end of series | — | **TERMINAL** | `UNFILLED` | `never_triggered`/`never_filled_after_trigger` | `execution.py:3057-3066` | **yes** |

Unreachable in production: `PROTECTION_EXIT`, `BE_EXIT`, `REVERSE_TOUCH_CANCEL`,
`EXITED_OB_BEFORE_ARM`, `USED_OB_RETRACE_CANCEL`, `FIRST_FAILED_TAG_CANCEL` (§6.15).

### 9.3 Live-layer lifecycle (a separate machine)

Derived from the frame diff, **not** from engine states:

| Engine transition | Intent | Guard | Ledger status path | Source |
|---|---|---|---|---|
| `fill_time` appears, outcome not exited | `OPEN_POSITION` | rails | `simulated` (dry_run) / `sent`→`confirmed`\|`failed` | `intents.py:102-106` |
| `fill_time` appears **and** already exited | `SKIP_INTRA_WINDOW` | — | `simulated` | `intents.py:97-100` |
| Was filled, not exited → now exited | `CLOSE_POSITION` (carries `net_r`) | rails | as above | `intents.py:110-114` |
| Was filled, still open, `stop` changed | `MODIFY_STOP` | rails | as above | `intents.py:118-124` — **unreachable** (§6.15) |

Reconciler states (`live/executor.py:19-106`): findings carry `severity`/`code`/`detail`;
`frozen=True` on an unexpected magic-tagged position, which converts **every** intent that cycle to
ledger status `frozen` and blocks all execution.

**Pine can observe from OHLC alone:** creation, arm, tap, fill, invalidation, stop/TP exit,
unfilled, open, and the state-/PM-driven blocks (given a correct daily market state). **Pine cannot
observe:** anything news-driven, anything ledger/broker-driven, `SKIP_INTRA_WINDOW`, or the
dry-run suppression.

---

## 10. Configuration resolution

### 10.1 Resolution chain

```
scripts/run_backtest.ACTIVE_CONFIG                     (src/config.py dataclass defaults)
   └─ replace(**overrides)  ← rb.load_config_overrides(generated_configs/d6cdae…json)
        └─ assert portfolio_include_disabled_cohorts == True      runner.py:80-81
             └─ replace(end_date = frontier_date)                 runner.py:82   ← THE ONLY LIVE DELTA
                  └─ per-job kwargs  simulation_kwargs(...)       run_backtest.py:2533
                       └─ scenario kwargs (entry model/threshold/delay)  run_backtest.py:2576-2586
```

Contract/version identity:

| Identity | Value | Gate |
|---|---|---|
| `GOLDEN_CONFIG_RELPATH` | `generated_configs/d6cdae589b1e4c37a67763253c466067.json` | path constant, `config.py:21` |
| `ENGINE_VERSION_EXPECTED` | `5bb6372c…305be` (3-file digest) | hard fail, `runner.py:62-65` |
| `ENGINE_MANIFEST_ID_EXPECTED` | `6cb6cbcd…9854b` (30-file digest) | hard fail, `runner.py:66-71` |
| loaded-module provenance | every governed module imported from `LUX_ROOT` | hard fail, `runner.py:72-75` |
| `policy_version` | `2026-07-09.te-v1.2-surgical-disable` | `deployed_policy.v1.json` |
| `execution_policy.checksum` | `c619def209db9843` (State Target Policy v1, rev 1, **status `Candidate`**) | scenario `meta` |
| `INSTANCE_ID` | `live-eurusd-golden-001` | keys every `intent_id`, `world.py:67-71` |

Note the target policy is stamped **`status: "Candidate"`** while running in production — worth an
operator decision, though it changes nothing mechanically.

### 10.2 Resolved values for one processing cycle

| Parameter | Value | Pine treatment |
|---|---|---|
| `symbol` | `EURUSD` (broker symbol = `EURUSD` + empty suffix) | constant |
| `detection_timeframe` | `15min` | constant |
| `execution_timeframe` | `1min` | constant |
| `swing_length` | `50` | **input** (default 50) |
| `ob_filter` | `Atr` | **input** (Atr \| Cumulative Mean Range) |
| `pip_size` | `0.0001` | constant |
| `min_ob_size_pips` / `max_ob_size_pips` | `0` / `100` | **inputs** |
| `structure_filter` | `both` | **input** |
| `allowed_structure_directions` | all four | **input** (4 booleans) |
| `entry_models` | `["triggered_edge"]` | constant |
| `triggered_edge_trigger_thresholds` | `[25]` | **input** |
| `triggered_edge_candle_delays` | `[3]` | **input** |
| `triggered_edge_entry_level_pct` | `0` | **input** |
| `ob_entry_depth_pct` | `0` | **input** |
| `execution_modes` | `["allow_multi_position"]` | constant |
| `trade_direction` | `both` | constant |
| `rr_multiple` | `2` | **input** |
| `stop_buffer_pips` | `1` | **input** |
| `spread_pips` / `slippage_pips` / `commission_r_per_trade` | `0.2` / `0.2` / `0` | **inputs** |
| `protection_modes` | `["baseline"]` | constant (disabled) |
| `be_enabled` | `false` | constant (disabled) |
| `reverse_touch_cancel_enabled` | `false` | constant (disabled) |
| `session_filter_enabled` / `allowed_sessions` | `false` / `[]` | constant (disabled) |
| `triggered_edge_cancel_on_retrace` / `…_on_first_failed_tag` | `false` / `false` | constant (disabled) |
| `news_blackout_enabled` | `true`, `impacts=["high"]`, ±3 min | **input** (see §12 L-07) |
| `news_flatten_minutes_before_blackout` | `5` | **input** |
| `portfolio_policy_enabled` / `_mode` / `_file` | `true` / `enforce` / `configs/policy/deployed_policy.v1.json` | **generated constant** |
| `portfolio_include_disabled_cohorts` | `true` | **generated constant** — drives §6.13 |
| `session_strategy_scenario` | 24 cohorts, 62 state-blocks, 78 custom-RR cells | **generated constant** |
| `start_date` / `end_date` | `2015-01-01` / frontier date | see §10.5 |
| Market state | EMA 200, BBW 20/2, ADX 14, chop < 18, BBW threshold **2.342**, `emaConfirmDays 0` | **inputs** |

### 10.3 Overrides and precedence

Verified precedence at the fill point (`execution.py:2682-2826`):

```
cohort risk_reduction (move_stop)  >  cohort BE  >  global risk_reduction  >  global BE
cohort state_overrides[state].rr   >  cohort target.rr                      (execution.py:2770)
cohort eligibility.states[state]   >  cohort eligibility.base  >  cohort enabled  (scenario.py:50-54)
cohort risk_amount                 >  scenario global_default.risk_amount   >  1.0
```

Eligibility state rules apply **only when the state is both known and `confirmed`**
(`execution.py:2736`). Legacy `state_overrides[*].mode == "block"` is folded into eligibility via
`elig_states.setdefault(state, "block")` — an **explicit** eligibility entry wins
(`scenario.py:120-123`).

### 10.4 Pine inputs vs generated constants

**Should be Pine inputs** (small, meaningful, an operator may want to vary):
`swing_length`, `ob_filter`, `min/max_ob_size_pips`, `structure_filter`,
`allowed_structure_directions`, `trigger_threshold_pct`, `delay_candles`, `entry_level_pct`,
`rr_multiple`, `stop_buffer_pips`, `spread/slippage/commission`, regime parameters.

**Must be generated constants from a canonical snapshot** (too large or too drift-prone to
hand-type): the 24-cohort scenario with its 62 state-blocks and 78 custom-RR cells, and the
**effective** portfolio-policy table (post `with_disabled_as_label`).

**Drift risk — stated plainly.** Hand-transcribing the cohort grid into Pine will drift. There are
`24 × (1 + 6 + 6) = 312` addressable cells — of which **164 are populated today** (24 cohort bases
+ 78 `state_overrides` + 62 `eligibility.states`), measured by
`tools/oracle/extract_contract.py`; a single wrong cell silently changes eligibility for
one session/structure/direction/state combination and will not be caught by eyeballing the chart.
The mitigation is a **generator**: a small script that reads
`generated_configs/d6cdae…json` + `deployed_policy.v1.json`, applies `with_disabled_as_label`,
and emits a Pine constant block plus a **content hash**, which the indicator displays in the HUD so
a stale transcription is visible at a glance. Manual duplication without such a generator should
not be accepted.

### 10.5 Fallback and invalid-configuration behaviour

| Condition | Behaviour | Source |
|---|---|---|
| `portfolio_include_disabled_cohorts` mismatch | `RuntimeError` — refuses to run | `runner.py:80-81` |
| `entry_scenarios` yields ≠ 1 triggered-edge scenario | `RuntimeError` | `runner.py:142-143` |
| Engine digest / manifest / provenance mismatch | `RuntimeError` — refuses to trade | `runner.py:62-75` |
| Invalid `LiveConfig` numerics | `ValueError` at startup | `config.py:74-100` |
| `LIVE_MODE=live` | `SystemExit` | `main.py:211-214` |
| Policy doc invalid | `PolicyError` | `policy.py:127-187` |
| Policy cohort **missing** | fail-**safe**: `ENABLED/LABEL/unknown_cohort` — never blocks | `policy.py:342-343` |
| Unknown market state / warm-up / unconfirmed | base eligibility, base target; never invented | `execution.py:2736,2763` |
| `risk_reduction_mode != "single_step"` | `ValueError` | `execution.py:2287-2290` |
| `risk_amount` negative/NaN/non-numeric | ignored → falls back | `scenario.py:9-21` |
| Unsupported `risk_reduction.kind` | recorded, never executed | `scenario.py:98-99` |
| News file missing while blackout enabled | `FileNotFoundError` | `run_backtest.py:1846-1847` |

**`end_date` is the frontier date, not the frontier timestamp.** `filter_date_range` keeps
`t < end_date + 1 day`, so the recompute includes **the whole UTC day containing the boundary**,
including 1m candles *after* the boundary that arrived in the live segment. Those later candles can
therefore resolve fills and exits within the current cycle. This is production behaviour, and it is
why intents are keyed on state change rather than timestamp equality (`intents.py:76-81`).

---

## 11. Data and timestamp semantics

| Aspect | Production | TradingView | Risk |
|---|---|---|---|
| Historical provider | Dukascopy-derived M1, `data/candles/EURUSD_1m_extended_2015_2026.csv`, ends 2026-06-19 | TradingView's own aggregator | **high** |
| Live provider | MT5 terminal feed, <MT5_SERVER> | — | **high** |
| Seam | frozen wins `<= frozen_end`; live wins `>` | n/a | documented as accepted-and-monitored (`config.py:13`) |
| Price type | Dukascopy **bid**; MT5 bars are bid | TradingView FX is typically bid, but broker-dependent | medium |
| Canonical time base | `utc-v2` — UTC above the gateway | chart timezone is user-set | **must set chart to UTC** |
| Server clock model | base **+2 h**, DST per **America/New_York** — measured, *not* an IANA European zone (`config.py:58-69`) | n/a | affects only bar labelling below the gateway |
| DST | **No DST logic anywhere in the pinned engine.** Sessions are pure UTC hours. | Exchange sessions shift with DST | **high** — L-06 |
| Weekend | No weekend logic. Absent bars simply do not exist. | TradingView inserts/removes per session | medium |
| Missing candles | Silently dropped by `dropna` after resample | Chart has its own gaps | **high** — shifts `candle_index` |
| Duplicate bars | Bridge dedupes (first wins); `prepare_candles_for_simulation` sorts but does **not** dedupe | n/a | low |
| Out-of-order | Sorted by time at `execution.py:663`; bridge appends only `> last stored` | n/a | low |
| Volume | Summed on resample, **never used in any decision** | — | none |
| Tick size / rounding | `pip_size = 0.0001`; **no price rounding anywhere** — raw float comparisons | Pine floats | low but real |
| Timestamp precision | pandas ns; cache keys use `Timestamp.value` (int ns) | Pine ms | low |
| Bar timestamp convention | **Bar-open** (left-labelled resample) | Pine `time` is bar-open | aligned |
| Live partial bars | Never — only closed M1 bars enter the segment (`gateway.closed_m1_bars`) | Pine `barstate.isrealtime` sees partials | **must gate on `barstate.isconfirmed`** |
| Spread | A config constant (0.2 pips), applied post-hoc to every row | Not modelled | low |

### 11.1 Minimum TradingView settings for a meaningful comparison

1. Symbol: an **EURUSD bid** feed; prefer the FTMO/broker feed if available, else a single fixed
   provider held constant across all comparisons.
2. Chart timezone: **UTC**.
3. Timeframe: **15m** for structure/OB parity; **1m** for entry/exit parity.
4. Extended/regular session: **regular**, with the session filter effectively off — the engine has
   no session concept beyond UTC hours.
5. History depth: as deep as the plan allows. All history-seeded values (§6.3, §6.4, §6.10, §6.6)
   degrade with shallow history, so the comparison window must **exclude a warm-up prefix** —
   recommend discarding at least the first 200 15m bars for ATR and the first 200 daily bars for
   the regime panel.
6. `barstate.isconfirmed` gating on every state mutation, to avoid repainting.

---

## 12. Pine limitation register

| ID | Python behaviour | Pine limitation | Exact impact | Deterministic approximation | Approximation label | Expected divergence | Verification method |
|---|---|---|---|---|---|---|---|
| **L-01** | Full recompute from 2015 every cycle; history may be retroactively revised | Pine is forward-only and never revises a closed bar | Pine can never reproduce a retroactive change | None. Accept forward-only semantics | `FORWARD-ONLY` | Rare; only when a revised row changes `fill_time`/`outcome`/`stop` | Golden-trace diff over a window containing a known revision |
| **L-02** | pandas `resample("15min")` over the frozen+live M1 series | TradingView builds its own 15m bars from its own feed | Different bar boundaries/counts ⇒ different pivots, different `candle_index` | Use the native 15m chart; never rebuild from 1m | `FEED-DEPENDENT` | Small but nonzero bar-count delta | Compare bar counts and OHLC per bar against exported M1→15m |
| **L-03** | `_pine_rma(tr, 200)` seeded with `tr[0]`; ATR gate uses `>=` | `ta.atr`/`ta.rma` seed with an SMA | `parsed_high/low` flip decisions differ near the threshold ⇒ different OB boxes | Hand-roll RMA with first-value seed | `EXACT (if history aligned)` | Zero with identical history; unbounded with truncated history | Per-bar ATR export vs Pine plot |
| **L-04** | `swing_trend_bias` never resets; seeded by the first break in loaded history | Pine's history is bounded by plan/chart depth | **BOS↔CHoCH tags can be permanently wrong** on a short chart | Warm-up exclusion; display bias explicitly | `HISTORY-SEEDED` | Deterministic once past warm-up **if** the first break matches | Compare the first 20 structure events against the Python trace |
| **L-05** | `ob_id` = monotonic counter from bar 0 of the 2015→now series | Pine cannot process 2015→now at 15m within limits | Pine `ob_id` ≠ Python `ob_id` | Local counter, clearly labelled | `LOCAL-ID` | Always different unless full history | Map by `(detection_time, direction, top, bottom)` instead of by id |
| **L-06** | Daily regime panel on **UTC** calendar days, shifted one day | `request.security` daily bars follow the exchange session (FX ≈ 17:00 NY) | Different day boundaries ⇒ different `marketState` ⇒ **different eligibility** | Build the daily panel from intraday bars keyed on UTC date, not `"D"` | `UTC-DAY RECONSTRUCTED` | Low if reconstructed correctly; high if `"D"` is used naively | Compare `marketState` per UTC date against the panel export |
| **L-07** | Economic-calendar blackout ±3 min, flatten −5 min, `impact=high` | **No calendar data in Pine** | `NEWS_BLACKOUT`, `news_touch_cancel`, `NEWS_FLATTEN` are unreproducible | Optional manual timestamp array input | `NEWS-UNAVAILABLE` | Every news-affected candidate | HUD flag; exclude news-affected rows from parity scoring |
| **L-08** | Unbounded OB list; per-OB search over pivot→detection | `max_boxes_count` 500, `max_lines_count` 500, `max_labels_count` 500; array/loop limits | Old OBs cannot all stay drawn | Ring-buffer drawings; keep full state in arrays, draw only the last N | `DRAW-WINDOWED` | Visual only, if state is kept separately | Assert state-array length ≠ drawing count |
| **L-09** | `candle_index` = position in the loaded array; the 3-candle delay counts array positions | Pine `bar_index` counts chart bars | If the chart has gaps the M1 series lacks (or vice versa), the +3 delay lands on a different bar | Count **confirmed bars** in a local counter, not `bar_index` | `INDEX-LOCAL` | Only at session/weekend edges | Compare `trigger_candle_index → fill index` deltas |
| **L-10** | `spread_pips`/`slippage_pips` are config constants applied post-hoc | Pine has `syminfo.mintick` but no engine spread model | `net_r` differs if Pine uses live spread | Use the same constants | `CONFIG-CONSTANT` | Zero | Recompute `net_r` from `pnl_r` |
| **L-11** | Same-candle stop/TP ⇒ **LOSS**; fill-candle TP needs gap-fill or close-confirm | Pine has no built-in convention for an indicator | Wrong outcome on ambiguous candles | Implement `_exit_outcome` / `_fill_candle_exit_outcome` verbatim | `EXACT` | Zero | Targeted fixture (§16.6) |
| **L-12** | Live rails: kill switch, duplicate ledger, daily loss, max open, unknown position | No broker, no ledger, no account state | Pine may show a fill the broker never received | None | `EXECUTION-STATE UNKNOWN` | Whenever a rail fires | Permanent HUD warning |
| **L-13** | Reconciler can freeze a whole cycle | No broker truth | Same as L-12 | None | `EXECUTION-STATE UNKNOWN` | Whenever frozen | HUD warning |
| **L-14** | `SKIP_INTRA_WINDOW`: fill+exit inside one 15m window is never mirrored | Pine sees 1m fills directly and cannot know the 15m cadence rule | Pine shows a trade production never sent | **Reproducible**: mark any trade whose fill and exit fall in the same 15m window | `INTRA-WINDOW` | Zero if implemented | Compare against `cycles.jsonl` skipped counts |
| **L-15** | `LIVE_MODE=dry_run` ⇒ every intent is `simulated` | Pine has no mode concept | Pine implies live trading | Static HUD banner | `SHADOW MODE` | n/a | Manual |
| **L-16** | Config comes from a hashed JSON + a versioned policy table | Pine constants are hand-maintained | Silent drift (§10.4) | Generator + content hash in HUD | `CONFIG-HASH <h>` | Zero if generated | Compare hash to the Python-side hash |
| **L-17** | `prepare_news_cache`, regime panel, policy table built once per run over all history | Pine computes per bar, forward-only | Cannot pre-scan the future — but production doesn't either (leakage-safe by design) | Forward-only equivalents | `EXACT` | Zero | Fixture |
| **L-18** | Engine identity gated by two hashes + module provenance | No equivalent | A Pine indicator can silently mismatch the engine | Display the expected `engine_manifest_id` prefix as a constant | `PIN-DISPLAY-ONLY` | n/a | Manual |

**Nothing above is classified as "reproducible" without the corresponding production
implementation having been read.** Items marked `EXACT` were verified line-by-line in §6.

---

## 13. Visual display specification

Derived strictly from production state. Every element traces to a §5 field.

### 13.1 Persistent HUD

| Row | Source | Notes |
|---|---|---|
| **Parity banner** | static | `SHADOW MODE · EXECUTION STATE UNKNOWN · NEWS UNAVAILABLE` — always visible (L-12/13/15/07) |
| `CONFIG-HASH` | generated constant | §10.4 |
| Session | `_session_for_hour(hour_utc)` | label + key |
| Cohort key | `(session_key, structure, direction)` | as resolved at the current bar |
| Market state | `marketState` + `confirmed` | plus `Bull/Bear`, `Expand/Compress`, `Chop/Trend` |
| Regime numerics | `pxVsEma`, `bbw` (vs 2.342), `adx` (vs 18) | debug-mode detail |
| Structure bias | `swing_trend_bias` | `BULL` / `BEAR` / `UNSET` — **must show `UNSET` during warm-up** |
| Current swing high | `swing_high.level`, `.index`, `.crossed` | |
| Current swing low | `swing_low.level`, `.index`, `.crossed` | |
| Active OB count | length of the pending array | |
| Selected candidate | local id, `direction`, `structure_tag`, `top`, `bottom` | |
| Candidate state | `PENDING` / `TAPPED` / `ARMED` / `FILLED` / terminal | §9.2 |
| Arm detail | `trigger_time`, `trigger_candle_index`, bars until `+3` | |
| Eligibility | `ALLOWED` / `BLOCKED` | |
| **Block reasons, ordered** | the §7 evaluation order | show *all* that would apply, in production order, first one authoritative |
| PM decision | `portfolio_policy_regime` (deployed **and** effective), `portfolio_decision_reason`, `portfolio_cohort_key` | show the `include-disabled` override explicitly |
| Entry / Stop / Target | `plan.entry`, `plan.stop`, `plan.tp` | |
| RR | `plan.rr_multiple` — **base and state-overridden** | show both when they differ |
| Risk | `plan.risk` in price and pips | |
| Protection | static `DISABLED (baseline)` | §6.15 |
| Break-even | static `DISABLED` | §6.15 |
| Latest transition | event name + timestamp | §15 |

### 13.2 Chart objects

| Object | Data | Style intent |
|---|---|---|
| Swing high marker | `pivot_index`, `high[pivot]` | plotted at the pivot bar, **drawn only at confirmation (+50 bars)** |
| Swing low marker | `pivot_index`, `low[pivot]` | same |
| Confirmation lag connector | pivot → detection bar | optional, debug |
| `BOS` label | `detection_time`, `break_level` | at the breaking bar |
| `CHoCH` label | `detection_time`, `break_level` | visually distinct from BOS |
| **Internal / external structure** | **DO NOT DRAW** | The distinction does not exist (§5.2, §19.1) |
| Order block box | `origin_time`→`detection_time`, `top`..`bottom` | one box per OB |
| Flipped-origin OB | `top < bottom` | must be rendered distinctly — it is a *rejected* candidate (§6.3) |
| Pending OB | candidate alive | |
| Armed OB | `triggered_edge_armed` | |
| Filled OB | `fill_time` set | |
| Invalidated OB | `INVALID` | |
| Blocked OB | `STATE_BLOCKED` / `REGIME_BLOCKED` / `COHORT_DISABLED` | reason in the label |
| Unfilled OB | `UNFILLED` | |
| ~~Mitigated~~ / ~~retired~~ / ~~expired~~ | **DO NOT DRAW** | No such states (§8.3) |
| OB id label | local id | must carry the `LOCAL-ID` marker (L-05) |
| Trigger level line | `top − 0.25·depth` / `bottom + 0.25·depth` | the 25 % arm threshold |
| Entry line | `plan.entry` | |
| Stop line | `plan.stop` | |
| Target line | `plan.tp` | redrawn if a state override changes RR |
| ~~Protection level~~ / ~~Break-even level~~ | **DO NOT DRAW** | Both disabled (§6.15) |
| Session background | UTC-hour bands | six bands per §5.8 |
| Warm-up shading | first 50 15m bars (swings), first ~200 (ATR), first 200 daily (regime) | parity is meaningless inside |

### 13.3 Debug mode

Always visible: HUD §13.1, chart objects §13.2.

Debug-only (expensive or noisy):
`parsed_high`/`parsed_low` per bar, ATR(200) RMA value and the `2×` threshold,
`high_volatility` flag, `width_pips`, `_ob_depth`, penetration % per candle,
`tapped_before_trigger` markers, `max_distance_away_before_fill_price`,
`_entry_depth_pct`, `effective_entry_depth_pct`, EMA200 / BBW / ADX daily values,
`knownAt` / `shiftedDays`, cohort-index hit/miss, `state_eligibility` (`rescued`),
per-bar `candle_index` vs `bar_index` drift counter, and the drawing-buffer occupancy
(for L-08).

Never displayed: the 10 ghost fields (§2.5), all BE/protection/risk-reduction fields, and every
`Live?=no` row from §7.

---

## 14. Debug-mode specification

Grouped by cost:

| Group | Contents | Cost | Default |
|---|---|---|---|
| **G0 — always** | HUD core, structure labels, OB boxes, entry/stop/target | low | on |
| **G1 — structure internals** | swing confirmation connectors, `crossed` flags, bias transitions, warm-up shading | low | off |
| **G2 — volatility internals** | ATR RMA series, `2×` threshold, `high_volatility`, parsed prices, flipped-origin highlighting | medium | off |
| **G3 — candidate internals** | penetration % per candle, tap markers, arm countdown, delay-window fields | medium | off |
| **G4 — regime internals** | EMA/BBW/ADX plots, per-day state table, confirm flag | **high** (`request.security`) | off |
| **G5 — cohort internals** | resolved cohort key, base vs effective eligibility, base vs override RR, PM decision trace | low | off |
| **G6 — parity instrumentation** | `candle_index` drift counter, drawing-buffer occupancy, config hash, engine-pin prefix | low | on |

---

## 15. Transition-label catalogue

One label per **actual** production transition. Every entry below maps to a verified code path.

| Event | Source state | Destination | Reason | Timestamp | ID | Price | Debug values | Source |
|---|---|---|---|---|---|---|---|---|
| `SWING_HIGH_CONFIRMED` | leg | `BEARISH_LEG` | `leg_change == -1` | pivot bar time | — | `high[pivot]` | `pivot_index`, confirm index | `order_blocks.py:165-170` |
| `SWING_LOW_CONFIRMED` | leg | `BULLISH_LEG` | `leg_change == +1` | pivot bar time | — | `low[pivot]` | same | `order_blocks.py:159-164` |
| `BOS_BULL` | bias `0`/`BULLISH` | `BULLISH` | `prev<=lvl ∧ cur>lvl` | detection bar | new `ob_id` | `break_level` | `prev_close`,`cur_close` | `order_blocks.py:177-181` |
| `CHOCH_BULL` | bias `BEARISH` | `BULLISH` | same predicate | detection bar | new `ob_id` | `break_level` | same | `order_blocks.py:179` |
| `BOS_BEAR` | bias `0`/`BEARISH` | `BEARISH` | `prev>=lvl ∧ cur<lvl` | detection bar | new `ob_id` | `break_level` | same | `order_blocks.py:200-204` |
| `CHOCH_BEAR` | bias `BULLISH` | `BEARISH` | same predicate | detection bar | new `ob_id` | `break_level` | same | `order_blocks.py:202` |
| `OB_CREATED` | — | CREATED | successful `_make_order_block` + size filter | `detection_time` | `ob_id` | `top`,`bottom` | `origin_index`, `width_pips` | `order_blocks.py:193-197` |
| `OB_REJECTED_SIZE` | CREATED | dropped | `width_pips > 100` | detection bar | *(none)* | `top`,`bottom` | `width_pips` | `order_blocks.py:122` |
| `OB_REJECTED_RISK` | — | not admitted | `risk <= 0` (flipped origin) | admission bar | — | — | `depth` | `execution.py:706` |
| `CANDIDATE_ADMITTED` | — | PENDING | `detection_time <= t` | `created_time` | local id | `entry`,`stop`,`tp` | `created_candle_index` | `execution.py:2345` |
| `CANDIDATE_TAPPED` | PENDING | PENDING(tapped) | `_is_filled` while unarmed | `tapped_time` | local id | `entry` | `tapped_candle_index` | `execution.py:2525` |
| `CANDIDATE_ARMED` | PENDING | ARMED | penetration ≥ 25 % | `trigger_time` | local id | trigger level | `trigger_candle_index` | `execution.py:2517-2519` |
| `CANDIDATE_NEWS_PAUSED` | any | paused | blackout | candle time | local id | — | event name | `execution.py:2495` |
| `CANDIDATE_REARMED` | paused | PENDING | blackout ended | `rearmed_at` | local id | — | — | `execution.py:2513` |
| `CANDIDATE_NEWS_TOUCH_CANCEL` | any | TERMINAL | `news_touch_cancel` | candle time | local id | `entry` | event | `execution.py:2502-2504` |
| `CANDIDATE_BLOCKED_PM` | ARMED | TERMINAL | `regime_blocked` + `regime_block_reason` | candle time | local id | `entry` | cohort key, decision | `execution.py:2671` |
| `CANDIDATE_BLOCKED_STATE` | ARMED | TERMINAL | `state_target_block` | candle time | local id | `entry` | `marketState` | `execution.py:2748-2750` |
| `CANDIDATE_BLOCKED_COHORT` | ARMED | TERMINAL | `cohort_disabled` | candle time | local id | `entry` | cohort key | `execution.py:2752-2755` |
| `CANDIDATE_BLOCKED_NEWS` | ARMED | TERMINAL | `news_blackout_cancel` | candle time | local id | `entry` | event | `execution.py:2846-2848` |
| `CANDIDATE_RESCUED` | ARMED | ARMED | `state_eligibility = "rescued"` | candle time | local id | — | state | `execution.py:2761` |
| `TARGET_OVERRIDDEN` | ARMED | ARMED | state custom RR | candle time | local id | new `tp` | old/new RR | `execution.py:2770-2779` |
| `TRADE_FILLED` | ARMED | FILLED | in-range after delay | `fill_time` | `trade_id` | `entry` | fill index, `fill_session` | `execution.py:2852` |
| `TRADE_INVALIDATED` | PENDING/ARMED | TERMINAL | `invalidated_before_fill` | candle time | local id | far edge | — | `execution.py:2966` |
| `TRADE_WIN` | FILLED | TERMINAL | TP (fill-candle rule or later) | candle time | `trade_id` | `tp` | `pnl_r`,`net_r` | `execution.py:2472`, `2932` |
| `TRADE_LOSS` | FILLED | TERMINAL | stop (wins ties) | candle time | `trade_id` | `stop` | `pnl_r`,`net_r` | same |
| `TRADE_NEWS_FLATTEN` | FILLED | TERMINAL | flatten window | candle time | `trade_id` | close | event | `execution.py:2407` |
| `TRADE_OPEN_AT_END` | FILLED | TERMINAL | series end | — | `trade_id` | — | — | `execution.py:3054` |
| `CANDIDATE_UNFILLED` | PENDING/ARMED | TERMINAL | `never_triggered` \| `never_filled_after_trigger` | — | local id | — | — | `execution.py:3061-3065` |
| `INTRA_WINDOW_SKIP` | FILLED | TERMINAL(live) | fill+exit in one 15m window | boundary | `trade_id` | — | outcome | `intents.py:97-100` |

Deliberately **absent** (would be fabrication): `OB_MITIGATED`, `OB_EXPIRED`, `OB_RETIRED`,
`OB_SUPERSEDED`, `PROTECTION_ARMED`, `BE_ARMED`, `BE_MOVED`, `INTERNAL_BOS`, `EXTERNAL_BOS`.

---

## 16. Parity-test strategy

### 16.1 Does production already emit enough?

**No.** What exists:

| Artefact | Contents | Sufficient? |
|---|---|---|
| Trades DataFrame (`result["trades"]`) | Per-**trade** rows with `TRADE_COLUMNS` | Covers terminal outcomes but not per-bar state. **Never written to disk on the live path** — only held in `RunnerState`. |
| `live_state/ops/cycles.jsonl` | Per-cycle: boundary, bars appended, intent counts, reconcile findings | Cycle-level, not bar-level |
| `heartbeat.json`, `provenance.json` | Freshness + data-seam | No |
| CT telemetry payload | Status snapshot | No |
| `run_backtest` report path | Full CSVs + summaries | **Not executed by the live node** |

Missing for a per-bar oracle: swing state, `swing_trend_bias`, per-bar OB status, per-candidate
status, ordered block-reason codes, and the resolved market state per bar.

### 16.2 Smallest instrumentation-only change (proposal — NOT implemented)

**Not implemented in this phase, per instruction.** The minimal change is an **opt-in, off-by-
default, write-only** trace:

- One new module, e.g. `live/oracle_trace.py`, plus an env flag `LIVE_ORACLE_TRACE=<path>`.
- Two call sites, both in `live/runner.py::golden_pipeline`, both inside the existing
  `if artifacts is not None:` capture idiom (`runner.py:152-155`) so the no-trace path stays
  byte-identical:
  1. after `core.detect_order_blocks` + filters — dump the OB frame;
  2. after `execute_scenario_job` — dump the trades frame.
- Per-bar structure state requires either re-deriving it from the OB frame (**preferred — zero
  engine change**, since `pivot_index`/`detection_index`/`structure_tag` are already recorded and
  the leg machine is a pure function of the 15m bars), or a genuine engine change (**rejected** —
  it would break the manifest hash and the trade pin).

**Recommendation: derive, do not instrument the engine.** A standalone reader can reconstruct the
full per-bar trace offline from the 15m frame + the OB frame + the trades frame, with **zero**
change to the 30 governed files and therefore zero risk to the engine pin.

### 16.3 Canonical trace schema

One row per 15m bar (structure fields) with a nested 1m section (candidate fields):

```
symbol, timeframe, bar_time(UTC), open, high, low, close, volume,
session_label, session_key,
current_leg, swing_trend_bias,
swing_high_level, swing_high_index, swing_high_crossed,
swing_low_level,  swing_low_index,  swing_low_crossed,
structure_event ∈ {"", BOS_BULL, CHOCH_BULL, BOS_BEAR, CHOCH_BEAR},
atr200, parsed_high, parsed_low, high_volatility,
obs_created[]: {ob_id, direction, structure_tag, top, bottom, break_level,
                origin_index, pivot_index, detection_index, width_pips},
active_ob_ids[], candidate_status{ob_id: PENDING|TAPPED|ARMED|FILLED|<terminal>},
market_state, trend_state, volatility_state, chop_state, state_confirmed,
cohort_key, portfolio_regime_deployed, portfolio_regime_effective,
eligibility, block_reasons[]  (ordered, production order),
entry, stop, tp, risk, rr_base, rr_effective,
lifecycle_state, transitions[],
config_hash, engine_manifest_id, policy_version
```

### 16.4 Comparison tolerances

| Field class | Rule |
|---|---|
| `structure_event`, `structure_tag`, `direction`, `session_key`, `market_state`, `eligibility`, `block_reasons`, `lifecycle_state`, outcomes | **Exact match. Zero tolerance.** |
| `top`, `bottom`, `entry`, `stop`, `tp`, `break_level`, swing levels | `<= 1e-7` (sub-tenth-pip) when feeds match; `<= 0.5 pip` when feeds differ, and then **flagged as feed-driven** |
| `atr200`, `bbw`, `adx`, `pxVsEma` | `<= 1e-6` relative, **only outside the warm-up prefix** |
| `risk`, `rr_effective` | `<= 1e-9` |
| `pnl_r` | Exact (`-1.0` or `rr_multiple`) |
| `net_r` | `<= 1e-6` |
| `ob_id` | **Not compared.** Match on `(detection_time, direction, top, bottom)` (L-05) |
| `candle_index` | **Not compared.** Compare index *deltas* (L-09) |
| News-affected rows | **Excluded from scoring**, counted and reported separately (L-07) |

### 16.5 Golden fixtures

| Fixture | Window | Purpose |
|---|---|---|
| `F-INIT` | first 400 15m bars from the true series start | warm-up, cold-start leg asymmetry, `swing_trend_bias` seeding |
| `F-STEADY` | one clean month, mid-history, no news | baseline structure + OB parity |
| `F-LONDON` | 20 sessions, 07:00–10:00 UTC | most active cohort |
| `F-SESSIONS` | 5 consecutive full UTC days | all six session bands incl. the `ny_pm` 15–17 window |
| `F-DST` | 2026-03-08 ± 3 days and 2026-03-29 ± 3 days | the measured US-vs-EU DST divergence (`config.py:58-69`) |
| `F-NEWS` | a week with several high-impact events | news-driven divergence measurement |
| `F-STATE` | a window spanning a `marketState` change | eligibility flip |
| `F-FRONTIER` | the most recent 2 days | live-segment seam behaviour |

### 16.6 Required test cases

| # | Case | Expectation |
|---|---|---|
| T-01 | Initialization / warm-up | No swings before bar 50; first break tagged **BOS**; `swing_trend_bias` starts `UNSET` |
| T-02 | Cold-start leg asymmetry | An initial `new_leg_high` records **nothing** (`leg_change == 0`) |
| T-03 | Equal highs in the right window | Strict `>` ⇒ **no** swing |
| T-04 | Equal lows in the right window | Strict `<` ⇒ **no** swing |
| T-05 | `new_leg_high` and `new_leg_low` on one bar | High wins (`elif`) |
| T-06 | Consecutive same-direction breaks | Second is **BOS**, not CHoCH |
| T-07 | Rapid alternating breaks | Correct BOS/CHoCH alternation; `crossed` latches |
| T-08 | Bull and bear break on one bar | Bull emitted first; bear tag uses the bias the bull just set |
| T-09 | `prev_close` exactly at the swing level | Break **allowed** (`<=` / `>=`) |
| T-10 | High-volatility origin bar | `top < bottom`; `risk <= 0`; **no candidate** |
| T-11 | ATR exactly at `2×` threshold | `>=` ⇒ flip occurs |
| T-12 | OB width exactly 100 pips | `> 100` is false ⇒ **accepted** |
| T-13 | Equal parsed extremes in the pivot window | **Earliest** index wins |
| T-14 | Overlapping OBs | Both live independently; no supersession |
| T-15 | Penetration exactly 25 % | `>=` ⇒ **armed** |
| T-16 | Fill on exactly `trigger_idx + 3` | **Allowed** |
| T-17 | Fill attempt at `trigger_idx + 2` | **Refused** |
| T-18 | Entry exactly at `candle.low` or `candle.high` | In-range ⇒ **fill** |
| T-19 | Fill and invalidation on one candle | **Fill wins** (order of branches) |
| T-20 | Gap-fill same-candle TP | **Booked** |
| T-21 | Non-gap same-candle TP, close below target | **Not booked**; resolves later |
| T-22 | Same-candle stop and TP | **LOSS** |
| T-23 | Fill-candle stop | **LOSS**, booked immediately |
| T-24 | Session boundary at 07:00 / 10:00 / 12:00 / 15:00 / 17:00 UTC | `[start, end)`; 17:00 ⇒ `outside` |
| T-25 | DST dates 2026-03-08 and 2026-03-29 | Sessions **do not shift** (no DST logic) |
| T-26 | Missing 15m bars | Bar dropped, not filled; index deltas explain the shift |
| T-27 | Duplicate M1 bars | Bridge dedupe; no double-count |
| T-28 | Unfilled at series end | `UNFILLED` + `never_triggered` \| `never_filled_after_trigger` |
| T-29 | Each block reason individually | One fixture per **live** row in §7 (B11,B12,B13,B18,B19,B21,B22,B23,B26) |
| T-30 | State-override RR change | `tp` moves; `entry`/`stop`/`risk` unchanged |
| T-31 | State rescue inside a disabled cohort | `state_eligibility = "rescued"` |
| T-32 | Unconfirmed / warm-up state | **Base** eligibility and **base** target used |
| T-33 | PM `DIRECTION_AWARE` counter-trend | `direction_mismatch` |
| T-34 | PM `STATE_ONLY` in Chop | `state_not_allowed` |
| T-35 | PM cohort absent from table | Fail-safe: **allowed**, `unknown_cohort` |
| T-36 | `include_disabled_cohorts` override | All 12 EURUSD `DISABLE` cohorts behave as `LABEL` |
| T-37 | Stop/target price rounding | No rounding applied anywhere |
| T-38 | Protection / BE / risk-reduction | All **inert**; no lines drawn |
| T-39 | Intra-window fill+exit | Marked `INTRA-WINDOW`, excluded from mirrored trades |
| T-40 | Config change | `CONFIG-HASH` changes; parity re-baselined |
| T-41 | Engine restart / state restoration | Cycle replays; identical `intent_id`s; ledger suppresses duplicates |
| T-42 | Boundary arithmetic | `last_m1=10:44` ⇒ `boundary=10:30`; `10:45` ⇒ `10:30`; `10:46` ⇒ `10:45` |

### 16.7 Existing production tests that already pin audited behaviour

These are the **existing** regression tests protecting the behaviours mapped above. Each Pine stage
in §18 should be checked against the corresponding Python test's expectations, since these are the
behaviours the engine has already committed to.

**Engine — `Lux-OB-Backtester/tests/` (32 pytest-compatible modules, 6 script-style):**

| Test module | Pins | Audit section | Pine stage |
|---|---|---|---|
| `test_strategy_core_slice2_swings.py` | swing leg machine, constants | §6.2 | S3 |
| `test_strategy_core_slice4_order_blocks.py` | **the highest-value module for Pine.** `test_pine_rma_seeding_and_recursion` (first-value seed), `test_true_range_first_row_ignores_missing_previous_close`, `test_parsed_price_flip_on_high_volatility_candle`, `test_size_filter_threshold_equality_is_strict`, `test_bullish_then_bearish_creation_order_ids_and_tags`, `test_duplicate_breaks_do_not_recreate_crossed_swings`, `test_make_order_block_empty_search_returns_none`, `test_cumulative_mean_range_divisor_quirk_preserved` | §6.3, §6.4, §6.5 | S2, S4, S5 |
| `test_strategy_core_slice3_sessions.py`, `test_session_assignment.py` *(script-style)* | UTC-hour session table incl. `ny_pm` | §6.11 | S1 |
| `test_regime_parity.py` | byte-parity of the daily panel against the frozen JS/Python fixture | §6.10 | S6 |
| `test_regime_config.py`, `test_regime_emission.py`, `test_regime_filter.py`, `test_regime_direction_filter.py` | regime params, emission, filter/direction predicates | §6.10, §6.13 | S6, S10 |
| `test_portfolio_policy.py`, `test_portfolio_policy_mirror.py`, `test_portfolio_execution.py`, `test_pm_annotate_gate.py` | policy schema, checksum, `decide` fail-safe, enforce/annotate gate | §6.13 | S10 |
| `test_eligibility_policy.py`, `test_state_target_policy.py`, `test_combined_smoke_eligibility_target.py` | eligibility base/rescue/block, state target overrides | §6.12 | S10 |
| `test_same_candle_fill_ordering.py` | **`_fill_candle_exit_outcome`** gap-fill / close-confirm rule | §6.9 | S9 |
| `test_delayed_te_fill.py` *(script-style)* | the +3-candle delay | §6.7 | S8 |
| `test_strategy_core_slice7_execution.py` | walk-level extraction parity | §3.3 | S7–S9 |
| `test_strategy_core_slice8_boundary.py` | core/driver boundary contract | §4 | — |
| `test_risk_amount.py`, `test_risk_reduction_move_stop.py`, `test_be_*.py`, `test_fft_width_gate.py`, `test_confirmed_revisit.py`, `test_cancel_exit_before_arm.py`, `test_post_stop_continuation.py` | features **inert in production** (§6.15) | §6.15 | not staged |

**Live wrapper — `live-trading-control-tower_emergent-main/backend/tests/`:**

| Test module | Pins | Audit section |
|---|---|---|
| `test_live_slice.py` | `test_latest_closed_boundary` (**§3.5 boundary arithmetic**), `test_diff_deterministic_and_idempotent_ids`, `test_diff_transitions_exit_stopmove_and_intra_window`, `test_skip_intra_window_is_deterministic_and_never_applied`, `test_assemble_candles_seam_frozen_wins`, `test_bridge_appends_dedupes_and_heartbeats`, rails tests | §3.1, §3.4, §7 B29–B36, §9.3 |
| `test_engine_identity.py` | manifest / digest / module-provenance gating | §2.1, §10.1 |
| `test_production_hardening.py`, `test_regression_repairs.py` | LR-1 commit deferral, duplicate suppression, reconcile-first | §3.1 |
| `test_recovery.py`, `test_supervision.py` | restart / state restoration (T-41) | §16.6 |
| `test_canonical_publication.py`, `test_telemetry_*.py`, `test_account_observation.py`, `test_status_account_rows.py`, `test_observation_blast_radius.py` | telemetry scope guards (off the Pine surface) | §2.3 |

**Notably absent — and therefore where new fixtures are most needed:** there is **no** existing
test for the `_add_parsed_prices` → `_make_order_block` interaction over a real price series, none
for the OB **invalidation** rule (§6.8), and none asserting the **ordering** of the block gates in
§7. Fixtures `F-INIT` and `F-STEADY` should close all three.

### 16.8 Test-suite baseline measured during this audit

Run with the production venv (`.venv\Scripts\python.exe`), `PYTHONPATH` set to each repo root.
**Read-only; nothing was modified in either repository.**

| Suite | Result |
|---|---|
| `Lux-OB-Backtester/tests` (6 script-style modules excluded — they call `sys.exit()` at import and crash pytest collection) | **257 passed, 1 skipped, 2 failed** |
| `live-trading-control-tower_emergent-main/backend/tests` | **423 passed, 2 skipped, 1 collection error** |

All four anomalies were investigated and are **pre-existing environment artefacts, not logic
failures**. None invalidates any claim in this audit:

| Anomaly | Cause | Impact on this audit |
|---|---|---|
| `test_strategy_core_slice8_boundary.py::test_pure_core_import_loads_no_driver_modules_and_writes_nothing` | The test's audit hook flags any `socket.*` event during `import strategy_core`. Probed directly: **`import pandas` alone** emits `socket.gethostname`; `strategy_core` adds **zero** further events and loads **zero** driver modules. The test cannot distinguish its dependency's import-time hostname lookup from a real violation. No `socket` usage exists anywhere in the Lux repo. | **None** — the §4 purity claim is confirmed, by direct probe rather than by this test |
| `test_portfolio_replay.py::test_full_replay_no_mismatches_and_reasons` | Reports `EURUSD: missing run/candle file (skipped)` — the replay validator needs prior backtest run outputs absent from this checkout | **None** — `src/portfolio_replay.py` is off the live path (§2.4) |
| `test_control_tower_api.py` collection error | `ModuleNotFoundError: requests` — the FastAPI console test needs deps not installed in the live-node venv | **None** — Control Tower console, not trading logic |
| 6 script-style Lux test modules | `test_cancel_exit_before_arm`, `test_confirmed_revisit`, `test_delayed_te_fill`, `test_run_metadata`, `test_session_assignment`, `test_strategy_core_slice1` call `sys.exit()` at module import, which raises `INTERNALERROR` in pytest collection. They are runnable directly (`python tests/<file>.py`) | **None**, but note that `test_delayed_te_fill` and `test_session_assignment` cover behaviour Pine must mirror (§6.7, §6.11) and will not run under a plain `pytest tests/` invocation |

**Recommendation:** the parity harness (§17) should invoke the 6 script-style modules directly, not
via pytest, so their coverage is not silently lost.

### 16.9 Failure reporting and regression process

- Per-fixture report: rows compared, exact-match failures (**always fatal**), tolerance failures,
  feed-attributed divergences, news-excluded rows, warm-up-excluded rows.
- Any exact-match failure blocks the stage. Tolerance failures are triaged into
  **feed-driven** (accepted, logged with magnitude) vs **logic-driven** (fatal).
- Every fixture is re-run whenever the Pine file, the generated config block, or the engine pin
  changes. The HUD's `CONFIG-HASH` and engine-pin prefix make a stale run self-evident.

---

## 17. Golden-trace proposal

**Not implemented in this phase.** Summary of the recommendation from §16.2:

1. **Preferred — offline derivation, zero engine change.** A standalone reader in the *Control
   Tower* repo (never in `LUX_ROOT`) that loads the same M1 CSVs, applies `src/resample.py`, replays
   the pure leg/BOS machine (a faithful transcription of `order_blocks.py:144-220`), joins the OB
   frame and the trades frame, and emits §16.3. Because it lives outside the 30 governed files it
   cannot move `engine_manifest_id`, and it can be verified against the live node's own OB/trades
   frames.
2. **Fallback — opt-in capture hook.** Extend the existing `artifacts` capture in
   `golden_pipeline` (`runner.py:152-155`) behind `LIVE_ORACLE_TRACE`. This touches
   `live/runner.py` only — **not** a governed engine file — so the pin is still safe. It costs one
   dump per cycle and is off by default.
3. **Rejected — instrumenting `strategy_core`.** Any edit inside the walk changes
   `engine_manifest_id`, which makes `verify_engine()` refuse to trade (`runner.py:66-71`). Not
   acceptable for a debugging aid.

Trace files should be written under `live_state/oracle/` (git-ignored, like the rest of
`live_state/`) and stamped with `config_hash`, `engine_manifest_id` and `policy_version`.

---

## 18. Staged implementation plan

Each stage is gated: **nothing advances until its parity fixture passes.** The order below deviates
from the suggested sequence in two places, because the real dependency graph requires it:

- **Volatility/parsed prices must precede order-block creation** — `top`/`bottom` are *parsed*
  values, so an OB cannot be built before the ATR flip is exact (§6.3).
- **Market state must precede entry eligibility** — eligibility is keyed on `marketState`
  (§6.12), so the daily panel is a hard prerequisite, not a later refinement.

| # | Mirrors | Repository symbols | Pine state | Visual output | Fixture | Acceptance | Known limitations | Prereqs |
|---|---|---|---|---|---|---|---|---|
| **S1** | Data/time/session parity | `src/resample.py:resample_candles`; `strategy_core/sessions.py` | none | session bands, UTC clock, bar-count readout | `F-SESSIONS` | 100 % session-label match; bar-count delta measured and recorded | L-02, L-06 | — |
| **S2** | Volatility + parsed prices | `order_blocks.py:_true_range`, `_pine_rma`, `_add_parsed_prices` | ATR RMA var | ATR plot, `high_volatility` markers, parsed H/L | `F-STEADY` | ATR `<= 1e-6` rel. outside warm-up; flip flags 100 % | **L-03** | S1 |
| **S3** | Swing primitives | inlined loop `order_blocks.py:144-171` | `current_leg`, two swing records | swing markers with +50 confirmation | `F-INIT`, `F-STEADY` | 100 % pivot match outside warm-up; T-01..T-05 pass | L-04 warm-up | S1 |
| **S4** | BOS/CHoCH + bias | `order_blocks.py:173-220` | `swing_trend_bias`, `crossed` | BOS/CHoCH labels, bias in HUD | `F-INIT`, `F-STEADY` | 100 % event + tag match; T-06..T-09 pass | **L-04** | S3 |
| **S5** | Order-block creation | `_make_order_block`, `_passes_size_filter` | OB array | OB boxes with local ids | `F-STEADY` | 100 % match on `(detection_time, direction, top, bottom)`; T-10..T-13 pass | L-05, L-08 | S2, S4 |
| **S6** | Market state | `strategy_core/regime.py`; `run_backtest.py:2430-2437` | daily panel arrays | state in HUD, EMA/BBW/ADX debug | `F-STATE` | 100 % `marketState` per UTC date outside warm-up | **L-06** | S1 |
| **S7** | Candidate creation + arming | `execution.py:2329-2374`, `_planned_trade`, `_triggered_edge_touched` | candidate array | entry/stop/target lines, 25 % trigger line, arm markers | `F-STEADY` | 100 % arm bar match; T-15 passes | L-09 | S5 |
| **S8** | Fill, delay, invalidation | `execution.py:2644`, `2957-2968` | index counter | fill/invalid markers | `F-STEADY` | 100 % fill + invalidation bar match; T-16..T-19, T-23 pass | **L-09** | S7 |
| **S9** | Exits and RR | `_exit_outcome`, `_fill_candle_exit_outcome`, `_apply_execution_costs` | per-trade outcome | WIN/LOSS markers, `pnl_r`/`net_r` | `F-STEADY` | 100 % outcome match; T-20..T-22 pass | L-11 | S8 |
| **S10** | Cohort + PM eligibility | `scenario.py:_build_cohort_index`; `policy.py:decide`; `execution.py:485-572`, `2711-2779` | generated constant tables | eligibility + ordered block reasons in HUD | `F-LONDON`, `F-STATE` | 100 % eligibility and reason match; T-29..T-36 pass | **L-16** | S6, S7 |
| **S11** | Full lifecycle + transitions | §9.2, §15 | transition log | transition labels | `F-STEADY`, `F-FRONTIER` | every §15 event emitted at the right bar | L-01 | S9, S10 |
| **S12** | Intra-window + parity flags | `intents.py:70,95-100`; `runner.py:98-103` | boundary tracker | `INTRA-WINDOW` marks, parity banner | `F-FRONTIER` | matches `cycles.jsonl` skip counts; T-39, T-42 pass | L-12..L-15 | S11 |
| **S13** | HUD + debug modes | §13, §14 | — | full HUD, G0–G6 | all | no regression in S1–S12 | L-08 | S12 |
| **S14** | Golden-trace harness | §17 | — | automated diff report | all | all fixtures pass; report generated | L-01 | S13 |

**Explicitly not staged** (nothing to mirror): protection, break-even, risk reduction,
reverse-touch-cancel, session filter, FFT/retrace cancel, ghost tracker, internal/external
structure, mitigation, OB expiry/retirement.

---

## 19. Open evidence gaps

Honest list of what was **not** established. None blocks Phase 1 as scoped, but each should be
closed before the corresponding stage.

### 19.1 Concepts named in the brief that do not exist in the pinned engine

Searched and **not found** as production behaviour: internal structure, external structure,
mitigation, partial mitigation, OB expiration, OB retirement, OB supersession, protection
activation (configured off), break-even movement (configured off), candidate expiration. §8.3
documents the absence. **Gap:** if these exist in a *different*, non-pinned implementation, that
implementation is not what runs here — confirm the brief's vocabulary was not carried over from
the excluded Pine files or the Mac-side repo.

### 19.2 Stale in-repo documentation

- `strategy_core/types.py:36` claims OB lifecycle fields "mutate in place: mitigation, breach,
  taps". No such mutation exists in the pinned code (only `width_pips`). Either the comment is
  stale or a consumer outside the 30 governed files does this. **Not resolved.**
- `strategy_core/regime.py:10` states "Nothing in `src/execution.py` imports this yet." It is
  imported and used (`execution.py:485-572`, `2728-2732`). Stale docstring.
- `live/intents.py:24` defines `_DECIDED` which is never referenced. Dead constant.

### 19.3 Not read in full

| Item | Why it matters | Risk |
|---|---|---|
| `relevant_news_currencies(config)` | Determines the EURUSD news currency set (§6.14) | low — news is unreproducible in Pine anyway (L-07) |
| `_regime_filter_block_row` internals (`execution.py:420-484`) | The `REGIME_FILTER_BLOCKED` outcome string was not confirmed | low — the branch is inert (B16) |
| `run_backtest.py` outside the ~600 lines on the live path (of 4126) | Report/sweep/CLI code | low — verified not reachable from `execute_scenario_job` |
| `strategy_core/execution.py` lines 279-660, 1180-1560, 1810-2040 | Metrics, delay-window, excursion post-pass | **medium** — these write trade-row fields the HUD may want; none affect fills |
| `live/mt5_gateway.py`, `publisher.py`, `telemetry.py`, `state.py` bodies | Verified by interface and call sites, not line-by-line | low — outside the Pine surface |
| `src/execution.py:prepare_news_cache` full body | Read the window logic only | low |
| `_apply_execution_costs` exact `net_r` formula | Needed for exact `net_r` parity in S9 | **medium** — read before S9 |

### 19.4 Not executed

The `golden_pipeline` was **not run** during this audit (a full recompute takes ~1119 s and the
production node holds the state-dir lock). All pipeline claims are from code reading plus the
verified engine pin. **Only** the engine-identity verifier was actually executed.

### 19.5 ~~Unverified numeric assumption~~ — CLOSED

§6.3 item 2 (bar-0 TR resolving to `H−L` because pandas `max(axis=1)` skips NaN) was flagged as
inferred. **Now closed by executed test evidence:**
`tests/test_strategy_core_slice4_order_blocks.py::test_true_range_first_row_ignores_missing_previous_close`
asserts `tr.iloc[0] == 1.0` for `high=2, low=1` — i.e. exactly `H−L`, with the NaN previous close
skipped by the row-wise max. The companion
`test_pine_rma_seeding_and_recursion` pins the first-value seed
(`[10,20,30]` with `length=2` → `[10.0, 15.0, 22.5]`). Both **passed** in the run recorded in
§16.8. The §6.3 description is therefore verified, not inferred, and **S2 is unblocked**.

### 19.6 Operational observations (not defects, but worth an operator decision)

- The scenario's `execution_policy.status` is **`"Candidate"`** while deployed in production (§10.1).
- The `ct-capacity-worktree` branch carries *"ob_id is a PUBLIC identifier, not a run-local label"*.
  If that lands on the running branch, §6.6 and L-05 must be re-audited.
- The live node is in `dry_run`; **no parity claim about broker execution can be made** from
  current data.

---

## 20. Go / no-go recommendation

### 20.1 Verdict

**GO for Phase 1, Stages S1 through S9** (data/time/session → volatility → swings → BOS/CHoCH →
order blocks → market state → candidates → fills → exits).

The original restriction to S1 was lifted during validation: §19.5 (the ATR-seed assumption, the
one blocker for S2) was **closed by executed test evidence** (§16.8), and the engine's own test
suite was found to already pin most of S2–S9's acceptance criteria (§16.7).

**CONDITIONAL GO for S10** (cohort + PM eligibility) — proceed only once the config **generator**
from §10.4 exists. Hand-transcribing 164 populated config cells (of 312 addressable) is the largest
silent-failure risk in the
project and must not be accepted.

**NO-GO for any stage that presents Pine output as authoritative.** The oracle must ship with the
parity banner from §13.1 from its first commit, not added later.

### 20.2 Why go

- The canonical runtime, branch, commit, engine pin and deployed configuration are all **verified,
  not assumed** — the manifest verifier was executed and matched.
- The trading-relevant decision surface is small and pure: `strategy_core` plus `src/resample.py`.
  It has no I/O, no clock, and no hidden state (`strategy_core/__init__.py:9-15`).
- Every algorithm in §6 was read line-by-line, and its comparison operators, tie-breaks and
  ordering are documented.
- More than half of the engine's configurable machinery is **inert** under the deployed config
  (§6.15), which shrinks the Pine surface substantially.
- Every genuinely blocking limitation is registered with a named approximation and a verification
  method (§12).

### 20.3 Why restricted

Four risks are structural, not incidental:

1. **L-04 — `swing_trend_bias` is history-seeded and never resets.** On a chart with less history
   than 2015→now, BOS/CHoCH tags can be permanently inverted. This is not a tolerance issue; it is
   a correctness cliff. S3/S4 must prove convergence on `F-INIT` before S5 is attempted.
2. **L-03 — the ATR uses a non-standard RMA seed** and gates on `>=`. It decides `parsed_high` /
   `parsed_low`, which decide the OB box itself. Everything downstream inherits this.
3. **L-06 — the regime panel uses UTC calendar days**, which TradingView's daily bars do not. Since
   eligibility is keyed on `marketState`, a naive `request.security(…, "D", …)` would produce
   plausible-looking but wrong block decisions.
4. **L-16 — 164 populated config cells** (of 312 addressable) must reach Pine without drift.
   Hand-transcription
   should not be accepted; the generator + HUD content hash from §10.4 is the gate.

### 20.4 Conditions to attach to Phase 1

1. ~~Resolve §19.5 before S2.~~ **Closed** — see §16.8 / §19.5.
2. Read `_apply_execution_costs` (`execution.py:1476-1506`) before S9 — the exact `net_r` formula is
   the one remaining unread symbol on the Pine surface (§19.3).
3. Build the config generator (§10.4) **before S10** — no hand-typed cohort grid.
4. Ship the §13.1 parity banner in the first commit.
5. Never draw mitigation, expiry, retirement, protection, break-even, or internal/external
   structure (§8.3, §15).
6. Do not open any of the excluded `.pine` files as a starting point (§2.4).
7. Re-run this audit if `engine_manifest_id`, `policy_version`, or the Golden config hash changes.
8. Re-check §16.8's four test anomalies at the start of each stage — if any *changes character*
   (e.g. the purity test starts reporting a non-`socket` violation), stop and re-audit.

### 20.5 Confirmation of scope compliance

- ✅ No Pine Script was written.
- ✅ No production behaviour was changed.
- ✅ Documentation-only: one new file (this document) plus one navigation line in `PROJECT_STATE.md`.
- ✅ No file inside `LUX_ROOT` was modified — the engine manifest still verifies.
- ✅ The live node was not stopped, restarted, or interfered with.

---

# 21. Engine evolution and parity synchronisation (Phase 0.5)

Phase 0 mapped the engine as it is today. This section defines how the Pine oracle stays correct
when it changes. Operational instructions live in
[tradingview_visual_oracle_maintenance.md](tradingview_visual_oracle_maintenance.md); this section
is the design and its justification.

**Synchronisation model — strictly one-way:**

```
Python production implementation      ← the only authority
   ↓  tools/oracle/extract_contract.py
canonical parity contract             ← contracts/tradingview_oracle_contract.json (generated)
   ↓  tools/oracle/generate_pine.py   [Phase 1]
generated constants / enums
   ↓
Pine visual oracle                    ← generated, never hand-edited
   ↓  golden-trace comparison
parity verdict                        ← artifacts/tradingview_oracle/parity_manifest.json
```

Pine is never an authority, and there is no reverse edge in this graph.

## 21.1 Canonical parity contract

**Location:** `contracts/tradingview_oracle_contract.json` (generated),
`contracts/tradingview_oracle_contract.schema.json` (its schema).

Chosen over `golden/` because `golden/run-001/` is an immutable archive of one historical run,
whereas the contract is *current deployed state* and is regenerated on every engine change. It sits
in the Control Tower repo, never in `LUX_ROOT` — writing into the engine tree would move
`engine_manifest_id` and make the deployment refuse to trade.

Extracted live from the deployed engine, 47 KB, covering: the fingerprint; all 30 governed file
digests; the resolved Pine-relevant configuration; feature flags and any unsupported-active
violations; the session table; market-state parameters; the compiled 24-cohort index with its 164
populated cells; the effective portfolio-policy table; time/data semantics; and 26 enums.

**Extraction discipline.** Every value carries a `_source` marker:

| Source | Count | Meaning |
|---|---|---|
| `live` | 17 enums | imported from the running engine — cannot drift silently |
| `scan` | 9 enums | parsed from pinned source (values exist only as string literals) |
| `manual` | **0** | hand-maintained behavioural values |

`manual_behavioural_values == 0` is asserted by
`test_no_behavioural_value_is_hand_maintained`. The only hand-written content in the contract is
the *oracle's own* vocabulary (`oracle_only_enums`, `oracle_status_extensions`), which describes
Pine, not production.

The scan exists because the dominant production block path (`state_target_block`) is a bare string
literal with no importable constant. A hand-listed mapping would silently miss the next one.

## 21.2 Engine fingerprint

**Implemented:** `tools/oracle/fingerprint.py`.

```
oracle_engine_id   : lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.0.0_g0.1.0
oracle_engine_hash : 63b8144e3ca91e2b068e27be73522d33f2b7e0cd893fca2dc129924fb0c0a779
```

> **Superseded at Stage S1.** The trace schema and generator both moved, so the live
> fingerprint is now `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.1.0_g0.2.0` /
> `08344d7e…6639`. The value above is retained deliberately: it is the worked example
> showing that a schema bump *does* move the fingerprint, which is the property this
> section exists to demonstrate. The authoritative current value is whatever
> `python -m tools.oracle.check_freshness` prints. See §22.4.

Composed from **content**, never revisions. The commit is neither sufficient nor necessary, and
this deployment demonstrates both: the engine is pinned at one commit while its configuration lives
in two separately-versioned files; and the production gate itself already distrusts the commit,
gating on `ENGINE_VERSION_EXPECTED` **and** `ENGINE_MANIFEST_ID_EXPECTED`.

| Input | Why it is in |
|---|---|
| `engine_manifest_id` | the real 30-file engine identity |
| `engine_version` | Lux's 3-file digest; continuity with the Golden pin |
| `resolved_config_hash` | `run_backtest.config_hash` — production's own config identity |
| `policy_version`, `policy_content_sha256` | the policy table moves independently of the engine |
| `contract_schema_version`, `trace_schema_version`, `generator_version` | each changes what Pine renders |
| `symbol`, `detection_timeframe`, `execution_timeframe` | the data context the build is valid in |

**Two deliberate exclusions:**

- **`end_date`** — the single value the live runner overrides every cycle (`runner.py:82`). It
  advances with the frontier, so hashing it would invalidate the Pine build daily for no
  behavioural reason, and it has no Pine meaning (a chart has no end date). Asserted by
  `test_fingerprint_excludes_the_live_only_delta`.
- **The environment** (python/numpy/pandas/platform) — **recorded, never hashed**. The project has
  measured a ~220 ULP `bbw_value` shift between hosts from dependency versions alone
  (`live/engine_identity.py:184-202`, `live/deploy_check.py:90-92`). Hashing it would invalidate
  every build on a patch bump that cannot change a rendered decision; dropping it would hide a real
  cause of numeric divergence. It therefore travels with the build as provenance and is consulted
  only during divergence triage. Asserted by `test_fingerprint_does_not_hash_the_environment`.

The readable id is a HUD label; tools compare only the hash. `compose()` accepts the three version
fields as arguments (defaulting to the module constants) so an archived manifest stays re-derivable
from its own `inputs` after a generator bump — otherwise old parity records would be unfalsifiable.

## 21.3 Pine version banner

The HUD banner extends the Phase 0 §13.1 block with generated identity. Every field is a
generated constant, so it cannot be forgotten.

```
ORACLE  v<oracle_version>   <oracle_engine_id>
ENGINE  <engine_manifest_id[:12]>   CFG <resolved_config_hash[:8]>   POL <policy_version>
SCHEMA  contract v1.0.0 · trace v1.0.0 · generator v0.1.0
CONTEXT EURUSD · 15m/1m · chart TZ must be UTC
BUILT   <generated_at>
PARITY  <global_status>   S1-S5 MATCHED · S6-S14 UNIMPLEMENTED
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
SHADOW MODE · EXECUTION STATE UNKNOWN · NEWS UNAVAILABLE
```

Statuses: `MATCHED`, `PARTIAL`, `UNVERIFIED`, `STALE_ENGINE`, `STALE_CONFIG`, `STALE_CONTRACT`,
`STALE_TRACE_SCHEMA`, `ENGINE_MISMATCH`, `CONFIG_MISMATCH`, `UNSUPPORTED_DATA_CONTEXT`,
`INCOMPATIBLE`.

Pine cannot query the repository, so the build's own identity is **baked in at generation** and the
*runtime* checks are the ones Pine can actually make: symbol, timeframe and chart timezone against
`supported_context` → `UNSUPPORTED_DATA_CONTEXT`; and available history depth against
`minimum_history_bars` → the warm-up warning. Repo-side staleness is answered by
`check_freshness`, not by the chart.

**The last two lines are not suppressible by any input.** The Phase 0 warning must survive every
cosmetic setting.

## 21.4 Generated enums and reason codes

**Implemented:** `tools/oracle/enums.py`. 26 enums extracted, including `market_state` (6),
`session_key`/`session_label` (6 each), `trade_outcome` (15 scanned), `missed_reason` (12),
`cancel_reason` (6), `ob_final_status` (18 + label map), `policy_regime` (4), `safety_rail` (6),
`intent_action` (4).

`unmapped_against()` makes the rule enforceable: **generation fails when production grows a value
Pine has no mapping for.** Nothing falls back to a generic bucket.

### The one declared divergence, and why

The driver's OB status vocabulary (`run_backtest._status_label`, 18 values) **does not cover the
production-live block outcomes.** `_derive_ob_lifecycle_from_trade` (`run_backtest.py:3072-3167`)
has no branch for `STATE_BLOCKED` / `COHORT_DISABLED` / `REGIME_BLOCKED`, so all three fall through
to `else: status = "unknown"` (L3147-3150).

Measured on the committed golden run
(`golden/run-001/parity-oracle/rehearsal/order_blocks.csv`, 2080 rows):

| `ob_final_status` | count | share |
|---|---|---|
| **`unknown`** | **809** | **38.9 %** |
| `win` | 589 | 28.3 % |
| `loss` | 485 | 23.3 % |
| `news_touch_cancel` | 120 | 5.8 % |
| `pending` | 53 | 2.5 % |
| `news_flatten` | 24 | 1.2 % |

Every one of the 809 `unknown` rows carries `lifecycle_reason = state_target_block`.

So the largest single group of order blocks in production has no status name. Mirroring
`_status_label` verbatim would render 39 % of the chart as UNKNOWN. The oracle therefore adds three
statuses — `state_blocked`, `cohort_disabled`, `regime_blocked` — each recorded in
`contract.oracle_status_extensions` with its justification and its
`driver_status_would_be: "unknown"`, and the HUD shows `lifecycle_reason`, which is populated
correctly in Python.

This is a **declared divergence from the driver vocabulary**, not licence to invent labels anywhere
else. `test_ob_status_vocabulary_does_not_cover_the_block_outcomes` pins it: if production ever
grows proper statuses, that test fails and the extensions are retired in favour of them.

## 21.5 Versioned per-bar trace

**Designed:** `contracts/tradingview_oracle_trace.schema.json` (v1.0.0). One record per 15m
detection bar, with a nested array of the 1m execution bars that changed state.

Every field declares a **comparison class**, which is what makes the tolerance policy mechanical
rather than a judgement call each time:

| Class | Rule |
|---|---|
| `exact` | zero tolerance — any difference fails |
| `price` | within the fixture's price tolerance |
| `normalized` | compared after canonicalisation |
| `feed` | expected to differ between providers; recorded, not failed |
| `python_only` | no Pine counterpart; never compared |
| `pine_unavailable` | Pine cannot compute it; its presence in Pine output is itself an error |

`bar_index` is `python_only` (compare deltas — L-09); `ob_id` is `python_only` (match on
`(detection_time, direction, top, bottom)` — L-05); `block_reasons` is `exact` **including order**,
because production evaluation order is part of the behaviour.

The schema carries `warmup_excluded_bars` in its header so the three history-seeded families (ATR
seed, `swing_trend_bias`, regime panel) are excluded from scoring by construction rather than by
someone remembering.

**The exporter is not built** and, per Phase 0 §17, should **derive** the trace offline from the OB
frame + trades frame + 15m bars rather than instrument the walk. Instrumenting `strategy_core`
would move `engine_manifest_id` and make the deployment refuse to trade — an unacceptable price for
a debugging aid.

## 21.6 Change-impact detection

**Implemented:** `contracts/tradingview_oracle_impact_map.json` + `tools/oracle/impact.py`.

Maps every governed file to `category → contract sections → Pine modules → trace fields → fixtures
→ stages → Python tests`. Worked example:

```
$ python -m tools.oracle.impact --files strategy_core/order_blocks.py
ORACLE IMPACT: ALGORITHM_PARITY_UPDATE
  pine modules : pine/15_volatility, pine/20_swings, pine/30_structure, pine/40_order_blocks
  fixtures     : F-INIT, F-STEADY
  stages       : S2, S3, S4, S5
  python tests : tests/test_strategy_core_slice2_swings.py,
                 tests/test_strategy_core_slice4_order_blocks.py::test_pine_rma_seeding_and_recursion, …
```

`--vs-contract` is the strongest mode: it recomputes all 30 governed digests and compares against
the contract, so it needs no git and catches uncommitted edits.

**Unmapped governed files fail closed** to `FULL_PARITY_REVALIDATION_REQUIRED`.
`test_impact_map_covers_every_governed_file` keeps all 30 mapped — it caught five genuine gaps
during this phase (`src/config.py`, `src/order_blocks.py`, `src/run_outputs.py`,
`strategy_core/__init__.py`, `strategy_core/swings.py`), all now classified.

## 21.7 Generated configuration pipeline

**Designed; generator is Phase 1.** The extraction half is built and working — the contract already
carries the compiled cohort index and effective policy table, which is the hard part. What remains
is serialising them into Pine syntax.

Required generator behaviour, all already enforced on the extraction side:

- loads the canonical resolved configuration **through production's own code path**
  (`load_config_overrides` → `replace(ACTIVE_CONFIG, …)`), never a re-implementation;
- applies the same deployed override rules (`with_disabled_as_label`);
- **fails on unknown fields** (exit 4) and **on missing required fields** (exit 3);
- **fails when an unsupported feature becomes active** (exit 4);
- never silently falls back to defaults;
- deterministic — verified by extracting twice and comparing SHA-256.

**Proposed Phase 1 paths:** `tools/oracle/generate_pine.py`,
`generated/tradingview_oracle.pine` (the single artefact TradingView needs),
`generated/tradingview_oracle_manifest.json`.

TradingView requires one pasteable file, so the generator **assembles** it from structured
fragments under `pine/src/` rather than asking anyone to copy-paste between modules. The assembled
file's hash is recorded in the parity manifest, which is how hand-editing is detected (§21.9).

## 21.8 Structured Pine source and build

Pine v6 libraries cannot carry the mutable cross-module state this oracle needs, and TradingView
ultimately wants one file — so the strategy is **generated single-file assembly** from ordered
fragments:

| Fragment | Contents |
|---|---|
| `pine/src/00_header.pine` | generated identity, fingerprint, banner constants |
| `pine/src/05_generated_config.pine` | generated cohort/policy/enum tables |
| `pine/src/10_data_time.pine` | session table, UTC handling, bar counters |
| `pine/src/15_volatility.pine` | first-value-seeded RMA, parsed prices |
| `pine/src/20_swings.pine` | inlined leg machine |
| `pine/src/30_structure.pine` | BOS/CHoCH, `swing_trend_bias` |
| `pine/src/40_order_blocks.pine` | OB construction, arrays, drawing buffers |
| `pine/src/50_market_state.pine` | daily panel over UTC days |
| `pine/src/60_candidates.pine` | plan, arm, delay |
| `pine/src/70_cohorts.pine` | eligibility + PM gate |
| `pine/src/80_risk_targets.pine` | stop/target/RR |
| `pine/src/90_lifecycle.pine` | fills, exits, transitions |
| `pine/src/95_limitations.pine` | limitation banners, exclusions |
| `pine/src/99_hud.pine` | HUD + debug groups |

Fragments `00` and `05` are **generated**; the rest are hand-written but assembled mechanically.
Both the fragment hashes and the assembled-file hash go into the parity manifest, so a hand-edit to
either layer is detectable.

## 21.9 Parity manifest

**Schema designed:** `contracts/tradingview_oracle_parity_manifest.schema.json`.
**Location:** `artifacts/tradingview_oracle/parity_manifest.json` (committed —
`artifacts/` is not git-ignored; `live_state/` is, which is why the manifest does not live there).

Records: oracle version; the full fingerprint block; production provenance; the Pine source hash
and its fragment hashes; supported context incl. `minimum_history_bars`; **per-stage status**;
derived `global_status`; tested windows; golden-fixture hashes; active limitations with
*expected vs measured* divergence; `unsupported_active_features` (must be empty); enum coverage
(`unmapped` must be empty); and the validation record.

**No manifest exists yet** — there is no Pine build. `check_freshness` correctly reports
`UNVERIFIED`, which is the honest answer.

## 21.10 Stale-oracle detection

**Implemented:** `tools/oracle/check_freshness.py`. Content-based; timestamps are never consulted.

Precedence is deliberate and not alphabetical — the most fundamental divergence wins, because
fixing it may resolve the others:

```
INCOMPATIBLE  >  STALE_TRACE_SCHEMA  >  STALE_ENGINE  >  STALE_CONFIG
              >  STALE_CONTRACT      >  UNVERIFIED    >  PARTIAL  >  CURRENT
```

`INCOMPATIBLE` outranks everything because the oracle would be *wrong*, not merely old.
`STALE_TRACE_SCHEMA` comes next because every fixture is invalid, so no other verdict can be
trusted. Exit codes 0–6 make it CI-usable.

A failed engine pin short-circuits to `INCOMPATIBLE` rather than any "stale" status: if the
deployed engine is not the approved one, nothing downstream can be trusted at all.

## 21.11 Compatibility rules

1. A build is compatible with **exactly one** `oracle_engine_hash`.
2. Config-only changes require regeneration — unchanged algorithms ≠ unchanged output.
3. A newly **active** unsupported feature ⇒ `INCOMPATIBLE`.
4. A newly added but **inactive** feature ⇒ recorded, not blocking.
5. Changed limitation semantics ⇒ explicit register review.
6. Changed trace schema ⇒ all fixtures regenerated.
7. Feed divergence is tolerable; algorithm mismatch never is.
8. **A partial implementation never displays `MATCHED` globally.**

`global_status` is derived, never hand-set: `MATCHED` only when every stage is `MATCHED` *and*
validation is `PASS`.

## 21.12 Strict failure conditions

No silent fallback, no warning-only path for anything compatibility-critical.

| Condition | Detector | Exit |
|---|---|---|
| Engine pin invalid | `engine_access.load_engine` | 2 |
| Ungoverned module loaded (import shadowing) | `verify_loaded_modules` | 2 |
| Golden include-disabled flag flipped | `resolve_config` | 2 |
| Required config field vanished | `extract_contract` | 3 |
| Unsupported feature active / value outside supported set | `extract_contract` | 4 |
| Production enum value with no Pine mapping | `enums.unmapped_against` | generation fails |
| Trace schema moved | `check_freshness` | 5 |
| Generated Pine file hand-edited | `check_freshness` (hash vs manifest) | 6 |
| Non-deterministic output | `test_contract_extraction_is_deterministic` | test fail |
| Governed file with no impact-map entry | `impact.analyse` | fails closed to full revalidation |

## 21.13 Test-ownership matrix

| Subsystem | Python tests | Contract/extraction | Pine fixture | Stage |
|---|---|---|---|---|
| swings | `test_strategy_core_slice2_swings`, `slice4` | impact-map `swing-legs` | F-INIT, F-STEADY | S3 |
| volatility / parsed | `slice4::test_pine_rma_seeding…`, `::test_true_range_first_row…`, `::test_parsed_price_flip…` | `parsed-prices-atr` | F-STEADY | S2 |
| BOS/CHoCH | `slice4::test_duplicate_breaks…` | `structure-bos-choch` | F-INIT | S4 |
| order blocks | `slice4` | `order-block-construction` | F-STEADY | S5 |
| sessions | `slice3_sessions`, `test_session_assignment` *(script-style)* | `sessions` | F-SESSIONS | S1 |
| market state | `test_regime_parity`, `test_regime_config` | `market-state` | F-STATE | S6 |
| entry/fill | `test_delayed_te_fill` *(script-style)* | `entry-trigger-and-fill` | F-STEADY | S7–S8 |
| exits | `test_same_candle_fill_ordering` | `exit-resolution` | F-STEADY | S9 |
| **invalidation** | **none** | `invalidation` | F-STEADY | S8 |
| cohorts | `test_eligibility_policy`, `test_state_target_policy` | `cohort-eligibility` | F-LONDON, F-STATE | S10 |
| portfolio policy | `test_portfolio_policy`, `test_portfolio_execution` | `portfolio-policy` | F-LONDON | S10 |
| boundary | `backend/tests/test_live_slice::test_latest_closed_boundary` | `live-boundary` | F-FRONTIER | S1, S12 |
| oracle tooling | `backend/tests/test_oracle_contract` (29) | — | — | — |

The invalidation gap is unchanged from Phase 0 §16.7 and is still the highest-value missing test.

## 21.14 Phase 0.5 deliverable status

Status as at the end of Phase 0.5, with the Stage-S1 outcome recorded alongside so this
table does not go stale:

| # | Deliverable | End of Phase 0.5 | After Stage S1 |
|---|---|---|---|
| 1 | Parity contract | **implemented** — extracted from live production | unchanged |
| 2 | Engine fingerprint | **implemented** | unchanged (values moved — §22.4) |
| 3 | Generated configuration pipeline | extraction implemented; Pine serialisation designed | **implemented** — `generate_pine.py` |
| 4 | Generated enums / reason codes | **implemented** — 26 enums, 0 manual | + generated session/transition codes |
| 5 | Versioned per-bar trace schema | **designed** (v1.0.0); exporter deferred | **v1.1.0 + exporter implemented** — `export_trace.py` |
| 6 | Change-impact dependency map | **implemented** — all 30 governed files mapped | + S1 fragments, fixtures, `utc-day-key` |
| 7 | Stale-oracle detector | **implemented** | + fragment / fixture / stage-status detection |
| 8 | Structured Pine source/build | **designed** — 14 fragments, generated assembly | **implemented** — 8 S1 fragments, deterministic assembly |
| 9 | Parity manifest | **schema implemented**; no build to record | **first real manifest written** |
| 10 | Update workflow | **documented**; two working commands | six commands; see maintenance §11 |
| 11 | Strict failure conditions | **implemented and tested** | + Pine lint, tamper, context guards |
| 12 | Compatibility status rules | **defined** | **exercised** — S1 UNVERIFIED, global PARTIAL |

---

# 22. Stage S1 implementation record â€” data, time, session

**Status: S1 UNVERIFIED Â· GLOBAL PARTIAL.** The Python side and the static Pine
validation pass in full; the TradingView comparison has **not** been performed, because
TradingView cannot be executed from this VPS. S1 is promoted to `MATCHED` only by
`tools/oracle/verify_s1.py --record PASS` after an operator completes the checklist in
Â§22.9. Nothing in the repository will promote it automatically.

## 22.1 Production semantics mirrored (re-verified by execution)

Every statement below was **executed** against the pinned engine during S1, not taken
from Â§3â€“Â§6 prose. Three corrections and one new constraint came out of it (Â§22.10).

| Property | Measured value | Evidence |
|---|---|---|
| Timestamp dtype after normalisation | `datetime64[us, UTC]` â€” tz-aware, microsecond | `prepare_candles_for_simulation` |
| Bar label | **bar-OPEN**; pandas resample is left-labelled, left-closed | `src/resample.py` |
| Session input | the **UTC hour** of the bar-open timestamp, nothing else | `sessions.py:33-38` |
| Session window | `start <= hour < end` â€” inclusive start, **exclusive end** | verified bar-by-bar |
| 07:00 / 06:59 | `london` / `asia` | `F-S1-BOUNDARIES` |
| 17:00 | `outside` (fallback) | `F-S1-BOUNDARIES` |
| DST | **none anywhere** â€” `sessions.py` contains no `tz_convert`, `tz_localize`, `zoneinfo`, `pytz` or `astimezone` | token scan + 4 DST fixtures |
| UTC day | `strategy_core.regime.utc_date_key` â€” UTC **calendar** day | `F-S1-MIDNIGHT` |
| Daily aggregation | `resample("1D")` over UTC timestamps | `run_backtest.py:2430` |
| Missing intervals | **dropped** by `dropna`, never filled | `F-S1-GAP` |
| Duplicate timestamps | **preserved** â€” production sorts but does not de-duplicate | `F-S1-DUPLICATE` |
| Out-of-order rows | silently re-sorted, **stable** | `F-S1-OUTOFORDER` |
| Bar completeness | only **closed** bars ever enter production | `live/mt5_bridge.py` |
| Canonical bar schema | `time,open,high,low,close,volume`, `%Y-%m-%d %H:%M:%S+00:00` | frozen dataset header |

## 22.2 New constraint discovered: the timeframe must divide 60

A session window is `[start, end)` on the **hour**, so a bar may be assigned exactly one
session only if it cannot span an hour boundary. Measured against the production
schedule over a full day:

| Timeframe | Bars/day | Straddles a session boundary? |
|---|---|---|
| M1 / M5 / M15 / H1 | 1440 / 288 / 96 / 24 | **no** |
| **H4** | 6 | **yes â€” 4 bars/day** |

H4 and above are therefore **refused** as an unsupported data context, even though the
engine would happily consume the candles. Enforced in `export_trace._tf_minutes` and in
the Pine guard; pinned by `test_every_supported_timeframe_divides_sixty`.

## 22.3 Files created

| Path | Role |
|---|---|
| `tools/oracle/export_trace.py` | offline S1 trace exporter (pure adapter) |
| `tools/oracle/session_codes.py` | contract-derived numeric codes for Pine plotting |
| `tools/oracle/build_fixtures.py` | golden fixtures cut from the frozen dataset |
| `tools/oracle/generate_pine.py` | deterministic generation + assembly + manifest |
| `tools/oracle/lint_pine.py` | static Pine validation |
| `tools/oracle/verify_s1.py` | S1 verification + TradingView checklist |
| `tools/oracle/compare_tv_export.py` | mechanical diff of a TradingView chart-data export vs the trace |
| `pine/src/*.pinefrag` (8) | hand-written fragments |
| `pine/generated/tradingview_visual_oracle.pine` | **generated** single-file artefact |
| `golden/tradingview_oracle/s1/` | 11 fixtures + `INDEX.json` |
| `artifacts/tradingview_oracle/parity_manifest.json` | first real parity manifest |
| `artifacts/tradingview_oracle/s1_tv_checklist.md` | operator verification checklist |
| `backend/tests/test_oracle_s1.py` | 69 S1 tests |

## 22.4 Trace schema decision â€” 1.0.0 to 1.1.0

S1 implements four of eleven per-bar sections. The v1.0.0 schema made `structure`
**required**, which would have forced every S1 row to carry fabricated structure
placeholders â€” indistinguishable from implemented parity, and precisely the failure this
design exists to prevent.

The schema was therefore bumped to **1.1.0**: `header.stage` and
`header.sections_implemented` were added, `detection_bar.structure` became optional, and
`utc_day` / `bar_completeness` / `data_context` / session-transition fields were added.
A section that is not implemented is **absent** â€” not null, not zero â€” and a comparator
must ignore anything outside `sections_implemented`.

The bump propagated exactly as designed: the fingerprint moved from `â€¦_t1.0.0_g0.1.0` to
`â€¦_t1.1.0_g0.2.0`, which is the intended signal.

## 22.5 Engine and configuration fingerprint embedded

```
lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.1.0_g0.2.0
08344d7e7412507f38e357aff29cba64552f72827473e968d79e0d0da96f6639
```

Baked into the Pine header and the `05_generated_contract` constants, and cross-checked
by `verify_s1` â€” which reads the constants back **out** of the emitted `.pine` text and
compares them to the trace header, so a chart and a trace from different contracts cannot
be silently compared.

`ORACLE_GENERATED_AT` is derived from the **contract**, not the wall clock. A wall-clock
stamp would make every regeneration a different file and destroy the tamper check;
volatile metadata lives in the manifest, which is not content-addressed.

## 22.6 Parity output mechanism

Pine cannot write files, so ten values are plotted to `display.data_window`, each with an
exact counterpart in the trace: `oracleBarEpochMs`, `oracleSessionCode`, `oracleDayKey`,
`oracleTransCode`, `oracleUtcHour`, `oracleCtxCode`, `oracleBarsSinceTrans`,
`oracleSessIsFallback`, `oracleBarClosed`, `oracleBuildCode`. All are **numeric** â€”
comparing background colours by eye is not verification. An `alertcondition` emits a
pipe-delimited payload on every transition for bulk export.

Session and transition codes are **derived from the contract**, not hand-assigned, so
they cannot drift from production. `CODE_UNKNOWN = -1` renders visibly rather than
defaulting.

## 22.7 Pine static validation

`tools/oracle/lint_pine.py` â€” **0 errors, 0 warnings** on the 614-line artefact. Each
rule encodes a mistake that would silently corrupt parity here, and each was
**negative-tested** to prove it fires:

`global_array_push` Â· `exchange_day` (`request.security(â€¦,"D",â€¦)` / `timeframe.change("D")`)
Â· `implicit_timezone` Â· `window_inclusivity` Â· `warning_suppressible` Â· `absolute_path` Â·
`wallclock_stamp` Â· `stage_scope` Â· `not_a_strategy` Â· `unclosed_bracket`

Three defects were found and fixed by this process:

1. **`array.push` at global scope** re-executes on every bar, so `var` arrays would grow
   without bound and every index lookup would drift. Switched to `array.from()` inside
   the `var` initialiser.
2. **The `exchange_day` and `wallclock_stamp` rules never fired** â€” the linter blanked
   string literals before matching, so the `"D"` it was hunting for had already been
   erased. Fixed by running literal-sensitive rules against a comments-only-stripped
   view. Found by negative-testing the linter itself.
3. **`prevSessKey` was read after being overwritten** â€” the visuals fragment ran after
   the session fragment's state carry, so every session-close label would have shown the
   *current* session. Fixed with explicit `dispPrev*` snapshots taken before the carry.

## 22.8 Golden fixtures

11 fixtures, all cut from the real frozen dataset
(`EURUSD_1m_extended_2015_2026.csv`), byte-identical on rebuild:

| Fixture | Rows | Pins |
|---|---|---|
| `F-S1-DAY` | 1438 | a full UTC day â€” every boundary, the fallback, the rollover |
| `F-S1-BOUNDARIES` | 18 | HH:59 / HH:00 / HH:01 at all six boundaries |
| `F-S1-MIDNIGHT` | 119 | UTC day-key rollover |
| `F-S1-WEEKEND` | 956 | the ~48 h weekend gap |
| `F-S1-DST-US` | 300 | US DST start 2026-03-08 (the broker's measured shift date) |
| `F-S1-DST-EU` | 286 | EU DST start 2026-03-29 |
| `F-S1-DST-END-US` | 240 | US DST end 2025-11-02 â€” the repeated local hour |
| `F-S1-DST-END-EU` | 296 | EU DST end 2025-10-26 |
| `F-S1-GAP` | 1348 | a 90-minute hole â€” proves intervals are dropped |
| `F-S1-DUPLICATE` | 61 | a duplicated timestamp |
| `F-S1-OUTOFORDER` | 60 | deterministically shuffled rows |

Plus three **unsupported-context** cases that must be refused before any bar is read:
`GBPUSD/15min`, `EURUSD/H4`, `EURUSD/1440min`.

DST-end fixtures use **2025** dates: the frozen dataset ends 2026-06-19, so the autumn
2026 transitions have no candles. Using in-range dates keeps them real rather than
synthetic.

## 22.9 Parity results

**Python side â€” 11/11 fixtures PASS**, plus 3/3 unsupported-context refusals and 4/4
Pine-to-trace cross-checks.

| Field | Match type | Tolerance | Fixtures | Python result | TradingView result |
|---|---|---|---|---|---|
| normalized UTC timestamp | exact | 0 | 11 | **PASS** | *not run* |
| UTC day key | exact | 0 | 2 | **PASS** | *not run* |
| session ID / key | exact | 0 | 8 | **PASS** | *not run* |
| session code | exact | 0 | 11 | **PASS** | *not run* |
| session active / fallback | exact | 0 | 11 | **PASS** | *not run* |
| session transition | exact | 0 | 3 | **PASS** | *not run* |
| transition reason | exact | 0 | 3 | **PASS** | *not run* |
| bars since transition | exact | 0 | 11 | **PASS** | *not run* |
| symbol context | exact | 0 | 3 | **PASS** | *not run* |
| timeframe context | exact | 0 | 3 | **PASS** | *not run* |

**The TradingView column is empty and must stay empty until the comparison is actually
performed.** S1 parity is *half* proven: the Python half. The checklist at
`artifacts/tradingview_oracle/s1_tv_checklist.md` lists the 18 boundary bars and 6
transition bars with the exact Data Window integers to confirm, plus the three
unsupported-context charts.

## 22.10 Corrections to earlier phases

| Correction | Was | Now |
|---|---|---|
| Weekend gaps | assumed a `gap_resync` transition | **No transition at all.** The FX week closes ~Fri 21:45 UTC and reopens ~Sun 22:00 UTC â€” *both inside the same `outside` window* â€” so the ~48 h gap is invisible to session logic. A Pine build emitting a weekend "session close" would diverge. |
| Supported timeframes | not constrained | must **divide 60**; H4 straddles 4 bars/day |
| Trace schema | `structure` required | optional; sections are stage-scoped |

## 22.11 Limitations registered for S1

| ID | Label | Impact on S1 |
|---|---|---|
| L-02 | `FEED-DEPENDENT` | TradingView builds its own bars; a missing/extra bar shifts `bar_index` but must not change any session code for a bar present in both |
| L-06 | `UTC-DAY RECONSTRUCTED` | day must come from the bar timestamp; `request.security(â€¦,"D",â€¦)` is rejected by the linter |
| L-09 | `INDEX-LOCAL` | never compare `bar_index`; the checklist matches on UTC timestamp |
| L-12 | `EXECUTION-STATE UNKNOWN` | no broker/ledger state in Pine |
| L-15 | `SHADOW MODE` | node is dry-run |
| **L-19** | `BUILD-TIME IDENTITY` | *new* â€” Pine cannot query the repo, so the HUD reports what the build **mirrors**; freshness is a repo-side answer only |
| **L-20** | `MANUAL-COMPARISON` | *new* â€” TradingView has no headless run path, so S1 parity needs an operator; this is why S1 is UNVERIFIED |

## 22.12 Verification attempt of 2026-08-03 — not completed

A TradingView verification was attempted from the VPS and **could not be completed**.
Recorded here so the reason is evidence, not memory:

| Route | Outcome |
|---|---|
| VPS browser pane -> tradingview.com | loads, but **anonymous** (`sessionid` cookie absent) |
| Anonymous `/pine/` editor | editor renders and reports "Pine Script v6", but there is **no "Add to chart"** control |
| Operator's connected Chrome (macOS) | **logged in** — a personal saved layout loaded — but the standalone `/pine/` editor still exposes no "Add to chart"; that control lives only in the chart's bottom panel |
| Clipboard paste of the 34 KB artefact into Monaco | rejected — the browser pane does not share the OS clipboard |

Logging in was **not** attempted: entering a password is outside what this agent may
do. Nothing was saved to the operator's TradingView account, and the browser was
returned to the layout it started on.

The blocking requirement is not the compile — it is that the per-bar integers live in
the **Data Window**, which only exists once the indicator is on a chart, and whose
values are canvas-rendered rather than readable from the DOM. Reading ~24 of them by
automation would be guesswork, and a misread recorded as PASS is precisely the false
`MATCHED` this apparatus exists to prevent.

**S1 therefore remains UNVERIFIED.** No result was recorded.

## 22.13 What changed as a result: evidence-gated recording

Rather than leave the operator with a 24-row hand-transcription task, the manual step
was removed from the critical path:

* **`tools/oracle/compare_tv_export.py`** ingests TradingView's *Export chart data* CSV,
  joins it to the Python trace on the UTC bar timestamp, and diffs every oracle plot
  exactly. Bars present on only one side are classified `FEED_DIFFERENCE` (L-02) and do
  not fail; a bar present on both whose values disagree does. An export missing the
  oracle columns is refused rather than silently skipped.
* Self-tested against a synthesised export: it passes a faithful one, and catches both
  a single wrong session code (located at 07:00, the London open) and a one-bar shift
  (135 mismatches) — the two defects most likely to occur.
* **`verify_s1 --record PASS` is now evidence-gated.** It refuses without `--feed`,
  without `--compiled PASS`, and without passing comparator reports for **both**
  `F-S1-DAY` and `F-S1-BOUNDARIES` whose build fingerprint matches the manifest. A
  refused record leaves the manifest byte-identical. Verified at every level.

The operator's remaining work is: paste, compile, export twice, run the comparator,
record. The numeric comparison is no longer a reading exercise.

## 22.12 Criteria for beginning S2

S2 (volatility / parsed prices) may begin when:

1. the S1 TradingView checklist is completed and recorded with `--record PASS`;
2. `check_freshness` reports `PARTIAL` with `S1: MATCHED`;
3. Pine is regenerated so the embedded `ORACLE_S1_STATUS` reads `MATCHED`;
4. Â§19.5's ATR-seed evidence is carried into the S2 fragment design â€” the RMA is
   **first-value seeded**, not `ta.rma` / `ta.atr`.

S2 may be **developed** before step 1 completes, but no S2 parity claim can be made while
its foundation is unverified: every S2 value is computed over the bar series S1 defines.

---

# 23. S1–S4 verification against a real TradingView export (2026-08-04)

The first verification cycle run against a genuine `OANDA:EURUSD` 15m export. It
produced a **conclusive result that the original method could not have reached**,
and it invalidates one assumption this document has carried since §22.

## 23.1 What was verified

| | |
|---|---|
| Feed | `OANDA:EURUSD`, 15m, standard candles, UTC |
| Export | `TV_ORACLE_OANDA_EURUSD_15M_2025-09-30_TO_2025-12-31.csv` |
| Export sha256 | `099f7ae84a73fa8488a2f289dc2d71dd5cf164ace2c4e3a6d1daa2a20e68e158` |
| Rows / columns | 20,919 / 48 — 0 malformed, 0 duplicate timestamps |
| Pine source sha256 | `92492fcebb222503036f61359924bfade26d031782707d2b3087f0582b68037f` (unmoved) |
| Fingerprint | `lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.2.0_g0.5.1` |
| Compilation | PASS (operator-reported) |
| Mandatory columns | S1 7/7, S2 7/7, S3 12/12, S4 5/5 — all present |

The chart's earliest bar is **2025-09-30 21:00**, not the fixture's 00:00: OANDA's
15m history begins at the 21:00 trading-day open. The two sides therefore seed
84 bars apart, which the analysis below accounts for explicitly.

## 23.2 The finding that changes the method

**The production dataset and the TradingView OANDA feed are different price
series.** Measured over the 6,240 shared bars:

| Field | bit-exact | median (tv − py) | mean | max |
|---|---|---|---|---|
| open | 12.4% | +2.0e-5 | +0.243 pip | 6.5e-4 |
| high | 6.9% | +2.0e-5 | +0.237 pip | 6.5e-4 |
| low | 10.9% | +2.0e-5 | +0.249 pip | 7.6e-4 |
| close | 10.1% | +2.0e-5 | +0.252 pip | 7.5e-4 |

TradingView quotes **higher on ~87% of bars** while the mean bar *range* is
identical (5.427 vs 5.439 pip, ratio 0.998). That is the signature of **bid vs
mid**: `LUX_ROOT/data/candles/EURUSD_1m_extended_2015_2026.csv` is bid, the OANDA
chart is mid. No constant correction reconciles them — the best single shift
(1.6e-5) explains only 29.6% of bars, because the spread varies intraday and
blows out at the 22:00 rollover, which is exactly where the 5e-4+ outliers sit.

**Consequence.** S3's price tolerance is 1e-6. The feed's own noise floor is
2e-5 — *twenty times looser than the tolerance*. Comparing Pine-on-OANDA against
Python-on-production-data therefore measures the **feed**, not the Pine, and can
never pass for any price-derived stage. This is limitation **L-02 escalating from
a footnote to a hard constraint**, and §22's claim that a matched start makes
"every field match exactly" is **false on any feed but production's own**.

## 23.3 Result — production-data basis (`--input-basis fixture_bars`)

Strict, `--warmup-bars 0`. Report `artifacts/tradingview_oracle/s1_s4_compare.json`
(sha256 `92d5401a15d73fe17f38d74aef51f34b3c6ade169528412adb85854959d3fea2`).

```
[FAIL] S1  6240 bars   first divergence 2025-09-30 21:00 oracleTransCode py=0 pine=4
[FAIL] S2  6240 bars   first divergence 2025-09-30 21:00 oracleTrueRange py=2.50e-4 pine=3.10e-4
[FAIL] S3  6240 bars   first divergence 2025-09-30 21:00 oracleS3Ready   py=1 pine=0
[FAIL] S4  6240 bars   first divergence 2025-10-01 06:30 oracleBias      py=1 pine=0
```

S1 is price-independent, so its divergences are diagnostic. It agrees on
**6,220 / 6,240 bars**, and `oracleSessionCode`, `oracleUtcHour`, `oracleDayKey`,
`oracleSessIsFallback` and `oracleCtxCode` are exact on **all 6,240**. The 20 bad
bars form exactly two runs, both structural, neither a Pine defect:

1. **12 bars, 2025-09-30 21:00–23:45** — chart start. Pine's first bar declares a
   transition (code 4) and resets `bars_since_transition` to 0; Python is mid-series
   at 16. Converges at the next real transition. `HISTORY_SEED`.
2. **8 bars, 2025-12-25 22:00–23:45** — the OANDA feed is **missing 36 bars**
   (2025-12-24 22:00 → 2025-12-25 07:45, Christmas). Python resets at the 22:00
   transition; TradingView's previous bar is 2025-12-24 21:45, same session, so no
   transition fires and its counter runs on. `FEED_DIFFERENCE`.

Run 2 matters beyond itself: **excluding one-sided bars does not contain their
effect.** The 36 absent bars are excluded from scoring, yet they still corrupt a
stateful counter on 8 bars that *are* shared. L-02's "excluded, not failed" rule
is necessary but not sufficient for any stateful field.

## 23.4 Result — chart-bars basis (`--input-basis chart_bars`)

The export carries OANDA's own OHLC, so the Python engine can be re-run over the
**exact bars Pine saw**, removing the feed as a variable. This mode was added to
`compare_stages` for this purpose and is covered by four tests.

Strict, no exclusion — `s1_s4_chart_bars_strict.json`:

```
[PASS] S1  6240 bars        0 divergences in 43,680 field comparisons
[PASS] S2  6240 bars        0 divergences in 43,680 field comparisons
[FAIL] S3  6190 bars        first divergence 2025-10-01 09:30 (bar 50)
[FAIL] S4  6190 bars        first divergence 2025-10-01 09:45 (bar 51)
```

**S1 and S2 are exact on every bar with no exclusion whatsoever** — including
`oracleTrueRange`, `oracleAtr`, `oracleVolMeasure`, `oracleParsedHigh/Low`,
`oracleVolFlip` and `oracleAtrWarm`. The first-value-seeded RMA of §19.5 is
confirmed correct against a real chart.

Every S3/S4 divergence is confined to **bars 50–205**:

| field | bad | first | last | clean after |
|---|---|---|---|---|
| oracleHasSwingHigh / oracleSwingHigh | 118 | bar 88 | bar 205 | 6,034 bars |
| oracleHasSwingLow / oracleSwingLow | 78 | bar 50 | bar 127 | 6,112 bars |
| oracleSwingLowCrossed | 77 | bar 51 | bar 127 | 6,112 bars |
| oracleCurrentLeg | 38 | bar 50 | bar 87 | 6,152 bars |
| oracleBias / oracleTrend | 113 | bar 51 | bar 163 | 6,076 bars |
| oracleStructEvent / Count / BrokenLevel | 1 each | bar 51 | bar 51 | 6,188 bars |
| oracleS3Ready, oracleSwingHighCrossed | **0** | — | — | all |

After bar 205: **0 divergences in 6,034 bars for S3 and S4** — every decision
exact, every price inside 1e-6, for ~63 days. That is L-04 (`swing_trend_bias`
seeded by the first break in loaded history) and the 50-bar swing warm-up,
converging and never recurring.

With the seed declared — `--warmup-bars 206`, report
`s1_s4_chart_bars_seed206.json` — **S1, S2, S3 and S4 all PASS over 6,034 bars.**

## 23.5 Classification of every discrepancy

| # | Discrepancy | Class | Evidence |
|---|---|---|---|
| 1 | S1 `oracleTransCode`/`BarsSinceTrans`, 12 bars at chart start | `HISTORY_SEED` | Pine bar 0 has no predecessor; converges at the next transition |
| 2 | S1 same fields, 8 bars on 2025-12-25 | `FEED_DIFFERENCE` | 36 bars absent from OANDA over the holiday |
| 3 | S2 `oracleTrueRange`, 261 bars | `FEED_DIFFERENCE` | 0 failures on all 8 bars with bit-identical inputs; 0 failures over 6,240 bars on the chart-bars basis |
| 4 | S2 `oracleAtr`/`VolMeasure`, 346 bars, last 2025-10-06 | `HISTORY_SEED` | converges then exact for 5,894 bars; 0 on chart-bars basis |
| 5 | S2 `oracleAtrWarm`, 84 bars | `HISTORY_SEED` | exactly the 84-bar start offset |
| 6 | S2 parsed prices + `oracleVolFlip` | `FEED_DIFFERENCE` | knife-edge threshold under a 0.24-pip shift; 0 on chart-bars basis |
| 7 | S3 swing levels, 5,986 bars | `FEED_DIFFERENCE` | diff median = 2e-5 = the feed noise floor; only 4.1% inside the 1e-6 tolerance |
| 8 | S3/S4 residual, bars 50–205, chart-bars basis | `HISTORY_SEED` | L-04 + 50-bar warm-up; 0 divergences thereafter |
| 9 | S4 all fields, production basis | `INVALIDATED` | S3 did not pass; S4 is computed on S3's output |
| 10 | `--out` crash after writing the report | `TOOLING_DEFECT` (fixed) | `relative_to` raised for any path outside the repo, *after* the file was written |

**No `PINE_LOGIC` defect was found in S1, S2, S3 or S4.**

## 23.6 Recorded status

`verify_s1 --record FAIL` was recorded against the production-data comparison,
which is the honest result for the question *"will this chart show production's
numbers?"* — it will not, and no amount of Pine work will change that.

`check_freshness` reports `UNVERIFIED` / global `PARTIAL`. Nothing was promoted to
`MATCHED`: the chart-bars evidence answers a **narrower** question than the parity
contract's `MATCHED` currently means, and silently widening it would be exactly
the kind of quiet redefinition §21 exists to prevent. Resolving that is a contract
decision, not a verification one — see 23.7.

## 23.7 What this means for the contract, and for S5

The oracle can be **behaviourally faithful** but not **numerically identical** on
any feed other than production's own. Two coherent options:

* **A — split the status.** `LOGIC_MATCHED` (chart-bars, proven for S1–S4) and
  `FEED_MATCHED` (production basis, unattainable on OANDA). Honest, and makes the
  limitation permanent and visible rather than a recurring surprise.
* **B — source a bid feed.** If a TradingView feed quoting the same bid series
  exists, the production basis becomes attainable. Unverified, and outside the
  scope of this cycle.

**S5 is not blocked by Pine.** S1–S4 are proven faithful, so the foundation S5
would build on is sound. S5 *is* blocked by the contract question above: order
blocks are anchored to parsed prices and swing levels, so on an OANDA chart some
blocks will sit at different prices and a minority will not form at all — S5 would
reproduce this same unverifiable outcome one layer deeper, and with `ob_id`
(L-05) and the 38.9% `unknown` final-status population (§20.4) on top.

Decide A or B before S5 begins.

---

# 24. Parity status split, and Stage S5 (2026-08-05)

Wave 1 established that "is this faithful?" has three different answers with
three different kinds of evidence. §24.1–24.4 replace the single overloaded
status with a model that can say all three; §24.5–24.8 implement S5 on top of it.

## 24.1 Why one status could not work

The v1 manifest carried one string per stage
(`UNIMPLEMENTED` / `IMPLEMENTED_UNVERIFIED` / `UNVERIFIED` / `MATCHED` /
`FAILING`). §23 produced a build for which **no value in that vocabulary was
true**: S1–S4 are faithful ports, and the chart cannot show production's numbers.
`MATCHED` would have claimed the second; `FAILING` would have blamed the Pine for
OANDA's quoting convention.

Three questions, three answers, three sources of evidence:

| Dimension | Question | Established by | Can a Pine defect move it? |
|---|---|---|---|
| **Logic parity** | given the SAME bars and the same declared initialisation, does Pine reproduce the Python algorithm? | a `chart_bars` comparison | **yes — only this one** |
| **Feed parity** | does the charted feed deliver production's bars? | `measure_feed` over raw OHLC | no |
| **Production replay parity** | does the chart reproduce the exact historical production trace? | a `fixture_bars` comparison | no (presupposes both above) |

Plus **implementation status** — "does this build contain the stage at all" —
which is a property of the artefact, never carried across a regeneration.

`tools/oracle/parity_status.py` is the single authority. It enforces the rules
that keep the dimensions from re-merging:

* a matched logic status **must** carry `input_basis = chart_bars`; a
  `fixture_bars` report is refused, because its divergences are ambiguous;
* `LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP` **must** carry a positive
  `bootstrap_bars` and a window — an undeclared bootstrap is what the status
  exists to make visible;
* `REPLAY_MATCHED` is refused while feed parity is anything but `FEED_MATCHED`.

## 24.2 Fail-closed migration

A v1 manifest migrates in memory on every read and on disk on the next write. A
legacy `MATCHED` is **not** promoted: it was recorded under a comparison that
could not separate the three questions, so it answers none of them. It becomes
`LOGIC_UNVERIFIED` with the discarded claim preserved in `migrated_from`.
`UNIMPLEMENTED` is the only value that survives intact. An unrecognised schema
string is refused outright rather than best-effort parsed — a manifest is the
sole authority on whether a chart is current, and guessing at one written by a
foreign tool would produce a confident answer from a document we do not
understand.

## 24.3 Two defects this work exposed

**Regeneration destroyed evidence.** `build_manifest` rebuilt `stages` from a
hard-coded map and reset `validation` and `tested_windows`, while
`verify_s1 --record` printed, on success, *"now regenerate Pine so the embedded
status matches"*. Following the documented workflow silently deleted the record
that had just been made — and it did, in this session, to a `FAILING` record
made an hour earlier. Fixed by `carry_forward_evidence`, and generation now
resolves the stage records **once** so the embedded Pine constants and the
written manifest agree within a single run.

**A test was writing to the live engine.** `test_engine_identity.py` mutated
`LUX_ROOT/strategy_core/execution.py` on every suite run and restored it in a
`finally`. Byte identity was asserted, so `git status` stayed clean and the
digests always matched — which is exactly why it survived so long. It was not
harmless: for the duration of the write the live engine's source on disk was
corrupt, and `live/main.py`'s startup `verify_engine()` would have **refused to
start the trading node** had it restarted in that window. Under
`-n 2 --dist loadscope` it also raced a concurrent worker asserting engine
identity against the same tree. The mutation now happens on a mirrored governed
tree in `tmp_path`.

The guard that should have caught it scanned only `tools/oracle/*.py`, required
the write verb and the lowercase token `lux_root` **on the same line**, and
matched only a hard-coded absolute path. The offender was in `backend/tests/`,
bound `LUX_ROOT / …` on one line and wrote six lines later. The replacement does
AST taint analysis scoped per function, anchored on the `Lux-OB-Backtester`
literal (the only unambiguous marker — tests legitimately point `cfg.lux_root` at
synthetic trees), and is itself negative-tested against the original defect.

## 24.4 What the operator now sees

```
GLOBAL: PARTIAL
S1   IMPLEMENTATION: IMPLEMENTED   LOGIC: LOGIC_MATCHED
     FEED: FEED_DIFFERENT          PRODUCTION REPLAY: REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT
S2   … as S1
S3   LOGIC: LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP   BOOTSTRAP: 206 bars
S4   … as S3
S5   LOGIC: LOGIC_UNVERIFIED
S6–S14: UNIMPLEMENTED
```

The HUD carries a compact version of the same split, and states the cause
plainly — `FEED BASIS DIFFERENT: PROD BID / CHART MID` — so a reader cannot take
the green logic rows as "this chart shows production's numbers".

`UNVERIFIED` is reserved for a build with **no** evidence in any dimension. A
newly-implemented stage awaiting its first export reports `PARTIAL`: dragging the
headline to "nothing is verified" while four stages carry real evidence is as
misleading as the overclaim the model exists to prevent.

**Evidence is bound to the build it measured.** Each recorded dimension stores
the `build_fingerprint` it was gathered under, and freshness reports
`stage_evidence_predates_build` when that no longer matches. Landing S5 bumped
the trace schema to 1.3.0 and therefore moved the fingerprint, so this finding is
active now: the S1–S4 evidence is still the best available, and it is no longer
allowed to pass unremarked.

## 24.5 The S5 entry gate

The old gate — "S1–S4 must all be MATCHED" — is unsatisfiable by construction,
because MATCHED conflated feed parity and feed parity is unattainable on the
available chart. The replacement gates on the dimension a Pine defect can move:

```
GATE S5  depends on ['S1', 'S2', 'S3', 'S4']
  S1: logic=LOGIC_MATCHED                          basis=chart_bars
  S2: logic=LOGIC_MATCHED                          basis=chart_bars
  S3: logic=LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP  bootstrap=206  basis=chart_bars
  S4: logic=LOGIC_MATCHED_WITH_DECLARED_BOOTSTRAP  bootstrap=206  basis=chart_bars
RESULT: OPEN
```

It additionally refuses any stage claiming replay parity without feed parity, and
any dependency whose bootstrap is declared without a size or a window.

## 24.6 S5 — what production actually does

From `strategy_core/order_blocks.py:127-222`, read from the code:

1. A break (S4) triggers a candidate. The swing is marked `crossed` and the bias
   flips **before** the block is built — so a size-rejected block still burns its
   swing and still flips the bias. **Rejection is not a no-op.**
2. The origin bar is the extreme **parsed** price over `[pivot_index,
   detection_index-1]` — `.loc` is inclusive at both ends, and
   `search[search == search.min()].index[0]` takes the **earliest** bar on a tie.
3. Bounds are `origin.parsed_high` / `origin.parsed_low`. On a high-volatility
   origin those are **swapped**, so `top < bottom`. The inverted box is
   production behaviour and is not normalised.
4. The size gate is `abs(top-bottom)/pip_size`, rejected when `< min` or
   `> max`. Both comparisons are **strict**, so the gate is **inclusive at both
   boundaries**: a width of exactly 100.0 pips **passes**. `min = 0.0` and width
   is non-negative, so the min gate can never reject.
5. `ob_id` is written into the dict **before** the gate, but the counter
   increments **only inside the accept branch** — so a rejected block consumes no
   id and the next accepted one reuses the integer. `ob_id` is dense `1..N` over
   accepted blocks.
6. Bullish is evaluated first and the two branches are independent `if`s, so both
   can fire on one bar. The bull break sets the bias that the bear branch then
   reads, so a same-bar bearish block can be tagged `CHoCH` from a bias that did
   not exist when the bar opened.
7. **No lifecycle state is assigned at creation.** There is no status, active,
   mitigated or invalidated field; membership in the list *is* admission. So
   creation is cleanly separable, and S5 stops there.

Config, resolved: `swing_length 50`, `ob_filter "Atr"`, `pip_size 0.0001`,
`min_ob_size_pips 0.0`, `max_ob_size_pips 100.0`. The ATR length (200) and the
volatility multiple (2.0) are **hardcoded in production**, not configurable.

## 24.7 The canonical reference

`tools/oracle/replay_structure.py` now performs the whole creation path and is
compared field-for-field against `detect_order_blocks`:

> **2,080 of 2,080 order blocks reproduced exactly** over the full production
> dataset — 4,271,083 M1 rows → **285,790 detection bars**, 2015-01-01 to the
> frontier — matching on `ob_id`, `direction`, `structure_tag`, `top`, `bottom`,
> `width_pips`, `break_level`, `pivot_index`, `origin_index` and
> `detection_index`, with exact float equality on the prices.

This supersedes the earlier, weaker claim of *350/350 over 49,898 bars*, which
compared only tag, pivot and break level and stopped short of the box, the gate
and the id. The check also asserts **cardinality** (a dropped or invented block
would otherwise still match on every row it emitted) and **id density**
(`1..N`, which is the observable consequence of the counter incrementing only on
accept).

A fixture-scale version runs in the suite; the full-dataset sweep takes ~3.5
minutes and is run on demand.

## 24.8 S5 in Pine, and what is deliberately absent

`pine/src/65_order_blocks.pinefrag`. The origin scan is the hard part: the window
starts at a pivot an **unbounded** number of bars back, so `ta.lowest` cannot
express it. It is accumulated — seeded on the swing-creation bar by walking the
`swing_length` offsets oldest-first with a strict comparison (preserving the
earliest-tie rule), then folded forward one bar at a time, always folding in
`[1]` because the window is `[pivot, break-1]` and excludes the current bar.

Drawn: order-block boxes anchored at the origin bar, bull/bear distinct, the
`ob_id` and tag on the box, and in debug mode a marker for a size-rejected
candidate with its width. Retention is bounded like every other drawing object.

**Not implemented, deliberately:** mitigation, invalidation, expiry, retirement,
entries, stops, targets, market-state eligibility, trade lifecycle. A test
asserts the generated Pine contains none of them.

**L-05 stands and is unavoidable.** Production's `ob_id` counts from 2015; a
chart's counts from the first loaded bar. They will not match, and the comparator
therefore does **not** compare `ob_id` — it compares the *sequence* via
`oracleObActive`, which only advances on an append and so encodes the property
that actually matters. Match a block on `(detection_time, direction, top,
bottom)`.

## 24.9 Known gaps

* **S5 logic parity is UNVERIFIED.** The Pine compiles statically and mirrors a
  reference proven exact against production, but it has never been on a chart. It
  will stay `LOGIC_UNVERIFIED` until a `chart_bars` export is compared. Nothing
  promotes it in the meantime.
* **No dedicated S5 edge-case fixtures.** The existing fixtures exercise S5
  (41 / 12 / 39 blocks) but contain **no size-gate rejection and no inverted
  box** — those paths are covered by unit tests against the gate and the origin
  scan, not by an end-to-end fixture. Building fixtures that contain them is
  outstanding.
* ~~**A same-bar bull+bear pair is order-fragile downstream.**~~ **WITHDRAWN —
  see §25.1.** A same-bar pair is not merely rare, it is UNREACHABLE: the two
  break conditions are mutually exclusive. All 2,080 production order blocks
  carry distinct `detection_time` values, so the unstable sort has no ties to
  lose.

---

# 25. Closing the S5 gaps (2026-08-05)

§24.9 listed three open items. Two are now closed, and the third turns out not to
have been a hazard at all.

## 25.1 CORRECTION — the same-bar ordering hazard does not exist

§24.9 recorded that "a same-bar bull+bear pair is order-fragile downstream"
because `prepare_order_blocks_for_simulation` sorts by `detection_time` with
pandas' unstable quicksort. **That was wrong**, and the correction is stronger
than the original claim: a same-bar pair is not rare, it is **unreachable**.

The two break conditions are mutually exclusive:

```
bull needs   prev_close <= SH   and   close >  SH
bear needs   prev_close >= SL   and   close <  SL
both     =>  SL <= prev_close <= SH        =>  SL <= SH
             close > SH >= SL              =>  close > SL
             but bear needs close < SL     =>  CONTRADICTION
```

There is no ordering of the swing levels that satisfies both on one bar. Three
independent confirmations:

| Check | Result |
|---|---|
| bars carrying >1 structure event, 285,790 detection bars | **0** |
| smallest gap between two OPPOSITE-side breaks, 11 years | **2 bars** (never 1, never 0) |
| distinct `detection_time` across all 2,080 production order blocks | **2,080 — no duplicates** |

So the unstable sort has **no ties to lose**. `detection_time` is already
monotonic, the sort is a no-op, `ob_id` is `1..N` in row order, and
`prepare_order_blocks_for_simulation` returns the rows unchanged — all verified
directly against the engine.

The consequences that §24.6 drew from a same-bar pair — "both can fire on one
bar", "the bear block is tagged CHoCH from a bias the bull break just set",
`event_count == 2` — describe code paths that exist but can never execute. They
are still mirrored, because production still contains them, but they are
**unreachable**, not merely rare.

**What remains is conditional, and is recorded as such:** if production ever
loosens a break condition so two blocks can share a bar, the unstable sort
becomes live immediately. `test_production_order_block_rows_have_no_orderable_ties`
asserts the no-duplicates property directly, so that change fails a test instead
of silently reordering trades.

## 25.2 The size gate has never fired

Replaying the full frozen dataset:

| | |
|---|---|
| order blocks created | 2,080 |
| size-gate **rejections** | **0** |
| width range | 0.80 – 38.90 pips |
| widths within 1 pip of the 100.0 limit | 0 |

The `max_ob_size_pips = 100.0` gate has never rejected an order block in eleven
years — the widest block ever produced is 39% of the limit. So no slice of real
history can exercise the accept/reject boundary, the "a rejection consumes no
id" property, or the dense-id consequence that follows it. Those need synthetic
fixtures, which is why §25.3 exists.

`min_ob_size_pips = 0.0` cannot reject anything either: the width is an absolute
value and therefore never negative.

## 25.3 An exact 100.0-pip width is not representable

The gate is `width > max_pips`, strict, so a block of exactly 100.0 pips must
PASS. That case cannot be built:

```
width = |high - low| / pip_size
```

and no pair of representable prices at EURUSD levels yields exactly `100.0`.
Searching every 6-decimal price pair across 0.90–1.70, and every 8-decimal pair
across a narrower band, returned **zero** hits. The reachable widths step
straight over the boundary:

```
closest width <= 100.0 :  99.99999999999787
closest width >  100.0 : 100.00000000000009
gap                    :   2.2e-12 pips
```

This is not a reason to skip the test — it *is* the test. `F-S5-SIZE-AT-LIMIT`
holds the first of those and is ACCEPTED; `F-S5-SIZE-OVER` holds the second, one
ULP wider, and is REJECTED. That is the finest discrimination the number system
permits, and it is a stronger statement about a strict comparison than an exact
100.0 would have been.

It also quantifies the parity risk §24.8 flagged: a block within ~2e-12 pips of
the limit could land on either side under a different float implementation. No
such block exists in production history, so the risk is real but currently
unrealised.

## 25.4 The S5 fixture set

| Fixture | Source | What it pins |
|---|---|---|
| `F-S5-SIZE-UNDER` | synthetic | inside the limit → accepted, `ob_id` 1 |
| `F-S5-SIZE-AT-LIMIT` | synthetic | widest representable acceptance |
| `F-S5-SIZE-OVER` | synthetic | one ULP wider → rejected, **no id consumed** |
| `F-S5-ID-DENSITY` | synthetic | rejection then acceptance → next id is **1, not 2**; the rejection still burns its swing and flips the bias |
| `F-S5-INVERTED` | **real**, 2026-04-17 | high-volatility origin → `top < bottom`, not normalised |
| `F-S5-ORIGIN-TIE` | **real**, 2025-11-25 | equal extremes → **earliest** bar wins |
| `F-S5-OPPOSITE-BREAKS` | **real**, 2026-02-11 | opposite-side breaks close together; at most one candidate per bar |
| `F-TV-S1S5` | **real**, the export window | feed gap, 7 ties, the inverted box, 59 opposite pairs |

The synthetic scenarios exploit three properties of the algorithm rather than
simulating it: identical consecutive bars create **no** swings (the leg test is
strict), a constant-range baseline pins the ATR so `2 × atr` becomes a placeable
threshold, and the first swing production can ever record is a swing **low**
(`current_leg` starts BEARISH, so a `new_leg_high` while bearish is a no-op).
Getting that last one backwards yields a fixture with no order block at all and
no obvious reason why.

Every synthetic fixture is re-verified against the production replay at build
time and again in the test suite. A scenario that stops exhibiting its edge
**fails** rather than quietly becoming a fixture that asserts nothing.

## 25.5 Evidence is not carried across a build change

The S1–S4 logic evidence was gathered against `…t1.2.0_g0.5.1`. Landing S5 moved
the trace schema to 1.3.0, which moves the build fingerprint. That evidence has
been **mechanically demoted** to `LOGIC_UNVERIFIED` by
`record_parity --invalidate-stale`, with the superseded claim preserved under
`superseded` so the demotion is auditable.

The S1–S4 algorithms were not intentionally changed. That is not the point:
"probably unaffected" is a judgement call, and this apparatus exists to remove
judgement calls from parity claims. One export restores all five stages.

## 25.6 What the export now proves

The comparator's S5 field set compares candidate presence, side, tag, bounds,
width, break level, gate result, rejection reason, and the active-inventory
progression. Two fields deserve note:

* **`oracleObOriginBack` / `oracleObPivotBack`** carry identity as a *distance*
  rather than an index, because `bar_index` is feed-local (L-09) — only the gap
  between two bars survives a feed that disagrees about which bars exist.
* **`oracleObChecksum`** is a running total of every accepted bound. `ob_id` is
  history-local (L-05) and cannot be compared at all; the checksum can, and it
  makes a single wrong box anywhere in the loaded window fail the comparison
  rather than only on the bar that drew it.

A missing S5 column is now a hard failure rather than `NO_EVIDENCE`. The two read
identically in a summary line, and S5's whole premise is that a box drawn on
screen is not evidence.

---

# 26. Wave 2a — S6 UTC daily regime and market state (2026-08-05)

## 26.1 The live path is not the one the name suggests

Part 1 re-verification found the deployed call path is **not** the global regime
gate. Read from source, not from the Phase 0 audit:

```
regime_gate_enabled                = False
build_regime_runtime_config(...)   -> None
regime_emit_for_run(...)           -> None        ← the in-loop gate NEVER fires
portfolio_emit_for_run(...)        -> ACTIVE, mode "enforce"
state_policy_emit_for_run(...)     -> ACTIVE      ← where state_target_block comes from
        both call _portfolio_panel_index (run_backtest.py:2413)
        whose docstring: "independent of the global regime gate so enforce mode
        works even when regime_gate_enabled is False"
            -> daily_regime_panel(records, cfg, "EURUSD")   regime.py:181
```

So the panel **is live**, and mirroring the thing called "the regime gate" would
have reproduced dead code. Verified empirically by calling all three emit
functions against the resolved production config inside the LUX working
directory: `regime_emit` None, the other two active, both returning the identical
336-day panel with threshold 2.342.

**Production aggregates the 1-MINUTE frame**, not the 15m detection frame:
`.resample("1D").agg(first/max/min/last).dropna()`, then hands already-daily
records to `daily_regime_panel` (whose own `resample_daily` is then an identity).
The 15m-sourced daily aggregate is provably identical — max of maxes, min of
mins, last of lasts, and the first 15m open IS that day's first 1m open — and
that equivalence is asserted by a test rather than assumed.

## 26.2 The algorithm, pinned

| | |
|---|---|
| daily bar | UTC calendar day; open=first, high=max, low=min, close=last; a day with no bars is **absent**, not zero-filled |
| EMA | length **200**, `ewm(adjust=False)`, alpha 2/201, **first-value seeded**, a gap passes the previous value through |
| Bollinger | length **20**, multiplier **2.0**, `rolling().std()` default = **ddof=1 (SAMPLE)** |
| BBW | `(2 × mult × sd) / basis × 100`, threshold **2.342** fixed for EURUSD |
| ADX | Wilder, length **14**, smoothed with the SAME first-value-seeded ewm at alpha 1/14; `tr[0] = high-low`; +DM/-DM require a STRICTLY larger and strictly positive move |
| shift | the row FOR day D uses day **D-1**'s features; day 0 is null |
| classifier | `px_vs_ema > 0` → Bull else Bear · `bbw > threshold` → Expand else Compress · `adx < 18` → the state NAME becomes `{trend}/Chop` while `volatilityState` still reports Expand/Compress |

Three operators are load-bearing and all are edge-reachable: a price **exactly on**
the EMA is **Bear** (measured: `pxVsEma` is exactly 0.000 on 2015-01-02, the seed
day); a BBW **exactly at** 2.342 is Compress; an ADX **exactly at** 18.0 is Trend.

## 26.3 The reference, proven

`tools/oracle/replay_regime.py` recomputes every intermediate and is checked
field-for-field against `daily_regime_panel`:

> **3,586 / 3,586 UTC days** over the full 2015→2026 production dataset
> (4,271,083 M1 rows), across all eleven panel fields.

| | max abs diff | ULP |
|---|---|---|
| EMA | **0.0** | 0 on 3,585 days — bit-exact |
| ADX | **0.0** | 0 on 3,584 days — bit-exact |
| pxVsEma | **0.0** | bit-exact |
| BBW | 1.21e-10 | max 794,549 — and **zero** state-code mismatches |

The BBW ULP figure is far above the audit's earlier ~220 because the replay sums
the 20-element window directly while pandas uses a numerically-stable online
algorithm; the summation ORDER differs. That is the honest measurement, and it is
harmless here for a reason that was measured rather than assumed:

> the closest any of 3,586 real days comes to the 2.342 threshold is **3.865e-4**
> — a margin of about **3.2 million times** the replay noise.

Hence the comparator's split: a declared float band for intermediates, **exact**
for every state code. A float inside tolerance never excuses a code mismatch.

## 26.4 Pine

`pine/src/66_regime.pinefrag`. The daily series is accumulated from the chart's
own intraday bars — there is no grouping primitive in Pine, so the day is built
with running state and finalised on the first bar of the next UTC day, at which
point the completed day's features classify the new day. That IS the one-day
shift, expressed causally.

State is bounded: a **20-element** ring of daily closes for the Bollinger window,
and scalars for everything else. Nothing grows with chart length.

Four built-ins are forbidden and now fail lint: `ta.ema` (SMA-seeded),
`ta.stdev`/`ta.variance` (population), `ta.adx`/`ta.dmi` (RMA seeded from a sum).
`request.security(..., "D", ...)` and `timeframe.change("D")` were already
rejected by the `exchange_day` rule.

## 26.5 Measured bootstrap

| | |
|---|---|
| first valid EMA | daily index 1 (2025-10-01) |
| first valid ADX | daily index 2 (2025-10-02) |
| first valid **BBW** | daily index **20** (2025-10-23) — the binding constraint |
| first market state | daily index **20** (2025-10-23) |
| **S6 bootstrap** | **1,632 bars = 20 UTC days**, first scored bar 2025-10-23 00:00 |
| bars after it with no state | **0** — no recurring invalidity |

S6's bootstrap is **its own measurement**, not the 206-bar structural one: it is
set by 20 daily closes, not by a 50-bar swing window. All six states occur in the
chart window, with 2,328 transitions.

## 26.6 Production replay is permanently unattainable — reason recorded

| | |
|---|---|
| production daily history | 3,586 days (from 2015-01-01) |
| chart daily history | 226 days |
| market-state agreement | **68.4%** — 31.6% differ |
| nature | **trend sign flips**: full `Bull` +1.434 vs chart `Bear` −0.959 |
| EMA(200) convergence | **never** — 3.06e-03 relative at the final day |

EMA(200) has a ~200-day time constant seeded from one close 226 days ago; a 15m
chart caps the daily series, so scrolling cannot fix it. Recorded as
`REPLAY_UNATTAINABLE_IN_CURRENT_CONTEXT`, reason
**`INSUFFICIENT_DAILY_HISTORY_FOR_EMA200_SEED`**.

**Logic parity is still attainable** — chart-bars re-runs Python over the chart's
own bars, so both sides seed identically. A green S6 logic row therefore means
"Pine reproduces Python on this history", never "this chart shows production's
regime". The HUD says so in two non-suppressible rows.

## 26.7 Scope

`net_r` is **excluded**: `gross_r = row["pnl_r"]` with an early return when
`fill_time == ""`, i.e. post-fill exit accounting, not an entry-time value. It
belongs to the lifecycle wave. `market_state` stays in the trace's
`sections_unimplemented` even though S6 computes the panel — that section is the
per-trade attachment (S8), which reads the panel but is not the panel.

## 26.8 TradingView's 64-plot ceiling (found on a live chart)

The first S6 build compiled, rendered, and then died with:

```
Runtime error: RE10140 — The script creates too many plots (82). The limit is 64.
```

A **runtime** limit: not the compiler, and not this repo's linter, which had no
concept of a plot budget. S1–S6 had grown to 83 plot-family calls (`plot`,
`plotchar` and the rest share one budget) against a hard cap of 64.

Dropping exported fields to fit would have meant the comparator scoring things
the chart no longer published, so instead the **small-integer fields of S1, S3
and S6 travel packed** — one composite plot per stage, mixed-radix, LSB-first.
Every field keeps its exact value; only the transport changed. The spec lives in
`session_codes.py::PACKED_SPEC` and BOTH sides derive from it: the generator
emits the multipliers, the comparator decodes with them. A hand-written
multiplier on either side would be a silent decode error, so neither has one.

The two active swing levels also stopped being plots and became `line` objects
(bounded, created once and moved), and the debug parsed-price crosses were
removed — their values remain in the debug table and in
`oracleParsedHigh`/`oracleParsedLow`, and a marker is not evidence.

| | |
|---|---|
| before | 83 plot-family calls |
| after | **60 of 64**, four slots of headroom |
| mandatory export columns | 63 → **45** (nothing dropped; three stages packed) |

`lint_pine` now counts plot-family calls, **errors** above 64 and **warns** with
fewer than six slots left — negative-tested at 67. The next stage will need more
packing, and the linter will now say so before a chart does.

