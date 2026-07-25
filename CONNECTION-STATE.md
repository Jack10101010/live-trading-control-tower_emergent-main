# Truthful Connection State (UI-1)

Read-only observability. No control affordance, no VPS connectivity, no
authentication, no WebSockets, no live order submission.

## Why there is no "connected" light

A Control Tower being reachable, an execution node publishing, and MT5 being
reachable are **three different facts**. Collapsing them into one indicator makes
the most dangerous failure invisible: a green light that actually means "the web
server answered".

UI-1 therefore reports four independent dimensions, each with its own evidence
source. `GET /api/live/status` no longer carries a `connected` boolean — it was
derived from "a snapshot exists at all" and ignored freshness entirely, so a node
that died days ago still read as connected.

## The four dimensions

| Dimension | States | Evidence source |
| --- | --- | --- |
| **Backend** | `starting` · `available` · `unavailable` | The **client's** own ability to reach the API. A backend cannot report its own unreachability, so this is the one dimension derived in the frontend. |
| **Telemetry** | `never_received` · `fresh` · `stale` · `unavailable` | The stored snapshot and its `published_at`. |
| **Execution node** | `unknown` · `connected` · `disconnected` | Telemetry freshness, plus the node's own L1A liveness beacon when that source is readable. |
| **MT5 bridge** | `unknown` · `healthy` · `degraded` · `unavailable` | MT5-derived facts the **node itself** reported in its snapshot. The Control Tower never contacts MT5. |

Backend `available` never implies node `connected`. Node `connected` never
implies bridge `healthy`. Each row above is answered separately.

## Backend truth sources

Everything traces to one of three things — a node-published fact, the absence of
one, or the reader's own failure to obtain one. Nothing is inferred from the
fixture world.

- **Snapshot store** (`runtime_overlay`, `kind = live_snapshot`, UI-2): the latest
  validated snapshot per instance, keyed by the node's `published_at`.
- **Node liveness beacon** (L1A `process.aliveness`, via `backend/ops_status.py`):
  the only evidence that can make the node `disconnected` rather than `unknown`.
  In the target topology the node runs on a separate machine and its files are not
  visible to the tower, so this is normally absent — which is exactly why its
  absence must never be read as absence of the node.
- **Snapshot contents**: `market.available` / `market.feed_healthy` and
  `reconciliation.snapshot_status` / `reconciliation.frozen`, all node-observed.

## Freshness semantics

Age is computed **only** from the node's own `published_at`. `received_at` — the
tower's own clock — is displayed but never used to compute age, because a delayed
snapshot that arrives "just now" is still old news.

- `fresh` — age ≤ threshold (inclusive at the boundary)
- `stale` — age > threshold
- Threshold: `live_telemetry.DEFAULT_STALE_AFTER_S` (120 s), imported by the
  connection module rather than redeclared, so freshness has exactly one
  definition.

Timestamps are never estimated. An age that cannot be computed renders as
`unknown`, never as "just now".

## `unknown` vs `unavailable`

This distinction is the point of the whole module.

- **`unknown`** — no evidence either way. Absence of a reading is not a reading.
  A stale snapshot makes the node `unknown`, **not** dead: the node keeps trading
  and protecting the account with the Control Tower offline (invariant **I-10**),
  so silence is genuinely ambiguous. Likewise the node samples MT5 only on cycles
  containing an OPEN, so "not sampled" is the common case and must not read as a
  fault.
- **`unavailable`** — positive evidence that something could not be read or
  reached: the node reported `snapshot_status: unavailable` (it tried to read
  broker positions and could not), or the stored snapshot fails validation.

## Stale semantics

`stale` is a statement about the **observation**, never about the system. Past the
threshold the tower stops claiming anything is current; it does not start claiming
something is broken.

Consequently a stale snapshot **discards** its bridge reading: a healthy feed
observed six hours ago is not a current observation, so the bridge returns to
`unknown` rather than displaying the last known good value.

## Error states, visually distinguished

| Condition | Surface |
| --- | --- |
| Backend unreachable | Backend `unavailable`; all other dimensions collapse to `unknown` with "could not be reached", plus an explicit note that this does not mean the node stopped. |
| Telemetry never received | Explicit first-run block. Never "healthy", "connected" or "online". |
| Snapshot fails validation | Telemetry `unavailable` + `malformed_snapshot`. |
| No `schema_version` declared | Telemetry `unavailable` + `unknown_schema`. |
| Version the tower does not support | Telemetry `unavailable` + `unsupported_schema`. |

Each problem carries its own label and remediation sentence, because they demand
different operator responses.

## Multiple instances

The model is multi-instance by construction: the API returns a list, no instance's
state can influence another's, and fleet roll-ups take the **worst** state across
instances, never the best — one silent node must not hide behind a healthy one.
The UI renders instances worst-first. UI-1 adds no instance *management*.

## API

`GET /api/live/connection` — the model above. Deliberately omits a `backend`
field; a backend asserting its own availability in its own payload would be
vacuous.

# Live Operations Strip (UI-3)

CONNECTION truth (above) answers *can I observe the node?* OPERATIONS truth
answers *what did the node say about its own safety?* They are deliberately
separate surfaces and must stay that way.

| Surface | Purpose |
| --- | --- |
| Connection panel (System view) | Detailed transport/observability breakdown — four dimensions with per-instance evidence. |
| **Operations strip** (persistent, every screen) | Compact safety-critical node state: mode, submission, kill switch, reconciliation, account, arming, OPEN, unresolved records. |

## Purpose

One dense caution panel that answers, at a glance: is the backend reachable, is
telemetry fresh, is the node observable, is the bridge healthy, what mode is
running, is submission disabled, is the kill switch engaged, is reconciliation
clean, is the account healthy, is the node armed, is OPEN blocked, are there
unresolved execution records, and when was the last valid snapshot published.

**Read-only by construction.** No button, no toggle, no mutation callback, no
command route. Asserted by tests, not merely intended.

## Truth sources

Only two endpoints, both pre-existing: `GET /api/live/connection` (UI-1) and
`GET /api/live/status` (UI-2). **No backend change was required** — every field
the strip needs was already in the persisted snapshot.

Fixture-world state is never a substitute for node state. The derivation module
imports nothing from the fixture world, and a guard test asserts it.

## Severity precedence

Four levels, deterministic, worst-wins. A healthy field can never lower the
reported severity.

| Level | Triggers |
| --- | --- |
| **critical** | kill switch engaged · reconciliation frozen · recovery required · account explicitly unhealthy · trade not allowed · expert trading off · unresolved SENT records · bridge unavailable *while telemetry is fresh* · node disconnected *while telemetry is fresh* · armed-account fingerprint mismatch |
| **warning** | telemetry stale · submission disabled · unarmed / expired / exhausted / disarmed · bridge degraded · OPEN explicitly blocked · recent execution block · unreadable telemetry |
| **unknown** | no telemetry ever · backend unavailable · node unknown · bridge unknown · required field unavailable · legacy snapshot lacking evidence |
| **healthy** | every contributing safety dimension has affirmative, *fresh* evidence |

Bridge-unavailable and node-disconnected escalate to critical **only** while
telemetry is fresh. Without current evidence they are merely unknown — an
unreachable Control Tower must never be presented as a node fault.

### Two chips carry no verdict

`mode` and `OPEN: not evaluated` are excluded from the aggregate:

- **mode** is context. Neither `dry_run` nor `live` is good or bad by itself: a
  dry-run node with clean evidence is healthy, and live mode alone never is.
- **OPEN "not evaluated"** is the permanent, correct state — UI-1 established that
  telemetry can never authorize an OPEN. Treating it as an unknown verdict would
  pin every instance at `unknown` forever and make `healthy` unreachable, which
  would destroy the scale's meaning. An OPEN that is explicitly **blocked** does
  contribute.

## Stale-data behaviour

Past the freshness threshold nothing describes the present:

- one **explicit stale banner** in the strip, not a subtle per-chip colour shift
- every operational chip degrades to `unknown` and is marked historical
- the last observed value survives only as labelled history (`armed (stale)`)
- the snapshot timestamp and age stay prominent, in absolute **and** relative form
- the overall severity can never be healthy

## Unknown versus unhealthy

`unknown` means no evidence; `unhealthy` means evidence of a problem. Neither is
ever rendered as healthy, and they are never merged. Account health that was not
sampled is `unknown`; account health the node evaluated and rejected is
`critical`.

## Multiple instances

No instance selector existed, so the strip adds a minimal local one — a native
`<select>` **view filter**, not an action control. Fleet severity always reflects
the **worst** instance regardless of the filter, so a critical instance can never
be hidden by selecting a healthy one. Instances render worst-first. UI-3 adds no
instance registry, creation, deletion or management.

## Reason codes

The node's own vocabulary is preserved verbatim beside readable text. Unrecognised
codes render raw at warning severity — never dropped, never given a more
optimistic explanation than the code supports. Codes are deduplicated and ordered
worst-severity first, preserving source order within a band.

## Accessibility

Text-first: every chip renders label + value, severity is stated as a word
(`CRITICAL`), and a non-colour glyph (■ ▲ · ●) carries severity in greyscale. No
animation and no pulsing anywhere — including the two pre-existing pulses UI-3
removed. Details use a native `<details>`/`<summary>`, so disclosure is keyboard
operable and screen-reader announced without custom ARIA. Timestamps appear in
both absolute and relative form. Tooltips only repeat text already on screen.

## Controls intentionally excluded

No kill-switch, arm/disarm, submission, runtime-mode, CLOSE/MODIFY or order
control. No deployment or instance management. The node stays autonomous; the
Control Tower observes.

## Existing indicators corrected by UI-3

| Indicator | Was | Now |
| --- | --- | --- |
| Execution-mode banner | Pulsing `LIVE` from **fixture** deployments | `plan mock/demo/live`, scope-labelled, no pulse |
| Realtime chip | `live` (meant: browser long-poll works) | `stream ok` / `stream offline`, no pulse |
| Confidence chips | `Reconcile`, `Broker` — same words as node truth | `fx Reconcile`, `fx Broker` |
| Deployment status dot | Fixture `Armed`, colliding with Slice-8 arming | scope-labelled as a fixture record |

## What this slice does not do

The frontend still issues no commands. Nothing here connects the Mac to the VPS,
adds authentication, VPN, TLS or WebSockets, or changes any node, execution,
arming, reconciliation or telemetry-contract behaviour.
