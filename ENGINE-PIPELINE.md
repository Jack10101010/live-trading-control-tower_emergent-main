# ENGINE-PIPELINE.md — canonical end-to-end runtime pipeline

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 2)
**Status:** descriptive only. Nothing here changes trading behaviour.

Everything below is read from the current source at:

| Repo | Path | Commit |
|---|---|---|
| Control Tower | `/Users/jack/Documents/Dev Projects/live-trading-control-tower-live-prep` | `d6e776f` |
| Lux engine | `/Users/jack/Documents/Dev Projects/Lux-OB-Backtester` | `d978074` |

The two repos are bound at runtime by `LuxSession` (`live/runner.py:75`), which
inserts the Lux root on `sys.path` and `os.chdir`s into it, because the Lux
driver's contract is CWD = Lux repo root (the deployed policy is read by the
relative path `configs/policy/deployed_policy.v1.json`).

---

## 0. One-paragraph summary

A single-threaded loop wakes every 10–60 s, appends any newly closed M1 bars
from MT5, and asks the runner whether the latest **closed 15-minute boundary**
has advanced. If it has not, the cycle ends in microseconds. If it has, the
runner **re-runs the entire 2015→present backtest from scratch**, diffs the
resulting trade frame against the previous one, converts the differences into
`OrderIntent`s, executes them through the safety rails, and publishes a
telemetry snapshot to the Control Tower. There is no incremental state: every
boundary re-derives eleven years of history.

---

## 1. Launcher

`live/main.py :: build()` → `main()` (`live/main.py:199`).

`build()` constructs, in order: `LiveConfig`, `MT5Gateway`, `MT5BarBridge`,
`LuxSession` + `LiveRunner`, `Executor`, `CTPublisher`, `OpsLog`. `LuxSession`
construction is where engine identity is verified — `verify_engine()`
(`live/runner.py:98`) refuses to proceed when `engine_version()` differs from
`ENGINE_VERSION_EXPECTED` (`live/config.py:16`), with the explicit message
"refusing to trade on an unverified engine".

Startup cost is paid once: import of the Lux driver, `pandas`, and
`strategy_core`. It is not part of any cycle measurement.

## 2. Scheduler

`live/main.py:242` — `while True:` … `time.sleep(cadence.sleep_s)`.

Cadence is scheduler-owned; the runner is deliberately cadence-unaware.

| Constant | Value | Source |
|---|---|---|
| `CADENCE_BASE_SLEEP_S` | 10.0 | `live/main.py:41` |
| `CADENCE_MAX_IDLE_SLEEP_S` | 60.0 | `live/main.py:42` |
| `MAX_CONSECUTIVE_ERRORS` | 10 | `live/main.py:36` |

`_cadence_transition()` (`live/main.py:56`) is a pure, total function over the
**completed** cycle record. Idle cycles double the sleep up to the 60 s ceiling;
the ceiling is deliberately below the 120 s heartbeat-freshness contract
enforced by `deploy_check`. `_CadenceState` is process-local and never
persisted, never serialised, never added to `RunnerState` or `OpsLog`.

The first cycle runs immediately; the sleep follows.

## 3. Lifecycle (one cycle)

`live/main.py :: cycle()` (`live/main.py:100`). Stages in strict order, each
wrapped in coarse wall-clock timing recorded into `stage_timings`:

| # | Stage | Call | Timing key |
|---|---|---|---|
| 1 | Pending recovery | `executor.drain_pending()` | `drain_s` |
| 2 | Confirmed-close accounting | `executor.account_closed_deals()` | `accounting_s` |
| 3 | Market data | `bridge.poll_once()` | `bridge_s` |
| 4 | Strategy evaluation | `runner.run_once()` | `runner_s` |
| 5 | Execution | `executor.apply(intents)` | `executor_s` |
| 6 | Publish | `publisher.build_payload()` + `publish()` | `publish_s` |

Ordering is load-bearing and documented in source: drain precedes fresh
evaluation so a crash-recovered intent is never overtaken; accounting precedes
the rails so "a loss realised at 09:00 must be able to block a 09:15 entry in
the SAME cycle". A frozen/ambiguous drain result short-circuits stages 3–5 with
`status: frozen_pending_recovery`.

The whole body is wrapped so that `liveness.end_cycle()` always runs, and
exceptions are caught, logged into the cycle record, and the loop continues —
the supervisor handles repeats via `MAX_CONSECUTIVE_ERRORS`.

**Liveness beacon.** `live/liveness.py` runs a single daemon thread writing
`<state_dir>/ops/liveness.json` on a wall-clock tick, so liveness keeps
advancing while the main thread is blocked inside the long compute stage. Its
own docstring names that stage "the ~900s Golden pipeline call". The beacon
writes **locally only** — it never publishes to the tower. Consequence, and it
matters for capacity: **a telemetry publication can only happen at the end of a
cycle, so the observed interval between publications is a lower bound on cycle
duration.**

## 4. Market data

`live/mt5_bridge.py :: MT5BarBridge.poll_once()`.

Appends newly closed M1 bars to the live segment CSV
(`config.live_segment_csv` → `<market_data_dir>/EURUSD_1m_live.csv`,
`live/config.py:154`). Returns `last_bar_time` and `appended` count.

The engine's candle input is therefore **two files**:

* frozen history — `data/candles/EURUSD_1m_extended_2015_2026.csv` in the Lux
  repo (`live/runner.py:399`), and
* the live segment appended by the bridge.

`_assemble_from_bytes()` (`live/runner.py:155`) joins them under a fixed seam
policy: frozen rows win at or before the frozen end, live rows win strictly
after. No interleaving, no rewrite.

## 5. Input identity + the two skip gates

Before any parsing, `_capture_bytes()` (`live/runner.py:211`) reads each input
file exactly once and computes `input_revision` =
`sha256(version | frozen_sha256 | live_sha256 | engine_version)` over a locked
canonical preimage (`live/runner.py:144`). The version literal is part of the
preimage, so any format change yields a non-equal revision — fail-safe
recompute, no migration path.

**C4 fast idle** (`live/runner.py:387`). A process-local `_FastIdleMemo` holds
the `(input_revision, boundary)` pair most recently validated by a *complete*
C3 decision. If the freshly captured revision equals both the stored and the
memoised revision, the cycle returns `no_new_bar` **without ever constructing a
DataFrame**. This is the invariant that makes idle cycles cheap.

**C3 gate** (`live/runner.py:410`). Slow path: parse the captured bytes, compute
`latest_closed_boundary()` (`live/runner.py:277`) =
`floor(last_m1_time + 1min, 15min) − 15min`, and skip only when the boundary
**and** the exact input revision both match durable state. A missing stored
revision (pre-C3 state) is treated as unknown and always re-evaluates.

Detection timeframe is fixed at 15 minutes; the 1-minute source granularity is
fixed by the MT5 bridge seam. Neither is a parameter.

## 6. The Lux engine — `golden_pipeline`

`live/runner.py :: LiveRunner.golden_pipeline()` (`live/runner.py:317`). This is
the measured interval in `live/benchmark.py`. It calls the **same** driver
helpers the research runs use — zero re-implemented strategy logic. The only
config delta from the Golden JSON is `end_date`, extended to the frontier.

Stages, in source order, with the `PhaseProfiler` key each is timed under:

| # | Phase key | Call | Module |
|---|---|---|---|
| 1 | `filter_date_range` | `rb.filter_date_range` | `scripts/run_backtest.py` |
| 2 | `prepare_candles` | `core.prepare_candles_for_simulation` | `strategy_core` |
| 3 | `load_news_calendar` | `rb.load_news_calendar_events` | driver |
| 4 | `load_news_events` | `rb.load_news_events` | driver |
| 5 | `prepare_news_cache` | `prepare_news_cache` | `src/execution.py` (deliberate src-resident seam) |
| 6 | `resample` | `rb.resample_candles(…, detection_timeframe)` | driver |
| 7 | `detect_order_blocks` | `core.detect_order_blocks` | `strategy_core/order_blocks.py` |
| 8 | `tag_obs_news` | `rb.tag_order_blocks_with_news` | driver |
| 9 | `filter_obs` | `rb.filter_order_blocks_by_structure` (+ direction) | driver |
| 10 | `prepare_obs` | `core.prepare_order_blocks_for_simulation` | `strategy_core` |
| 11 | `execute_scenario_job` | `rb.execute_scenario_job` | driver → `simulate_trades` |

`PhaseProfiler` (`live/runner.py:35`) conforms to `strategy_core.ports.Profiler`
but is **never injected into core execution** — it only wraps driver-side stage
calls. Its readings are operational telemetry and never enter deterministic
artifacts.

Exactly one entry scenario is permitted: the pipeline asserts a single
`triggered_edge` scenario and raises otherwise (`live/runner.py:349`).

### 6a. Order blocks

`strategy_core/order_blocks.py :: detect_order_blocks()` (line 128). A single
forward pass over the **resampled M15** frame. Loop-carried state: `current_leg`,
`swing_trend_bias`, `swing_high`, `swing_low`, and the `ob_id` counter.

`_add_parsed_prices()` (line 43) computes the volatility measure — either
`_pine_rma(true_range, 200)` (an RMA seeded from the first bar) or a cumulative
mean range. **Both are causal but seeded from frame inception**, which is the
structural reason the engine cannot start from a truncated window. See
`ORDER-BLOCK-IDENTITY.md` §3.

### 6b. Ghost tracker

`strategy_core/ghost_tracker.py`. Constructed inside `simulate_trades`
(`strategy_core/execution.py:2305`) as `GhostTracker(_ghost_config)`. Tracks
cancelled/unfilled order blocks so their counterfactual outcome can be
attributed. Keyed by `str(ob["ob_id"])` (`ghost_tracker.py:176`, `:482`), with
`id(ob)` as a last-resort fallback — see the identity spec for why that fallback
matters.

Fields are merged into the trade rows at `strategy_core/execution.py:3070` via
`ghost_tracker.get_fields(row.get("ob_id"))`.

### 6c. Scenario engine / trade walk

`scripts/run_backtest.py :: execute_scenario_job()` (line 2529) →
`strategy_core/execution.py :: simulate_trades()` (line 2121).

The core is a **pure-Python per-candle loop** over the full M1 frame:
`for candle_index, candle in enumerate(candle_records)`
(`strategy_core/execution.py:2327`). Order blocks are consumed through a
monotonic `ob_cursor` keyed on `detection_time`. Loop-carried state includes
`active_trades`, `pending`, `ob_cursor`, the accumulating `trades` list, and the
`GhostTracker`.

Prior optimisation passes are already present and marked in source: PERF-B
(precomputed OB cursor lookups replacing per-candle pandas access, noted as a
"~19% hotspot"), PERF-I (caller-supplied `candle_records` reuse), PERF-J
(positionally-aligned precomputed news arrays).

An optional exact call-count instrument exists: `profile_simulation_hot_path`
(`src/config.py:54`, default `False`) enables `_new_hot_path_profile()`
(`strategy_core/execution.py:1760`) with counters `candle_iterations`,
`active_checks`, `pending_checks`, `base_row_creations`,
`news_blackout_lookups`, `news_flatten_lookups`, `fill_checks`,
`invalidation_checks`. It is surfaced via `attach_hot_path_profile()`
(`scripts/run_backtest.py:2519`).

## 7. Intent generation

`live/intents.py :: diff_frontier()` (line 68), called from
`live/runner.py:432`.

The frozen engine is the **only** state machine; the broker mirrors engine
transitions at the frontier bar:

| Engine transition at frontier | Intent action |
|---|---|
| fill | `OPEN_POSITION` |
| exit (`WIN`,`LOSS`,`NEWS_FLATTEN`,`PROTECTION_EXIT`,`BE_EXIT`) | `CLOSE_POSITION` |
| stop move (BE/RR) | `MODIFY_STOP` |

`intent_id = sha1(INSTANCE_ID | trade_id | transition | frontier_bar)[:20]`
(`live/intents.py:44`) — replaying a diff regenerates byte-identical ids, which
is what makes duplicate suppression exact.

On the first ever run `prev` is `None`, no intents are emitted, and the cycle
returns `status: bootstrap`.

## 8. Durable commit

`live/runner.py:435-441`. Every generated intent is durably reserved as PENDING
**before** a single atomic commit, so a boundary can never become durable
without its intents:

```
for intent in intents: state.reserve_pending(intent)
state.store_frame(trades_str, boundary_str, input_revision)
state.save()      # ONE atomic commit
```

`RunnerState.save()` (`live/state.py:234`) is temp → write → flush → **fsync** →
atomic rename. The fsync is explicitly there to close the power-loss window the
rename alone leaves open. The C4 memo is primed only *after* the save returns.

## 9. Execution + reconciliation

`live/executor.py`. `apply()` (line 376) runs intents through the safety rails;
`reconcile()` (line 79) compares broker positions against the local mirror,
classifying findings as `MISSING`, `ORPHAN`, or `FOREIGN`
(`live/reconciliation.py:48-50`) on a `(magic, symbol)` ownership basis.
`drain_pending()` (line 528) replays durably-reserved intents after a crash.
`account_closed_deals()` (line 248) is the confirmed-close realised-R path
(off by default — see `CLOSE-ACCOUNTING-RUNBOOK.md`).

## 10. Publisher + telemetry

`live/publisher.py :: build_payload()` (line 31) projects safe fields out of the
node's own live objects — no broker read, no recomputation.

`publish()` (line 56) **always writes the durable fallback file first**, so no
tower state (unreachable, slow, unauthorised) can affect trading. Then one
POST attempt per cycle to `<ct_base_url>/live/ingest`, no retry loop. An HTTP
rejection is deliberately distinguished from a network failure: `HTTPError` is
caught before `URLError`, so 401/403 yields explicit `unauthorized: True`
rather than masquerading as a timeout.

Schema `ct.node-telemetry.v1`; contract in `NODE-TELEMETRY-CONTRACT.md`;
backend-side validation and freshness in `backend/live_telemetry.py`.
`cycle.sequence` publishes as `null` by design — the ops cycle record carries no
monotonic counter, and inventing one at the publisher would be a fabricated
ordering guarantee.

## 11. Operational logging

`live/ops_log.py`. Append-only JSONL at `<state_dir>/ops/cycles.jsonl`, one
record per cycle carrying `duration_s`, `boundary`, `last_bar`, `bars_appended`,
`status`, intent/applied/blocked/skipped counts, reconcile findings, `error`,
`stage_timings` and `phase_timings`. Plus `<state_dir>/ops/heartbeat.json`.
`live/shadow_report.py` aggregates it and asserts `no_boundary_overrun`
— *"no recompute > 900s bar interval"* (`live/shadow_report.py:19`).

**This file is the single best source of production capacity truth, and it lives
on the VPS filesystem.** It was not readable during this milestone. See
`CAPACITY-BASELINE.md` §7.

---

## 12. Data-flow diagram

```
                    ┌──────────────────────────────────────────┐
                    │ main() loop — sleep 10s..60s (adaptive)   │
                    └───────────────────┬──────────────────────┘
                                        │
        ┌───────────────────────────────▼───────────────────────────────┐
        │ cycle()                                                       │
        │                                                               │
        │  1 drain_pending ──► 2 account_closed_deals ──► 3 bridge.poll │
        │                                                        │      │
        │                                                        ▼      │
        │                                            ┌────────────────┐ │
        │                                            │ runner.run_once│ │
        │                                            └───────┬────────┘ │
        │   capture bytes (frozen CSV + live segment CSV)    │          │
        │            │                                       │          │
        │            ├─ C4 fast-idle memo hit ──► no_new_bar ┤ (µs)     │
        │            ├─ C3 boundary+revision match ► no_new_bar         │
        │            └─ else: parse ──► golden_pipeline ─────┤          │
        │                                                    │          │
        │        filter_date_range ─ prepare_candles ─ news  │          │
        │        resample ─ detect_order_blocks ─ tag_news   │          │
        │        filter/prepare_obs ─ execute_scenario_job   │          │
        │                     (per-candle walk over ALL M1)  │          │
        │                                                    ▼          │
        │                          diff_frontier ──► OrderIntent[]      │
        │                                                    │          │
        │        reserve_pending(each) + store_frame + save() (atomic)  │
        │                                                    │          │
        │  5 executor.apply ──► safety rails ──► MT5 ────────┘          │
        │                                                               │
        │  6 publisher.build_payload ──► POST /live/ingest              │
        │       └─ durable fallback file written FIRST                  │
        └───────────────────────────────────────────────────────────────┘
                                        │
                                        ▼
                       Control Tower backend/live_telemetry.py
                       → ops_status projection → UI
```

---

## 13. What this pipeline does *not* do

Stated explicitly because the incremental design depends on it:

* It does not cache any intermediate artifact between cycles. Order blocks,
  parsed prices, the news cache, and the full trade walk are all recomputed
  from inception on every boundary advance.
* It does not persist engine-internal state. The only durable state is the
  *output* frame (`prev_trades.csv`) plus boundary/revision — see
  `ENGINE-STATE.md`.
* It does not process more than one symbol. EURUSD only; there is no symbol
  loop anywhere in the pipeline.
* It does not parallelise. One process, one thread of compute, plus one daemon
  liveness thread that performs no compute.
