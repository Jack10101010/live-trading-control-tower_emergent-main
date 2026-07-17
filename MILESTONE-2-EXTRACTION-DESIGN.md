# Milestone 2 — Core Extraction Design & Dependency Audit

**Status: DESIGN ONLY.** No code written, no files moved, no Lux modification. Executes Architecture V1.2 §10.1 for the Golden Research Profile.
**Extraction target:** the complete Triggered-Edge strategy pass of Golden Run #1 (+292.66R / 635 decided / 2,060 rows) — **not** the baseline entry model. The extracted core must reproduce the Golden Research Profile exactly (include-disabled ON, all 24 cohorts, 78 custom targets, 62 state blocks, PM v1.2 enforce).
**Source revision:** the pinned as-run tree — Lux HEAD `6be5bbb` + the snapshotted dirty files (`golden/run-001/engine-snapshot/`), engine identity `c5e29837…` (byte-verified reproducible). **Extraction must start from this tree, committed/tagged first (M2 Task 1).**
**Date:** 2026-07-16.

---

## STEP 1 — Function classification (every Lux `src/` function)

Audit basis: full function inventory + impurity scan (file/network I/O, prints, wall clock, randomness, multiprocessing) across all of `src/`. Headline finding: **`src/execution.py` (96 functions, 4,196 lines) has zero I/O — the strategy heart is already computationally pure.** Impurities are confined to loaders/writers/drivers. Categories: **PURE CORE** (extract), **INFRA** (stays in driver), **OUT-OF-SCOPE** (not needed for the Golden pass). Priority: P1 = required for the Golden pass; P2 = supporting; P3 = not extracted now.

| Module | Function(s) | Responsibility | Depends on | Category | Prio |
|---|---|---|---|---|---|
| `structure.py` | `detect_swings` (+ `BULLISH_LEG`/`BEARISH_LEG`) | Pine-parity swing detection (right-window confirm) | pandas only | **PURE CORE** | P1 |
| `order_blocks.py` | `_true_range`, `_pine_rma`, `_add_parsed_prices` | TR, RMA-200 ATR vs cumulative-mean-range volatility measure | pandas | **PURE CORE** | P1 |
| `order_blocks.py` | `_make_order_block`, `_ob_width_pips`, `_passes_size_filter`, `detect_order_blocks` | OB creation, BOS/CHoCH tagging, size filter, mitigation-in-loop | structure.py | **PURE CORE** | P1 |
| `execution.py` — *sessions* | `_SESSION_SCHEDULE`, `_session_for_hour`, `_utc_session`, `_candle_session`, `_cohort_session_key` | UTC session clock + cohort session keys | none | **PURE CORE** | P1 |
| `execution.py` — *prep* | `prepare_candles_for_simulation`, `prepare_order_blocks_for_simulation`, `prepare_news_cache` | dict-record conversion; news blackout/flatten window cache (consumes pre-loaded events; no I/O) | pandas | **PURE CORE** | P1 |
| `execution.py` — *planning* | `_planned_trade`, `_base_trade_row`, `_entry_threshold_key`, `_entry_model_key`, `_entry_family`, `_ob_depth`, `_to_pips`, `_entry_depth_pct`, `_penetration_price` | candidate plan: entry/stop/target math, row scaffold | none | **PURE CORE** | P1 |
| `execution.py` — *delayed entry / triggered edge* | `_triggered_edge_touched`, `_triggered_edge_retrace_distance`, `_triggered_edge_retrace_threshold`, `_triggered_edge_mark_tap`, `_delay_window_init`, `_delay_window_update`, `_apply_delay_validity_fields`, `_apply_triggered_edge_metadata`, `_update_pending_metrics`, `_apply_pending_metrics` | 25% threshold, 3-candle delay state machine, same/next-candle modes, tap tracking | none | **PURE CORE** | P1 |
| `execution.py` — *cancel/invalidate* | `_is_fully_breached`, `_price_has_exited_ob_entry_side`, `_fft_distance_from_entry_side_pips`, `_fft_required_move_away_pips`, `_dw_entry_side_exit`, `_dw_ob_occupied` | FFT/retrace/breach cancellation predicates, OB invalidation | none | **PURE CORE** | P1 |
| `execution.py` — *news gating* | `_utc_naive_timestamp`, `_time_cache_key`, `_news_blackout_match`, `_news_flatten_match`, `_apply_news_blackout`, `_apply_news_touch_cancel`, `_mark_news_paused`, `_copy_news_pause_metadata`, `_apply_news_flatten` | blackout/pause/cancel/flatten gates over the prepared cache | none | **PURE CORE** | P1 |
| `execution.py` — *regime gate* | `_stamp_regime_row`, `_regime_state_for_candle`, `_regime_filter_blocks`, `_regime_direction_blocks`, `_regime_filter_block_row` | market-state gate (state/direction predicates → REGIME_BLOCKED) | none | **PURE CORE** | P1 |
| `execution.py` — *portfolio gate* | `_portfolio_regime_gate`, `_portfolio_decision_for`, `_stamp_portfolio_row`, `_portfolio_policy_block_row`, `enrich_trades_with_portfolio_policy` | PM policy enforcement (LABEL/STATE_ONLY/DIRECTION_AWARE) → REGIME_BLOCKED rows | portfolio_policy.py | **PURE CORE** | P1 |
| `execution.py` — *scenario/cohort* | `_build_cohort_index` | scenario compilation: cohort enabled, per-state eligibility blocks (STATE_BLOCKED), custom target resolution — **the target/eligibility authority** | sessions | **PURE CORE** | P1 |
| `execution.py` — *fills & exits* | `_is_filled`, `_exit_outcome`, `_fill_candle_bookable_extreme`, `_fill_candle_exit_outcome`, `_direction_allowed`, `_has_reverse_conflict`, `_close_breach_distance`, `_protection_trigger`, `_apply_protection_exit`, `_apply_session_filter`, `_apply_fill_metrics`, `_apply_missed_metrics`, `_init_active_metrics`, `_update_active_metrics`, `_apply_active_metrics`, `_apply_exit_metrics`, `_flatten_r` | fill detection, exit resolution, same-candle ambiguity rules, metrics | none | **PURE CORE** | P1 |
| `execution.py` — *costs/R* | `_cost_r_components`, `_apply_execution_costs`, `_apply_weighted_r`, `_resolve_risk_amount` | spread/slippage/commission in R; weighted R | none | **PURE CORE** | P1 |
| `execution.py` — *BE machinery* | `_be_arm_hit`, `_be_stop_hit`, `_be_arm_hit_on_fill_candle`, `_be_scenario_key` | break-even arm/stop predicates (unused in Golden — `be: null` — but part of the walk code path) | none | **PURE CORE** | P2 |
| `execution.py` — *main loop* | `simulate_trades` | THE walk: candles × OBs → candidates → gates → fills → exits → rows | all above | **PURE CORE** | P1 |
| `execution.py` — *frame/summary* | `_trade_frame`, `assign_execution_trade_numbers`, `normalize_target_set`, `target_exit_columns`, `rr_token`, `summarize_trades`, `enrich_trades_with_market_state`, `enrich_trades_with_stop_anchored_excursions`, `_coerce_float`, `_coerce_int` | rows → DataFrame, trade numbering, summary aggregates | pandas | **PURE CORE** | P2 |
| `execution.py` — *profiling* | `_new_hot_path_profile`, `_profile_increment`, `_profiled_base_trade_row` (+ `from time import monotonic`) | hot-path timing (off in Golden: `profile_simulation_hot_path:false`) | **`time.monotonic` ⚠** | **INFRA (port)** | P2 |
| `execution.py` — *variants* | `simulate_trades_be_multiarm`, `simulate_trades_be_multiarm_inloop_active_wick`, `simulate_entry_penetration_batch`, `_canonical_entry_penetration_key` | BE-multiarm & penetration-batch research variants (unused in Golden pass) | core | **PURE CORE (dormant)** | P3 |
| `regime.py` (10 fns) | EMA200/BBW/ADX 6-state classifier, shifted panel, `regime_emit_for_run` | market-state panel (compute-only; parity-tested vs frontend) | numpy/pandas | **PURE CORE** | P1 |
| `portfolio_policy.py` (18 of 19 fns) | `validate_policy`, `load_policy_doc`, `decide`, `regime_gate_plan`, `with_disabled_as_label`, `cohort_key`, canonicalisers, checksum fns | policy table, decisions, gate plans, DISABLE→LABEL override | hashlib/json (pure) | **PURE CORE** | P1 |
| `portfolio_policy.py` | `load_policy(path)` (the one `open()`) | file → `load_policy_doc` | filesystem ⚠ | **INFRA** (seam already exists) | P1 |
| `config.py` | `BacktestConfig` dataclass (+ path constants) | config schema | `Path` constants ⚠ | **PURE CORE** (dataclass) / INFRA (path constants) | P1 |
| `resample.py` | `resample_candles` | 1m → 15min detection frame | pandas | **PURE CORE** | P1 |
| `data_loader.py` | `load_candles` | CSV → DataFrame | `read_csv` ⚠ | **INFRA** | P1 (driver) |
| `ghost_tracker.py` (6 fns) | cancelled-OB counterfactual tracking | observational only | datetime parse (pure) | **PURE CORE (dormant)** | P3 |
| `retest_tracker.py` / `fair_baseline.py` / `portfolio_replay.py` / `sweeps.py` / `reports.py` / `run_outputs.py` / `resume_support.py` / `run_metadata.py` | exports, baseline comparison pass, replay validator, sweep planning, report writing, output/manifest writing, resume caching, run metadata | research/driver machinery | I/O throughout | **INFRA / OUT-OF-SCOPE** | P3 |
| `scripts/run_backtest.py` (driver) | config load, candle/news/policy loading (`load_news_calendar_events` L1833+), regime/portfolio emit assembly, scenario passes, output writing, progress/manifest | **the BacktestDriver** per V1.2 §3 | all I/O | **INFRA** | stays |

*Note:* `fair_baseline.py` participates in the run's `scenario_baseline` output but not in the Golden **strategy pass** — OUT-OF-SCOPE for extraction; the driver keeps producing it unchanged.

---

## STEP 2 — Extracted module layout

New package `strategy_core/` (a sibling package inside the Lux repo initially — V1.2 says the core is a library both Lux and the trading node import; keeping it in-repo for M2 avoids cross-repo churn during parity, and it can be lifted out verbatim later).

```
strategy_core/
├── __init__.py            # public API surface only
├── types.py               # frozen dataclasses/TypedDicts: CandleRecord, OrderBlockRecord,
│                          #   TradePlan, TradeRow, CohortRule, GatePlan, NewsWindow, SimResult
├── config.py              # StrategyConfig (the BacktestConfig dataclass, path-constant-free)
├── sessions.py            # _SESSION_SCHEDULE, session_for_hour, utc_session, candle_session,
│                          #   cohort_session_key                       [imports: nothing]
├── swings.py              # detect_swings (verbatim structure.py)      [imports: pandas]
├── order_blocks.py        # TR/RMA/CMR measure, OB detect/filter/mitigate  [imports: swings]
├── regime.py              # 6-state classifier + shifted panel (verbatim)  [imports: np/pd]
├── policy.py              # portfolio_policy minus load_policy(path): PolicyTable,
│                          #   decide, regime_gate_plan, with_disabled_as_label, checksums
│                          #                                            [imports: types]
├── scenario.py            # _build_cohort_index: cohort rules, per-state eligibility,
│                          #   custom-target resolution                 [imports: sessions, types]
├── news.py                # prepare_news_cache + all blackout/flatten/touch-cancel predicates
│                          #                                            [imports: types]
├── entries.py             # planning + delayed-entry/triggered-edge state machine + cancels
│                          #                                            [imports: sessions, types]
├── walk.py                # fills, exits, ambiguity rules, protection, active metrics, BE
│                          #   predicates, costs/R                      [imports: entries, types]
├── engine.py              # simulate_trades (the loop) + regime/portfolio gate application
│                          #   + trade_frame/summarize                  [imports: all above]
└── ports.py               # Profiler port (no-op default; replaces time.monotonic),
                           #   future Clock port                        [imports: nothing]
```

**Interfaces (public exports; everything else stays private):**
- `swings.detect_swings(df, length) -> df`
- `order_blocks.detect_order_blocks(candles, ob_filter, pip_size, min/max) -> list[OrderBlockRecord]`
- `regime.regime_emit_for_run(candles, cfg) -> RegimeEmit`
- `policy.load_policy_doc(doc) / decide / regime_gate_plan / with_disabled_as_label`
- `scenario.build_cohort_index(scenario) -> CohortIndex`
- `news.prepare_news_cache(events_df, candles, flatten_minutes) -> NewsCache`
- `engine.simulate_trades(candles, order_blocks, cfg, *, news_cache, regime_emit, portfolio_emit, cohort_index, profiler=NoopProfiler) -> SimResult(rows, frame, summary)`

**Contract:** every public function takes immutable-by-convention inputs and returns fresh outputs; no function reads a file, the network, the environment, or any clock; the only injectables are the ports. The driver (`run_backtest.py`) keeps: loading candles/news/policy, assembling emits, invoking `engine.simulate_trades`, and all writing.

---

## STEP 3 — Dependency graph & violations

```
                    ┌────────────────────────── strategy_core ──────────────────────────┐
                    │  ports  ◄─ engine ─► walk ─► entries ─► sessions                   │
                    │             │  │        └────────────► types                       │
                    │             │  ├─► scenario ─► sessions/types                      │
                    │             │  ├─► news ─► types                                   │
                    │             │  ├─► policy ─► types                                 │
                    │             │  ├─► regime                                          │
                    │             │  └─► order_blocks ─► swings                          │
                    └────────────────────────────▲───────────────────────────────────────┘
                                                 │ (inward only)
        scripts/run_backtest.py (BacktestDriver: data_loader, news loader, load_policy(path),
        reports, run_outputs, resume_support, sweeps, fair_baseline, parallelism, manifests)
```

**Proofs / current-state audit:**
- **Inward-only:** achievable — no core-candidate module imports any I/O module today (verified imports: `structure` → nothing; `order_blocks` → structure; `regime` → np/pd; `execution` → dataclasses, `time.monotonic`, np/pd; `portfolio_policy` → hashlib/json).
- **No cycles:** current graph is acyclic (structure ← order_blocks; execution imports order-blocks *records*, not the module; portfolio_policy standalone). Proposed layout preserves acyclicity by construction (types/sessions at the bottom, engine at the top).
- **No I/O in core:** already true for every P1 function (0 impurity hits in execution/structure/order_blocks/regime/resample/sweeps).
- **Determinism:** same inputs ⇒ same outputs today, given identical library versions (see Step 6 float risk).

**Violations to fix during extraction (complete list):**
1. `execution.py: from time import monotonic` — profiling only (`profile_simulation_hot_path` false in Golden). → `ports.Profiler` (no-op default); behavioural no-op.
2. `portfolio_policy.load_policy(path)` — file read. → stays with the driver; core exposes `load_policy_doc(doc)` (already exists).
3. `config.py` module-level `PROJECT_ROOT/DATA_DIR/...` `Path` constants — filesystem-shaped constants imported by drivers. → constants stay in Lux; the dataclass moves.
4. `run_outputs.engine_version()` hashes `src/execution.py` **by path** — extraction inherently changes it (see Step 5 re-baselining rule).
5. No other violations found: no `datetime.now`, no randomness, no env reads, no prints, no threads/process pools inside `src/` core candidates (parallelism, if any, lives in the driver; verified absent from `execution.py`).

---

## STEP 4 — Extraction sequence (smallest safe slices; Lux runnable after every slice)

Strangler pattern: each slice **moves code verbatim** into `strategy_core/` and leaves a **re-export shim** at the old location (`from strategy_core.swings import *`, plus explicit names), so every existing Lux import keeps working. No signature changes, no renames, no "cleanups". One slice = one commit = one parity gate.

| # | Slice | Files moved (verbatim) | Interface/adapters created | Parity risk | Verification |
|---|---|---|---|---|---|
| 0 | **Pin the tree** | none | commit/tag `golden-run-001` (dirty files incl. as-run engine) | none | `engine_version()` == `c5e29837…` at the tag |
| 0.5 | **Fast oracle** | none | generate `parity-fast` baseline: Golden config narrowed **only** by `start_date/end_date` (2024-01-01→2026-06-19) on the pinned tree, archived like run-001 | none (pre-extraction) | run completes; outputs archived + hashed |
| 1 | **Scaffold** | none | empty `strategy_core/` package + `types.py` (aliases only) + `ports.py` | ~zero | Lux full test-suite/imports still pass; `parity-fast` byte-identical |
| 2 | **swings + sessions** | `structure.py` → `swings.py`; session block of `execution.py` → `sessions.py` | shims: `src/structure.py` re-exports; `execution.py` imports sessions from core | low (pure moves) | `parity-fast` byte-identical trades CSVs |
| 3 | **order_blocks** | `order_blocks.py` → core | shim in `src/order_blocks.py` | low | `parity-fast` + `ob_sanity`/`order_blocks.csv` byte-compare |
| 4 | **regime + policy** | `regime.py` → core; `portfolio_policy.py` minus `load_policy(path)` → `policy.py` | `src/*` shims; `load_policy` stays, delegating to core `load_policy_doc` | low | `parity-fast`; policy checksum functions re-verified against `86ff709c…` |
| 5 | **news + scenario** | news-cache/predicates + `_build_cohort_index` → `news.py`/`scenario.py` | `execution.py` imports from core | **medium** (touch-cancel/flatten timing; per-state resolution) | `parity-fast` byte-identical incl. NEWS_* and STATE_BLOCKED counts |
| 6 | **entries + walk** | planning, triggered-edge state machine, cancels, fills/exits/costs → `entries.py`/`walk.py` | `execution.py` imports from core | **high** (the state machine mutates `item` in place — semantics must move verbatim) | `parity-fast` byte-identical; spot-diff 20 known trades field-by-field |
| 7 | **engine** | `simulate_trades` + gates + frame/summarize → `engine.py`; `src/execution.py` becomes a shim façade | `ports.Profiler` replaces `monotonic` | **high** | **FULL golden parity** (Step 5) on 2015→2026 |
| 8 | **Freeze** | none | import-lint CI rule (core imports nothing outside itself); golden replay job | none | full parity re-run green; SCOPE-LOCK addendum |

Slices 2–6 gate on `parity-fast` (minutes, not the 6.7-minute full run each time); slices 6→7 additionally gate on the **full** Golden Run.

---

## STEP 5 — Golden parity plan

Three parity tiers, applied per slice:

- **Structural parity** (every slice): Lux imports resolve; the driver runs end-to-end; output files exist with identical schemas (same columns, same row counts). Cheap sanity — never sufficient alone.
- **Behavioural parity — THE GATE** (every slice): byte-identical regeneration of the run artifacts on the pinned dataset via the *unchanged driver*: `trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv` (primary oracle — all 2,060 rows: every candidate, STATE_BLOCKED/REGIME_BLOCKED/news outcome, fill, exit, rr, net_r), `order_blocks.csv`, and `summary.json` (numeric fields; timestamp/path fields excluded). Slices 2–6 prove this on `parity-fast`; slices 7–8 prove it on the **full** Golden window against the archived reference (sha `ca925d54…`). Byte-compare first; on any diff, fall back to field-level diff to localise (same `to_csv` path is retained, so float formatting is stable). Secondary oracles: `resolved_execution_map.json` regeneration equality; the abandoned one-cohort slice as a cross-section check.
- **Event parity** (deferred to the Journal milestone, per V1.2 §10.2): once the core emits V1.2 §2.1 events, replaying Golden candles must yield events that reconcile 1:1 to the trades oracle. Not an M2 gate — M2's contract is behavioural parity of the existing artifacts.

**engine_version re-baselining rule (I-3):** `engine_version()` hashes `src/execution.py`/`run_backtest.py`/`resume_support.py` bytes — extraction necessarily changes it. Each slice commit that alters those files records: old hash → new hash + "extraction slice N, behavioural parity proven (artifact shas …)" in the commit message. The *golden reference* manifest keeps `c5e29837…` forever; parity is proven by artifacts, never by engine-hash equality. `resume_support.py` is **not moved** (it's in the hash set and pure driver machinery) — resume caches self-invalidate on version change, which is correct.

---

## STEP 6 — Risk audit (everything that could change July 14 behaviour)

1. **In-place mutation semantics** — the delayed-entry state machine mutates pending `item` dicts across candles (`_delay_window_update`, `_triggered_edge_mark_tap`, `_mark_news_paused`); rows are mutated by `_apply_*` in a fixed order. Any "clean-up" into immutable copies changes behaviour. **Mitigation: verbatim moves; immutability is enforced at the *public engine boundary*, not inside the loop (M2 keeps interior mechanics untouched).**
2. **Ordering assumptions** — dict insertion order (cohort index iteration, pending-item lists), candle iteration order, OB detection order, and `assign_execution_trade_numbers` sequence. Python ≥3.7 dict order is stable, but any reordering of set iteration (e.g. `allowed_session_set`) or replacing lists with sets would silently reorder rows. **Mitigation: no data-structure substitutions; row order byte-compared.**
3. **Floating point** — `cumsum` CMR measure, `_pine_rma` recursion, cost arithmetic: results are stable only under identical numpy/pandas versions and identical operation order. **Requirements are unpinned.** **Mitigation: M2 Task 2 snapshots the exact installed versions (`pip freeze`) next to the tag; parity runs use that environment; no vectorisation/reordering of arithmetic.**
4. **Hidden module state** — `_time_cache_key` caching (news time keys), hot-path profile dict, module-level `_SESSION_SCHEDULE`. All deterministic; caches must move with their consumers. **Mitigation: keep cache scope identical; profiler behind a no-op port.**
5. **Time dependence** — only `time.monotonic` (profiling, off in Golden). No wall-clock reads anywhere else. **Mitigation: port + assert `profile_simulation_hot_path=false` in parity runs.**
6. **Shared references** — candles/OB dict-records are shared between planning and walk; copying them would break identity-based updates (e.g. OB mitigation flags). **Mitigation: preserve pass-by-reference exactly.**
7. **Portfolio side effects** — `with_disabled_as_label` mutates a *copy* of the policy table in memory; the driver decides when to apply it. Moving `policy.py` must not change *where* the override is applied (driver-side). **Mitigation: the override call remains in `run_backtest.py`.**
8. **CSV serialisation** — float formatting comes from pandas `to_csv` defaults; parity is byte-level only if writing stays in the untouched driver (`run_outputs`/driver paths). **Mitigation: writers never move in M2.**
9. **Resume caches** — `resume_support` may reuse prior outputs keyed by engine_version; a stale cache could fake parity. **Mitigation: parity runs use fresh output dirs and `--no-resume` (or equivalent), verified in the run log.**
10. **`reverse_touch_cancel` legacy flag** — config false / not forced; must remain read from config, never re-defaulted.
11. **numpy/pandas dtype edges** — `_coerce_float/_coerce_int` behaviour on NaN/strings must move verbatim (they shape blank-vs-0 fields in the CSV).
12. **Environment drift between machines** — parity must be executed on the operator's machine (same interpreter/env as July 14 ideally, else the frozen `pip freeze` env); the sandbox is unsuitable for the full run.

---

## STEP 7 — Milestone 2 implementation plan (ordered; ~30–90 min each)

1. **Pin the as-run tree.** In Lux: new branch `golden-run-001-engine`, commit the 9 tracked-dirty files, tag `golden-run-001`; verify `engine_version()` == `c5e29837…` at the tag. *(30m)*
2. **Freeze the environment.** `pip freeze` → `golden/run-001/engine-snapshot/environment.freeze.txt`; record interpreter version; note any deltas from the July 14 machine. *(30m)*
3. **Generate the fast oracle.** Run the Golden config with dates narrowed to 2024-01-01→2026-06-19 (only change), fresh output dir, resume off; archive + hash as `golden/run-001/parity-fast/`. *(60–90m incl. runtime)*
4. **Write the parity harness.** A comparison script (driver-side tooling, not core): byte-compare + field-level fallback diff for trades/OB/summary artifacts between two run dirs; exit code = gate. Test it on reference-vs-reference. *(60m)*
5. **Slice 1 — scaffold.** `strategy_core/` package, `types.py` (aliases), `ports.py` (NoopProfiler); no Lux imports change. Run structural checks + `parity-fast`. *(45m)*
6. **Slice 2a — swings.** Move `structure.py` verbatim → `strategy_core/swings.py`; shim old module. `parity-fast` gate. *(45m)*
7. **Slice 2b — sessions.** Move the session block from `execution.py` → `sessions.py`; execution imports from core. `parity-fast` gate. *(60m)*
8. **Slice 3 — order_blocks.** Move + shim; gate incl. `order_blocks.csv` byte-compare. *(60m)*
9. **Slice 4a — regime.** Move `regime.py`; shim; gate. *(45m)*
10. **Slice 4b — policy.** Move `portfolio_policy.py` minus `load_policy(path)`; `load_policy` delegates; re-verify checksum fns against `86ff709c…`; gate. *(60m)*
11. **Slice 5a — news.** Move news cache + predicates → `news.py`; gate with explicit NEWS_FLATTEN=15 / NEWS_TOUCH_CANCEL=9 count assertion on `parity-fast` scale. *(90m)*
12. **Slice 5b — scenario.** Move `_build_cohort_index` → `scenario.py`; gate with STATE_BLOCKED distribution assertion. *(60m)*
13. **Slice 6a — entries.** Move planning + triggered-edge machinery + cancels → `entries.py` (verbatim, mutation semantics intact); gate. *(90m)*
14. **Slice 6b — walk.** Move fills/exits/protection/costs/BE predicates → `walk.py`; gate + 20-trade field-by-field spot diff. *(90m)*
15. **Slice 7 — engine.** Move `simulate_trades` + gate application + frame/summarize → `engine.py`; `src/execution.py` becomes a façade; profiler port wired. `parity-fast` gate. *(90m)*
16. **FULL GOLDEN PARITY.** Re-run the complete 2015→2026 Golden config on the pinned dataset through the extracted core; byte-compare all oracles vs `golden/run-001/reference/`. Any diff → stop, localise, fix, re-run. *(90m incl. runtime)*
17. **Freeze & enforce.** Import-lint rule (core imports: stdlib-pure + numpy/pandas only; no src/, no scripts/, no I/O modules); parity harness wired as the golden-replay job; engine_version re-baseline recorded. *(60m)*
18. **Close-out.** SCOPE-LOCK addendum (core package + new engine_version lineage), one PROJECT_STATE milestone entry, M2 verdict vs V1.2 §10.1 ("done when golden runs reproduce"). *(45m)*

**Definition of done (V1.2 §10.1, terminology corrected):** Milestone 2's gate is **structural parity + row-for-row behavioural parity + artifact parity** — the extracted `strategy_core` reproduces Golden Run #1's strategy pass row-for-row (byte-level on the primary trades oracle, order-block artifact, and stable summary fields), Lux remains fully runnable as the BacktestDriver around the core, the dependency rule is lint-enforced, and the golden replay is a repeatable CI-style gate. **Event-for-event journal parity is NOT an M2 requirement** — it begins when the core emits the frozen event-journal schema in the later journal/replay milestone.

---

*Approval of this document unlocks Task 1. Nothing has been moved, modified, or committed yet.*
