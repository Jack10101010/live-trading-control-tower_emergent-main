# The activation gate

*M-ACTIVATE-READINESS-1. The admission policy, the contradiction matrix and what
activation makes observable. Companion to `ACTIVATION-RUNBOOK.md`.*

## Six permissions, not one boolean

`activation_policy.ActivationVerdict` answers six independent questions about one
received observation. There is deliberately **no `is_live()`**: a single boolean
would have to pick one meaning, and every surface reading it would assume a
different one.

| permission | granted when | never granted by |
|---|---|---|
| `node_identity_visible` | a payload validated at all | — always true for a usable payload |
| `node_mt5_observation_visible` | the node stated an MT5 connection observation | the tower's own adapter |
| `node_account_admissible` | the node's own `available` is `true`, numbers are real, identity matches the pin | node health alone |
| `broker_truth_admissible` | same as above | node telemetry alone |
| `analytics_input_admissible` | admissible **and** not degraded | stale data is fine here; degraded is not |
| `execution_authority` | **never** — a constant `False` | anything at all |

`execution_authority` is a constant rather than a computation. There is no input
that can turn it on, which is a stronger statement than "no input currently
does".

## Two admission questions, kept apart

**Provenance** answers *who observed this?* and is earned from evidence.
**Admission** answers *is this the account this deployment is pinned to?* and is
a question about configuration.

Collapsing them would force one of two lies: an identity mismatch would either
rewrite the record's origin (false about where it came from) or pass unremarked
(false about what it is). So a genuine reading of the wrong account keeps
`provenance: node_mt5` and carries `admitted: false` with a named reason, and is
**shown as refused** rather than hidden. An operator needs to see that a node is
loudly reporting the wrong account; "no authoritative account source" would send
them to wait for something that is already arriving.

Pinning is via `CONTROL_TOWER_EXPECTED_ACCOUNT` / `CONTROL_TOWER_EXPECTED_SERVER`,
falling back to the execution side's pre-existing `NODE_EXPECTED_ACCOUNT_FINGERPRINT`
so that one fact under two names cannot leave one of them unset. Both set to
different values is a checker FAIL — nothing in the runtime can say which is right.

**Unpinned is not an error**, but an unpinned deployment cannot detect an account
switch, which is why the checker reports it as WARN and never PASS.

**A pinned deployment refuses an observation with NO identity.** The first
version compared only when a fingerprint was present, so a node publishing
`health.available: true` with `identity.available: false` delivered real balances
with `admitted: True` and no reasons — the condition the pin exists to catch,
defeated by omitting a block. "Cannot be checked" is not "passed the check".

The pin applies to **both** sources. It was originally enforced only on
node-relayed records, on the reasoning that this Mac cannot read a terminal —
reasoning about the current host, not about the code.

## The rules, in one list

- Node telemetry never grants broker truth. The two provenance sets are disjoint,
  asserted at runtime by `node_and_broker_authority_are_disjoint()`.
- Account data never grants execution authority.
- Provenance is earned from validated observation, never from configuration.
- Missing availability fails closed — compared with `is True`, never truthiness.
- Malformed fields fail closed; unknown schema versions fail closed.
- Stale data stays stale, and stays **admitted**: it is real, just old.
- Degraded outranks stale, and blocks analytics while leaving the account visible.
- Local mock never outranks node MT5 truth. Precedence is by **authority**, not
  locality.
- Two genuine sources that disagree are both kept and the disagreement is raised.
- Storage location grants no authority. Endpoint name grants no authority.
- Identity or server mismatch prevents admission; so does an identity that
  cannot be checked while a pin is set.
- **The unrecognised fails closed everywhere, including in the shape of a
  value.** `admitted` is honoured only as `true`, `null` or absent; every other
  form — `"false"`, `0`, `{}` — is a refusal, not an admission.
- Structural impossibility is a refusal; economic alarm is a warning.

## Contradiction matrix

Twenty-four conditions, each with a test. `backend/tests/test_activation_policy.py`
holds the policy-level cases; `test_activation_e2e.py` holds the ones that need
the full receive path.

| # | condition | admitted | UI state | startup | execution |
|---|---|---|---|---|---|
| 1 | node current, account absent | none | node visible, "account source unavailable" | continues | unaffected |
| 2 | node current, account `available: false` | none | identical to 1 — same fact | continues | unaffected |
| 3 | node current, account valid | the account | LIVE account card, `node_mt5` | continues | unaffected |
| 4 | node stale, account timestamp current | the account | visibly **stale**, "last reported" | continues | unaffected |
| 5 | node degraded, values present | the account | degraded card + node's own reasons; **analytics blocked** | continues | unaffected |
| 6 | MT5 observed, no account supplied | none | observation visible, no account; `mt5_contradiction` | continues | unaffected |
| 7 | account supplied, MT5 unreachable | the account | account visible **plus** `mt5_contradiction` warning | continues | unaffected |
| 8 | account login ≠ pinned | **none** | red "Account observation refused", precise copy | continues | unaffected |
| 9 | account server ≠ pinned | **none** | as 8, naming the server | continues | unaffected |
| 10 | local MT5 and node MT5 agree | both | two rows, same figures | continues | unaffected |
| 11 | local MT5 and node MT5 differ | **both** | both rows **plus** `contradictory_account_sources` | continues | unaffected |
| 12 | local reports A, node reports B | both | two distinct accounts — legitimate, no warning | continues | unaffected |
| 13 | mock local + genuine node | node only | mock dropped by the gate, no trace | continues | unaffected |
| 14 | two nodes, same account, different balances **sampled within 60 s** | **both** | both rows **plus** contradiction naming both nodes | continues | unaffected |
| 14b | two nodes, same account, samples far apart | **both** | both rows, **no** warning — that is drift, not disagreement | continues | unaffected |
| 15 | two nodes, different accounts | both | two cards — a legitimate deployment, no warning | continues | unaffected |
| 16 | snapshot timestamp in the future | per identity | freshness judged on arrival, so unaffected | continues | unaffected |
| 17 | skew beyond 120 s | per identity | admitted, but checker **FAILs** `node.clock_skew` | continues | unaffected |
| 18 | NaN / Infinity / a numeric string / a bool | **none** | ingest returns 400 naming the field path | continues | unaffected |
| 19 | equity absent, balance present | the account | balance shown, equity `—`. **Not a refusal** | continues | unaffected |
| 20 | payload claims `node_mt5` without evidence | **none** | the claim is ignored; provenance is derived | continues | unaffected |
| 21 | pinned, account observed with **no identity** | **none** | refused, `account_identity_unverifiable` | continues | unaffected |
| 22 | **negative balance or equity** | **the account** | shown, with a `account_figure_negative` warning | continues | unaffected |
| 23 | envelope claims fresh over an old arrival | per identity | **stale** — the verdict takes the more pessimistic witness | continues | unaffected |
| 24 | `admitted` is a non-boolean (`"false"`, `0`, `{}`) | **none** | refused: the unrecognised fails closed | continues | unaffected |

### Structurally impossible versus economically alarming (row 22)

A negative balance or equity is **admitted and shown**, with a warning. The
distinction is deliberate and was corrected during certification:

* **Refused** — values that cannot be measurements: `NaN`, `Infinity`, a numeric
  string, a bool, a list. These are contract violations.
* **Warned** — values that are real but notable: negative balance or equity,
  free margin exceeding equity. These are facts about a bad day.

The gate originally refused negatives too, which blanked the entire account. An
account goes below zero when a gap runs through a stop-out, and that is the
moment an operator most needs the number. It was also the only thing the rule
could still catch: `live_telemetry._finite` already rejects `NaN`/`Infinity` at
ingest, so on any payload that arrived over the wire, the non-negative clause's
sole reachable effect was refusing a genuine loss.

This module defines **no** financial validation beyond structural possibility.
Deciding that an equity is "too negative" is not something a projection may do.

### One freshness authority, two witnesses (row 23)

`verdict()` read `entry["stale"]` and believed it while the projection recomputed
the age from the tower's arrival clock. In production the two agree, because
`_live_status_entry` sets the flag from the same observation — but "they agree
today" is not a property, and a divergence would have shown a fresh checker
verdict beside a stale account card with nothing to explain it.

`recomputed_stale()` now takes the **more pessimistic** of the reported flag and
a recomputation, so it can only ever be stricter than either witness alone. An
arrival the tower cannot date — missing, unparseable, or timezone-naive — reads
stale. The checker adds a genuinely independent witness (`node.freshness_independent`)
which recomputes the age against **its own** clock and FAILs when the two
authorities disagree.

### Why money is compared only between near-simultaneous samples

Row 14 is the one that needed care. Equity moves with every tick that moves an
open position, and balance moves whenever a trade closes, so comparing them
across two observers who looked at different moments raises a contradiction on a
correct two-node deployment — **permanently**. That is the alarm fatigue this
module's own docstring warns about, arriving through the door meant to prevent
it.

Identity facts (`server`, `currency`) are compared always: two genuine sources
must never disagree about those, whenever they looked. Money facts (`balance`)
are compared only when both observers stated `observed_at` and the two are
within 60 s. `equity` is not compared at all.

Two properties hold across the whole table. **Startup always continues** — an
activation fault is a data condition, not a reason to refuse to run. And
**execution is unaffected in every row**, because no row can reach it.

Row 18 is stronger than originally designed: the expectation was per-field
coercion to `null`, and the ingest boundary turned out to reject the entire
snapshot with a named field path. Recorded rather than relaxed — a payload
containing an impossible number is a publisher fault, and believing the rest of
it would be trusting the same producer.

## Observability

No competing status system was created. Everything is on surfaces that already
existed.

| fact | where |
|---|---|
| resolved environment | `/api/health` → `environment` |
| node schema version | `/api/live/status` → entry `schema_version` |
| legacy vs canonical | `/api/live/status` → `legacy_source` |
| account capability | `activation_policy.capability_signal`, printed by the checker |
| observation availability | account `provenance` (`absent` when nothing was read) |
| observation origin | account `provenance` (`node_mt5` / `live_mt5` / `mock-fixture`) |
| last received / observation time | entry `received_at` · account `observedAt` |
| age, stale limit, basis | entry `liveness_age_seconds`, `stale_after_seconds`, `freshness_basis` |
| phase | reflected in the stale budget: 120 s idle, 900 s recompute |
| clock skew | derived by the checker from `published_at` vs `received_at` |
| **identity match result** | account `admitted` + `admissionReasons` — on node-relayed AND locally-read ACCOUNT records. **Positions, orders and the live-runtime broker panel are not pinned** — see the known boundary below. |
| selected source | account `nodeId` (`null` = locally read) |
| rejected-source reasons | `admissionReasons`, rendered as operator copy |
| contradiction warnings | `/api/operations/summary` → `warnings`, shown in the Warnings card |

Account identifiers are already one-way fingerprints when they reach the Mac.
The checker masks them a second time for terminal output that may end up in a
screenshot (`acctfp_0…cdef`), and prints no token, no absolute path and no
credential — asserted by `test_guard_12b`, over the AST of its `print` calls.
It also refuses any `--base` that is not `http(s)`, because `urlopen` accepts
`file://` and a mistyped host would receive the bearer token.

### A state worth naming: identity without health

A node may report `identity.available: true` with `health.available: false` —
it knows which account it is on and has no figures yet. That produces a genuine
`node_mt5` record whose money fields are all `null`, rendered as a card of
dashes. It is honest, and it deliberately flips the account status from
`unavailable` to `available`: the account source IS up, it has simply not
reported figures. Anything deriving performance from it sees `null`, never zero.

## Known boundaries — surfaces the pin does NOT reach

Named rather than left to be discovered, because each is a place a future
milestone must look.

**`/api/live-runtime` and `LiveRuntimePanel`.** `BrokerRuntimeView` carries
`balance` and `equity` and has no `admitted` field, so the identity pin does not
apply to it. It is fed **only** by this process's own supervisor snapshot — never
by node telemetry — so activation cannot put node-relayed money there, and on
this Mac the adapter is the mock, which the panel's provenance already marks. It
becomes reachable the day a Control Tower runs on a host with a real terminal.

**Positions and orders.** `PositionOperationalView` and `OrderOperationalView`
carry provenance but no admission, so a pinned deployment gates the ACCOUNT and
not the positions attributed to it. Neither carries a balance.

**The contradiction warning's audience.** `account_contradictions` reaches
`/api/operations/summary` and renders in the Operational Dashboard's Warnings
card. Accounts & Protection and Fleet Overview — the two pages that show the
disagreeing balances side by side — read `useOperationalFleet`, which does not
fetch the summary. Both records are shown; the fact that they disagree is one
page away.

**The node's own budget.** `stale_budget_for` derives the freshness budget from
a cycle status the node supplies (120 s idle, 900 s otherwise). `recomputed_stale`
now clamps that to the projection's 120 s, so a node cannot buy itself a larger
window — but the phase mechanism itself remains node-influenced by design.

## Where the code is

| | |
|---|---|
| policy | `backend/activation_policy.py` |
| checker | `backend/activation_check.py` (GET only) |
| synthetic payloads | `backend/tests/activation_payloads.py` |
| policy + matrix tests | `backend/tests/test_activation_policy.py` |
| offline end-to-end | `backend/tests/test_activation_e2e.py` |
| fifteen guards | `backend/tests/test_activation_guards.py` |
| UI activation states | `frontend/src/views/__tests__/activationStates.test.tsx` |
| frontend gate | `frontend/src/lib/operationalProvenance.ts` |
