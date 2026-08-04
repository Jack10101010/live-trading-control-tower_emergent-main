# PENDING-HOT-PATH-AUDIT.md — M-CAP-OPT-1 Phase 3

**Status:** audit complete. **Phase 4 (index implementation) was NOT started —
a semantic blocker was found and is reported here rather than forced.**

Source: `strategy_core/execution.py` at Lux `d978074` (engine_version
`5bb6372c…`, matching its pin). Measurements from `evidence/cap3-hotpath.json`
committed in `dfd3e7d`.

---

## 1. What the 133 M checks actually are

The loop is a **filter-rebuild**, not in-place mutation
(`strategy_core/execution.py:2479`):

```python
next_pending = []
for item in pending:
    _profile_increment(profile, "pending_checks")     # ← the 133,069,753
    ...
    next_pending.append(item)      # survive
    # or: trades.append(row); continue      # resolve (fill / cancel)
pending = next_pending
```

Measured: **133,069,753** pending checks against **4,271,746** candle
iterations — 31.15 pending orders examined per candle.

## 2. Reader/writer map

| Field | Read by | Written by | Mutable in loop? |
|---|---|---|---|
| `item["ob"]["top"]`, `["bottom"]`, `["direction"]` | `_ob_depth`, `_penetration_price`, `_is_filled`, `_update_pending_metrics` | — | **No** |
| `item["plan"]["entry"]` | `_is_filled`, metrics, inlined fill expr | — | **No** (PERF-F comment confirms) |
| `item["plan"]["trigger_penetration_pct"]` | `_triggered_edge_touched` | — | **No** |
| `item["plan"]["stop"]` | `_exit_outcome` | — | No |
| `item["plan"]["tp"]`, `["rr_multiple"]` | `_exit_outcome` | RR/protection logic | **Yes** |
| `item["metrics"]["max_distance_away_before_fill_price"]` | `_apply_pending_metrics` | `_update_pending_metrics` | **Yes — accumulator** |
| `item["triggered_edge_armed"]`, `trigger_time`, `trigger_candle_index`, `armed_at` | fill gate | arming branch | **Yes — latched** |
| `item["news_paused"]`, `rearmed_at` | news branch | news branch | **Yes** |
| `item["revisit_confirmed"]`, `require_revisit_confirmation` | fill gate | revisit branch | **Yes** |
| `active_trades` | `can_fill` (`:2637`) | `active_trades.append` (`:2943`) — **inside the same loop** | **Yes — cross-candidate** |

## 3. The ten questions

| # | Question | Answer |
|---|---|---|
| 1 | Partition by direction? | **Yes.** Every predicate branches on `ob["direction"]`, which is immutable. |
| 2 | Index by price range without changing first-match ordering? | **Yes in principle.** Each predicate reduces to a constant per-item threshold — see §4. |
| 3 | Same index for fill and invalidation? | **Yes.** Both are thresholds on `candle["low"]` (bullish) / `candle["high"]` (bearish). |
| 4 | Does evaluation order affect output? | **YES.** `can_fill` reads `active_trades`, which is appended to at `:2943` inside the loop. Under `single_position` / `one_per_direction`, whether a later item can fill depends on whether an earlier item just filled **on the same candle**. |
| 5 | Can more than one candidate trigger on one candle? | **Yes** under `allow_multi_position` (the Golden mode). |
| 6 | Can processing one candidate mutate another's eligibility? | **YES** — same mechanism as Q4. |
| 7 | Cross-trade / portfolio-policy effects? | Yes — `portfolio_emit`, `state_policy_emit`, `regime_emit` are wired into the same walk. |
| 8 | Is insertion order semantically observable? | **YES** (Q4/Q6). Any index MUST yield candidates in original list order. |
| 9 | Can pending geometry change after insertion? | Entry/stop/OB geometry: **no**. `tp`/`rr_multiple` and latched flags: **yes**. The indexable thresholds are all immutable. |
| 10 | What accounts for the repeated `dict.get`? | 1,907,495,402 calls ≈ 14 per pending check: `item["ob"]`, `item["plan"]`, then per-predicate re-reads of `top`/`bottom`/`direction`/`entry`/`trigger_penetration_pct`, plus `item.get(...)` for each latched flag in the inlined fill expression. Candle and OB rows are plain dicts (`to_dict("records")`). |

## 4. The predicates are genuinely indexable

All reduce to a constant threshold per item, monotone in one candle field:

| Predicate | Bullish | Bearish |
|---|---|---|
| `_is_filled` (`:1661`) | `candle.low <= entry` | `candle.high >= entry` |
| `_triggered_edge_touched` (`:998`) | `candle.low <= top − depth·(thr/100)` | `candle.high >= bottom + depth·(thr/100)` |

`entry`, `top`, `bottom`, `depth` and `thr` are all immutable per item, so both
thresholds are fixed at insertion. A price-bucket or sorted-boundary index over
those thresholds would return an exact superset with the predicates remaining
the final authority — satisfying the Phase 4 design constraints, and order can
be preserved by yielding in original list order.

**So the predicates are not the blocker.**

## 5. THE BLOCKER — the metric accumulator forbids candidate omission

`_update_pending_metrics(item, candle)` (`:1255`) runs for essentially every
surviving pending item on every candle:

```python
if ob["direction"] == "bullish":
    distance = max(0.0, float(candle["high"]) - float(plan["entry"]))
else:
    distance = max(0.0, float(plan["entry"]) - float(candle["low"]))
metrics["max_distance_away_before_fill_price"] = max(
    metrics["max_distance_away_before_fill_price"], distance)
```

This is a **path-dependent running maximum over every candle the order is
pending**, and it is exported on the trade row by `_apply_pending_metrics`
(`:1270`) as three columns:

* `max_distance_away_before_fill_price`
* `max_distance_away_before_fill_pips`
* `max_distance_away_before_fill_r`

**Measured call count: 133,068,546** — essentially one per pending check.

Critically, it is driven by the **opposite** candle field from the fill/trigger
predicates: bullish fills read `candle.low`, but the bullish metric reads
`candle.high`. So a candle that provably cannot fill or trigger an item can
still advance that item's metric.

**Consequence:** an index that skips candidates whose predicates cannot fire
would omit metric updates and change exported trade columns. The Phase 5
equivalence bar is byte-identical output with *no tolerance for economic fields
or identifiers*. The designed optimisation — omitting candidates from the scan —
is therefore **not semantically safe**, and was not implemented.

This is the "first semantic blocker" the milestone directed me to report rather
than force.

### 5a. A reformulation exists, but is not free

Because `max(0, high − entry)` is monotone increasing in `high`, the running max
over a window equals `max(0, (max high over the window) − entry)` — computable
in O(1) at resolution time from a prefix-max of highs (bullish) / prefix-min of
lows (bearish), with no per-candle visit.

**But the contributing candle set is not simply "every candle while pending".**
Measured: 133,069,753 pending checks vs 133,068,546 metric updates — a
**1,207-pair discrepancy**. Some branch resolves an item without a final metric
update. Reproducing the metric exactly requires reproducing that exception set
exactly; getting it wrong silently changes exported columns on ~1,207 occasions
across the run.

That is tractable but is a **separate, carefully-scoped piece of work with its
own correctness proof** — not a step to be folded into an indexing change.

## 6. Revised expectations

Two options, both narrower than the roadmap's original 25–43 % projection.

**Option A — predicate gating only** (keep the per-candle metric visit). Index
gates only `_triggered_edge_touched`/`_penetration_price`/`_ob_depth`/`_is_filled`.
Under cProfile those total ≈122 s of a 583.5 s profiled call (≈21 %); deflated
against the real 187.7 s phase that is **≈39 s, ≈15 % of the 256.6 s cycle**
[PROJECTION, from measured proportions — assumes near-total gating and uniform
cProfile deflation].

**Option B — Option A plus metric reformulation.** Removes the per-candle visit
entirely and approaches the original projection, but requires the §5a exception
proof.

Recommendation: **A first, measured, then decide on B.** A is safe with the
existing equivalence harness; B needs its own.

## 7. What was NOT done, and why

No index was implemented. Implementing Option A would have meant either
(a) shipping the designed candidate-skipping index, which is provably
output-changing, or (b) silently substituting a materially different design
mid-milestone. The milestone's Phase 3 instruction is explicit: *"If
fill/invalidation predicates cannot be safely separated or indexed, stop and
report the first semantic blocker rather than forcing an optimisation."*

The predicates *can* be indexed; the **candidates cannot be omitted**. That
distinction is the finding, and it changes the design — so it belongs in front
of a human before code is written against it.
