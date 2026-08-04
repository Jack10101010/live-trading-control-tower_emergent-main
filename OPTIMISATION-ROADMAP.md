# OPTIMISATION-ROADMAP.md — implementation order

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 8)
**Status:** roadmap only. **No optimisation was implemented in this milestone.**

Every milestone below is independently committable, independently revertible,
and ordered by measured runtime reduction per unit of risk.

---

## 0. Evidence tags

| Tag | Meaning |
|---|---|
| **[M]** MEASURED | From a run in this milestone; artefact under `evidence/`. |
| **[J]** JUDGEMENT | Engineering assessment from reading current source. Not measured. |
| **[P]** PROJECTION | Arithmetic extrapolation from [M]. Assumption stated inline. |

No projection below is presented as a measurement. Where a saving cannot be
bounded from evidence, it is left as a range with its reasoning, or marked
"unquantified".

---

## 1. What the evidence actually says

**[M]** Mean cycle 256.57 s (n=5, σ=3.70 s, spread 3.4 %), determinism PASS.

**[M]** Phase concentration:

| Phase | s | % |
|---|---:|---:|
| `execute_scenario_job` | 187.71 | 73.16 |
| `detect_order_blocks` | 26.78 | 10.44 |
| `tag_obs_news` | 25.49 | 9.93 |
| *(top three)* | *239.98* | *93.53* |
| everything else | 16.59 | 6.47 |

**[M]** Inside the 73 % phase: 4,271,746 candle iterations but **133,069,753
pending checks** (31.15 per candle), 133,069,753 fill checks, 132,654,597
invalidation checks — **≈266 M inner operations**, producing just **2,060 trade
rows**.

**[M]** Per-check overhead: **1,907,495,402 `dict.get` calls** (~14 per pending
check) and **395,721,513 `max()` calls** (~3 per pending check).

**[M]** Single-threaded: mean CPU 98.9 %, never above 100 %. Peak RSS 3,085 MiB.

**[M]** `ob_id` is stable under append-only extension (2,077 shared ids, 0
mismatches) and unstable under window-start change (2,042 of 2,042 changed) or
parameter change (296 changed).

**The conclusion the evidence forces:** the cost is not the size of the dataset.
It is (a) rescanning an unindexed pending list 133 M times and (b) paying ~17
dictionary/`max` operations per rescan. Both are addressable **without changing
what is computed** — which is essential, because this engine's contract is
byte-identical reproducibility.

---

## 2. Ordering principle

Highest measured runtime reduction first, **but** with a hard precondition:
anything that changes *what* is computed, or that persists derived state, must
come after the guard that makes such reuse safe. The identity experiments showed
the failure mode is silent.

So: cheap in-phase constant-factor wins first (no persistence, no new state),
then the guard, then structural caching.

---

## 3. The roadmap

### M-CAP-OPT-1 — Pending-list indexing
**Target:** `execute_scenario_job` inner rescan
**Evidence:** [M] 133,069,753 pending checks for 4,271,746 candles (31.15×)

> **UPDATE 2026-08-04 — this milestone was attempted and BLOCKED at Phase 3.**
> The audit (`PENDING-HOT-PATH-AUDIT.md`) found that the fill/trigger predicates
> *are* cleanly indexable — each reduces to a constant, immutable per-item price
> threshold — but that **candidates cannot be omitted from the scan**:
> `_update_pending_metrics` is a running-max accumulator that runs on every
> candle, feeds three exported trade columns, and reads the *opposite* candle
> field from the fill predicates. Skipping a candidate whose predicate cannot
> fire would still change its exported metric.
>
> Revised options: **A** — gate only the predicates, keep the per-candle metric
> visit, ≈15 % [PROJECTION]; **B** — additionally reformulate the metric as a
> prefix-max (mathematically exact, since `max(0, high−entry)` is monotone in
> `high`), approaching the original range but requiring an exact account of the
> measured 1,207-pair gap between pending checks and metric updates.
>
> The 25–43 % figure below was written before this audit and is **not**
> achievable by indexing alone. No index was implemented. See
> `OPEN-RISKS.md` R-2/R-3/R-4.

Today every candle re-examines the entire pending list, checking fill and
invalidation for each entry. The measured amplification is 31×.

Replace the linear rescan with a structure that surfaces only the pending orders
a given candle could plausibly affect — e.g. price-ordered buckets keyed on the
trigger/invalidation levels, so a candle's `[low, high]` range selects a slice
rather than the whole list.

* **Changes what is computed?** No. Identical predicates, identical order of
  evaluation among the orders that *can* match — only the candidates examined
  change.
* **Expected saving [P]:** if the examined set drops from ~31 to ~2–4 per candle,
  the inner-operation count falls by roughly 85–90 %. Applied to the ~120 s of
  `tottime` that the profile attributes to `_update_pending_metrics`,
  `_triggered_edge_touched`, `_penetration_price`, `_ob_depth` and `_is_filled`,
  that is **60–110 s off a 256 s cycle (25–43 %)**. Assumption: the predicates
  are separable by price range — **must be verified in source before committing
  to a number.** If some predicate depends on *all* pending orders, the saving
  shrinks toward zero for that predicate.
* **Risk [J]:** medium. Confined to one module; fully covered by the parity
  oracle.
* **Gate:** byte-identical trade frame vs the pinned Golden output.

### M-CAP-OPT-2 — Hot-path representation
**Target:** the ~14 `dict.get` + ~3 `max()` per pending check
**Evidence:** [M] 1.91 B `dict.get`, 396 M `max()` in one cycle

Candle and OB rows are plain dicts (`to_dict("records")`), so each predicate
re-reads its inputs by string key on every one of 133 M checks. Hoist the fields
each predicate needs into locals/tuples/slots once per candle (or once per
pending order at creation), and replace repeated two-argument `max()` with
direct comparisons.

* **Changes what is computed?** No — pure representation and lookup change.
* **Expected saving [P]:** the profile attributes 129.6 s `tottime` to `dict.get`
  and 34.2 s to `max` **under 2.68× cProfile inflation**; deflated, order
  **50–60 s combined**. Realistically recovering half gives **25–35 s (10–14 %)**.
  Assumption: deflation is uniform across call sites — approximate, and the
  reason this is a range.
* **Risk [J]:** low-medium. Mechanical but touches the hottest code; easy to
  introduce a subtle read-order change.
* **Gate:** byte-identical trade frame; benchmark determinism PASS.
* **Note:** partially overlaps M-CAP-OPT-1 — indexing removes checks, this makes
  the survivors cheaper. **Re-measure between the two; do not add the savings.**

### M-CAP-OPT-3 — Engine cache guard (no caching yet)
**Target:** none — this is the safety prerequisite
**Evidence:** [M] identity Experiments B and C

Implement `params_digest`, the window-start guard, and the frozen-**prefix**
hash, per `PERSISTENCE-DESIGN.md` §3.1–3.2. Wire them into telemetry. Cache
nothing; simply compute and publish whether a cache *would* have been valid.

* **Expected saving:** **zero, by design.**
* **Why it is ranked here [J]:** it is the precondition for OPT-4/5/6, and
  shipping it alone means the riskiest logic runs in shadow — accumulating
  evidence about how often the guard would have held in production — before any
  behaviour depends on it. This mirrors the shadow-mode approach that worked for
  close accounting.
* **Gate:** negative controls — perturb each guard input in turn (bump
  `engine_version`, change a parameter, rewrite a historical byte, move the
  window start) and assert **invalid** every time. Experiments B and C are the
  templates.

### M-CAP-OPT-4 — Order-block + news-tag cache
**Target:** `detect_order_blocks` 26.78 s + `tag_obs_news` 25.49 s = **20.4 %**
**Evidence:** [M] Experiment A — append-only extension leaves ids untouched

With the guard from OPT-3, persist detected and news-tagged order blocks
(`PERSISTENCE-DESIGN.md` §3.3–3.4) and extend only the tail. Requires the L2
detection resume state, including the candle-retention watermark.

* **Expected saving [P]:** `tag_obs_news` should collapse to near zero — 2,080
  OBs in eleven years, so a 15-minute tail almost never adds one. `detect_order_blocks`
  resume should leave only tail work. Combined **40–50 s (16–20 %)**.
  Assumption: the retention watermark is computed correctly.
* **Risk [J]:** medium-high. The watermark is the sharp edge —
  `PERSISTENCE-DESIGN.md` §4 — because under-retention yields subtly different
  order blocks from perfectly valid-looking files.
* **Gate:** parity oracle; plus an explicit test that a resumed detection over an
  extended frame equals a from-scratch detection, id for id.

### M-CAP-OPT-5 — Per-candle transform cache
**Target:** `prepare_news_cache` 10.24 s + `prepare_candles` 3.26 s +
`filter_date_range` 2.55 s + `resample` 0.37 s = **6.4 %**

Cache these per-candle transforms and extend by the tail.

* **Expected saving [P]:** **12–16 s (5–6 %)**.
* **Risk [J]:** low — pure functions of the frame, already guard-protected.
* **[J]** Ranked below OPT-4 purely on size. It is the easiest of the caching
  milestones and a reasonable place to prove the cache machinery if OPT-4 looks
  too risky to go first.

### M-CAP-OPT-6 — Simulation checkpoint / resume
**Target:** the remainder of `execute_scenario_job`
**Evidence:** [M] 73.16 % of the cycle

Persist and resume the walk state (`PERSISTENCE-DESIGN.md` §3.5).

* **Expected saving [P]:** in principle the walk reduces to the new tail —
  potentially **>90 % of whatever remains** after OPT-1/2. **Unquantified here:**
  the residual depends entirely on OPT-1 and OPT-2 outcomes, and quoting a
  number now would be a guess.
* **Risk [J]:** **highest in the roadmap.** Everything downstream — realised R,
  the frontier diff, intent identity — depends on the walk being byte-identical.
  Requires pickle or an explicit typed serialisation (§3.5 warning).
* **[J] Recommendation: do not start this until OPT-1 and OPT-2 have shipped and
  been re-measured.** If those two land at the top of their ranges, the cycle may
  already sit near 120–150 s on this hardware, with a 6–7× margin against the
  900 s bar interval — at which point this milestone's risk may no longer be
  worth its saving. **That decision should be made against fresh measurements,
  not against this document.**

### M-CAP-OPT-7 — Idle-cycle read amplification
**Target:** IO on fast-idle cycles
**Evidence:** [M] 263 MiB read per cycle, including idle ones

Every cycle re-reads and re-hashes 263 MiB to compute `input_revision`, even when
the C4 memo will immediately declare "nothing changed". At a 10 s cadence that is
~26 MiB/s of sustained IO to discover nothing.

Hash incrementally over the appended tail, retaining a rolling prefix digest —
preserving the existing property that the revision describes the exact bytes.

* **Expected saving [P]:** near-zero CPU on this hardware (the measured interval
  excludes IO). Potentially **material on the VPS** if its disk is slow —
  **unquantified, and dependent on the §9 diagnostic.**
* **Risk [J]:** medium — touches `input_revision`, which is a correctness gate,
  not a cache.
* **[J]** Ranked last deliberately: on measured evidence it saves nothing here.
  It moves up sharply if the VPS diagnostic shows IO-bound idle cycles.

### M-CAP-GUARD-1 — Identity-rebase guard *(correctness, not performance)*
**Target:** the silent-desynchronisation risk in `ORDER-BLOCK-IDENTITY.md` §6.1

If `ob_id`s were ever rebased while positions were open, every `trade_id` would
change at once: `diff_frontier` would see all old trade_ids vanish (emitting no
`CLOSE_POSITION`, so live positions stop being mirrored) and all new ones appear
(potentially emitting `OPEN_POSITION` with intent_ids the ledger has never seen).

`input_revision` detects that inputs changed and forces re-evaluation, but
nothing detects that the *identity space* was rebased.

Proposed: before acting on a diff, assert that trade_ids for currently-mirrored
positions still exist in the new frame; if a large fraction vanished
simultaneously, **freeze and alert** rather than trade.

* **Expected saving:** none — this is a safety guard.
* **Risk [J]:** low. Additive check on an existing decision point.
* **[J]** Listed last but **should ship early** — arguably alongside OPT-3, since
  both concern identity integrity and neither changes trading behaviour in the
  normal case. It is the only item here that protects money rather than time.

---

## 4. Sequencing

```
OPT-1 ──► re-measure ──► OPT-2 ──► re-measure ──┐
                                                 │
GUARD-1 ─────────────────────────────────────────┤ (independent, ship early)
                                                 │
OPT-3 (guard, shadow) ──► OPT-5 ──► OPT-4 ───────┤
                                                 │
                                                 └──► decide on OPT-6 with fresh data
OPT-7 — reprioritise after the VPS diagnostic
```

**Re-measure between every milestone.** The savings above are not additive:
OPT-1 removes checks that OPT-2 would have made cheaper, and OPT-4/5 remove work
whose share changes once OPT-1/2 land. Any figure computed by summing this
roadmap is wrong.

## 5. Honest summary of expected outcome

| Milestone | Class | Expected reduction | Confidence [J] |
|---|---|---|---|
| OPT-1 | constant factor | 25–43 % | medium — depends on predicate separability |
| OPT-2 | constant factor | 10–14 % | medium — cProfile deflation is approximate |
| OPT-3 | guard | 0 % | high — no saving by design |
| OPT-4 | structural | 16–20 % | medium |
| OPT-5 | structural | 5–6 % | high — pure functions |
| OPT-6 | structural | unquantified | low — decide with fresh data |
| OPT-7 | IO | unquantified | low — needs the VPS diagnostic |
| GUARD-1 | safety | 0 % | high |

**[P]** OPT-1 + OPT-2 alone plausibly take the measured 256.57 s cycle to roughly
**140–190 s on this hardware**, without persisting any derived state, without a
cache, and without changing what is computed. That is the cheapest and safest
half of the available win, and it is where implementation should start.

**The margin question remains open.** On Apple silicon the current engine uses
28.5 % of the 900 s bar interval — a 3.5× margin. **On the VPS the margin is
unknown**, and the indirect evidence in `CAPACITY-BASELINE.md` §7 is consistent
with it being negative. Until the read-only VPS diagnostic
(`CAPACITY-BASELINE.md` §9) is run, **no one should conclude either that this
roadmap is urgent or that it is unnecessary** — the Mac numbers do not settle it.

## 6. Explicitly out of scope

Recorded so they are not silently assumed:

* **Parallelism.** The engine is single-threaded [M]. Multi-core or vectorised
  execution could plausibly beat everything above, but it would change execution
  order and threaten byte-identical reproducibility. Not proposed here.
* **Rewriting the walk in a compiled language / numba.** Same reproducibility
  concern, much larger blast radius.
* **Reducing the dataset.** Would change results. Not an optimisation.
* **Widening the 900 s freshness budget.** Hides the problem rather than fixing
  it — the same reasoning applied to the dashboard staleness warning in
  M-ACTIVATION-STABILISE-1.
