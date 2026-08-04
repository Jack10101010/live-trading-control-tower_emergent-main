# INCREMENTAL-DESIGN.md — incremental processing architecture

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 6)
**Status:** architecture only. **No implementation. No code changed.**

Grounded in: `CAPACITY-BASELINE.md` (measurements), `ORDER-BLOCK-IDENTITY.md`
(hard gate), `ENGINE-STATE.md` (state classes), `ENGINE-PIPELINE.md` (stages).

---

## 1. What is recomputed today

Every time the 15-minute boundary advances, **100 % of the following is
recomputed from the first bar of 2015**, with nothing carried over from the
previous cycle:

| Stage | Mean cost | Recomputed from |
|---|---:|---|
| `execute_scenario_job` (trade walk) | 187.71 s | bar 0 |
| `detect_order_blocks` | 26.78 s | bar 0 |
| `tag_obs_news` | 25.49 s | all 2,080 OBs |
| `prepare_news_cache` | 10.24 s | all candles |
| `prepare_candles` | 3.26 s | all candles |
| `filter_date_range` | 2.55 s | all candles |
| everything else | 0.48 s | — |
| **total** | **256.57 s** | |

Plus, outside the measured interval, a 263 MiB CSV read and parse per slow-path
cycle — and a 263 MiB read *even on fast-idle cycles*, because `input_revision`
must describe the exact bytes.

**The work that is genuinely new each cycle is one 15-minute bar** — 15 M1 rows
out of 4,314,720. That is **0.00035 %** of the input. Everything else is a
re-derivation of a result the previous cycle already computed.

## 2. Why it is recomputed — the real constraint

It is not laziness in the driver. Two structural facts force it, and both are
measured or source-verified:

**(a) Inception-seeded volatility.** `_add_parsed_prices`
(`strategy_core/order_blocks.py:43`) computes either `_pine_rma(true_range, 200)`
— a recursive filter seeded from the *first bar of the supplied frame* — or
`true_range.cumsum() / bar_index`. Both are causal, but both depend on where the
frame starts. There is no bounded warm-up that makes a truncated window exact.

**(b) No engine-internal state is persisted.** The only durable artefacts are the
*output* frame (`prev_trades.csv`), the boundary and the input revision
(`ENGINE-STATE.md` §1). `active_trades`, `pending`, `ob_cursor`, the RMA
accumulator, the swing state and the `ob_id` counter all live and die inside a
single `simulate_trades` call.

So the engine cannot resume; it can only restart. **The incremental design is
therefore not "cache the answer" — it is "checkpoint the accumulators".**

## 3. What makes incremental processing sound

Measured in `ORDER-BLOCK-IDENTITY.md` §4, Experiment A: extending the frame by
appending bars produced **2,077 shared order-block ids with zero mismatches**.
Detection is causal and append-only extension disturbs nothing already computed.

That is the licence. It holds **only** under the four conditions in
`ORDER-BLOCK-IDENTITY.md` §7 — unchanged `engine_version`, append-only frozen
data, unchanged detection parameters, unchanged window start. Experiments B and
C measured what happens when conditions 3 or 4 are violated: **every** shared id
changes meaning (B: 2,042 of 2,042) or a 296-id tail shifts (C) — silently, with
a run that still completes and still looks plausible.

**Therefore the invalidation check is not an optimisation detail. It is the
safety mechanism, and it must fail closed.**

## 4. Proposed architecture

Three cooperating layers, each independently useful and independently
committable. Layer 1 alone is worth more than half the cycle; Layer 3 is the
largest win but the hardest.

```
┌──────────────────────────────────────────────────────────────────┐
│ L0  GUARD — cache validity                                       │
│     engine_version │ params_digest │ window_start │ frozen prefix │
│     ANY mismatch ⇒ discard everything, full replay, no exception  │
└────────────────────────────┬─────────────────────────────────────┘
                             │ valid
┌────────────────────────────▼─────────────────────────────────────┐
│ L1  APPEND-ONLY ARTEFACT CACHE        (targets ~62 s / 24 %)      │
│     • detected order blocks (append-only, keyed by ob_key)        │
│     • news-tagged OB rows                                         │
│     • news cache, prepared candles                                │
│     Recompute only the tail after the last cached bar.            │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│ L2  DETECTION RESUME STATE            (part of the same ~27 s)    │
│     RMA accumulator, TR cumsum + count, previous_close,           │
│     swing_high/low, current_leg, swing_trend_bias, ob_id counter  │
│     Resume detection at the last cached bar instead of bar 0.     │
└────────────────────────────┬─────────────────────────────────────┘
                             │
┌────────────────────────────▼─────────────────────────────────────┐
│ L3  SIMULATION CHECKPOINT             (targets ~188 s / 73 %)     │
│     active_trades, pending, ob_cursor, closed-trade rows,         │
│     GhostTracker states                                           │
│     Resume the walk at the checkpoint bar instead of bar 0.       │
└──────────────────────────────────────────────────────────────────┘
```

### L0 — the guard (must be built first)

A cache is only reusable when **all** hold:

| Condition | How it is checked | Already exists? |
|---|---|---|
| `engine_version` unchanged | equality against the cached value | yes — `verify_engine()` |
| detection params unchanged | `params_digest` = hash of `swing_length`, `ob_filter`, `pip_size`, `min_ob_size_pips`, `max_ob_size_pips`, `detection_timeframe`, plus every simulation parameter | **no — new** |
| window start unchanged | cached first-bar timestamp equals current first-bar timestamp | **no — new** |
| frozen data append-only | the cached prefix must be a genuine **byte-prefix** of the current frozen file: cached `prefix_sha256` over the first *N* bytes still matches | **no — new** |
| live segment consistent | rows strictly after the frozen end, per the existing seam policy | seam exists |

The frozen-prefix check is the important new one. The existing `input_revision`
hashes the *whole* file, so it detects that the file changed but cannot
distinguish "appended" (safe) from "rewritten" (must replay). Incremental reuse
needs that distinction, and it must default to replay when unsure.

**Failure policy: any doubt ⇒ full replay.** A full replay is 256 s and always
correct. A wrong cache hit is silent corruption of the identity space, which
`ORDER-BLOCK-IDENTITY.md` §6.1 shows can desynchronise the broker mirror. The
asymmetry is total, so the guard must be conservative to the point of paranoia.

### L1 — append-only artefact cache

Order-block detection output is append-only under a valid guard (Experiment A).
Cache the detected + news-tagged OB rows keyed by the content-addressed `ob_key`
from `ORDER-BLOCK-IDENTITY.md` §6.3, retaining `ob_id` verbatim as the engine's
sequence number.

`tag_obs_news` (25.5 s) tags 2,080 OBs against the calendar. Under append-only
extension only *new* OBs need tagging — and OBs are rare (2,080 in eleven years,
roughly one every two days). This phase should collapse to near zero.

`prepare_news_cache` (10.2 s) and `prepare_candles` (3.3 s) are per-candle
transforms over the whole frame; they can be cached and extended by the tail.

### L2 — detection resume state

To resume `detect_order_blocks` at bar *k* instead of bar 0, persist exactly the
loop-carried state, all of which is visible in
`strategy_core/order_blocks.py:136-143` and `:43-53`:

| Field | Source |
|---|---|
| `rma_previous` (ATR accumulator) | `_pine_rma` recursion variable |
| `tr_cumsum`, `bar_count` | cumulative-mean-range accumulators |
| `previous_close` | `_true_range` uses `close.shift(1)` |
| `swing_high` `{level,index,crossed}` | loop state |
| `swing_low` `{level,index,crossed}` | loop state |
| `current_leg`, `swing_trend_bias` | loop state |
| `ob_id` (next value) | the counter |
| `last_cached_bar_index`, `last_cached_bar_time` | resume point |

**One non-obvious requirement.** `_make_order_block` searches
`candles.loc[swing_high["index"] : detection_index-1]`, and an uncrossed swing
index can be arbitrarily far in the past. So a resume must retain candle rows
back to **`min(uncrossed swing_high.index, uncrossed swing_low.index)`** — a
bounded but *variable* tail, not a fixed lookback. The checkpoint must record
that watermark and keep candles from it forward. Getting this wrong produces
subtly different order blocks, which is exactly the failure mode the guard
cannot catch.

### L3 — simulation checkpoint

The 73 % phase. To resume `simulate_trades` at bar *k*:

| Field | Source |
|---|---|
| `active_trades` | `strategy_core/execution.py` loop state |
| `pending` | loop state — the list rescanned 133 M times |
| `ob_cursor` | monotonic index into `ob_detection_times` |
| accumulated `trades` rows | closed trades already decided |
| `GhostTracker._states` | keyed by `str(ob_id)` |
| portfolio / regime / state-policy emitters | `kwargs` built per run |

**Safe checkpoint boundary.** A checkpoint is only sound at a bar where no
partially-resolved intrabar state exists. The natural choice is a bar at which
`active_trades` and `pending` are both consistent — i.e. between candle
iterations, never inside one.

**This is the hardest layer and carries the most risk.** Everything downstream of
a trade row — realised R, the frontier diff, intent identity — depends on the
walk being byte-identical. It must be gated behind the parity oracle
(`golden/run-001/parity-oracle/`) proving a resumed run reproduces the
from-scratch trade frame exactly.

**There is a cheaper alternative that should be tried first.** The measured call
counts say the cost is not the 4.27 M-candle walk itself but the **133 M
pending-list rescans** — ~31 pending orders re-examined per candle, ≈266 M inner
operations. Indexing or bucketing the pending list so each candle examines only
the orders that could plausibly fill or invalidate attacks the same 73 % without
any persistence, any checkpoint, or any change to what is computed — only to how
it is looked up. `OPTIMISATION-ROADMAP.md` ranks it first for exactly that
reason.

## 5. What can be skipped, and what still cannot

| Work | Today | Under this design |
|---|---|---|
| Read + hash frozen CSV | every cycle | still every cycle — `input_revision` must describe exact bytes. Reducible only by hashing incrementally over the appended tail. |
| Parse CSV → DataFrame | every slow-path cycle | tail only |
| `filter_date_range`, `prepare_candles` | full frame | tail only |
| news load + `prepare_news_cache` | full frame | tail only |
| `resample` to M15 | full frame | tail only |
| `detect_order_blocks` | from bar 0 | resume from checkpoint (L2) |
| `tag_obs_news` | all 2,080 OBs | new OBs only (L1) |
| `execute_scenario_job` | from bar 0 | resume from checkpoint (L3) |
| `diff_frontier` | already incremental | unchanged |

**Cannot be skipped, ever:** the guard checks; the durable atomic commit;
`verify_engine`; publication.

## 6. What invalidates cached state

Ordered by likelihood, not severity.

| Trigger | Detection | Response |
|---|---|---|
| New bar appended | `input_revision` differs, frozen prefix still matches | **normal path** — extend from checkpoint |
| Engine code change | `engine_version` differs | full replay (and `verify_engine` refuses an unpinned version outright) |
| Detection/simulation parameter change | `params_digest` differs | full replay |
| Frozen history rewritten (not appended) | cached prefix is no longer a byte-prefix | full replay |
| Window start moved | cached first-bar time differs | full replay |
| News CSV changed | news-artefact digest differs | replay from the earliest affected bar; conservatively, full replay |
| Checkpoint schema version change | `schema_version` differs | discard cache, full replay |
| Corrupt/unreadable checkpoint | integrity check fails | discard cache, full replay |
| Crash mid-write | atomic-rename protocol means the old checkpoint survives intact | resume from the last good checkpoint |

**The news case deserves care.** The news CSV *was* modified in the working tree
during this milestone. News affects `prepare_news_cache`, `tag_obs_news` and
flatten/blackout behaviour deep inside the walk. A retroactive calendar
correction can change historical trade outcomes, so it cannot be treated as
append-only without proof. Until that proof exists, **any change to the news
digest should force a full replay.**

## 7. What requires replay

Replay = discard all cached state and recompute from bar 0. It is always correct
and costs one full cycle (256 s measured on this hardware).

Required on: any L0 guard failure; any integrity failure; any schema-version
change; first run after deployment; and **on operator demand** — there must be a
supported way to force a clean rebuild without deleting files by hand.

**Replay must remain a first-class, routinely-exercised path, not an emergency
one.** If replay is rare it will rot; the shadow/parity harness should run it
regularly so that the fallback is known to work on the day it is needed.

## 8. Correctness gates before any of this ships

Non-negotiable, and all of them already have infrastructure in the repo:

1. **Byte-identical parity.** A resumed run must reproduce the from-scratch trade
   frame exactly — same rows, same order, same `ob_id`/`trade_id`. The
   `golden/run-001/parity-oracle/` harness already does this comparison.
2. **Determinism unchanged.** `live.benchmark` verifies byte-identical trade
   frames across iterations by default; it must still pass.
3. **Identity stability.** After a resume, every `intent_id` for an unchanged
   transition must be unchanged — otherwise duplicate suppression silently
   breaks (`ORDER-BLOCK-IDENTITY.md` §6.1).
4. **Guard negative controls.** Deliberately corrupt each guard input in turn
   (bump `engine_version`, perturb a parameter, rewrite a historical byte, move
   the window start) and assert a **full replay** results — never a cache hit.
   Experiments B and C in the identity spec are the templates.
5. **Crash injection.** Kill the process between checkpoint write and rename;
   assert the previous checkpoint is intact and usable.

Gate 4 is the one that matters most. Gates 1–3 catch a cache that is *wrong
today*; gate 4 catches a cache that will become wrong *later*, which is the
failure mode that reaches production.
