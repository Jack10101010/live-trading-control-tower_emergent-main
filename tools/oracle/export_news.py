"""Production's news blackout windows, bounded, for the chart to apply itself.

    python -m tools.oracle.export_news --write

WHY
Production pauses pending orders, cancels a touched order and blocks new fills
inside a news blackout. Pine cannot read a file, so until now the chart drew a
setup as ALLOWED with no idea whether production would have blocked it — the one
thing a "trustworthy visual companion" must not do.

The calendar is small once filtered: the deployed filter keeps HIGH impact only,
and a +/-3 minute window. Over a year that is a few hundred windows, which fits
comfortably in generated Pine arrays.

WHAT IT IS NOT
An approximation. Events come from production's own calendar file through
production's own loader (`load_news_events`), which applies the deployed impact
and currency filters and computes `window_start` / `window_end`. Nothing here
re-implements the filter, and nothing infers news from price.

COVERAGE IS A HARD EDGE
The export records the last event it contains. Beyond that timestamp the chart
must say NEWS UNKNOWN rather than ALLOWED: an empty tail of the calendar is
indistinguishable from "no events scheduled", and treating it as permission is
exactly the silent-approximation failure this apparatus exists to prevent.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "news_windows.json"

#: A guard, not a policy. If the deployed filter ever widens enough to blow past
#: this the generator must refuse rather than silently truncate the schedule —
#: a partial blackout map is worse than none, because it reads as coverage.
MAX_WINDOWS = 4000


class NewsExportError(RuntimeError):
    pass


def source_span(engine, cfg) -> dict:
    """What production's CALENDAR FILE itself contains, before any filtering.

    Separate from `build` on purpose. `build` reports the windows the CHART
    will carry, which are narrowed by the impact filter, the currency filter
    and this tool's own `--start`. If the chart's coverage ends early, the
    operator has to know WHICH of those did it — a narrow export is a tooling
    choice, a short calendar file is a production data problem, and the fix is
    completely different.
    """
    import pandas as pd

    path = Path(engine.lux_root) / str(cfg.news_file)
    if not path.is_file():
        return {"path": str(cfg.news_file), "exists": False}
    frame = pd.read_csv(path, usecols=["time", "impact"])
    times = pd.to_datetime(frame["time"], utc=True)
    hi = times[frame["impact"].astype(str).str.lower().isin(
        {str(i).lower() for i in (cfg.news_blackout_impacts or [])})]
    return {
        "path": str(cfg.news_file), "exists": True,
        "rows": int(len(frame)),
        "first_event": times.min(),
        "last_event": times.max(),
        "last_event_at_impact": (hi.max() if len(hi) else None),
        "mtime_ms": int(path.stat().st_mtime * 1000),
    }


def build(engine, cfg, start=None, end=None) -> dict:
    import pandas as pd

    if not cfg.news_blackout_enabled:
        return {
            "schema": "tradingview-oracle-news-v1",
            "enabled": False, "windows": [], "count": 0,
            "_note": "news_blackout_enabled is False — production applies no "
                     "blackout, so the chart has nothing to apply either.",
        }

    lux = Path(engine.lux_root)
    with _in_lux(lux):
        events = engine.rb.load_news_events(cfg)

    frame = pd.DataFrame(events) if not isinstance(events, pd.DataFrame) \
        else events
    if frame.empty:
        raise NewsExportError(
            "the deployed filter produced NO events. That is either a broken "
            "calendar path or a filter that excludes everything; both would "
            "make the chart claim 'no news' for every bar.")

    lo = pd.Timestamp(start, tz="UTC") if start else None
    hi = pd.Timestamp(end, tz="UTC") if end else None

    def _ms(value):
        ts = pd.Timestamp(value)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return int(ts.value // 1_000_000)

    windows = []
    for row in frame.to_dict("records"):
        ts = pd.Timestamp(row["time"])
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        if lo is not None and ts < lo:
            continue
        if hi is not None and ts > hi:
            continue
        windows.append({
            "at_ms": _ms(row["time"]),
            "start_ms": _ms(row["window_start"]),
            "end_ms": _ms(row["window_end"]),
            "currency": str(row.get("currency") or ""),
            "impact": str(row.get("impact") or ""),
        })

    windows.sort(key=lambda w: w["start_ms"])
    if len(windows) > MAX_WINDOWS:
        raise NewsExportError(
            f"{len(windows)} windows exceeds the {MAX_WINDOWS} the chart can "
            "carry. Narrow the window with --start/--end rather than shipping a "
            "truncated schedule the chart would read as full coverage.")

    span = source_span(engine, cfg)
    return {
        "schema": "tradingview-oracle-news-v1",
        "enabled": True,
        "source_file": str(cfg.news_file),
        # The CALENDAR FILE's own last row, so a reader can tell a narrow
        # export from a stale production source.
        "source_rows": span.get("rows", 0),
        "source_last_event_ms": (
            _ms(span["last_event"]) if span.get("last_event") is not None
            else 0),
        "source_last_event_at_impact_ms": (
            _ms(span["last_event_at_impact"])
            if span.get("last_event_at_impact") is not None else 0),
        "impacts": list(cfg.news_blackout_impacts or []),
        "currencies": list(cfg.news_blackout_currencies or []),
        "minutes_before": int(cfg.news_blackout_minutes_before),
        "minutes_after": int(cfg.news_blackout_minutes_after),
        "flags": {
            "pause_pending_orders": bool(cfg.news_pause_pending_orders),
            "cancel_if_touched": bool(cfg.news_cancel_if_touched_during_blackout),
            "block_new_fills": bool(cfg.news_block_new_fills),
            "flatten_active_trades": bool(cfg.news_flatten_active_trades),
        },
        "count": len(windows),
        "first_ms": windows[0]["start_ms"] if windows else 0,
        # THE COVERAGE EDGE. Past this the chart says NEWS UNKNOWN.
        "last_ms": windows[-1]["end_ms"] if windows else 0,
        "windows": windows,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2025-06-01")
    ap.add_argument("--end", default=None)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)

    try:
        out = build(engine, cfg, args.start, args.end)
    except NewsExportError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    import datetime as dt

    def _fmt(ms):
        return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime(
            "%Y-%m-%d %H:%M") if ms else "-"

    print(f"enabled       : {out['enabled']}")
    if out["enabled"]:
        print(f"source        : {out['source_file']}")
        print(f"filter        : impact={out['impacts']} "
              f"currencies={out['currencies'] or 'ALL'}")
        print(f"window        : -{out['minutes_before']}m / "
              f"+{out['minutes_after']}m")
        print(f"flags         : {out['flags']}")
        print(f"windows       : {out['count']}")
        print(f"covers        : {_fmt(out['first_ms'])} .. {_fmt(out['last_ms'])}")
        print(f"beyond that the chart must report NEWS UNKNOWN")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"\nwritten: {args.out.relative_to(CT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
