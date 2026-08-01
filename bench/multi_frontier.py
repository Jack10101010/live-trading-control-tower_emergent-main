"""M-LIVE-STATE-1 Phase 1 — is the two-year window safe at MANY frontiers?

The earlier two-year result came from a single frontier (2026-07-31). One frontier
proves nothing about regimes: a window that happens to contain every relevant
order block in July 2026 may not in March 2020.

ORACLE ECONOMY. Re-running the 11-year oracle per frontier would cost ~850 s x N.
It is unnecessary: the walk is causal, so a trade's fill, entry, stop, target and
identity depend only on candles up to that fill. The single full-history run
therefore already contains the correct fills for EVERY frontier. Only `outcome`,
`exit_time` and `net_r` of trades still open at a frontier would differ, so the
comparison below is restricted to fill-level identity and excludes trades that
had not resolved by the frontier.

Frontiers are chosen to span regimes and to sit shortly after each known
>2-year-old-block trade, since those are precisely the cases a bounded window is
most likely to break.

Read-only. No MT5, no production state, no governed change.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parent.parent
OUT = REPO / "bench" / "out" / "windows"
PY = r"C:\Users\Administrator\Projects\live-trading-control-tower_emergent-main\.venv\Scripts\python.exe"
TRADE_KEY = ["direction", "detection_time", "fill_time", "entry", "stop", "tp"]
LIVE_REGION_DAYS = 90

# Frontier, why it was chosen.
FRONTIERS = [
    ("2016-06-30", "early history, thin window"),
    ("2017-03-31", "range regime"),
    ("2018-01-31", "trend regime"),
    ("2018-06-29", "mid-2018 volatility"),
    ("2019-03-29", "quarter/quarter-end"),
    ("2019-08-15", "just after L_449 (2.20y block trade)"),
    ("2019-12-31", "year boundary"),
    ("2020-03-31", "COVID volatility extreme"),
    ("2020-09-15", "just after S_617 (2.33y block trade)"),
    ("2020-12-31", "after S_613/S_612 (2.65y, 2.69y) + year boundary"),
    ("2021-01-20", "just after S_611 (2.72y block trade)"),
    ("2021-06-30", "mid-year, quiet"),
    ("2022-03-15", "strong trend"),
    ("2022-09-30", "high volatility"),
    ("2023-04-28", "after S_1348 (1.02y)"),
    ("2023-12-29", "year boundary"),
    ("2024-08-30", "after S_1581 (1.07y)"),
    ("2025-01-15", "just after L_1463 (2.09y block trade)"),
    ("2025-06-30", "after S_1279/S_1275 (3.59y, 3.66y)"),
    ("2026-02-10", "just after S_1207 (OLDEST, 4.58y block)"),
    ("2026-07-31", "current frontier"),
]


def keyed(df: pd.DataFrame) -> pd.Series:
    return df[TRADE_KEY].fillna("").astype(str).agg("|".join, axis=1)


def run(window: str, boundary: str) -> bool:
    tag = f"{window}@{boundary}"
    if (OUT / f"summary-{tag}.json").exists():
        return True
    r = subprocess.run([PY, "-m", "bench.live_window", "--window", window, "--boundary", boundary],
                       cwd=str(REPO), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  FAILED {tag}: {r.stderr.strip().splitlines()[-1] if r.stderr else '?'}")
        return False
    return True


def compare(oracle: pd.DataFrame, window: str, boundary: str) -> dict | None:
    tag = f"{window}@{boundary}"
    tp, sp = OUT / f"trades-{tag}.csv", OUT / f"summary-{tag}.json"
    if not (tp.exists() and sp.exists()):
        return None
    s = json.loads(sp.read_text())
    end = pd.Timestamp(boundary, tz="UTC") + pd.Timedelta(days=1)
    start = pd.Timestamp(s["start_date"], tz="UTC")
    live_from = end - pd.Timedelta(days=LIVE_REGION_DAYS)

    w = pd.read_csv(tp, dtype=str)
    w["fill_dt"] = pd.to_datetime(w["fill_time"], errors="coerce", utc=True)
    w["exit_dt"] = pd.to_datetime(w["exit_time"], errors="coerce", utc=True)
    o = oracle

    # Only trades that both FILLED and RESOLVED before the frontier are
    # comparable; an unresolved trade legitimately differs because the oracle saw
    # later candles the windowed run does not have.
    def slice_(d, lo):
        return d[d["fill_dt"].notna() & (d["fill_dt"] >= lo) & (d["fill_dt"] < end)
                 & d["exit_dt"].notna() & (d["exit_dt"] < end)]

    o_live, w_live = slice_(o, live_from), slice_(w, live_from)
    ok, wk = set(keyed(o_live)), set(keyed(w_live))
    o_all, w_all = slice_(o, start), slice_(w, start)
    oka, wka = set(keyed(o_all)), set(keyed(w_all))

    return {"window": window, "boundary": boundary, "start_date": s["start_date"],
            "total_s": s["total_s"], "obs": s["order_blocks"],
            "live_oracle": len(o_live), "live_window": len(w_live),
            "live_missing": len(ok - wk), "live_extra": len(wk - ok),
            "span_oracle": len(o_all), "span_missing": len(oka - wka), "span_extra": len(wka - oka),
            "live_clean": not (ok - wk) and not (wk - ok)}


def main() -> int:
    windows = sys.argv[1:] or ["2y"]
    oracle = pd.read_csv(OUT / "trades-full.csv", dtype=str)
    oracle["fill_dt"] = pd.to_datetime(oracle["fill_time"], errors="coerce", utc=True)
    oracle["exit_dt"] = pd.to_datetime(oracle["exit_time"], errors="coerce", utc=True)

    rows = []
    for window in windows:
        print(f"\n########## WINDOW {window} ##########")
        for boundary, why in FRONTIERS:
            if not run(window, boundary):
                continue
            r = compare(oracle, window, boundary)
            if not r:
                continue
            r["why"] = why
            rows.append(r)
            flag = "CLEAN" if r["live_clean"] else "*** LIVE DIVERGENCE ***"
            print(f"  {boundary}  {r['total_s']:>6.1f}s obs={r['obs']:>4}  "
                  f"live(oracle={r['live_oracle']:>3} miss={r['live_missing']:>2} "
                  f"extra={r['live_extra']:>2})  span(miss={r['span_missing']:>3} "
                  f"extra={r['span_extra']:>3})  {flag}   [{why}]")

    print("\n=== SUMMARY ===")
    for window in windows:
        sub = [r for r in rows if r["window"] == window]
        clean = sum(1 for r in sub if r["live_clean"])
        print(f"  {window}: {clean}/{len(sub)} frontiers with a CLEAN live region "
              f"({LIVE_REGION_DAYS}d)")
        bad = [r for r in sub if not r["live_clean"]]
        for r in bad:
            print(f"     ! {r['boundary']}: missing={r['live_missing']} extra={r['live_extra']} [{r['why']}]")
    (OUT.parent / "multi_frontier.json").write_text(json.dumps(rows, indent=1))
    print(f"\nwritten: {OUT.parent / 'multi_frontier.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
