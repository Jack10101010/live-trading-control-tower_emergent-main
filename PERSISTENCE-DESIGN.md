# PERSISTENCE-DESIGN.md — canonical persisted engine state

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PART 7)
**Status:** design only. **No code changed. No schema created.**

Companion to `INCREMENTAL-DESIGN.md`. This document specifies *what* would be
written, *how* it would be validated, and *how* it would fail — not how it would
be coded.

---

## 1. Design principles

Taken from patterns already proven in this repository, not invented here.

1. **The cache is never authoritative.** Durable trading state
   (`runner_state.json`, `prev_trades.csv`, the intent ledger) remains the sole
   source of truth. An engine checkpoint is a *performance artefact*. Deleting
   the entire checkpoint directory must leave the system correct and merely
   slower. This is the single most important property.
2. **Fail closed, always toward replay.** Any doubt — version, digest, schema,
   integrity, readability — discards the cache. Replay costs 256 s; a wrong
   cache hit corrupts the identity space (`ORDER-BLOCK-IDENTITY.md` §6.1).
3. **Atomic or absent.** Reuse the repository's canonical checkpoint pattern:
   same-dir temp → write → flush → **fsync** → atomic rename
   (`live/state.py:234`). The fsync is not optional: without it a rename can
   survive an OS crash while the data blocks do not.
4. **Separate files, separate lifetimes.** The engine checkpoint must never share
   a file with `runner_state.json`. A corrupt cache must not be able to take the
   intent ledger down with it.
5. **No migration.** Following the existing `INPUT_REVISION_VERSION` precedent
   (`live/runner.py:32`): "any format change yields a non-equal revision
   (fail-safe recompute; no migration)". A schema bump discards and rebuilds.
   Migration code for a rebuildable cache is pure risk with no upside.

---

## 2. Storage layout

```
<state_dir>/
  runner_state.json          ← EXISTING, authoritative, untouched by this design
  frames/prev_trades.csv     ← EXISTING, authoritative, untouched
  ops/                       ← EXISTING
  engine_cache/              ← NEW, entirely disposable
    manifest.json            ← guard + integrity, written LAST
    detection_state.json     ← L2 resume accumulators (small)
    order_blocks.parquet     ← L1 detected + news-tagged OBs (~2,080 rows)
    sim_checkpoint.pickle    ← L3 walk state (largest)
    candles_tail.parquet     ← retained candle tail from the swing watermark
```

`engine_cache/` is safe to `rm -rf` at any time, including while the runner is
stopped mid-write. That property should be tested, not assumed.

---

## 3. Schemas

Field lists are specifications, not code. Types are the Python types the current
engine already produces.

### 3.1 `manifest.json` — the guard

```jsonc
{
  "schema_version": "ct.engine-cache.v1",   // bump ⇒ discard, never migrate
  "created_at": "2026-08-04T10:33:18Z",
  "engine_version": "5bb6372c…",            // must equal verify_engine()

  "window": {
    "first_bar_time": "2015-01-01 00:00:00",// window-start guard (Experiment B)
    "last_bar_time":  "2026-06-19 08:45:00",// resume point
    "last_bar_index": 4314719,
    "m1_row_count":   4314720
  },

  "inputs": {
    "frozen_path":         "data/candles/EURUSD_1m_extended_2015_2026.csv",
    "frozen_prefix_bytes": 275823235,       // bytes covered by the cache
    "frozen_prefix_sha256":"ba32de0d…",     // hash of the FIRST N bytes ONLY
    "frozen_full_sha256":  "ba32de0d…",     // at build time
    "live_segment_sha256": "absent",
    "news_digest":         "sha256:…"       // news CSV — see §7
  },

  "params_digest": "sha256:…",              // see 3.2
  "artifacts": {                             // integrity, see §6
    "detection_state.json":  {"sha256":"…","bytes":812},
    "order_blocks.parquet":  {"sha256":"…","bytes":214... },
    "sim_checkpoint.pickle": {"sha256":"…","bytes":…},
    "candles_tail.parquet":  {"sha256":"…","bytes":…}
  }
}
```

**`frozen_prefix_sha256` is the load-bearing new field.** The existing
`input_revision` hashes the *whole* file, which detects that it changed but
cannot distinguish "appended" (safe to extend) from "rewritten" (must replay).
Hashing exactly the first `frozen_prefix_bytes` and requiring it still match
proves the cached region is byte-identical, which is precisely the precondition
Experiment A validated.

### 3.2 `params_digest`

SHA-256 over a canonical, sorted, explicitly-versioned serialisation of every
parameter that can move an `ob_id` or change a trade. At minimum, from
`golden_pipeline` and `simulation_kwargs`:

`swing_length`, `ob_filter`, `pip_size`, `min_ob_size_pips`, `max_ob_size_pips`,
`detection_timeframe`, `structure_filter`, `allowed_structure_directions`,
`start_date`, `end_date` semantics, `execution_modes[0]`, the entry scenario key,
`rr_multiple`, `stop_buffer_pips`, `ob_entry_depth_pct`, `entry_model`,
`entry_threshold_pct`, all `triggered_edge_*`, all `protection_*`, all
`news_*`, `portfolio_include_disabled_cohorts`, and the resolved policy digest.

**The digest must be built from an explicit allow-list, not
`dataclasses.asdict(config)`.** An allow-list fails safe when a new parameter is
added (it is absent, so the digest is unchanged, so a stale cache could be
reused) — which is the wrong failure. Therefore the allow-list must additionally
include a **count and sorted name-list of all config fields**, so that adding
*any* field changes the digest and forces a replay. That inverts the failure
mode to the safe direction.

Experiment C measured why this matters: a single `min_ob_size_pips` 0.0 → 1.0
change silently shifted 296 order-block ids.

### 3.3 `detection_state.json` — L2

```jsonc
{
  "schema_version": "ct.detection-state.v1",
  "resume_bar_index": 285833,
  "resume_bar_time": "2026-06-19 08:45:00",
  "rma_previous": 0.00042817,          // _pine_rma recursion variable
  "tr_cumsum": 1234.56789,
  "tr_count": 285834,
  "previous_close": 1.08423,
  "current_leg": 1,
  "swing_trend_bias": 1,
  "swing_high": {"level": 1.09120, "index": 285102, "crossed": false},
  "swing_low":  {"level": 1.07880, "index": 285440, "crossed": true},
  "next_ob_id": 2081,
  "candle_retention_watermark_index": 285102   // see §4
}
```

Floats are stored as exact `repr()` round-trippable decimal strings, not
truncated. A rounded ATR accumulator would silently diverge the parsed-price
flips and therefore the order blocks — a corruption no integrity check would
catch, because the file would be perfectly well-formed.

### 3.4 `order_blocks.parquet` — L1

All columns `_make_order_block` produces (`strategy_core/order_blocks.py:86`)
plus the news tags added by `tag_obs_news`, plus two cache-only columns:

* `ob_key` — `sha256(origin_time│detection_time│direction│structure_tag│top│bottom)`,
  the content-addressed window-independent key from `ORDER-BLOCK-IDENTITY.md` §6.3;
* `cached_at_bar_index`.

`ob_id` is stored **verbatim and unchanged**. `ob_key` is additional, used only
by the cache layer for cross-checking. Changing `ob_id` would break `trade_id`,
parity artifacts and Golden reference outputs.

### 3.5 `sim_checkpoint` — L3

The walk state from `ENGINE-STATE.md` §5.1: `active_trades`, `pending`,
`ob_cursor`, accumulated closed-trade rows, `GhostTracker._states`, and the
resume bar index.

**Format warning.** These are nested Python structures containing pandas
`Timestamp`s. Pickle is the only zero-fidelity-loss option, and pickle is
version-fragile and unsafe to load from an untrusted source. Mitigations, all
required: the file is confined to the node's own state directory; its SHA-256 is
in the manifest and checked before load; the manifest records the exact Python
and pandas versions and a mismatch forces replay. If those constraints prove
uncomfortable, the alternative is an explicit typed serialisation with
round-trip equality tests — more work, but no pickle. **This choice should be
made deliberately at implementation time, not defaulted into.**

---

## 4. The candle-retention watermark

`_make_order_block` searches `candles.loc[swing_index : detection_index-1]`, and
an uncrossed swing index can be arbitrarily far back. A resume must therefore
retain candle rows from
`min(uncrossed swing_high.index, uncrossed swing_low.index)` forward — a
**bounded but variable** tail, not a fixed lookback.

`candles_tail.parquet` holds exactly that region. If the watermark cannot be
determined, the cache is invalid and a replay is required. Under-retaining here
produces subtly different order blocks from perfectly valid-looking files, and
no digest would detect it — which makes this the most dangerous single field in
the design.

---

## 5. Versioning and migration

| Version field | Governs | On mismatch |
|---|---|---|
| `schema_version` (manifest) | overall cache layout | discard, replay |
| `schema_version` (per artefact) | that artefact | discard, replay |
| `engine_version` | engine source identity | discard, replay (`verify_engine` refuses an unpinned engine outright) |
| `params_digest` | strategy configuration | discard, replay |
| Python / pandas version | pickle compatibility | discard, replay |

**There is no migration path and there should never be one.** Every byte is
rebuildable from durable inputs in 256 s. Migration code would be an untested
path operating on stale data in a system whose entire contract is byte-identical
reproducibility.

## 6. Validation and integrity

Checked in this order at startup; the first failure discards the cache and
replays. Ordering is cheapest-and-most-likely-to-fail first.

1. `manifest.json` exists, parses, has the expected `schema_version`.
2. `engine_version` matches.
3. Python/pandas versions match (only if pickle is used).
4. `params_digest` matches a freshly computed digest.
5. `window.first_bar_time` matches the current frame's first bar.
6. Every artefact listed in `artifacts` exists with the recorded byte length.
7. Every artefact's SHA-256 matches.
8. `frozen_prefix_sha256` recomputed over the first `frozen_prefix_bytes` of the
   current frozen file still matches.
9. `window.last_bar_time` is ≤ the current frame's last bar (never ahead).
10. The retained candle tail actually covers `candle_retention_watermark_index`.

**The manifest is written LAST.** Artefacts first, fsynced, then the manifest by
atomic rename. A crash at any point leaves either the previous complete manifest
(pointing at the previous complete artefacts) or no manifest at all. There is no
interleaving in which a manifest references a half-written artefact.

Corollary: artefacts must be written to **new** filenames (content- or
generation-suffixed) rather than overwritten in place, so the old manifest's
references stay valid until the new manifest lands. Old generations are pruned
only after the new manifest is durable.

## 7. The news input

`prepare_news_cache`, `tag_obs_news` and the flatten/blackout logic inside the
walk all consume the news calendar. A retroactive calendar correction can change
*historical* trade outcomes — so news is **not** append-only, and cannot be
treated as such without proof.

Until such proof exists: `news_digest` covers the full news CSV, and **any change
forces a full replay**. This is deliberately conservative. It was a live concern
during this milestone — `data/news/master_economic_calendar_2020_present.csv`
was modified in the working tree while measurements were being taken.

## 8. Crash recovery and startup

**Startup:** run §6 checks 1–10. Pass ⇒ load and resume. Fail ⇒ log the specific
failing check, discard `engine_cache/`, replay. The failing check must be named
in the log; "cache invalid" alone would make a persistently-replaying node
undiagnosable.

**Crash during write:** atomic rename plus manifest-last means the previous
generation survives intact. Worst case is the loss of one cycle's cache
progress.

**Crash during read:** no state was mutated; the next start re-runs validation.

**Power loss:** fsync before rename closes the window where a rename survives but
its data blocks do not — the same reasoning already documented at
`live/state.py:234`.

**Divergence — the case that must not be silent.** If a resumed run ever produces
a trade frame differing from a from-scratch run, that is a correctness failure,
not a cache miss. It must: refuse to emit intents from the resumed frame, discard
the cache, replay from scratch, and raise an operational alert. Silently
preferring one frame over the other would let a subtly wrong engine trade.

## 9. Rollback strategy

Four levels, cheapest first:

| Level | Action | Effect | Cost |
|---|---|---|---|
| 1 | `ENGINE_CACHE_ENABLED=false` | Cache never read or written; byte-identical to today | one restart |
| 2 | Delete `engine_cache/` | Next cycle replays and rebuilds | one cycle (256 s) |
| 3 | Revert the cache-layer commit | Code returns to today's behaviour | deploy |
| 4 | Revert to the pinned `engine_version` | Full engine rollback | deploy + `verify_engine` |

**Level 1 is the required property.** Like `LIVE_CLOSE_ACCOUNTING_ENABLED`, the
cache layer must ship behind a default-off flag so that "turn it off" is a
config change and a restart, not a deployment. And, exactly as in
`CLOSE-ACCOUNTING-RUNBOOK.md` §1, the flag binds at process start — a live edit
will not take effect, which is correct for a trading process.

## 10. Observability

Without these, a cache that silently stops working looks identical to one that
is working. Each should appear in `ops/cycles.jsonl` and in node telemetry:

| Signal | Why |
|---|---|
| `cache_status` = `hit` / `miss` / `invalid` / `disabled` | the primary health signal |
| `cache_invalid_reason` (the named failing check) | makes a repeatedly-replaying node diagnosable |
| `bars_replayed` vs `bars_resumed` | shows the cache is actually saving work |
| `cache_build_s`, `cache_load_s` | the cache must not cost more than it saves |
| `cache_bytes` | growth watch |
| consecutive-miss counter | **alarm-worthy**: a cache that always misses is a silent full-cost regression that no functional test would catch |

The last one is the one most likely to be omitted and most likely to matter.
