# Runtime source boundary

*Established by M-WORLD-0. Read this before adding any data file to `backend/`.*

## The rule

**The runtime backend package contains code and configuration. Never authored
demonstration records.**

| | location | may ship to production |
|---|---|---|
| Runtime source | `backend/`, `live/` | yes |
| Authored fixture world | `_fixtures/world.v1.json` | **no** |
| Design documents | `_fixtures/brief.md`, `_fixtures/contracts.md` | no |

## The move

| | before | after |
|---|---|---|
| path | `backend/fixtures/world.v1.json` | `_fixtures/world.v1.json` |
| SHA-256 | `afb0636fd7ef78fe6a8fa9e8e503b62bdc93751251c675b6221f11c0719249bf` | *identical* |
| bytes | 30 921 | 30 921 |
| copies in repo | 2 (byte-identical, kept in sync by hand) | **1** |

Contents unchanged — no sentinel, id or timestamp was touched. This was an
architectural move, not fixture-data cleanup.

`ENGINEERING-HANDOFF.md` required three copies to stay byte-identical by hand
and warned that "a drift silently breaks reads or tests". Copies drift; one copy
cannot.

## Why it mattered

Not loading the fixture in production is a **runtime** property, enforced by
`environment.require_fixture_activation_allowed()`. Not *shipping* it is a
**structural** property. Only the second survives someone adding a new loader.

While `world.v1.json` sat inside `backend/`, any packaging step that copies the
package recursively — a Dockerfile, an rsync, a wheel with `package_data` —
would have carried 22 collections of authored accounts, brokers, trades,
packages, decisions and an operator identity into a production artefact.

## Path resolution

One default, one override, **no search list**.

```python
fixture_preview_service.fixture_asset_path()
# → $FIXTURE_PREVIEW_ASSET, else <repo>/_fixtures/world.v1.json
```

The default is derived from the module's own location, so it does not depend on
the working directory a process started in. `server.FIXTURE_SEARCH_PATHS` is
retained as a name but delegates.

It previously held three candidates: the backend copy plus two hardcoded
`/app/...` container paths that exist on no machine in this repository. **A
search list is how a temporary fallback becomes permanent** — nobody can tell
which entry answered, so no entry can safely be removed. There is deliberately
no fallback to the old location: a stale reference fails loudly in preview tests
rather than silently working for another year.

## Why `mock_broker_data.py` stays in `backend/`

It is a **test double**, not authored demonstration data:

- it is *code* — Python literals, no file, no I/O;
- every record it produces is stamped `mock-fixture` and rejected by every
  authority gate;
- the mock adapter must work with **no dev assets present at all**, which is
  precisely what makes the runtime fixture-independent.

Collapsing it with the fixture world is what created the original coupling.
M-MOCK-DECOUPLE-1 separated them, and that separation is why this move was
possible.

## How production stays fixture-impossible

Three independent mechanisms, none of which relies on the others:

1. **Structural** — the asset is outside every runtime package.
2. **Environment** — `POLICY_PRODUCTION.world_may_load = False`;
   `fixture_preview_service.get_world()` refuses before examining any path, so
   a physically present file is irrelevant.
3. **Boundary** — the derived fixture-surface middleware refuses every
   fixture-backed route.

## How tests opt in

```python
def test_something(fixture_preview):      # loads the real dev asset
def test_something(install_fixture):      # publishes a world you constructed
FIXTURE_PREVIEW_ASSET=/path/to/alt.json   # points at a different asset
```

An autouse conftest fixture resets the service around every test, so no test
inherits fixture state from another.

**Domain and unit tests must not load the authored world.** Importing `server`
performs zero fixture reads, ordinary endpoint tests perform zero, and MockBroker
tests perform zero — all asserted in `test_world_isolation.py` and
`test_runtime_source_boundary.py`.

## Measured

| | eager load (pre-ISOLATE) | now |
|---|---|---|
| fixture reads at import | 1 | **0** |
| import wall time | 1.854 s | **0.356 s** |
| import RSS | +89.8 MiB | **+53.4 MiB** |
| first ordinary request | — | 10.8 ms, +1.56 MiB, 0 loads |
| first *preview* load | — | 4.3 ms, +0.38 MiB |

## What a future M-PREVIEW-DELETE-1 would remove

Eighteen gated preview routes, of which **twelve still carry
operational-looking legacy paths**:

`/api/fleet` · `/api/trades` · `/api/packages` · `/api/packages/active` ·
`/api/recommendations` · `/api/decisions/{id}` · `/api/deployments` ·
`/api/deployments/{id}` · `/api/policy/{instrument}/matrix` ·
`/api/portfolio/status` · `/api/risk/assessment` ·
`/api/runtime/active-package` · `/api/commands/{name}` ·
`/api/strategy/decisions` · `/api/strategy/evaluate`

plus three explicitly named: `/api/dev/fixture-world` ·
`/api/dev/fixture-events` · `/api/dev/fixture-broker-health`

and two frontend views: `views/dev/FixtureFleetPreview.tsx` ·
`views/dev/FixtureTradesPreview.tsx`.

Those twelve legacy paths are the strongest remaining argument for that
milestone: an operational-looking URL is a standing invitation to wire something
to it. They are gated, refused without the asset, and have zero ordinary
consumers — so this is a naming and surface-area concern, not an honesty one.
