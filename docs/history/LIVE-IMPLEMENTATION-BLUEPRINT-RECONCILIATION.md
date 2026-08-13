# Live Implementation Blueprint — Reconciliation & Correction Report

**Workspace:** `live-trading-control-tower-live-prep` · branch `live-implementation-prep` · baseline `81e468c…640f9`
**Type:** Read-only reconciliation. No code modified, no commits, no VPS, no MT5. The existing `LIVE-IMPLEMENTATION-BLUEPRINT.md` is unchanged; where this report and the blueprint conflict, **this report governs**.
**Date:** 2026-07-18
**Premise:** discovering that a component *exists* does not clear a previously-identified safety defect. The blueprint is a valid factual inventory; it is **not yet canonical**.

---

## Audit LR-1 — strategy-state vs execution-state durability

### Exact persistence ordering (traced)

| Step | File · line | State changed | Becomes durable | vs intent gen | vs reconciliation |
|---|---|---|---|---|---|
| no-new-bar gate | `runner.py:148` | reads `last_boundary` | — | before | before |
| strategy recompute | `runner.py:153` | none (pure) | — | before | before |
| intent generation | `runner.py:159` `diff_frontier` | in-memory `intents` list | **never persisted as intents** | — | before |
| store frame (in-mem) | `runner.py:162` → `state.py:50-55` | `prev_frame_file`, `prev_frame_hash`, `last_boundary`, writes `frames/prev_trades.csv` | at next save | after | before |
| **runner commit** | `runner.py:163` → `state.py:43-47` | **`last_boundary` + frame durable (atomic os.replace)** | **HERE** | after | **before** |
| executor invoked | `main.py:57-58` | — | — | after | — |
| reconcile | `executor.py:75,40` | may `mirror_set(None)` in-mem | at executor save | after | during |
| ledger reserve (SENT) | `executor.py:112` | in-mem ledger `SENT` | at `executor.py:99` | after | after |
| broker submit | `mt5_gateway.py:136` (live only) | broker order | server-side immediately | after | after |
| mirror set | `executor.py:118` | in-mem mirror ticket | at `executor.py:99` | after | after |
| ledger confirm | `executor.py:130` | in-mem `CONFIRMED/FAILED` | at `executor.py:99` | after | after |
| **executor commit** | `executor.py:99` → `state.py:43` | **ledger + mirror durable** | **HERE** | after | after |
| cycle log | `main.py:68` → `ops_log.py:45-50` | `cycles.jsonl` + heartbeat | at write | after | after |

### The defect

The **boundary and prev-frame advance is committed durably in the runner transaction (`runner.py:163`) before `executor.apply` even runs (`main.py:57`), and long before the intent ledger is durably reserved (`executor.py:99`).** The no-new-bar gate (`runner.py:148`) keys **solely** on `last_boundary`. Therefore the exact crash sequence in the brief is real:

> recompute succeeds → runner persists `last_boundary` + frame (`runner.py:163`) → executor has not durably reserved the intents → crash → restart sees boundary processed (`runner.py:148`) → `no_new_bar` → intents never regenerated (and `prev_frame` has already advanced, so even a forced re-diff yields nothing) → the OPEN/CLOSE/MODIFY is silently lost.

This is a **split transaction**: strategy-state and execution-state are two separate `state.save()` commits, and the *gate that guards replay* is advanced by the *first* commit. The slice's own design intended otherwise — `M3-LIVE-VERTICAL-SLICE-DESIGN.md:88` specifies persisting "last bar, last trades frame hash, **open-intent ledger**" as one **crash-safe** unit; the code splits them.

### Crash matrix

| # | Crash point | Restart outcome | Classification |
|---|---|---|---|
| 1 | before runner commit (`<runner.py:163`) | boundary not advanced → full recompute → intents regenerated | **safe replay** |
| 2 | after runner commit, before `executor.apply` (`runner.py:163`→`main.py:58`) | `no_new_bar`; intents never regenerated/executed | **skipped intent (silent loss)** — LR-1 core |
| 3 | during reconciliation (`executor.py:40`, pre-execute) | `no_new_bar`; intents lost (reconcile mutations were in-mem, not yet durable) | **skipped intent** |
| 4 | after ledger reserve, before broker submit (`executor.py:112`→`136`) | nothing durable (save is at :99); broker untouched; `no_new_bar` | **skipped intent** (no duplicate) |
| 5 | after broker submit, before executor commit (`mt5_gateway.py:136`→`executor.py:99`) | broker holds position; ledger/mirror lost; `no_new_bar` → next cycle reconcile sees unknown magic position → **FREEZE** | **frozen / unknown outcome — recoverable via reconciliation** |
| 6 | after confirm, before mirror durable (same in-mem window, commits together at `executor.py:99`) | identical to #5 (confirm+mirror share one commit) → reconcile freeze | **frozen / unknown — recoverable via reconciliation** |
| 7 | during final cycle logging (`main.py:68`) | executor already committed (`:99`); only `cycles.jsonl`/heartbeat line lost; reader tolerates (`ops_log.py:60-63`) | **safe** (cosmetic log gap) |

**Key asymmetry:** duplicate-submission risk is **low** across the matrix *because* the boundary-dedup gate suppresses regeneration — but that same gate is precisely what causes the **silent skip** in cases 2–4. The design traded duplicate-risk for skip-risk. In dry-run this is inert (no broker); in **live** it means an engine-believed entry is never opened, or a CLOSE/MODIFY never reaches the broker, leaving a stale stop — later surfacing only when a downstream transition references a non-existent mirror (`safety.py:54-57` `unknown_position` block) or reconcile freezes (case 5/6).

### Classification: **LR-1 = CONFIRMED**

Cases 2–4 are silent intent loss; cases 5–6 degrade to freeze (safe-ish, but the intent ledger is blind to what happened). **Do not promote to live while LR-1 is unresolved.**

---

## No-new-bar gate audit

**Condition:** `runner.py:148` — `if self.state.data["last_boundary"] == boundary_str: return no_new_bar`, where `boundary = latest_closed_boundary(max(candles["time"]))` (`runner.py:144-145,78-83`).

**Identities compared:** the **latest closed 15m boundary timestamp only.** Nothing else — not row count, not file mtime, not live-candle content, not frozen-candle content, not any input-revision hash. (`frame_hash` at `state.py:26` and `prev_frame_hash` hash the *output* frame, not the input; `provenance.json` carries a *static* frozen sha, never re-checked in the loop.)

**Detection table (conceptual):**

| Change | Detected? | Why |
|---|---|---|
| Appended candle advancing a new 15m close | ✅ yes | new boundary ≠ stored |
| Appended M1 within the still-forming window | ✅ correctly ignored | boundary unchanged = correct bar-close semantics |
| Corrected historical live candle (same latest boundary) | ❌ **no** | boundary unchanged → skip |
| In-place rewrite, unchanged row count | ❌ **no** | content never inspected |
| Changed OHLC at same frontier timestamp | ❌ **no** | `max(time)` unchanged → same boundary |
| Rewritten file, unchanged mtime granularity | ❌ **no** | mtime never read |
| Changed frozen file (correction not advancing max time) | ❌ **no** | frozen content not hashed in-loop |
| Correction not advancing the latest boundary | ❌ **no** | gate is boundary-only |

**Precise failure statement:** the runtime **incorrectly skips recomputation whenever any live or frozen input is corrected/rewritten in a way that does not advance the latest closed 15m boundary.** Structure-level corrections (an amended OHLC in a prior closed window changing OB detection) are exactly this case and are silently ignored.

---

## Correction-safe design gap

Required invariant: `candidate boundary unchanged AND exact candle-input revision unchanged → no_new_bar`. The runtime implements only the first clause.

| Requirement | Status | Evidence |
|---|---|---|
| Byte-exact live-input snapshot | **absent** | `assemble_candles` reads the file fresh each cycle (`runner.py:68,142-143`); no snapshot |
| Full SHA-256 of live input content | **absent** | no per-cycle input hash anywhere in the gate path |
| Parse from the same captured bytes that were hashed | **absent** | file is re-opened and re-parsed; no capture |
| Verified frozen-data pin | **partial** | checked once in `deploy_check.py:79` (existence) + static sha in `provenance.json`; **not** enforced per cycle |
| Exact composite input revision | **absent** | only `last_boundary` |
| Atomic persistence of `last_recomputed_input_revision` | **absent** | persists `last_boundary` (`state.py:55`), not an input revision |
| Strategy outputs/state bound to that revision | **partial** | bound to boundary; `prev_frame_hash` is an output hash, not an input revision |
| Bounded periodic full audits | **absent** | none |
| Correction detection when timestamps/row counts unchanged | **absent** | see gate table above |
| No hash-then-reopen TOCTOU window | **absent** | no hashing at all; and read-fresh pattern is inherently TOCTOU-exposed |

**Phase 4 (blueprint) load-once cache verdict:** it is **only a subordinate optimization**, and **as written it could mask corrections** (a cached frozen frame would additionally hide frozen-file corrections that the boundary gate already misses). It is **not safe as a standalone Phase 4** and **requires the exact-input-revision model to exist first**. Do not reduce correction-safety to a frozen-DataFrame cache; the cache must live *inside* the revision model, keyed on the composite input revision.

---

## Heartbeat reassessment

| Heartbeat | Path | Writer | Schema | Freq | Meaning | Consumer | Stale ⇒ |
|---|---|---|---|---|---|---|---|
| Market-data | `market_data_dir/heartbeat.json` | `MT5BarBridge._heartbeat` (`mt5_bridge.py:45-49`) | `{at,last_bar_time,appended,error}` | per poll (cycle start) | ingestion liveness / last bar | `test_live_slice.py:130` (+ CT/operators) | feed stalled |
| Cycle/ops | `state_dir/ops/heartbeat.json` | `OpsLog.cycle_end` (`ops_log.py:47-50`) | `{at,boundary,status,error,duration_s}` | per completed cycle | cycle completion outcome | `deploy_check.py:55` (+ `test:437`) | cycles not completing |

**Classification: `DISTINCT LAYER-SPECIFIC HEALTH SIGNALS`.** Different paths, schemas, semantics, and consumers — not two authoritative writers of one contract. **The blueprint's "single heartbeat writer — VIOLATION" (§4) is overstated and is hereby corrected.** The genuine gap is different and unaddressed by either file: **neither proves *process* liveness *during* the ~963 s compute** (both write only at cycle edges), and there is **no process-vs-progress liveness distinction**. That is the real work item (a dedicated liveness signal, multi-signal wedge detection per Recovery Audit §7) — not de-duplication.

---

## Recovered later-safety findings

The specific safety vocabulary (LR-1, TOCTOU, input-revision, execution journaling, arming, allowlists, stale-signal, spread ceiling, bounded audits) does **not** appear in this worktree's docs (grep: none) — the `81e468c` baseline **predates** the `FX-OB-Research-Lab` audit series (Architecture V1 §I/invariants, Audit #1 §I activation gate, Audit #2 §2/§4–6, Recovery §7). Those remain the "later approved safety architecture." The slice's own docs partially anticipate them (`M3…:88` crash-safe intent ledger; `MVP-EXECUTION-PLAN.md:85,106,108` idempotent-no-duplicate + restart-recovery + kill-−9 drills), so LR-1 is a gap against **this project's own stated criteria**, not just external audits.

| Finding | Applies to runtime? | Fully addressed in code? | In blueprint? | Severity | Phase |
|---|---|---|---|---|---|
| LR-1 split cycle/intent transaction | yes | **no** | no (missed) | **Critical** | corrective P1 |
| Execution journaling / atomic commit boundary | yes | partial (two `state.save` commits, not one atomic unit) | no | High | P1 |
| Pending-intent recovery | yes | **no** (no pending store; gate blocks regen) | no | **Critical** | P1–P2 |
| Unknown broker outcome | yes | partial (reconcile freeze, cases 5–6) | partial | High | P2 |
| Idempotency across restart | yes | partial (ledger blocks *reserved* dupes; nothing for *unreserved* skips) | overstated as ✅ | High | P1–P2 |
| Startup reconciliation before arming | yes | partial (`deploy_check restart` drill; not an in-loop arm gate) | no | High | P2/P7 |
| Exact-input revision | yes | **no** | no (Phase 4 conflates it with a cache) | **Critical** | P3 |
| Correction-safe fast-idle | yes | **no** (boundary-only gate) | no | **Critical** | P3 |
| TOCTOU prevention | yes | **no** | no | Medium-High | P3 |
| Broker account/server allowlist | yes (live) | **no** (only symbol whitelist `safety.py:37`) | listed as P2 | High | P7 |
| Arming controls (token/expiry) | yes (live) | **no** (P1 refuses live at `main.py:84`; no positive arm) | listed as P2 | High | P7 |
| Stale-signal rejection | yes | **no** | listed as P2 | Medium | P7 |
| Spread/slippage ceiling | yes (live) | **no** | listed as P2 | Medium | P7 |
| Heartbeat semantics / process-vs-progress liveness | yes | partial (edge-only, no liveness-during-compute) | **overstated** (wrong root cause) | Medium | P4 |
| Bounded periodic audits | yes | **no** | no | Medium | P3 |

---

## Canonical-plan correction

**1. Recovery discoveries that remain valid.** The `live/` runtime exists and is coherent; runner calls Lux driver helpers with zero re-implemented strategy logic (`runner.py:1-10,97-135`); Lux is the sole strategy authority, engine-hash pinned (`runner.py:51`, `config.py:15`); MT5 write path is contained to `mt5_gateway.py` and gated by `mode=="live"` (`executor.py:104`) with P1 refusing live (`main.py:84`); idempotent intent *identities* exist (`intents.py:44`); reconcile-freeze-on-unknown exists (`executor.py:60-65`); the parity oracle exists (`rehearsal.py`); cycle JSONL exists (`ops_log.py`).

**2. Blueprint claims that remain valid.** Stage map and line references (§1); seam verdicts for config/market-data/intents/gateway/publisher; the ~963 s cause (per-cycle 273 MB re-read, `runner.py:68,142`); Lux-stays-byte-unchanged rule; live promotion NO-GO.

**3. Incomplete claims.** "Idempotent across restart" — true only for *reserved* intents, not for the *skip* window (LR-1). "Reconcile-before-act" — true, but blind to *missed* opens (reconcile only catches unknown/missing, not never-submitted). Phase 4 "load-once cache" — omits the exact-input-revision prerequisite.

**4. False / overstated claims.** (a) Heartbeat "single-writer VIOLATION" — **false**; they are distinct layer-specific signals. (b) The blueprint's roadmap ordering puts performance (Phase 4 cache) at the same tier as correctness and **before** any durability fix — **inverted**. (c) Implicit "execution is fully built" tone underweights LR-1 and correction-safety.

**5. Omitted live-mode blockers.** LR-1 split transaction; pending-intent recovery; exact-input revision + correction-safe idle; TOCTOU; account/server allowlist; positive arming control; stale-signal; spread/slippage ceiling; bounded periodic audit; in-loop startup-reconcile-before-arm gate.

**6. Corrected implementation order (derived from the code, correctness first):**

- **Phase 0 — Safe baseline verification** (read-only): run `rehearsal` parity gate + `pytest` + `deploy_check preflight`. Confirms green baseline. *No code.*
- **Phase 1 — LR-1 durable cycle/intent transaction:** make boundary-advance and intent-ledger reservation a **single atomic commit**, OR move the `no_new_bar` gate to key on a durably-reserved intent/commit marker rather than `last_boundary` alone. Root fix for crash cases 2–4. Touches `runner.py`, `state.py`, `executor.py`, `main.py` ordering.
- **Phase 2 — Restart & crash recovery:** pending-intent replay for the reserved-but-unconfirmed window; formalize the freeze path for cases 5–6; kill-−9 drills (`MVP…:106`).
- **Phase 3 — Exact-input revision + correction-safe fast-idle:** composite input revision (frozen pin sha + live-segment content sha), snapshot-then-parse-same-bytes (TOCTOU-free), `last_recomputed_input_revision` atomic persistence, bounded periodic full audit. Replaces the boundary-only gate.
- **Phase 4 — Heartbeat/liveness contract:** keep the two distinct signals; add a dedicated process-liveness signal that advances during compute; multi-signal wedge detection (Recovery Audit §7). *Low-risk; may run parallel to P2/P3.*
- **Phase 5 — Profiling & benchmarking:** per-phase timers (driver-side only, never in Lux); reuse `rehearsal`/injection seams.
- **Phase 6 — Frozen-data caching within the exact-input model:** the load-once optimization, keyed on the Phase 3 revision; parity-gated by `rehearsal`.
- **Phase 7 — Remaining live-promotion rails:** account/server allowlist, arming token+expiry, spread/slippage ceiling, stale-signal rejection, in-loop startup-reconcile-before-arm.

This differs from the blueprint (which front-loaded heartbeat consolidation and a naive cache) and from the brief's suggested order only in placing the heartbeat contract (P4) after crash-recovery, since LR-1 is the highest-severity finding and the heartbeat is low-risk and partly parallelizable.

**7. Revised Go/No-Go verdict:** the blueprint is **not canonical**. This reconciliation supersedes its §4 heartbeat claim, its roadmap ordering, and its "GO for Phases 0–5" framing.

---

## Final verdict — three independent decisions

- **Read-only baseline verification: GO.** Running `rehearsal`, `pytest`, and `deploy_check preflight` is safe and is the correct first action; it validates the recovered inventory and the LR-1 crash reasoning against live behavior.
- **Observability / performance work: QUALIFIED GO / PARTIAL NO-GO.** Profiling (P5) and the heartbeat/liveness contract (P4) are GO. **Performance caching (blueprint Phase 4 / this P6) is NO-GO until the exact-input-revision model (P3) exists** — a naive cache would compound the correction-masking defect. Correctness (P1–P3) precedes all performance work.
- **Live promotion: NO-GO.** LR-1 is CONFIRMED; correction-safe recomputation is absent; pending-intent recovery, arming controls, and account/server allowlists do not exist. Promotion is barred until at least Phases 1–3 and 7 are complete and drilled per `MVP-EXECUTION-PLAN.md:85`.

No code was modified, no commits or branches created, the existing blueprint is untouched, no VPS contact, no MT5 connection.
