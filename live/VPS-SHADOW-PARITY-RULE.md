# VPS-SHADOW-PARITY-RULE.md — what promotes a candidate, exactly

Companion to `GOLDEN-ARTEFACT-CONTRACT.md`. Normative.

---

## The rule

> **A candidate is promotable to shadow when its full-history
> `golden.entry_trades.file.v1` output is BYTE-IDENTICAL to production's,
> produced on the same VPS from the same inputs in the same environment —
> verified by `live/golden_compare.py --mode same-host`, which also writes a
> complete zero-mismatch field census.**

No tolerances. Not on diagnostics, not on anything: on the same host there is no
legitimate source of float divergence, so any difference is a real behaviour
change.

## What the archive comparison is — and is not

`--mode archive` compares against the Mac-produced Golden archive under the
narrow policy (`allowlist: ema_value, px_vs_ema, bbw_value, adx_value`;
`max_ulp: 2`; everything else exact; every mismatch enumerated as permitted or
forbidden).

* **PASS** → cross-machine corroboration on top of the gate.
* **FAIL with only allowlisted ≤2-ULP mismatches** → cannot happen (that is a PASS).
* **FAIL with anything else** → STOP. That is not float noise; it is a
  behaviour difference or an environment problem. Do not promote, do not widen
  the allowlist, do not regenerate the archive.

The archive can **block** promotion by revealing a forbidden mismatch; it can
never **grant** promotion. Only same-host equality grants.

## Validation commands (VPS)

```powershell
# 1. produce candidate + production outputs on the SAME host, same inputs
#    (run each engine once; write each entry_trades CSV to a distinct dir)

# 2. THE GATE — candidate vs production, same host
python -m live.golden_compare --mode same-host `
  --ref  C:\path\to\production\trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv `
  --cand C:\path\to\candidate\trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv `
  --report C:\path\to\evidence\same-host-census.json

# 3. THE WITNESS — candidate vs the Mac archive (full census, narrow policy,
#    plus the recorded dataset-revision ledger)
python -m live.golden_compare --mode archive `
  --ref  <ct-repo>\golden\run-001\reference\trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv `
  --cand C:\path\to\candidate\...csv `
  --known-input-drift <ct-repo>\live\golden_known_input_drift.json `
  --report C:\path\to\evidence\archive-census.json

# 3b. pin the inputs the digests are a function of
Get-FileHash <lux>\data\candles\EURUSD_1m_extended_2015_2026.csv   # expect ba32de0d…
Get-FileHash <lux>\data\news\master_economic_calendar_2020_present.csv  # expect 7e37fc88…

# 4. rehearsal — ABSOLUTE PATHS ONLY (fail-fast validates the ref up front)
python -m live.rehearsal --lux-root <abs> --golden-ref <abs> --out <abs>

# 5. candidate suite — refuses untracked test pollution
$env:CANDIDATE_VALIDATION = "1"
python -m pytest backend\tests -q
```

## Expected rehearsal outcome on the VPS, stated in advance

`rehearsal.py` pins the Mac-producer digests (`ca925d54…` etc.), which are
**unreproducible from a clean checkout on any machine**: the archive-era candle
bytes (`314a0efa…`) were replaced on 2026-08-02 by the rebuilt dataset
(`ba32de0d…`). Its three `byte_parity_*` checks are therefore **expected to
fail**, and the failure census must decompose EXACTLY into (a) the three
`r_if_no_target` cells in `live/golden_known_input_drift.json` and (b)
allowlisted diagnostic fields within 2 ULP. Confirm with the archive-mode
census (which applies both); anything else is a real alarm. A rehearsal
byte-parity failure whose census is fully explained this way is the documented
input-revision + cross-machine case, not a candidate defect. Every other rehearsal check
(bootstrap, frontier diff, no-duplicate intents, dry-run suppression,
determinism, row counts, net_r) must PASS — those are machine-independent.

## Abort conditions (any one → roll back, report, stop)

* same-host candidate vs production: any byte or census difference;
* archive census: any forbidden mismatch (wrong field, >2 ULP, identifier,
  timestamp, outcome, economic value, row count/order);
* verify_engine or engine_manifest refusal on the deployed tree;
* rehearsal failure in any machine-independent check;
* candidate suite failures beyond the two documented environment errors;
* any position, order, or non-dry-run action appearing anywhere.

## Test-count contract

Clean checkout, candidate suite (`CANDIDATE_VALIDATION=1`):

| Host | Expected |
|---|---|
| Mac (no MT5 credentials, candidate Lux as sibling) | **467 passed, 2 skipped**, 2 known `test_control_tower_api.py` errors |
| VPS (MT5 present) | **469 passed, 0 skipped**, same 2 known errors |

(Measured on this branch in a VPS-equivalent layout, not projected. A Mac run
whose sibling Lux checkout is PRODUCTION instead of the candidate additionally
fails the 4 layout-dependent `real_lux` tests — a path-resolution artefact of
the developer machine, documented in M-CAP-INTEGRATE-1, not a candidate defect.)

The two skips are `MT5_LOGIN`-gated scope-guard tests. The prior confusion
(431 vs 433) was exactly this plus six untracked `test_oracle_*` files inflating
VPS collection — which `CANDIDATE_VALIDATION=1` now refuses by name.

## Deployment switching and rollback — the paired-tree rule

CT and Lux deploy and roll back as ONE unit. A switch is not "done" when the
`git checkout` returns; it is done when the coherence block below passes. This
section exists because of a real incident (2026-08-06): a rollback ran
`git checkout` in both repos, Lux succeeded, CT **aborted** on a locally
modified file, and the tree sat with CT on the candidate and Lux on production
until verification caught it.

### Never `git stash pop` during a deployment or rollback

`git stash pop` is the mechanism that caused the incident: a pop that partially
applies leaves modified files in the working tree, and the next branch switch
aborts on them — after the sibling repo has already moved. During switching:

1. Preserve local work as **plain file copies** into the current
   `artifacts/<milestone>/` evidence directory (outside tracked source).
2. `git checkout -- <file>` to clear the tree.
3. Switch branches.
4. Copy the preserved files back only AFTER the coherence block passes, and
   only if they belong on the now-current branch.

`git stash` may still hold a safety duplicate (`git stash push` then later
`git stash apply` + verify + `git stash drop`), but a switch must never DEPEND
on stash state, and `pop` — which deletes on a half-success — is banned in
this procedure outright.

### The coherence block

Run after EVERY switch, deploy or rollback, before reporting it complete:

```
CT   HEAD                    == the intended CT commit
Lux  HEAD                    == the intended Lux commit
ENGINE_VERSION_EXPECTED       (live/config.py)      recomputed == pinned
ENGINE_MANIFEST_ID_EXPECTED   (live/config.py)      manifest verify PASS
EXPECTED_COMMIT               (live/deploy_check.py) == Lux HEAD
EXPECTED_COMMIT               (live/rehearsal.py)    == Lux HEAD
git status --porcelain        no tracked modifications in either repo
```

Any line failing = the switch DID NOT HAPPEN, regardless of what individual
git commands printed. Fix and re-verify before anything else — the forbidden
state is not "on the old version"; it is CT and Lux on MISMATCHED versions
with pins that describe neither.

### Rollback target

Rollback restores the last known-coherent pair — currently:

* CT  `hardening/m3-p1-production-fixes`
* Lux `golden-run-001-engine`

whose pins (`ENGINE_VERSION_EXPECTED`, `ENGINE_MANIFEST_ID_EXPECTED`, both
`EXPECTED_COMMIT`s) travel WITH the CT commit — never edit pins during a
rollback; checking out the CT commit restores them by construction.
