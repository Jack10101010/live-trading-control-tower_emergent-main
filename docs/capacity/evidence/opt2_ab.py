"""M-CAP-OPT-2 — interleaved A/B measurement of execute_scenario_job.

The first attempt compared two SEQUENTIAL benchmark runs and was invalidated by
machine drift: phases the change cannot affect (detect_order_blocks,
load_news_calendar, resample) improved ~21-33% between them, which is not
attributable to the optimisation.

This design removes drift:
  * pre-stages computed ONCE; only execute_scenario_job is timed;
  * modes ALTERNATE ref/opt/ref/opt..., so any monotonic drift affects both
    arms almost equally;
  * a fresh copy of the inputs is NOT needed (execute_scenario_job does not
    mutate candles/obs), but the trade-frame hash is verified every iteration so
    a silent divergence cannot hide inside a timing run;
  * one warm-up pair is discarded.
"""
import hashlib
import json
import statistics
import sys
import time
from dataclasses import replace
from pathlib import Path

WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(WT))
import os
os.chdir(WT)

import pandas as pd
import scripts.run_backtest as rb
import strategy_core as core
import strategy_core.execution as ex

OUT = Path(sys.argv[1]); OUT.parent.mkdir(parents=True, exist_ok=True)
PAIRS = int(sys.argv[2]) if len(sys.argv) > 2 else 5

GOLDEN = WT / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"
overrides, _ = rb.load_config_overrides(str(GOLDEN))
base = replace(rb.ACTIVE_CONFIG, **overrides)
# counters OFF: they add ~16% and are not part of the baseline methodology
cfg = replace(base, end_date="2026-06-19", profile_simulation_hot_path=False)

print("loading + building stages once...", flush=True)
candles_raw = pd.read_csv(WT / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv")
candles = rb.filter_date_range(candles_raw, cfg)
candles = core.prepare_candles_for_simulation(candles)
calendar_events = rb.load_news_calendar_events(cfg)
news_events = rb.load_news_events(cfg)
from src.execution import prepare_news_cache
news_cache = prepare_news_cache(news_events, candles,
                               cfg.news_flatten_minutes_before_blackout)
detection = rb.resample_candles(candles, cfg.detection_timeframe)
obs = core.detect_order_blocks(detection, swing_length=cfg.swing_length,
                               ob_filter=cfg.ob_filter, pip_size=cfg.pip_size,
                               min_ob_size_pips=cfg.min_ob_size_pips,
                               max_ob_size_pips=cfg.max_ob_size_pips)
obs = rb.tag_order_blocks_with_news(obs, calendar_events)
obs = rb.filter_order_blocks_by_structure(obs, cfg.structure_filter)
obs, _ = rb.filter_order_blocks_by_structure_direction(
    obs, cfg.allowed_structure_directions)
sim_obs = core.prepare_order_blocks_for_simulation(obs)
entry = [x for x in rb.entry_scenarios(cfg) if x["mode"] == "triggered_edge"]
mode_name = cfg.execution_modes[0]


def one(gate: bool):
    ex.TE_GATE_ENABLED = gate
    job = {"kind": "entry", "execution_mode": mode_name, "scenario": entry[0],
           "job_id": f"{mode_name}:entry:{entry[0]['key']}", "label": entry[0]["key"]}
    t0 = time.monotonic()
    out = rb.execute_scenario_job(job, cfg, candles, sim_obs, obs, news_cache)
    dt = time.monotonic() - t0
    trades = out["results"][0]["trades"]
    h = hashlib.sha256(trades.to_csv(index=False).encode()).hexdigest()
    return dt, h, int(len(trades))


samples = {"ref": [], "opt": []}
hashes = set()
order = []

print("warm-up pair (discarded)...", flush=True)
for g in (False, True):
    dt, h, n = one(g)
    hashes.add(h)
    print(f"  warmup {'opt' if g else 'ref'}: {dt:.2f}s rows={n}", flush=True)

for i in range(PAIRS):
    for gate, key in ((False, "ref"), (True, "opt")):
        dt, h, n = one(gate)
        samples[key].append(dt)
        hashes.add(h)
        order.append({"pair": i, "mode": key, "seconds": round(dt, 4)})
        print(f"  pair {i} {key}: {dt:.2f}s", flush=True)

assert len(hashes) == 1, f"OUTPUT DIVERGED ACROSS MODES: {hashes}"


def stats(v):
    return {"n": len(v), "mean": statistics.mean(v), "p50": statistics.median(v),
            "min": min(v), "max": max(v),
            "stdev": statistics.stdev(v) if len(v) > 1 else None}


r, o = stats(samples["ref"]), stats(samples["opt"])
report = {
    "design": "interleaved A/B on execute_scenario_job; pre-stages built once; "
              "one warm-up pair discarded; hot-path counters OFF",
    "pairs": PAIRS,
    "trades_frame_sha256": hashes.pop(),
    "reference": r, "optimised": o,
    "mean_saving_s": r["mean"] - o["mean"],
    "mean_saving_pct": (r["mean"] - o["mean"]) / r["mean"] * 100,
    "p50_saving_s": r["p50"] - o["p50"],
    "p50_saving_pct": (r["p50"] - o["p50"]) / r["p50"] * 100,
    "samples": order,
}
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k: v for k, v in report.items() if k != "samples"}, indent=2))
