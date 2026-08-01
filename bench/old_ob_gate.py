"""Old order-block trade-quality gate.

Aggregate net R over nine trades decides nothing. This inspects them
individually and then bands them, because +4.06R could equally be one lucky
outlier carrying eight losers, and those two worlds imply opposite decisions.

Counterfactual method: the Golden config runs `allow_multi_position` with
`reverse_touch_cancel_enabled` force-disabled by the driver, and the engine
documents that gate as "the ONLY cross-trade coupling channel in
allow_multi_position". If that holds, trades are independent and removing a
block's trades changes nothing else -- so an age policy can be evaluated exactly
by subtraction. That independence is ASSUMED here and validated separately by a
re-run; it is stated rather than buried because every counterfactual number below
depends on it.

Read-only over bench/out/windows/trades-full.csv.
"""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

OUT = Path(__file__).resolve().parent / "out" / "windows"
YEAR = 365.25


def num(s):
    return pd.to_numeric(s, errors="coerce")


def load() -> pd.DataFrame:
    t = pd.read_csv(OUT / "trades-full.csv", dtype=str)
    t["fill_dt"] = pd.to_datetime(t["fill_time"], errors="coerce", utc=True)
    t["det_dt"] = pd.to_datetime(t["detection_time"], errors="coerce", utc=True)
    t = t[t["fill_dt"].notna() & t["det_dt"].notna()].copy()
    t["age_days"] = (t["fill_dt"] - t["det_dt"]).dt.total_seconds() / 86400.0
    t["age_years"] = t["age_days"] / YEAR
    for c in ("net_r", "pnl_r", "mfe_r", "mae_r", "entry", "stop", "tp",
              "max_ob_penetration_pct", "fill_penetration_pct"):
        if c in t.columns:
            t[c + "_f"] = num(t[c])
    return t


def stats(d: pd.DataFrame, label: str) -> dict:
    r = d["net_r_f"].dropna()
    wins, losses = r[r > 0], r[r < 0]
    be = r[(r == 0)]
    gp, gn = float(wins.sum()), float(abs(losses.sum()))
    pf = (gp / gn) if gn > 0 else float("inf") if gp > 0 else 0.0
    return {"band": label, "n": int(len(r)), "wins": int(len(wins)), "losses": int(len(losses)),
            "be": int(len(be)),
            "win_rate": round(100 * len(wins) / len(r), 1) if len(r) else 0.0,
            "gross_pos_r": round(gp, 3), "gross_neg_r": round(-gn, 3),
            "net_r": round(float(r.sum()), 3),
            "avg_r": round(float(r.mean()), 4) if len(r) else 0.0,
            "median_r": round(float(r.median()), 4) if len(r) else 0.0,
            "profit_factor": round(pf, 3) if pf != float("inf") else "inf",
            "largest_win": round(float(wins.max()), 3) if len(wins) else 0.0,
            "largest_loss": round(float(losses.min()), 3) if len(losses) else 0.0,
            "expectancy_r": round(float(r.mean()), 4) if len(r) else 0.0}


def main() -> int:
    t = load()
    total_r = float(t["net_r_f"].sum())
    print(f"filled trades {len(t)}   total net R {total_r:+.2f}\n")

    print("=" * 118)
    print("INDIVIDUAL TRADES WITH CAUSAL ORDER BLOCK OLDER THAN 1 YEAR")
    print("=" * 118)
    old = t[t["age_years"] > 1.0].sort_values("age_days")
    cols = ["trade_id", "direction", "detection_time", "fill_time", "outcome",
            "net_r_f", "mfe_r_f", "mae_r_f", "entry_f", "stop_f", "tp_f",
            "fill_session", "structure_tag", "max_ob_penetration_pct_f"]
    for _, r in old.iterrows():
        print(f"\n  trade_id      : {r['trade_id']}")
        print(f"  instrument    : EURUSD   direction: {r['direction']}   session: {r.get('fill_session','')}")
        print(f"  ob origin     : {r.get('origin_time','<n/a>')}")
        print(f"  ob detection  : {r['detection_time']}")
        print(f"  entry time    : {r['fill_time']}")
        print(f"  AGE at entry  : {r['age_days']:.1f} d ({r['age_years']:.2f} y)")
        print(f"  outcome       : {r['outcome']}   net_r {r['net_r_f']:+.3f}")
        print(f"  MFE / MAE (R) : {r.get('mfe_r_f')} / {r.get('mae_r_f')}")
        print(f"  entry/stop/tp : {r.get('entry_f')} / {r.get('stop_f')} / {r.get('tp_f')}")
        print(f"  structure tag : {r.get('structure_tag','')}")
        print(f"  prior OB touch: max_ob_penetration_pct={r.get('max_ob_penetration_pct_f')} "
              f"fill_penetration_pct={r.get('fill_penetration_pct_f')}")

    print("\n" + "=" * 118)
    print("NON-OVERLAPPING AGE BANDS")
    print("=" * 118)
    bands = [("<1y", t[t["age_years"] <= 1]), ("1-2y", t[(t["age_years"] > 1) & (t["age_years"] <= 2)]),
             ("2-3y", t[(t["age_years"] > 2) & (t["age_years"] <= 3)]),
             ("3-5y", t[(t["age_years"] > 3) & (t["age_years"] <= 5)])]
    cum = [(">1y", t[t["age_years"] > 1]), (">2y", t[t["age_years"] > 2]), (">3y", t[t["age_years"] > 3])]
    rows = []
    hdr = f"{'band':>6} {'n':>4} {'W':>3} {'L':>3} {'BE':>3} {'win%':>6} {'net_r':>8} {'avg_r':>8} {'med_r':>8} {'PF':>7} {'maxW':>7} {'maxL':>7} {'%totR':>7}"
    print(hdr)
    for label, d in bands + cum:
        s = stats(d, label)
        s["pct_of_total_r"] = round(100 * s["net_r"] / total_r, 2) if total_r else 0.0
        s["sample_warning"] = "SEVERE (n<10)" if s["n"] < 10 else ("small (n<30)" if s["n"] < 30 else "")
        rows.append(s)
        print(f"{label:>6} {s['n']:>4} {s['wins']:>3} {s['losses']:>3} {s['be']:>3} "
              f"{s['win_rate']:>6.1f} {s['net_r']:>+8.2f} {s['avg_r']:>+8.3f} {s['median_r']:>+8.3f} "
              f"{str(s['profit_factor']):>7} {s['largest_win']:>+7.2f} {s['largest_loss']:>+7.2f} "
              f"{s['pct_of_total_r']:>+7.2f}")
    for s in rows:
        if s["sample_warning"]:
            print(f"  ! {s['band']}: sample size {s['n']} -- {s['sample_warning']}")

    print("\n" + "=" * 118)
    print("COUNTERFACTUAL EXPIRY (by subtraction; valid only if trades are independent)")
    print("=" * 118)
    base = stats(t, "no expiry")
    print(f"{'policy':>10} {'n':>5} {'net_r':>9} {'win%':>6} {'PF':>7} {'expectancy':>11} {'maxDD_r':>9} {'longest_loss_streak':>20}")

    def dd_and_streak(d):
        d = d.sort_values("fill_dt")
        r = d["net_r_f"].fillna(0.0).to_numpy()
        eq = r.cumsum()
        peak = pd.Series(eq).cummax().to_numpy()
        maxdd = float((eq - peak).min()) if len(eq) else 0.0
        streak = best = 0
        for x in r:
            streak = streak + 1 if x < 0 else 0
            best = max(best, streak)
        return round(maxdd, 3), int(best)

    cf = []
    for label, maxage in [("1y", 1.0), ("2y", 2.0), ("3y", 3.0), ("5y", 5.0), ("none", None)]:
        d = t if maxage is None else t[t["age_years"] <= maxage]
        s = stats(d, label)
        mdd, streak = dd_and_streak(d)
        removed = len(t) - len(d)
        s.update(max_dd_r=mdd, longest_loss_streak=streak, trades_removed=removed,
                 net_r_delta=round(s["net_r"] - base["net_r"], 3))
        cf.append(s)
        print(f"{label:>10} {s['n']:>5} {s['net_r']:>+9.2f} {s['win_rate']:>6.1f} "
              f"{str(s['profit_factor']):>7} {s['expectancy_r']:>+11.4f} {mdd:>+9.2f} {streak:>20}")
    for s in cf:
        if s["trades_removed"]:
            print(f"  expiry {s['band']:>4}: removes {s['trades_removed']:>2} trades, "
                  f"net R change {s['net_r_delta']:+.2f}")

    print("\n== ANNUAL BREAKDOWN OF >1y TRADES ==")
    old2 = old.copy()
    old2["year"] = old2["fill_dt"].dt.year
    for y, g in old2.groupby("year"):
        print(f"  {y}: {len(g)} trades  net_r {g['net_r_f'].sum():+.3f}")

    (OUT.parent / "old_ob_gate.json").write_text(json.dumps(
        {"total_net_r": round(total_r, 3), "bands": rows, "counterfactuals": cf}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
