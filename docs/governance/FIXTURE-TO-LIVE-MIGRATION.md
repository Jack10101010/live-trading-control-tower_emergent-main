# Fixture → Live Migration Scoreboard

**Purpose:** the canonical roadmap for retiring fixture/demo data from the
Control Tower. Update the Status column as milestones land; the summary totals
should trend from predominantly RED to entirely GREEN. Border classifications
are enforced by `frontend/src/lib/cardProvenance.ts` + the required `Panel`
provenance prop (see `CONTROL-TOWER-DATA-DEPENDENCY-AUDIT.md`).

Legend: colour = current border · Difficulty/Risk = Low/Med/High ·
DYNAMIC = flips green automatically when its real source activates.

## Scoreboard

| # | Panel / data source | Provenance | Colour | Backend source | Live replacement | Blockers | Diff | Risk | Target milestone | Done |
|---|---|---|---|---|---|---|---|---|---|---|
| 1 | Risk limits (accounts) | honest-unconfigured | RED (accounts still fixture) | `/api/risk/limits` — **fixture severed; returns honest unconfigured contract; fixture-immune (tested)** | config-sourced limits + node account telemetry (future) | real account config model (future GREEN) | Med | **Resolved** (no fabricated numbers) | **M-RISK-1** | ☑ |
| 2 | System Confidence (shell gauge, context chips, fleet rail) | **removed** | n/a (no card) | `/api/system-confidence` — **honest `computed:false`; fixture fiction severed (tested)** | future model from genuine telemetry only | a real confidence model (deliberately not built) | Low | **Resolved** (no fabricated certainty) | **M-CONF-1** | ☑ |
| 3 | Fleet deployment tiles / brokers / accounts | **removed from operator UI** | n/a (no fixture card) | `/api/operations/{nodes,accounts}` filtered to `live_mt5` | same, real MT5 adapter | demo MT5 connection | Med | **Resolved** (no fixture records) | **M-FLEET-2** | ☑ |
| 4 | Pair trades / ghost trades / pending orders / blocked intents | **removed from operator UI** | n/a (no fixture card) | `/api/operations/{orders,positions}` filtered to `live_mt5` | same, real MT5 adapter | demo MT5 + ledger ORIGIN contract | Med | **Resolved** (no fixture records) | **M-TRADES-1** | ☑ |
| 5 | Analytics (performance, equity curve) | fixture-derived | RED | computed from WORLD trades | recompute from ledger closes | #4 | Low | High | M-TRADES-2 | ☐ |
| 6 | Operational Dashboard | adapter-fed | DYNAMIC | `/api/operations/*` (mock adapter) | same endpoints, real adapter | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 7 | Live Runtime panel | adapter-fed | DYNAMIC | `/api/live-runtime` (mock adapter) | same, real adapter | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 8 | Trade Ledger panel | adapter-fed durable | DYNAMIC | `/api/ledger/*` (mock-ingested) | same, real broker history | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 9 | Charts (market-data + pair) + derived overlays | provider-dep. | DYNAMIC | market_data engine (provider=fixture) | provider=mt5 or store/polygon | MT5/store feed wired as default | Med | Med | M-FEED-1 | ☐ |
| 10 | Market State snapshot cards | fixture | RED | WORLD.marketStateSnapshots | node-published market state | node telemetry | Med | Med | M-NODE-TEL-1 | ☐ |
| 11 | Broker Health view | fixture | RED | WORLD.brokerHealth | real MT5 health telemetry | demo MT5 + health surface | Med | High | M-MT5-READ-2 | ☐ |
| 12 | Edge Monitor (main view + pair edge tab + Pair Health expectancy/win-rate) | **removed** | RED (honest placeholder surfaces) | `/api/edge-monitor` — **honest `computed:false`; WORLD.edgeMonitor severed, fixture-immune (tested)** | ledger + broker-history derived metrics (M-TRADES-2 and beyond) | no performance model exists; needs real trade history | Low | **Resolved** (no fabricated track record) | **M-EDGE-1** | ☑ |
| 13 | Recommendations / drafts / decision chains (fixture) | **migrated to durable store** | n/a | `/api/trade-recommendations*` | — | — | Med | **Resolved** (no fixture recs) | **M-REC-1** | ☑ |
| 14 | Packages / versioning / comparisons / policy panels | **removed from operator UI** | n/a (unavailable states) | none — no registry exists | a real package registry (unbuilt) | the registry itself | High | **Resolved** (no fabricated versions) | **M-PKG-1** | ☑ |
| 15 | Events feed (journal) | **runtime only** | n/a (no fixture rows) | `events.db` (runtime) | — | — | Low | **Resolved** (no merged stream) | **M-EVENTS-1** | ☑ |
| 16 | Feed Health card | mixed | RED | runtime health + placeholders | split: provider row GREEN / unwired rows placeholder | split work only | Low | Low | M-EVENTS-1 | ☐ |
| 17 | Feature flags (Settings + System cards + gates) | constants | RED | hardcoded dict `server.py:4510` | env/deployment-manifest config | config mechanism | Low | Low | M-FLAGS-1 | ☐ |
| 18 | Replay panels | replay | RED | WORLD.replaySessions | real replay engine output | replay engine | High | Low | (deferred) | ☐ |
| 19 | Placeholders (latency histogram, snapshot integrity, tick/news rows) | placeholder | RED | none | node reconciliation + feed telemetry | node telemetry | Med | Low | M-NODE-TEL-2 | ☐ |
| 20 | Engine Components / Deployments & Manifests (System) | fixture | RED | WORLD | node-published manifest | node telemetry | Med | Med | M-NODE-TEL-1 | ☐ |
| 21 | Settings (profile/appearance/notifications/shortcuts) | runtime-config | **GREEN** | `/api/operator/preferences` | — | — | — | — | done | ☑ |
| 22 | Storage & Feeds (System) | live | **GREEN** | `/runtime/health` | — | — | — | — | done | ☑ |
| 23 | Security Baseline | live | **GREEN** | `/api/security/config` | — | — | — | — | done | ☑ |
| 24 | Recommendations (durable operator store) | live | **GREEN** | `/api/trade-recommendations*` | — | — | — | — | done | ☑ |
| 25 | Scenario store panels | live | **GREEN** | `/api/scenarios*` | — | — | — | — | done | ☑ |

## Summary totals (as of this milestone)

- **Total classified panels/sources:** 25 families (≈70 rendered cards)
- **GREEN (live/runtime-config):** 5 families
- **DYNAMIC (green when real source activates):** 4 families (#6–9)
- **RED fixture:** 11 · **RED mixed:** 2 · **RED placeholder:** 2 · **RED constants:** 1 · **RED replay:** 1
- **M-RISK-1 complete:** the highest-risk fabricated numbers (funded risk rules) can no longer render anywhere.
- **M-CONF-1 complete:** the fabricated confidence score/band/signals are gone from the header, context bar and fleet rail; the endpoint reports `computed:false`.
- **M-ENV-1 complete** (supersedes the planned M-MODE-1; see
  `MOCK-DATA-ERADICATION-PLAN.md` §11): the two fail-open production defaults are
  closed. `CONTROL_TOWER_ENVIRONMENT` (`development` when unset, `production` only when
  explicit) declares which broker adapters and market-data providers are *admissible*;
  it is an admissibility policy, not a behaviour selector, and adds no mode enum —
  `CONTROL_TOWER_MODE`, `execution_mode` and `connection_policy` keep their existing
  meanings untouched. In production the fixture world is never loaded, so the rows below
  that still read WORLD answer `501 fixture_world_unavailable` instead of fabricated
  data; each is still retired by its own milestone. Development behaviour is unchanged,
  so no row's colour changes here.
- **M-REC-1 complete:** the first genuine MIGRATION rather than a removal. The decisive
  audit finding was that `recommendation_store.py` contains no WORLD read of any kind —
  durable and fixture recommendations have never been able to mix — so the durable store
  could be admitted where `/api/ledger/*` could not. `useRecommendations` now reads
  `/api/trade-recommendations`; the Edge Monitor pipeline is durable-backed with distinct
  empty/unavailable states. Authored evidence (p-values, sample sizes, NATIVE badges) was
  NOT carried across: the durable record has its own contract and a field the old card
  expected is not a reason to invent one. Drafts and decision chains report unavailable —
  no durable draft store exists, and turning recommendations into "drafts" would invent a
  lifecycle state the system does not implement. Also closed a gap M-PKG-1 left:
  `usePolicyMatrix` still fetched `/api/policy/{i}/matrix`, whose 144 cells derive from
  `WORLD["packages"]`; it no longer fetches at all.
- **M-PKG-1 complete:** every operator-facing package, version, hash, promotion date,
  policy matrix and package comparison is gone. This milestone found no source to switch
  to: **there is no package registry module anywhere in the backend** — it was never
  built — so `/api/packages`, `/api/packages/active` and `/api/policy/{i}/matrix` all
  read `WORLD["packages"]`. Every version number an operator has ever seen was written
  by hand. It therefore resolves to unavailable, deliberately: synthesising a version
  from configuration would assert that a specific strategy build is deployed and
  governing decisions, which nothing here can support — and it would look *derived*
  rather than authored, making it more dangerous than the fixture. PolicyEngineView and
  the two Versioning views became unavailable states; StrategyPackages, the System
  packages panel and the pair package panels likewise; `PackageVersionChip` renders "no
  package registry" instead of a version. Fixture packages remain at `/dev/fixture-fleet`.
- **M-EVENTS-1 complete:** the last mixed-origin stream is gone. `/api/events` merged
  `WORLD["events"]` with `events.db` into one seq-ordered collection, and the merge was
  invisible — the three seeds carry no marker, so an operator reading the audit trail
  could not tell which entries described things that actually happened. Unlike the fleet
  and trade domains this was a SPLIT, not a removal: `events.db` is genuinely
  authoritative (every row records a command this process dispatched). Runtime events
  stay; fixture seeds move to `/api/dev/fixture-events`, labelled. The delta channel
  (`/events/live`) no longer emits fixture rows, the runtime event count no longer
  includes them, and `_FIXTURE_MAX_SEQ` was DELETED — it anchored runtime seq allocation
  and the stream head to a development file, so the numbering of real events depended on
  the fixture. An empty runtime store now yields an empty stream instead of three
  invented events.
- **M-TRADES-1 complete:** fixture live trades, ghost trades and blocked intents no
  longer render on ANY ordinary operator route. `useTrades` (nine consumers, including
  the chart overlay and the analytics engine) is gone; `useOperationalTrades` reads
  positions and orders through the M-FLEET-2 provenance gate. The laundering risk was
  confirmed live: `/api/operations/positions` returns a `mock-fixture` position complete
  with `entryPrice` and `currentPrice`, and it is rejected. `/api/ledger/*` reports
  `provenance:"durable-store"` — a STORAGE class, not an origin — so ledger history is
  reported unavailable. **Missing contract:** the ledger must state which adapter
  produced each fill before its history can be shown. Positions and orders are kept as
  separate collections. The chart no longer plots invented entry/SL/TP markers on real
  price data. Analytics refuses to derive any figure from an inadmissible input, so an
  empty rejected list can never become a zero-performance report (M-TRADES-2 unstarted).
  Fixture trades survive only at the unlinked `/dev/fixture-trades`.
- **M-FLEET-2 complete:** fixture Fleet/Broker/Account records no longer render on ANY
  ordinary operator route. The decisive finding was that `/api/operations/*` is not
  automatically authoritative — under the development-default mock adapter it stamps
  `mock-fixture` and carries the same invented $100,000 balance, so switching endpoints
  would have laundered fixture data into an operational-looking surface. Instead a single
  gate (`frontend/src/lib/operationalProvenance.ts`) admits only `live_mt5` and fails
  closed on unknown provenance; all 13 consumers were rewired through it. Pair navigation
  now derives from `/api/instruments` (configured `RUNTIME_SYMBOLS`), which asserts
  nothing operational. Fixture fleet data survives only at the unlinked development route
  `/dev/fixture-fleet`, enforced by repository-wide source guards. Under the mock adapter
  the fleet UI is intentionally empty. Rows 3 and 20 are resolved for fixture content;
  they turn GREEN when a real MT5 adapter reports (M-MT5-READ-1).
- **M-FLEET-1 complete:** Fleet Overview and Accounts & Protection no longer present
  fixture records as operational truth. `/api/fleet` now states its own provenance
  (`schemaVersion:1`, `provenance:"fixture"`, `source:"development_fixture"`, plus a
  detail naming `/api/operations/{accounts,nodes}` as the real projections). Every
  fixture deployment and account carries a visible FIXTURE tag and can never be painted
  in the live colour; the page banners "Fixture data — not live". Two genuine invented
  values were removed from Accounts & Protection: `Math.min(...[], 100)` reported a 100%
  drawdown buffer for an account with no deployments, and `[].reduce(..., 0)` reported
  "$0.00" daily P/L for an unknown P/L — both now render "—". Three distinct states are
  no longer conflated: no source, genuine empty, and populated. Rows 3 and 4 stay RED
  because the records remain fixture-sourced; what changed is that they can no longer be
  mistaken for live. Wiring them to `/api/operations/*` is M-MT5-READ-1 / M-TRADES-1.
- **M-EDGE-1 complete:** the fabricated track record (expectancy 0.39R, win rate 33.5%, edge drift, policy health, research/ghost-vs-live) is gone from the Edge Monitor view, the pair edge tab and the Pair Health card; `/api/edge-monitor` reports `computed:false`. The pair tab remains present as an honest unavailable surface; feature-flag behaviour is unchanged. AnalyticsView and trade-derived analytics stay OUT of scope (M-TRADES-2).
- **UI backed by genuine operational data today:** ≈20% of families (5/25);
  with a demo MT5 connection (M-MT5-READ-1 + M-FEED-1) the four DYNAMIC
  families flip automatically → ≈36% with zero further UI work.

## Milestone order (risk-first)

M-RISK-1 → M-CONF-1 → M-EDGE-1 → M-TEL-1 → **M-ENV-1** (boundary; prerequisite for
everything below) → M-MT5-READ-1 (flips #6–8) → M-FEED-1 (flips #9) →
M-FLEET-1 → M-TRADES-1/2 → M-NODE-TEL-1/2 → M-EVENTS-1 → M-REC-1 →
M-FLAGS-1 → M-GATE-1 → M-PKG-1 → (deferred: replay).

Definition of done for the migration: every row ☑, zero RED borders rendered,
and the temporary border system itself retired (final milestone).
