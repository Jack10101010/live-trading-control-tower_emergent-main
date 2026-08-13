# M-MOCK-DECOUPLE-1 — implementation brief

*Prepared by M-WORLD-ORDINARY-1. Ready to implement; nothing here is aspirational.*

This is the **last blocker** between the current tree and M-WORLD-0 (deleting
`world.v1.json` from the runtime source tree).

---

## The remaining coupling, exactly

`server._broker_context()` injects four callables into the context handed to
**every** adapter:

| callable | current source |
|---|---|
| `accounts()` | `_preview_world().get("accounts", [])` |
| `brokers()` | `_preview_world().get("brokers", [])` |
| `live_trades()` | `_preview_world().get("liveTrades", [])` + runtime overlay |
| `trade_current(id)` | the same list, overlaid |

Only `MockBroker` consumes them — `MT5Adapter` reads none, which is why
`fixture_surfaces.BOUNDARY_HELPERS` treats `_broker_context` as a boundary. The
consuming sites are `backend/broker.py` lines 172, 177, 191, 217, 224, 235, 251,
265, 328, 361, 415.

**Consequence today:** under the development-default mock adapter, a request to
`/api/operations/accounts` reaches those lambdas and lazily loads the whole
22-collection UI fixture. Pinned by
`backend/tests/test_world_isolation.py::test_3b_the_mock_broker_still_pulls_the_ui_fixture_lazily`,
which asserts the current behaviour and **fails when this milestone lands** —
the fix is to move its assertion into `test_3` and delete it.

Nothing dishonest reaches an operator either way: those records carry
`mock-fixture` provenance and every ordinary gate rejects them. The cost is
memory, startup coupling, and a fixture file the runtime cannot yet lose.

---

## The smallest self-contained dataset required

`MockBroker` needs far less than the UI fixture. From the consuming sites, the
minimum is:

```python
# backend/mock_broker_data.py  — NEW, ~60 lines, no file I/O, no JSON
MOCK_ACCOUNTS = (
    {"accountId": "mock_acct_1", "brokerId": "mock_brk_1",
     "baseCurrency": "USD", "balance": 100000.0, "equity": 100412.0},
)
MOCK_BROKERS = (
    {"brokerId": "mock_brk_1", "name": "Mock Broker", "connection": "Connected"},
)
MOCK_LIVE_TRADES = (
    # exactly two: one `managing` (open) and one closed. `test_live3_operations`
    # clones the open one, so the shape must keep `state`, `tradeId`,
    # `clientOrderId`, `brokerOrderId`, `symbol`, `side`, `size`, `entry`,
    # `sl`, `tp`.
)
```

Three notes that matter:

1. **Keep the same invented figures** (100000 / 100412). They are already the
   sentinels every honesty guard scans for; changing them would silently blind
   a dozen existing tests.
2. **Python literals, not a JSON file.** A second JSON file would recreate the
   load-at-import problem in a new place. These are test-double constants and
   belong in source.
3. **This is a TEST DOUBLE, not a UI preview.** Do not merge it with the fixture
   world, and do not let the dev-preview routes read it — that is the collapse
   `M-WORLD-ISOLATE-1` Phase 7 explicitly warned against.

---

## Implementation steps

1. Add `backend/mock_broker_data.py` with the constants above.
2. In `_broker_context()`, branch on the adapter kind: when `mock`, bind the four
   callables to `mock_broker_data`; otherwise bind them to `lambda: []`. The
   fixture accessor disappears from the context entirely.
3. Update `fixture_surfaces.BOUNDARY_HELPERS` — `_broker_context` no longer needs
   to be a boundary, because it no longer reads the fixture at all. Removing it
   makes the derived gate stricter.
4. Move `test_3b`'s assertion into `test_3` and delete `test_3b`.

## Tests that will need attention

| test | why |
|---|---|
| `test_live3_operations.py::test_different_entities_are_independent` | deep-copies the fixture world and clones its one open trade; retarget to `mock_broker_data` |
| `test_control_tower_api.py` (~line 772) | reads accounts from the preview world |
| `test_risk_limits_contract.py` | iterates fixture accounts to prove funded rules cannot influence the response |
| `test_architecture_hardening.py::test_gated_surfaces_still_serve_when_the_fixture_is_present` | asserts `/api/fleet` etc. still serve — unaffected, but re-run to confirm |

## What still reads the fixture afterwards

Only explicit previews:

- `/api/dev/fixture-world`, `/api/dev/fixture-events`, `/api/dev/fixture-broker-health`
- the legacy gated routes retained for preview compatibility: `/api/fleet`,
  `/api/trades`, `/api/packages`, `/api/packages/active`, `/api/recommendations`,
  `/api/decisions/{id}`, `/api/deployments`, `/api/policy/{i}/matrix`,
  `/api/portfolio/status`, `/api/risk/assessment`, `/api/runtime/active-package`,
  `/api/commands/{name}`
- `views/dev/FixtureFleetPreview.tsx` and `views/dev/FixtureTradesPreview.tsx`

## Does M-WORLD-0 then become safe?

**For the runtime source tree, yes** — no ordinary code path would read
`world.v1.json`, and the process would start, serve and pass its suite without
it (already proven by
`test_6_a_missing_fixture_does_not_break_ordinary_startup`).

**For deletion outright, no** — the twelve legacy preview routes and two dev
preview views would lose their data. M-WORLD-0 therefore has a choice to make
that this brief does not pre-empt:

- **(A)** delete the twelve legacy routes and their two preview views, then
  delete the fixture; or
- **(B)** move `world.v1.json` out of the backend package into a dev-only
  location and keep the previews.

Option A is the larger deletion and the cleaner end state. Either way, **after
M-MOCK-DECOUPLE-1 the fixture is a development asset only**, which is the
property M-WORLD-0 needs.
