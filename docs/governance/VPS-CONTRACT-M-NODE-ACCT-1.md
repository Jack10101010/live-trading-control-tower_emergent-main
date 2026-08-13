# VPS contract — M-NODE-ACCT-1 account observation

*What the Mac Control Tower genuinely needs from the execution node, and nothing
else. Written by M-ACTIVATE-READINESS-1; this is the basis for the VPS
implementation prompt.*

## The good news first

**The shape already exists.** `live/telemetry.py` already builds
`account.identity` via `safe_identity` and `account.health` via `safe_health`,
both inside the existing `ct.node-telemetry.v1` envelope. The Mac already
projects them (`operational_projection._node_accounts`), already gates them
(`activation_policy.account_admissible`) and is already tested against them
offline (`backend/tests/test_activation_e2e.py`).

So this is **not a new contract**. It is a request to *populate a block the
node already emits*, on more cycles than it does today.

No new schema version is required. No new endpoint. No new field is requested
below that the Mac does not already consume — anything the Mac ignores is
deliberately absent from this document, because a field nobody reads is a field
that drifts.

---

## MUST

### M1 · Populate `account.identity` whenever the terminal is readable

```json
"account": {
  "identity": {
    "available": true,
    "fingerprint": "acctfp_0123456789abcdef",
    "server": "FTMO-Demo",
    "currency": "USD",
    "trade_mode": "demo"
  }
}
```

- **`account.identity` MUST accompany `account.health`.** A pinned deployment
  refuses a health-only observation with `account_identity_unverifiable`: there
  is nothing to compare against the pin, and admitting it would deliver real
  balances into a deployment that believes it is pinned.
- `available` **MUST** be a real boolean, `true` only when the node actually
  read the terminal this cycle. The Mac compares with `is True`; a missing key,
  a null, a `0`, or the string `"true"` all read as **not observed**.
- `fingerprint` **MUST** be `account_fingerprint(login, server)` — the existing
  SHA-256 helper. The raw MT5 login **MUST NOT** be published (see N1).
- `server` **MUST** be the terminal's own server string, verbatim.

### M2 · Populate `account.health` on the same cycles

```json
"health": {
  "available": true, "healthy": true,
  "balance": 4211.5, "equity": 4180.25, "free_margin": 4000.0,
  "currency": "USD", "trade_allowed": false, "trade_expert": true,
  "observed_at": "2026-08-02T11:59:50Z", "reasons": []
}
```

- `available` — same rule as M1.
- `balance`, `equity`, `free_margin` **MUST** be finite real numbers, or
  **`null`**. `NaN` and `Infinity` are refused at ingest and the whole snapshot
  is rejected with `numeric_invalid (account.health.balance)`. Note that
  Python's own `json.dumps` emits a bare `Infinity` token by default and
  `json.loads` accepts it, so **no library will stop you** — the node must check.
- **Negative values MUST be sent as they are.** A negative balance or equity is
  a real account state after a gap through a stop-out, and the Mac admits it and
  shows it with an `account_figure_negative` warning. Do not clamp to zero and
  do not suppress the sample: a suppressed loss is the most dangerous kind of
  missing number. (The Mac briefly refused negatives itself; that was a defect
  and is fixed.)
- **`null` MUST mean "not reported", never zero.** The Mac renders `null` as
  `—` and a `0` as a measurement, all the way to the pixel.
- `trade_allowed` / `trade_expert` are **tri-state**: `null` means "not
  reported", which is not `false` ("forbidden"). Do not default them.
- `observed_at` **MUST** be when the NODE sampled the terminal, ISO-8601 with an
  explicit UTC offset. It is displayed beside the figures; it is **not** what
  freshness is judged on (see M4). It **is** what makes two observers' figures
  comparable: the Mac only calls two balances contradictory when both stated
  when they looked and the two moments are within 60 s. Omit it and the Mac
  cannot distinguish disagreement from drift, so it will not claim either.

### M3 · Sample on more than OPEN cycles

Today the node samples its account only on cycles containing an OPEN. That is
why a healthy node shows no account for long stretches, and why the Mac treats
that as the ordinary starting state rather than a fault.

For activation to be observable, the node **MUST** sample at least once per
publish cycle, or on a stated interval, and say so. An account observed once a
day is indistinguishable from a broken one.

### M4 · Timestamps

- `published_at` **MUST** be ISO-8601 with an explicit UTC offset.
- The Mac judges **liveness** on its own arrival clock (`received_at`), never on
  `published_at`. A node cannot make itself look live by stamping a recent time.
- `published_at` does two other things, and an earlier version of this document
  wrongly said it did only one. It feeds **data staleness** (`observation()`
  derives `data_stale` from it, and the envelope's `stale` is
  `liveness_stale OR data_stale`), and it is compared with `received_at` to
  detect **clock skew** — a difference above **120 s** is a checker FAIL,
  because past that point no freshness verdict is reliable.
- A future-dated `published_at` **MUST NOT** be used to extend freshness; it
  cannot, but do not rely on that as a feature.

### M5 · Identity must be stable

The same account **MUST** always yield the same fingerprint. The Mac pins
`CONTROL_TOWER_EXPECTED_ACCOUNT` to that value; a fingerprint that changes for
the same account produces `account_identity_mismatch` and blanks the surface.

---

## SHOULD

### S1 · Report `reconciliation.snapshot_status` honestly

The Mac derives an MT5 connection observation from it. Reporting `unavailable`
while supplying an account raises a visible `mt5_contradiction` warning — which
is correct behaviour, and better than reporting `ok` when the snapshot was not
readable.

### S2 · Populate `cycle.status` with the node's own verdict

`error` or a frozen reconciliation makes the node **degraded** on the Mac.
Degraded outranks stale, keeps the account visible, and **blocks analytics** —
deriving performance from a node that has reported its own view is compromised
would compound a fault the node itself declared.

### S3 · Keep `reasons` populated when refusing

`safe_health` already forwards the node's own `HealthVerdict.reasons`. They are
displayed verbatim. The Mac **never recomputes** `healthy`.

---

## MAY

- `account.identity.currency` and `trade_mode` — consumed, but the projection
  falls back sensibly when absent.
- Additional fields anywhere in the payload. The Mac ignores unknown fields
  within a known schema version; forward compatibility is by ignoring, not by
  refusing.
- `positions` detail. The node's positions carry no price or profit, so the Mac
  reports `unrealizedPnL` as `null` rather than deriving one. Adding price data
  would be a separate milestone with its own review.

---

## MUST NOT

### N1 · Never publish the raw MT5 login

It is an account number. `account_fingerprint` exists precisely so identity can
be compared without disclosing it. The Mac performs no second redaction — it
assumes what arrives is already a fingerprint.

### N2 · Never supply provenance as authority

**The node MUST NOT send a `provenance` field, and the Mac would ignore it if
it did.** Provenance on the Mac is *derived from validated evidence*:
`broker_provenance.for_node_observation(observed=...)` stamps `node_mt5` only
when the node's own `available` flag is `true` and the payload validated.

This is the single most important line in this document. The entire honesty
programme exists because provenance was once assigned by configuration —
`adapter=mt5` produced `provenance: live_mt5` with a null balance, a green LIVE
frame conjured from an environment variable. A node that could *declare* its own
authority would reintroduce that defect over the network.

### N3 · Never send `available: true` with an all-null sample

`live/telemetry._carries()` prevents this on the node side today, and the rule
belongs here because **the Mac does not re-check it**: `_node_accounts` will
project a `node_mt5`, admitted record with `balance`, `equity` and `free_margin`
all null from `health.available: true` alone. Every figure renders `—`, so
nothing false is displayed — but the account status flips from *unavailable* to
*available* on the strength of a claim with no content.

This is the exact "confident emptiness" the programme removes, and it is one of
the few rules where the Mac is trusting the node rather than verifying it.

### N4 · Never change `schema_version` without a Mac-side milestone

`ct.node-telemetry.v2` is **refused at ingest** with a 400 — fail-closed, by
design. Unknown versions do not silently downgrade to legacy: `is_legacy_payload`
requires the *absence* of every v1-only section, so a v2 body is rejected rather
than gutted by the legacy adapter.

If the schema must change, the Mac must learn the new version first.

### N5 · Never send zero for unknown

A `0` balance is a claim that the account has no money. `null` is the claim that
nothing was read. These are different facts and the Mac renders them
differently.

---

## Compatibility behaviour, stated explicitly

| the node sends | the Mac does |
|---|---|
| legacy flat payload (pre-UI-2) | adapts it, marks `legacy_source: true`, every unknown field **null** — never invented. Node visible; account impossible. |
| canonical v1, no account block | node visible and **current**. No account. `capability: unknown`. This is the ordinary state, not a fault. |
| canonical v1, `available: false` | identical to the above — a missing key and an explicit `false` are the same fact: nothing was observed. |
| canonical v1, valid account **with identity** | `node_mt5` provenance, admitted if identity matches the pin. |
| canonical v1, health WITHOUT identity, deployment pinned | provenance `node_mt5`, **`admitted: false`**, reason `account_identity_unverifiable`. Balances arriving with nothing to check them against are refused — see M1. |
| canonical v1, health WITHOUT identity, deployment unpinned | admitted. Nothing was checked, and the checker says so as WARN. |
| canonical v1, wrong account | provenance **stays** `node_mt5` (the reading is genuine), `admitted: false`, reason `account_identity_mismatch`. The record is shown as REFUSED with precise copy — not hidden, because an operator must see that a node is reporting the wrong account. |
| malformed v1 | rejected at ingest with 400 and a named field. **A previously good observation is not overwritten.** |
| unknown schema version | rejected at ingest with 400. |
| non-finite number | whole snapshot rejected with `numeric_invalid` and the field path. |

---

## The one gap, and why it is not blocking

**There is no positive capability marker in the contract.**

The Mac cannot distinguish:

- a **new** VPS with account observation, currently idle (has not sampled yet), from
- an **old** VPS that will never report an account.

Both produce a canonical payload with `available: false`. The Mac reports
`CAPABILITY_UNKNOWN` and the checker prints WARN. Guessing "supported" would let
an operator conclude this milestone had landed when it had not; guessing
"unsupported" would send them debugging an idle node.

This is **operational, not safety-critical**: neither state can produce false
money, and both fail closed. It costs an operator time, not correctness.

**Requested, at the node's discretion:** a top-level `capabilities` list — e.g.
`"capabilities": ["account_observation"]` — declared once per payload and
independent of whether a sample was taken this cycle. The Mac would read it as
a positive marker and upgrade `CAPABILITY_UNKNOWN` to `CAPABILITY_SUPPORTED`,
turning a WARN into a PASS. It is additive, so it needs no version bump, and the
Mac will ignore it until a Mac-side milestone consumes it.

Note this remains subject to **N2**: a capability list says what the node *can*
do, never that a particular reading is authoritative. Authority stays derived
from evidence.

---

## How to verify from the VPS side

Nothing here needs the Mac. `backend/tests/activation_payloads.py` builds every
payload class this document describes, and every field in it already exists in
`live/telemetry.py`. Building a snapshot that matches `account_payload()` and
passes `live_telemetry.validate` is sufficient evidence that the Mac will admit
it.
