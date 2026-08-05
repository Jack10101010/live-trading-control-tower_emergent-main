# ENGINE-IDENTITY.md — what engine identity covers, and what it used to miss

**Milestone:** M-CAP-GOV-1
**Status:** implemented on the isolated Lux branch `capacity/m-cap-opt-2-engine`.
**Nothing is deployed. No production pin has been changed.**

---

## 1. The defect

`engine_version()` (`src/run_outputs.py`) hashed a hand-curated three-file list:

```python
_ENGINE_VERSION_SOURCES = ("src/execution.py",
                           "scripts/run_backtest.py",
                           "src/resume_support.py")
```

The M2 slices relocated the strategy into `strategy_core/`. `src/execution.py`
became a ~102-line shim that re-exports `strategy_core.execution`. The list was
never updated. Measured:

| File | Governed before? | Lines | Contains |
|---|---|---:|---|
| `src/execution.py` | **yes** | 102 | `import strategy_core.execution as _core_execution` |
| `strategy_core/execution.py` | **no** | 3,920 | `simulate_trades` — the entire trade walk |
| `strategy_core/order_blocks.py` | **no** | 222 | `detect_order_blocks`, `ob_id` assignment |
| `strategy_core/ghost_tracker.py` | **no** | 598 | ghost state |
| `strategy_core/{news,scenario,swings,policy,regime,sessions,types,ports}.py` | **no** | — | relocated strategy logic |

### Reproduced numerically

M-CAP-OPT-2 modified `strategy_core/execution.py` — the trade walk. On that
worktree the **old policy still produces exactly the production pin**:

```
production pin              5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be
v1 policy, Option A present 5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be   ← identical
```

Pinned as a regression test:
`tests/test_engine_identity_coverage.py::test_option_a_defect_reproduced_v1_blind_v2_sees_it`.

## 2. Who trusted it, and what each did with it

| Consumer | Location | Failure behaviour | Gap before repair |
|---|---|---|---|
| `LuxSession.verify_engine()` | `live/runner.py:106` | raises "refusing to trade on an unverified engine" | accepted a modified trade walk |
| Preflight | `live/deploy_check.py:92` | check fails | same |
| Benchmark harness | `live/benchmark.py:242` | aborts | same |
| Run manifest stamp | `scripts/run_backtest.py:1533` | records the value | stamped a stale identity |
| RESUME version-drift guard | `run_backtest.py:3272` → `adopt_run_output_dir` | refuses to adopt a folder | would have reused outputs from a *different* engine |
| **`input_revision`** | `live/runner.py:145-152` | C3 gate skips re-evaluation | **a changed engine did not invalidate the cached boundary decision** |

The last row is the most serious: `engine_version` is hashed into
`input_revision`, so an uncovered engine change also failed to force a recompute.

### Did any other layer cover `strategy_core/`?

No. Checked, not assumed:

* **Archive manifest** (`golden/run-001/ARCHIVE-MANIFEST.json`) — hashes 14
  artefacts: datasets, configs, reference outputs, the deployed policy. **No
  source files at all.** Its `engine_identity.engine_version` is
  `c5e29837…`, a stale stamp from archive time that matches neither the current
  pin nor the repaired value.
* **`detection_config_hash` / `execution_config_hash`** — cover *configuration*,
  not code.
* **`input_revision`** — covers *data* bytes plus `engine_version`, so it
  inherited the same blind spot.

## 3. The repair

A **deterministic recursive policy** replaces the curated list, so a new module
cannot silently escape coverage:

```python
ENGINE_VERSION_POLICY  = "lux.engine-version.v2"      # in the preimage
_ENGINE_SOURCE_ROOTS   = (("strategy_core", "**/*.py"),   # whole package
                          ("src", "*.py"))                # flat package
_ENGINE_SOURCE_FILES   = ("scripts/run_backtest.py",)     # the driver
_ENGINE_SOURCE_EXCLUDE_SUFFIXES = ("_test.py",)
_ENGINE_SOURCE_EXCLUDE_PARTS    = ("__pycache__",)
```

**30 files governed:** 12 `strategy_core/`, 17 `src/`, 1 driver.

### Inclusion / exclusion rationale

| Included | Why |
|---|---|
| `strategy_core/**/*.py` | it *is* the strategy — detection, trade walk, ghosts, news, scenario, swings, policy, regime |
| `src/*.py` | driver-side semantic modules (config, data_loader, resample, structure, portfolio_policy, resume_support, the shim, …) |
| `scripts/run_backtest.py` | the driver that composes a run |

Deliberate **over-coverage**: a few `src/` modules (e.g. `reports.py`) probably
cannot change trade output. They are included anyway — over-coverage costs an
occasional unnecessary identity change; under-coverage costs an unverified
engine trading real money.

| Excluded | Why |
|---|---|
| `tests/`, `*_test.py` | cannot affect production behaviour |
| `__pycache__` | build artefacts |
| `scripts/*` except the driver | research/one-off tooling |
| datasets, configs, outputs, runtime state | data identity belongs to the run manifest and `input_revision`, not to *code* identity |

### Properties, each pinned by a test

| Property | Test |
|---|---|
| one-byte change in any governed file moves identity | `test_one_byte_change_moves_identity` (9 files) |
| a new module is covered automatically | `test_added_semantic_module_is_covered_automatically`, `…nested_package…` |
| delete / rename moves identity | `test_deleting…`, `test_renaming…` |
| docs / tests / data do **not** move it | `test_non_semantic_change_does_not_move_identity` (5 paths) |
| enumeration-order independent | `test_enumeration_order_does_not_matter` (glob reversed) |
| location independent | `test_identity_is_location_independent` |
| separator independent | `test_manifest_keys_are_posix_separated` |
| byte-based, not timestamp-based | `test_identity_is_byte_based_not_timestamp_based` |
| path is in the preimage (content swap detected) | `test_content_swap_between_two_files_moves_identity` |
| missing declared file / root / empty root fails closed | `test_missing_declared_file…`, `test_missing_root…`, `test_empty_root…` |
| symlink escape refused | `test_symlink_escape_is_refused` |
| duplicate root refused | `test_duplicate_source_root_is_refused` |
| policy literal participates | `test_policy_literal_participates_in_the_hash` |
| **the Option A defect** | `test_option_a_defect_reproduced_v1_blind_v2_sees_it` |

### Deliberate behaviour change

v1 recorded a missing source as `<absent>` so the stamp was "always computable".
v2 **raises**. Silently stamping an identity for an incomplete tree is exactly
how an unverified engine reaches production; an engine whose sources cannot be
enumerated has no identity to report.

## 4. `engine_version` vs the manifests — kept distinct

Not collapsed. Three different questions:

| Artefact | Question it answers | Gate? |
|---|---|---|
| `engine_version()` | "is this the same **code**?" — one opaque digest | **yes** — `verify_engine`, and embedded in `input_revision` |
| `engine_source_manifest()` *(new)* | "**which file** changed?" — itemised `relpath → sha256` | no — diagnostic and re-pin support |
| run manifest (`run_backtest.py`) | run **provenance**: code identity *alongside* config and data identity | no — a record |

## 5. Identities

| Label | Value |
|---|---|
| Pre-repair production pin (`ENGINE_VERSION_EXPECTED`) | `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` |
| v1 policy on the Option A worktree | `5bb6372c…` — **identical: the defect** |
| v2 policy @ `d978074` (no Option A, no repair) | `2604fae01ae968e4e930073c0546a10e3969027818096a8092b2e200c02c46db` |
| v2 policy + governance repair only | `66a9f1642aa90842ca9ae052b89a2b4c2319b3deddc89fac005f405d06f64794` |
| **v2 policy + repair + Option A (proposed)** | **`33e1a089705cf3a8c9601bd96d29d79d4172cb82432d8417294d47b3acfd27ec`** |

Files responsible for each step:

* `2604fae0…` → `66a9f164…` — **`src/run_outputs.py`**, which is now
  self-governed (the hashing policy is part of the identity it computes).
* `66a9f164…` → `33e1a089…` — **`strategy_core/execution.py`** (Option A). This
  is the transition v1 could not see.

## 5a. STATUS UPDATE — M-CAP-REPIN-1 applied to the CANDIDATE branch

The plan in §6 below has now been **executed on the isolated candidate branch
only**. Production remains on `5bb6372c…`.

| Pin | Old | New |
|---|---|---|
| `live/config.py ENGINE_VERSION_EXPECTED` | `5bb6372c…` | **`559fcb66…`** |
| `live/deploy_check.py EXPECTED_COMMIT` | `d978074a…` | `b96fa7ae…` |
| `live/rehearsal.py EXPECTED_COMMIT` | `d978074a…` | `b96fa7ae…` |
| `ARCHIVE-MANIFEST.json engine_identity` | `c5e29837…` | **annotated, not overwritten** |

Candidate manifest id: `6e036e0da4cf2b2b20cd52cc254e246b7e2074f062bc280980b3e8c435e56204`.

**A second pin was found by tooling, not by inspection.** `deploy_check
preflight` failed `lux_commit_pinned` after the engine re-pin — there is a
separate Lux *commit* pin that my grep for the engine hash missed, and it is
**duplicated** across `deploy_check.py` and `rehearsal.py`. Both are updated and
cross-referenced. This is the same duplicated-constant hazard that produced the
original `engine_version` defect; it is recorded as **R-11** rather than
refactored, to keep this milestone a re-pin and not a redesign.

Full package: `PROMOTION-PACKAGE.md`.

## 6. Re-pin plan — proposed, NOT applied

Strictly separated:

| Layer | Status |
|---|---|
| **Code implementation** | done, on the isolated Lux branch |
| **Proposed production identity** | `33e1a089…` — written here only |
| **Actually deployed value** | **unchanged: `5bb6372c…`** |

`live/config.py :: ENGINE_VERSION_EXPECTED` has **not** been touched.

### Consequence to plan for

With the repair in place and the pin unchanged, `verify_engine()` now **fails**
against the repaired worktree — which is the gate working. Concretely:

* `live/benchmark.py:242` calls `verify_engine()` and will abort. Measurement
  harnesses that call `execute_scenario_job` directly (the interleaved A/B used
  in this workstream) are unaffected.
* `live/deploy_check.py` preflight will report a mismatch.

### Sequence when deployment is authorised

1. Land the governance repair and confirm Golden parity (byte-identical trades).
2. Decide whether Option A ships in the same release — it changes the value.
3. Set `ENGINE_VERSION_EXPECTED` to the value produced by the exact tree being
   deployed (`33e1a089…` for repair + Option A; `66a9f164…` for repair alone).
4. Refresh `golden/run-001/ARCHIVE-MANIFEST.json → engine_identity`, currently
   the stale `c5e29837…`.
5. Run `deploy_check` and confirm the engine check passes.
6. Shadow-run before enabling live trading; confirm the trade frame still hashes
   to the Golden value.
7. Rollback = restore the previous `ENGINE_VERSION_EXPECTED` and revert the
   engine commit; both are one-line changes.

**Do not re-pin by copying a value from this document.** Recompute it from the
tree actually being deployed — a pin transcribed from a doc is exactly the class
of error this milestone exists to remove.
