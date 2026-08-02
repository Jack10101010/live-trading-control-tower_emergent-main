"""M-LIVE-STATE-5 Phase 1 — is ob_id a usable merge identity? (No.)

`detect_order_blocks` assigns ob_id as a RUN-RELATIVE 1-based counter
(strategy_core/order_blocks.py: `ob_id = 1` ... `ob_id += 1`), so the same
physical block is numbered differently in a full-history run than in a two-year
one. GhostTracker keys state by `str(ob["ob_id"])`, and pending/active records
reference blocks the same way -- so carrying state across window sizes without
remapping would silently orphan every ghost and rebind every reference wrongly.

This measures the instability and validates a content identity instead.
"""
import pandas as pd
from pathlib import Path

W = Path(__file__).resolve().parent / "out" / "windows"
KEY = ["direction", "origin_time", "detection_time", "top", "bottom"]


def main() -> int:
    full = pd.read_csv(W / "obs-full.csv", dtype=str)
    win = pd.read_csv(W / "obs-2y.csv", dtype=str)
    full["k"] = full[KEY].agg("|".join, axis=1)
    win["k"] = win[KEY].agg("|".join, axis=1)
    common = set(full["k"]) & set(win["k"])
    fmap, wmap = dict(zip(full["k"], full["ob_id"])), dict(zip(win["k"], win["ob_id"]))
    mismatch = [k for k in common if fmap[k] != wmap[k]]
    print(f"blocks in both runs        : {len(common):,}")
    print(f"ob_id differs              : {len(mismatch):,} "
          f"({100*len(mismatch)/max(1,len(common)):.1f}%)")
    for k in sorted(mismatch)[:3]:
        print(f"   full={fmap[k]:>5}  window={wmap[k]:>4}  det={k.split('|')[2]}")
    for name, df in (("full", full), ("2y", win)):
        print(f"content-key collisions {name:>4}: {int(df['k'].duplicated().sum())}")
    for cols in (["detection_time", "direction"], KEY):
        kk = full[cols].agg("|".join, axis=1)
        print(f"  key {cols}: unique {kk.nunique():,}/{len(full):,}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
