"""M-LIVE-WINDOW-1 — WHERE does a cold bounded window diverge from the oracle?

Model A diverges at every window size. That alone does not choose an
architecture, because there are two very different causes:

  * divergence clustered near the WINDOW START is a cold-start artifact -- the
    engine lacks the preceding structure, order-block warm-up and open trades.
    Carrying that state forward (Model B) is exactly the fix, and the steady
    state would be sound.
  * divergence near the FRONTIER means the live decision itself depends on
    history the window cannot supply, and no amount of carried state derived
    from inside the window would rescue it.

This measures the distance of every mismatching trade from the window start, so
the choice between A, B and C rests on locality rather than a bare count.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "out" / "windows"
TRADE_KEY = ["direction", "detection_time", "fill_time", "entry", "stop", "tp"]


def keyed(df: pd.DataFrame) -> pd.Series:
    return df[TRADE_KEY].fillna("").astype(str).agg("|".join, axis=1)


def main() -> int:
    oracle = pd.read_csv(OUT / "trades-full.csv", dtype=str)
    oracle["fill_dt"] = pd.to_datetime(oracle["fill_time"], errors="coerce", utc=True)
    oracle["det_dt"] = pd.to_datetime(oracle["detection_time"], errors="coerce", utc=True)

    print(f"{'win':>5} {'span_d':>7} {'n_or':>5} {'miss':>5} {'extra':>6} "
          f"{'miss<30d_of_start':>18} {'miss_last_90d':>14} {'extra<30d_of_start':>19} {'extra_last_90d':>15}")
    rows = []
    for tag in ("3mo", "6mo", "1y", "2y", "3y", "5y"):
        sp = OUT / f"summary-{tag}.json"
        tp = OUT / f"trades-{tag}.csv"
        if not (sp.exists() and tp.exists()):
            continue
        s = json.loads(sp.read_text())
        start = pd.Timestamp(s["start_date"], tz="UTC")
        end = pd.Timestamp(s["boundary"])
        if end.tzinfo is None:
            end = end.tz_localize("UTC")

        w = pd.read_csv(tp, dtype=str)
        w["fill_dt"] = pd.to_datetime(w["fill_time"], errors="coerce", utc=True)
        w["det_dt"] = pd.to_datetime(w["detection_time"], errors="coerce", utc=True)

        o_in = oracle[oracle["fill_dt"].notna() & (oracle["fill_dt"] >= start)]
        w_in = w[w["fill_dt"].notna() & (w["fill_dt"] >= start)]
        ok, wk = keyed(o_in), keyed(w_in)
        miss = o_in[~ok.isin(set(wk))]
        extra = w_in[~wk.isin(set(ok))]

        def near_start(d, days=30):
            return int(((d["fill_dt"] - start).dt.total_seconds() / 86400.0 <= days).sum())

        def near_end(d, days=90):
            return int(((end - d["fill_dt"]).dt.total_seconds() / 86400.0 <= days).sum())

        span = (end - start).days
        print(f"{tag:>5} {span:>7} {len(o_in):>5} {len(miss):>5} {len(extra):>6} "
              f"{near_start(miss):>18} {near_end(miss):>14} "
              f"{near_start(extra):>19} {near_end(extra):>15}")

        # why is each missing trade missing? its causal OB predates the window.
        ob_before = int((miss["det_dt"] < start).sum())
        rows.append({"window": tag, "span_days": span, "oracle": len(o_in),
                     "missing": len(miss), "extra": len(extra),
                     "missing_with_OB_before_window": ob_before,
                     "missing_near_start_30d": near_start(miss),
                     "missing_in_last_90d": near_end(miss),
                     "extra_near_start_30d": near_start(extra),
                     "extra_in_last_90d": near_end(extra)})

    print("\n== WHY ARE ORACLE TRADES MISSING? (causal OB detected before the window) ==")
    for r in rows:
        print(f"  {r['window']:>4}: {r['missing_with_OB_before_window']:>3} of {r['missing']:>3} "
              f"missing trades have an order block older than the window start")

    print("\n== FRONTIER TEST: does the LIVE decision region diverge? ==")
    for r in rows:
        live_bad = r["missing_in_last_90d"] + r["extra_in_last_90d"]
        print(f"  {r['window']:>4}: {live_bad} mismatching trades in the last 90 days "
              f"{'<-- LIVE REGION AFFECTED' if live_bad else '<-- live region clean'}")

    (OUT.parent / "divergence_locality.json").write_text(json.dumps(rows, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
