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

## 4. Supervision — repeating Task Scheduler trigger

**NSSM / any Windows service is unusable here.** A service runs in Session 0, and
the MetaTrader5 Python API reaches the terminal by IPC *within a session*; from
Session 0 it fails with `-10003`. The node must run in the **interactive
Administrator session** alongside `terminal64.exe`. That constraint drives
everything below.

### 4.1 What supervises what

```
Task Scheduler  --(repeating trigger, every 5 min)-->  powershell.exe  (launcher)
                                                          |
                                                          +-- python.exe (venv redirector stub)
                                                                 |
                                                                 +-- python.exe  <-- THE NODE
```

The launcher stays alive for the node's whole life, so Task Scheduler is always
tracking a genuinely long-running process rather than a shim.

| State | What happens | Bound |
|---|---|---|
| healthy | launcher still running -> Scheduler logs event **322** "did not launch ... already running" and does nothing | — |
| node + launcher absent | next trigger starts a launcher, which starts the node | **<= 5 min** |
| launcher absent, node alive | next trigger starts a launcher; the OS lock refuses the second node; launcher logs `already running - existing healthy owner` and exits **0** | — |
| maintenance marker present | launcher exits **before starting Python**; node is not resurrected | — |

### 4.1.1 Reboot behaviour — a real gap, know it

Supervision resumes automatically after a **logon** (both triggers are armed, and
`StartWhenAvailable` catches a repetition boundary missed while the machine was
down). It does **not** resume after an unattended **reboot**, and the cause is
the interactive-session constraint itself:

* `AutoAdminLogon` is **not** configured, so no interactive session exists until
  someone signs in. A task with `LogonType=InteractiveToken` cannot run without
  one, so neither trigger can fire.
* `terminal64.exe` has **no autostart** entry, so even after a manual logon MT5
  is not running and the node would refuse to start with
  `MT5 gateway unavailable — refusing to start` (it fails closed, correctly).

So a reboot needs an operator: sign in, start MT5, confirm the demo account, then
let the 5-minute trigger pick the node up (or `schtasks /Run`). Enabling
auto-logon would fix it but stores a reusable credential on the box — a security
decision for the operator, deliberately not taken here.

### 4.2 Do NOT rely on "restart on failure"

`RestartOnFailure` is still present in the task XML but **nothing depends on it**,
because on this host it does not work. Measured: three separate non-zero action
results — `3221225786` (twice) and `2147942401` (once) — were each logged as
event **201 "successfully completed"**, event **203 was never emitted**, and no
restart ever occurred. Task Scheduler treats *any* action that ran to completion
as success regardless of its exit code; restart-on-failure covers the task
failing to *launch*. Launcher exit codes are therefore **diagnostic only**:

| Code | Meaning |
|---|---|
| 0 | normal end · already-running (healthy owner) · maintenance active |
| 86 | node killed by a console CTRL event (`0xC000013A`) |
| 87 | node exit code unavailable |
| 90 / 91 | demo-identity guard / `LIVE_MODE` guard refused |
| other | the node's own exit code |

An exit **0** from a lock refusal is deliberate: a five-minutely "already
running" stand-down must never read as a failing task.

### 4.3 Exactly one node

The authority is the **OS runtime lock** (`msvcrt.locking` on
`live_state\ops\runtime.lock`), not the scheduler and not the lock *file*. The
kernel releases it when the holder dies, so a stale `runtime.lock` left by a hard
kill blocks nothing — there is no stale-lock heuristic to get wrong. A second
node exits with:

```
REFUSED: state dir already owned by a live process: pid=<n> host=<h> at=<t>
```

### 4.4 Maintenance — stopping the node on purpose

A repeating trigger resurrects anything that merely stops. `live_state\ops\MAINTENANCE`
is the only thing that distinguishes *intentional* from *unexpected*.

**Presence is the signal; the content is a human-readable reason and is never
parsed.** A parsed marker would reintroduce exactly the malformed-content
ambiguity the marker exists to remove. Empty, binary, half-written, or containing
the literal text `enabled=0` — it all means the same thing: **do not start**.

Fail-closed direction, and the two directions are not symmetric: leaving a
dry-run shadow down is observable and costs nothing, whereas resurrecting a node
an operator stopped on purpose can run the engine against half-edited state.

```bat
:: enter maintenance
echo swapping the engine pin > C:\trading\live_state\ops\MAINTENANCE

:: confirm maintenance is recognised (expect the WARN row)
python -m live.status --no-mt5 | findstr /C:"supervision maintenance"

:: stop the node cleanly (finishes the current cycle first, up to ~30 min)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\trading\live_state\ops\stop-live-shadow.ps1 -Wait

:: confirm it stayed down across >= 2 trigger intervals (>= 11 min)
python -m live.status --no-mt5

:: leave maintenance
del C:\trading\live_state\ops\MAINTENANCE

:: start now instead of waiting up to 5 min for the trigger
schtasks /Run /TN "Live Trading Control Tower - Dry Run Shadow"

:: confirm recovery
python -m live.status --no-mt5
```

> A manual stop **without** entering maintenance first is temporary by design —
> the next trigger restores the node within 5 minutes. That is the intended
> behaviour, not a bug.

> **`schtasks /End` is not a node stop.** It terminates the task's action — the
> launcher — and the node, which has its own console, survives as an orphan still
> holding the runtime lock. Measured. Use `stop-live-shadow.ps1`, which sends
> `CTRL_BREAK` to the node pid recorded in `ops\lifecycle.json`; `live/main.py`
> turns that into "finish this cycle, then exit" and writes
> `last_clean_shutdown` + `stop_reason=SIGBREAK`.

### 4.5 Editing the launcher

`live_state\ops\start-live-shadow.ps1` is git-ignored machine configuration.
It must stay **ASCII-only**: the file has no BOM, Windows PowerShell 5.1 reads it
as CP1252, and a UTF-8 em dash ends in `0x94` — a closing double quote — which
silently terminates a string and broke the launcher once already. After **every**
edit:

```bat
powershell -NoProfile -ExecutionPolicy Bypass -File C:\trading\live_state\ops\validate-launcher.ps1
```

which checks encoding, absence of a BOM, real parse, and the safety invariants
(`LIVE_MODE=dry_run`, pinned demo login/server, `MT5_PASSWORD` never set,
`$proc.Handle` materialised, null `ExitCode` never treated as success,
maintenance gate present).

`$null = $proc.Handle` is load-bearing: `Start-Process -PassThru` does not retain
the native handle, so after the child exits `$proc.ExitCode` is `$null` and
`exit $null` becomes **exit 0** — every node death would report success.

### 4.6 Overnight soak

**Start (evening).** All four must hold before you walk away:

```bat
python -m live.status                     :: expect HEALTHY, and maintenance row OK
del C:\trading\live_state\ops\MAINTENANCE  :: must NOT exist -- else nothing recovers
schtasks /Query /TN "Live Trading Control Tower - Dry Run Shadow" /V /FO LIST | findstr /C:"Status"
:: one launcher + two python.exe (stub + node):
tasklist /FI "IMAGENAME eq python.exe"
```

**Leave alone overnight:** the launcher, both `python.exe` processes, the
scheduled task, `runtime.lock`, and the `ops\` directory. Do not run
`live.main` by hand — it will be refused, which is harmless, but it adds noise.

**Morning verification:**

```bat
python -m live.status
type C:\trading\live_state\ops\lifecycle.json
type C:\trading\live_state\ops\heartbeat.json
findstr /C:"already running" /C:"MAINTENANCE" /C:"launcher exit" C:\trading\live_state\ops\live-shadow.out.log
powershell -NoProfile -Command "Get-WinEvent -LogName Microsoft-Windows-TaskScheduler/Operational -MaxEvents 200 | Where-Object { $_.Message -like '*Dry Run Shadow*' } | Sort-Object TimeCreated | Select-Object TimeCreated,Id"
```

**Success criteria:** `HEALTHY`; heartbeat advancing; cycles continuing in
`ops\cycles.jsonl`; ledger still **0 sent/confirmed**; at most a handful of
`crash_recovery` restarts, each followed by a healthy node; event **322**
(ignored trigger) is the dominant scheduler event.

**Abort immediately if:** the ledger contains any `sent`/`confirmed` entry;
`LIVE_MODE` is anything but `dry_run`; more than one node holds the lock;
lifecycle sits in `DEGRADED`; or the node restarts repeatedly without ever
reaching `RUNNING` (a crash loop — enter maintenance and investigate rather than
letting it churn).

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

## 6.1 Daily health check — one command
```bat
python -m live.status              :: includes a read-only MT5 probe
python -m live.status --no-mt5     :: filesystem only (safe when MT5 is down)
```
Answers, in one read-only pass: bot healthy · MT5 healthy · reconciliation ·
ledger (and **zero sent/confirmed in dry_run**) · market data · time base ·
engine progressing · heartbeat current · replay occurring · stalled ·
intervention required. Exit 0 = healthy, 1 = attention. Writes nothing, opens
no orders, and is safe to run while the shadow loop is live.

Verdicts: `HEALTHY` · `DEGRADED` (review the WARN rows) · `UNHEALTHY`
(intervention required).

## 7. Daily glance
`type C:\trading\live_state\ops\heartbeat.json` — the beat carries a `phase`:
`cycle_running` (a recompute is in flight; these legitimately run for minutes)
or `idle` (waiting for the next boundary). Investigate when `idle` is older than
5 min, or `cycle_running` exceeds the 900s bar budget. `deploy_check runtime`
applies the same phase-aware thresholds. Kill switch (blocks new opens
instantly): `type nul > C:\trading\live_state\KILL` — delete the file to clear.

## 6.2 Engine identity — what is governed, and how to change it
Two independent identities are gated at **startup and in preflight**; a mismatch
in either refuses to trade.

| Identity | Covers | Constant |
|---|---|---|
| `engine_version` | Lux's own 3-file digest (`src/execution.py`, `scripts/run_backtest.py`, `src/resume_support.py`) | `ENGINE_VERSION_EXPECTED` |
| `engine_manifest_id` | **all 30 governed files** — `strategy_core/**`, `src/**`, `scripts/run_backtest.py` | `ENGINE_MANIFEST_ID_EXPECTED` |

`engine_version` alone is **not sufficient**. Its file list predates Milestone 2,
which relocated every strategy subsystem into `strategy_core/` and left
`src/execution.py` a re-export shim. Measured on the pinned tree, the production
import closure is 23 local modules and that digest covers **3** — the walk,
order-block detection, swings, regime, policy, sessions, news and the ghost
tracker could all be edited without moving it. The manifest closes that gap. Both
are kept: `engine_version` preserves continuity with the Golden Run pin.

`live/engine_manifest.json` lists every governed file and its digest, so a failure
names the exact file. Digests are taken over LF-normalised content, so a Windows
`autocrlf` checkout and a macOS checkout of the same commit agree.

Inspect at any time (writes nothing):
```bat
python -m live.engine_identity
```

**Re-approving the manifest is a governance act, not a build step.** It is the
only thing standing between an edited strategy and a live account. Regenerate
*only* alongside a deliberate, reviewed engine change:
```bat
python -m live.engine_identity --write live\engine_manifest.json
```
then copy the printed `engine_manifest_id` into `ENGINE_MANIFEST_ID_EXPECTED` in
`live/config.py` and commit both together. The constant is a second lock: editing
the manifest JSON alone still fails the gate. Re-run Phase 0 parity afterwards —
a new identity means a new engine.

`python -m live.status` reports **`engine identity intact?`** so drift is visible
day to day, not only at startup.

## 6.3 Runtime lifecycle and recovery
The process moves through explicit phases; illegal transitions raise rather than
being logged and ignored. **Only `RUNNING` may execute intents.**

```
INITIALIZING → VALIDATING → CONNECTING → RECONCILING → READY → RUNNING
                    ↓            ↓            ↓          ↓        ↓
                        DEGRADED → RECOVERING          STOPPING → STOPPED
```

Reconciliation now runs **at startup before `READY`, and at the head of every
cycle**. It previously ran only inside `executor.apply()` — i.e. only on a cycle
that produced intents, measured as 1 of 6 realistic cycle outcomes, and never at
startup. A broker-side stop-out therefore stayed invisible for as long as the
engine was quiet, leaving the mirror (and with it `max_open_positions` and the
daily-loss counter) stale. Broker truth now leads the cycle.

**One process per state directory**, enforced by an OS lock at
`ops\runtime.lock`. A second start is refused and names the holder. The lock is
released by the kernel on crash or power loss, so there is no stale-lock
recovery step.

`ops\lifecycle.json` records phase, reconciliation status, restart reason, last
clean shutdown, last crash (time + phase + pid) and recovery duration. All of it
surfaces in `python -m live.status`.

### Shutdown — and its measured limit
A stop signal (`SIGINT`/`SIGBREAK`) is turned into a clean stop **after the
current cycle**, which writes a `STOPPED` marker so the next start reports
`clean_restart` instead of a crash.

> **This works only while the loop is between cycles.** Verified directly on this
> host: an idle loop exits `rc=0` with `reason=SIGBREAK`, but a signal delivered
> during a long C-bound Lux recompute kills the process with
> `STATUS_CONTROL_C_EXIT (0xC000013A)` before Python can run the handler. Since a
> full recompute runs for minutes, **assume any stop during a cycle is a hard
> kill.** That case is covered by atomic writes plus crash detection, not by a
> graceful stop. Making the recompute interruptible is checkpointing work and is
> deliberately out of scope.

Practical consequence for operators: prefer stopping the task when
`heartbeat.json` shows `phase=idle`. Stopping during `cycle_running` is safe —
the cycle replays — but it will be reported as a crash.

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

## 7.2 Fault behaviour (qualified by test, see test_production_qualification.py)
| Fault | Behaviour | Recovery |
|---|---|---|
| MT5 terminal crash / IPC drop | cycle error, `ensure_connected` retries next cycle | **automatic** |
| Broker returns no tick / no rates | structured error, no bars appended, boundary unchanged | **automatic** |
| Duplicate / out-of-order bars | deduplicated + sorted; re-poll appends nothing | **automatic** |
| Lux raises mid-cycle | cycle logged `status=error`, **boundary NOT advanced** → replays | **automatic** |
| Disk full at commit | commit raises, boundary unchanged → cycle replays | automatic once space is freed |
| Disk full while writing ops log | recorded as `ops_log_failed`, loop survives | automatic |
| 10 consecutive errors | process exits non-zero **by design** for the supervisor | **operator/supervisor** |
| Corrupt `runner_state.json` | refuses to start, file quarantined to `.corrupt` | **operator** (restore backup or delete to re-bootstrap) |
| Corrupt live segment tail | actionable error naming the bad row | **operator** (truncate or re-backfill) |
| Missing/mismatched provenance | refuses to start | **operator** (archive + re-backfill) |
| Invalid config (negative lots etc.) | `build()` refuses before any broker contact | **operator** |
| Modified strategy/engine file | refuses to start, names the changed file | **operator** (restore the pinned tree, or re-approve per §6.2) |
| Crash during the frame write | next start refuses, quarantines `prev_trades.corrupt.csv`, then re-bootstraps the baseline and emits no intents | **automatic** (one cycle) |
| Hard kill / power loss mid-cycle | boundary never advanced → cycle replays; ledger suppresses anything already applied | **automatic** |
| Second process started on one state dir | refused at startup, names the holder | **operator** (stop the duplicate) |
| Startup reconciliation freezes | refuses to enter RUNNING, stays `DEGRADED` and observable | **operator** (resolve the unknown position) |
| Broker unreachable during reconcile | freezes; mirror is NOT assumed empty | **automatic** on reconnect |
| Engine module loaded from an unhashed path | refuses to start, names the module | **operator** (remove the stray copy / fix `PYTHONPATH`) |
| VPS clock drift > 5 min | refuses to start, message names **NTP** not config | **operator** |
| Market closed at startup | permitted (`unverified` time base) | n/a |
| Unknown magic-tagged position | reconcile **FREEZES**, never auto-closes | **operator** |

## 8. Promotion report (any time; evidence gates, no calendar requirement)
```bat
python -m live.shadow_report --state-dir %LIVE_STATE_DIR%
```
PROMOTE appears only when all gates pass (default: ≥480 processed bars ≈ 5 FX
days, zero duplicates/missed bars/critical reconciliation/runner errors, max
cycle < 600s, no 900s boundary overrun; tune via SHADOW_MIN_OBSERVED_BARS /
SHADOW_MAX_CYCLE_TIME_S). Send `ops\shadow-report.json` (plus any real
OPEN/CLOSE/MODIFY intents from `ops\cycles.jsonl`) back for the P1→P2 review.
