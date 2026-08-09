"""M-LIVE-NEWS-1 operator surface for the live economic calendar.

    python -m live.news_cli status     # what the node currently believes
    python -m live.news_cli refresh    # force a fetch now (ignores the TTL)
    python -m live.news_cli events     # the cached rows the node will consume

`status` is read-only and touches no network, so it reports what the RUNNING
node would decide right now — not what a fresh fetch would say. That distinction
matters: the question an operator needs answered before arming is "is the cached
calendar good enough", not "can this shell reach the internet".

The node refreshes itself once per cycle on its own TTL, so `refresh` exists for
two other jobs: proving the source works from this host, and giving a scheduled
task something to call if the node is stopped for a long maintenance window.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from live.config import LiveConfig
from live.news_feed import (MAX_STALE_S, REFRESH_TTL_S, REQUIRED_HORIZON_S,
                            NewsCalendar, next_event, relevant)


def _fmt(e) -> str:
    return ("none in covered period" if not e else
            f"{e['time']}  {e['impact'].upper():6s} {e['currency']}  {e['event']}")


def _status(cal: NewsCalendar, now) -> int:
    h = cal.health(now)
    allowed, reason, detail = cal.verdict(now)
    ev = cal.events()
    rel = cal.relevant_events()
    print(f"now (UTC)          {now.isoformat()}")
    print(f"source             {h.source} <{cal.url}>")
    print(f"cache              {cal.cache_path}")
    print(f"last refresh       {h.fetched_at}"
          + (f"  ({h.age_s/60:.1f} min ago)" if h.age_s is not None else ""))
    print(f"coverage until     {h.coverage_until}"
          + (f"  ({h.coverage_s/3600:.1f}h remaining)" if h.coverage_s is not None else ""))
    print(f"events (all/rel)   {len(ev)} / {len(rel)}")
    print(f"watching           impacts={cal.impacts} currencies={cal.currencies} "
          f"window=+/-{cal.before_min:g}/{cal.after_min:g}m enabled={cal.blackout_enabled}")
    print(f"flatten on news    {cal.telemetry_block(now)['flatten_active_trades']}  "
          "(M-LIVE-NEWS-1 removed the pre-blackout flatten)")
    print(f"health             {h.status.upper()} - {h.detail}")
    if h.last_error:
        print(f"last error         {h.last_error}")
    for cur in cal.currencies:
        nxt = next_event(relevant(ev, [cur], cal.impacts), now)
        print(f"next {'/'.join(cal.impacts).upper()} {cur:4s}  {_fmt(nxt)}")
    print(f"NEW OPEN           {'ALLOWED' if allowed else 'BLOCKED - ' + str(reason)}")
    if not allowed:
        print(f"                   {detail}")
    print(f"thresholds         ttl={REFRESH_TTL_S}s max_stale={MAX_STALE_S}s "
          f"required_horizon={REQUIRED_HORIZON_S}s")
    return 0 if h.ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Live economic calendar operations.")
    ap.add_argument("command", choices=("status", "refresh", "events"))
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--relevant-only", action="store_true",
                    help="events: only rows that can block this instrument")
    args = ap.parse_args(argv)

    cal = NewsCalendar(LiveConfig())
    now = datetime.now(timezone.utc)

    if args.command == "refresh":
        out = cal.refresh(now)
        print(json.dumps(out, indent=1))
        if not out.get("ok"):
            return 2
        return _status(cal, now) if not args.json else 0

    if args.command == "events":
        rows = cal.relevant_events() if args.relevant_only else cal.events()
        if args.json:
            print(json.dumps(rows, indent=1))
        else:
            print(f"{len(rows)} cached events  (source={cal.cache_path})")
            for e in rows:
                print("  " + _fmt(e))
        return 0

    if args.json:
        print(json.dumps(cal.telemetry_block(now), indent=1))
        return 0 if cal.health(now).ok else 1
    return _status(cal, now)


if __name__ == "__main__":       # pragma: no cover
    sys.exit(main())
