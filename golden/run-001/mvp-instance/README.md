# ⛔ ABANDONED — see ABANDONED.md. Superseded 2026-07-16 by the full-strategy scope
# (the complete July 14 reference run is the behavioural target, not one cohort).
# Preserved for audit history only. Original content below.

# MVP Instance — Derived Configuration (golden/run-001/mvp-instance)

**Cohort:** `EURUSD | ny_pm | choch_short` (locked; verified ENABLED in deployed policy `2026-07-09.te-v1.2-surgical-disable`, `DIRECTION_AWARE`, confidence HIGH, n=66).

## Files
- `config.mvp.json` — derived from the reference run's `config.json` (sha `d1dc4518…`) by ONE change: `session_strategy_scenario.cohorts[*].enabled` set true **only** for `ny_pm | CHoCH | Short` (24→1 enabled). Every other field byte-equal. Provenance embedded under `_derived_from`. sha256 `3e23f445c2ffd78847f8d65533d9afee928651a53dd207c16d05c62713e3b35a`.
- `expected_cohort_slice.from_reference.csv` — the 54 reference-run rows for this cohort (18 WIN / 16 LOSS / 19 REGIME_BLOCKED / 1 STATE_BLOCKED; decided n=34; **net_r sum +2.94R** after costs). This is the behavioral expectation any derived run / extracted core must reproduce for this cohort. sha256 `c71eae5a…`.

## Derived run status: DEFERRED (deliberate)
The derived config has **not** been executed. Reasons, recorded for honesty:
1. The as-run engine is Lux HEAD `6be5bbb` **plus uncommitted working-tree changes** (`src/config.py`, `src/execution.py` — additive `bookable_mfe_*` analytics — plus the driver `scripts/run_backtest.py`; all modified 2026-07-12/13, i.e. *before* the reference run, so the reference run itself used them). Until that tree is committed/tagged, a derived run cannot be attributed to a pinned revision — undermining the behavioural-equivalence discipline this archive exists to protect. Commit/tag first; the manifest `engine_version c5e29837…` is the verification anchor.
2. The reference run already contains this cohort's complete, PM-enforced slice (the expected_cohort_slice above); the derived run adds no new behavioural information for Milestone 1's purpose (scope lock + immutable reference).
3. Executing the derived run is the natural first act of **Milestone 3 (golden parity)**, after the driver revision is committed/pinned — run it then, diff against `expected_cohort_slice.from_reference.csv` (candidate-for-candidate, block-for-block, R-for-R), and only then extract.

**Do not edit `config.mvp.json` or the slice. A change means a new derivation with new hashes.**
