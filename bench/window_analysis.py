"""M-LIVE-WINDOW-1 Phases 2-3 — order-block age distribution and window equivalence.

Two questions, both answered from the artifacts `live_window.py` dumps:

  Phase 2  How old is the order block that causes a trade? That distribution --
           not a guessed policy -- decides the smallest safe live window.
  Phase 3  Does a bounded window reproduce the 11-year oracle's decisions for the
           period both cover?

`ob_id` is assigned per run (1-based within the run), so it CANNOT be used to
match across windows -- a 3-month run and an 11-year run number the same block
differently. Matching is therefore on content: a trade is keyed by
(direction, detection_time, fill_time, entry, stop, tp).

Read-only over bench/out/windows/. Writes nothing outside bench/out/.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "out" / "windows"
TRADE_KEY = ["direction", "detection_time", "fill_time", "entry", "stop", "tp"]
COMPARE_COLS = TRADE_KEY + ["outcome", "exit_time", "net_r", "rr_multiple", "structure_tag"]


def load(tag: str) -> pd.DataFrame | None:
    p = OUT / f"trades-{tag}.csv"
    return pd.read_csv(p, dtype=str) if p.exists() else None


def key_series(df: pd.DataFrame) -> pd.Series:
    # `dtype=str` still leaves EMPTY cells as float NaN, and unfilled candidate
    # rows have empty entry/stop/tp -- so fill before joining or the key blows up.
    return df[TRADE_KEY].fillna("").astype(str).agg("|".join, axis=1)


def phase2_age(oracle: pd.DataFrame, obs: pd.DataFrame) -> dict:
    t = oracle.copy()
    t["fill_dt"] = pd.to_datetime(t["fill_time"], errors="coerce", utc=True)
    t["det_dt"] = pd.to_datetime(t["detection_time"], errors="coerce", utc=True)
    filled = t[t["fill_dt"].notna() & t["det_dt"].notna()].copy()
    filled["age_days"] = (filled["fill_dt"] - filled["det_dt"]).dt.total_seconds() / 86400.0
    filled["net_r_f"] = pd.to_numeric(filled["net_r"], errors="coerce")

    print("== PHASE 2: ORDER-BLOCK AGE AT TRADE TRIGGER ==")
    print(f"order blocks total     : {len(obs):,}")
    print(f"trades (filled)        : {len(filled):,}")
    a = filled["age_days"]
    print(f"age at trigger (days)  : min {a.min():.2f}  median {a.median():.2f} "
          f"p90 {a.quantile(0.9):.2f}  p99 {a.quantile(0.99):.2f}  max {a.max():.2f}")

    rows = []
    for label, days in [("90d", 90), ("180d", 180), ("1y", 365), ("2y", 730),
                        ("3y", 1095), ("5y", 1826)]:
        older = filled[filled["age_days"] > days]
        rows.append({"threshold": label, "days": days, "trades_older": int(len(older)),
                     "pct_of_trades": round(100 * len(older) / max(1, len(filled)), 3),
                     "net_r_of_older": round(float(older["net_r_f"].sum(skipna=True) or 0.0), 3)})
        print(f"  caused by OB older than {label:>5}: {len(older):>5} trades "
              f"({100*len(older)/max(1,len(filled)):6.3f}%)  net_r {older['net_r_f'].sum():+.2f}")

    # how much history does a given window actually need to keep on the books?
    print("\n  max actionable OB age (days):", round(float(a.max()), 2))
    print("  trades needing >2y of history:", int((a > 730).sum()))

    # blocks never traded
    traded_keys = set(filled["detection_time"].astype(str))
    never = obs[~obs["detection_time"].astype(str).isin(traded_keys)]
    print(f"  order blocks never producing a trade: {len(never):,} of {len(obs):,} "
          f"({100*len(never)/max(1,len(obs)):.1f}%)")

    return {"trades_filled": int(len(filled)), "obs_total": int(len(obs)),
            "age_days": {"min": float(a.min()), "median": float(a.median()),
                         "p90": float(a.quantile(0.9)), "p99": float(a.quantile(0.99)),
                         "max": float(a.max())},
            "thresholds": rows, "obs_never_traded": int(len(never))}


def phase3_equiv(oracle: pd.DataFrame, tag: str, summary: dict) -> dict | None:
    win = load(tag)
    if win is None:
        return None
    start = pd.Timestamp(summary["start_date"], tz="UTC")
    o = oracle.copy()
    o["fill_dt"] = pd.to_datetime(o["fill_time"], errors="coerce", utc=True)
    w = win.copy()
    w["fill_dt"] = pd.to_datetime(w["fill_time"], errors="coerce", utc=True)

    # Compare LIKE WITH LIKE. Both frames carry unfilled candidate rows (no
    # fill_time); filtering the oracle by fill_dt silently dropped those while
    # keeping all of the window's, which made every window look like it invented
    # hundreds of trades. Restrict BOTH to filled rows inside the shared span.
    o_in = o[o["fill_dt"].notna() & (o["fill_dt"] >= start)]
    w = w[w["fill_dt"].notna() & (w["fill_dt"] >= start)]
    ok, wk = set(key_series(o_in)), set(key_series(w))
    missing, extra = ok - wk, wk - ok

    # field-level drift on the trades both produced
    common = ok & wk
    o_idx = o_in.set_index(key_series(o_in))
    w_idx = w.set_index(key_series(w))
    drift = 0
    for k in list(common):
        a, b = o_idx.loc[k], w_idx.loc[k]
        if isinstance(a, pd.DataFrame) or isinstance(b, pd.DataFrame):
            continue
        if any(str(a.get(c)) != str(b.get(c)) for c in COMPARE_COLS):
            drift += 1

    verdict = "EXACT" if not missing and not extra and not drift else "DIVERGES"
    print(f"  {tag:>5}: oracle_in_window={len(o_in):>5} window={len(w):>5} "
          f"missing={len(missing):>4} extra={len(extra):>4} field_drift={drift:>4}  {verdict}")
    return {"window": tag, "start_date": summary["start_date"],
            "oracle_trades_in_window": int(len(o_in)), "window_trades": int(len(w)),
            "missing": int(len(missing)), "extra": int(len(extra)), "field_drift": drift,
            "verdict": verdict,
            "sample_missing": sorted(missing)[:5], "sample_extra": sorted(extra)[:5]}


def main() -> int:
    oracle = load("full")
    if oracle is None:
        print("full oracle trades not present yet")
        return 1
    obs = pd.read_csv(OUT / "obs-full.csv", dtype=str)

    report = {"phase2": phase2_age(oracle, obs), "phase3": []}

    print("\n== PHASE 3: WINDOW EQUIVALENCE vs 11-YEAR ORACLE ==")
    print("  (compared over the span each window covers; content-keyed, not ob_id)")
    for tag in ("3mo", "6mo", "1y", "2y", "3y", "5y"):
        sp = OUT / f"summary-{tag}.json"
        if not sp.exists():
            continue
        r = phase3_equiv(oracle, tag, json.loads(sp.read_text()))
        if r:
            report["phase3"].append(r)

    print("\n== PERFORMANCE / SIZE MATRIX ==")
    print(f"{'window':>6} {'m1_rows':>10} {'det_bars':>9} {'obs':>6} {'trades':>7} "
          f"{'total_s':>9} {'walk_s':>8} {'rss_mb':>8}")
    perf = []
    for tag in ("3mo", "6mo", "1y", "2y", "3y", "5y", "full"):
        sp = OUT / f"summary-{tag}.json"
        if not sp.exists():
            continue
        s = json.loads(sp.read_text())
        walk = next((x["wall_s"] for x in s["stages"] if "WALK" in x["stage"]), None)
        print(f"{tag:>6} {s['m1_rows']:>10,} {s['detection_bars']:>9,} {s['order_blocks']:>6,} "
              f"{s['trades']:>7,} {s['total_s']:>9.1f} {walk:>8.1f} {s['peak_rss_mb']:>8.0f}")
        perf.append({"window": tag, **{k: s[k] for k in
                     ("m1_rows", "detection_bars", "order_blocks", "trades", "total_s", "peak_rss_mb")},
                     "walk_s": walk})
    report["performance"] = perf

    (OUT.parent / "live_window_report.json").write_text(json.dumps(report, indent=1))
    print(f"\nwritten: {OUT.parent / 'live_window_report.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
