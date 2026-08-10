"""One command: production changed, refresh the 15-minute TradingView build.

    python -m tools.oracle.update_detection_visual --write

WHAT IT DOES, IN ORDER
    1. verify the engine pin (both digests) — refuse to build from an
       unverified tree
    2. re-extract the parity contract from the deployed engine
    3. re-extract the resolved TARGET TABLE and diff it against the last one
    4. regenerate the detection Pine
    5. print the config hash, the source hash, the cell counts and every
       changed target cell

WHAT IT DOES NOT DO
    Touch production. Nothing here writes to the engine tree, and the historical
    replay is left alone — that is a separate, slow extraction
    (`tools.oracle.export_replay`) because it walks the 1-minute series, and it
    is not needed to refresh the LIVE targets.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle import target_table as tt
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError,
                                        load_engine, resolve_config)


def _fmt(ms) -> str:
    import datetime as dt
    return (dt.datetime.fromtimestamp(int(ms) / 1000, dt.timezone.utc)
            .strftime("%Y-%m-%d %H:%M")) if ms else "-"


def _news_step(engine, cfg, write: bool) -> bool:
    """Refresh the embedded blackout schedule and say plainly whether NOW is
    covered. Returns False when the chart will report NEWS UNKNOWN today.

    The distinction this exists to draw: the CHART's coverage can end early
    because this tool exported a narrow range, or because production's own
    calendar file stops there. Only the second is a production problem, and
    only the first is fixable from here — so both are printed.
    """
    import datetime as dt

    from tools.oracle import export_news as en

    now_ms = int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000)
    try:
        data = en.build(engine, cfg, "2025-06-01", None)
    except en.NewsExportError as exc:
        print(f"news            : REFUSED — {exc}")
        return False

    src_last = int(data.get("source_last_event_at_impact_ms") or 0)
    last = int(data.get("last_ms") or 0)

    print(f"news source     : {data.get('source_file')}  "
          f"({data.get('source_rows', 0)} rows, last {cfg.news_blackout_impacts}"
          f" event {_fmt(src_last)})")
    print(f"news windows    : {data['count']}   "
          f"(impact={data['impacts']} ccy={data['currencies'] or 'EUR/USD'} "
          f"-{data['minutes_before']}m/+{data['minutes_after']}m)")
    print(f"news first      : {_fmt(data.get('first_ms'))} UTC")
    print(f"news last       : {_fmt(last)} UTC")

    covered = bool(last) and now_ms <= last
    gap_days = (now_ms - last) // 86_400_000 if last else 0
    print(f"now covered     : {'YES' if covered else 'NO'}"
          f"   (now {_fmt(now_ms)} UTC"
          + ("" if covered else f", {gap_days} days past the last window") + ")")

    if not covered:
        # Which of the two causes? The calendar file's own last qualifying
        # event answers it without ambiguity.
        if src_last and src_last <= last + 86_400_000:
            print("  ** PRODUCTION NEWS SOURCE IS STALE **")
            print(f"     {data.get('source_file')} ends at {_fmt(src_last)}.")
            print("     The LIVE engine reads this same file, so production's")
            print("     blackout gate has been inert since then — this is not")
            print("     a charting problem. Refresh the calendar in the Lux")
            print("     tree (scripts/fetch_forexfactory_batch.py) and re-run.")
        else:
            print("  ** THIS EXPORT IS NARROWER THAN THE SOURCE **")
            print(f"     the calendar has events to {_fmt(src_last)}; widen the")
            print("     export window in tools/oracle/export_news.py and re-run.")
        print("     Until then every live setup at today's date renders as")
        print("     NEWS UNKNOWN — the geometry is still drawn, muted and")
        print("     dashed, but it is never labelled ALLOWED.")

    if write:
        en.DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
        en.DEFAULT_OUT.write_text(
            json.dumps(data, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    return covered


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true",
                    help="write the contract, the target table and the Pine")
    args = ap.parse_args(argv)

    # ── 1. the pin ───────────────────────────────────────────────────────────
    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)
    print("engine pin      : OK (manifest and legacy digest both verify)")

    # ── 2. the contract ──────────────────────────────────────────────────────
    from tools.oracle.extract_contract import DEFAULT_OUT as CONTRACT_PATH
    from tools.oracle.extract_contract import build_contract, canonical_json
    contract = build_contract()
    if args.write:
        CONTRACT_PATH.write_text(canonical_json(contract), encoding="utf-8")
    print(f"engine          : "
          f"{contract['fingerprint']['oracle_engine_hash'][:16]}")
    print(f"fingerprint     : {contract['fingerprint']['oracle_engine_id']}")

    # ── 3. the target table, and what moved ──────────────────────────────────
    prior = None
    if tt.DEFAULT_OUT.is_file():
        try:
            prior = json.loads(tt.DEFAULT_OUT.read_text(encoding="utf-8"))
        except ValueError:
            prior = None
    try:
        table = tt.build(engine, cfg)
    except tt.TargetTableError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    c = table["counts"]
    print(f"config hash     : {table['config_hash'][:16]}"
          + ("" if not prior else
             ("   (unchanged)" if prior.get("config_hash") == table["config_hash"]
              else f"   was {str(prior.get('config_hash'))[:16]}")))
    print(f"target cells    : {c['base_cells']} base "
          f"({c['base_cells_enabled']} enabled) · "
          f"{c['addressable_cells']} addressable · "
          f"{c['state_target_overrides']} state overrides")

    changes = tt.diff(prior, table)
    if prior is None:
        print("changed cells   : (no previous table to compare)")
    elif not changes:
        print("changed cells   : 0")
    else:
        print(f"changed cells   : {len(changes)}")
        for line in changes:
            print(f"    {line}")

    if args.write:
        tt.DEFAULT_OUT.parent.mkdir(parents=True, exist_ok=True)
        tt.DEFAULT_OUT.write_text(
            json.dumps(table, indent=1, sort_keys=True) + "\n", encoding="utf-8")

    # ── 3b. the news schedule ────────────────────────────────────────────────
    # Re-exported EVERY run, from production's own loader. The chart applies
    # this to decide whether production would have been allowed to fill, so a
    # schedule left behind at the last refresh is worse than none: it reads as
    # coverage.
    news_ok = _news_step(engine, cfg, args.write)

    # ── 4. the Pine ──────────────────────────────────────────────────────────
    from tools.oracle.generate_pine import GenerateError, generate
    try:
        out = generate(write=args.write, target="detection_15m")
    except GenerateError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2
    m = out["manifest"]
    print(f"source hash     : {m['pine_source']['sha256']}")
    print(f"pine            : {m['pine_source']['path']}  "
          f"({m['pine_source']['line_count']} lines)")

    # ── 5. what the chart will still be missing ──────────────────────────────
    replay = CT_ROOT / "artifacts" / "tradingview_oracle" / "replay_setups.json"
    if replay.is_file():
        try:
            r = json.loads(replay.read_text(encoding="utf-8"))
            same = r.get("engine_hash") == contract["fingerprint"][
                "oracle_engine_hash"]
            print(f"replay overlay  : {len(r.get('setups') or [])} setups, "
                  f"{'current' if same else 'STALE — will be DROPPED'}"
                  + ("" if same else
                     "; re-run `python -m tools.oracle.export_replay --write`"))
        except ValueError:
            print("replay overlay  : unreadable")
    else:
        print("replay overlay  : none (live setups are unaffected)")

    if not args.write:
        print("\nDRY RUN — nothing written. Re-run with --write.")
    else:
        print("\nPaste the file above into TradingView. Production is untouched.")
    if not news_ok:
        print("NOTE: news coverage does NOT include today — see above. Live "
              "setups will render NEWS UNKNOWN.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
