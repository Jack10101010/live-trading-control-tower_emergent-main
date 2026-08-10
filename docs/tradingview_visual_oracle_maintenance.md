# TradingView Visual Oracle — Maintenance Runbook

**Audience:** whoever just changed the Python engine and needs to know what that
means for the TradingView chart.

**Rule:** Python is the source of truth. Pine is generated. Changes flow one way.
If Pine and Python disagree, **Pine is wrong.**

For the full behavioural map see
[tradingview_visual_oracle_parity_audit.md](tradingview_visual_oracle_parity_audit.md).
This document is the operational half — what to run, in what order, and what each
failure means.

---

## 0. The 30-second version

```bash
python -m tools.oracle.check_freshness
```

| Output | Meaning | Do this |
|---|---|---|
| `CURRENT` | The released Pine build is the deployed engine | nothing |
| `PARTIAL` | Matches, but some stages aren't implemented yet | nothing (expected during Phase 1) |
| `UNVERIFIED` | No manifest, or never validated | run the update workflow (§3) |
| `STALE_CONFIG` | Config or policy moved | §3, config-only path |
| `STALE_ENGINE` | Governed engine content moved | §3, then §4 to scope the work |
| `STALE_CONTRACT` | Committed contract ≠ fresh extraction | `extract_contract --write` |
| `STALE_TRACE_SCHEMA` | Trace schema moved | regenerate **every** fixture |
| `INCOMPATIBLE` | Unsupported feature is live, pin invalid, or a generated file was hand-edited | **stop** — see §6 |

Exit codes: `CURRENT`/`PARTIAL` → 0, `UNVERIFIED` → 1, `STALE_ENGINE` → 2,
`STALE_CONFIG` → 3, `STALE_CONTRACT` → 4, `STALE_TRACE_SCHEMA` → 5,
`INCOMPATIBLE` → 6. Safe for CI.

---

## 1. What exists today (Phase 0.5)

| Path | What it is | Status |
|---|---|---|
| `contracts/tradingview_oracle_contract.json` | The extracted parity contract — **generated** | ✅ implemented |
| `contracts/tradingview_oracle_contract.schema.json` | Its schema | ✅ |
| `contracts/tradingview_oracle_trace.schema.json` | Per-bar trace schema v1.0.0 | ✅ designed |
| `contracts/tradingview_oracle_parity_manifest.schema.json` | Released-build manifest schema | ✅ designed |
| `contracts/tradingview_oracle_impact_map.json` | Change → Pine module/fixture/stage map | ✅ implemented |
| `tools/oracle/engine_access.py` | Read-only governed-engine loader | ✅ |
| `tools/oracle/fingerprint.py` | Engine fingerprint | ✅ |
| `tools/oracle/enums.py` | Enum/reason-code extraction | ✅ |
| `tools/oracle/extract_contract.py` | Contract generator | ✅ |
| `tools/oracle/check_freshness.py` | Stale-oracle detector | ✅ |
| `tools/oracle/impact.py` | Change-impact analyser | ✅ |
| `backend/tests/test_oracle_contract.py` | 29 drift/safety tests | ✅ |
| `tools/oracle/generate_pine.py` | Pine generator + single-file assembly | ✅ **S1** |
| `tools/oracle/export_trace.py` | Offline canonical trace exporter | ✅ **S1** |
| `tools/oracle/session_codes.py` | Contract-derived numeric codes for Pine | ✅ **S1** |
| `tools/oracle/build_fixtures.py` | Golden fixture builder | ✅ **S1** |
| `tools/oracle/lint_pine.py` | Static Pine validation | ✅ **S1** |
| `tools/oracle/verify_s1.py` | S1 verification + TradingView checklist | ✅ **S1** |
| `pine/src/*.pinefrag` | 8 hand-written fragments | ✅ **S1** |
| `pine/generated/tradingview_visual_oracle.pine` | **Generated** artefact — never hand-edit | ✅ **S1** |
| `golden/tradingview_oracle/s1/` | 11 golden fixtures | ✅ **S1** |
| `artifacts/tradingview_oracle/parity_manifest.json` | Released-build record | ✅ **S1** |
| `backend/tests/test_oracle_s1.py` | 69 S1 tests | ✅ **S1** |

`check_freshness` currently reports **`UNVERIFIED`** with `S1: UNVERIFIED` — correct:
the build exists and is internally consistent, but no TradingView comparison has been
recorded. See §11.

---

## 2. Safety contract of this tooling

Everything under `tools/oracle/` is **read-only** and is tested to stay that way
(`test_tooling_never_touches_live_state_or_the_broker`):

- never writes inside `LUX_ROOT`;
- never touches `live_state/` — the live node owns it and holds an OS lock
  (`live/lifecycle.py::ProcessLock`);
- never constructs `MT5Gateway`, `Executor`, `LiveRunner`, `CTPublisher` or
  `AccountObserver`, so it cannot reach the broker or the ledger;
- restores the process working directory (the Lux driver requires `chdir`).

Verified with a `sys.addaudithook` probe: importing the governed engine performs
no writes, no `mkdir`/`rename`/`remove`, and no subprocess calls.

**You can run any of these commands while the live node is trading.**

---

## 3. The update workflow

### 3.1 Normal path

```bash
python -m tools.oracle.extract_contract --write
```

```bash
python -m tools.oracle.impact --vs-contract
```

Run the first to refresh the contract, the second to learn what the change costs
you. `impact` prints the exact Pine modules, trace fields, fixtures, stages and
**Python test files** involved — you should never have to guess which tests to run.

Then, in order:

1. **Change Python.** Normal engineering.
2. **Run the engine's own tests** — `impact` names them. Note that 6 Lux test
   modules are script-style (`sys.exit()` at import) and crash `pytest`
   collection; run those directly with `python tests/<file>.py`.
3. **Re-stamp the engine pin** if governed content moved: the deployment gates on
   `ENGINE_MANIFEST_ID_EXPECTED` in `live/config.py` and will refuse to trade
   until it matches (`python -m live.engine_identity --write live/engine_manifest.json`).
4. **`extract_contract --write`** — regenerate the contract.
5. **`impact --vs-contract`** — confirm the scope is what you expected.
6. *(Phase 1)* regenerate Pine, export traces, run the named fixtures.
7. *(Phase 1)* update `artifacts/tradingview_oracle/parity_manifest.json`.
8. **`check_freshness`** — must return `CURRENT` or `PARTIAL` before you publish.

### 3.2 Why there is no single `update_tradingview_oracle.py` yet

Step 3 changes a **production gate**. An all-in-one command that re-stamped the
engine pin as a side effect would let a strategy edit sail past the check that
exists to catch it — the deployment deliberately refuses to trade on an
unverified engine (`live/runner.py:62-75`).

So the pin re-stamp stays a separate, deliberate operator act. Once the Pine
generator and trace exporter exist, the *remaining* steps (4–8) are safe to wrap
in one command, because none of them can affect trading. That wrapper is a
Phase 1 deliverable; today the two commands in §3.1 are the whole of it.

### 3.3 Config-only change

A change to `generated_configs/…json` or `configs/policy/deployed_policy.v1.json`
touches no algorithm, but it **still requires regeneration** — the 164 populated
cohort cells are compiled into Pine constants.

```bash
python -m tools.oracle.impact --files configs/policy/deployed_policy.v1.json
```

→ `GENERATED_CONSTANTS_UPDATE`, `pine/70_cohorts`, fixtures `F-LONDON` `F-STATE`,
stage `S10`.

---

## 4. Reading an impact result

```bash
python -m tools.oracle.impact --since origin/main
python -m tools.oracle.impact --files strategy_core/order_blocks.py
python -m tools.oracle.impact --vs-contract          # no git needed
```

`--vs-contract` is the strongest form: it recomputes every governed file digest
and compares against the contract, so it catches uncommitted edits too.

| Category | Meaning |
|---|---|
| `NO_ORACLE_IMPACT` | Not mirrored by Pine. Record and move on. |
| `CONFIG_ONLY_ORACLE_UPDATE` | Regenerate constants, re-run affected fixtures. |
| `GENERATED_CONSTANTS_UPDATE` | An enum/table changed. Regeneration **fails** if Pine lacks a mapping. |
| `ALGORITHM_PARITY_UPDATE` | A decision rule changed. Re-derive the Pine module, re-baseline fixtures. |
| `TRACE_SCHEMA_UPDATE` | Bump `TRACE_SCHEMA_VERSION`, regenerate **all** fixtures. |
| `PINE_LIMITATION_CHANGE` | Review the limitation register — what's mirrorable changed. |
| `FULL_PARITY_REVALIDATION_REQUIRED` | Broad or unclassified. Re-run everything. |

**An unmapped governed file fails closed** to `FULL_PARITY_REVALIDATION_REQUIRED`.
That's deliberate: a file nobody has classified is not assumed harmless. Fix it by
adding an entry to `contracts/tradingview_oracle_impact_map.json` — never by
widening the default. `test_impact_map_covers_every_governed_file` keeps all 30
governed files mapped.

---

## 5. The fingerprint

```
lux-6cb6cbc_cfg-a4cb908_pol-d1dfc11_c1.0.0_t1.1.0_g0.2.0
```

(`t1.1.0_g0.2.0` since Stage S1 — the trace schema and generator both moved. Older
documents may quote `t1.0.0_g0.1.0`; the live value is whatever
`python -m tools.oracle.check_freshness` prints.)

Composed from **content**, not revisions:

| Input | Why |
|---|---|
| `engine_manifest_id` | 30-file governed digest — the real engine identity |
| `engine_version` | Lux's 3-file digest, kept for continuity (covers 3 of ~23 modules — insufficient alone) |
| `resolved_config_hash` | `run_backtest.config_hash` over the resolved config |
| `policy_version` + `policy_content_sha256` | the policy table can move independently |
| `contract_schema_version` | contract shape |
| `trace_schema_version` | fixture validity |
| `generator_version` | generator output can change for identical inputs |
| `symbol`, `detection_timeframe`, `execution_timeframe` | the data context the build is valid for |

**Excluded on purpose:**

- **`end_date`** — the one value the live runner overrides every cycle
  (`live/runner.py:82`). It advances daily and has no Pine meaning. Including it
  would invalidate the build every day for no behavioural reason.
- **The environment** (python/numpy/pandas/platform) — **recorded, never hashed.**
  The project has measured a ~220 ULP `bbw_value` shift between hosts from
  dependency versions alone (`live/engine_identity.py:184-202`). Hashing it would
  invalidate every build on a patch bump that cannot change a rendered decision;
  ignoring it would hide a real cause of numeric divergence. So it travels with
  the build and is consulted only during divergence triage.

The readable id is a **HUD label**. Tools always compare `oracle_engine_hash`.

---

## 6. Failure conditions — no silent fallback

Generation or validation **stops** on any of these. None is warning-only.

| Condition | Detected by | Exit |
|---|---|---|
| Engine pin invalid (`engine_version` or manifest mismatch) | `engine_access.load_engine` | 2 |
| Loaded module not governed (import shadowing) | `verify_loaded_modules` | 2 |
| A `PINE_RELEVANT_CONFIG` field vanished | `extract_contract` | 3 |
| An unsupported feature became **active** | `extract_contract` | 4 |
| Deployed value outside the supported set | `extract_contract` | 4 |
| Production enum grew a value Pine can't map | `enums.unmapped_against` | generation fails |
| Generated Pine file hand-edited | `check_freshness` (hash vs manifest) | 6 |
| Trace schema moved | `check_freshness` | 5 |
| Non-deterministic contract output | `test_contract_extraction_is_deterministic` | test fail |
| Golden config include-disabled flag flipped | `resolve_config` | 2 |

### The features that make the oracle INCOMPATIBLE if switched on

All are inert today (audit §6.15). If any goes live, the chart would be missing a
whole mechanism — that is *incompatible*, not merely stale:

`be_enabled`, `session_filter_enabled`, `reverse_touch_cancel_enabled`,
`triggered_edge_cancel_on_retrace`, `triggered_edge_cancel_on_first_failed_tag`,
`batch_entry_penetration`

plus any deviation from `protection_modes=["baseline"]`,
`execution_modes=["allow_multi_position"]`, `entry_models=["triggered_edge"]`,
`trade_direction="both"`, `detection_timeframe="15min"`,
`execution_timeframe="1min"`, `symbol="EURUSD"`,
`directional_entry_mode="symmetric"`.

---

## 7. Compatibility rules

1. A Pine build is compatible with **exactly one** `oracle_engine_hash`.
2. **Config-only changes still require regeneration.** Algorithms unchanged is not
   the same as output unchanged.
3. A newly **active** unsupported feature ⇒ `INCOMPATIBLE`.
4. A newly added but **inactive** feature ⇒ recorded, not blocking.
5. Changed limitation semantics ⇒ explicit review of the register.
6. Changed trace schema ⇒ every fixture regenerated.
7. Feed-driven divergence is tolerable; an algorithm mismatch never is.
8. **A partial implementation must never display `MATCHED` globally.**

Stage status is per-stage, and the global status is derived, never hand-set:

```
S1-S5   MATCHED
S6-S9   UNIMPLEMENTED
GLOBAL  PARTIAL PARITY
```

`global_status` is `MATCHED` only when **every** stage is `MATCHED` *and*
validation is `PASS`.

---

## 8. Things that will bite you

**The engine has no OB lifecycle state machine on the live path.** Order blocks
are rebuilt from scratch every cycle. The `ob_final_status` vocabulary you see in
`golden/run-001/.../order_blocks.csv` is a *driver-side post-hoc derivation*
(`run_backtest.py:3072-3167`), not live state.

**That vocabulary does not cover the dominant production outcome.** It has no
branch for `STATE_BLOCKED` / `COHORT_DISABLED` / `REGIME_BLOCKED`, so all three
fall through to `unknown`. Measured on the committed golden run: **809 of 2080
order blocks (38.9 %) — the largest single group — are `unknown`, and every one of
them is actually `state_target_block`.** The oracle therefore adds three explicit
statuses (`contract.oracle_status_extensions`) and shows `lifecycle_reason`, which
carries the truth. This is a **declared divergence from the driver vocabulary**,
not licence to invent labels elsewhere.

**`portfolio_include_disabled_cohorts: true` rewrites every `DISABLE` cohort to
`LABEL` in memory.** 12 of the 24 EURUSD cohorts read `DISABLE` in the deployed
JSON and **none of them can block**. Only 5 cohorts can block at all (4
`DIRECTION_AWARE`, 1 `STATE_ONLY`). Pine must encode the **effective** column;
the contract carries both so the override is visible rather than silent.

**`detect_swings` is dead on the live path.** The live pipeline uses the inlined
swing loop inside `detect_order_blocks`. Changing `strategy_core/swings.py` alone
has no oracle impact — the impact map says so explicitly.

**Three history-seeded values never converge on a short chart:** the ATR RMA seed
(first-value, not Pine's SMA seed), `swing_trend_bias` (never reset — can
permanently invert BOS/CHoCH tags), and the daily regime panel. Parity comparisons
must exclude a warm-up prefix; the trace header carries
`warmup_excluded_bars` for exactly this.

---

## 9. Bumping a schema version

| Bump | When | Consequence |
|---|---|---|
| `CONTRACT_SCHEMA_VERSION` | contract shape changes | fingerprint changes → all builds stale |
| `TRACE_SCHEMA_VERSION` | trace shape changes | **every golden fixture must be regenerated** |
| `GENERATOR_VERSION` | generator output changes for identical inputs | fingerprint changes → regenerate Pine |

All three live in `tools/oracle/fingerprint.py`.
`test_schema_versions_agree_between_code_and_contract` keeps the trace schema file
and the constant in step.

---

## 11. Stage S1 — the build, the trace, and how to verify it

### Rebuild everything

```bash
python -m tools.oracle.extract_contract --write && python -m tools.oracle.build_fixtures --write && python -m tools.oracle.generate_pine --write && python -m tools.oracle.verify_s1 --checklist --write-report
```

Each step is separately runnable; run them in that order after any Python change.

### Export a trace

```bash
python -m tools.oracle.export_trace --stage S1 --symbol EURUSD --timeframe 15min --input golden/tradingview_oracle/s1/F-S1-DAY.csv --output artifacts/tradingview_oracle/trace_F-S1-DAY.json
```

`--timeframe` accepts `1min`/`15min` (or `M1`/`M15`). Anything that does not divide 60
is **refused**: a session window is `[start, end)` on the UTC hour, so an H4 bar would
straddle a boundary and have two valid answers.

### Compare a TradingView export (Route A — preferred)

```bash
python -m tools.oracle.compare_tv_export --fixture F-S1-DAY --timeframe 15min --export <tv-export.csv> --feed "OANDA:EURUSD" --write
```

Right-click the chart → **Export chart data…** → CSV, then run this. It joins on
the UTC bar timestamp and diffs every oracle plot exactly, so the comparison is
mechanical rather than a reading exercise. Hand-transcribing Data Window values is
the most likely way to record a *wrong* parity result — a false `MATCHED` would be
inherited by every later stage.

Bars present on only one side are reported as `FEED_DIFFERENCE` (L-02) and do not
fail; a bar present on both whose values disagree does. An export missing the
oracle plot columns is **refused**, not silently skipped.

### Verify S1 (the only path to `MATCHED`)

`verify_s1` runs everything that can be run here — 11 fixtures, 3 context refusals, the
Pine linter, and a cross-check that the constants baked into the `.pine` match the trace
header. It then writes the operator checklist.

**It cannot compare Pine to Python.** TradingView has no headless or API run path, so
that step is manual and is limitation **L-20**. Until it is done:

```
S1: UNVERIFIED    GLOBAL: PARTIAL
```

To complete it: open `artifacts/tradingview_oracle/s1_tv_checklist.md`, follow it on a
EURUSD chart with the timezone set to **UTC**, then:

```bash
python -m tools.oracle.verify_s1 --record PASS --feed "OANDA:EURUSD" --compiled PASS --operator "<name>" --evidence artifacts/tradingview_oracle/s1_tv_compare.json
```

`--record PASS` is **refused** unless all of these hold — it cannot be used as a
rubber stamp, by anyone:

| Requirement | Why |
|---|---|
| Python side + static validation clean | a PASS over failures is a false claim |
| `--feed` given | candle differences explain later-stage divergence |
| `--compiled PASS` given | the actual Pine v6 compiler outcome must be recorded |
| comparator evidence for `F-S1-DAY` **and** `F-S1-BOUNDARIES` | the per-bar fields must be machine-proven |
| evidence fingerprint == manifest fingerprint | evidence from another build proves nothing |
| evidence compared > 0 bars | an empty overlap is not verification |

After recording, regenerate so the embedded `ORACLE_S1_STATUS` matches the manifest.

### Reading the chart output

Ten numeric values are plotted to the **Data Window** (Alt+D), each with an exact
counterpart in the trace. Match on `oracleBarEpochMs` → `bars[].bar_epoch_ms`, then
compare `oracleSessionCode`, `oracleDayKey`, `oracleTransCode`, `oracleUtcHour`,
`oracleCtxCode`, `oracleBarsSinceTrans`. All comparisons are **exact** — S1 has no
tolerance fields, and matching background colours by eye is not verification.

### S1 gotchas

**A weekend produces no session transition.** The FX week closes ~Fri 21:45 UTC and
reopens ~Sun 22:00 UTC — both inside the same `outside` window (17:00–24:00) — so the
~48 h gap is invisible to session logic. `gap_resync` needs a gap that *also* crosses a
session boundary. A Pine build that drew a weekend "session close" would be wrong.

**Duplicates survive.** `prepare_candles_for_simulation` sorts (stably) but does **not**
de-duplicate, so a duplicated timestamp reaches the walk twice. The trace shows both.

**Missing intervals are dropped, not filled.** So `bar_index` is not a time grid —
compare index *deltas*, never absolute values (L-09).

**Never hand-edit `pine/generated/`.** `check_freshness` compares its hash to the
manifest and reports `INCOMPATIBLE`. Editing a `pine/src/*.pinefrag` without
regenerating reports `STALE_CONTRACT`.

## 12. Packed plot transport (Wave 2b-i)

TradingView caps a script at **64 plot-family calls** and raises `RE10140` at
**runtime on a live chart** — not at compile time, and no static check caught it
until it was hit for real at 83 plots. Small-integer fields therefore travel
several to a plot.

`tools/oracle/session_codes.py::PACKED_SPEC` is the single authority. It maps a
**group** (one plot) to `[(field, width, offset), …]`, LSB-first mixed radix.
`PACKED_GROUPS` maps a stage to its groups — S5 has two, because its per-bar
decision flags and its latched identity distances differ in magnitude by three
orders and one radix would waste width on every field.

Three constants per field are generated into the Pine — `PACK_<G>_<i>`
(multiplier), `PACK_<G>_W<i>` (width), `PACK_<G>_O<i>` (offset) — so the
fragments contain **no hand-written radix arithmetic at all**. That is the only
arrangement in which the encode and the decode cannot drift.

**Widths are measured, not guessed.** Each carries its observed maximum over the
full production window in a comment, and `test_declared_width_leaves_measured_headroom`
fails if one is narrowed below it.

**Overflow fails loudly.** Pine has no range check, so `f_packv` (fragment 10)
CLAMPS into `[0, width-1]`. A clamped field produces a packed integer that cannot
equal Python's — whose `pack` raises outright — so the comparator reports a
mismatch instead of agreeing with a corrupted decode. `na` maps to code 0 before
the offset, which is why every optional field is specified with `offset = 1`.

Budget after Wave 2b-i: **50 of 64, 14 free.** `lint_pine` **errors** above 64 and
errors again if fewer than `PLOT_RESERVE` (2) remain.

### Changing the transport invalidates evidence

`compare_stages.export_schema_hash()` fingerprints the column list plus the
packed encoding, and it is stamped into every recorded logic-parity claim. The
engine hash does not move when only the Pine changes, so without this a packing
refactor would inherit a `LOGIC_MATCHED` verdict on a build whose exported
numbers had never been compared. Changing `PACKED_SPEC`, `PACKED_PLOT`,
`PACKED_GROUPS` or `STAGE_COLUMNS` demotes every affected stage to
`LOGIC_UNVERIFIED` with the prior claim preserved under `superseded`. **One fresh
export restores it.**

## 13. Stage S7 — why there is no Pine

`simulate_trades` does **not** run on 15-minute bars. `run_backtest` resamples to
`detection_timeframe` (15m) only to detect order blocks; the pending loop is
handed `candles`, which is the raw `candle_file` —
`EURUSD_1m_extended_2015_2026.csv`. Admission, arming, the delay counter and the
touch test are therefore **minute by minute**, and `triggered_edge_candle_delays
= 3` means three *minutes*.

A 15-minute bar carries four numbers. It cannot order arm → delay → touch inside
itself, so the pre-fill path is not reproducible on the current chart. The status
model records that as `LOGIC_UNATTAINABLE_IN_CURRENT_CONTEXT` with reason
`INTRABAR_EXECUTION_PATH_UNAVAILABLE` (limitation **L-21**) — a fifth vocabulary
that means "someone looked, and the chart cannot supply the answer", distinct
from FAILED (the Pine is wrong) and UNVERIFIED (nobody looked).

The Python reference exists and is self-checked against production:

```bash
python -m tools.oracle.replay_prefill --selfcheck --start 2026-01-01 --end 2026-03-01
```

It compares the **pre-fill terminal** of every admitted order block against
`simulate_trades`. Two defects were caught this way and both are pinned by tests:

* the touched-during-blackout cancel reuses the fill gate's arm/delay clause, so
  an **unarmed** triggered-edge order can never be news-cancelled;
* `INVALIDATED_BEFORE_EDGE_ENTRY` (`execution.py:2958`) is a real pre-fill
  terminal — price through the block, strict `<`/`>` against the block's far
  edge. Missing it made three setups in two months read as something else.

### Dimension 5 — input availability

Production reads an economic calendar (**L-22**). A news-affected bar is not a
Pine defect, not a feed difference and not an unverified bar, so it needed its
own dimension: `INPUT_AVAILABLE` / `INPUT_COVERAGE_PARTIAL` /
`INPUT_UNAVAILABLE`. `INPUT_COVERAGE_PARTIAL` is **refused without numbers** —
total, unavailable and scored bars must add up, because "matched on most bars"
with no denominator reads as a pass.

The 15-minute build no longer sits at `INPUT_UNAVAILABLE` for news. Run

```
python -m tools.oracle.export_news --write
```

and the schedule is generated into fragment 08 through production's **own**
`load_news_events` — its impact filter, its currency filter, its ±3 minute
margins. The chart then applies production's own inclusive per-minute match
(`strategy_core/news.py::_news_blackout_match`) across the fifteen 1-minute
candles inside each bar. Two things this deliberately is **not**:

* it is not an overlap test. A window opening 30 seconds after a bar's final
  minute overlaps that bar but contains none of its minute candles, and
  production's first blocked candle is in the *next* bar. `f_newsState` walks
  the minute grid instead, and `test_a_window_after_the_last_minute_of_a_bar_does_not_block_that_bar`
  pins exactly that case;
* it is not open-ended. `NEWS_LAST_MS` is a hard edge. Past it the verdict is
  **NEWS UNKNOWN**, never ALLOWED, because an exhausted calendar and a quiet one
  are indistinguishable from inside Pine and only one of them is permission. An
  UNKNOWN suppresses the risk-reward tool and prints `NEWS UNKNOWN` on the label
  instead of an R multiple; the HUD carries an unsuppressible
  `NEWS UNKNOWN - DATA ENDS <date>` row.

Re-export whenever the calendar file or any news config field moves — the
artefact records the filter it was built with and the tests compare it to the
deployed config.

## 15. Governed production trees are IMMUTABLE during tests

`LUX_ROOT` is the live trading engine. **No test may write to it — not even
temporarily.**

A tamper test must mutate an **isolated mirror** under `tmp_path`.
`backend/tests/test_engine_identity.py::_mirror_governed_tree` copies the
governed files out, preserving relative paths; because `governed_files` globs, a
directory containing exactly those files yields exactly the same manifest, so the
mirror is identity-equivalent to the live tree without being it.

**Write-and-restore is prohibited, and restoring the content does not make it
safe.** During the write window:

* the governed source on disk is genuinely corrupt, and `verify_engine()` would
  **refuse to start the trading node** if it restarted then;
* `backend/pytest.ini` runs `-n 2 --dist loadscope`, so a concurrent worker
  asserting engine identity against the same real tree can observe the corrupt
  state;
* the file's mtime is restamped on every run — which is how it was caught.

Enforced by `test_nothing_in_this_repository_writes_into_lux_root`, which scans
the whole repository and grades every write:

| grade | meaning | excusable? |
|---|---|---|
| **ROOT-ANCHORED** | the path is anchored at the live tree (`LUX_ROOT / …`) | **never** |
| tree-derived | a relative path from the tree, joined onto a tmp_path | by a marker |

A `# lux-write-audited: <reason>` comment directly above a `def` excuses **that
one function**, and only for tree-derived writes. It is scoped to the function
because a file-scoped marker once silenced an entire module. **No comment can
excuse a ROOT-ANCHORED write** — that rule is itself tested.

## 14. Two builds, not one

Production has two time domains, so the oracle has two generated builds. See
`docs/tradingview_visual_oracle_dual_architecture.md` for the full rationale.

| target | timeframe | owns |
|---|---|---|
| `detection_15m` | 15m | S1–S6 |
| `execution_1m` | 1m | S7+ |

`--write` **requires** an explicit `--target`. Defaulting a destructive operation
would let a typo regenerate the wrong oracle, and only one of them carries
recorded evidence.

```bash
python -m tools.oracle.generate_pine --target detection_15m --write
```

```bash
python -m tools.oracle.check_freshness --target execution_1m
```

Each target has its own manifest, export schema, plot budget and evidence.
`export_schema_hash(target)` is scoped, so a change confined to one build's
export surface cannot stale the other's recorded claims — tested in both
directions.

**The cross-timeframe handoff does not use `request.security`.** The 1-minute
build rebuilds the 15-minute frame itself, because production builds its
detection frame the same way (`resample_candles`: left-labelled, left-closed,
empty buckets dropped). Asking TradingView for 15-minute bars would introduce a
second aggregation nobody has verified. A bucket is finalised on the first
1-minute bar of the next bucket, so the frame is causal by construction.

## 16. Sessions are Europe/London wall-clock, not UTC

`strategy_core/sessions.py` (M-SESSION-DST-1, `e76fa92`, branch
`golden-run-001-engine`) classifies on `ZoneInfo("Europe/London")`. The schedule
numbers — 0/7/10/12/15/17 — are **London local** and never move; their UTC
projection moves with the UK clock:

```
London Lull = 10:00-12:00 Europe/London   ALWAYS
            = 09:00-11:00 UTC in BST
            = 10:00-12:00 UTC in GMT
```

Pine reads `hour(time, "Europe/London")` (fragment 30) and feeds that to
`f_sessionIndexForHour` (fragment 40). Nothing else is permitted: a fixed +1, a
month test or a hand-written transition table all fail in the ~3 weeks each
spring and autumn when the UK and US clocks disagree, and they fail differently
every year because the instants move.

Two places got this wrong and are now pinned by tests:

* **fragment 40** classified on `utcHour`, so for roughly seven months a year
  every cohort lookup used the wrong cell — silently, because both answers are
  valid session keys;
* **`tools/oracle/export_trace.py`** derived its reference from `ts.hour`, which
  would have scored a UTC-classifying chart as *correct*. It now calls
  `strategy_core.sessions._london_hour`.

`backend/tests/test_oracle_session_dst.py` compares Pine and production hour by
hour across 2025-2026 (17,520 instants, four transitions) and asserts the
spring-forward and autumn-back instants explicitly. Re-record the replay after
any session change: the corrected classifier moved two of the 109 recorded
setups between outcome buckets.

## 17. A resting order block is an opportunity, not a trade

The single most important semantic in the live layer. Production does **not**
decide a trade at detection; it records a candidate and waits, with no broker
order anywhere. Every execution input is read at the FILL candle:

| what | where |
|---|---|
| session cohort | `execution.py:2882` `_candle_session(candle)` |
| cohort cell | `execution.py:2950` `_cohort_index.get((_cohort_session_key(candle), …))` |
| market state | `execution.py:2965` `_regime_state_for_candle(candle, …)` |
| target rewritten | `execution.py:3008` `plan["tp"]` / `plan["rr_multiple"]` |

So a block detected in London on Monday and triggered in New York on Thursday is
evaluated against **Thursday's** session and state, and its target may be a
different number — or the cell may say BLOCK and no trade happens at all.

Fragment 68 therefore keeps two things apart:

* **box colour = structural / trade lifecycle.** BLUE resting · ORANGE trade
  active · GREEN won · RED lost · GREY terminal without a trade.
* **label = execution eligibility right now.** `NOW · London · Bull/Expand` /
  `TRADE 2.25R · provisional`, or `NOW BLOCK`, or `NEWS UNKNOWN`.

Overloading the box with both is what produced a chart of grey and orange
rectangles: grey meant "news unknown" and orange meant "bearish", so BLUE was
unreachable in practice.

**A BLOCK verdict never deletes the block.** Structural existence and execution
eligibility are different things. The block stays blue and keeps growing; if the
session or state later moves to an allowed cell the same block becomes tradeable
again, with no new block created.

The provisional preview is recomputed every bar (Option A), but only where its
answer can be read — on the bar a block resolves, on a phase change, and within
one bar of the right-hand edge. Session, state and news are hoisted to bar scope
because they cannot differ between blocks; what remains per block is four array
reads. Without that gate a per-bar refresh is ~40 blocks x ~40 string
concatenations x every bar on the chart.

## 18. The object budget is per SCRIPT, not per layer

Pine's ceiling is **500 objects per type for the whole indicator**. `indicator()`
defaults `max_lines_count` to **50** when it is omitted — and it was omitted,
while boxes and labels were raised to 500. The replay layer alone draws up to
four lines per setup, so entry/stop/target lines were being evicted by the
engine within a few setups. That is what "the risk-reward tools are not showing"
was, and Pine evicts silently rather than erroring.

Current worst case, asserted by
`test_the_object_budget_fits_pines_ceiling`:

| type | replay | live (4/3/1 x 50) | markers | total |
|---|---|---|---|---|
| lines | 150 | 200 | 120 + 2 | 472 |
| boxes | 150 | 150 | 120 | 420 |
| labels | 150 | 50 | 2 x 120 | 440 |

`i_maxObjects` backs FOUR buffers (`oracleLines`, `oracleLabels`, `s5_boxes`,
`s5_obLabels`), so it counts twice against labels. Raise any cap and re-run that
test before shipping.

## 19. The fill is the authority, and the arm is not

Production does not decide anything when a candidate arms. `execution.py` reads
the session (2882), the cohort cell (2950), the market state (2965) and rewrites
the target (3008) **at the fill candle**, and the fill only happens on the first
1-minute candle at index >= `arm + triggered_edge_candle_delays` whose range
STRADDLES the entry.

The chart used to treat the arm bar as the fill. Measured across every filled
trade production has on record (659):

| | count | share |
|---|---|---|
| arm and fill on a different 15m bar | 324 | **49.2%** |
| different session | 54 | **8.2%** |
| different UTC day (so a different market state) | 20 | **3.0%** |
| **would have selected a different matrix cell** | 57 | **8.6%** |

Worst case on record: armed 2020-11-23 in `ny_pm`, filled 2021-03-25 in `asia`.

So fragment 68 now has an explicit **ARMED** phase. It stays blue — an armed
order is not a position — and resolves nothing. Session, state, cohort,
eligibility and RR are read on the bar a fill is PROVEN.

### What 15-minute bars can and cannot prove

* A **later** straddling bar is proof: one bar is 15 minutes and the delay is 3.
* The **arm bar** itself may or may not contain a legal fill, and its minutes
  cannot be ordered. The approach that penetrates 25% has crossed the edge on
  the way in, so the arm bar almost always straddles — which is exactly why it
  cannot be assumed to be the fill.

When the arm bar straddles there are TWO candidate fill bars. If they agree on
session, state, eligibility and target the answer is the same either way and the
setup resolves. If they disagree the setup is **UNDECIDABLE** (cyan) and the
chart picks nothing. Nothing fabricates the minute (L-21).

`request.security_lower_tf` was investigated as a way to recover the real minute
grid. It is rejected for now: TradingView caps total intrabars (~20k-40k
depending on the account), silently returning `na` beyond that, so decidability
would depend on the viewer's subscription tier — a non-deterministic input to a
parity claim.

## 20. The replay must reach as far as production does

`export_replay` ran over production's FROZEN candle file, which ends 2026-06-19.
The running node continues on a live MT5 segment, so its trades extend months
past that. A 2026-07-29 trade was invisible on the chart for exactly this
reason — the recording's INPUT stopped 27 days before the order block was
detected. That is not a lifecycle divergence and no amount of Pine work fixes it.

```bash
python -m tools.oracle.snapshot_execution_feed --from <the live 1m csv> --write
python -m tools.oracle.export_replay --seam --write
```

The first step exists because `tools/oracle/` is forbidden from naming the live
node's directory at all (`test_tooling_never_touches_live_state_or_the_broker`
walks the AST for the literal), and that guard is worth keeping: nothing in the
oracle toolchain should have a hard-coded route into the node's state. So the
path is supplied by the operator, the file is copied into an oracle-owned
artefact with a digest and a span, and the trailing row is dropped because the
node appends while we read.

The seam rule is production's own, from `provenance.json`: frozen rows win at or
before the frozen end, live rows win after. `filter_date_range` is applied to the
FROZEN part only — it clips to `cfg.end_date`, which would discard exactly the
rows the extension exists to add.

**The seam is declared, never blended away.** The artefact carries a `seam` block
(frozen end, live row count, source name and sha), the build emits `RP_SEAM_MS` /
`RP_SEAM_TXT`, and the HUD shows `feed seam  frozen to <date>, live MT5 after`.
A recording that spans two feeds must say so.

Recovered on the first run: **109 -> 135 setups**, 26 detected past the frozen
end, 8 of them COMPLETED.

## 10. Quick reference

```bash
python -m tools.oracle.extract_contract
```

```bash
python -m tools.oracle.extract_contract --write
```

```bash
python -m tools.oracle.impact --vs-contract
```

```bash
python -m tools.oracle.check_freshness --json
```

```bash
python -m pytest backend/tests/test_oracle_contract.py -q
```
