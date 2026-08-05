# CAPACITY-EVIDENCE.md — M-CAP-OPT-2

**Milestone:** M-CAP-OPT-2 — safe predicate gating + R-3 characterisation
**Baseline:** `dfd3e7d` (`docs/capacity/CAPACITY-BASELINE.md`) — **not rewritten here**

Evidence classes are the same as the baseline: **[M]** measured in this
milestone, **[J]** engineering judgement, **[P]** projection.

---

## 1. Why broad candidate indexing was rejected

Recorded in full in `PENDING-HOT-PATH-AUDIT.md` (committed at `8af25d7`).
Summary: the fill/trigger predicates *are* cleanly indexable — each reduces to a
constant, immutable per-item price threshold — but **candidates cannot be
omitted from the scan**, because `_update_pending_metrics` is a running-max
accumulator that runs on essentially every candle, feeds three exported trade
columns, and reads the *opposite* candle field from the fill predicates (bullish
fill tests `candle.low`; the bullish metric accumulates on `candle.high`).

Skipping a candidate whose predicate cannot fire would still change its exported
metric. So the index was not built. What was built instead is the narrower,
provably safe change below.

## 2. The Option A gate, exactly

**One predicate, one call site.** `_triggered_edge_touched` was the hottest
function in the baseline — 129,374,023 calls, and through it 129,505,521
`_penetration_price` and 129,379,463 `_ob_depth` calls [M, `dfd3e7d`]. It has
exactly one call site (`strategy_core/execution.py:2516`).

Original:

```python
def _triggered_edge_touched(ob, plan, candle):
    threshold = plan.get("trigger_penetration_pct", "")
    if threshold == "" or threshold is None:
        return False
    depth = _ob_depth(ob)                       # float(top) - float(bottom)
    if depth <= 0:
        return False
    return _penetration_price(ob, candle) >= depth * (float(threshold) / 100.0)
```

Every operand it reads is **immutable for the life of the pending item** —
verified by exhaustive grep: no assignment to `ob["top"]`, `ob["bottom"]`,
`ob["direction"]` or `plan["trigger_penetration_pct"]` exists anywhere in the
module. (`plan["tp"]` and `plan["rr_multiple"]` *are* mutated; neither is read
by this predicate.)

So `_te_prepare(ob, plan)` computes, **once per pending item**, exactly the same
sub-expressions with the same operands in the same order:

```python
depth    = float(ob["top"]) - float(ob["bottom"])   # identical to _ob_depth
required = depth * (float(threshold) / 100.0)       # identical to the RHS
```

and `_te_eval(te, candle)` performs the remaining comparison inline:

```python
max(0.0, te["top"] - float(candle["low"])) >= te["required"]      # bullish
max(0.0, float(candle["high"]) - te["bottom"]) >= te["required"]  # bearish
```

### Semantic proof

1. **No float operation is reordered, introduced or removed.** The cached
   `depth`/`required` are produced by the identical expressions on identical
   operands; IEEE-754 is deterministic for fixed operands, so the cached values
   are *bit-identical* to the recomputed ones. Asserted directly by comparing
   `.hex()` representations in `tests/test_te_gate_equivalence.py`.
2. **Both early-`False` branches are preserved.** `_te_prepare` returns `None`
   exactly when `threshold` is `""`/`None` or `depth <= 0`, and `_te_eval(None,
   …)` is `False`.
3. **The `max(0.0, …)` is retained**, not algebraically eliminated. Removing it
   would have been provably equivalent for `required > 0`, but it was left in
   place to keep the operation sequence literally identical.
4. **Direction dispatch is copied, not corrected.** `_penetration_price` treats
   anything that is not `"bullish"` as bearish; the gate does the same, and a
   test pins that behaviour rather than "fixing" it.
5. **Nothing else is touched.** No candidate is skipped, no metric update is
   skipped, no lifecycle mutation, invalidation, ordering, precedence or
   representation is changed. `_update_pending_metrics` still runs exactly as
   before — proven by the hot-path counters being identical (below).

### Reference mode

`TE_GATE_ENABLED` (module-level, default `True`) selects the path; `False`
calls the original `_triggered_edge_touched`, unchanged. The flag is bound once
per `simulate_trades` call into a local, so a mid-run change cannot split one
simulation across both paths. `_te` is always prepared regardless of mode, so
reference mode is a pure *evaluation-path* switch that cannot drift from
creation state.

## 3. Equivalence result [M]

Full canonical dataset, both modes in one process over identical inputs
(`evidence/opt2-equivalence.json`):

| Check | Result |
|---|---|
| Trades frame SHA-256, reference | `b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e` |
| Trades frame SHA-256, optimised | `b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e` |
| **Frames byte-identical** | **yes** |
| Rows | 2,060 = 2,060 |
| Column order identical | yes |
| Per-field mismatches (24 fields incl. `trade_id`, `ob_id`, `entry`, `stop`, `tp`, `fill_time`, `exit_time`, `outcome`, `pnl_r`) | **0** |
| The three `_update_pending_metrics` columns | **identical** |
| Hot-path counters (all 8) | **identical, delta 0** |

Counter identity is the strongest single signal: `pending_checks`,
`fill_checks`, `invalidation_checks`, `candle_iterations`, `base_row_creations`,
`news_*` and `active_checks` are all unchanged, which means the optimised path
executed the *same number of the same semantic operations* — it only made each
cheaper.

Unit/property level (`tests/test_te_gate_equivalence.py`,
`tests/test_te_gate_adversarial.py`, 44 tests): 300,000 randomized comparisons
against the original as oracle, exactly-representable boundary cases
(`top=1.5, bottom=1.0, thr=50%` → `required=0.25`, `1.5−1.25` exact), one-ULP
either side of the boundary, degenerate depth, absent threshold, NaN/±inf
prices, string-typed prices, and unknown direction — zero mismatches.

## 4. R-3 exception taxonomy [M] — CLOSED

Full detail in `OPEN-RISKS.md` R-3. The 1,207-call gap is exactly the pending
checks that resolve through the **fill branch** (the only resolution path with
no `_update_pending_metrics` call):

| Component | Count |
|---|---:|
| filled (WIN 441 + LOSS 194 + NEWS_FLATTEN 15) | 650 |
| STATE_BLOCKED | 457 |
| REGIME_BLOCKED | 100 |
| **total** | **1,207** ✓ |

Cross-check: 1,207 fill-branch rows + 853 other rows = 2,060 =
`base_row_creations`. The accounting closes with no residual.

Semantically correct: the metric is *max distance away **before fill***, so the
fill candle contributes nothing.

## 4a. A measurement that was taken, rejected, and redone

Recorded because the discarded numbers are more instructive than the final ones,
and because they would otherwise look like a 32 % win.

**First attempt (INVALID).** Two sequential `live.benchmark` runs, reference then
optimised, 5 measured iterations each, determinism PASS in both:

| | reference | optimised | apparent |
|---|---:|---:|---|
| mean total | 261.30 s | 176.02 s | **−32.6 %** |
| p50 total | 262.62 s | 172.46 s | −34.3 % |

**Why it was rejected.** Phases the change *cannot* touch improved too:

| phase | apparent gain | can Option A affect it? |
|---|---:|---|
| `execute_scenario_job` | −35.97 % | **yes** — the only phase it can |
| `resample` | −32.99 % | no |
| `tag_obs_news` | −26.22 % | no |
| `filter_date_range` | −23.66 % | no |
| `prepare_news_cache` | −22.97 % | no |
| `prepare_candles` | −22.93 % | no |
| `load_news_calendar` | −22.01 % | no |
| `detect_order_blocks` | −20.82 % | no |

The change lives entirely inside `simulate_trades`; it cannot make
`load_news_calendar` 22 % faster. The uniform ~21–33 % lift across unrelated
phases is machine drift — the Lux test suites were run concurrently during the
reference window, inflating it. Attributing that to the optimisation would have
overstated the result by roughly threefold.

**Second attempt (the one reported).** Interleaved A/B on
`execute_scenario_job`: pre-stages built once, modes alternating
ref/opt/ref/opt so monotonic drift hits both arms equally, one warm-up pair
discarded, trade-frame SHA-256 verified on every iteration so a divergence
cannot hide inside a timing run, hot-path counters off, and nothing else running
on the machine.

## 4b. Benchmark result [M]

Interleaved A/B, 5 pairs + discarded warm-up pair, nothing else running
(`evidence/opt2-ab-report.json`). Every one of the 12 runs produced trade frame
SHA-256 `b43e3248…` — identical to the equivalence run, so no timing iteration
hid a divergence.

| | reference | optimised | saving |
|---|---:|---:|---|
| mean | 136.27 s | 121.02 s | **15.25 s (11.19 %)** |
| p50 | 135.83 s | 120.01 s | 15.82 s (11.65 %) |
| min | 135.56 s | 119.74 s | |
| max | 138.03 s | 125.34 s | |
| stdev | 1.02 s | 2.42 s | |

**These are `execute_scenario_job` timings in a warm, repeated-call context
(pre-stages built once and reused), not the 187.71 s the baseline measures for
that phase inside a full cold pipeline.** The percentage above is therefore a
percentage *of this harness's phase time*, and must not be quoted as an
end-to-end figure.

### Converting to end-to-end [P]

The optimisation removes a **fixed count** of operations — roughly 129.4 M ×
(3 function calls + ~6 dict lookups + 1 division + 1 multiplication). That work
does not scale with cache warmth, so the **absolute** saving is the more
transferable quantity:

* **Absolute basis:** 15.25 s off the baseline's 256.57 s cycle → **5.94 %**.
* **Proportional basis** (if the saving instead scales with the phase's cold
  187.71 s): 11.19 % × 73.16 % → **8.19 %**.

**End-to-end saving is therefore 5.9 – 8.2 %, and the absolute basis is the
better-founded of the two.**

## 4c. Retention decision — RETAIN, but NOT on the ≥10 % clause

The gate: retain only if parity is exact **and** either (i) end-to-end saving
≥ 10 %, or (ii) the implementation is exceptionally small and the hot-path call
reduction is materially valuable.

| Condition | Verdict |
|---|---|
| Parity exact | **YES** — byte-identical frames across 12 independent runs, 24 fields zero mismatches, all 8 counters delta 0, 44 unit/property/adversarial tests incl. 300 k randomized comparisons |
| (i) end-to-end ≥ 10 % | **NO — 5.9–8.2 %.** Stated plainly; the earlier 32.6 % figure was drift and is rejected (§4a) |
| (ii) exceptionally small + valuable call reduction | **YES** — 73 lines added / 2 removed, one file, one call site, one-line reference-mode rollback; removes ≈388 M function calls and ≈129 M dict lookups per cycle, worth a measured 15.25 s |

**Retained under clause (ii), explicitly not clause (i).** The change is small
enough to review in one sitting, provably output-identical, and trivially
reversible; the measured 15.25 s/cycle is real even though it falls short of
10 % end-to-end.

## 4d. VPS projection [PROJECTION — NOT measured]

VPS median **1,387 s** [M, operator diagnostic], CPU-bound, ≈5.41× slower than
this Mac on identical work.

If the absolute saving scales with machine slowness (CPU-bound, same
instruction mix): 15.25 s × 5.41 ≈ **82 s**.

| | value |
|---|---|
| VPS median today [M] | 1,387 s |
| Projected median after Option A [P] | **≈ 1,275 – 1,305 s** |
| Budget (bar interval) | 900 s |
| Overrun today [M] | **1.54×** |
| Projected overrun [P] | **≈ 1.41 – 1.45×** |

**Option A does not fix the overrun.** It moves the median from ~1.54× to
~1.43× of budget; the engine still cannot keep up with the 15-minute boundary,
and the 36 % skipped-boundary figure would improve only marginally. This is a
projection from a Mac measurement and **must not be reported as a VPS result**;
only a VPS-side `ops/cycles.jsonl` re-measurement can confirm it.

## 5. Governance — `engine_version` did NOT move, and that is a finding

| | |
|---|---|
| Old `engine_version` | `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` |
| New `engine_version` | `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` |
| Changed? | **No** |
| Re-pin required? | **No** |

**But the reason is a defect, not safety.** `_ENGINE_VERSION_SOURCES`
(`src/run_outputs.py:12`) hashes only `src/execution.py`,
`scripts/run_backtest.py` and `src/resume_support.py`. After the M2 relocation,
`src/execution.py` is a **102-line shim** while
`strategy_core/execution.py` — 3,920 lines containing `simulate_trades` — is
**outside the fingerprint entirely**, along with `order_blocks.py`,
`ghost_tracker.py`, `news.py`, `scenario.py`, `swings.py`, `policy.py` and
`regime.py`.

So `verify_engine()`'s promise to refuse "an unverified engine" is substantially
hollow today. This milestone demonstrates it: the trade walk was modified and
the fingerprint did not move.

Filed as **R-9 (HIGH)** and proposed as **M-CAP-GOV-1**. Not fixed here —
extending the hash set changes the pin for everyone and needs its own governed
milestone (new hash set, re-pin, `ENGINE_VERSION_EXPECTED` update, shadow run).

**Golden parity / shadow requirements for Option A:** the byte-identical trades
frame over the full canonical dataset (§3) is the parity evidence. Because the
fingerprint does not move, deploying Option A would *not* be gated by
`verify_engine` — which is precisely why it must not be deployed without the
explicit authorisation this milestone does not have.

**Rollback:** set `TE_GATE_ENABLED = False` (one line, reference path, no other
change), or revert the single engine commit.

## 6. Isolation

The engine change is on a **separate Lux branch and worktree**
(`capacity/m-cap-opt-2-engine` at `/Users/jack/Documents/Dev Projects/lux-cap-opt-2`,
branched from `d978074`). The production engine branch `golden-run-001-engine`
was **not modified** — verified byte-identical before and after (128 working-tree
entries, same 5 tracked files, `engine_version` matching its pin).

Both roots read byte-identical inputs: the candle CSV is a **hardlink** (same
inode; a symlink was correctly rejected by the repo's own
`validate_safe_relative_path` guard), and the news CSV, Golden config and
deployed policy were verified equal by SHA-256.

## 7. Current VPS evidence [MEASURED — operator-supplied diagnostic]

| Metric | Value |
|---|---|
| Median cycle | **1,387 s** |
| p95 cycle | **1,850.8 s** |
| Skipped boundaries | **36 %** |
| Profile | CPU-bound |

Against the 900 s bar interval: median is **1.54×** the budget, p95 **2.06×**.
This resolves the open question from `dfd3e7d` §7 — the runner *is* running
continuously and each recompute overruns; roughly one boundary in three is never
evaluated.

Mac median 256.57 s vs VPS median 1,387 s ⇒ the VPS is **≈5.41× slower** on
identical work.

**Telemetry timeouts are a separate operational issue and are not a capacity
result.** The engine figures above come from `ops/cycles.jsonl`; publication
gaps and node-freshness warnings must not be quoted as evidence about engine
runtime.
