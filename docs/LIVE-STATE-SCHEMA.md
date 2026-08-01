# Model B — live-state schema (design)

Bounded two-year candle window **plus** compact carried state. Not a general
checkpoint engine: it persists only what cannot be rederived from the window.

Derived from measured engine reads, not from the shape of the data structures:

* the bar loop is a fold — `ctx` is built once and never written during the loop,
  so `SimState` is the sole carrier across bars (M-INCREMENTAL-1 audit);
* order-block detection is **append-only** — every shorter history's block
  sequence is a strict prefix of the longer one, so `ob_cursor` stays valid
  (commit `0b0b5d2`, measured over 288,714 M15 bars);
* blocks are consumed strictly in `detection_time` order via
  `while ob_cursor < ob_count and ob_detection_times[ob_cursor] <= candle["time"]`;
* nothing compares `ob` or `plan` by identity (`is`), and ghost identity is
  `str(ob["ob_id"])` since `2415bd0` removed the `id(ob)` process-address
  fallback — so restoring **copies** is safe.

**No age expiry.** Every lifecycle-valid block is retained regardless of age;
measured maximum actionable age is 1673 days (4.58 y). Two years bounds the
*candle window* only.

---

## 1. Actionable order-block record

One record per block still able to affect a future decision. The engine reads
these from `ctx['ob_records']`, produced by `obs.to_dict("records")`, so the
durable form is exactly that row restricted to fields the walk actually reads.

| field | writer | reader | mutability | serialization | why omitting it changes behaviour |
|---|---|---|---|---|---|
| `ob_id` | `detect_order_blocks` | ghost identity, trade rows | immutable | int | ghost state is keyed on it; losing it breaks ghost continuity and trade provenance |
| `direction` | detection | `_direction_allowed`, plan build | immutable | str | decides whether a plan is created at all |
| `origin_time` | detection | news tagging, reporting | immutable | ISO str | feeds `ob_origin_news_*`; absent ⇒ tags differ |
| `detection_time` | detection | **cursor ordering**, plan build | immutable | ISO str | the cursor advances on this; absent ⇒ ordering undefined |
| `top`, `bottom` | detection | entry level, penetration, invalidation | immutable | float | entry/stop/target are derived from these |
| `break_level` | detection | structure logic | immutable | float | used by invalidation checks |
| `structure_tag` | `filter_order_blocks_by_structure` | structure filter, trade row | immutable | str (`BOS`/`CHoCH`) | already-applied filter result; recomputing needs pre-window structure |
| `pivot_time`, `pivot_index`, `origin_index`, `detection_index` | detection | diagnostics + ordering | immutable | int/str | index fields are window-relative — **must be rebased on restore**, see risks |
| `origin_open/high/low/close`, `parsed_high`, `parsed_low`, `width_pips` | detection | size filters, penetration maths | immutable | float | width/size gates and penetration percentages |
| `swing_length`, `ob_filter` | detection | provenance | immutable | int/str | binds the record to the detection parameters that produced it |
| `ob_*_news_window/event/currency/impact/event_time/window_start/window_end`, `ob_*_minutes_from_news` (16 cols) | `tag_order_blocks_with_news` | news gating | immutable once tagged | str/float/bool | retagging an old block needs calendar events from outside the window |

**Lifecycle / mutable state** — the part that genuinely evolves and cannot be
rederived without replaying pre-window candles:

| field | meaning | why it must persist |
|---|---|---|
| `consumed` | the cursor has passed this block and a plan was (or was not) created | prevents a restored block being re-offered and duplicating a plan |
| `touched` / `first_touch_time` | price has entered the block | drives revisit-confirmation and first-failed-tag logic |
| `max_penetration_pct` / `_pips` / `_price` | deepest penetration so far | already computed by current logic; carried verbatim, **not reinterpreted** |
| `fully_breached` | penetration passed the invalidation threshold | an invalidated block must never reappear as eligible |
| `invalidated` / `invalidated_time` | terminal lifecycle state | terminal blocks are dropped from the actionable set |

Recorded, not reinterpreted: the >100 % penetration observation is a
[research backlog](RESEARCH-BACKLOG.md) item. Current eligibility rules are
persisted exactly as they are.

---

## 2. Minimal live state (outside the candle window)

`SimState` carries seven values; only five are needed after restoration.

| field | include? | justification |
|---|---|---|
| `pending[]` | **yes** | plans awaiting trigger/fill; each references an `ob` + `plan` dict plus per-order revisit/cancel flags. Losing it drops live orders. |
| `active_trades[]` | **yes** | open positions with stop/target/BE/protection state. Losing it abandons open risk. |
| `ghost_tracker._states` | **yes** | `Dict[str, GhostState]` keyed by `ob_id` (content-based since `2415bd0`). Feeds ghost columns on emitted rows. |
| `ob_cursor` | **yes** | consumption frontier into the actionable block list. Append-only detection makes it stable. |
| `_be_applied_any` | **yes** | one bool; gates the break-even annotation pass at `finalize`. |
| `trades[]` | **no** | append-only output stream, never read inside the loop; already persisted as the diff frame by `live/state.py`. |
| `profile` | **no** | diagnostics only. |

Plus, outside `SimState`:

| field | justification |
|---|---|
| `last_boundary` | resume point; already the CT-side commit marker |
| `window_start` / `window_end` | which candle span the state was built against |
| binding fields (below) | fail-closed identity |

**Deliberately excluded** — rederivable from the window, so persisting them would
be bulk without benefit: candle arrays, the full 2,104-block history, resampled
M15 frames, `news_cache`, `analytics_config`, `ctx` in its entirety.

---

## 3. Binding and invalidation

Persisted alongside every record; **any** mismatch discards the state and
triggers a governed full-oracle rebuild. No default may silently repair state.

`schema_version` · `engine_version` · `engine_manifest_id` · `config_hash` ·
`symbol` · `timeframe` · `world.instance_id` · `frozen_dataset_fingerprint`
(size + mtime_ns) · `live_segment_fingerprint` · `window_start` · `last_boundary`
· `payload_sha256`.

Rules:

* atomic write — temp file, `fsync`, rename (the pattern `live/state.py` already uses);
* **crash before commit** ⇒ the boundary is reprocessed (state still points at the previous boundary);
* **crash after commit** ⇒ resume from the following boundary;
* checksum mismatch or truncation ⇒ quarantine the file, rebuild — never partially trust;
* state from another world, symbol, config or engine version ⇒ fail closed;
* candle amendment **behind** the window start ⇒ rebuild (the window cannot prove the block lifecycle);
* candle amendment **inside** the window ⇒ rebuild, unless it is strictly an append after `last_boundary`;
* restoration must be visible in `live.status` — a node silently running on restored state is not operable.

Bootstrap uses the full 11-year oracle. It must be a governed, occasional event,
never a fifteen-minute one.

---

## 4. Known risks in this design

1. **Window-relative indices.** `origin_index`, `detection_index`, `pivot_index`
   and `fill_candle_index` are positions into the *current* candle frame. A
   two-year window rebases them every cycle. They must either be re-derived on
   restore or excluded from any comparison — otherwise identity checks fail
   spuriously and, worse, `be_delay_candles` / `triggered_edge_delay_candles`
   arithmetic could silently shift. **This is the sharpest correctness risk in
   Model B and is not yet resolved.**
2. **Ghost state size** is unmeasured.
3. **Structure tags** are persisted as already-computed. If structure filtering
   is ever made window-sensitive, the carried tag and a recomputed tag could
   disagree.
