# First Live Trade Runbook (LIVE-5B)

The operator procedure for the first trade. Prerequisites:
[WINDOWS_VPS_SETUP.md](WINDOWS_VPS_SETUP.md). **Use a demo account.**

> **Status on the development host:** this runbook has NOT been executed.
> `MetaTrader5` has no macOS build, so the trade stages cannot run here. Every
> stage up to the gateway boundary is proven by automated tests; the stages
> beyond it require the VPS. See §7.

---

## 1. Startup

```bat
REM 1. Terminal first — the Python API attaches to a RUNNING terminal.
REM    Launch MT5, confirm the demo account is logged in, leave it open.

REM 2. Environment (see WINDOWS_VPS_SETUP.md §2.4)
set CONTROL_TOWER_BROKER_ADAPTER=mt5
set MARKET_DATA_PROVIDER=mt5

REM 3. Backend — this also starts the runtime refresh loop.
cd backend
python -m uvicorn server:app --host 127.0.0.1 --port 8000
```

Verify, in order. **Do not skip ahead on a failure** — later stages depend on
earlier ones:

| Check | Expected |
|---|---|
| `GET /api/integration/diagnostics` | `ready: true`; no `fail` rows |
| `GET /api/live-runtime` | `broker.connected: true`, `adapterKind: "mt5"` |
| | `broker.accountType: "demo"` ← **confirm this** |
| | `runtime.state: "CONNECTED"` |
| | symbol `availability: "ok"`, `provenance: "mt5"` |
| `python3 -m integration_smoke` | stages 1–3 PASS |
| Dashboard | Broker/Market/Runtime cards populated, no "—" in balance/equity |

If anything fails, the diagnostics response names the failing check with a
`why` and a `fix`. Start there, not in the logs.

---

## 2. Arm the pipeline

```bat
set LIVE_PRODUCER_ENABLED=1
REM restart the backend for this to take effect
```

Then set execution mode — deliberately not an env var:

```bash
curl -X POST http://127.0.0.1:8000/api/execution/mode \
  -H "Content-Type: application/json" \
  -d '{"mode":"manual_live","reason":"first demo trade","confirm":true}'
```

An authorization grant must also be active (`POST /api/authorization/grants`).
Confirm both landed: the preflight endpoint in §3 will tell you.

---

## 3. Wait for a proposal, then inspect it

A Scenario appears when a **completed M15 candle** closes beyond the previous
candle's range. Nothing appears until a real break occurs — that may be minutes
or hours.

When the Recommendation panel shows a `PROPOSED` item:

1. Read the terms: entry, stop, take profit, quantity (`0.01`), risk (`0.25%`),
   planned R (`2.0`), expiry (45 minutes from the candle close).
2. Run preflight:
   `GET /api/trade-recommendations/{id}/preflight`
3. Require `clear: true`. Every blocker lists what and why.

> **The rule behind the proposal has no edge.** It is a candle break, never
> backtested or tuned. You are testing the pipeline, not the signal.

---

## 4. Accept, then submit

**Accepting records a decision. It places no order.** That separation is the
point — acceptance and submission are two deliberate acts.

1. Accept in the Recommendation detail panel (reason required).
2. Confirm status `ACCEPTED`, `outcome: NOT_EXECUTED`, no linked intent.
3. Submit the order, passing `recommendationId` so lineage is captured:

```bash
curl -X POST http://127.0.0.1:8000/api/execution/market-order \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: first-demo-trade-1" \
  -d '{"instrument":"EURUSD","side":"buy","quantity":0.01,
       "stopLoss":<from the recommendation>,"takeProfit":<from the recommendation>,
       "recommendationId":"<rcm_...>","scenarioId":"<scn_...>","confirm":true}'
```

The Idempotency-Key is **required**. A retry with the same key returns the
original outcome and never creates a second order.

> **Financial rails apply to this command (UES).** Until the UES milestone this
> manual submission was gated only on authorization — mode, arming, identity,
> confirmation, account binding, reconciliation — and *not* on the kill switch,
> daily-loss limit, max-open cap or symbol whitelist, which were enforced on the
> autonomous runner alone. Both paths now consult the same `live/safety.py`
> rails, so this order is refused with a `financial_rail_<name>` reason if any
> limit is breached. If you have engaged the kill file (`LIVE_KILL_FILE`), this
> submission will be denied — that is intended. **Closing and cancelling are
> deliberately exempt** and remain available at all times, so you can always
> reduce exposure.

---

## 5. Monitor, close, verify

| Stage | Where | Expected |
|---|---|---|
| Position appears | dashboard Execution card | `activePositions: 1` |
| Stop and target | MT5 terminal | match the recommendation exactly |
| Live updates | Runtime card | `CONNECTED`, projection age < 10s |
| Close | position controls, or let stop/target resolve | position count returns to 0 |
| Ledger | ledger panel | closed trade with Scenario **and** Recommendation lineage |

Ledger ingestion is **not** automatic on close (a known limitation): trigger the
refresh, then confirm the lineage.

Finally: `python3 -m integration_smoke` → **12/12 PASS**. That is the acceptance
criterion for the first trade.

---

## 6. Shutdown

1. **Confirm no open positions** — dashboard `activePositions: 0` and the
   terminal agrees. Never shut down over an open position: the runtime stops
   observing it, but the broker keeps it.
2. Let one more tick complete so the projection reflects the final state.
3. Stop the backend (Ctrl-C). This stops the runtime loop and the gateway
   session cleanly via the shutdown hook.
4. Close the MT5 terminal.
5. Unset `LIVE_PRODUCER_ENABLED` so a restart does not start proposing.

---

## 7. Before enabling LIVE (real money)

Do all of these first:

- [ ] A full demo trade completed 12/12 smoke stages
- [ ] `accountType` showed `demo` throughout — confirm it now shows `real`
      deliberately, not by accident
- [ ] Authentication enabled if the API is reachable off-loopback
      (`CONTROL_TOWER_AUTH_ENABLED=1`), and **not** over plain HTTP
- [ ] Operator identity understood: it is **asserted, not authenticated** — the
      auth boundary is one shared token, so any caller could assert any operator
      id. Every decision records its `identityAssurance`
- [ ] `LIVE_MAX_SPREAD`, `LIVE_MAX_FEED_AGE_S`, `LIVE_DAILY_LOSS_LIMIT_R`,
      `LIVE_MAX_OPEN_POSITIONS` reviewed against your risk appetite
- [ ] The kill file path (`LIVE_KILL_FILE`) known and reachable
- [ ] You accept that the candle rule has no demonstrated edge

**Recommendation: do not enable LIVE for signal reasons.** Enable it only to
verify that real-money plumbing behaves like demo plumbing, at minimum size.
