# Control Tower — Claude Code Implementation Handoff

**Type:** Planning→implementation transition. Documentation only — no code, commits, branches, staging, deploy, VPS, or MT5. This document is the single entry point for Claude Code. It does not redesign architecture, redefine milestones, or re-audit; it aligns the existing documents with the now-frozen programme.
**Date:** 2026-07-18
**Verified baseline (operator, Mac):** repo `live-trading-control-tower-live-prep`, branch `live-implementation-prep`, HEAD `53383eb` ("Phase 1: implement LR-1 durable intent transaction"), tracked worktree clean, no staged/unstaged changes. Phase 1 LR-1: implemented · reviewed · corrected · tested (35/35) · committed. Golden rehearsal: **PASS** (`exit=0`, run twice on the Mac).

---

## Task 1 — Documentation roles

| Document | Purpose | Owns | Does NOT own | Status |
|---|---|---|---|---|
| **Control Tower Architecture V1** (`CONTROL-TOWER-ARCHITECTURE-V1.md`) | Long-term target architecture | Component design, dependency rule, invariants, event-journal spine, extension model | Implementation order, milestone scope, current runtime state | **Frozen** (changes only via ADR) |
| **Parity Policy v2** | Engineering governance for parity | Parity rules (strategy/core ownership, engine identity, deterministic replay, idempotency, execution ownership) | Architecture design, milestone order | **Frozen** — *not present as a file in the repo working tree*; its rules exist operationally (Lux `strategy_core/BOUNDARY.md` + boundary tests, `engine_version` pinning, `golden/tools/parity_harness.py`, `live/rehearsal.py`). See §3 ambiguity. |
| **PDR-001** | Implementation programme | The C1–C5 sequence + acceptance | Architecture, parity rules | **Frozen** — *not present as a file in the repo working tree*; its programme is the frozen C1–C5 recorded in §3 below. See §3 ambiguity. |
| **Live Implementation Blueprint** (`LIVE-IMPLEMENTATION-BLUEPRINT.md`) | Describe the runtime that exists | Factual runtime call-graph/seams (§1–§3) | Roadmap/ordering (superseded); heartbeat "violation" claim (superseded) | **Historical / partial** — runtime map accurate; roadmap & §4 heartbeat claim superseded by the Reconciliation |
| **Blueprint Reconciliation** (`LIVE-IMPLEMENTATION-BLUEPRINT-RECONCILIATION.md`) | Correct superseded Blueprint findings | LR-1 confirmation, corrected correctness-first order, heartbeat reclassification | Frozen architecture; final C1–C5 names | **Living / working reference** (governs over the raw Blueprint) |
| **Corrected Canonical Implementation Map** (`CANONICAL-IMPLEMENTATION-MAP.md`) | Working implementation map | Architecture-conformance table, parity status, per-component seams, repo status | The C1–C5 definitions (its §6/§7 *proposed* mapping is **superseded** by the frozen C1–C5 in §3) | **Working reference** — valid except §6/§7 (see supersession banner in that file) |
| **Phase 1 LR-1 Design** (`LIVE-IMPLEMENTATION-PHASE1-LR1-DESIGN.md`) | Record of the completed LR-1 transaction | Durable-intent design + at-most-once/freeze guarantee | Future milestones | **Historical (completed)** — context for C3's LR-1 gate |

---

## Task 2 — Consistency check (genuine inconsistencies only)

1. **Resolved:** Blueprint roadmap/heartbeat vs Reconciliation → the **Reconciliation governs**; the Blueprint's Phase 1–5 numbering is retired.
2. **Resolved:** C1–C5 definitions → now **frozen** by the operator (§3); the Canonical Map's withdrawn proposal is superseded (banner added to that file).
3. **Remaining (ambiguity, not contradiction):** **Parity Policy v2** and **PDR-001** are named authoritative but are **not present as files in the repo working tree** — Claude Code cannot open them. Their *content* is covered operationally (parity discipline) and by the frozen C1–C5. See §3.
4. **Consistent:** architecture conformance, parity status (no violations), and repository status agree across Architecture V1, the Reconciliation, and the Canonical Map.

No architecture, parity, milestone, or repository-status contradictions remain among the documents that are present.

---

## Task 3 — Milestone alignment (frozen programme)

The implementation programme is **frozen** as:

- **C1 — Instrumentation**
- **C2 — Candidate boundary helper**
- **C3 — InputRevision + snapshot + LR-1 gate**
- **C4 — Fast idle**
- **C5 — Cadence**

These are not redefined here. All present documentation is aligned to them; the only artifact that predated them (`CANONICAL-IMPLEMENTATION-MAP.md` §6/§7) now carries a supersession banner pointing to this list. Live-promotion rails, the full event journal, and the Notifier are **outside** C1–C5 (classified as longer-term / live-promotion work).

---

## Task 4 — Implementation readiness (one paragraph each)

**C1 — Instrumentation.** Goal: add operational observability (per-phase cycle timing/counts, runner sub-phase profiling, a benchmark harness, and a liveness signal that advances during long compute) with zero behavioural change. Dependencies: none (LR-1 complete). Seam: `live/ops_log.py` (`cycles.jsonl`/`heartbeat` already exist), `live/main.py` cycle stage boundaries, `live/runner.py` `golden_pipeline` stages timed via the Lux `ports.Profiler` seam, `live/shadow_report.py` gates. Acceptance: `cycles.jsonl` carries per-phase durations, benchmark yields stable p50/p95, heartbeat advances mid-cycle, rehearsal still PASS, Lux byte-unchanged. Allowed: `live/ops_log.py`, `live/main.py`, `live/runner.py` (driver-side timing only), `live/mt5_bridge.py`, `live/shadow_report.py`, new `live/benchmark.py`, tests. Forbidden: Lux `strategy_core` (edit — timing only via the port), `golden/**`, `backend/fixtures/**`, executor/state ledger, `intents.py`. Risk: **Low** (metrics only).

**C2 — Candidate boundary helper.** Goal: formalize the candidate-boundary computation (latest closed 15m boundary) as a reusable, independently-testable helper the C3 gate consumes. Dependencies: none hard. Seam: `live/runner.py` `latest_closed_boundary`/`run_once` (`runner.py:78,144`). Acceptance: helper returns the correct boundary across cases (incl. DST/session/gap edges), `run_once` outputs unchanged, rehearsal parity holds. Allowed: `live/runner.py`, tests. Forbidden: Lux core, `golden/**`, executor/state ledger, fixtures. Risk: **Low** (pure boundary arithmetic).

**C3 — InputRevision + snapshot + LR-1 gate.** Goal: make `no_new_bar` correction-safe — an exact InputRevision (content hash of frozen pin + live-segment bytes), snapshot-then-parse from the hashed bytes (TOCTOU-free), gate on *(boundary unchanged AND revision unchanged)*, and persist `last_recomputed_input_revision` atomically with the existing LR-1 boundary+PENDING commit. Dependencies: **C2**; LR-1 (done). Seam: `live/runner.py` gate (`runner.py:148`) + `assemble_candles` (`runner.py:68`), `live/state.py` (additive revision field). Acceptance: corrected candle at same boundary → recompute; unchanged input → `no_new_bar`; TOCTOU eliminated; revision committed atomically with boundary+PENDING; rehearsal byte-parity PASS. Allowed: `live/runner.py`, `live/state.py`, tests. Forbidden: Lux core, `golden/**`, executor ledger internals, `intents.py`, fixtures. Risk: **Medium** (touches recompute trigger + LR-1 commit; parity-guarded).

**C4 — Fast idle.** Goal: when boundary and InputRevision are unchanged, return `no_new_bar` cheaply — skip the 273MB reload/parse and pipeline; load-once frozen cache keyed on the C3 revision. Dependencies: **C3 (mandatory)**. Seam: `live/runner.py` `assemble_candles`/`run_once` (`runner.py:68,142`). Acceptance: no-new-bar cycles skip the reload; cached frame byte-identical to fresh; corrected input busts the cache; rehearsal parity PASS; per-cycle time drops (measured by C1). Allowed: `live/runner.py`, tests. Forbidden: Lux core, `golden/**`, executor/state ledger, fixtures. Risk: **Medium** (cache must key on the exact revision or it masks corrections; C3 is the guard).

**C5 — Cadence.** Goal: formalize scheduler cadence — replace the fixed `time.sleep(10)` with boundary-aligned cadence, deterministic backlog/catch-up, and a bounded cycle budget, using C1 instrumentation to hold SLOs. Dependencies: **C1**, **C3/C4** (fast idle makes tight cadence feasible after the 963s reduction). Seam: `live/main.py` loop (`time.sleep`), `live/runner.py` statuses, `live/shadow_report.py` budgets. Acceptance: cadence aligns to 15m closes; backlog caught up deterministically; no eligible boundary skipped; steady-state within budget; rehearsal parity PASS. Allowed: `live/main.py`, `live/runner.py`, tests. Forbidden: Lux core, `golden/**`, executor/state ledger, fixtures. Risk: **Low–Medium** (timing only; must not skip an eligible boundary).

---

## Task 5 — Claude Code document set

Provide these, and only these, for C1–C5 implementation (one reason each):

1. **This handoff (`CLAUDE-CODE-HANDOFF.md`)** — the single index: frozen milestones, per-milestone seams, permitted/forbidden files, and the canonical doc list.
2. **Control Tower Architecture V1** — the frozen invariants and dependency rule Claude Code must not violate (esp. the strategy-core ownership boundary).
3. **Parity governance** — **Parity Policy v2 if it exists as a Mac file; otherwise** the operational equivalent: Lux `strategy_core/BOUNDARY.md` + `tests/test_strategy_core_slice8_boundary.py`, and `golden/tools/parity_harness.py` + `live/rehearsal.py` — the parity rules and the gate every change must pass.
4. **Blueprint Reconciliation** — the accurate current-state description and correctness-first ordering (supersedes the raw Blueprint).
5. **Corrected Canonical Implementation Map** (with its supersession banner) — the architecture-conformance table and per-component seams; ignore its §6/§7 (superseded by this handoff's §3).
6. **Phase 1 LR-1 Design** — context for C3's LR-1-gate interaction (the transaction C3 extends).

**Excluded as obsolete/duplicative for C1–C5 execution:** the raw Live Implementation Blueprint's roadmap (superseded by #4 — provide the Blueprint only if its runtime call-graph is wanted, but #5 echoes it); `MVP-EXECUTION-PLAN.md`, `M3-LIVE-VERTICAL-SLICE-DESIGN.md`, `MILESTONE-2-EXTRACTION-DESIGN.md`, `SCOPE-LOCK.md`, `PROJECT_STATE.md`, `AUDIT-AND-ROADMAP.md` (historical context only, not needed to execute C1–C5). Where the Blueprint and Reconciliation overlap, **use the Reconciliation**.

---

## Final output

**1. Recommended document set for Claude Code:** items 1–6 in Task 5 (this handoff; Architecture V1; parity governance [Policy v2 or the Lux boundary + golden/rehearsal equivalent]; Blueprint Reconciliation; Corrected Canonical Map w/ banner; Phase 1 LR-1 design).

**2. Recommended implementation order:** **C1 → C2 → C3 → C4 → C5.** Hard dependencies: C3 requires C2; C4 requires C3; C5 requires C1 and C3/C4. C1 and C2 have no unmet dependencies and can begin immediately.

**3. Remaining planning ambiguity:** exactly one — whether **PDR-001** and **Parity Policy v2** exist as standalone documents on the Mac (outside this repo's working tree, where they are absent). If they exist, include them in the set and treat them as authoritative over the operational equivalents; if they do not, their content is fully covered by the frozen C1–C5 (this handoff §3) and the operational parity discipline (Task 5 #3). This ambiguity does **not** block C1: C1 = Instrumentation depends on neither document's physical file.

**4. Final verdict:**

**READY TO BEGIN CLAUDE CODE IMPLEMENTATION**

Evidence: HEAD `53383eb` is a clean, committed Phase-1 LR-1 baseline; LR-1 is reviewed/corrected/tested; the Golden rehearsal PASSED twice on the Mac (parity intact after LR-1); the programme is frozen as C1–C5; C1 (Instrumentation) has no unmet dependency, a parity-safe scope (observability only, timing via the sanctioned `ports.Profiler` seam), identified seams, and enumerated permitted/forbidden files; and the document set is de-duplicated and internally consistent. The single open item (physical presence of PDR-001 / Parity Policy v2) is covered by equivalents and does not gate C1.
