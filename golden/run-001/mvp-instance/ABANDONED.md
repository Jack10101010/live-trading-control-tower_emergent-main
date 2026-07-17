# ⛔ ABANDONED DERIVATION — DO NOT USE

**Status: ABANDONED (2026-07-16, operator scope correction).** The one-cohort narrowing in this
directory (`config.mvp.json`, 24→1 cohorts) was based on an earlier MVP planning assumption that
was **rejected by the operator** on 2026-07-16.

**The live MVP authority is the COMPLETE strategy of the unmodified July 14 reference run**
(`golden/run-001/reference/` — all 24 cohorts, PM v1.2 enforcement, per-cohort/state eligibility
blocks and custom targets). See `SCOPE-LOCK.md` (corrected) and
`golden/run-001/resolved_execution_map.json`.

Rules:
- Do **not** use `config.mvp.json` for core extraction, golden parity, paper, or live.
- `expected_cohort_slice.from_reference.csv` remains a valid *per-cohort view* of the reference
  run (useful as one of many parity cross-sections), but it is **not** the behavioural target.
- Files are preserved unmodified for audit history only.
