# MT5 Gateway Troubleshooting (LIVE-5B)

**Start here:** `GET /api/integration/diagnostics`. It runs the whole chain in
dependency order and, for the first failing link, tells you what failed, why and
the likely fix. Once a prerequisite fails, dependent checks report `skipped`
rather than piling up derived errors — so fix the **root cause** first.

```bash
curl -s localhost:8000/api/integration/diagnostics | python -m json.tool
cd backend && python3 -m integration_smoke      # the 12-stage checklist
```

---

## The defect this replaced

Before LIVE-5B, `broker._load_live_gateway()` wrapped the entire import-and-
construct in a bare `except Exception: return None`. Every possible cause — a
`live.config` failure, a bad `LUX_ROOT`, a partial SDK install, an expired login
— surfaced as one sentence:

> `MetaTrader5 package unavailable on this host`

which was often **false** and never actionable. `load_live_gateway_diagnostic()`
now names the failing step, and the adapter reports that specific reason.

---

## Failures by check

### `platform` FAIL — `Darwin`/`Linux`
The MetaTrader5 package is a **Windows wheel only**. There is no macOS or Linux
build. Run the backend on the VPS beside the terminal (`local_loopback` is the
only approved profile). This is not a bug and there is no workaround.

### `mt5_package` FAIL — `ModuleNotFoundError`
`pip install MetaTrader5`. If already installed:
- confirm the **same interpreter** that runs the backend can import it;
- confirm both Python and the terminal are **64-bit**;
- a venv the service does not use is the usual culprit.

### `connection_policy` FAIL — `profile_not_approved`
`CONTROL_TOWER_CONNECTION_PROFILE` is set to a remote profile. Only
`local_loopback` is approved. Unset it. The listed prerequisites
(`tls_trust_configured`, `private_network_attested`, `secrets_management`,
`remote_activation_attested`) are not satisfiable by configuration alone.

### `adapter_selection` FAIL — `mock (default)`
`set CONTROL_TOWER_BROKER_ADAPTER=mt5` and **restart**. The healthiest terminal
in the world is irrelevant while the mock adapter is selected.

### `market_provider` WARN — `fixture selected`
Quotes and candles are **derived**, not broker ticks. The dashboard shows
`provenance: "fixture"`. Set `MARKET_DATA_PROVIDER=mt5`. Do not trade while a
symbol's provenance is `fixture`.

### `credentials` WARN / FAIL
- WARN, none set: MT5 attaches to whatever account the terminal is already
  logged into — verify it is the intended one.
- FAIL, partial: set **both** `MT5_LOGIN` and `MT5_SERVER` (plus
  `MT5_PASSWORD`). A partial set makes `initialize()` fail.

### `gateway_load` FAIL
Read the detail; it names the step.
| Detail mentions | Fix |
|---|---|
| `live.config could not be imported` | check `LUX_ROOT`, `LIVE_STATE_DIR` |
| `LiveConfig could not be constructed` | a `LIVE_*` variable is malformed (e.g. a non-numeric `LIVE_MAX_SPREAD`) |
| `MetaTrader5 package is not importable` | see `mt5_package` above |

### `terminal` FAIL
The detail is MT5's own `last_error()` text.
| Text contains | Meaning | Fix |
|---|---|---|
| `initialize`, `terminal` | terminal not running / not found | start it, log in once, leave it open; set `MT5_PATH` to `terminal64.exe` |
| `authoriz`, `invalid account`, `login` | credentials rejected | re-enter login/password/server in the terminal. **A demo password expires with the account** |
| `not available`, `not found` | package absent | see `mt5_package` |

Also check: *Allow algorithmic trading* enabled, and no modal dialog blocking
the terminal (an unattended terminal showing a popup stops answering).

### `symbol` FAIL — `no quote`
MT5 returns `0.0` for "no quote", and LIVE-5B **refuses zero as a price**. So:
- add the symbol to **Market Watch**;
- use the broker's exact name including any suffix (`EURUSD.r`, `XAUUSD.a`) —
  see the alias map in `server._MT5_ALIASES`;
- confirm the market is open (weekends and holidays quote nothing).

### `execution_mode` FAIL — `observe`
Expected until you arm it. `POST /api/execution/mode` with `manual_live`.
Deliberately not an env var.

### `gateway_live_mode` WARN — `dry_run`
`LIVE_MODE` is the **gateway's own** guard, separate from the Control Tower
execution mode. Both must permit a real order.

### `scenario_producer` WARN — `disabled`
Expected by default. `LIVE_PRODUCER_ENABLED=1` to let completed candles produce
proposals. Off by default so a restart never starts proposing trades.

---

## Runtime states

| State | Meaning | Action |
|---|---|---|
| `STARTING` | no successful tick yet | wait one interval; then diagnose |
| `CONNECTED` | broker connected, evidence fresh | none |
| `DEGRADED` | answering but unhealthy: latency > 2000ms, or not every symbol quoting | check VPS load and Market Watch |
| `STALE` | evidence older than 20s | the loop is wedged or the feed froze; check `lastFailureDetail` |
| `RECONNECTING` | 2+ consecutive failures; auto-reconnect running | watch `reconnectAttempts` vs `reconnectSuccesses` |
| `STOPPED` | deliberately stopped | restart the backend |
| `UNKNOWN` | broker age indeterminable | diagnose |

**Automatic recovery** covers the read path only: a reconnect opens a terminal
session and places no order, so it is safe to automate. Anything that could
affect a **position** is never auto-recovered — the runtime fails closed and
reports, and a human decides.

---

## Reading the logs

Structured and **change-only** — a state line is emitted on transition, not per
tick (at 5s a per-tick line would be ~17k lines/day of noise).

```
runtime.state at=… from=CONNECTED to=RECONNECTING connection=Disconnected latency_ms=… tick=… failures=1 reconnects=0 detail=…
runtime.reconnect ok attempt=1 detail=connected
pipeline.scenario_created at=… scenario=scn_… instrument=EURUSD direction=long structure=BREAK_OF_HIGH candle_closed=…
pipeline.recommendation_created at=… recommendation=rcm_… scenario=scn_… entry=… stop=… target=… quantity=0.01
AUDIT mt5_gateway_unavailable reason=…
```

No credential value is ever logged. Diagnostics mask logins and server names
(`ab…yz`) because diagnostics get pasted into issue trackers; variable *names*
like `MT5_PASSWORD` do appear in remediation advice, which is intended.

---

## Escalation

If diagnostics report `ready: true` but a trade still fails, the problem is past
the gateway: check the preflight endpoint, then the execution pipeline's own
denial reason (`stage` + `code` in the market-order response). Those gates are
unchanged since LIVE-2/LIVE-3 and deny by default.
