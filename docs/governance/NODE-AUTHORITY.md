# Authority classes in the Control Tower

*Established by M-NODE-READ-1. Read this before adding any field to a node card,
an account card, or a provenance gate.*

## The four authorities

They are independent. Each answers a different question, from different
evidence, and none of them implies any other.

| Authority | Answers | Evidence | Seam |
|---|---|---|---|
| **NODE OPERATIONAL** | Is a node running, and what is it doing? | A validated `ct.node-telemetry.v1` snapshot | `backend/node_provenance.py`, `frontend/src/lib/nodeProvenance.ts` |
| **BROKER ACCOUNT** | What does the broker say about the account? | An observed MT5 read — locally (`live_mt5`) or relayed by a node (`node_mt5`) | `backend/broker_provenance.py`, `frontend/src/lib/operationalProvenance.ts` |
| **EXECUTION** | May this tower dispatch an order right now? | Governed execution mode, authorization grant, financial rails | `execution_safety`, `command_authorization`, `execution_context` |
| **RECONCILIATION** | Does local state match broker state? | The node's own reconciliation report | `live/executor.reconcile` on the VPS; `reconciliation.py` for tower-side posture |

### The implications that do not hold

- A node publishing perfectly says **nothing** about whether its MT5 terminal is
  readable. `account.health.available: false` is the ordinary case for a healthy
  node — it samples the account only on cycles containing an OPEN.
- An account observation says **nothing** about node health. A node can relay a
  good account reading and then go silent, or report a frozen cycle while its
  terminal reads fine.
- Neither grants execution safety. Node presence is not authorization.
- `broker_provenance.is_broker_truth("node-telemetry")` is `False` and must stay
  `False`. `node_provenance.is_authoritative_node_observation` rejects every
  broker origin. Tests assert both directions.

## Why the gates are separate rather than one wider gate

Admitting `node-telemetry` into `AUTHORITATIVE_PROVENANCE` would have been one
line. That set also guards balances, equity, positions, orders and analytics
admission — so a heartbeat would have implied an account. The two questions get
two functions, with deliberately dissimilar names so they cannot be swapped by
autocomplete:

```
isAuthoritative(record)                    → may this be shown as BROKER truth?
isAuthoritativeNodeObservation(record)     → may this be shown as NODE truth?
```

## What a node card may and may not carry

**May** — identity, deployment profile, lifecycle, cycle status and boundary,
engine identity, publication/freshness decomposition, node-reported degradation,
and an MT5 connection observation *when the node explicitly published one*.

**May not** — balance, equity, margin, account number, realised or floating P&L,
pending-order counts, risk utilisation, drawdown buffer, funded status, or
trading posture. All of those require broker authority.

### The mis-attribution this rule exists to prevent

Before M-NODE-READ-1, seven of the nine fields on a node view were the **Control
Tower describing itself** under the node's name:

| Field | Actual source |
|---|---|
| `adapter` | the Mac's `broker_layer.active_kind()` |
| `broker` | the Mac broker snapshot's provenance |
| `connectionState` | the Mac adapter's `.connection().state` |
| `reconciliationState` | the Mac execution store's posture |
| `executionMode` | the Mac's governed execution mode |
| `authorizationSummary` | the Mac's authorization grant |
| `activeScenarioCount` | the Mac's scenario store |
| `accountFingerprintMasked` | fell back to the Mac broker snapshot's account — under the mock adapter, the **fixture** account, masked |

All stamped `node-telemetry`, on a card titled with the VPS node's instance id.
Those fields are now `None` on a node view. The facts are real and operators need
them, so the tower-level ones are raised at the **summary** level instead
(`build_summary`), where they are correctly attributed.

## Freshness has exactly one authority

`live_telemetry.observation()` (M-TEL-1) is the only thing that decides whether a
snapshot is stale. It judges on **`received_at`** — the tower's arrival clock —
so a node cannot make itself look fresh by publishing a manipulated timestamp,
and its budget is **phase-aware**: 120 s for an idle node (`cycle.status ==
"no_new_bar"`), one bar interval (900 s) otherwise, because a warm recompute
legitimately publishes nothing while it runs.

`connection_state.instance_view` used to compute its own verdict — `now -
published_at` against a flat 120 s. The two genuinely disagreed: during any
recompute, `/api/live/connection` reported the node stale and unknown while
`/api/live/status` reported the same snapshot fresh. **M-NODE-READ-1 removed
that second authority**; `instance_view` now calls `observation()` directly, so
the two surfaces return literally the same verdict.

This was safe to change here because `build_connection_state` has exactly one
consumer — `GET /api/live/connection`, which is display-only. The execution
safety path builds `NodeFacts` from `_live_status_entry`, the canonical envelope,
and never touched this module.

**Rule for future work:** nothing may compute staleness from timestamps. Consume
`observation()`, or consume a field that came from it. `telemetry_state(age,
budget)` is retained only for callers that hold an age and nothing else; do not
add new ones.

## Node lifecycle states

`absent` · `current` · `stale` · `degraded`

Decided in `node_provenance.classify_lifecycle` from the node's own reported
conditions. **`degraded` outranks `stale`**: a node that reported a freeze and
then went quiet still reported a freeze, and showing only "stale" would replace a
specific, actionable failure with a vague one.

Degradation is never inferred. Every reason names a field the node published:
`cycle.status` in `{error, frozen_pending_recovery}`, `runtime.kill_switch_active`,
`execution.cycle_frozen`, `reconciliation.frozen`, or
`reconciliation.snapshot_status == "unavailable"`.

## Deferred contract gaps

### 1. `stopped` is not observable — do not synthesise it

`ct.node-telemetry.v1` carries no lifecycle "stopped" signal. A node that halts
simply stops publishing, which is **indistinguishable** from a node that cannot
reach this Control Tower — and invariant I-10 says the node keeps trading and
protecting the account while the tower is offline. Silence is therefore
ambiguous, and deriving `stopped` from staleness would assert a halt on the
evidence of a network problem.

Closing this properly requires the node to publish an explicit lifecycle
shutdown record, or the tower to consume the node's L1A liveness beacon over the
wire. Both are node-side contract changes. **Until then there are four states,
not five, and `NODE_STOPPED` deliberately does not exist in the code.**

### 2. `deployment_id` / `execution_node_id` are always null

`live/telemetry.build_snapshot` sets both to `None` pending UI-4. There is no
authoritative deployment concept, which is why no surface renders deployment
cards. Do not manufacture one from node identity.

### 3. Account observation requires a VPS change

Tracked in `VPS-PROMPT-M-NODE-ACCT-1.md`. The node samples account state only on
cycles containing an OPEN and only in `live` mode for identity, so under
`dry_run` it publishes no account observation at all. **M-MT5-READ-1 is not
complete** — its receive path is built and tested, but no genuine account has
ever reached it.

## Precedence: real data must never be shadowed by fixture data

`build_accounts` originally consulted the execution nodes only when the local
adapter observed *nothing*. Under the mock adapter the local adapter observes
plenty — the fixture world's $100,000 account — so it won precedence and the node
was never asked. A node relaying genuine MT5 truth would have been shadowed by a
fixture account, and since the frontend gate rejects the fixture record, the
operator would see "unavailable": **real data suppressed by invented data.**

Precedence is now by **authority, not locality**: a local read wins only if it is
genuine broker truth (`broker_provenance.is_broker_truth`). Anything else defers
to the node.
