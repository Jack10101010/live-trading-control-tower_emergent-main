# Canonical implementation map

*The single current statement of what this Control Tower knows, where each fact
comes from, and what it is still waiting for. Established by
M-RELEASE-CHECKPOINT-1 at commit `d64536d`.*

**Supersedes** the four recovery-era documents now in
[`../history/`](../history/README.md), none of which describes the current
system.

**Scope:** the production Control Tower branch `live-implementation-prep` only.
Model B research is a separate branch and appears nowhere below.

---

## How to read the status column

| marker | meaning |
|---|---|
| **ACTIVE** | implemented, and producing real data today |
| **WAITING** | implemented and tested, producing nothing until the VPS supplies input |
| **UNAVAILABLE BY DESIGN** | the honest answer is "no source exists"; a number here would be invented |
| **PREVIEW ONLY** | development fixture, reachable only at `/api/dev/fixture-*` |
| **DEFERRED** | deliberately not built; named so it is not mistaken for an oversight |

Every row's claims are asserted by the tests in the last column. Where a claim
is supported only by this document, it says so.

---

## 1 · Environment and adapter selection

| | |
|---|---|
| **Environment boundary** | **ACTIVE** · `backend/environment.py` · resolved value on `/api/health.environment` · production policy is `broker_adapters={mt5}`, `market_data_providers={mt5}`, `world_may_load=False`, `replay_permitted=False` · owner **M-ENV-1** (`c32bae9`, `e0bd058`) · `test_environment_boundary.py` |
| **Broker adapter selection** | **ACTIVE** · `backend/broker_layer.py`, `CONTROL_TOWER_BROKER_ADAPTER` · currently `mock` · production cannot select `mock` — it is not in the production policy set · owner **M-ENV-1** · `test_environment_boundary.py` |
| **Market-data provider** | **ACTIVE** · `MARKET_DATA_PROVIDER` · production admits `mt5` only; `fixture`, `mock_live` and `replay` are refused · owner **M-ENV-1** · `test_environment_boundary.py` |
| **Capabilities** | **ACTIVE** · `backend/capability.py` → `/api/capabilities` · reports what this build can do, derived, never hardcoded · owner **M-FLAGS-1** (`66b6272`) · `frontend/src/lib/__tests__/capability.test.ts` |

**The Mac must not set `CONTROL_TOWER_BROKER_ADAPTER=mt5`.** The MetaTrader5
binding is Windows local-terminal IPC; M-MT5-READ-1 proved that setting it
produced `provenance: live_mt5` with a null balance.

---

## 2 · Node telemetry — the only live external input

| aspect | current state |
|---|---|
| producer | the Windows VPS execution node, `live/telemetry.py` |
| transport | `POST /api/live/ingest`, schema `ct.node-telemetry.v1` (the only accepted version) |
| validation | `backend/live_telemetry.py::validate_snapshot` — malformed and unknown-version bodies are refused **400** and cannot overwrite the last good snapshot |
| legacy | `is_legacy_payload` requires the ABSENCE of every v1-only section, so a broken v1 body is refused rather than adapted |
| persistence | `_LIVE_STATUS` (memory) **and** `runtime_overlay` kind `live_snapshot` (durable); reads merge memory over store |
| provenance | `node-telemetry`, from `node_provenance.for_node_observation(validated=True)` |
| freshness authority | `activation_policy.recomputed_stale` — the envelope flag ORed with a recomputation on the tower's **arrival** clock, budget clamped to the tighter of the phase budget and 120 s |
| admission gate | `node_provenance.is_authoritative_node_observation` — a set disjoint from the broker set |
| ordinary UI | Fleet Overview, SystemView, ScopeNavigator, LiveOperationsStrip |
| status | **ACTIVE** — a node publishing today is visible today |
| owner | **M-TEL-1** (`8a839d0`), **M-NODE-TEL-1** (`a124bcc`), **M-NODE-READ-1** (`7f6fe77`) |
| tests | `test_node_read_contract.py`, `test_telemetry_freshness.py`, `test_activation_surfaces.py` |

Budgets: **120 s** idle (`no_new_bar`), **900 s** recompute. The node supplies the
cycle status that selects the budget, so `recomputed_stale` clamps it — a node
cannot buy itself a larger window.

---

## 3 · Account observation — the activation target

| | local MT5 | node-relayed MT5 |
|---|---|---|
| producer | `backend/broker.py::MT5Broker.account_snapshot` | VPS `safe_identity` / `safe_health` |
| reaches the Mac via | in-process adapter | `account.identity` + `account.health` in the telemetry envelope |
| projection | `operational_projection._local_accounts` | `operational_projection._node_accounts` |
| provenance | `live_mt5`, only when the sample carries a field | `node_mt5`, only on `available is True` |
| identity pin | `_local_admission` | `activation_policy.account_admissible` |
| status | **UNAVAILABLE BY DESIGN on this Mac** — no local terminal exists | **WAITING** on VPS M-NODE-ACCT-1 |
| owner | **M-MT5-READ-1** (`aab1bbc`) | **M-ACTIVATE-READINESS-1/2** (`b335b54`, `d64536d`) |

**Admission is not provenance.** Provenance answers *who observed this*;
admission answers *is this the account this deployment is pinned to*. A genuine
reading of the wrong account keeps `node_mt5` and carries `admitted: false` with
a named reason, and is shown as **refused** rather than hidden.

**Reconciliation between the two** is `activation_policy.reconcile_account_sources`,
called from `build_accounts`. Precedence is by authority, not locality. Two
genuine sources that disagree are both kept and
`activation_policy.account_contradictions` raises the disagreement into
`/api/operations/summary.warnings`.

---

## 4 · Operational projections

| surface | status | notes |
|---|---|---|
| `/api/operations/nodes` | **ACTIVE** | node cards carry **no** tower state — `adapter`, `broker`, `connectionState`, `executionMode`, `reconciliationState` are all null by design |
| `/api/operations/accounts` | **WAITING** | carries refused records with `admitted` + `admissionReasons`; the UI gate drops them |
| `/api/operations/summary` | **ACTIVE** | the same records plus tower-level `warnings`, including contradictions |
| `/api/live/status`, `/api/live/connection` | **ACTIVE** | receipt-side truth |
| `/api/health` | **ACTIVE** | `backendMode: fixture` means the dev asset is READABLE ON DISK — presence is not activation |
| `/api/live-runtime` | **ACTIVE but dormant** | fed only by this process's supervisor; provenance `absent` today. **Outside the identity pin** — see §9 |

Owner **M-FLEET-1/2** (`235bcc8`, `f11ef3b`), **M-NODE-READ-1**.
Tests `test_activation_surfaces.py`, `test_operational_projection.py`.

---

## 5 · Ledger, analytics and the durable stores

| domain | status | authority |
|---|---|---|
| Ledger **initiation** origin | **ACTIVE** | recorded per intent; `backend/execution_store.py` |
| Ledger **execution** origin | **ACTIVE** | persisted explicitly — an entry states which adapter produced the fill · **M-LEDGER-ORIGIN-1** (`4044918`) |
| Trade analytics | **ACTIVE, admitting nothing today** | `/api/ledger/analytics` admits **MT5 execution origin only**; mock-executed entries are excluded, so the honest answer under the mock adapter is empty · **M-TRADES-2** (`bec9415`) |
| Trades / orders / positions on operator surfaces | **UNAVAILABLE BY DESIGN** | `durable-store` names where a record is KEPT, not where it came from · **M-TRADES-1** (`0a4d92b`) |
| Recommendations | **ACTIVE** | durable operator store; no fixture path into it, audited · **M-REC-1** (`3b3b8d3`) |
| Events | **ACTIVE** | the mixed-origin stream was split: genuine BotEvents vs `/api/dev/fixture-events` · **M-EVENTS-1** (`82bf533`) |
| Packages / policy matrices | **UNAVAILABLE BY DESIGN** | there is no package registry. It was never built; deriving a version from configuration would assert a deployed strategy build · **M-PKG-1** (`521b7f6`) |
| Market state / component telemetry | **UNAVAILABLE BY DESIGN** | fixture removed, no genuine source · **M-NODE-TEL-1** |
| System confidence | **DELETED** | a fabricated composite · **M-CONF-1** (`1267ffa`) |
| Edge / performance statistics | **DELETED** | **M-EDGE-1** (`f2d2544`) |
| Funded-account rules | **UNAVAILABLE BY DESIGN** | severed from the risk path · **M-RISK-1** (`17902ab`) |

---

## 6 · The fixture world

| | |
|---|---|
| asset | `_fixtures/world.v1.json` — **outside every runtime package** (M-WORLD-0, `91e5c5d`) |
| loader | `backend/fixture_preview_service.py` — lazy; zero reads at import, zero on an ordinary request |
| routes | **eight**, all `/api/dev/fixture-*`, each naming itself a fixture (M-PREVIEW-DELETE-1, `70a0bf1`, `e7cae12`) |
| production | refused three independent ways: structural (outside the package), environment (`world_may_load=False`), boundary (derived middleware) |
| MockBroker | `backend/mock_broker_data.py` — Python literals, **no fixture read at all** (M-MOCK-DECOUPLE-1, `4d654bb`) |
| ordinary UI | reads **zero** fixture endpoints (M-WORLD-ORDINARY-1, `d02b496`) |

The eight preview routes, derived not hand-listed:
`/api/dev/fixture-world`, `-fleet`, `-trades`, `-packages`, `-packages/active`,
`-recommendations`, `-events`, `-broker-health`.

---

## 7 · Authority that is never granted

| authority | status |
|---|---|
| Execution authority | **DEFERRED and structurally unreachable from activation.** `ActivationVerdict.execution_authority` is a literal `False` at both construction sites. Execution is governed by `execution_safety` / `command_authorization`, which `activation_policy` does not import |
| Reconciliation authority | **ACTIVE, untouched by activation.** `backend/reconciliation*.py` |

---

## 8 · The activation checker

`backend/activation_check.py` — `python -m activation_check`. GET only, one
network call site, redirects refused, identifiers masked, node strings clipped.
23 checks; exit 1 on any FAIL. **WAITING** on the same VPS input as §3.

Owner **M-ACTIVATE-READINESS-1/2**. Tests `test_activation_checker_controls.py`
(31 negative controls — every conditional check driven to failure).

---

## 9 · Known boundaries

Named so a future milestone knows where to look. None is reachable by
node-relayed data, which is what activation introduces.

1. **`/api/live-runtime` / `LiveRuntimePanel`** — `BrokerRuntimeView` carries
   balance and equity and has no `admitted` field. Fed only by this process's
   supervisor, never by node telemetry.
2. **Positions and orders** — carry provenance but no admission.
3. **The contradiction warning's audience** — it renders in the Operational
   Dashboard's Warnings card; Accounts & Protection and Fleet Overview show the
   disagreeing balances but not the warning.
4. **The phase budget is node-influenced** by design; clamped, not removed.

---

## 10 · The one external prerequisite

Everything marked **WAITING** waits on exactly one thing: the VPS populating
`account.identity` and `account.health` on more than OPEN cycles. See
[`VPS-CONTRACT-M-NODE-ACCT-1.md`](VPS-CONTRACT-M-NODE-ACCT-1.md) and
[`activation-compatibility.json`](activation-compatibility.json).

No new schema version, no new endpoint, and no field the Mac does not already
consume.
