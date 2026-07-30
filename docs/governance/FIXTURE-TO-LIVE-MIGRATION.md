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
| 2 | System Confidence panel + shell chip | fixture | RED | `/api/system-confidence` → WORLD | remove (or rebuild from real signals much later) | none — removable today | Low | **High** | **M-CONF-1** | ☐ |
| 3 | Fleet deployment tiles / brokers / accounts | fixture | RED | `/api/fleet` → WORLD | `/api/operations/{nodes,accounts,positions}` | node telemetry published | Med | High | M-FLEET-1 | ☐ |
| 4 | Pair trades / ghost trades / pending orders / blocked intents | fixture | RED | WORLD.liveTrades etc. | `/api/ledger/*` + `/api/operations/{orders,positions}` | real broker history (demo MT5) | Med | High | M-TRADES-1 | ☐ |
| 5 | Analytics (performance, equity curve) | fixture-derived | RED | computed from WORLD trades | recompute from ledger closes | #4 | Low | High | M-TRADES-2 | ☐ |
| 6 | Operational Dashboard | adapter-fed | DYNAMIC | `/api/operations/*` (mock adapter) | same endpoints, real adapter | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 7 | Live Runtime panel | adapter-fed | DYNAMIC | `/api/live-runtime` (mock adapter) | same, real adapter | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 8 | Trade Ledger panel | adapter-fed durable | DYNAMIC | `/api/ledger/*` (mock-ingested) | same, real broker history | demo MT5 connection | Low | Med | M-MT5-READ-1 | ☐ |
| 9 | Charts (market-data + pair) + derived overlays | provider-dep. | DYNAMIC | market_data engine (provider=fixture) | provider=mt5 or store/polygon | MT5/store feed wired as default | Med | Med | M-FEED-1 | ☐ |
| 10 | Market State snapshot cards | fixture | RED | WORLD.marketStateSnapshots | node-published market state | node telemetry | Med | Med | M-NODE-TEL-1 | ☐ |
| 11 | Broker Health view | fixture | RED | WORLD.brokerHealth | real MT5 health telemetry | demo MT5 + health surface | Med | High | M-MT5-READ-2 | ☐ |
| 12 | Edge Monitor (4 panels + pair edge tabs) | fixture | RED | `/api/edge-monitor` → WORLD | research pipeline export (much later) | none — gate off today | Low | High | M-GATE-1 | ☐ |
| 13 | Recommendations / drafts / decision chains (fixture) | fixture | RED | WORLD | durable trade-recommendation store (exists) | consumer migration | Med | Med | M-REC-1 | ☐ |
| 14 | Packages / versioning / comparisons / policy panels | fixture | RED | WORLD.packages | real package registry (later) | package model | High | Med | M-PKG-1 | ☐ |
| 15 | Events feed (journal) | mixed | RED | WORLD.events ⊕ events.db | split: runtime events only (fixture seeds dropped) | split work only | Low | Med | M-EVENTS-1 | ☐ |
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
- **UI backed by genuine operational data today:** ≈20% of families (5/25);
  with a demo MT5 connection (M-MT5-READ-1 + M-FEED-1) the four DYNAMIC
  families flip automatically → ≈36% with zero further UI work.

## Milestone order (risk-first)

M-RISK-1 → M-CONF-1 → M-MT5-READ-1 (flips #6–8) → M-FEED-1 (flips #9) →
M-FLEET-1 → M-TRADES-1/2 → M-NODE-TEL-1/2 → M-EVENTS-1 → M-REC-1 →
M-FLAGS-1 → M-GATE-1 → M-PKG-1 → (deferred: replay).

Definition of done for the migration: every row ☑, zero RED borders rendered,
and the temporary border system itself retired (final milestone).
