# PROMOTION-PACKAGE.md — M-CAP-REPIN-1

**Deployment-ready package. NOTHING IN IT HAS BEEN DEPLOYED.**
No VPS contact, no MT5 contact, no service restart, no production branch mutated.

---

## 1. Candidate commits

| Repo | Branch | Commit | Contents |
|---|---|---|---|
| Lux | `capacity/m-cap-opt-2-engine` | `748f86b` | M-CAP-OPT-2 — Option A (triggered-edge gate) |
| Lux | ″ | `119f2d6` | M-CAP-GOV-1 — governance repair |
| Lux | ″ | **`b96fa7a`** | M-CAP-OPT-3 — Option B (prefix metric) ← candidate HEAD |
| Control Tower | `capacity/m-capacity-consolidate-1` | `4f375fd` | capacity evidence + governance docs |
| Control Tower | ″ | *(this milestone)* | M-CAP-REPIN-1 re-pin |

Production branches, unchanged and to remain so until promotion:
`golden-run-001-engine @ d978074` · `live-implementation-prep @ d6e776f`.

## 2. Computed identities — recompute, do not transcribe

| Field | Value |
|---|---|
| Policy | `lux.engine-version.v2` |
| Governed sources | **30** (12 `strategy_core/`, 17 `src/`, 1 driver) |
| **engine_version (candidate)** | **`559fcb66385e5e9fe757e61dfdc5e9c01d50abd358cdbb771105403d874a8c04`** |
| **manifest id (candidate)** | `6e036e0da4cf2b2b20cd52cc254e246b7e2074f062bc280980b3e8c435e56204` |
| engine_version (production, pre-repair) | `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` |

**Re-derive both from the tree you are actually deploying:**

```
cd <lux-root> && python3 -c "
from src.run_outputs import engine_version, engine_source_manifest
import hashlib, json
print('engine_version:', engine_version())
m = engine_source_manifest()
print('manifest_id   :', hashlib.sha256(json.dumps(m, sort_keys=True, separators=(',',':')).encode()).hexdigest())
print('governed      :', len(m), 'files')"
```

A pin transcribed from a document is the exact failure class this workstream
exists to remove.

### Files causing identity movement (vs production `d978074`)

| File | Why |
|---|---|
| `src/run_outputs.py` | the governance policy itself, now self-governed |
| `strategy_core/execution.py` | Options A + B (the trade walk) |

Nothing else. No file was added or removed from the governed set.

## 3. Candidate pin changes

| Location | Change |
|---|---|
| `live/config.py` `ENGINE_VERSION_EXPECTED` | `5bb6372c…` → **`559fcb66…`** |
| `golden/run-001/ARCHIVE-MANIFEST.json` `engine_identity` | **annotated, not overwritten** — see below |
| `backend/tests/test_engine_repin_contract.py` | new: proves exactly-one-identity acceptance |

**Why the archive manifest was annotated rather than rewritten.** Its
`engine_identity.engine_version` (`c5e29837…`) records the engine that produced
*that archived run*. Overwriting it with the candidate value would claim the
archived outputs came from an engine that did not produce them — falsifying
provenance. Nothing reads it programmatically. It now carries `_policy`,
`_status` and `_candidate_engine_version_m_cap_repin_1` fields so a reader
cannot mistake a stale stamp for a live gate.

**`rehearsal-report.json` was deliberately left alone** — it is *output* from a
past rehearsal run (written by `live/rehearsal.py`, never read). It will be
regenerated when rehearsal runs against the candidate.

## 4. Golden parity — PASS

Run through the **real production entry point** (`LuxSession` + `LiveRunner.golden_pipeline`),
not a test harness.

| Run | rows | SHA-256 | verdict |
|---|---:|---|---|
| cold (fresh process) | 2,060 | `b43e32489453ff8f…` | PASS |
| warm iteration 1 | 2,060 | `b43e32489453ff8f…` | PASS |
| warm iteration 2 | 2,060 | `b43e32489453ff8f…` | PASS |
| independent fresh process | 2,060 | `b43e32489453ff8f…` | PASS |

Outcome distribution identical to canonical: INVALID 809, STATE_BLOCKED 457,
WIN 441, LOSS 194, REGIME_BLOCKED 100, UNFILLED 35, NEWS_FLATTEN 15,
NEWS_TOUCH_CANCEL 9.

Inputs verified byte-identical to the proven benchmark inputs (candle CSV is a
**hardlink** — same inode — plus SHA-256 equality on news, Golden config and
deployed policy).

## 5. Identity-consumer validation

| Consumer | Result |
|---|---|
| `LuxSession` binding + chdir contract | OK |
| `verify_engine()` | **ACCEPTED** the candidate |
| `engine_version()` recomputed in-process | matches the pin |
| `engine_source_manifest()` / manifest id | 30 files, `6e036e0d…` |
| **`input_revision`** moves with engine identity | **confirmed** — old vs new preimage digests differ |
| `deploy_check` preflight | uses the same constant (see §9 commands) |
| Run manifest generation | stamps `engine_version()` |
| RESUME drift guard | compares `engine_version()` |
| CT engine-version contract test | follows the constant |

### Negative controls — the gate accepts exactly ONE value

18 tests in `backend/tests/test_engine_repin_contract.py`:

* accepts `559fcb66…`
* **rejects** `5bb6372c…` (old production), `2604fae0…` (v2, no changes),
  `66a9f164…` (governance only), `33e1a089…` (repair + Option A only)
* rejects empty, `None`, malformed, all-zero, **upper-cased**, over-long,
  truncated, and space-padded variants
* `test_the_gate_accepts_exactly_one_value_not_a_set` — guards against anyone
  widening the check to unblock a deploy
* rejection message names both values for diagnosis

Source-level controls (from M-CAP-GOV-1, re-verified against this tree):
one-byte change to `strategy_core/execution.py`, `order_blocks.py` or
`ghost_tracker.py` **moves** identity; README, tests, `*_test.py`, datasets and
non-driver scripts **do not**; missing governed source, empty root, symlink
escape and duplicate root all **fail closed**.

## 6. Shadow deployment plan — NOT executed here

Preconditions, all to be confirmed **before** any restart:

* `dry_run` remains **on**; AutoTrading **off**
* open positions = 0, pending orders = 0, ledger entries for the session = 0
* current engine preserved for rollback (see §7)
* a clean stop: allow the in-flight cycle to finish, then stop — never kill
  mid-`save()` (the atomic-rename window is the one place a hard kill hurts)

Bounded observation window:

1. Deploy candidate commits to the VPS Lux + CT checkouts.
2. Start with `dry_run` on. Confirm `deploy_check` passes and `verify_engine`
   accepts — a refusal here means the deployed tree is not the candidate tree.
3. Run for **≥ 20 completed boundary cycles**.
4. For every cycle, compare the produced trade frame and intents against the
   canonical oracle; require `b43e3248…` on a full-history evaluation.
5. Require: no new errors, no identity refusals, no frozen-state entries, no
   `identity_drift_frozen` from M-CAP-GUARD-1.
6. Collect real cycle durations from `ops/cycles.jsonl` (§8).
7. Only then consider disabling `dry_run` — a separate decision, not part of this.

## 7. Rollback

| Level | Action | Restores |
|---|---|---|
| 1 | `METRIC_PREFIX_ENABLED = False` | drops Option B; identity → `33e1a089…` |
| 2 | `TE_GATE_ENABLED = False` too | drops A+B; identity → `66a9f164…` |
| 3 | revert Lux to `d978074`, restore `ENGINE_VERSION_EXPECTED = 5bb6372c…` | full pre-milestone state |

**Rollback identity is recoverable**: `5bb6372c…` is recorded here, in
`live/config.py`'s comment block, and in `docs/capacity/governance/ENGINE-IDENTITY.md`.
Levels 1–2 are one-line changes but **still move engine identity**, so each
requires its own matching pin — a flag flip alone will be *refused* by
`verify_engine`. That is correct behaviour and must be planned for, not
worked around.

## 8. Post-deployment measurement gate

Minimum **20 completed trading-day cycles** before any performance claim:

| Metric | Source | Baseline to beat |
|---|---|---|
| min / median / p90 / p95 / max cycle | `ops/cycles.jsonl` `duration_s` | median 1,387 s, p95 1,850.8 s |
| boundary coverage / skipped boundaries | cycle boundaries vs 15 m grid | 36 % skipped |
| CPU / RSS | host counters | CPU-bound; Mac peak 2,751 MiB |
| telemetry delivery | ingest 200s, no gaps | — |
| cycle errors / frozen state | `ops/cycles.jsonl` `error`, `frozen` | zero |

**Projection to be tested, not assumed:** median ≈ 1,090 s, ≈1.21× the 900 s
budget, skipped boundaries ≈17 %.

**Stated plainly: A+B is NOT projected to meet the 900 s bar interval.** It is
projected to get below 20 minutes and to roughly halve skipped boundaries. If
the measured result is materially worse than ~1,090 s, the Mac→VPS ratio
assumption (5.41×) is wrong and the projection should be discarded, not adjusted.

## 9. Validation commands

```
# identity (run in the deployed Lux root)
python3 -c "from src.run_outputs import engine_version; print(engine_version())"

# preflight
cd <ct-root> && python3 -m live.deploy_check

# rehearsal (regenerates rehearsal-report.json against the candidate)
cd <ct-root> && python3 -m live.rehearsal

# gate contract
python3 -m pytest backend/tests/test_engine_repin_contract.py -q

# governance + options
cd <lux-root> && python3 -m pytest tests/test_engine_identity_coverage.py \
    tests/test_te_gate_equivalence.py tests/test_te_gate_adversarial.py \
    tests/test_optb_range_extremum.py tests/test_optb_metric_contract.py -q

# full-history Golden parity  (expect b43e3248…, 2,060 rows)
```

## 10. Pre-deploy evidence checklist

- [ ] candidate commits present on the VPS checkouts, HEADs match §1
- [ ] `engine_version()` on the VPS tree equals the pin — recomputed, not copied
- [ ] `deploy_check` passes
- [ ] Golden parity `b43e3248…` / 2,060 rows on the VPS tree
- [ ] `dry_run` on, AutoTrading off, positions/orders/ledger zero
- [ ] previous engine commit + old pin recorded for rollback
- [ ] `ops/cycles.jsonl` rotated or baselined so before/after is separable
