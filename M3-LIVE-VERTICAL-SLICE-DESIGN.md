# M3 — Smallest Production Vertical Slice: One Live Golden Instance

**Scope:** one frozen Golden-compatible EURUSD strategy instance: closed MT5 bars in →
deterministic decisions → safely placed + reconciled orders → status in the Control Tower.
**Behavioural authority:** `strategy_core` at Lux commit `d978074a` (engine_version `5bb6372c…`,
Golden anchor `c5e29837…` @ tag `golden-run-001`; 8/8 byte-identical parity gates).
**Design date:** 2026-07-17. Audit-first; no code has been written for this milestone.

---

## PART 1 — INFRASTRUCTURE AUDIT (what exists today)

### 1.1 Control Tower backend (`live-trading-control-tower_emergent-main/backend/`)
| Module | State | Verdict for the slice |
|---|---|---|
| `broker.py` (523) | `Broker` ABC + full **MockBroker** + **MT5Adapter SKELETON** (never connects; every op no-op; symbol translator stubs; registry `_ACTIVE="mock"`) | The seam to complete. Interface is right; body is empty. |
| `broker_sync.py` (301) | Structured reconciliation (`reconcile(broker_snap, runtime_snap)` → findings w/ severity), fault injection | REUSE as-is for live reconciliation reporting. |
| `execution.py` (321) | Command pipeline: validators (`v_broker_connected`, capability checks) → `Can*` policies → ExecutionResult; dry-run endpoint | REUSE as the operator-command path (pause/resume/close/flatten). |
| `market_data.py` (673) | Fixture/Replay/MockLive providers + synth candles + MarketSnapshot | Display-path only. Not decision data. |
| `data_service.py` (484) | `MarketDataProviderPort` + **HistoricalStore** (M1 bars from `MARKET_DATA_DIR`, bisect ranges, TF aggregation, gap detection) + PolygonAdapter | REUSE HistoricalStore as the CT read side of the canonical bar dir. |
| `risk_engine.py`, `portfolio.py` | Limits/assessments over runtime views | REUSE read-only in v1 (advisory display). |
| `scheduler.py` | Tick/queue/trigger (CT-side evaluation cadence) | Not the live clock (runner owns its clock). |
| `strategy.py` | Read-only `StrategyEngine` = policy-alignment placeholder — **NOT the Lux strategy** | Stays display-only. The live runner replaces its role for this instance. |
| `server.py` (1,739) | FastAPI `/api`: broker/*, execution/*, strategy/*, scheduler/*, market-data/*, runtime/health, world; sqlite `events.db`/`runtime.db` | REUSE as the publication surface. |
| Frontend | Full operator UI (orders, trades, inspector, decision chain, Deployment Manifest, flags) | REUSE unchanged — it renders whatever the backend serves. |

### 1.2 FX-OB-Research-Lab (`live-tower/`)
`app/marketdata/`: `contracts.py` (canonical `Bar`/`Tick`/`News`, `Timeframe`, UTC enforcement),
`feeds/` (`MarketDataFeed` protocol: connect/subscribe/get_history/normalize_symbol; `FileFeed`,
`ReplayFeed`, `MockFeed` — **no MT5 feed exists**), `storage/bar_store.py` (`BarStore`, monthly
parquet/json partitions, tested), JSON Schemas `md.bar.v1` / `md.tick.v1` / `md.news.v1`.
Verdict: the canonical bar contract + store to write MT5 bars into.

### 1.3 Lux-OB-Backtester
Frozen `strategy_core` (11 modules + `BOUNDARY.md` 12-name surface), the impure seams
(`src.execution.prepare_news_cache`, `src.portfolio_policy.load_policy`), the parity harness,
and the research driver. `sidecar/server.py` = research bundle server (no MT5). **No real MT5
code exists anywhere in the estate** — the CT skeleton is the only artifact.

### 1.4 Gap list (what does not exist)
1. Real MT5 connectivity (the `MetaTrader5` python package requires a Windows MT5 terminal).
2. A bar-ingestion bridge MT5 → canonical BarStore/M1 dir.
3. A live-runner daemon that invokes the frozen core on closed bars.
4. A decision→order-intent diff layer and an order executor with idempotency + reconciliation.
5. A publisher wiring runner state into the CT backend.
6. Live news-calendar ingestion (research uses a static CSV through 2026-06).
7. Resolution of two operator decisions (§5).

---

## PART 2 — DESIGN

### 2.0 The one architectural commitment: recompute-on-close
The frozen core is a batch engine with unbounded memory (CMR cumsum, RMA-200 ATR, live-OB set,
multi-position path dependence — proven in BOUNDARY-ANALYSIS.md). Any incremental re-implementation
would forfeit Golden compatibility. Therefore the live instance **re-runs the full deterministic
pipeline on every closed 15m bar** over the entire history (2015 → now), exactly as the parity
gates do, and derives actions from the *diff* between consecutive runs at the frontier.

Cost: the full pipeline is ~6–7 min on the operator Mac (54s OB detection + ~5 min walks) against
a 15-minute cadence → comfortable margin, and only the Triggered-Edge pass is needed live (the
baseline pass is research-only), cutting ~90s. Correctness: **by construction identical** to the
Golden engine — no new strategy code exists to be wrong. Optimisation is explicitly deferred.

### 2.1 Components (4 new, all thin)

**A. `mt5-bridge` (runs on a Windows VPS next to the MT5 terminal)**
- Loop: on each M1 close, `mt5.copy_rates_from_pos(symbol, TIMEFRAME_M1, ...)` for closed bars
  only; normalise to `md.bar.v1` (UTC, canonical symbol); append to the canonical M1 store
  (BarStore partitions + a flat `EURUSD_1m_live.csv` segment for the runner); heartbeat file.
- Startup backfill: pull maximum available M1 history to close the gap from the frozen dataset's
  end (2026-06-19) to now; record vendor provenance.
- Implements the existing `MarketDataFeed` protocol (`MT5Feed`) so ReplayFeed/FileFeed remain
  interchangeable for rehearsal.
- Transport to the runner machine: the store directory synced (or the runner also runs on the
  VPS — deployment choice, §5.3).

**B. `live-runner` (one process, one instance: EURUSD | Golden Research Profile)**
On each detected closed 15m boundary (from the M1 store, never wall-clock guessing):
1. Assemble candles = frozen Golden M1 dataset + bridge segment (seam audited once, §5.2).
2. Run the frozen pipeline via the documented `strategy_core` BOUNDARY surface +
   `src.execution.prepare_news_cache` — identical config values as `d6cdae…json` (target pass
   only), `portfolio_include_disabled_cohorts` per the §5.1 decision.
3. Diff trades output N vs N−1 restricted to the frontier bar: new pending entries, fills,
   cancellations, exits, stop moves (BE/move-stop), news-flatten directives.
4. Emit an ordered list of **OrderIntents** `{intent_id = hash(trade_id, transition), action,
   side, entry, stop, target, reason}` — deterministic and idempotent by construction.
5. Persist runner state (last bar, last trades frame hash, open-intent ledger) — crash-safe
   resume = re-run + re-diff; idempotency keys make replays harmless.

**C. `order-executor` (completes the existing `MT5Adapter`)**
- Modes: `dry_run` (log + publish only — the paper phase), `live` (real MT5 order ops).
- Reconcile-before-act: every cycle pulls MT5 positions/orders, maps by `intent_id` (stored in
  MT5 order comment/magic), computes expected-vs-actual via the existing `broker_sync.reconcile`,
  and only then applies new intents. Unknown positions → freeze + alert, never auto-close.
- Safety rails (hard, config-frozen): max 1 instance, symbol whitelist = EURUSD, max open
  positions cap (Golden multi-position observed max), fixed risk-per-trade, daily loss
  kill-switch, global kill file honoured before any order op, market-closed/requote/partial-fill
  handling logged as reconciliation findings.
- Fill prices WILL differ from modelled fills (spread/slippage were modelled at 0.2/0.2 pips).
  The executor records model-vs-actual divergence per trade; it never feeds back into decisions.

**D. `ct-publisher` (runner-side, ~trivial)**
- Pushes to the existing CT backend: runner heartbeat + engine identity (engine_version, config
  hash, policy sha) → `/runtime/health` section; frontier decisions + intent ledger + reconcile
  findings → events.db-backed endpoints; positions/orders via the broker routes once the
  MT5Adapter is live. CT flags: `broker=mt5` replaces `mock` for this deployment only.
- The CT remains **observation + operator commands**; it never computes trading truth (its
  placeholder StrategyEngine stays display-only). Operator commands (pause/flatten/kill) flow
  CT → execution.py pipeline → executor's command inbox.

### 2.2 What is deliberately NOT in the slice
Multi-symbol, position sizing beyond fixed risk, incremental engine, live news feed (frozen CSV
+ manual refresh procedure in v1 — news blackout list must be refreshed weekly, an operational
runbook item), Polygon cross-checks, CT write-path expansion, journal/event-spine rework,
Live Candidate Profile creation.

### 2.3 Phase order (each phase gated)
1. **P0 Rehearsal:** runner + executor in `dry_run` against ReplayFeed over the frozen dataset —
   must reproduce the Golden trades frame byte-identically at every frontier (the existing parity
   harness is the gate, run per-day sampled).
2. **P1 Shadow:** bridge live, runner live, executor `dry_run` — intents logged + published to CT
   for ≥2 weeks; verify decision stream sanity, bar-seam integrity, uptime.
3. **P2 Paper:** demo MT5 account, executor `live` — full order lifecycle + reconciliation on
   demo; model-vs-actual divergence report.
4. **P3 Live:** funded account, minimum size, kill-switch drill first.
Promotion between phases is an explicit operator decision recorded in PROJECT_STATE.md.

---

## PART 3 — §5 OPEN OPERATOR DECISIONS (block P2+)
**5.1 PM-DISABLE (standing since M1):** trade the 12 DISABLE cohorts as Golden did
(include-disabled ON: 194/635 decided, +85.71R ≈ 29% of net) or enforce the deployed policy.
Required before paper trading. The runner takes it as one config flag.
**5.2 Data seam:** frozen research dataset (vendor X, ends 2026-06-19) + MT5 vendor bars from
cutover. Different vendors ⇒ slightly different bars ⇒ eventually different OBs. Options:
(a) accept the seam and monitor divergence (fastest), (b) re-baseline the whole history from the
MT5 vendor and re-freeze a new Golden reference (cleanest, requires one new Golden run + gate).
**5.3 Deployment topology:** runner on the Mac with store sync from the VPS, or runner+bridge
co-located on the VPS (fewer moving parts, needs Python env parity — determinism across
environments was proven once in the M2 foundation, must be re-proven on the VPS).

## PART 4 — Milestone exit criteria
P0 byte-identical rehearsal PASS · P1 two-week shadow with zero unexplained decision diffs and
zero missed bars · P2 demo orders placed/modified/closed with clean reconciliation and a
model-vs-actual report · CT shows live status, decisions, orders, and honours pause/flatten/kill
· §5 decisions recorded · PROJECT_STATE.md updated per phase.
