# Activation checklist

*One page. The detail lives in the documents referenced from each line — this
exists so the ORDER cannot be improvised at 11pm.*

Established by M-RELEASE-CHECKPOINT-1. Supersedes any sequencing implied
elsewhere; where this and another document disagree about ORDER, this wins.

---

## The five things that are easy to get wrong

1. **Export `CONTROL_TOWER_EXPECTED_ACCOUNT` and `_SERVER` in the shell that
   STARTS THE BACKEND.** The gate runs inside the backend process and reads its
   environment at startup. Variables set only in the checker's shell constrain
   nothing — the tower will admit whatever arrives while the tool reports a
   mild WARN. This was found by rehearsal, not by reasoning.
2. **`CONTROL_TOWER_ENVIRONMENT` must not be `production`.** Leave it unset.
3. **Never set `CONTROL_TOWER_BROKER_ADAPTER=mt5` on the Mac.** The MetaTrader5
   binding is Windows local-terminal IPC; setting it once produced a green LIVE
   account with a null balance.
4. **If you serve `frontend/dist`, rebuild it and back up the old one.** A new
   backend behind an old bundle fails quietly: fields the bundle does not know
   about are simply not rendered, so a REFUSED account looks admitted.
5. **Verify the node-only state before any account arrives.** It is the step
   people skip and the one that proves nothing is being invented.

---

## Before

- [ ] Mac commit is the reviewed one · `git rev-parse HEAD`
- [ ] Working tree clean · `git status --porcelain` prints nothing
- [ ] **VPS commit recorded** — from the M-NODE-ACCT-1 final report
- [ ] `./validate-activation-candidate.sh` → **VERDICT: PASS**
- [ ] State directory backed up · `cp -a "$CONTROL_TOWER_STATE_DIR" "$CONTROL_TOWER_STATE_DIR.pre-activation"`
- [ ] `frontend/dist` copied aside if you serve it
- [ ] The expected account fingerprint **written down** — you cannot detect the
      wrong one afterwards if you cannot state the right one first

## Start

- [ ] Stop the backend; confirm `/api/health` fails
- [ ] Rebuild the frontend **or** run the Vite dev server — decide before starting
- [ ] Start the backend **with** `CONTROL_TOWER_EXPECTED_NODE`,
      `CONTROL_TOWER_EXPECTED_ACCOUNT`, `CONTROL_TOWER_EXPECTED_SERVER`
- [ ] `/api/health` → `environment: development`, `brokerKind: mock`,
      `tradingReady: false`

## Node before account

- [ ] `/api/live/status` shows the expected instance, `legacy_source: false`,
      `schema_version: ct.node-telemetry.v1`
- [ ] `/api/operations/nodes` → `provenance: node-telemetry`,
      `lifecycleState: current`, and `adapter` / `broker` / `connectionState` /
      `executionMode` **all null**
- [ ] `/api/operations/accounts` → **no** `live_mt5` or `node_mt5` record
- [ ] Fleet Overview reads "1 node reporting · account source unavailable"

**A healthy node with no account here is success, not a partial failure.**

## Account

- [ ] Account arrives · `provenance: node_mt5`, `admitted: true`
- [ ] `margin`, `marginLevel`, `leverage`, `unrealizedPnL`, `realizedPnLToday`,
      `openRisk` are **null** and render `—`
- [ ] The node card still says `node-telemetry` — it must not gain broker
      provenance from its own account payload

## Verify

- [ ] `python3 -m activation_check` → exit 0
- [ ] `account.pin_enforced_by_runtime` **PASS** — this is what proves the
      backend, not your shell, is pinned
- [ ] `node.freshness_independent` **PASS**
- [ ] Any **FAIL** → stop and roll back. No exceptions, and in particular
      `account.refused` means a real terminal read of an account that is not
      yours

## Eyes

- [ ] Accounts & Protection — one card per observing node; every unpublished
      figure reads `—`; no red refusal banner
- [ ] Operational Dashboard — Warnings card carries no
      `contradictory_account_sources`
- [ ] Fleet Overview — no "account observation REFUSED"
- [ ] Execution remains disabled. It is a constant `False` in the activation
      verdict; nothing here can change that

## If anything is wrong

- [ ] **Preserve evidence first** — `ACTIVATION-ROLLBACK.md` §1. The received
      payloads are the only artefact that can settle whether the node or the
      tower was wrong
- [ ] Then roll back code, bundle and state — in that order
- [ ] Do **not** restart the VPS. It is not the thing being rolled back

---

## Documents

| for | read |
|---|---|
| what the gate decides and why | [`ACTIVATION-GATE.md`](ACTIVATION-GATE.md) |
| the full step-by-step procedure | [`ACTIVATION-RUNBOOK.md`](ACTIVATION-RUNBOOK.md) |
| triggers and the restore path | [`ACTIVATION-ROLLBACK.md`](ACTIVATION-ROLLBACK.md) |
| what the VPS must send | [`VPS-CONTRACT-M-NODE-ACCT-1.md`](VPS-CONTRACT-M-NODE-ACCT-1.md) |
| what blocks activation, derived from code | [`ACTIVATION-DEPENDENCY-GATE.md`](ACTIVATION-DEPENDENCY-GATE.md) |
| the machine-readable contract | [`activation-compatibility.json`](activation-compatibility.json) |
| what exists and what is waiting | [`CANONICAL-IMPLEMENTATION-MAP.md`](CANONICAL-IMPLEMENTATION-MAP.md) |

**No credential, account number or host identifier appears in any of them.**
