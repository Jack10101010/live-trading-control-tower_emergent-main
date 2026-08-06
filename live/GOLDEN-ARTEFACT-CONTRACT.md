# GOLDEN-ARTEFACT-CONTRACT.md

**Machine-readable authority: `live/golden_contract.json`.** This file explains;
the JSON governs. `backend/tests/test_golden_contract.py` pins both.

---

## 1. Why this exists

Three different digests were all being called "the Golden hash":

| Digest | What it actually is |
|---|---|
| `ca925d54…` | `golden.entry_trades.file.v1` — the trades **CSV file** written by the driver (`save_scenario_csv` → pandas `to_csv`), produced on the **Mac operator machine**. 2,060 rows, 211 columns. |
| `7febbb34…` | the **same file artefact** produced on the **VPS**. Differs from `ca925d54…` because the serialization family is machine-dependent (see §3). |
| `b43e3248…` | `harness.entry_trades.mem.v1` — an **in-memory digest** (`frame.astype(str).to_csv(index=False)`) used by the capacity/optimisation equivalence harnesses. Never a file; skips the driver's execution-number pass. |

They are **three artefacts, not one**. `ca925d54…` and `7febbb34…` are the same
artefact on different hosts; `b43e3248…` is a different serializer entirely.
Comparing any two digests across rows of this table is a category error, and it
cost a failed VPS shadow gate.

## 2. The two comparisons, and which one gates deployment

### Same-host candidate parity — THE DEPLOYMENT GATE

Candidate vs production, run on the **same host**, same inputs, same
environment: **byte-identical file, plus a complete zero-tolerance field
census**. Implemented by `live/golden_compare.py --mode same-host`.

The VPS already demonstrated this gate passing: candidate and production both
produced `7febbb34…` on the VPS. **That equality is what promotes a candidate.**
An archived cross-machine float discrepancy must never veto a candidate that is
byte-identical to production where it will actually run.

### Cross-machine archive comparison — THE REGRESSION WITNESS

Candidate output vs the Mac-produced archive: full field census with a narrow,
evidence-based allowlist. Implemented by `--mode archive`. It never promotes; it
corroborates, and it alarms if anything outside the allowlist moves.

## 3. The measured cross-machine discrepancy

* Field: `bbw_value`, trade `S_22` — first divergence found by rehearsal's
  comparator (which stops at the first difference; the **full census** is
  exactly what `golden_compare.py` adds).
* Archive `2.540400593491076` vs VPS `2.540400593491075` — parsed doubles are
  **exactly 2 ULP apart** (measured; shortest round-trip repr is bijective, so
  these are the true computed values).
* Producer: the four regime diagnostics (`ema_value`, `px_vs_ema`, `bbw_value`,
  `adx_value`) come from vectorised `rolling().mean/std(ddof=1)` and `ewm`
  reductions — summation order differs across CPU architecture and numpy/BLAS
  builds (Mac arm64 vs VPS x86-64).
* Economic impact: none possible without detection. The four fields are
  **write-only diagnostics** (`strategy_core/execution.py:361-366`, "Pure
  mapping — no indicator math"). Classification consumes the underlying values
  against a **fixed** threshold (`bbwThresholdMode=fixed`, 2.342; S_22 margin
  ≈0.198 vs ULP ≈4.4e-16). Any actual flip would surface in `market_state` /
  `volatility_state` / `outcome` / `net_r` — all zero-tolerance.

Hence the archive policy: allowlist exactly those four fields, `max_ulp: 2`
(measured, not preferred), everything else exact.

**Census caveat, stated honestly:** the S_22 finding is a first-divergence
result. The complete cross-machine census does not exist yet — producing it is
step 5 of the VPS resume prompt, and the archive-mode comparator fails loudly if
that census contains anything outside the narrow policy.

## 3a. Input pinning — discovered by running the comparator on real data

Artefact digests are functions of **(engine, inputs)**, and both input files
have drifted since the archive was produced:

| Input | Archive era | Now | Effect measured on real data |
|---|---|---|---|
| Frozen candles | `314a0efa…` (recorded in ARCHIVE-MANIFEST) | `ba32de0d…` (rebuilt 2026-08-02; historical bars revised by the Dukascopy re-download) | exactly **3 cells**, all `r_if_no_target` (counterfactual analytics; "never alters outcome / pnl_r / fill") — recorded in `live/golden_known_input_drift.json` |
| News calendar | committed `7e37fc88…` | Mac working tree carries an **uncommitted** refresh `85c25c35…` | **698 cells** of news-field differences if the working-tree file is used. Clean checkouts are immune; this is a Mac-workstation hazard only |

Consequences:

1. **`ca925d54…` is unreproducible from a clean checkout on any machine** — the
   archive-era candle bytes are not in the repo (`reference/candles.csv` is
   absent). It remains valid as the recorded digest of the archived run.
2. The archive witness therefore runs with the **known-input-drift ledger**
   (`--known-input-drift live/golden_known_input_drift.json`): the three
   recorded cells are reclassified `explained_input_drift` on an exact
   key+field+ref+cand match. That is provenance, not tolerance — a new value in
   the same cell stays forbidden, and the same-host gate refuses the ledger
   outright.
3. Validated end-to-end on real data (Mac, committed inputs, candidate engine):
   census = 3 explained, 0 permitted, 0 forbidden → PASS. With the uncommitted
   news file: 698 forbidden → FAIL, which is the comparator doing its job.

## 4. Rules that follow

1. Never write "the Golden hash". Name the artefact id from the contract.
2. Byte digests of `*.file.v1` artefacts are comparable **same-host only**.
3. The deployment gate is same-host candidate-vs-production equality.
4. The archive is a witness. If it alarms outside the allowlist, stop and
   investigate; do not regenerate the archive to make a test pass.
5. Any new Golden artefact gets a contract entry before its hash is quoted
   anywhere.
6. Validation runs pin their inputs: dataset SHA-256 and news SHA-256 are
   recorded next to every produced digest. A digest without recorded input
   hashes is not evidence.
