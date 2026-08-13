> **SUPERSESSION NOTICE (2026-07-18).** The C1–C5 mapping *proposed* in §6 (Milestone mapping) and §7 (Implementation readiness) of this document is **WITHDRAWN**. The authoritative frozen programme is: **C1 Instrumentation · C2 Candidate boundary helper · C3 InputRevision + snapshot + LR-1 gate · C4 Fast idle · C5 Cadence** (exact-input revision lives inside C3; heartbeat/profiling/benchmark are C1; live-promotion rails are outside C1–C5). See `CLAUDE-CODE-HANDOFF.md` §3–§4 for the governing definitions. Everything else in this document (architecture conformance, parity status, repository status, per-component seams) remains valid. Also updated: the Golden rehearsal is **COMPLETE — PASS** (run twice on the Mac, `exit=0`), and the verified baseline is HEAD `53383eb` on `live-implementation-prep`, clean worktree.

# Control Tower — Canonical Implementation Map

**Type:** Read-only architectural reconciliation. No code, commits, branches, staging, or edits to existing docs. This new document is the single canonical engineering reference; it supersedes conflicting terminology in the planning docs but modifies none of them.
**Date:** 2026-07-18
**Environment note:** produced from filesystem + code evidence. Git is unavailable in this sandbox (worktree gitdir is Mac-side), so committed-vs-worktree lineage is not verifiable here; all "current repository" statements describe the on-disk working tree.

---

## 0. Authoritative source availability (blocking context)

The five authoritative sources are **not all present in the repository**:

| # | Source | Present? | Evidence |
|---|---|---|---|
| 1 | Control Tower Architecture V1 | **YES** | `CONTROL-TOWER-ARCHITECTURE-V1.md` (§0–§13) |
| 2 | Parity Policy v2 | **ABSENT** | 0 files/mentions repo-wide |
| 3 | PDR-001 | **ABSENT** | 0 files/mentions repo-wide |
| 4 | Live Implementation Blueprint | **YES** | `LIVE-IMPLEMENTATION-BLUEPRINT.md` + `-RECONCILIATION.md` (governing) + `-PHASE1-LR1-DESIGN.md` |
| 5 | Current repository | **YES** | `live/`, `backend/`, `golden/`, Lux (sibling repo) |

**Primary conflict:** sources #2 (Parity Policy v2) and #3 (PDR-001) — the two that define the engineering rules and the implementation programme — do not exist in the repo. The parity *practices* they would encode **do** exist operationally (golden byte-parity, `engine_version` pinning, Lux boundary tests, `parity_harness.py`, rehearsal), and the *programme* exists fragmented across `MVP-EXECUTION-PLAN.md` (M1–M7), the Blueprint (Phase 0–5), and the accepted Reconciliation (corrected P0–P7). This map treats the **Reconciliation's corrected order as the authoritative programme** (it is the latest accepted correction and supersedes the Blueprint's ordering) and the **golden/Lux parity discipline as the de-facto Parity Policy**, flagging that neither is a ratified "v2"/"PDR-001" document.

**C1–C5 do not exist as a defined framework anywhere in the repo.** Task 6/7 therefore *propose* a canonical C1–C5 definition derived from the Reconciliation; it requires operator ratification before it is authoritative.

---

## Task 1 — Architecture validation (vs Architecture V1)

| Component (Arch V1 §) | Status | Evidence / divergence |
|---|---|---|
| **Shared Core** (§3) | **Implemented (consumed)** | Lux `strategy_core` (sibling repo, pinned `d978074`, engine `5bb6372c…`); consumed pure via `runner.golden_pipeline` (`runner.py:97-135`, `core.*`). No re-implementation. |
| **Trading Node** (§1 modular monolith) | **Implemented (dry-run)** | `live/` package = one process hosting the cycle; single instance (EURUSD). Multi-instance supervisor not built (single-instance M3 scope). |
| **Data Service** (§4.2) | **Partial / Diverged** | `mt5_bridge` (MT5 M1 feed) + `assemble_candles` (frozen CSV + live segment). No Polygon adapter, no gap-backfill service, no multi-provider, no divergence monitor. |
| **Session/Calendar Service** (§4.3) | **Diverged (in-core)** | Sessions/news handled **inside Lux core**, not a separate CT service. Acceptable for parity (Lux owns it); the standalone service is unbuilt. |
| **MT5 Adapter / Gateway** (§4.5) | **Implemented (MT5 only)** | `mt5_gateway.py` — sole MetaTrader5 touchpoint; structured `(ok, data)`. **Missing:** capability declaration, symbol normalization beyond suffix, retries/backoff. |
| **Execution Engine** (§4.5) | **Implemented (mirror model)** | `executor.py` — order lifecycle, idempotent intents, ledger state machine, SENT-before-submit, reconcile-freeze. **Missing:** capability-adaptation, slippage recording, bounded retries. |
| **Event Journal** (§2 "the spine") | **Diverged (major)** | **Not** the designed append-only event journal with the mandated envelope. Actual durable stores: `runner_state.json` (ledger/mirror/boundary), `cycles.jsonl` (per-cycle ops), `heartbeat.json`. **Absent:** `event_id`, `seq`, `schema_version`, `event_time` vs `wall_time`, `config_version`, `correlation_id`/`causation_id` (grep count 0). This is the largest architectural drift — a deliberate lightweight substitute for the M3 slice. |
| **Risk Guard** (§4.4) | **Partial** | Pre-trade gate present: `safety.py` (kill-switch file, symbol whitelist, duplicate-intent, daily-loss, max-positions, unknown-position). **Absent:** autonomous guard, dead-man's switch, equity floor, account/server allowlist, arming token, spread/slippage ceiling (grep count 0). |
| **Reconciler** (§4.6) | **Partial** | `executor.reconcile` (reconcile-before-act, freeze-on-unknown) + `drain_pending` SENT adoption. **Absent:** periodic-timer reconciliation, full `ReconciliationReport` event, adopt/alert policy matrix. |
| **Gateway API + Control Tower** (§4.7) | **Partial** | `backend/server.py` (FastAPI; `/api/live/ingest`, `/api/live/status`; SQLite command/event/audit loop) + `frontend/`. Thin-client display present; full command plane (start/stop/flatten/halt) partial. |
| **Notifier** (§4.8) | **Missing** | No Telegram/push/alerting (grep count 0). |
| **Deployment model** (§6) | **Implemented (config)** | Mac CT + Windows VPS node + MT5; `config.py` env-driven; `live/VPS-RUNBOOK.md` present. Matches intent. |
| **Dependency rule** (§13.1) | **Honored** | Core does no I/O (Lux boundary tests); no order calls outside gateway/executor; broker identity contained. |
| **Invariants** (§13.2) | **Partial** | Determinism/journal-append/broker-containment honored; "heartbeat fresh during compute" **not** implemented (dual edge-only heartbeats); "CT displays, node decides" honored. |

**Architectural drift summary:** (1) Event Journal reduced to ledger + cycles.jsonl without the mandated envelope — **DIVERGED, deliberate**; (2) Notifier, autonomous Risk Guard, arming/allowlists — **MISSING, deferred to live-promotion**; (3) Data Service single-source — **PARTIAL**. None are parity or ownership violations; all are scope-staged.

---

## Task 2 — Blueprint validation

The governing blueprint is `LIVE-IMPLEMENTATION-BLUEPRINT.md` **as corrected by** `-RECONCILIATION.md`.

| Blueprint section | Classification |
|---|---|
| §1 Runtime map (call graph, line refs) | **Still accurate** (matches on-disk `live/`) |
| §2 Seam verdicts (config/data/intents/gateway/publisher) | **Still accurate** |
| §3 Insertion points | **Still accurate** (unimplemented, but seams valid) |
| §4 Invariant table — "single heartbeat writer VIOLATION" | **Incorrect → superseded** by Reconciliation §heartbeat (distinct layer-specific signals) |
| §5 Roadmap ordering (Phase 1 = heartbeat, Phase 4 = naive cache) | **Superseded** by Reconciliation's corrected order (correctness-first: LR-1 → crash recovery → exact-input revision → heartbeat → profiling → caching → live rails) |
| §6 Go/No-Go ("GO for Phases 0–5") | **Superseded** by Reconciliation verdict |
| `-RECONCILIATION.md` (LR-1 confirmed, corrected order, 3 verdicts) | **Still accurate and governing** |
| `-PHASE1-LR1-DESIGN.md` (design + exactly-once correction) | **Still accurate**; implemented |

Net: the **Reconciliation** is the accurate current description; the **original Blueprint's roadmap and heartbeat claim are superseded**; the Blueprint's factual runtime map remains accurate.

---

## Task 3 — Programme validation (PDR-001)

**PDR-001 is absent** (§0), so a line-by-line PDR comparison is impossible. Mapping the repository to the *de-facto* programme (Reconciliation corrected order + PROJECT_STATE milestones):

| Programme item | Status | Production files |
|---|---|---|
| Milestone 1 — scope lock + Golden Run #1 | **Already complete** (PASSED) | `golden/run-001/**`, `SCOPE-LOCK.md` (Lux-side) |
| Milestone 2 — core extraction + parity harness | **Already complete** (PASSED, slices 1–2) | Lux `strategy_core/**`, `golden/tools/parity_harness.py` |
| M3 — live vertical slice (dry-run shadow) | **Partially complete** | `live/*.py` (all 15 modules) |
| Reconciliation P0 — baseline verification | **Complete** | tests + deploy_check (dry-run gates green) |
| Reconciliation P1/P2 — LR-1 durable transaction + crash recovery | **Complete** | `live/state.py`, `live/runner.py`, `live/executor.py`, `live/main.py`, `backend/tests/test_live_slice.py` |
| Reconciliation P3 — exact-input revision / correction-safe idle | **Not started** | (target: `live/runner.py` `assemble_candles`/`run_once`) |
| Reconciliation P4 — heartbeat/liveness contract | **Not started** | (target: `live/ops_log.py`, `live/mt5_bridge.py`, `live/main.py`) |
| Reconciliation P5 — profiling/benchmarking | **Not started** | (new `live/benchmark.py`; `live/runner.py` timers) |
| Reconciliation P6 — frozen-data caching within revision model | **Not started / Blocked on P3** | `live/runner.py` |
| Reconciliation P7 — live-promotion rails (allowlist, arming, spread ceiling, autonomous guard, Notifier) | **Not started / Deferred** | `live/safety.py`, `live/config.py`, `live/main.py`, new notifier |
| Event Journal (Arch V1 §2 full envelope) | **Deferred** | not yet built |
| Golden rehearsal re-confirmation post-LR-1 | **Blocked (Mac-only)** | `live/rehearsal.py` (15–20 min; cannot run in sandbox) |

---

## Task 4 — Parity validation (vs Parity Policy v2)

Parity Policy v2 is absent as a document; validated against the **operational parity discipline** (golden byte-parity, Lux boundary tests, engine identity, rehearsal, idempotency).

| Rule | Compliance | Evidence |
|---|---|---|
| Strategy ownership | ✅ | All strategy computation via Lux `core.*` (`runner.py:110`); no CT recompute |
| Shared-core ownership | ✅ | `strategy_core` pure; Lux boundary tests forbid `MetaTrader5`/IO imports |
| Lux boundaries | ✅ | Live node imports Lux read-only; zero re-implementation |
| Deterministic replay | ✅ | `rehearsal` RUN B/B2 determinism; `diff_frontier` pure |
| Engine identity | ✅ | `verify_engine` refuses mismatch (`runner.py:51`); `ENGINE_VERSION_EXPECTED=5bb6372c…` (`config.py:15`) |
| Replay parity | ✅ (by construction; Mac-unconfirmed post-LR-1) | LR-1 change touches only the ledger, not trade frames; 35/35 tests |
| Idempotency | ✅ | sha1 intent ids (`intents.py:44`) + duplicate rail + PENDING durability + at-most-once SENT |
| Execution ownership | ✅ | `order_send` only in `mt5_gateway.py`; no order calls elsewhere |

**NO PARITY VIOLATIONS FOUND.** Caveat: verified against the de-facto discipline, not a ratified Parity Policy v2 document; and the post-LR-1 Golden rehearsal must be re-run on the Mac to *confirm* replay parity (expected to pass by construction).

---

## Task 5 — Implementation matrix (remaining items)

| Requirement | Current Status | Repository Evidence | Remaining Work | Files Allowed To Change | Acceptance Test |
|---|---|---|---|---|---|
| Exact-input revision / correction-safe idle | Not started | `no_new_bar` gates on `last_boundary` only (`runner.py:148`); no input hash | Composite input revision (frozen sha + live-segment content sha), snapshot-then-parse (TOCTOU-free), persist `last_recomputed_input_revision`, bounded periodic audit | `live/runner.py`, `live/state.py`, tests | Corrected candle at same boundary triggers recompute; unchanged input → no_new_bar; rehearsal parity holds |
| Heartbeat/liveness contract | Not started | Dual edge-only heartbeats; no liveness during compute | In-process liveness signal + multi-signal wedge detection; consolidate semantics | `live/ops_log.py`, `live/mt5_bridge.py`, `live/main.py`, tests | `heartbeat` advances mid-cycle; wedge distinguished from long compute |
| Profiling/benchmarking | Not started | 963s cycle uncharacterized | Per-phase timers (driver-side only), off-VPS harness | new `live/benchmark.py`, `live/runner.py`, tests | Per-phase p50/p95 over ≥50 cycles; Lux byte-unchanged |
| Frozen-data caching (within revision) | Not started (blocked on revision) | `assemble_candles` re-reads 273MB/cycle (`runner.py:68`) | Load-once cache keyed on input revision | `live/runner.py`, tests | `rehearsal` byte-parity holds; cached==fresh |
| Autonomous Risk Guard + rails | Not started | Pre-trade rails only (`safety.py`) | Equity floor, dead-man, account/server allowlist, arming token, spread/slippage ceiling | `live/safety.py`, `live/config.py`, `live/main.py`, tests | Each rail blocks; kill-switch persists; drill each |
| Notifier | Missing | none | Independent alert channel (Telegram/push) | new module, `live/main.py` | Alert fires with CT offline |
| Event Journal (full envelope) | Deferred | ledger + cycles.jsonl only | Envelope (event_id/seq/schema_version/event_time/wall_time/config_version) | new journal module + consumers | Replay reconstructs state from journal |
| Post-LR-1 Golden rehearsal | Blocked (Mac) | cannot run in sandbox | Run on Mac | none (verification) | `VERDICT: PASS`, exit 0 |

---

## Task 6 — Milestone mapping (terminology reconciliation)

| Canonical concept | Equivalent names across docs | Status |
|---|---|---|
| Scope lock + Golden Run #1 | "Milestone 1" (PROJECT_STATE, SCOPE-LOCK); "M1" (MVP plan) | **Complete** |
| Core extraction + parity harness | "Milestone 2" (PROJECT_STATE); "M2" (MVP plan); "MILESTONE-2-EXTRACTION-DESIGN" | **Complete** |
| Live vertical slice (dry-run shadow) | "M3" (M3-LIVE-VERTICAL-SLICE-DESIGN); "MVP" | **Partially complete** (dry-run runs; live gated) |
| Baseline verification | "Phase 0" (Blueprint & Reconciliation) | **Complete** |
| **Durable intent transaction** | **"LR-1"; "Phase 1 LR-1" (design doc); Reconciliation "P1/P2"** — NOT the Blueprint's "Phase 1" (heartbeat) | **Complete** |
| Exact-input revision / correction-safe idle | Reconciliation "P3"; (Blueprint had no equivalent) | **Not started** → *proposed C1* |
| Heartbeat/liveness | Blueprint "Phase 1"/"Phase 4-adjacent"; Reconciliation "P4" | **Not started** → *proposed C2* |
| Profiling/benchmark | Blueprint "Phase 2/3"; Reconciliation "P5" | **Not started** → *proposed C3* |
| Frozen-data caching | Blueprint "Phase 4"; Reconciliation "P6" | **Not started** → *proposed C4* |
| Live-promotion rails | Reconciliation "P7"; Arch V1 §4.4/§4.8/§I | **Not started** → *proposed C5* |

**Obsolete/confusing names to retire:** the Blueprint's "Phase 1–5" numbering (superseded; its "Phase 1"=heartbeat collides with "Phase 1 LR-1"). **Renamed work:** "LR-1" == Reconciliation "P1/P2". **Undefined:** "C1–C5" — no frozen source; the mapping above is the **proposed** canonical definition (pending ratification).

---

## Task 7 — Implementation readiness (proposed C1–C5)

> These definitions are **derived** from the accepted Reconciliation corrected order (P3–P7), since no frozen C1–C5 spec exists. They require one-line operator ratification before use.

**C1 — Exact-input revision & correction-safe idle** (= Reconciliation P3)
- Purpose: `no_new_bar` must also detect input corrections (not just boundary advance).
- Scope: composite input revision, snapshot-then-parse (TOCTOU-free), `last_recomputed_input_revision`, bounded periodic audit.
- Permitted: `live/runner.py`, `live/state.py`, `backend/tests/test_live_slice.py`.
- Forbidden: Lux `strategy_core`, `golden/**`, `live/executor.py` ledger core, `backend/fixtures/**`.
- Dependencies: LR-1 complete (done).
- Acceptance: corrected candle at same boundary → recompute; unchanged input → no_new_bar; rehearsal parity holds.
- Rollback: revert to boundary-only gate (single function).
- Risk: **Medium** (touches recompute trigger; parity-guarded).

**C2 — Heartbeat/liveness contract** (= P4)
- Purpose: prove liveness during long compute; multi-signal wedge detection.
- Permitted: `live/ops_log.py`, `live/mt5_bridge.py`, `live/main.py`, tests. Forbidden: strategy/executor core, Lux.
- Dependencies: none hard (may follow C1). Acceptance: heartbeat advances mid-cycle; wedge ≠ long compute. Rollback: revert to edge-only. Risk: **Low**.

**C3 — Profiling/benchmarking** (= P5)
- Purpose: characterize the 963s cycle per phase.
- Permitted: new `live/benchmark.py`, `live/runner.py` timers, tests. Forbidden: Lux core (driver-side timing only).
- Dependencies: C1 (measure the revision-aware path). Acceptance: per-phase p50/p95; Lux byte-unchanged. Rollback: delete harness. Risk: **Low**.

**C4 — Frozen-data caching within the revision model** (= P6)
- Purpose: eliminate per-cycle 273MB re-read, safely.
- Permitted: `live/runner.py`, tests. Forbidden: Lux core, golden artifacts.
- Dependencies: **C1 (mandatory)** — cache must key on the input revision. Acceptance: `rehearsal` byte-parity holds; cached==fresh frame. Rollback: remove cache. Risk: **Medium**.

**C5 — Live-promotion rails** (= P7)
- Purpose: autonomous Risk Guard, account/server allowlist, arming token, spread/slippage ceiling, Notifier; the gate to remove the `LIVE_MODE=live` refusal.
- Permitted: `live/safety.py`, `live/config.py`, `live/main.py`, new notifier, tests. Forbidden: strategy/parity core.
- Dependencies: C1–C4 + real-terminal MT5 verification of the live SENT path. Acceptance: each rail drilled; kill-switch persists; Notifier fires CT-offline; first live order carries broker-side SL. Rollback: re-assert `main.py:93` refusal. Risk: **High** (live safety).

---

## Final summary

**Architecture status:** Conforms to Architecture V1 for the shared-core, node, gateway, execution-engine, and dependency-rule/parity layers. Deliberate divergences: Event Journal reduced to ledger + `cycles.jsonl` (no full envelope); Notifier, autonomous Risk Guard, and arming/allowlists missing (deferred to live-promotion). No parity or ownership violations.

**Repository status:** Clean `live/` node (no TODO/HACK, no swallowed exceptions, MT5 contained). Dry-run shadow cycle fully implemented; LR-1 durable transaction + SENT-payload correction present and tested (35/35). Live submission dormant behind `main.py:93` + `executor.py:202`. Git lineage unverifiable from sandbox.

**Parity status:** NO PARITY VIOLATIONS FOUND (vs operational discipline; Parity Policy v2 doc absent; post-LR-1 Golden rehearsal must be re-run on the Mac to confirm).

**Programme status:** M1, M2, Phase 0, and LR-1 (P1/P2) complete. P3–P7 not started; Event Journal and live rails deferred. PDR-001 absent — de-facto programme = Reconciliation corrected order.

**Completed milestones:** M1, M2, Phase 0, Phase 1 LR-1.
**Remaining milestones:** proposed C1 (exact-input revision), C2 (heartbeat), C3 (profiling), C4 (caching), C5 (live rails).

**Documentation requiring updates (not done here):** add PDR-001, Parity Policy v2, and the frozen C1–C5 spec to the repo (currently absent); record LR-1 completion in `PROJECT_STATE.md`; retire the Blueprint's superseded Phase 1–5 numbering; correct the Blueprint §4 heartbeat "violation" claim.

**Safe next implementation:** proposed **C1 — exact-input revision & correction-safe idle**, at the `no_new_bar` gate in `live/runner.py` (`run_once`/`assemble_candles`) + `live/state.py`. It is the next correctness item, parity-guarded, and unblocks C4.

**Expected implementation order:** C1 → C2 → C3 → C4 (needs C1) → C5 (needs C1–C4 + real-terminal verification). Event Journal envelope can be scheduled alongside C2/C3 if audit-grade replay is required before live.

**Final verdict:**

**NOT READY TO BEGIN C1** — for one specific, non-code reason: **C1 has no ratified definition in the repository.** PDR-001, Parity Policy v2, and the C1–C5 specification are absent (§0), so "C1" is undefined; the definition in Task 7 is *proposed*, not authoritative. The repository *itself* is otherwise ready — clean, parity-intact, LR-1 complete, next seam identified. Ratify the proposed C1 definition (or import the frozen C1–C5 spec + PDR-001 + Parity Policy v2), and implementation can begin immediately at the `live/runner.py` `no_new_bar` seam with no further audit required.
