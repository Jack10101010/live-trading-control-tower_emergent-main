# Historical documents

Superseded planning and recovery-era documents, kept because they contain
reasoning that is not recorded anywhere else. **None of them describes the
current system.**

For the current state, read
[`../governance/CANONICAL-IMPLEMENTATION-MAP.md`](../governance/CANONICAL-IMPLEMENTATION-MAP.md).

## Why these were archived rather than deleted or committed as-is

All four sat untracked in the repository root for two weeks. That is the worst
of both states: they are not version-controlled, so nothing records when they
stopped being true, and they are present, so a reader finds them and believes
them. Committing them unchanged would have published stale architecture as
current; deleting them would have discarded the only written record of several
decisions.

Each is dated **2026-07-18/19**, which is **before the honesty programme
started** (M-RISK-1, 2026-07-30). Everything they say about fixture data, fleet
records, provenance, packages, recommendations and telemetry predates its
removal.

| document | still true | superseded by |
|---|---|---|
| `CANONICAL-IMPLEMENTATION-MAP.md` | the architecture-conformance table and per-component seams, roughly | `docs/governance/CANONICAL-IMPLEMENTATION-MAP.md` |
| `CLAUDE-CODE-HANDOFF.md` | the documentation-roles table; the frozen C1–C5 names | `docs/handoffs/` |
| `LIVE-IMPLEMENTATION-BLUEPRINT.md` | the `live/` module inventory — useful for VPS work | line numbers are stale; roadmap superseded |
| `LIVE-IMPLEMENTATION-BLUEPRINT-RECONCILIATION.md` | the LR-1 persistence-ordering trace | governs over the raw blueprint where they conflict |

## Specific claims that are now FALSE

- Baseline `53383eb` / `81e468c` — 116 commits behind `d64536d`.
- "Git is unavailable in this sandbox" — it is available via the worktree's
  `--git-dir`; every milestone since has been verified against real history.
- Every statement about fixture-backed operator surfaces. Eighteen
  fixture-backed routes became eight, all under `/api/dev/fixture-*`, and the
  authored world moved out of the runtime package entirely (M-WORLD-0).
- "Parity Policy v2 and PDR-001 are ABSENT" remains true, but the C1–C5
  programme they governed is complete and has been overtaken by the honesty
  programme and the activation milestones.

## Secret sweep

Swept for account numbers, credentials, tokens, absolute user paths, email
addresses, SSH paths and host identifiers before archiving. Clean. The phrase
"arming token" appears in three of them and is a **feature name**, not a value.
