# Supervised Demo Live-Fire Runbook (M-LIVE-FIRE-1)

*One page. Every step: expected result → stop condition → recovery.
Operator (you) + Claude, market hours, ~60–90 min. Nothing here touches the
production node's state: the node is STOPPED under MAINTENANCE throughout and
the drill uses an isolated state dir.*

**Constants:** demo 1514217330 / FTMO-Demo · 0.01 lots · magic 77001 ·
drill state dir `C:\Users\Administrator\drill_state` · driver
`python -m tools.live_fire_drill --state-dir C:\Users\Administrator\drill_state <action>`

## PRE-FIRE (AutoTrading still OFF)
1. Create MAINTENANCE; `stop-live-shadow.ps1 -Wait`; verify STOPPED + lock released + 0 nodes.
   *Stop if:* stop does not complete → investigate before anything else.
2. Read-only checks: MT5 authorized as 1514217330/FTMO-Demo; positions/orders 0/0;
   `symbol_info(EURUSD).volume_min == 0.01`; spread sane; drill state dir empty/new.
   *Stop if:* any mismatch. *Recovery:* none needed — nothing changed yet.
3. Drill refusal smoke (expected: 4× REFUSED — production dir, dry_run, wrong login, oversize).

## ARM (the ONLY escalation moment)
4. Operator enables AutoTrading in the terminal (Algo Trading button ON).
5. In the drill shell ONLY: `set LIVE_MODE=live` (process-scoped; the launcher,
   the node and every other shell stay dry_run).
   *Expected:* `drill status` shows mode=live, snapshot_ok, 0 positions.
   *Stop if:* anything nonzero at broker. *Recovery:* AutoTrading OFF; done.

## OPEN
6. `drill open` → expected: ledger `sent`→`confirmed`, real ticket in mirror,
   position visible in MT5 with magic 77001, comment = intent_id prefix.
   *Stop if:* `failed` result or no position → record, go to DISARM (nothing to unwind)
   or if position exists without ledger confirm → reconcile (step 8) decides.

## VERIFY + NEGATIVE CONTROL 1 (duplicate)
7. `drill resend-open` (same intent_id) → expected: **blocked, rail=duplicate_intent,
   still exactly one position at broker.**
   *Stop if:* a second position appears → IMMEDIATE: close both manually in MT5,
   AutoTrading OFF, milestone FAILED.
8. `drill reconcile` → expected: no findings, not frozen (mirror matches broker).

## MODIFY
9. `drill modify --stop <price ~10 pips below market>` → expected: confirmed;
   MT5 shows new SL.
   *Stop if:* failed → acceptable to proceed to CLOSE; record.

## NEGATIVE CONTROL 2 (manual close, broker-side exit)
10. Operator closes the position by hand in MT5.
11. `drill reconcile` → expected: `missing_position` finding, mirror dropped,
    `broker_closed` recorded — NOT frozen.
12. `drill close` → expected: accounting-only close (`broker_closed: true`),
    **no second broker order**, ledger confirmed.
    *Stop if:* a new order/position appears → close manually, FAILED.

## KILL DRILL (flat)
13. Create `KILL` file in the DRILL state dir; `drill open --frontier drill-window-2`
    → expected: **blocked, rail=kill_switch, nothing at broker.** Remove KILL.

## RECONCILE-FREEZE DRILL (flat)
14. Operator opens a tiny manual position in MT5 (magic 0), `drill reconcile`
    → expected: `foreign_positions` info only, NOT frozen. Close it.
    (The unknown-magic freeze path is unit-tested; manufacturing a real
    magic-77001 orphan would require a raw API order — deliberately skipped.)

## DISARM
15. `set LIVE_MODE=` (unset in drill shell); AutoTrading OFF in terminal.
    *Expected:* `drill status` refuses actions again; broker flat; 0/0.

## POST-FIRE
16. Archive the drill state dir + MT5 history screenshot into `artifacts/live-fire-1-<date>/`.
17. Verify broker flat, production `live_state` untouched (ledger/mirror still 0/0).
18. Remove MAINTENANCE → scheduler restarts the node in dry_run → verify RUNNING,
    coherent, witness intact, canonical telemetry flowing.
19. Overnight: node stays dry_run. (Armed-node operation is a LATER milestone —
    it additionally needs the launcher's LIVE_MODE/validator pins changed
    deliberately, which this drill deliberately does not touch.)

**Global abort:** anything unexpected at the broker → close all positions
manually in MT5, AutoTrading OFF, unset LIVE_MODE, photograph state, stop.
Rollback is always: flat at broker + drill dir archived + node restarted in
dry_run. Production state cannot be affected: the drill physically cannot
write to it (path refusal) and the node is stopped while armed.
