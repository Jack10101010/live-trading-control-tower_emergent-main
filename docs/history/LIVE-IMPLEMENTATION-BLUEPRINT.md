# Live Implementation Blueprint

**Workspace:** `live-trading-control-tower-live-prep` (worktree) · branch `live-implementation-prep` · baseline `81e468c…640f9`
**Type:** Read-only architectural map + roadmap. No code modified, no commits, no branches, no VPS contact, no MT5 calls.
**Date:** 2026-07-18
**Status of prior audits:** superseded for planning purposes. This document describes the runtime that **actually exists** in this repository and is the implementation reference going forward.

> **Correction to earlier audits (1 & 2):** those were written before this worktree existed and concluded "CT execution is greenfield." **That is now false.** A complete, coherent dry-run shadow runtime exists under `live/` (15 modules, 1,825 LOC), plus supporting backend routes. Earlier proposals to *build* an order state machine, gateway ladder, reconciler, idempotent intents, safety rails, cycle JSONL, and a parity oracle are largely **already implemented**. The real task is **instrument, harden, and optimize** an existing runtime — not build one.

---

## 1. Runtime map — `python -m live.main`

Entry: `live/main.py:113` → `main()` `live/main.py:82`.

### 1.1 One-glance flow

```
main()  live/main.py:82
 ├─ refuse LIVE_MODE=live                         main.py:84-87   (P1 shadow guard)
 ├─ build()                                       main.py:33-44
 ├─ gateway.connect()                             main.py:89 → mt5_gateway.py:31
 └─ while True:  cycle();  sleep(10)              main.py:95-108
      cycle()                                     main.py:47-79
       ├─ ops.cycle_start()                       ops_log.py:27
       ├─ bridge.poll_once()      ── market data  mt5_bridge.py (→ gateway.closed_m1_bars mt5_gateway.py:62)
       ├─ runner.run_once()       ── Lux + intents runner.py:138  (→ golden_pipeline:97, diff_frontier intents.py:68)
       ├─ executor.apply(intents) ── reconcile+exec executor.py:73 (→ reconcile:40, _execute:103)
       ├─ publisher.build_payload/publish ─ publish publisher.py:26/50 (→ POST /api/live/ingest)
       └─ ops.cycle_end()         ── persist+heartbeat ops_log.py:30
 finally: gateway.disconnect()                    main.py:109-110 → mt5_gateway.py:44
```

### 1.2 Stage table

| Stage | Module · function · lines | Responsibility | Inputs | Outputs |
|---|---|---|---|---|
| Bootstrap | `live/main.py:33` `build()` | Construct all singletons; verify engine identity | env/config | `(config, gateway, bridge, runner, executor, publisher, ops)` |
| Config load | `live/config.py:22` `LiveConfig` (+ constants `:10-15`, `__post_init__:46`) | Frozen operator decisions as **constants** (SYMBOL, engine hash, golden config relpath); env-tunable ops knobs; absolute path resolution | env vars | `LiveConfig` |
| Scheduler / cycle owner | `live/main.py:95-108` while-loop + `live/runner.py:138-149` boundary gate | Drive one cycle per ~10 s poll; **skip when boundary unchanged** (`no_new_bar`); 10-consecutive-error exit for supervisor restart | — | per-cycle `record` |
| Heartbeat | `live/ops_log.py:47-50` `cycle_end` **and** `live/mt5_bridge.py` `_heartbeat` | Write `heartbeat.json` (post-cycle in ops; post-poll in bridge) + append `cycles.jsonl` | cycle results | `ops/heartbeat.json`, `ops/cycles.jsonl`, bridge `heartbeat.json` |
| Market data | `live/mt5_bridge.py` `poll_once` → `live/mt5_gateway.py:62` `closed_m1_bars` | Pull **closed** M1 bars (excludes forming minute), append dedup chronological rows to `EURUSD_1m_live.csv`; write `provenance.json` once | broker feed, `since` | appended live-segment rows |
| Lux invocation | `live/runner.py:138` `run_once` → `:97` `golden_pipeline` → `LuxSession:28` | Recompute-on-close through the **same** Lux driver helpers (`scripts.run_backtest`) + frozen `strategy_core`; only delta = `end_date` extended to frontier | frozen CSV + live segment | trades frame |
| Intent generation | `live/intents.py:68` `diff_frontier` | Frontier diff of consecutive trade frames → ordered, **idempotent** `OrderIntent`s (sha1 identity); OPEN/CLOSE/MODIFY_STOP/SKIP_INTRA_WINDOW | prev + cur frames, frontier bar | `list[OrderIntent]` |
| Gateway | `live/mt5_gateway.py:18` (`open_position:119`, `modify_position_sl:141`, `close_position:151`, `snapshot:94`) | **Sole** MetaTrader5 touchpoint; structured `(ok, data|error)`, never raises SDK exceptions upward | intent params | broker results |
| Reconciliation | `live/executor.py:40` `reconcile` → `backend/broker_sync.py:230` `reconcile` | Pull broker snapshot, diff vs mirror; **FREEZE on unknown magic-tagged position** (alert, never auto-close); missing→un-mirror; foreign→report | broker snapshot, state mirror | `ReconcileReport` |
| Persistence | `live/state.py:43` `save` (atomic tmp+rename); ledger/mirror/daily; `live/ops_log.py:45` `cycles.jsonl` | Crash-safe state: last boundary, prev-frame hash+file, intent ledger, trade→ticket mirror, daily realized-R | cycle mutations | `runner_state.json`, `frames/prev_trades.csv`, `cycles.jsonl` |
| Publishing | `live/publisher.py:50` `publish` → `backend/server.py:1727` `POST /api/live/ingest` | Best-effort CT push; **always** writes `publish_last.json` first; network failure never affects trading | payload | delivery dict, fallback file |
| Shutdown | `live/main.py:109-110` finally + `:101-102` error-exit | Disconnect gateway; exit non-zero on 10 consecutive errors so supervisor restarts visibly | — | clean disconnect |

### 1.3 Cross-cutting components (not per-stage but load-bearing)

- **Safety rails** `live/safety.py:32` `evaluate` — every intent passes before any broker action: kill-switch file, symbol whitelist, duplicate-intent (ledger), daily-loss limit, max-open-positions, unknown-position guard. Frozen at process start.
- **Deploy verifier** `live/deploy_check.py` — `preflight | runtime | restart` phases; pins Lux commit `d978074…` (`:38`), engine hash (`config.py:15`), asserts **zero** sent/confirmed ledger entries in dry-run (`:114`), restart-resume drill (`:131`).
- **Rehearsal / parity oracle** `live/rehearsal.py` — P0 byte-for-byte gate proving the live recompute path reproduces the frozen Golden system (RUN A/B/B2/BASE, entry+baseline+order-block parity, restart/dup/dry-run-never-submits). **This is the parity oracle earlier audits proposed to build — it exists.**
- **Shadow report** `live/shadow_report.py` — aggregates `cycles.jsonl` into a promotion report.

---

## 2. Architectural seams — UNCHANGED / EXTEND / REPLACE

Reuse maximally; the runtime is sound. Nothing warrants REPLACE.

| Stage / component | Verdict | Why |
|---|---|---|
| Config (`config.py`) | **UNCHANGED** | Constants-vs-knobs split is deliberate and correct; new ops knobs are additive env fields. |
| Scheduler / main loop (`main.py`) | **EXTEND** | Sound single loop. Extend only to (a) start a heartbeat thread and (b) thread a profiler through `cycle()`. No structural change. |
| Market data (`mt5_bridge`, `gateway.closed_m1_bars`) | **UNCHANGED** | Closed-bar discipline and append-dedup are correct. |
| Lux invocation (`runner.golden_pipeline`, `LuxSession`) | **EXTEND** | Correct and parity-guarded. Extend only with a load-once cache for the 273 MB frozen CSV (`assemble_candles` reads it **every cycle** — `runner.py:142-143,68`) and a driver-side sub-phase profiler. **Lux itself stays byte-for-byte UNCHANGED.** |
| Intent generation (`intents.diff_frontier`) | **UNCHANGED** | Deterministic idempotent identities; mirror model is clean. |
| Gateway (`mt5_gateway`) | **UNCHANGED** | Sole MT5 touchpoint; structured returns; write path already gated by `mode=="live"` upstream. |
| Reconciliation (`executor.reconcile` + `broker_sync`) | **EXTEND** | Freeze-on-unknown is correct. Extend reconcile-report fields into observability; add startup reconciliation gate before arming (P2). |
| Persistence (`state`, `ops_log`) | **EXTEND** | Atomic writes correct. Extend `cycle_end` record with phase timings (§3); no format break (append-only tolerant reader already at `ops_log.py:60-63`). |
| Publishing (`publisher` + `/live/ingest`) | **UNCHANGED** | Best-effort with fallback is the right pattern; also the template for a metrics sink. |
| Heartbeat (`ops_log` + `mt5_bridge`) | **EXTEND (+ consolidate)** | Post-cycle-only writes cannot prove liveness during the ~963 s compute (§4 invariant flag). Extend to a dedicated writer thread; consolidate the two writers. |
| Safety rails (`safety`) | **EXTEND** | Solid rail set. P2 adds: account/server allowlist, spread/slippage ceiling, stale-signal rejection, arming token (Audit #1 §I) — all additive checks in `evaluate`. |
| Deploy verifier / rehearsal / shadow report | **UNCHANGED** | Reuse as-is; rehearsal is the parity oracle. |

---

## 3. Safe insertion points (identification only — no implementation)

| Need | Exact seam | Note |
|---|---|---|
| Runtime instrumentation | `live/main.py:47-79` `cycle()` — wrap each stage call | Stage boundaries already isolated; time around `bridge.poll_once` / `runner.run_once` / `executor.apply` / `publish` |
| Phase profiler (sub-stage) | `live/runner.py:97-135` `golden_pipeline` — between the existing driver calls (filter/prepare/detect/execute) | Driver-side only; **never** inside `strategy_core`. Pass a profiler object; readings are diagnostic (see Recovery Audit §7). |
| Cycle JSONL | **Already exists** — `live/ops_log.py:45-46`. Extend `cycle_end` record (`:30-51`) with a `phases` dict | Append-only; reader already tolerates schema drift (`:60-63`) |
| Heartbeat improvements | `live/main.py:88-95` — start a daemon writer thread before the loop; it reads shared counters + `main_loop_last_progress_at` bumped at each `cycle()` phase boundary | Consolidate with `mt5_bridge._heartbeat`; multi-signal wedge detection per Recovery Audit §7 (never a wedge from stale progress alone) |
| Benchmark timers | `live/runner.py:97` `golden_pipeline` via the `_pipeline`/`_candles_provider` injection points (`runner.py:93-94`) already built for tests/rehearsal | Reuse `rehearsal.py` harness to benchmark off-VPS |
| Future metrics sink | Beside `live/publisher.py:50` `publish`, fed from the `cycle_end` record | Mirror the best-effort POST + fallback pattern; never blocks trading |

---

## 4. Invariant verification

| Invariant | Status | Evidence / flag |
|---|---|---|
| Single runtime owner | ✅ | `main.main` `main.py:82` is the only entrypoint loop |
| Single scheduler | ✅ | One `while True` (`main.py:95`); `runner.run_once` boundary-dedups (`runner.py:148`) |
| Single heartbeat writer | ⚠️ **VIOLATION** | Two writers of a `heartbeat.json`: `ops_log.cycle_end` (`ops_log.py:47`, `state_dir/ops/`) and `mt5_bridge._heartbeat` (`mt5_bridge.py`, `market_data_dir/`). Different paths, but two sources of "liveness" truth. **Consolidate to one writer** before building wedge detection. |
| Single persistence owner | ✅ (partitioned) | Non-overlapping domains: `RunnerState`→`runner_state.json`/frames; `OpsLog`→`cycles.jsonl`; `MT5BarBridge`→live segment. Only overlap is the dual heartbeat above. |
| Single publisher | ✅ | `CTPublisher` (`publisher.py`) is the only CT push |
| No duplicate loops | ✅ | `deploy_check`/`rehearsal` are separate one-shot invocations, not loops |
| No duplicated strategy logic | ✅ | `runner.golden_pipeline` calls Lux `rb.*`/`core.*` only; docstring + code confirm zero re-implementation (`runner.py:1-10,97-135`); `intents.py` reads engine outcome spellings, computes no strategy |
| Lux = only strategy authority | ✅ | `LuxSession.verify_engine` (`runner.py:51`) refuses on engine-hash mismatch; pins commit in `deploy_check.py:38` |

**One violation to resolve (dual heartbeat writer); everything else holds.**

---

## 5. Implementation roadmap

Grouped into small reviewable commits. **No commit can reach a broker write API** — the write path (`mt5_gateway` `order_send`) stays gated behind `mode=="live"` (`executor.py:104`), and P1 refuses `LIVE_MODE=live` at startup (`main.py:84`). Phases 1–4 are pure observability/perf and cannot alter trading behavior. Live promotion (P2) is out of scope for this roadmap.

### Phase 0 — Baseline verification
- **Purpose:** prove the worktree runs green before touching anything.
- **Files touched:** none (run `live.rehearsal`, `live.deploy_check preflight`, `pytest tests/`).
- **Complexity:** Low · **Risk:** None (read-only run) · **Verification:** rehearsal exit 0, engine hash matches `config.py:15`, tests green · **Rollback:** n/a.

### Phase 1 — Heartbeat consolidation + liveness thread
- **Purpose:** resolve the §4 dual-writer violation; prove liveness during the long compute (multi-signal, per Recovery Audit §7).
- **Files touched:** `live/ops_log.py`, `live/mt5_bridge.py` (remove/redirect its heartbeat), `live/main.py` (start writer thread).
- **Complexity:** Medium · **Risk:** Low (observability only; no trade path) · **Verification:** `heartbeat.json` freshness advances mid-cycle in a forced long-cycle test; `deploy_check runtime` heartbeat checks still pass (`deploy_check.py:104-108`) · **Rollback:** revert to post-cycle `cycle_end` write.

### Phase 2 — Phase profiler + extended cycle JSONL
- **Purpose:** measure per-phase time (settle the ~963 s attribution) without assuming strategy dominates.
- **Files touched:** `live/main.py` (`cycle` timing), `live/runner.py` (`golden_pipeline` sub-phase timers via profiler arg), `live/ops_log.py` (`phases` field).
- **Complexity:** Medium · **Risk:** Low · **Verification:** `cycles.jsonl` rows carry per-phase durations; old-format reader still parses (`ops_log.py:60-63`); **Lux `strategy_core` byte-unchanged** (no edits under Lux) · **Rollback:** drop the `phases` field (reader tolerant).

### Phase 3 — Benchmark harness
- **Purpose:** reproducible off-VPS cycle timing over frozen data.
- **Files touched:** new `live/benchmark.py` (reuses `runner.py:93-94` injection seams + `rehearsal` fixtures); no existing file edited.
- **Complexity:** Low · **Risk:** None (off-VPS, no broker) · **Verification:** stable p50/p95 across N runs; numbers reconcile with Phase 2 JSONL · **Rollback:** delete file.

### Phase 4 — Tier-1 frozen-data optimization (load-once)
- **Purpose:** eliminate the per-cycle 273 MB re-read in `assemble_candles` (`runner.py:68,142-143`).
- **Files touched:** `live/runner.py` (cache frozen frame in the process; re-validate by sha; live segment still merged each cycle).
- **Complexity:** Medium · **Risk:** Medium — must not change engine inputs.
- **Verification (mandatory):** `live.rehearsal` byte-for-byte parity **must** stay exit 0; cached vs fresh candle frame byte-identical; engine hash unchanged; `engine_version` unaffected (CT-side data caching only).
- **Rollback:** remove cache, revert to per-cycle read (single-function revert).

### Phase 5 — Observability polish (metrics sink, shadow-report fields)
- **Purpose:** optional metrics emitter + richer promotion report.
- **Files touched:** new emitter beside `publisher.py`; `live/shadow_report.py` fields.
- **Complexity:** Low · **Risk:** None (best-effort, never blocks) · **Verification:** trading path unaffected on sink failure · **Rollback:** disable emitter.

**Execution-hardening work (P2 — after this roadmap, gated):** account/server allowlist, spread/slippage ceiling, stale-signal rejection, arming token, startup-reconciliation-before-arming — all additive checks in `safety.py:evaluate` and `executor.reconcile`. Live gateway promotion remains the final, separately-approved step.

---

## 6. Go / No-Go

**GO for the observability + performance roadmap (Phases 0–5). NO-GO for live promotion (unchanged; P1 shadow guard stands).**

Rationale: the runtime exists, is coherent, and holds every architectural invariant except one contained, low-risk issue (dual heartbeat writer, §4). The write path is correctly quarantined and disabled. Phases 0–5 touch only observability, benchmarking, and CT-side data caching — none can alter Lux determinism (guarded by the existing `rehearsal` parity oracle) or reach a broker.

**Remaining blockers / open items:**
- **B-1 (must fix in Phase 1):** dual heartbeat writer — resolve before building wedge detection.
- **B-2 (verify in Phase 0):** confirm this worktree's `HEAD` is exactly `81e468c…` and Lux is pinned at `d978074…` (git not runnable from this sandbox — the worktree's gitdir points to a Mac-side path; verify on the Mac with `git -C <worktree> rev-parse HEAD`).
- **B-3 (data dependency):** `runner.run_once` expects the frozen CSV at `lux_root/data/candles/EURUSD_1m_extended_2015_2026.csv` (`runner.py:142`) and `deploy_check` pins its sha — confirm present before benchmarking.
- **B-4 (P2, not now):** live-promotion hardening (allowlists, arming token, spread ceiling, startup-reconcile gate) is required before any `LIVE_MODE=live` — explicitly out of scope here.

No code was modified, no commits or branches created, no VPS touched, no MT5 API called. This blueprint is the implementation reference; begin at Phase 0.
