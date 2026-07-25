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

## What this slice does not do

The frontend still issues no commands. Nothing here connects the Mac to the VPS,
adds authentication, VPN, TLS or WebSockets, or changes any node, execution,
arming, reconciliation or telemetry-contract behaviour.
