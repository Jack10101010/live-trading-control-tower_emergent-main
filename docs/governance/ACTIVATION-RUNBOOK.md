# Activation runbook

*M-ACTIVATE-READINESS-1. The procedure for switching the Control Tower from
"honestly empty" to "showing real money". Read the whole thing before starting.*

Activation is not a deployment. Nothing is installed and no code changes; a node
that was already publishing starts publishing an **account observation**, and the
Control Tower starts admitting it. The risk is not that it fails — a failure is
loud and empty. The risk is that it **succeeds against the wrong account**, which
looks exactly like success.

> **This runbook does not deploy the VPS side.** The account-observation contract
> (M-NODE-ACCT-1) has not landed. Every step below is verified against synthetic
> payloads in `backend/tests/test_activation_e2e.py`, which mirrors this
> sequence step for step — a green run there means these expected states are the
> real ones. Steps that depend on unlanded VPS work are marked **⧗ BLOCKED**.

---

## 0 · Before you touch anything

| | check | how |
|---|---|---|
| 0.1 | Mac commit is the reviewed one | `git -C <repo> rev-parse HEAD` matches the approved milestone commit |
| 0.2 | Working tree clean | `git status --porcelain` prints nothing |
| 0.3 | Branch is `live-implementation-prep` | `git rev-parse --abbrev-ref HEAD` |
| 0.4 | VPS commit recorded | **⧗ BLOCKED** — record the VPS commit hash here once M-NODE-ACCT-1 lands. Mixed contracts are the single most likely cause of a confusing activation. |
| 0.5 | State directory backed up | `cp -a "$CONTROL_TOWER_STATE_DIR" "$CONTROL_TOWER_STATE_DIR.pre-activation"` |
| 0.6 | Current observations backed up | the durable snapshots live in `runtime.db` inside the state directory; the copy above covers them. **Do not delete anything before copying.** |

Write down, on paper or in the activation ticket, the account fingerprint you
**expect**. If you cannot state it before starting, you cannot detect the wrong
one afterwards, and step 9 will only be able to say WARN.

---

## 1 · Decide the frontend serving mode FIRST

The one ordering mistake that produces a genuinely confusing session is a new
backend serving an old bundle. `admitted` / `admissionReasons` are new fields; a
bundle built before this milestone ignores them, so a refused account would
render as a normal live account card.

| mode | when | command |
|---|---|---|
| Vite dev server | ordinary activation | `cd frontend && npm run dev` — always current, no build step |
| Built bundle | you are serving `dist` | `cd frontend && npm run build` **before** starting the backend |

**Do not overwrite `frontend/dist` casually.** If the current `dist` is the
rollback artefact, copy it aside first: `cp -a frontend/dist frontend/dist.rollback`.

---

## 2 · Stop the backend

Stop whatever is running on the API port — `Ctrl-C` in its terminal, or kill the
`uvicorn server:app` process. Do not `kill -9`; the overlay store commits per
operation, but a clean stop is free.

Confirm it is down: `curl -sf http://127.0.0.1:8000/api/health` must fail.

---

## 3 · Start the backend in development

```bash
cd backend
export CONTROL_TOWER_STATE_DIR="<the state directory you backed up>"
export CONTROL_TOWER_EXPECTED_NODE="vps-node-1"          # the instance you expect
export CONTROL_TOWER_EXPECTED_ACCOUNT="<the fingerprint you wrote down>"
export CONTROL_TOWER_EXPECTED_SERVER="<the broker server you expect>"
python3 -m uvicorn server:app --host 127.0.0.1 --port 8000
```

**Environment rules, all of them load-bearing:**

- `CONTROL_TOWER_ENVIRONMENT` **must not be `production`.** Production refuses
  the fixture, changes the boundary middleware and is not what this procedure
  was verified against. Leave it unset; the resolved value is printed at startup
  and checked by step 9.
- **Do not set `CONTROL_TOWER_BROKER_ADAPTER=mt5` on the Mac.** The MetaTrader5
  binding is Windows local-terminal IPC. The Mac cannot read a terminal, and
  M-MT5-READ-1 proved that setting it produced `provenance: live_mt5` with a
  null balance — a green frame from an environment variable.
- `MARKET_DATA_PROVIDER` likewise stays as it is.
- `CONTROL_TOWER_EXPECTED_ACCOUNT` / `_SERVER` are what make an account switch
  detectable. **Unset is not an error, but it is not safe either**: the runtime
  admits whatever the node reports and the checker reports WARN, never PASS.
- **Two variables name the expected account.** The execution side has always
  used `NODE_EXPECTED_ACCOUNT_FINGERPRINT`. `CONTROL_TOWER_EXPECTED_ACCOUNT`
  falls back to it when unset, so pinning either one pins both — but setting
  both to *different* values pins the deployment to two accounts, and the
  checker FAILs on it. Set one, or set both to the same value.
- When a pin is set, an observation carrying **no identity at all** is refused,
  not admitted. "Cannot be checked" is not "passed the check" — a node
  publishing balances with `identity.available: false` would otherwise deliver
  real money into a deployment that believes it is pinned.

---

## 4 · Verify `/api/health` before expecting anything

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool
```

Expected:

| field | expected | meaning |
|---|---|---|
| `environment` | `development` | not production |
| `brokerKind` | `mock` | the Mac has no terminal; this is correct |
| `backendMode` | `fixture` | **only means the dev asset is readable on disk.** Presence is not activation. |
| `liveNodeConnected` | `false` initially | no node has published yet in this process |
| `tradingReady` | `false` | derived from named gates; stays false |

---

## 5 · Wait for canonical node telemetry

The node publishes on its own cycle. Watch:

```bash
watch -n 5 'curl -s http://127.0.0.1:8000/api/live/status | python3 -m json.tool | head -40'
```

You are waiting for `instances` to contain your expected instance and the entry
to show `legacy_source: false` and `schema_version: ct.node-telemetry.v1`.

A `legacy_source: true` entry means the VPS is still on the pinned pre-UI-2
build. That is a valid state — the node is visible and honest — but **it can
never produce an account observation**, so activation stops here until the VPS
is updated. The checker reports this as `node.contract: WARN [legacy_payload]`.

---

## 6 · Verify the node-only state

Before any account arrives, confirm the tower is in the correct starting state.
This is the step people skip, and it is the one that proves nothing is being
invented.

```bash
curl -s http://127.0.0.1:8000/api/operations/nodes    | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/operations/accounts | python3 -m json.tool
```

| surface | expected |
|---|---|
| node `provenance` | `node-telemetry` |
| node `lifecycleState` | `current` |
| node `adapter`, `broker`, `connectionState`, `executionMode`, `accountFingerprintMasked` | **all `null`** — a node card must not carry the tower's own state |
| node `mt5Observation` | `null` — not sampling is the ordinary case, not a disconnection |
| accounts | no record with `provenance` in `live_mt5` / `node_mt5` |
| Fleet Overview | "1 node reporting · account source unavailable" |
| Accounts & Protection | "No authoritative account source" |

**A healthy node here is a success, not a partial failure.** The node samples its
account only on cycles containing an OPEN.

---

## 7 · Wait for the account observation ⧗ BLOCKED

**⧗ This step cannot be completed until M-NODE-ACCT-1 lands on the VPS.** See
`VPS-CONTRACT-M-NODE-ACCT-1.md` for what the Mac requires.

When it does, the account arrives inside the same telemetry envelope. Expect:

| surface | expected |
|---|---|
| account `provenance` | `node_mt5` |
| account `admitted` | `true` |
| account `nodeId` | your expected instance |
| account `margin`, `marginLevel`, `leverage`, `unrealizedPnL`, `realizedPnLToday`, `openRisk` | **`null`** — the node does not publish these, and `null` renders `—`, never `0` |
| node `provenance` | still `node-telemetry` — it must not gain broker provenance from its own account payload |
| node `mt5Observation` | `observed` |

---

## 8 · Run the activation checker

```bash
cd backend
CONTROL_TOWER_EXPECTED_NODE=vps-node-1 \
CONTROL_TOWER_EXPECTED_ACCOUNT=<fingerprint> \
CONTROL_TOWER_EXPECTED_SERVER=<server> \
python3 -m activation_check --base http://127.0.0.1:8000
```

It performs **GET requests only** and exits non-zero on FAIL. WARN is
informational and exits 0 — an unpinned expectation or an idle node is not a
safety failure, and exiting non-zero for it would train you to ignore the code.

Representative output from the offline suite:

```
  PASS  environment.reachable                observed=200
  PASS  environment.resolved                 observed=development  expected=development
  PASS  environment.adapter                  observed=mock
  WARN  environment.fixture_asset_reachable  observed=fixture  expected=runtime (asset absent)  [dev_asset_readable_presence_is_not_activation]
  PASS  node.expected_instance               observed=vps-node-1  expected=vps-node-1
  PASS  node.schema_version                  observed=ct.node-telemetry.v1
  PASS  node.contract                        observed=canonical
  PASS  node.freshness                       observed=age=0.014s basis=received_at  expected=<= 900.0s
  PASS  node.clock_skew                      observed=5.0s  expected=<= 120.0s
  PASS  node.cycle_status                    observed=ok
  PASS  node.mode                            observed=dry_run
  PASS  account.capability                   observed=observed  expected=observed
  PASS  account.admissible                   observed=True  expected=True
  PASS  account.no_mock_admitted             observed=1 genuine / 0 rejected  expected=no fixture figure on an admitted record
  PASS  account.identity                     observed=acctfp_0…cdef @ FTMO-Demo
  PASS  account.balance_valid                observed=4211.5
  PASS  account.equity_valid                 observed=4180.25
  PASS  account.reaches_ui                   observed=1
  PASS  ui.endpoints_available               observed=6
  PASS  ui.node_inventories_agree            observed=vps-node-1  expected=vps-node-1
  PASS  ui.no_fixture_provenance_admitted    observed=none  expected=none

  ACTIVATION VERDICT: WARN
```

That is the **complete** line set for a fully pinned, fully successful run —
copied from an actual execution against the offline suite, not abridged. The one
WARN is the development asset being readable on disk, which is true in every
checkout. An unpinned run adds two more WARNs (`node.expected_instance`,
`account.identity_pinned`) and is still exit 0.

### Any FAIL stops activation

| check | meaning | action |
|---|---|---|
| `account.refused` | **A genuine terminal read of an account you are not pinned to**, or one that carries no identity to compare against the pin. | STOP. Roll back. Do not continue. |
| `account.identity_pinned` FAIL | `CONTROL_TOWER_EXPECTED_ACCOUNT` and `NODE_EXPECTED_ACCOUNT_FINGERPRINT` name **different** accounts. | STOP. The deployment is pinned twice, to two things. |
| `environment.resolved` | the process is not in development | STOP. Restart with the correct environment. |
| `node.clock_skew` | node and tower clocks disagree beyond 120 s | STOP. Any freshness verdict is unreliable. |
| `ui.node_inventories_agree` | two endpoints disagree about which nodes exist | STOP. Contract mismatch. |
| `ui.no_fixture_provenance_admitted` · `account.no_mock_admitted` | the fixture world's invented figures (100 000 / 100 412) appeared on a record that passed the gate | STOP. Roll back immediately. |
| `account.reaches_ui` | the policy admits an account the projection does not show | STOP. The gate and the projection disagree. |

---

## 9 · Review the UI with your own eyes

The checker verifies facts. It cannot verify that the screen is legible. Open:

**Fleet Overview** — the node appears with its real identity, profile and engine
version. Warnings, if any, are the node's own words. No LIVE account frame
unless step 7 completed.

**Accounts & Protection** — one card per account, one per observing node. Every
unpublished figure reads `—`. If a red **Account observation refused** banner is
present, stop and read it: it names which check failed and why.

**Operational Dashboard** — the Warnings card. A
`contradictory_account_sources` warning means two genuine sources disagree; both
records are shown and **nothing has been averaged or preferred**. That is a
condition to investigate, not to click through.

**System View** — freshness basis is `received_at` everywhere. A broker identity
that is absent reads absent.

---

## 10 · Execution stays disabled

Nothing in this procedure changes execution. `execution_authority` is
**structurally** false in `activation_policy.ActivationVerdict` — it is not a
value the policy can compute, it is a constant. Account data never grants it, and
enabling execution is a separate, separately reviewed decision.

---

## Unresolved placeholders

| ref | blocked on |
|---|---|
| 0.4 | the VPS commit hash for M-NODE-ACCT-1 |
| 7 | the account-observation contract itself |
| capability marker | the contract carries no positive "I can report accounts" signal, so an idle new node and an old node are indistinguishable. Reported as `CAPABILITY_UNKNOWN` / WARN. See the VPS contract document. |
