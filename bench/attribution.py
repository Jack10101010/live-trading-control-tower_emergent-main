"""Are the two-year window's live-region misses ALL caused by pre-window blocks?

The seven divergent frontiers were chosen to sit just after a known old-block
trade, so concluding "old blocks cause it" from the frontier labels would be
circular -- I picked those dates. This checks the causal block of every missing
trade directly.

If every miss has detection_time < window_start, the failure is exactly what
Model B's persisted order-block lifecycle restores, and nothing else is wrong
with a bounded window. If any miss has a block INSIDE the window, the window
breaks something a lifecycle record would not fix, and Model B is not sufficient.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "out" / "windows"
TRADE_KEY = ["direction", "detection_time", "fill_time", "entry", "stop", "tp"]
LIVE_DAYS = 90


def keyed(df):
    return df[TRADE_KEY].fillna("").astype(str).agg("|".join, axis=1)


def main() -> int:
    oracle = pd.read_csv(OUT / "trades-full.csv", dtype=str)
    for c in ("fill", "exit", "detection"):
        oracle[c + "_dt"] = pd.to_datetime(oracle[c + "_time"], errors="coerce", utc=True)

    rows = json.loads((OUT.parent / "multi_frontier.json").read_text())
    total_miss = inside = outside = 0
    print(f"{'frontier':>12} {'miss':>5}  causal order block of each missing trade")
    for r in rows:
        if r["live_missing"] == 0:
            continue
        b = r["boundary"]
        start = pd.Timestamp(r["start_date"], tz="UTC")
        end = pd.Timestamp(b, tz="UTC") + pd.Timedelta(days=1)
        live_from = end - pd.Timedelta(days=LIVE_DAYS)
        w = pd.read_csv(OUT / f"trades-2y@{b}.csv", dtype=str)
        for c in ("fill", "exit"):
            w[c + "_dt"] = pd.to_datetime(w[c + "_time"], errors="coerce", utc=True)

        def sl(d):
            return d[d["fill_dt"].notna() & (d["fill_dt"] >= live_from) & (d["fill_dt"] < end)
                     & d["exit_dt"].notna() & (d["exit_dt"] < end)]

        o_live, w_live = sl(oracle), sl(w)
        miss = o_live[~keyed(o_live).isin(set(keyed(w_live)))]
        print(f"{b:>12} {len(miss):>5}")
        for _, m in miss.iterrows():
            age = (m["fill_dt"] - m["detection_dt"]).days
            pre = m["detection_dt"] < start
            inside += (not pre)
            outside += pre
            total_miss += 1
            print(f"{'':>12}        {m['trade_id']:>8} det={m['detection_time'][:10]} "
                  f"fill={m['fill_time'][:10]} age={age:>5}d  net_r={m['net_r']:>7} "
                  f"{'PRE-WINDOW (Model B restores)' if pre else '*** INSIDE WINDOW ***'}")

    print(f"\ntotal live-region misses: {total_miss}")
    print(f"  caused by a block BEFORE the window start : {outside}")
    print(f"  caused by a block INSIDE the window       : {inside}")
    print("\nVERDICT:", "every miss is a pre-window block -> Model B is sufficient"
          if inside == 0 else "SOME misses originate inside the window -> Model B NOT sufficient")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
