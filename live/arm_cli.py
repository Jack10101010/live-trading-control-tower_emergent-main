"""Operator CLI for the arm token — the deliberate out-of-band action.

    python -m live.arm_cli status
    python -m live.arm_cli create --ttl-minutes 240 --max-opens 3
    python -m live.arm_cli disarm

`create` binds the token to the account the TERMINAL currently reports, not to
configuration: config is what an operator can get wrong, the terminal is ground
truth. It refuses unless the terminal is authorised, the account is a DEMO
account, and the broker is flat — arming onto an unknown position is exactly the
state the reconciliation freeze exists to stop.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from live.arming import ArmRuntime
from live.config import LiveConfig
from live.mt5_gateway import MT5Gateway


def _observed(cfg) -> tuple[bool, dict | str]:
    gw = MT5Gateway(cfg)
    ok, detail = gw.connect()
    if not ok:
        return False, f"gateway: {detail}"
    try:
        ok, snap = gw.read_account_state()
        return (True, snap) if ok else (False, str(snap))
    finally:
        gw.disconnect()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("status", "create", "disarm"))
    ap.add_argument("--ttl-minutes", type=int, default=240)
    ap.add_argument("--max-opens", type=int, default=3)
    ap.add_argument("--allow-live-account", action="store_true",
                    help="required to arm anything that is not a DEMO account")
    args = ap.parse_args(argv)
    cfg = LiveConfig()

    if args.action == "status":
        arm = ArmRuntime.load(cfg.state_dir)
        if arm is None:
            print("UNARMED (no token)")
            return 0
        print(json.dumps({
            "malformed": arm.malformed, "disarmed": arm.disarmed,
            "expires_at": arm.context.request_expires_at,
            "remaining_open_attempts": arm.remaining_attempts,
            "probation_max_opens": arm.context.probation_max_opens,
            "bound_server": arm.context.fingerprint.server,
            "mode": arm.context.mode,
            "created_at": arm.context.created_at,
            "now": datetime.now(timezone.utc).isoformat(),
        }, indent=1))
        return 0

    if args.action == "disarm":
        arm = ArmRuntime.load(cfg.state_dir)
        if arm is None:
            print("already unarmed")
            return 0
        arm.disarm("operator")
        print("DISARMED")
        return 0

    ok, snap = _observed(cfg)
    if not ok:
        print(f"REFUSED: cannot observe the account: {snap}")
        return 2
    acct = (snap or {}).get("account") or {}
    login, server = acct.get("login"), acct.get("server")
    if acct.get("trade_mode") != 0 and not args.allow_live_account:
        print(f"REFUSED: trade_mode={acct.get('trade_mode')} is not DEMO. "
              "Pass --allow-live-account only with deliberate authorisation.")
        return 2
    positions = (snap or {}).get("positions_total")
    if positions:
        print(f"REFUSED: broker is not flat ({positions} positions). Arm from a "
              "known-flat state so the first OPEN is the arm's own.")
        return 2
    arm = ArmRuntime.create(cfg.state_dir, login=login, server=server,
                            mode="live", ttl_minutes=args.ttl_minutes,
                            max_opens=args.max_opens)
    print(json.dumps({
        "ARMED": True, "expires_at": arm.context.request_expires_at,
        "remaining_open_attempts": arm.remaining_attempts,
        "bound_server": server, "mode": "live",
        "note": "LIVE_MODE=live and terminal AutoTrading are separate gates",
    }, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
