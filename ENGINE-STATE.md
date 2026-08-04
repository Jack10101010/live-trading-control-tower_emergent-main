# ENGINE-STATE.md — canonical cross-cycle state inventory

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 4)
**Status:** descriptive only. No code changed.

Sources: Control Tower `d6e776f`, Lux `d978074`.

Classification used throughout:

| Class | Meaning |
|---|---|
| **Persistent** | Must survive process death. Losing it loses money, breaks idempotency, or breaks broker↔engine agreement. |
| **Recomputable** | Can be rebuilt from durable inputs. Today all of it *is* rebuilt, every cycle, from inception. |
| **Derived** | A pure projection of something else in this table. Never a source of truth. |
| **Disposable** | Intentionally process-local. Loss must degrade cleanly to a slower correct path. |

---

## 1. Persistent

All of this lives in `<state_dir>/runner_state.json`, written by
`RunnerState.save()` (`live/state.py:234`) as temp → write → flush → fsync →
atomic rename, plus `<state_dir>/frames/prev_trades.csv`.

| Item | Key | Why it must persist |
|---|---|---|
| Last evaluated boundary | `last_boundary` | The C3 skip gate compares against it. Lose it and the runner re-evaluates a boundary it already acted on, re-emitting intents for transitions the broker has already executed. |
| Input revision of that boundary | `last_recomputed_input_revision` | Half of the C3 gate. A boundary without its revision is a boundary that might have been computed from different bytes. Source comment is explicit: a boundary "can never advance without the revision it was computed from". Absent (pre-C3 state) is treated as unknown and forces re-evaluation — fail-safe. |
| Previous trade frame | `prev_frame_file` → `frames/prev_trades.csv` | `diff_frontier` needs the previous engine output to compute transitions. Without it `prev is None` and the run is treated as `bootstrap`: **no intents at all**. Losing this file silently suppresses one cycle's trading. |
| Frame hash | `prev_frame_hash` | Integrity check on the frame file. |
| Intent ledger | `ledger` | The idempotency record. Every intent is durably reserved PENDING *before* the atomic commit, so a boundary can never become durable without its intents (LR-1). This is what `drain_pending()` replays after a crash. Losing it risks duplicate broker orders. |
| Broker mirror | `mirror` | `trade_id → ticket`. The local half of reconciliation; without it every live position looks like an `ORPHAN`. |
| Realised R by date | `realized_r_by_date` | Feeds the daily-loss rail. |
| Accounted deal ids | `accounted_deal_ids` | Exactly-once accounting across restarts, with a durable `accounted_at` marker (B4.5) so entries cannot expire before the ledger rows they guard. |
| Accounting telemetry | `accounting_telemetry` | Operational counters that must survive restart to stay monotonic. |
| `updated_at` | — | Write ordering / debugging. |

**Not currently persisted but financially load-bearing — see §5.**

## 2. Recomputable

Everything the engine computes inside `golden_pipeline`. Today **all of it is
rebuilt from inception on every boundary advance**; none of it is cached.

| Item | Produced by | Why it is recomputable | Why that is expensive |
|---|---|---|---|
| Assembled candle frame | `_assemble_from_bytes` (`live/runner.py:155`) | Pure function of the two input files, whose exact bytes are hashed into `input_revision`. | 263 MiB CSV parsed every slow-path cycle. |
| Date-filtered frame | `rb.filter_date_range` | Pure function of frame + config. | measured 2.7 s |
| Simulation-prepared candles | `core.prepare_candles_for_simulation` | Pure. | measured 3.3 s |
| Calendar events / news events | `rb.load_news_calendar_events`, `rb.load_news_events` | Pure function of the news CSV. | cheap (~0.06 s each) |
| News cache | `prepare_news_cache` (`src/execution.py`) | Pure function of news events + candles. | measured 10.3 s |
| M15 detection frame | `rb.resample_candles` | Pure downsample. | measured 0.28 s |
| Order blocks | `core.detect_order_blocks` | Pure, causal, single forward pass. | measured 26.6 s |
| News-tagged OBs | `rb.tag_order_blocks_with_news` | Pure join. | measured 25.3 s |
| Structure/direction-filtered OBs | `rb.filter_order_blocks_by_structure(_direction)` | Pure. | ~0.003 s |
| Simulation OBs | `core.prepare_order_blocks_for_simulation` | Pure. | ~0.014 s |
| Trade frame | `rb.execute_scenario_job` → `simulate_trades` | Pure function of candles + OBs + news cache + config. | **measured 189 s — 73% of the cycle** |

The engine is *deterministic* — `live/benchmark.py` verifies byte-identical
trade frames across iterations by default, and that verification passed on every
run in this milestone. Determinism is what makes caching sound in principle.

### 2a. The hidden constraint on recomputability

`_add_parsed_prices` (`strategy_core/order_blocks.py:43`) computes the
volatility measure as either `_pine_rma(true_range, 200)` or
`true_range.cumsum() / bar_index`. Both are **causal** — value at bar *i*
depends only on bars ≤ *i* — but both are **seeded from the first bar of the
supplied frame**:

* `_pine_rma` seeds `previous = <first value>` and recurses forward (line 33-41).
* the cumulative mean divides by the running bar count from the frame start.

Therefore the engine cannot be given a truncated window and produce the same
answer. There is no bounded warm-up length that makes a short window exact.
**This is the structural reason the pipeline reprocesses eleven years every
fifteen minutes**, and it is the single most important fact for
`INCREMENTAL-DESIGN.md`.

## 3. Derived

| Item | Derived from | Note |
|---|---|---|
| `OrderIntent[]` | `diff_frontier(prev_frame, cur_frame, frontier_bar)` | Pure function of two persisted/recomputed frames. |
| `intent_id` | `sha1(INSTANCE_ID│trade_id│transition│frontier_bar)` | Deterministic — replay regenerates byte-identical ids. That is exactly what makes duplicate suppression exact. |
| `trade_id` | `"L"/"S" + ob_id` (`strategy_core/execution.py:765`) | Inherits `ob_id`'s stability class. See `ORDER-BLOCK-IDENTITY.md`. |
| Ghost fields on trade rows | `GhostTracker.get_fields(ob_id)` | Merged at `strategy_core/execution.py:3070`. |
| Telemetry payload | `publisher.build_payload()` | Projection only — no broker read, no recomputation. |
| Freshness / staleness | `backend/live_telemetry.py` | Computed tower-side from `published_at`/`received_at`. |
| Candidate boundary | `latest_closed_boundary(last_m1_time)` | Pure arithmetic on the frame's max time. |

## 4. Disposable

Process-local by design. Every one of these must degrade to a correct slower
path when lost — and each does.

| Item | Where | Degradation on loss |
|---|---|---|
| `_FastIdleMemo` | `live/runner.py:181` | Explicitly "never authoritative, never persisted, may disappear at any time". Loss falls through to the C3 slow path. |
| `_CadenceState` | `live/main.py:45` | Lives only as a local in `main()`'s loop. Loss resets to base cadence. |
| `_CapturedInput` | `live/runner.py:194` | Single-use; discarded after a skip or a parse. |
| `PhaseProfiler` readings | `live/runner.py:35` | Operational telemetry only; explicitly never enters deterministic artifacts. |
| Liveness beacon fields | `live/liveness.py` | Fail-open by contract; a telemetry failure must never fail a trading cycle. |
| `GhostTracker` instance | rebuilt per `simulate_trades` call | Rebuilt from scratch each cycle. |
| `LuxSession` / loaded modules | `live/runner.py:75` | Rebuilt at process start; engine identity re-verified. |

## 5. Gaps found while compiling this inventory

Recorded, not fixed — this milestone changes nothing.

1. **The engine's internal walk state is not persisted anywhere.** `active_trades`,
   `pending`, `ob_cursor`, the RMA accumulator and the `ob_id` counter live only
   inside a `simulate_trades` call. That is *why* a resume is impossible today
   and a full replay is mandatory. It is a design consequence, not a defect —
   but it is the precise thing `INCREMENTAL-DESIGN.md` must make persistent.

2. **`prev_trades.csv` loss is silent.** `load_prev_frame()` returns `None` when
   the file is missing (`live/state.py:278`), and `run_once` treats `prev is
   None` as `bootstrap` — emitting **no intents** and logging a benign-sounding
   note. A corrupted or deleted frame file therefore suppresses a cycle's
   trading without raising. `prev_frame_hash` is stored but is not checked on
   load. Worth a guard in a later milestone.

3. **`cycle.sequence` is `null` by design.** There is no monotonic per-cycle
   counter in the ops record, so cycle ordering cannot be verified downstream.
   The publisher deliberately refuses to invent one. If ordering ever matters,
   it belongs in `ops_log`, not the publisher.
