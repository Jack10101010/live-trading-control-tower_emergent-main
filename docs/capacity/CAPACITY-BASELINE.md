# CAPACITY-BASELINE.md — canonical production baseline

**Milestone:** M-CAPACITY-CONSOLIDATE-1 (PARTS 1 & 3)
**Status:** measurement only. Nothing was optimised, and no trading behaviour changed.

> **The historical benchmark chain is UNAVAILABLE.** Prior capacity/Model-B
> research commits no longer exist and were not recovered. No figure in this
> document is carried over from that chain, reconstructed from memory, or
> inferred. Every number below was produced by a run executed during this
> milestone, or is explicitly labelled as an unverified prior claim.

---

## 0. Evidence classification

Used consistently throughout. Nothing is mixed.

| Tag | Meaning |
|---|---|
| **MEASURED** | Produced by a run in this milestone, on this hardware, with artefacts committed under `evidence/`. |
| **MEASURED (indirect)** | Observed from production telemetry that reached the tower, plus a stated source-verified inference chain. |
| **ESTIMATED** | Derived arithmetically from MEASURED figures. The derivation is shown. |
| **PROJECTED** | Extrapolation to hardware or conditions not measured. Always flagged with its assumption. |
| **UNAVAILABLE** | Could not be obtained in this milestone. Stated as such — never filled in with a guess. |

---

## 1. Environment (MEASURED)

| Field | Value |
|---|---|
| Control Tower commit | `d6e776f0ac8fc39e9d9c799c7ba0a162a6a43358` |
| Lux commit | `d978074a2ee938fb4803e50d419788c584d6ef57` |
| `engine_version` | `5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be` |
| Expected `engine_version` | identical — **verified match** |
| Platform | macOS-26.3.1-**arm64** (Apple silicon) |
| Python | 3.12.6 |
| Harness | `python -m live.benchmark` (C1-C, pre-existing) |
| Determinism verification | **PASS** on every iteration of every run |

**Working-tree caveat.** The Lux tree was not pristine at measurement time: 5
tracked files modified (`data/candles/EURUSD_manifest.json`,
`data/news/master_economic_calendar_2020_present.csv`,
`data/news/monthly/ff_2026_05.csv`, `scripts/fetch_forexfactory_batch.py`,
`scripts/fetch_forexfactory_calendar.py`) plus untracked research scripts.
**No file under `src/` or `strategy_core/` was modified**, which
`engine_version()` confirms by matching the pin exactly. The modified news CSV
*is* an engine input, so it participates in the measured news phases; it does not
affect engine identity.

### Dataset (MEASURED)

| Field | Value |
|---|---|
| Frozen candles | `data/candles/EURUSD_1m_extended_2015_2026.csv` |
| Size on disk | 275,823,235 bytes (**263.0 MiB**) |
| SHA-256 | `ba32de0d836fbe4d375c4533180f6074796c73bf5d0df16018040b799777235f` |
| Rows (M1 bars) | **4,314,720** |
| M15 detection bars after resample | **285,834** |
| News calendar | `data/news/master_economic_calendar_2020_present.csv`, 2,021,096 bytes |
| **Symbols processed** | **1 — EURUSD only.** There is no symbol loop anywhere in the pipeline. |
| Order blocks detected (full frame) | **2,080** |
| Order blocks after structure/direction filters | see §3 note |

---

## 2. Cycle duration (MEASURED)

`live.benchmark --iterations 5 --warmup 1`, warm-up excluded from all statistics.
Measured interval is exactly one `golden_pipeline()` call over an already-loaded
frame; candle loading, per-iteration frame copying, hashing and report writing
are all outside the timer.

| Statistic | Value |
|---|---|
| Iterations (measured) | 5 (+1 warm-up discarded) |
| Raw samples (s) | 253.46, 253.93, 254.65, 258.82, 261.99 |
| **Mean** | **256.57 s** |
| Median (p50) | 254.65 s |
| Min | 253.46 s |
| Max | 261.99 s |
| Stddev | 3.70 s |
| Spread (max−min)/min | **3.4 %** |

p95 is not quoted: the harness itself warns it is descriptive only below 20
iterations, and that warning is respected here.

**Recompute duration distribution.** With n=5 the distribution is tight and
unimodal — a 3.4 % spread with no outliers. This is a compute-bound, cache-warm
workload with no observed variance from IO or GC. A single earlier probe run
(1 iteration, separate process) gave 257.93 s, consistent with this population.

## 3. Phase breakdown — ranked (MEASURED)

Mean of 5 iterations. Percentages are of the 256.57 s mean total.

| Rank | Phase | Mean (s) | % of total | Min (s) | Max (s) |
|---:|---|---:|---:|---:|---:|
| 1 | **`execute_scenario_job`** | **187.710** | **73.16 %** | 185.148 | 190.574 |
| 2 | `detect_order_blocks` | 26.784 | 10.44 % | 26.353 | 27.523 |
| 3 | `tag_obs_news` | 25.485 | 9.93 % | 25.307 | 25.729 |
| 4 | `prepare_news_cache` | 10.237 | 3.99 % | 9.933 | 11.190 |
| 5 | `prepare_candles` | 3.257 | 1.27 % | 3.119 | 3.482 |
| 6 | `filter_date_range` | 2.550 | 0.99 % | 2.376 | 2.893 |
| 7 | `resample` | 0.370 | 0.14 % | 0.222 | 0.677 |
| 8 | `load_news_calendar` | 0.057 | 0.02 % | 0.056 | 0.059 |
| 9 | `load_news_events` | 0.054 | 0.02 % | 0.053 | 0.056 |
| 10 | `filter_obs` | 0.002 | 0.00 % | 0.002 | 0.004 |
| 11 | `prepare_obs` | 0.001 | 0.00 % | 0.001 | 0.002 |
| | **TOTAL** | **256.570** | **100 %** | | |

**Longest operations:** the top three phases account for **93.5 %** of every
cycle. Everything from rank 4 down totals 6.5 %, and ranks 7–11 together are
under 0.2 % — optimising them cannot matter.

### 3a. Exact call counts (MEASURED)

From the engine's **own** instrumentation — `profile_simulation_hot_path`
(`src/config.py:54`, default `False`), counters defined at
`strategy_core/execution.py:1760`. These are exact counts, not sampled, and
carry negligible overhead.

| Counter | Count | Per M1 candle |
|---|---:|---:|
| `candle_iterations` | 4,271,746 | 1.00 |
| **`pending_checks`** | **133,069,753** | **31.15** |
| **`fill_checks`** | **133,069,753** | **31.15** |
| **`invalidation_checks`** | **132,654,597** | **31.05** |
| `news_blackout_lookups` | 4,267,429 | 1.00 |
| `active_checks` | 65,099 | 0.015 |
| `news_flatten_lookups` | 65,000 | 0.015 |
| `base_row_creations` | 2,060 | 0.0005 |

**This is the single most important measurement in this document.**

The per-candle loop runs 4.27 M times, but inside it the **pending-order list is
rescanned in full on every candle** — 133 M pending checks, each performing a
fill check and (almost always) an invalidation check. That is ~31 pending orders
examined per candle, and **≈266 M inner operations** in total against only
**4.27 M** outer iterations.

For scale: `base_row_creations` is 2,060 — the engine does ~65,000 checks of
work for every trade row it actually creates, and ~129,000 inner operations per
created row.

The cost is therefore **not** "walking 4.3 M candles". It is *rescanning an
unindexed pending list at every candle*. That distinction changes the entire
optimisation strategy and is why PART 8 ranks pending-list indexing above
everything else.

### 3b. Call-graph hotspots (MEASURED)

`cProfile` over a single `execute_scenario_job` call. **Absolute times are
inflated — cProfile cost 2.68× wall clock (583.5 s vs 218.0 s uninstrumented).
Call counts are exact; times should be read as relative weights only.**

| Function | ncalls | tottime (s) | cumtime (s) |
|---|---:|---:|---:|
| `execution.py:2121 simulate_trades` | 1 | 207.78 | 581.47 |
| **`dict.get`** | **1,907,495,402** | **129.59** | 129.59 |
| `execution.py:1255 _update_pending_metrics` | 133,068,546 | 55.01 | 87.87 |
| `execution.py:998 _triggered_edge_touched` | 129,374,023 | 47.79 | 111.18 |
| **`builtins.max`** | **395,721,513** | **34.24** | 34.24 |
| `execution.py:992 _penetration_price` | 129,505,521 | 27.27 | 37.99 |
| `execution.py:971 _ob_depth` | 129,379,463 | 18.18 | 18.18 |
| `execution.py:1661 _is_filled` | 129,371,998 | 10.85 | 10.85 |
| `list.append` | 133,152,415 | 9.58 | 9.58 |
| `dict.setdefault` | 133,133,645 | 9.36 | 9.36 |
| `news.py:13 _utc_naive_timestamp` | 4,332,483 | 8.50 | 8.50 |

**`dict.get` is called 1.9 billion times in one cycle** — roughly **14 dictionary
lookups per pending check**. Candle rows and order-block rows are plain dicts
(`candles.to_dict("records")`, `obs.to_dict("records")`), so every one of the
five hot predicates re-reads its inputs by string key, 133 M times over.
`builtins.max` at 396 M calls (≈3 per pending check) is the same story.

This reframes the optimisation target precisely:

* the **outer** walk is 4.27 M iterations — not the problem;
* the **inner** rescan is 133 M checks — the structural problem;
* the **per-check overhead** is ~14 dict lookups + ~3 `max()` calls — the
  constant-factor problem.

Both are addressable without changing *what* is computed — only how it is looked
up and how often. That matters because this engine's contract is byte-identical
reproducibility.

**Instrumentation caveat.** The counter-enabled run took 218.0 s against the
187.7 s baseline: `_profile_increment` fires 133 M+ times, costing ~16 %. The
counts are exact; that run's timing is not the baseline and is not used as one.

Supporting artefacts: `evidence/hotspots_tottime.txt`,
`evidence/hotspots_cumtime.txt`, `evidence/execute_scenario_job.pstats`.

**Run shape confirmed:** 4,271,746 M1 bars simulated, 2,080 order blocks after
filters, **2,060 trade rows produced**.

## 4. CPU (MEASURED)

Sampled at 5 s intervals for the full 5-iteration run (220 samples).

| Metric | Value |
|---|---|
| Mean CPU | **98.9 %** |
| Max CPU | 100.0 % |
| Effective parallelism | **1.0 core** |

The engine is **single-threaded and CPU-bound**. It never exceeds one core.
There is no parallelism in the pipeline: one process, one compute thread, plus a
daemon liveness thread that performs no compute. On a multi-core host every core
but one is idle for the entire cycle.

## 5. Memory (MEASURED)

Same 220 samples.

| Metric | Value |
|---|---|
| **Peak RSS** | **3,085 MiB** |
| Median RSS | 788 MiB |
| Min RSS (between iterations) | 40 MiB |
| Peak VSZ | 405,691 MiB |

Peak RSS is **~11.7× the 263 MiB on-disk dataset** — the cost of materialising
4.31 M rows as a pandas frame *and* as a list of per-candle dicts
(`candles.to_dict("records")`, `strategy_core/execution.py:2313`).

RSS returns to ~40 MiB between iterations, so this is **per-cycle working set,
not a leak**.

**On "private bytes":** that is a Windows counter. This measurement host is
macOS, where the closest equivalent is RSS, reported above. Windows private
bytes for the VPS is **UNAVAILABLE** — see §7. macOS VSZ is a virtual
reservation and is not comparable to Windows private bytes; it is recorded only
for completeness and should not be quoted as a memory figure.

## 6. Throughput and IO (MEASURED / ESTIMATED)

| Metric | Value | Class |
|---|---|---|
| Whole-pipeline throughput | **16,817 M1 bars/s** | ESTIMATED — 4,314,720 rows ÷ 256.57 s |
| Simulation-walk throughput | **22,986 M1 bars/s** | ESTIMATED — 4,314,720 rows ÷ 187.71 s |
| Inner-operation rate | ≈1.42 M pending-checks/s | ESTIMATED — 266 M ÷ 187.71 s |

**IO profile.** Deliberately small and almost entirely outside the measured
interval:

* **Reads per slow-path cycle:** the frozen CSV (263 MiB) + the live segment,
  each read exactly once by `_capture_bytes` (`live/runner.py:211`), with no
  path re-opened after hashing (no TOCTOU window). The news CSV (1.9 MiB) is
  read once. Total ≈265 MiB per recompute.
* **Reads per fast-idle cycle:** the same two candle files are still read in
  full to compute `input_revision` — but no DataFrame is constructed. So even an
  *idle* cycle pays ~263 MiB of file IO every 10–60 s.
* **Writes per slow-path cycle:** `frames/prev_trades.csv`, plus one
  `runner_state.json` (temp → write → flush → fsync → atomic rename), plus the
  publisher's durable fallback JSON. All small.
* The benchmark's measured interval **excludes** candle loading, so the 256.57 s
  figure is pure compute. Real production cycles pay the read on top.

**Idle-cycle read amplification is worth noting for PART 8:** at a 10 s cadence,
re-hashing 263 MiB to discover "nothing changed" is ~26 MiB/s of sustained
pointless IO. It is correct — the revision must describe the exact bytes — but
it is not free, and on a slow VPS disk it may be a material share of idle cost.
This was not separately measured here; it is flagged, not quantified.

## 7. Production (VPS) figures

### What is UNAVAILABLE

`live/ops_log.py` writes `<state_dir>/ops/cycles.jsonl` on the VPS with the real
per-cycle `duration_s`, `stage_timings` and `phase_timings`, and
`<state_dir>/ops/liveness.json`. **This is the authoritative production capacity
record and it was not readable during this milestone** — it lives on the VPS
filesystem and this milestone authorises no VPS access. It is not reconstructed
or estimated here.

Also UNAVAILABLE: VPS CPU model and core count, Windows private bytes, disk
throughput, and whether the host is memory-constrained enough to page.

### MEASURED (indirect) — publication cadence

The one production signal that *is* available is when telemetry reached the
tower. The inference chain is source-verified:

1. `publisher.publish()` is called **unconditionally, once per cycle**
   (`live/main.py:167`), with no change-detection and no retry loop.
2. `live/liveness.py` writes only to the local filesystem and **never
   publishes**.
3. Therefore **a publication can occur only at the end of a cycle**, and the
   interval between publications is a lower bound on cycle duration.

Observed on the tower:

| Source | Observation |
|---|---|
| Backend access log window | 2026-08-03 09:54 → 2026-08-04 11:13 (**25.3 h**, spans a backend restart; the log appends) |
| `POST /api/live/ingest` | **20**, all HTTP 200, all from `100.120.124.97` |
| Mean inter-arrival | **≈76 min** |
| Directly observed consecutive gaps | 17:55:58 → 18:29:22 = **33 m 24 s**; 09:06:40 → 09:34:44 = **28 m 04 s**; 09:34:44 → 09:36:01 = **1 m 17 s** |
| Cycle statuses seen (4 samples) | `recomputing`, `ok`, `ok`, `recomputing` — **`no_new_bar` never observed** |

**What this does and does not establish.** It establishes that the VPS completed
only ~20 cycles in 25.3 hours. If the runner was up continuously, the scheduler's
own ceiling (`CADENCE_MAX_IDLE_SLEEP_S = 60 s`) means idle cycles should publish
at least once a minute — which would be ~1,500 publications, not 20. Never
observing `no_new_bar` across 4 samples points the same way.

**Two hypotheses remain, and this milestone cannot distinguish them:**

* **(a)** the runner is up continuously and each cycle genuinely takes ~30–76 min,
  i.e. recompute overruns the 900 s bar interval by 2–5×; or
* **(b)** the runner is not running continuously.

Both are consistent with everything observable from the tower. **This is not
resolved here and is not guessed at.** The read-only VPS diagnostic in §9 is
what settles it, and it is a prerequisite for trusting any VPS-side projection.

### PROJECTED — and why no VPS number is quoted

A Mac-to-VPS scaling factor would require knowing the VPS CPU. It is not known,
so **no projected VPS cycle time is stated**. What can be said without
extrapolation:

* MEASURED on Apple silicon: **256.6 s** against a **900 s** bar interval →
  **28.5 % budget utilisation**, a 3.5× margin.
* `live/shadow_report.py:19` asserts `no_boundary_overrun` — *"no recompute >
  900s bar interval"* — so the 900 s ceiling is an explicit, pre-existing design
  constraint, not one invented here.
* `live/liveness.py`'s docstring refers to "the ~900s Golden pipeline call".
  That is an **unverified prior claim** carried in a comment, not a measurement
  from the lost research chain, and it is ~3.5× the figure measured here. It is
  recorded because it is in the tree, not because it is corroborated.

## 8. What the baseline says

1. **One phase dominates.** `execute_scenario_job` is 73.2 % of the cycle. The
   top three phases are 93.5 %.
2. **The real cost is an unindexed rescan, not the candle walk.** 266 M inner
   operations against 4.27 M outer iterations — a ~31× amplification factor.
3. **The engine uses one core.** Mean CPU 98.9 %, never above 100 %. Whatever
   the VPS core count is, all but one core is idle during recompute.
4. **Peak RSS is 3.0 GiB** for a 263 MiB dataset. On a memory-constrained VPS
   this alone could force paging and would plausibly explain a large slowdown —
   testable, and currently untested.
5. **The margin on this hardware is 3.5×; the margin on the VPS is unknown and
   the evidence suggests it may be negative.**
6. **Every cycle recomputes eleven years from inception**, because both
   volatility measures are seeded from frame start
   (`ORDER-BLOCK-IDENTITY.md` §3). Nothing is cached between cycles.

## 9. Read-only VPS diagnostic (prerequisite for any VPS projection)

To be run **on the VPS** by the operator. Read-only: no restart, no config
change, no trading impact. Replace `<STATE_DIR>` with the node's configured
state directory.

```powershell
# 1. Is the runner actually running, and since when?
Get-Process python -ErrorAction SilentlyContinue |
  Select-Object Id, StartTime, CPU, WorkingSet64, PrivateMemorySize64

# 2. Real per-cycle durations — the authoritative record (last 50 cycles)
Get-Content "<STATE_DIR>\ops\cycles.jsonl" -Tail 50 |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Select-Object cycle_start, duration_s, status, boundary, bars_appended, error |
  Format-Table -AutoSize

# 3. Duration distribution over the whole log
Get-Content "<STATE_DIR>\ops\cycles.jsonl" |
  ForEach-Object { ($_ | ConvertFrom-Json).duration_s } |
  Measure-Object -Average -Maximum -Minimum

# 4. Status histogram — how many cycles were fast-idle vs full recompute?
Get-Content "<STATE_DIR>\ops\cycles.jsonl" |
  ForEach-Object { ($_ | ConvertFrom-Json).status } |
  Group-Object | Sort-Object Count -Descending

# 5. Per-phase timings from the most recent recompute
Get-Content "<STATE_DIR>\ops\cycles.jsonl" |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.phase_timings } |
  Select-Object -Last 3 -ExpandProperty phase_timings

# 6. Is the liveness beacon advancing (i.e. is it stuck mid-cycle)?
Get-Content "<STATE_DIR>\ops\liveness.json"

# 7. Host capability — cores, RAM, and whether it is paging
Get-CimInstance Win32_Processor |
  Select-Object Name, NumberOfCores, NumberOfLogicalProcessors, MaxClockSpeed
Get-CimInstance Win32_OperatingSystem |
  Select-Object TotalVisibleMemorySize, FreePhysicalMemory
Get-Counter '\Memory\Pages/sec' -MaxSamples 5
```

Questions it answers, in order of importance:

1. **Is the runner continuously up?** (§7 hypothesis (a) vs (b)) — items 1, 4, 6.
2. **What is the real recompute duration and does it exceed 900 s?** — items 2, 3.
3. **Does the VPS phase profile match the Mac's 73/10/10 split?** — item 5.
4. **Is the host paging under the 3.0 GiB peak?** — item 7.

Until items 1–2 are answered, **no VPS capacity claim in any later milestone
should be treated as measured.**

---

## 10. Evidence artefacts

Committed under `evidence/`:

| File | What it is |
|---|---|
| `benchmark-baseline.json` / `.md` / `.csv` | Full 5-iteration harness output, per-iteration phase timings |
| `benchmark-probe.json` / `.md` | Independent single-iteration run (separate process) |
| `baseline-resource.csv` | 220 CPU/RSS/VSZ samples at 5 s intervals |
| `cap3-hotpath.json` | Exact hot-path call counts + cProfile top-30 by `tottime` |
| `hotspots_tottime.txt` / `hotspots_cumtime.txt` | cProfile call-graph hotspots |
| `execute_scenario_job.pstats` | Raw cProfile data, re-analysable with `pstats` |
| `cap5-identity.json` | Order-block identity experiment results |
| `cap5_identity_probe.py`, `cap3_hotpath_probe.py` | The probes themselves, for reproduction |
