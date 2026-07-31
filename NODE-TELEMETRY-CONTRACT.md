# Node Telemetry Contract (`ct.node-telemetry.v1`)

UI-2. The read-only seam by which the live execution node tells the Control Tower
what it actually did. One snapshot, one schema version, one direction.

## Direction and ownership

```
live node  ──POST /api/live/ingest──▶  Control Tower backend  ──GET /api/live/status──▶  UI
 (decides)                              (validates, persists)                            (displays)
```

The node is authoritative (Architecture V1 invariant **I-7**: *CT displays; the
node decides*). Every value in a snapshot is something the node already decided or
observed while trading. The Control Tower must never recompute strategy behaviour,
entry eligibility, risk, position size, reconciliation status or execution outcomes
from a snapshot. Strategy behaviour is owned solely by the Lux-OB-Backtester core.

Telemetry is observational and **never a precondition for anything**. A Control
Tower that is offline, slow, unreachable or absent cannot affect execution,
recovery, reconciliation, arming, CLOSE, MODIFY or the kill switch (**I-10**).
`CTPublisher.publish` writes `<state_dir>/publish_last.json` *before* any network
attempt and swallows delivery failure.

## Where the code lives

| File | Role |
| --- | --- |
| `live/telemetry.py` | Node side. Builds the snapshot; owns the redaction rules. |
| `live/publisher.py` | Node side. Builds + POSTs; fail-soft delivery. |
| `backend/live_telemetry.py` | Backend side. Independent validator, legacy adapter, freshness. |
| `backend/server.py` | `POST /api/live/ingest`, `GET /api/live/status`, durable store. |
| `backend/ops_status.py` | Consumer. Normalizes the v1 snapshot back to the flat facts the FROZEN L1A model reads. |
| `backend/tests/test_node_telemetry.py` | The contract's tests, including the drift test. |

The constants (`SCHEMA_VERSION`, list bounds, `REQUIRED_TOP_LEVEL`) are duplicated
across `live/telemetry.py` and `backend/live_telemetry.py` **on purpose**: node and
backend are separate deployables on separate machines, and the backend has no
repo root on `sys.path`. `test_shared_constants_do_not_drift_between_node_and_backend`
is what keeps the duplication honest — update both sides together.

## Snapshot shape

Top-level sections, all required:

| Section | Contents |
| --- | --- |
| `schema_version` | `ct.node-telemetry.v1`. Mandatory. |
| `instance_id` | Node instance identity. |
| `published_at` | Node's own UTC timestamp, timezone-aware. |
| `cycle` | Status, boundary, last bar, trade rows, note. |
| `runtime` | Mode, submission-disabled, kill switch, `open_eligibility`. |
| `engine` | Lineage: expected vs actual engine version, config fingerprint (the golden config's hash — never its path), input revision, symbol, timeframe, deployment profile, data seam. |
| `account` | `identity` (fingerprint, server, currency, trade mode) and `health` (balance, equity, free margin, trade flags). |
| `arming` | Slice-8 arm status, expiry, probation, attempts remaining, fingerprint match. |
| `market` | Slice-5 sample: bid, ask, derived spread, derived tick age, feed health. |
| `reconciliation` | Slice-4 report: frozen, snapshot status, counts, bounded findings, recovery-required. |
| `risk` | Daily realized R, daily date, open mirror count, configured limits. |
| `positions` | Mirrored positions joined with reconciliation outcomes. |
| `execution` | Cycle intents, durable pending intents, attempts, blocks, unresolved-sent, ledger counts, drained. |

### `open_eligibility` — observed blockers, **not** an authorization verdict

`eligible` is tri-state and **can never be `true`**:

| Value | Meaning |
| --- | --- |
| `false` | At least one blocker was actually observed. Each is listed as a stable reason code naming a real rail or runtime condition. |
| `null` | No blocker observed, and authorization was **not** evaluated (`authorization_not_evaluated_by_node_rails`). |

Two gates are structurally invisible to telemetry: the arm session's expiry is
**monotonic** and process-local (only `ArmRuntime.authorize_open` can judge it), and
the Slice-5/6/7 rails run per intent inside the executor at OPEN time. A summary
that returned `true` once its own visible checks passed would contradict the node —
an expired arm session, an unavailable runtime identity or a fingerprint mismatch
all read as eligible while `authorize_open` refuses. The Control Tower must never
present a second authorization opinion (I-7).

Reason codes: `mode_not_live`, `submission_disabled`, `kill_switch_active`,
`not_armed`, `arm_expired`, `arm_disarmed`, `probation_exhausted`,
`runtime_identity_mismatch`, `runtime_identity_not_evaluated`,
`reconciliation_frozen`, `unresolved_sent`, `account_health_unavailable`,
`account_health_blocked`.

### Arming status vs authorization

`arming.status` / `arming.armed` describe the installed **session**, not
authorization. Precedence mirrors `authorize_open`: disarmed → expired → allowance.
Expiry here is read from the request's own recorded ISO timestamp and is
**pessimistic-only** — it can add a blocker, never remove one — because the
authoritative deadline is monotonic.

### Health verdicts are not recomputed

`account.health` publishes the raw sample the node took plus `available`.
`healthy` stays `null`: the authoritative evaluation is the rail inside the
executor, and its outcome is published through `execution.blocks[].rail` when an
OPEN is actually evaluated. Telemetry deliberately does not run a second health
evaluation, because two evaluations of one safety question can disagree.

## Redaction — the safety-critical rule

**Publishable:** hashed account fingerprint, broker server name, currency, trade
mode, balance/equity/margin as already computed by the node, arm
status/expiry/attempts, reason codes, bounded execution summaries, engine lineage,
mirrored/reconciled positions.

**Never publishable:** the MT5 login or password, any raw credential, the arm
nonce, the arm request digest, arm-request file contents, any replay-sensitive
value, raw broker request payloads, API tokens, raw environment values.

The account login is an account number and is replaced by a one-way fingerprint:

```
acctfp_<first 16 hex of sha256("<login>|<server>")>
```

Same account → same fingerprint (so binding can be verified); the fingerprint does
not disclose the account number. `test_snapshot_never_contains_login_nonce_or_digest`
asserts against the serialized bytes, not field names, so a leak through a new
nested field fails the suite.

## Observed-only semantics — no invention

`account.identity`, `account.health` and `market` are published **only when the
node actually sampled them this cycle** (the Slice 5/7/8 rails sample once per
OPEN-containing cycle). Building a snapshot never triggers a broker read to fill a
gap, so publication cannot add load or a new failure mode to a trading cycle.

Absent data is `null` with `available: false` — never a zero, a default, or a
fixture value. `available: true` is a claim that something was really observed: an
object carrying none of the expected fields is treated as not observed.
Likewise `reconciliation.clean` is `null` when no report exists, because "no
reconciliation ran" must never read as "reconciliation was clean".

`positions` carries no price or PnL field at all — this path has neither, so any
such field could only be fabricated.

## Bounds

A snapshot is a summary, never a log: positions ≤ 50; intents, attempts, blocks,
unresolved-sent and findings ≤ 25 each; reason lists ≤ 12; strings clipped to 200
characters and stripped of non-printables. Position rows and eligibility reason
lists are validated per item, so a malformed row is rejected rather than coerced
into a plausible-looking one. The body is capped at 256 KiB — refused on the
declared `Content-Length` before buffering where one is present, and re-checked
after reading for chunked bodies. Telemetry therefore cannot become a
memory-growth vector.

## Validation and persistence

`POST /api/live/ingest` rejects a malformed payload with `4xx` and a stable reason
code (`schema_version_unsupported`, `published_at_invalid`, `list_too_long`,
`numeric_invalid`, …). **A rejected snapshot cannot overwrite the last valid one**:
a broken publisher degrades to visible staleness, never to plausible-looking wrong
state.

Valid snapshots are held in memory and persisted durably (SQLite `runtime_overlay`,
`kind = live_snapshot`, latest-only per instance). This is not an event journal and
not replay. Persistence deliberately does **not** reuse `_put_overlay`, which
honours the dry-run contextvar and skips writes — a simulated broker effect must
never make real observed telemetry vanish.

"Latest" is by the node's own `published_at`, **not** arrival order. A snapshot
strictly older than the one already held is ignored (logged, still `200`): a delayed
or retried publish must never discard state the node has already reported —
including a reconciliation **freeze**, which would otherwise let a halted system
read as resolved. Equal timestamps replace, so an idempotent retry is absorbed. The
hot cache and the durable store obey the same rule, so a reader can never see one
run backwards relative to the other. The rule is per instance.

`GET /api/live/status` returns the validated snapshot wrapped in an observation
envelope: `published_at` (node), `observed_at` (server), `received_at`,
`age_seconds`, `stale`, `stale_after_seconds`, `schema_version`,
`legacy_source`, plus the freshness diagnostics below. An absent node yields an
explicit `emptyState` string rather than zeros.

### Freshness: server-observed, phase-aware

Freshness is judged on **server-observed arrival** (`received_at`, recorded by the
ingest endpoint), not on the node's own `published_at`. A node therefore cannot
make itself appear fresh by publishing a manipulated timestamp — the only clock
that decides liveness is the tower's. Where no arrival time exists (the pull path
via `node_client`, or a record persisted before this was carried through),
freshness falls back to `published_at`, which is the previous behaviour.
`freshness_basis` states which was used.

**Liveness and data currency are separate facts, and both must hold.**

| Field | Meaning |
|---|---|
| `age_seconds` | Age of the **observation** (`published_at`). Unchanged meaning. |
| `liveness_age_seconds` | Age since **arrival** — how long the node has been quiet. |
| `data_stale` | The observations are old, however recently they arrived. |
| `liveness_stale` | The node has gone quiet beyond its budget. |
| `stale` | `liveness_stale OR data_stale` — the single boolean, unchanged in name and never more permissive than the facts behind it. |

A packet that arrives now but carries hour-old observations is **not** healthy;
a node recomputing for ten minutes without publishing is **not** dead.

### Known duplication — deferred, NOT addressed in M-TEL-1

`backend/connection_state.telemetry_state()` still re-implements telemetry
staleness independently, judged on `published_at` against a flat threshold. It is
therefore a second staleness authority that can disagree with `/api/live/status`
about whether the same node is stale — it does not benefit from the
server-observed basis or the phase-aware budget above.

Now that `/api/live/status` publishes authoritative `liveness_stale` /
`data_stale` verdicts, `connection_state` should eventually consume the canonical
verdict instead of maintaining its own. Recorded here so the divergence is known
rather than discovered later; deliberately out of scope for M-TEL-1, which
changes exactly one read path.

Clock skew is deliberately **not** a published field: it is `published_at` minus
`received_at`, both already in this envelope, and `clock_skew_seconds` already
names an unrelated concept (`arming.ArmPolicy`, permitted arm-request tolerance).
Skew nonetheless affects the verdict — a snapshot dated beyond the budget into the
future reads stale, never maximally fresh.

**Phase-aware thresholds** replace the former flat 120 s timeout, which marked a
healthy node stale roughly two minutes into a legitimate recompute:

| Cycle status | Budget | Why |
|---|---|---|
| `no_new_bar` (idle) | **120 s** (`DEFAULT_STALE_AFTER_S`, unchanged) | Between bars the node publishes at least every 60 s, so this is two missed publishes. |
| `ok`, `bootstrap`, frozen, absent/unknown | **900 s** (`RECOMPUTE_STALE_AFTER_S`) | One full 15 m bar interval — a recompute legitimately publishes nothing while it runs. |

The recompute budget is deliberately **not** generous. A measured warm recompute of
~1119 s exceeds one bar interval and is reported stale, because a node that cannot
finish within its own cadence has a genuine capacity problem the tower must keep
signalling. `RECOMPUTE_STALE_AFTER_S` mirrors `live/shadow_report.BAR_INTERVAL_S`,
which gates the `no_boundary_overrun` promotion check on the same threshold; the
two are duplicated to keep `live_telemetry` free of any `live.*` dependency, and an
anti-drift test asserts they stay equal. An unknown or absent cycle status takes the
larger budget: misreading a working node as dead is the defect being corrected, and
a genuine outage still trips that budget shortly afterwards. Nothing is augmented from the fixture world, and no package hash is
fabricated: the live-ingest narration events now carry `packageHash: null`, because
the node runs the Lux strategy core and the backend's active fixture package is
unrelated to it.

## Legacy compatibility

The pinned VPS dry-run node still publishes the pre-UI-2 flat payload and must not
be redeployed. `normalize_legacy` adapts it into the v1 envelope and marks it
`legacy_source: true`. Every field the old shape does not carry stays `null` — the
adapter invents no identity, health, arming, market or lineage.

Both ambiguity directions are rejected rather than guessed: a payload that
*declares* a `schema_version` but fails validation is not downgraded through the
adapter, and a payload that *omits* `schema_version` yet carries v1-only sections is
rejected as `schema_version_missing` rather than gutted (adapting it would silently
replace real `runtime` / `arming` / `market` / `risk` state with "unavailable"
nulls). Legacy detection additionally requires a key the old publisher really
emitted.

**Retirement condition:** delete `is_legacy_payload`, `normalize_legacy` and their
tests once the VPS node has been redeployed from a commit containing
`live/telemetry.py` and `/api/live/status` reports `legacy_source: false` for every
instance. The adapter has no other reason to exist.

## Other consumers of `publish_last.json`

`publish_last.json` is both the fail-soft fallback and source **S6** of the frozen
L1A Operational Status Model (`backend/ops_status.py` → `GET /api/ops/status`).
Changing the payload shape therefore changes that model's input. `ops_status.py`
normalizes a v1 snapshot back onto the flat S6 fact names at *collection* time; the
frozen projector itself is untouched and no field of the L1A output model is added,
renamed or removed. Facts the snapshot genuinely does not carry (e.g. the full
`skipped` list, which v1 reduces to a count) remain `"unknown"` rather than being
fabricated. `test_publisher_payload_still_feeds_the_frozen_ops_status_model` ties
the real publisher output to that consumer.

## Not yet wired

The frontend does not consume this contract yet (UI-3+). The transport has **no
authentication**. VPN, authentication and TLS are all required before a real VPS is
pointed at a Control Tower.
