# Validation Policy

*Adopted 2026-08-07 after two weeks in which validation repetition consumed the
majority of available engineering time. The identity systems below exist so
that expensive validation runs only when something it validates has changed.*

## The one rule

**`engine_manifest_id` unchanged ⇒ engine output unchanged ⇒ no replay.**
CT-side changes (telemetry, status, publisher, UI, docs, tests, ops scripts)
structurally cannot alter engine output. Never replay history to validate them.

## Daytime development (every prompt)

- Focused test files for the modules actually touched, plus
  `test_telemetry_scope_guards.py` when the telemetry path is touched.
- Safety snapshot: two per state change (before stop, after start) — not per step.
- Representative **1–2 year windowed parity** ONLY when engine trading behavior
  actually changes, and only if nightly is too far away.
- Reports: verdict, deltas, blockers, one table. Detail goes to `artifacts/`.

## Milestone (per completed chunk)

- Candidate suite (`CANDIDATE_VALIDATION=1`, ~21 s).
- `deploy_check preflight` (~1 min) if deploying.
- Coherence block (both HEADs, both pins, recomputed engine identity) on any
  branch switch — see `live/VPS-SHADOW-PARITY-RULE.md`.

## Nightly (unattended — the VPS is idle ~20 h/day)

- Full backend suite; failure-identity diff against the last known-good run.
- Same-host golden compare against the preserved production artifact.
- Soak/sampler review of the day's cycles from `cycles.jsonl`.

## Release / promotion only

- Full-history replay: **only when `engine_manifest_id` changes**, once per
  engine candidate.
- Same-host byte-equality gate (the promotion authority) + archive witness
  (`live/golden_compare.py`; policy in `live/golden_contract.json`).
- Shadow window (20+ cycles from `cycles.jsonl` — no separate collector).
- Reconnect/recovery drill.

## Never

- Rerun expensive validation because CT/UI/telemetry/docs changed.
- Regenerate a reference artifact that is already preserved and pinned.
- Re-prove invariants mid-operation that were proven at its start.
- Cross-machine byte parity (impossible by construction — see
  `live/GOLDEN-ARTEFACT-CONTRACT.md`).
