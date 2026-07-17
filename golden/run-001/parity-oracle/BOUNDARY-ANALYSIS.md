# Fast-Oracle Boundary Analysis (M2 Foundation Task 3)

**Question:** can a date-narrowed run (e.g. start 2024-01-01) serve as a fast parity oracle?
**Answer: NO — rejected.** Chosen method: **Option A** (full-history run, unmodified config; compare the full output population). Analysis below.

## Why a cold-start narrowed run is NOT behaviourally equivalent

Audited each carried-state component against the actual engine (`golden-run-001` tag):

| Component | Memory horizon | Cold-start effect at a 2024 boundary |
|---|---|---|
| Cumulative mean range (CMR) | **Infinite — cumulative from series start** (`true_range.cumsum()/n`, `order_blocks._add_parsed_prices`) | Permanently different values. (Golden uses `ob_filter="Atr"`, so CMR is computed-but-unconsumed — still disqualifying in general and fatal if the filter ever changes.) |
| ATR = `_pine_rma(TR, 200)` | Recursive RMA — **exponential tail, unbounded memory**; ~≥1000 15m bars to converge only approximately | OB size gating (`volatility_measure`) differs near the boundary; approximate convergence ≠ row equality. |
| Regime panel (EMA200 daily / BBW / ADX, `src/regime.py`) | EMA200 on **daily** bars → years of tail; ADX/Wilder also recursive | Confirmed prior-day states near/after the boundary can differ → different eligibility/blocks/targets. |
| Swings (`detect_swings`, length 50) | 50 bars either side | Bounded (~50×15m), but pivot legs latch (`current_leg`) from series start. |
| OB lifecycle | **Unbounded** — OBs created years earlier remain active until mitigated/breached | A 2024 start silently deletes every pre-2024 OB that was still live in the Golden run. |
| Delayed-entry pending state | Bounded per-candidate but seeded by OB/tap history | Different OB set ⇒ different pending machines. |
| News/session boundaries | Bounded (minutes) | Safe. |
| Multi-position interaction (`allow_multi_position` + trade numbering) | Path-dependent across the whole run | Row IDs (`S_…`) and interactions shift with any population change. |

Conclusion: options B (warm-up + discard) and C (state-equality proof at a boundary) would require proving equality of *unbounded-memory* recursions and the live-OB set — more work than the saving justifies, with residual risk.

## Chosen design — Option A (degenerate, and better)

The full Golden run costs only ~7 minutes on the operator's Mac (manifest timings) and is tractable in this sandbox in the background. Therefore **the fast oracle IS the full-history run**: identical config (`generated_configs/d6cdae….json`, sha `7b877cab…` — the byte-identical original), identical dataset (sha `314a0efa…`), unmodified pinned engine (tag `golden-run-001`, engine_version `c5e29837…`), fresh output directory, resume off (no `resume_existing_run_folder` in the config ⇒ a new run folder is created; nothing to reuse).

- **Comparison window = the entire output** (strictly stronger than any "later window" subset).
- **Comparison population = all 2,060 rows** of the Triggered-Edge pass, plus `order_blocks.csv` and stable `summary.json` fields, via the parity harness — exact row equality on candidate identity/ordering, cohort, confirmed state, PM decision, state eligibility, resolved RR, outcome, entry/fill/exit timestamps, entry/stop/target prices, gross/net R, news outcomes.
- **Excluded warm-up rows: none.** Boundary: none (full history). Nothing is discarded.

## Executor (amended 2026-07-17): operator machine, not sandbox

The sandbox **cannot execute the oracle run at all**: every tool-call process tree is hard-killed at 45 s (bubblewrap PID-namespace teardown; no cron/systemd/sudo escape — verified empirically), and the run needs ~7 min. Three aborted sandbox attempts are marked with `ABORTED.txt` in `Lux-OB-Backtester/outputs/runs/` (parityoracle001/002, probeslice1) — none is a valid run.

Resolution: the oracle run executes **on the operator's Mac** (`run-parity-oracle.command` / equivalent one-line command; run-id `parityoracle003`), against the pinned working tree (engine_version re-verified `c5e29837…` immediately before handoff). This is strictly better than the sandbox plan: it is the **same environment that produced the July 14 reference**, so the environment caveat below is *eliminated* for the oracle comparison rather than merely managed.

## Environment caveat (superseded for the oracle; still relevant for slice gating)

The reference was produced on the operator's Mac with an unsnapshotted interpreter/library set. The oracle now runs in that same environment, so oracle-vs-reference float divergence cannot be environment-induced. For extraction **slice gating**, runs executed in the sandbox (py 3.10.12 / numpy 2.2.6 / pandas 2.3.3, `environment.observed.txt`) remain subject to the cross-environment caveat — and, per the executor finding above, full runs cannot execute in the sandbox anyway. Slice-gating discipline therefore becomes: **operator-machine baseline vs operator-machine candidate** (same environment both sides), with the harness itself runnable anywhere.
