# P1 Shadow — Windows VPS Runbook (dry_run ONLY)

Operator-executed; every step verified by `live.deploy_check`. Live execution
stays disabled (the runner refuses `LIVE_MODE=live` in P1).

## 1. Install
1. Windows VPS with the broker's **MT5 terminal** installed, logged in, and
   *Tools → Options → Expert Advisors → "Allow algorithmic trading"* enabled.
2. **Python 3.10+ (64-bit)**: `python --version`.
3. Copy both repos at their pinned states:
   - `C:\trading\Lux-OB-Backtester` — must be commit `d978074a` (includes the
     273MB frozen candles file and `configs/policy/deployed_policy.v1.json`).
   - `C:\trading\live-trading-control-tower` — current main (the `live/` package).
4. `pip install MetaTrader5 pandas numpy`

## 2. Configure (System environment variables, then reopen the shell)
```bat
setx LUX_ROOT "C:\trading\Lux-OB-Backtester" /M
setx LIVE_STATE_DIR "C:\trading\live_state" /M
setx MARKET_DATA_DIR "C:\trading\live_state\market_data" /M
setx LIVE_KILL_FILE "C:\trading\live_state\KILL" /M
setx LIVE_MODE "dry_run" /M
setx MT5_LOGIN "<account>" /M
setx MT5_PASSWORD "<password>" /M
setx MT5_SERVER "<broker-server>" /M
setx MT5_SYMBOL_SUFFIX "" /M          :: e.g. ".r" if the broker lists EURUSD.r
setx MT5_SERVER_BASE_UTC_OFFSET_HOURS "2" /M   :: broker clock — see §2.1
setx MT5_SERVER_DST_RULE "us" /M               :: us | eu | none
setx CT_BASE_URL "http://<ct-host>:8000/api" /M
setx PYTHONPATH "C:\trading\live-trading-control-tower" /M
```

## 2.1 Broker server clock — required
MetaTrader 5 encodes tick and bar times as the **broker server's wall clock
stamped as if it were UTC** (the vendor docs call this "UTC without the shift").
On an EEST server that makes every timestamp read 3 hours in the future. The API
exposes no timezone, so the clock is declared here; the gateway converts and
**everything above it runs on canonical UTC**.

The clock is modelled as **base offset + a DST calendar**, not as an IANA zone.
*Measured* on this broker (MT5 H1 history cross-referenced against the true-UTC
Dukascopy series): the offset flips +2h → +3h on the **US** DST date
(2026-03-08), **not** the EU date (2026-03-29). An IANA European zone is
therefore wrong for ~4 weeks a year (08–29 Mar and 25 Oct–01 Nov).

| Setting | Default | Meaning |
|---|---|---|
| `MT5_SERVER_BASE_UTC_OFFSET_HOURS` | `2` | standard-time offset (EET) |
| `MT5_SERVER_DST_RULE` | `us` | `us` (America/New_York) · `eu` (Europe/Brussels) · `none` |

* Re-measure if you change broker. Compare the weekly open in MT5 raw labels
  against a true-UTC reference across a March/November transition.
* `python -m live.deploy_check preflight` must report `mt5_time_base ...
  verified against live tick`. A converted tick in the **future** fails closed.
  During a US/EU shoulder period the check appends a `DST calendars disagree`
  note so the riskiest weeks are visible rather than silent.
* A reconnect re-verifies the clock — it may land on a different terminal.
* The live segment records its `time_base` in `market_data/provenance.json`,
  written **only when rows are appended** (never at construction, so an
  unverified segment can never certify itself). A segment with a mismatched
  **or missing** provenance is **refused at startup** — archive
  `EURUSD_1m_live.csv` + `provenance.json` and let the bridge re-backfill.

## 3. Preflight (must be ALL PASS before installing the service)
```bat
cd C:\trading\live-trading-control-tower
python -m live.deploy_check preflight
```

## 4. Service with automatic restart — NSSM (preferred)
```bat
nssm install live-shadow "C:\Python3xx\python.exe" -m live.main
nssm set live-shadow AppDirectory C:\trading\live-trading-control-tower
nssm set live-shadow AppEnvironmentExtra PYTHONPATH=C:\trading\live-trading-control-tower
nssm set live-shadow AppStdout C:\trading\live_state\ops\service.out.log
nssm set live-shadow AppStderr C:\trading\live_state\ops\service.err.log
nssm set live-shadow AppExit Default Restart
nssm set live-shadow AppRestartDelay 15000
nssm start live-shadow
```
Task Scheduler alternative: create a task running the same command
At startup + repeat every 5 minutes ("run if not already running"), with
"restart on failure" enabled.

## 5. Runtime verification (after ≥20 min during market hours)
```bat
python -m live.deploy_check runtime
```
Confirms: bars growing, heartbeat fresh + error-free, a 15m boundary
processed, ZERO sent/confirmed broker calls in the ledger, CT ingest
acknowledged.

## 6. Controlled restart drill (once, any time after first boundary)
```bat
python -m live.deploy_check restart      :: writes snapshot, tells you to restart
nssm restart live-shadow
:: wait ~2 minutes
python -m live.deploy_check restart      :: verifies resume/no-dupes/no-gap
```

## 7. Daily glance
`type C:\trading\live_state\ops\heartbeat.json` — the beat carries a `phase`:
`cycle_running` (a recompute is in flight; these legitimately run for minutes)
or `idle` (waiting for the next boundary). Investigate when `idle` is older than
5 min, or `cycle_running` exceeds the 900s bar budget. `deploy_check runtime`
applies the same phase-aware thresholds. Kill switch (blocks new opens
instantly): `type nul > C:\trading\live_state\KILL` — delete the file to clear.

## 7.1 Safety rails in force (all verified by tests)
| Rail | Blocks | Backed by |
|---|---|---|
| kill_switch | all but engine closes | `LIVE_KILL_FILE` presence |
| symbol_whitelist | non-whitelisted resolved broker symbol | `config.broker_symbol` |
| duplicate_intent | replayed intent ids | intent ledger |
| daily_loss_limit | new opens after −`LIVE_DAILY_LOSS_LIMIT_R` | realised `net_r` accrued on every mirrored close — **including broker-side SL/TP/manual exits**, which reconcile records so the engine's close still accounts for them |
| max_open_positions | opens beyond `LIVE_MAX_OPEN_POSITIONS` | mirror count |
| unknown_position | modify/close without a mirrored ticket | mirror map |

Realised R accrues on **mirrored closes only** — an intra-window fill+exit is
never sent to the broker and must not consume the loss budget.

## 8. Promotion report (any time; evidence gates, no calendar requirement)
```bat
python -m live.shadow_report --state-dir %LIVE_STATE_DIR%
```
PROMOTE appears only when all gates pass (default: ≥480 processed bars ≈ 5 FX
days, zero duplicates/missed bars/critical reconciliation/runner errors, max
cycle < 600s, no 900s boundary overrun; tune via SHADOW_MIN_OBSERVED_BARS /
SHADOW_MAX_CYCLE_TIME_S). Send `ops\shadow-report.json` (plus any real
OPEN/CLOSE/MODIFY intents from `ops\cycles.jsonl`) back for the P1→P2 review.
