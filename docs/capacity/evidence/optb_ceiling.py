"""Throwaway: what is the ABSOLUTE ceiling for removing the metric rescan?

Stubs _update_pending_metrics to a no-op. Output is WRONG (metrics are zeroed)
- this measures only the time the rescan costs, to decide whether the exact
reformulation is worth 21 careful edits.
"""
import sys, time
from dataclasses import replace
from pathlib import Path
WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(WT)); import os; os.chdir(WT)
import pandas as pd, scripts.run_backtest as rb, strategy_core as core
import strategy_core.execution as ex

GOLDEN = WT/"generated_configs"/"d6cdae589b1e4c37a67763253c466067.json"
ov,_ = rb.load_config_overrides(str(GOLDEN))
cfg = replace(replace(rb.ACTIVE_CONFIG, **ov), end_date="2026-06-19",
              profile_simulation_hot_path=False)
c = pd.read_csv(WT/"data"/"candles"/"EURUSD_1m_extended_2015_2026.csv")
c = core.prepare_candles_for_simulation(rb.filter_date_range(c, cfg))
cal = rb.load_news_calendar_events(cfg); nv = rb.load_news_events(cfg)
from src.execution import prepare_news_cache
nc = prepare_news_cache(nv, c, cfg.news_flatten_minutes_before_blackout)
det = rb.resample_candles(c, cfg.detection_timeframe)
obs = core.detect_order_blocks(det, swing_length=cfg.swing_length, ob_filter=cfg.ob_filter,
        pip_size=cfg.pip_size, min_ob_size_pips=cfg.min_ob_size_pips,
        max_ob_size_pips=cfg.max_ob_size_pips)
obs = rb.tag_order_blocks_with_news(obs, cal)
obs = rb.filter_order_blocks_by_structure(obs, cfg.structure_filter)
obs,_ = rb.filter_order_blocks_by_structure_direction(obs, cfg.allowed_structure_directions)
sim = core.prepare_order_blocks_for_simulation(obs)
e = [x for x in rb.entry_scenarios(cfg) if x["mode"]=="triggered_edge"]
m = cfg.execution_modes[0]
job = {"kind":"entry","execution_mode":m,"scenario":e[0],
       "job_id":f"{m}:entry:{e[0]['key']}","label":e[0]["key"]}

real = ex._update_pending_metrics
def run(label):
    t=time.monotonic(); rb.execute_scenario_job(job,cfg,c,sim,obs,nc); dt=time.monotonic()-t
    print(f"  {label}: {dt:.2f}s", flush=True); return dt

ex.TE_GATE_ENABLED = True
print("warm-up"); run("warmup")
res = {}
for i in range(3):
    ex._update_pending_metrics = real
    res.setdefault("with", []).append(run(f"{i} WITH metric rescan"))
    ex._update_pending_metrics = lambda item, candle: None
    res.setdefault("without", []).append(run(f"{i} WITHOUT (no-op stub)"))
ex._update_pending_metrics = real
import statistics
w, wo = statistics.mean(res["with"]), statistics.mean(res["without"])
print(f"\nmean WITH    : {w:.2f}s")
print(f"mean WITHOUT : {wo:.2f}s")
print(f"CEILING saving: {w-wo:.2f}s ({(w-wo)/w*100:.1f}% of phase)")
