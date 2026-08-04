# ORDER-BLOCK-IDENTITY.md — canonical identity specification

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 5 — hard gate)
**Status:** verification only. **No code was modified.**

Sources: Control Tower `d6e776f`, Lux `d978074`.
Empirical evidence: `evidence/cap5-identity.json`, produced by
`evidence/cap5_identity_probe.py` (read-only; imports the production engine and
calls `detect_order_blocks` on differently-windowed inputs).

---

## 1. The identity chain

```
ob_id            int, sequential counter        strategy_core/order_blocks.py:142
   │
   ├──► trade_id     f"{L|S}_{ob_id}"           strategy_core/execution.py:765
   │       │
   │       └──► intent_id  sha1(INSTANCE_ID│trade_id│transition│frontier_bar)[:20]
   │               │                            live/intents.py:44
   │               └──► MT5 order comment  intent_id[:26]
   │                                            live/mt5_gateway.py:425
   │                                              │
   │                                              └──► reconciliation match
   │                                                   live/reconciliation.py
   │
   └──► ghost id     str(ob["ob_id"])           strategy_core/ghost_tracker.py:176,:482
```

Every downstream identifier is a **pure function of `ob_id`**. `ob_id` is
therefore the root of the entire identity system, including the broker-side
idempotency key.

---

## 2. How `ob_id` is actually assigned

`strategy_core/order_blocks.py:142`:

```python
ob_id = 1
for index in range(swing_length, len(candles)):
    ...
    order_block = _make_order_block(candles, ob_id, ...)
    if order_block is not None and _passes_size_filter(
            order_block, pip_size, min_ob_size_pips, max_ob_size_pips):
        order_blocks.append(order_block)
        ob_id += 1
```

Three properties follow directly from that code, and all three matter:

1. It is a **1-based sequential counter over the supplied frame**, not a hash of
   the order block's own content.
2. It increments **only when the block survives the size filter**. A block that
   is detected but filtered out consumes no id — so the *filter parameters* are
   part of the numbering.
3. It is assigned in **detection order**, which is frame order.

An `ob_id` carries no intrinsic information about the order block. Two different
runs can assign the same id to entirely different market events.

---

## 3. Causality — why incremental processing is possible at all

Detection is strictly causal. At bar `index` the loop reads only
`candles.loc[pivot_index+1 : index]`, and `_make_order_block`
(`order_blocks.py:59`) searches only `candles.loc[pivot_index : detection_index-1]`.
Nothing reads beyond the current bar.

The volatility measures are causal too, but with a critical qualifier
(`order_blocks.py:43-53`):

| Measure | Form | Causal? | Seeded from |
|---|---|---|---|
| `_pine_rma(true_range, 200)` | recursive: `prev = (prev*(n-1) + v)/n` | yes | the **first bar of the supplied frame** |
| `true_range.cumsum() / bar_index` | cumulative | yes | the **first bar of the supplied frame** |

So the engine is causal but **path-dependent from inception**. There is no
bounded warm-up that makes a truncated window exact — an RMA seeded at bar *k*
never converges to an RMA seeded at bar 0. This is the structural reason the
pipeline reprocesses eleven years on every fifteen-minute boundary, and it
constrains every incremental design that follows.

---

## 4. Empirical verification (the hard gate)

Three experiments on the real production frame: 4,314,720 M1 rows →
285,834 M15 detection bars → **2,080 order blocks**.

### A. Append-only extension — `ob_id` is STABLE ✅

Truncated the frame by 500 M15 bars at the **end**, then compared every shared
id's intrinsic coordinates (`origin_time`, `detection_time`, `direction`,
`top`, `bottom`).

```
full_ob_count       2080
truncated_ob_count  2077
shared_ids          2077
mismatched_ids         0     ← every shared id identical
prefix_stable       true
```

**Extending the window appends new ids and never disturbs existing ones.** This
is the property that makes incremental processing sound, and it is now measured
rather than assumed.

### B. Different window START — `ob_id` is WINDOW-RELATIVE ❌

Dropped the first 5,000 M15 bars.

```
truncated_ob_count       2042
shared_ids               2042
mismatched_ids           2042     ← ALL of them
common_origin_times      2042
same_ob_different_id     2042     ← every single OB got a different id
window_relative          true
```

**Every order block that exists in both runs received a different id.** Not a
few — all 2,042. `ob_id` is entirely relative to where the frame begins.

### C. Parameter change — `ob_id` is PARAMETER-RELATIVE ❌

Same frame, `min_ob_size_pips` 0.0 → 1.0.

```
perturbed_ob_count     2079
shared_ids             2079
mismatched_ids          296
same_ob_different_id    296
parameter_relative     true
```

Ids are stable up to the first order block the new filter rejects, then every
subsequent id shifts by one. The 296 figure is exactly that tail.

---

## 5. Classification

| Identifier | Class | Verdict |
|---|---|---|
| `ob_id` | **window-relative + parameter-relative** | **Unsuitable for persistence** across any change of window start, `swing_length`, `ob_filter`, `pip_size`, `min_ob_size_pips` or `max_ob_size_pips`. Stable **only** under append-only extension of a fixed-start, fixed-parameter frame. |
| `trade_id` (`L_/S_ + ob_id`) | same class as `ob_id` | Unsuitable for persistence under the same conditions. Carries direction, which adds no stability. |
| `intent_id` (sha1 of instance│trade_id│transition│frontier_bar) | **deterministic, but only as stable as `trade_id`** | Globally stable *given* a stable `trade_id`. Replay regenerates byte-identical ids — which is precisely what makes duplicate suppression exact. Inherits every instability above. |
| ghost id (`str(ob_id)`) | same class as `ob_id`, **plus a non-deterministic fallback** | See §6.2. |
| MT5 `magic` | **globally stable** | Configured constant (`config.magic_number`), not derived. Safe to persist. |
| MT5 `ticket` | **globally stable** | Broker-assigned, authoritative, opaque to the engine. Safe to persist. |
| MT5 `comment` (= `intent_id[:26]`) | inherits `intent_id` | The broker-side identity link. |
| `symbol` | globally stable | Configured. |
| `input_revision` | **globally stable, content-addressed** | `sha256(version│frozen_sha256│live_sha256│engine_version)`. The only identifier in the system that is a genuine content hash. |

---

## 6. Consequences that matter financially

### 6.1 `ob_id` drift is a safety issue, not a caching issue

`intent_id` is the broker idempotency key — it is written into the MT5 order
comment and is what `drain_pending()` and the ledger use to avoid re-sending an
order. Because `intent_id` derives from `trade_id` derives from `ob_id`:

> Any change that shifts `ob_id` invalidates the identity of every open and
> pending intent simultaneously.

Concretely, if the frozen history were ever rewritten (not appended), or a
detection parameter changed while positions were open, then on the next cycle:

* `diff_frontier` compares `prev_frame` and `cur_frame` **by `trade_id`**
  (`live/intents.py:50`). Every old `trade_id` would appear to have vanished and
  every new one to have appeared.
* The vanished ones produce no `CLOSE_POSITION` (a disappeared row is not an
  exit transition), so real open positions would stop being mirrored.
* The appeared ones would look like fresh fills and could generate
  `OPEN_POSITION` intents with brand-new `intent_id`s that the ledger has never
  seen — so duplicate-suppression would not stop them.

**What protects this today.** `input_revision` hashes the frozen bytes, the live
bytes *and* `engine_version`, and the C3 gate forces re-evaluation whenever it
changes; `verify_engine()` refuses to run at all on an unexpected
`engine_version`. So a *silent* parameter or history change cannot reach the
engine unnoticed — the revision changes and the engine re-evaluates.

**What is NOT protected.** Re-evaluation is not the same as *reconciliation of
identity*. Nothing compares the old and new id-spaces or refuses to act when
every `trade_id` changes at once. The gate detects that inputs changed; it does
not detect that identity was rebased. Recorded here as a finding; fixing it is
out of scope for this milestone and is proposed as a guard in
`OPTIMISATION-ROADMAP.md`.

> **CLOSED 2026-08-04 by M-CAP-GUARD-1** (`live/identity_guard.py`). Continuity
> is now bound to an identity-space key over engine version, window start,
> symbol/timeframe and every detector parameter that can change whether an id
> increments — plus a direct frame cross-check that shared `trade_id`s still
> describe the same order block and that no prior `trade_id` vanished. On
> refusal the runner returns `identity_drift_frozen`, emits no intents, advances
> no durable state, and cannot reach `executor.apply()`. `end_date` is
> deliberately excluded from the key: it advances every cycle, and Experiment A
> proves append-only extension preserves prior ids. See `OPEN-RISKS.md` R-1 for
> the residual.

### 6.2 The ghost-id fallback can be non-deterministic

`strategy_core/ghost_tracker.py:176` and `:482`:

```python
self.ob_id = str(ob.get("ob_id") or ob.get("id") or id(ob))
```

If `ob_id` is missing **or falsy**, the tracker falls back to `id(ob)` — the
CPython memory address. That is not reproducible across processes, and it would
silently produce a different ghost key on every run.

Two notes, in fairness to the code:

* On the production path `ob_id` is always present and ≥ 1, so the fallback is
  unreachable today. `ob_id` starts at 1 and only increments, so the `or` chain
  cannot be tripped by a legitimate id (there is no `ob_id == 0`).
* It is nevertheless a latent determinism hazard: any future change that makes
  `ob_id` start at 0, or that routes a hand-built dict through the tracker,
  would introduce address-dependent behaviour into an engine whose entire
  contract is byte-identical reproducibility.

Recorded, not fixed.

### 6.3 What a persistent id would have to look like

For any future persisted engine state, the key must be **content-addressed and
window-independent**. The order block's own intrinsic coordinates are already
sufficient and are already computed:

```
ob_key = sha256(origin_time │ detection_time │ direction │ structure_tag │ top │ bottom)
```

All six fields come straight from `_make_order_block` (`order_blocks.py:86-105`)
and none depends on frame position or on how many blocks preceded it.

**This must not replace `ob_id` in the engine.** `ob_id` is embedded in
`trade_id`, in every parity artifact and in the Golden reference outputs;
changing it would break parity and change trading behaviour. The correct shape
is an **additional** persisted key used only by the incremental cache layer, with
`ob_id` retained verbatim as the engine's own sequence number. See
`INCREMENTAL-DESIGN.md` §4.

---

## 7. The invariant the incremental design must enforce

Cached engine state may be reused **only** when all of the following hold. Any
one failing requires a full replay from inception.

1. `engine_version` is unchanged (already enforced by `verify_engine`).
2. The frozen dataset is unchanged **and extended only by appending** — never
   rewritten in place. Verified by the frozen SHA-256 already inside
   `input_revision`, plus a new requirement that the cached prefix be a genuine
   byte-prefix of the current file.
3. Every detection parameter is unchanged: `swing_length`, `ob_filter`,
   `pip_size`, `min_ob_size_pips`, `max_ob_size_pips`.
4. The window start is unchanged.

Experiment A proves reuse is exact when 1–4 hold. Experiments B and C prove it
is silently wrong when 3 or 4 are violated — silently, because the run still
completes and still produces plausible order blocks. That is why this is a hard
gate rather than a performance note.
