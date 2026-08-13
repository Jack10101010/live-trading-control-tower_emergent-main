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
setx CT_BASE_URL "http://<ct-host>:8000/api" /M
setx PYTHONPATH "C:\trading\live-trading-control-tower" /M
```

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
`type C:\trading\live_state\ops\heartbeat.json` — staleness > 5 min ⇒ investigate
(`service.err.log` first). Kill switch (blocks new opens instantly):
`type nul > C:\trading\live_state\KILL` — delete the file to clear.

## 8. Promotion report (any time; evidence gates, no calendar requirement)
```bat
python -m live.shadow_report --state-dir %LIVE_STATE_DIR%
```
PROMOTE appears only when all gates pass (default: ≥480 processed bars ≈ 5 FX
days, zero duplicates/missed bars/critical reconciliation/runner errors, max
cycle < 600s, no 900s boundary overrun; tune via SHADOW_MIN_OBSERVED_BARS /
SHADOW_MAX_CYCLE_TIME_S). Send `ops\shadow-report.json` (plus any real
OPEN/CLOSE/MODIFY intents from `ops\cycles.jsonl`) back for the P1→P2 review.

---

## Durability guarantees (appended per production-readiness audit, finding F-2)

**What is durable, and how.** The execution-critical stores — `runner_state.json`
(intent ledger, trade→ticket mirror, boundary, daily-R) and the arming
consumed-request ledger — are written with the repository's canonical
checkpoint pattern: same-directory temp file → write → flush → **fsync** →
atomic rename. The backend ops journal, ops notifier state and the liveness
beacon already used this pattern; `RunnerState.save()` and
`arming.write_consumed_ledger()` were upgraded to match it.

**Why fsync exists / what it protects.** The atomic rename alone protects
against *process* death (a reader sees the old or the new file, never a torn
one) but not against *OS crash or power loss*: filesystems may journal the
rename before the file's data blocks reach disk, so after power loss the new
filename can exist with old or empty content — silently reverting the
execution ledger after broker state has advanced, or reverting the arming
ledger so an already-spent arm request looks unspent. fsync forces the data to
stable storage before the rename, closing that window.

**What remains impossible to protect.**
- *Directory-entry durability:* the rename itself is not fsync'd (directory
  fsync is unsupported on Windows and deliberately omitted repo-wide to match
  the canonical pattern). After power loss within a very short window the OLD
  complete file may reappear. Recovery remains fail-closed: a ledger older
  than broker truth surfaces as an unmirrored magic-tagged position →
  reconciliation FREEZE, never a duplicate submission.
- *Storage-stack lies:* fsync cannot defeat drive/controller write caches that
  acknowledge before persisting, nor filesystem corruption. Mitigate at the
  VPS level (battery-backed/enterprise storage, disk health monitoring).
- *Diagnostics writers* (`heartbeat.json`, bridge heartbeat, publisher
  fallback, `cycles.jsonl` append) are intentionally best-effort and NOT
  fsync'd — losing an observability record must never block or slow trading.

**Validation entrypoint.** `./validate.sh` at the repo root is the canonical
pre-promotion validation (environment preflight → backend suite → API contract
tests against a real local server with isolated `CONTROL_TOWER_STATE_DIR` →
frontend type-check/build → frontend unit tests). It fails loudly on missing
requirements instead of silently skipping test groups.
