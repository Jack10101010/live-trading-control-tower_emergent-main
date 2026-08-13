# Activation dependency gate

*Exactly what must arrive from VPS M-NODE-ACCT-1 before activation, derived by
withholding each field from the real admission code and reading the verdict —
not by reading the contract document.*

Established by M-RELEASE-CHECKPOINT-1. Every row was produced by feeding a
payload to `activation_policy.account_admissible` / `activation_check.run` with
that field removed, and recording what actually happened.

> **This list and `VPS-CONTRACT-M-NODE-ACCT-1.md` must agree.** A requirement
> that exists only in the checker is a ghost requirement; a requirement that
> exists only in prose is a wish. Where the two differed, the code won and the
> contract was corrected.

---

## BLOCKING — activation cannot reach a PASS without these

| field / capability | why | Mac parser | Mac test | checker if absent | verdict |
|---|---|---|---|---|---|
| `account.identity.available: true` | identity is only identity when the node says it observed it. A pinned deployment cannot compare against a block the node did not read | `activation_policy.account_admissible` | `test_identity_the_node_says_it_did_not_observe_is_not_identity` | `account.refused` FAIL | **FAIL** |
| `account.identity.fingerprint` | the value the pin compares against | same | `test_identity_pin_matrix` | `account.refused [account_identity_unverifiable]` | **FAIL** |
| `account.identity.server` | pinned separately; a right account on the wrong server is the wrong account | same | `test_identity_pin_matrix` | `account.refused [account_identity_unverifiable]` | **FAIL** |
| `account.health.available: true` **or** `identity.available: true` | without either, nothing was observed and there is no account | `account_admissible` | `test_1_node_current_account_absent` | `account.admissible WARN [account_not_observed]` · `account.reaches_ui` never PASSes | **WARN, never PASS** |
| finite numeric `balance` / `equity` / `free_margin` (or `null`) | NaN/Infinity are not measurements | `live_telemetry.validate_snapshot` | `test_numeric_adversaries_on_balance` | ingest returns **400**; the node never appears | **FAIL** |
| `published_at` within 120 s of arrival | beyond that the clocks disagree and no freshness verdict is reliable | `activation_check.check_node` | `test_the_checker_FAILS_on_clock_skew_beyond_the_budget` | `node.clock_skew` FAIL | **FAIL** |
| `schema_version: ct.node-telemetry.v1` | the only accepted version | `live_telemetry.validate_snapshot` | `test_malformed_and_unknown_version_are_refused_at_ingest` | ingest **400** | **FAIL** |
| sampling on more than OPEN cycles | an account observed once a day is indistinguishable from a broken one | — | — | `account.capability WARN` indefinitely | **WARN forever** |

**The pin itself is a Mac-side blocking prerequisite, not a VPS one.**
`CONTROL_TOWER_EXPECTED_ACCOUNT` / `_SERVER` must be set **on the backend
process**, not only in the operator's shell — `account.pin_enforced_by_runtime`
FAILs otherwise.

---

## WARN-ONLY — useful, not required

| field | effect if absent |
|---|---|
| `account.health.observed_at` | admitted, but money is never compared between two sources — the Mac cannot distinguish disagreement from drift, so it claims neither |
| `account.health.balance` / `equity` / `free_margin` | admitted; each renders `—`. Absence is honest, and `null` never becomes `0` |
| `account.health.currency`, `identity.currency`, `trade_mode` | cosmetic; the projection falls back |
| `trade_allowed` / `trade_expert` | tri-state — `null` means "not reported", which is not "forbidden" |
| `account.health.healthy` / `reasons` | displayed verbatim; the Mac never recomputes them |
| `reconciliation.snapshot_status` | drives the MT5-observation contradiction warning |
| `cycle.status` | selects the phase budget (clamped) and the degraded state |
| a `capabilities` marker | **does not exist in the contract.** `capability_signal` returns `unknown` and the checker WARNs. This is the one documented gap |

---

## FUTURE — outside first activation

- Position price and profit — the node publishes neither, so `unrealizedPnL`
  stays `null` rather than being derived.
- Execution authority — structurally unreachable from this path and out of
  scope for activation entirely.
- A capability marker upgrading `unknown` to `supported`.
- Pinning positions and orders (they carry provenance, not admission).

---

## No ghost requirements

Checked mechanically: every BLOCKING row above corresponds to a refusal the
code actually produces, and every MUST in `VPS-CONTRACT-M-NODE-ACCT-1.md`
appears here. Two corrections came out of that comparison:

1. The contract said `published_at` is used **only** for clock skew. It also
   feeds data staleness through `observation()`.
2. The contract implied the Mac enforces "never send `available: true` with an
   all-null sample". It does not — `_node_accounts` will project an admitted
   record with every figure null. Nothing false is displayed, but the account
   status flips to *available* on the strength of a claim with no content. That
   rule is enforced on the node side only, and is now marked as such.
