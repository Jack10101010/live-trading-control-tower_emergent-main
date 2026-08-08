"""M-NEWS-DST-VALIDATION-1 Part B — OLD vs NEW session clock, one variable.

Runs the SAME production golden pipeline twice in one process, on the same
machine, the same frozen dataset and the same strategy matrix. The ONLY
difference is the session classifier:

    OLD = classify on the raw UTC hour        (the pre-M-SESSION-DST-1 rule)
    NEW = classify on the Europe/London hour  (deployed)

Deliberately NOT compared against golden/run-001/reference: that archive was
produced on the Mac, with the ARCHIVE-ERA dataset (sha 314a0efa) before the
2026-08-02 Dukascopy revision (now ba32de0d), so a diff against it would
conflate the DST correction with a dataset rebuild and cross-machine float
noise. Isolating the variable requires both arms on this host.

Nothing is modified: the OLD arm is produced by monkeypatching the classifier's
hour function for the duration of that run only. The tracked strategy, the
cohort/state matrix, targets, policy, news data and costs are untouched.
"""
import json, os, sys, time
from pathlib import Path

CT = Path(r"C:\Users\Administrator\Projects\live-trading-control-tower_emergent-main")
LUX = Path(r"C:\Users\Administrator\Projects\Lux-OB-Backtester")
OUT = CT / "artifacts" / "dst-counterfactual"
sys.path.insert(0, str(CT))
os.environ.setdefault("LUX_ROOT", str(LUX))
os.environ.setdefault("LIVE_MODE", "dry_run")

import pandas as pd
from live.config import LiveConfig
from live.runner import LiveRunner, LuxSession

cfg = LiveConfig()
session = LuxSession(cfg.lux_root)          # chdir's into Lux (driver contract)
session.verify_engine()
runner = LiveRunner(cfg, session=session)

frozen = cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
candles = pd.read_csv(frozen)
END = "2026-06-19"                          # the Golden run's own end_date
print(f"candles: {len(candles)} rows | end_date {END}", flush=True)

import strategy_core.sessions as S
NEW_london_hour = S._london_hour

def OLD_london_hour(timestamp):
    """The pre-correction rule: classify on the raw UTC hour."""
    ts = pd.Timestamp(timestamp)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.hour

for arm, fn in (("new", NEW_london_hour), ("old", OLD_london_hour)):
    S._london_hour = fn
    S._LONDON_HOUR_CACHE.clear()
    t0 = time.time()
    print(f"[{arm}] starting full-history run...", flush=True)
    trades = runner.golden_pipeline(candles, END)
    dt = time.time() - t0
    p = OUT / f"trades_{arm}_sessions.csv"
    trades.to_csv(p, index=False)
    print(f"[{arm}] {len(trades)} rows in {dt:.0f}s -> {p.name}", flush=True)

S._london_hour = NEW_london_hour
S._LONDON_HOUR_CACHE.clear()
print("DONE", flush=True)
