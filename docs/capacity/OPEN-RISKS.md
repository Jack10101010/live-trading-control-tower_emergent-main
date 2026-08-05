# OPEN-RISKS.md — capacity/Model B workstream

Live register. Each entry states the evidence class and whether it is closed.

---

## R-1 — Order-block identity-space drift *(CLOSED by M-CAP-GUARD-1)*

**Was:** `ob_id` is window- and parameter-relative [MEASURED]. `intent_id`
derives from it and is the broker idempotency key. A rebase would emit no
`CLOSE_POSITION` for open positions (`diff_frontier` iterates `cur_frame` only)
while generating fresh `OPEN_POSITION` intents the ledger has never seen.

**Now:** `live/identity_guard.py` refuses continuity on key mismatch, coordinate
conflict, vanished prior trade, missing/corrupt stored key, or an unverifiable
frame. Refusal returns `identity_drift_frozen`: no intents, no durable advance,
no memo priming, and `executor.apply()` is unreachable — proven end to end in
`backend/tests/test_identity_guard_runner.py`.

**Residual:** the guard proves *numbering continuity*, not that the numbering is
*correct*. A rebase that coincidentally preserved every `trade_id`'s
`detection_time` and `direction` would pass. No such mechanism is known.

---

## R-2 — Pending-metric accumulator blocks candidate omission *(OPEN)*

`_update_pending_metrics` is a running maximum over every candle an order is
pending [MEASURED: 133,068,546 calls] and feeds three exported trade columns. It
reads the **opposite** candle field from the fill predicates (bullish metric
reads `high`; bullish fill reads `low`), so a candle that cannot fill an item can
still advance its metric.

**Impact:** the originally designed pending index — skipping candidates whose
predicates cannot fire — is **not semantically safe**. See
`PENDING-HOT-PATH-AUDIT.md` §5. Phase 4 was not implemented.

**Next:** Option A (predicate gating, keeps the per-candle visit, ≈15 %
[PROJECTION]) or Option B (adds metric reformulation, needs the §5a exception
proof). Decision required before implementation.

---

## R-3 — 1,207 metric-update omissions *(CLOSED by M-CAP-R3-1 — fully accounted)*

**Was:** pending checks 133,069,753 vs metric updates 133,068,546 — a 1,207
difference with no explanation.

**Now:** exactly one coherent cause, measured (`evidence/r3-report.json`). The
gap is precisely the set of pending checks that resolve through the **fill
branch** (`execution.py` 2644–2954), which is the only resolution path with no
`_update_pending_metrics` call:

| Component | Count |
|---|---:|
| filled trades (WIN 441 + LOSS 194 + NEWS_FLATTEN 15) | 650 |
| STATE_BLOCKED (portfolio-policy rejection inside the fill branch) | 457 |
| REGIME_BLOCKED (regime rejection inside the fill branch) | 100 |
| **total** | **1,207** ✓ |

Independent cross-check: 1,207 fill-branch rows + 853 non-fill-branch rows
(INVALID 809 + NEWS_TOUCH_CANCEL 9 + UNFILLED 35) = **2,060 = `base_row_creations`**.
The accounting closes exactly, with no residual.

**This is semantically correct, not a defect.** The metric is
`max_distance_away_before_fill`; on the candle where the order fills (or is
blocked at the point of filling) there is no "before fill" contribution to make.

### Phase 6 answers

1. **Is the contributing-candle set exactly reproducible?** **Yes** — every
   candle from creation while the item is pending, *excluding* the candle on
   which the fill branch produces a row.
2. **Can accumulation be a prefix maximum?** **Yes in principle.**
   `max(0, high − entry)` is monotone increasing in `high`, so the running max
   equals `max(0, (prefix-max high) − entry)` for bullish, and symmetrically
   `max(0, entry − (prefix-min low))` for bearish.
3. **Does same-bar mutation make prefix aggregation unsafe?** **No.** The metric
   reads only `candle["high"]`/`["low"]` and the immutable `plan["entry"]`;
   nothing another candidate mutates can affect it.
4. **Would Option B preserve exact bar counts and timestamps?** The metric is a
   price, and the two derived columns (pips, r) are pure functions of it and of
   immutable `pip_size`/`risk`. No bar count or timestamp is involved.
5. **Any candidate-specific window differing from ordinary pending lifetime?**
   **Yes — exactly one:** the fill-branch exclusion above.

**Consequence:** Option B is materially de-risked. The exception set is a single
class with an exact count, not an unknown. It still requires its own equivalence
proof and independent commit.

---

## R-9 — `engine_version` does not cover `strategy_core/` *(CLOSED by M-CAP-GOV-1)*

**Repaired** on the isolated Lux branch (`119f2d6`). A deterministic recursive
policy now governs **30 files** (12 `strategy_core/` + 17 `src/` + 1 driver) in
place of the curated 3-file list, pinned by 44 tests including the regression
that reproduces the exact Option A blind spot.

Identity moved from `5bb6372c…` (which the old policy still produced *with* the
trade walk modified) to `33e1a089…`. Golden parity confirmed unchanged:
`b43e3248…`, 2,060 rows.

**Residual — deliberate, and it is now the operative risk:** nothing is
deployed. `ENGINE_VERSION_EXPECTED` is still `5bb6372c…`, so `verify_engine()`
will *refuse* the repaired engine until a re-pin. That is the gate working, but
it means the repair delivers no protection in production until the re-pin
sequence in `governance/ENGINE-IDENTITY.md` §6 is executed. Until then the live
node continues to run under an identity that cannot see `strategy_core/`.

---

## R-10 — `_ENGINE_VERSION_SOURCES`-style drift can recur *(OPEN — low, structural)*

The repair removes the *current* blind spot, but the class of defect — a
governance list silently falling out of step with a code reorganisation — is
only prevented for files under the declared roots. A future relocation of
strategy code to a NEW top-level package (say `strategy_core2/` or `engine/`)
would escape coverage in exactly the same way.

Mitigations already in place: the roots are recursive, so anything added *inside*
them is captured automatically; an empty root fails closed; and the policy
literal is hashed, so changing the policy changes identity.

Not mitigated: a wholly new top-level package. A cheap future guard would be a
test asserting that every `.py` file reachable by import from
`scripts/run_backtest.py` is present in `engine_source_manifest()` — turning the
declared roots into a checked claim rather than an assumption.

---

## R-9-HIST — the original defect, retained for the record

**Found incidentally during M-CAP-OPT-2 Phase 9.**

`engine_version()` (`src/run_outputs.py:20`) hashes exactly three files:

```python
_ENGINE_VERSION_SOURCES = ("src/execution.py", "scripts/run_backtest.py",
                           "src/resume_support.py")
```

After the M2 slices relocated the strategy into `strategy_core/`, that set was
never updated. Measured today:

| File | In fingerprint? | Lines | Contains |
|---|---|---:|---|
| `src/execution.py` | **yes** | **102** | a shim: `import strategy_core.execution as _core_execution` |
| `strategy_core/execution.py` | **no** | **3,920** | `simulate_trades` — the entire trade walk |
| `strategy_core/order_blocks.py` | **no** | 222 | `detect_order_blocks`, `ob_id` assignment |
| `strategy_core/ghost_tracker.py` | **no** | 598 | ghost state |
| `strategy_core/{news,scenario,swings,policy,regime}.py` | **no** | — | relocated strategy logic |

**Consequence.** `LuxSession.verify_engine()` refuses to trade on an
"unverified engine", but the fingerprint it checks describes a 102-line shim.
The order-block detector, the trade walk, the ghost tracker and the relocated
news/scenario/swing/regime logic can all be modified without moving
`engine_version` — so a changed engine passes verification unchanged.

**Demonstrated by this milestone:** M-CAP-OPT-2 modifies
`strategy_core/execution.py` and `engine_version` is byte-identical
(`5bb6372c…` before and after). That is convenient here — no re-pin is needed —
but it is convenient *because of the defect*, not because the change was proven
safe by the fingerprint.

**Not fixed here.** Extending `_ENGINE_VERSION_SOURCES` changes the pin for
everyone and needs its own governed milestone: new hash set, re-pin,
`ENGINE_VERSION_EXPECTED` update, and a shadow run. Proposed as
**M-CAP-GOV-1**, and it should outrank further optimisation — an integrity gate
that does not gate is worse than a known-absent one, because it is trusted.

---

## R-4 — Evaluation order is semantically observable *(OPEN — constrains design)*

`active_trades` is appended to at `strategy_core/execution.py:2943` **inside**
the pending loop, and `can_fill` reads it at `:2637`. Under `single_position` /
`one_per_direction`, whether a later candidate fills depends on whether an
earlier one filled on the same candle [source-verified].

**Impact:** any index MUST yield candidates in original list order. Any
set/dict-ordering nondeterminism would change trading output.

---

## R-5 — VPS overruns the bar interval *(RESOLVED as to cause; the overrun itself is OPEN)*

**Was:** tower-side evidence (20 ingest POSTs / 25.3 h, ≈76 min mean
inter-arrival, `no_new_bar` never observed) was consistent with **either** a
boundary overrun **or** a non-continuous runner, and this was explicitly not
guessed at.

**Now — operator-supplied VPS diagnostic [MEASURED, not by me]:**

| Metric | Value |
|---|---|
| Median cycle | **1,387 s** |
| p95 cycle | **1,850.8 s** |
| Skipped boundaries | **36 %** |
| Profile | **CPU-bound** |

Hypothesis (a) is confirmed and (b) is eliminated: the runner *is* running, and
each recompute overruns the 900 s bar interval. Median is **1.54× the budget**;
p95 is **2.06×**. The 36 % skipped-boundary figure is the direct consequence —
roughly one boundary in three is never evaluated.

Cross-reference: Mac median 256.57 s vs VPS median 1,387 s ⇒ the VPS is
**≈5.4× slower** on identical work.

**Still open:** the overrun is real and unfixed. Option A alone does not close a
1.54× gap (see `CAPACITY-EVIDENCE.md`).

**Separately — telemetry timeouts are NOT a capacity result.** Publication
gaps and node-freshness warnings are an operational/transport concern and must
not be quoted as evidence about engine runtime; the engine numbers above come
from `ops/cycles.jsonl`, not from publication intervals.

---

## R-6 — Ghost-tracker `id(ob)` fallback *(OPEN — latent)*

`str(ob.get("ob_id") or ob.get("id") or id(ob))` (`ghost_tracker.py:176,:482`)
would key on a memory address if `ob_id` were ever falsy. Unreachable today
(`ob_id` starts at 1), but a determinism hazard in an engine whose contract is
byte-identical reproducibility.

---

## R-7 — `prev_trades.csv` loss is silent *(OPEN)*

`load_prev_frame()` returns `None` when the file is missing (`live/state.py`),
and `run_once` treats that as `bootstrap` — emitting **no intents** without
raising. `prev_frame_hash` is stored but not checked on load.

**Partially mitigated** by M-CAP-GUARD-1: a frame that loads but carries no
identity key now fails closed. A frame that is *absent entirely* still degrades
silently to bootstrap.

---

## R-8 — Dashboard/checker freshness budgets disagree *(OPEN — from M-ACTIVATION-STABILISE-1)*

Operational projection applies a flat 120 s node staleness budget; the activation
checker applies the phase-aware 900 s recompute budget. The dashboard therefore
warns "stale" between every publication while the node is healthy.
