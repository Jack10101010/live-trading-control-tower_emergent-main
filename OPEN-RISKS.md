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

## R-3 — 1,207 unexplained metric-update omissions *(OPEN)*

Pending checks 133,069,753 vs metric updates 133,068,546 — a 1,207 difference
[MEASURED]. Some resolution branch skips the final metric update. Benign today
(it is current behaviour and is what the Golden outputs encode), but it **must
be characterised exactly** before any reformulation of the metric, or exported
columns will silently change on those occasions.

---

## R-4 — Evaluation order is semantically observable *(OPEN — constrains design)*

`active_trades` is appended to at `strategy_core/execution.py:2943` **inside**
the pending loop, and `can_fill` reads it at `:2637`. Under `single_position` /
`one_per_direction`, whether a later candidate fills depends on whether an
earlier one filled on the same candle [source-verified].

**Impact:** any index MUST yield candidates in original list order. Any
set/dict-ordering nondeterminism would change trading output.

---

## R-5 — VPS capacity margin unknown *(OPEN — unchanged)*

`ops/cycles.jsonl` holds authoritative production durations and lives on the VPS
filesystem; not readable under current authorisation. Tower-side evidence (20
ingest POSTs / 25.3 h, ≈76 min mean inter-arrival, `no_new_bar` never observed)
is consistent with **either** a 2–5× boundary overrun **or** a non-continuous
runner. Not distinguished, not guessed. Diagnostic in `CAPACITY-BASELINE.md` §9.

**No Mac measurement settles this.** The 900 s budget question stays open.

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
