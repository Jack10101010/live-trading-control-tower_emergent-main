"""M-LIVE-FIRE-PREP-1 — supervised demo execution drill driver.

NOT a new execution path. This constructs OrderIntents exactly as
``live.intents`` does and hands them to the REAL ``Executor.apply`` with the
real ``MT5Gateway``, real ``SafetyRails``, real ledger/mirror state and real
reconciliation. The only thing synthetic is the *origin* of the intent — the
strategy fires too rarely (~1 trade / 2 days) to schedule a supervised window
around a natural signal, so the drill exercises every layer below the runner
on demand. The runner-and-above layers (identity guard, diff) carry their own
test evidence and are not what live-fire exists to prove.

SAFETY DESIGN
  * Refuses to run against the production state dir: ``--state-dir`` is
    mandatory and must not equal the production ``live_state``. The node
    itself stays stopped under MAINTENANCE for the whole window; the drill's
    ledger/mirror live in the isolated dir, so production state stays pristine.
  * Refuses login/server other than the approved demo account.
  * Refuses to OPEN unless ``LIVE_MODE=live`` was set EXPLICITLY for this
    process — it never sets it itself.
  * One action per invocation; the operator reads broker state between steps.
  * ``--lots`` defaults to 0.01 and refuses anything larger than 0.02.

Usage (each step is one command, run from the CT repo root):
  python -m tools.live_fire_drill --state-dir C:\\drill_state open
  python -m tools.live_fire_drill --state-dir C:\\drill_state resend-open
  python -m tools.live_fire_drill --state-dir C:\\drill_state modify --stop <price>
  python -m tools.live_fire_drill --state-dir C:\\drill_state close
  python -m tools.live_fire_drill --state-dir C:\\drill_state reconcile
  python -m tools.live_fire_drill --state-dir C:\\drill_state status
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

APPROVED_LOGIN = "1514217330"          # demo only; drill refuses anything else
APPROVED_SERVER = "FTMO-Demo"
DRILL_TRADE_ID = "DRILL_1"
MAX_LOTS = 0.02


def _refuse(msg: str) -> int:
    print(f"REFUSED: {msg}")
    return 2


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("open", "resend-open", "modify", "close",
                                       "reconcile", "status"))
    ap.add_argument("--state-dir", required=True,
                    help="ISOLATED drill state dir; production live_state is refused")
    ap.add_argument("--lots", type=float, default=0.01)
    ap.add_argument("--stop", type=float, default=None, help="modify: new SL price")
    ap.add_argument("--frontier", default="drill-window-1",
                    help="frontier tag folded into intent_id (stable per window)")
    args = ap.parse_args(argv)

    state_dir = Path(args.state_dir).resolve()
    prod = (REPO / "live_state").resolve()
    if state_dir == prod or prod in state_dir.parents or state_dir in prod.parents:
        return _refuse(f"state dir {state_dir} overlaps production {prod}")
    if os.environ.get("MT5_LOGIN") != APPROVED_LOGIN \
            or os.environ.get("MT5_SERVER") != APPROVED_SERVER:
        return _refuse(f"drill only runs against demo {APPROVED_LOGIN}/{APPROVED_SERVER}")
    if args.lots > MAX_LOTS:
        return _refuse(f"lots {args.lots} > drill maximum {MAX_LOTS}")

    os.environ["LIVE_STATE_DIR"] = str(state_dir)
    from live.config import LiveConfig
    from live.executor import Executor
    from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,
                              OrderIntent, _intent_id)
    from live.mt5_gateway import MT5Gateway
    from live.state import RunnerState

    cfg = LiveConfig()
    if args.action in ("open", "resend-open", "modify", "close") and cfg.mode != "live":
        return _refuse("LIVE_MODE is not 'live'. The drill NEVER sets it; set it "
                       "explicitly in this shell for the supervised window only")
    if abs(cfg.fixed_risk_lots - args.lots) > 1e-9:
        # the executor sizes OPENs from config, so keep the two in agreement
        cfg.fixed_risk_lots = args.lots

    state = RunnerState(state_dir)
    gateway = MT5Gateway(cfg)
    ok, detail = gateway.connect()
    if not ok:
        return _refuse(f"gateway connect failed: {detail}")
    try:
        a = gateway.snapshot()
        executor = Executor(cfg, state, gateway)

        def one(intent):
            print(f"intent_id={intent.intent_id} action={intent.action} "
                  f"trade={intent.trade_id}")
            res = executor.apply([intent])
            print(json.dumps(res, indent=1, default=str)[:2000])
            return 0 if not res.get("frozen") else 1

        if args.action == "status":
            print(json.dumps({"mode": cfg.mode, "magic": cfg.magic_number,
                              "lots": cfg.fixed_risk_lots,
                              "ledger": state.data["ledger"],
                              "mirror": state.data["mirror"],
                              "broker_closed": state.data.get("broker_closed", {}),
                              "snapshot_ok": a[0],
                              "positions": (a[1] or {}).get("positions") if a[0] else None},
                             indent=1, default=str))
            return 0
        if args.action == "reconcile":
            rep = executor.reconcile()
            print(json.dumps(rep.to_dict(), indent=1))
            return 1 if rep.frozen else 0

        # market prices for a plausible stop/target frame
        if args.action in ("open", "resend-open"):
            iid = _intent_id(DRILL_TRADE_ID, OPEN_POSITION, args.frontier)
            tick_ok, tick = gateway.snapshot()
            intent = OrderIntent(intent_id=iid, action=OPEN_POSITION,
                                 trade_id=DRILL_TRADE_ID, side="long",
                                 frontier_bar=args.frontier,
                                 entry=None, stop=0.0, target=0.0,
                                 reason="supervised drill OPEN")
            return one(intent)
        if args.action == "modify":
            if args.stop is None:
                return _refuse("modify requires --stop <price>")
            intent = OrderIntent(
                intent_id=_intent_id(DRILL_TRADE_ID, MODIFY_STOP, args.frontier),
                action=MODIFY_STOP, trade_id=DRILL_TRADE_ID, side="long",
                frontier_bar=args.frontier, stop=args.stop,
                reason="supervised drill MODIFY")
            return one(intent)
        if args.action == "close":
            intent = OrderIntent(
                intent_id=_intent_id(DRILL_TRADE_ID, CLOSE_POSITION, args.frontier),
                action=CLOSE_POSITION, trade_id=DRILL_TRADE_ID, side="long",
                frontier_bar=args.frontier, realized_r=0.0,
                reason="supervised drill CLOSE")
            return one(intent)
        return _refuse("unreachable")
    finally:
        gateway.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
