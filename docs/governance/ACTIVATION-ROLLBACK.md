# Activation rollback

*M-ACTIVATE-READINESS-1. What to do when activation goes wrong, decided in
advance so it is not decided at 11pm.*

The governing rule: **preserve the evidence before restoring the state.** A
rollback that deletes the payloads which caused the problem removes the only
record of what happened, and the next attempt starts from the same ignorance.

---

## Triggers — any one of these, stop and roll back

| # | trigger | how you notice |
|---|---|---|
| 1 | schema / version mismatch | `node.schema_version` FAIL, or ingest returning 400 repeatedly |
| 2 | **account identity or server mismatch, or an identity that cannot be checked** | `account.refused` FAIL; red banner on Accounts & Protection; "account observation REFUSED" on Fleet Overview |
| 3 | the fixture's invented figures on an admitted record | `account.no_mock_admitted` FAIL (`fixture_figures_admitted`); a `$100,000` / `$100,412` balance anywhere on an operational surface |
| 4 | the fixture world reached an operational surface | `ui.no_fixture_provenance_admitted` FAIL |
| 5 | freshness disagreement between surfaces | two endpoints reporting different staleness for one node |
| 6 | contradictory node/account truth | `contradictory_account_sources` in the Warnings card |
| 7 | a malformed payload was accepted | a node visible with fields that cannot be right |
| 8 | the UI claims LIVE without evidence | a green account frame with no `node_mt5` record behind it |
| 9 | backend/frontend contract mismatch | refused accounts rendering as normal cards; missing fields; console errors |
| 10 | repeated server errors | 5xx from any `/api/operations/*` endpoint |
| 11 | the two expected-account variables disagree | `account.identity_pinned` FAIL (`expected_account_pins_disagree`) |
| 12 | the runtime admitted an identity you did not pin | `account.pin_enforced_by_runtime` FAIL — the BACKEND is unpinned even though your shell is |
| 13 | the checker's clock disagrees with the tower's staleness | `node.freshness_independent` FAIL |

Triggers 2, 3, 4, 8, 11 and 12 are **immediate** — they mean the screen is asserting
something false about money. The rest permit a minute of diagnosis first.

---

## Procedure

> **These triggers are testable, and were not always.** Both fixture checks were
> originally written as predicates that could never be true — `PASS if not
> any(provenance == "mock-fixture" for a in genuine)` where `genuine` had
> already been filtered to exclude exactly that value. A document promising
> detection that cannot fire is worse than one promising nothing, so each check
> whose purpose is to fail now has a test that feeds it contamination and
> demands a FAIL (`test_the_checker_CAN_fail_its_anti_fixture_checks`).

### 1 · Preserve the evidence — BEFORE anything else

```bash
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p ~/activation-evidence/$STAMP
cp -a "$CONTROL_TOWER_STATE_DIR" ~/activation-evidence/$STAMP/state
curl -s http://127.0.0.1:8000/api/live/status           > ~/activation-evidence/$STAMP/live-status.json
curl -s http://127.0.0.1:8000/api/operations/nodes      > ~/activation-evidence/$STAMP/nodes.json
curl -s http://127.0.0.1:8000/api/operations/accounts   > ~/activation-evidence/$STAMP/accounts.json
curl -s http://127.0.0.1:8000/api/operations/summary    > ~/activation-evidence/$STAMP/summary.json
curl -s http://127.0.0.1:8000/api/health                > ~/activation-evidence/$STAMP/health.json
cd backend && python3 -m activation_check --json > ~/activation-evidence/$STAMP/checker.json
```

**Do not delete durable observations before copying them.** The snapshot table
in `runtime.db` holds the exact payload the tower received, which is the only
artefact that can settle "was the node wrong or was the tower wrong".

### 2 · Stop the backend

`Ctrl-C`, or kill the `uvicorn server:app` process. Confirm `/api/health` fails.

### 3 · Restore the previous commit

```bash
git -C <repo> status --porcelain      # must be clean; if not, stash — do not discard
git -C <repo> checkout <previous-known-good-commit>
```

Record which commit you came *from*. A rollback with no record of the failing
commit cannot be investigated.

### 4 · Restore the previous frontend bundle, if you serve `dist`

```bash
rm -rf frontend/dist && mv frontend/dist.rollback frontend/dist
```

If you were running the Vite dev server, nothing to do — a restart picks up the
restored source.

**Never leave a new bundle in front of an old backend, or the reverse.** Trigger
9 exists because that combination fails quietly: fields the bundle does not know
about are simply not rendered, so a refused account looks admitted.

### 5 · Restore the state directory

```bash
rm -rf "$CONTROL_TOWER_STATE_DIR"
cp -a "$CONTROL_TOWER_STATE_DIR.pre-activation" "$CONTROL_TOWER_STATE_DIR"
```

The evidence copy from step 1 is untouched by this.

### 6 · Restart the previous backend

Same command as the runbook's step 3, minus the `CONTROL_TOWER_EXPECTED_*`
variables if the previous commit predates them.

### 7 · Verify the prior contract is back

```bash
curl -s http://127.0.0.1:8000/api/health | python3 -m json.tool
curl -s http://127.0.0.1:8000/api/operations/accounts | python3 -m json.tool
```

Expected after rollback: `environment: development`, `brokerKind: mock`, and no
account with `live_mt5` / `node_mt5` provenance. The tower is back to honestly
empty, which is the safe resting state.

### 8 · Do NOT restart the VPS

The node is not the thing being rolled back. A VPS restart loses in-flight cycle
state and proves nothing about a Mac-side fault. It requires its own separately
justified decision.

---

## What rollback cannot undo

Nothing — and that is by construction, which is the point worth stating.

Activation writes **no orders, no positions, no fills and no execution records**.
`execution_authority` is a constant `False` in `ActivationVerdict`, not a
computed value. The only durable artefacts an activation produces are received
telemetry snapshots, and those are evidence rather than state. There is no
financial action to reverse.

---

## Mechanically tested

The restore path is exercised on throwaway state by
`backend/tests/test_activation_e2e.py::test_step_12_13_removing_the_observation_returns_to_unavailable`,
which removes the observation from **both** the in-memory cache and the durable
snapshot table and asserts the surfaces return to unavailable — specifically
that they do **not** fall back to the mock record. That is the rollback
end-state, asserted rather than assumed.

Note the distinction that test encodes: a node that merely **stops publishing**
keeps its last snapshot and goes visibly **stale**. Stale genuine data is still
genuine. Only **removal** returns a surface to unavailable.
