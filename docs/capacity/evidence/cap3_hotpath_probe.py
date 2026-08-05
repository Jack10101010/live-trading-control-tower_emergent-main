"""CAP-3 — exact hot-path call counts + cProfile call-graph hotspots.

Read-only. Uses the engine's OWN instrumentation (`profile_simulation_hot_path`,
src/config.py:54, default False) for exact call counts with negligible overhead,
then a separate cProfile pass for the call graph.

Writes only into the scratchpad.
"""
import cProfile
import io as _io
import json
import pstats
import sys
import time
from dataclasses import replace
from pathlib import Path

LUX = Path("/Users/jack/Documents/Dev Projects/Lux-OB-Backtester")
CT = Path("/Users/jack/Documents/Dev Projects/live-trading-control-tower-live-prep")
sys.path.insert(0, str(LUX))
sys.path.insert(0, str(CT))
import os
os.chdir(LUX)

import pandas as pd
import scripts.run_backtest as rb
import strategy_core as core

OUTDIR = Path(sys.argv[1]); OUTDIR.mkdir(parents=True, exist_ok=True)

GOLDEN = LUX / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"
overrides, _ = rb.load_config_overrides(str(GOLDEN))
base = replace(rb.ACTIVE_CONFIG, **overrides)

FRONTIER = "2026-06-19"          # same default the benchmark harness uses
frozen = LUX / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"

print("loading candles...", flush=True)
candles_raw = pd.read_csv(frozen)
print("rows:", len(candles_raw), flush=True)

results = {"dataset_rows": int(len(candles_raw)), "frontier_date": FRONTIER}


def build_stages(config):
    """Everything up to (not including) execute_scenario_job."""
    candles = rb.filter_date_range(candles_raw, config)
    candles = core.prepare_candles_for_simulation(candles)
    calendar_events = rb.load_news_calendar_events(config)
    news_events = rb.load_news_events(config)
    from src.execution import prepare_news_cache
    news_cache = prepare_news_cache(news_events, candles,
                                    config.news_flatten_minutes_before_blackout)
    detection = rb.resample_candles(candles, config.detection_timeframe)
    obs = core.detect_order_blocks(
        detection, swing_length=config.swing_length, ob_filter=config.ob_filter,
        pip_size=config.pip_size, min_ob_size_pips=config.min_ob_size_pips,
        max_ob_size_pips=config.max_ob_size_pips)
    obs = rb.tag_order_blocks_with_news(obs, calendar_events)
    obs = rb.filter_order_blocks_by_structure(obs, config.structure_filter)
    obs, _ = rb.filter_order_blocks_by_structure_direction(
        obs, config.allowed_structure_directions)
    sim_obs = core.prepare_order_blocks_for_simulation(obs)
    return candles, obs, sim_obs, news_cache


def make_job(config):
    entry = [x for x in rb.entry_scenarios(config) if x["mode"] == "triggered_edge"]
    assert len(entry) == 1, entry
    mode = config.execution_modes[0]
    return {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
            "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}


# ── PASS 1: exact hot-path counters (engine's own instrument) ────────────────
cfg_hot = replace(base, end_date=FRONTIER, profile_simulation_hot_path=True)
print("PASS 1: building stages (hot-path counters ON)...", flush=True)
candles, obs, sim_obs, news_cache = build_stages(cfg_hot)
results["order_blocks_after_filters"] = int(len(obs))
results["m1_bars_simulated"] = int(len(candles))

job = make_job(cfg_hot)
print("PASS 1: execute_scenario_job...", flush=True)
t0 = time.monotonic()
out = rb.execute_scenario_job(job, cfg_hot, candles, sim_obs, obs, news_cache)
t1 = time.monotonic()
res = out["results"][0]
summary = res["summary"]
results["pass1_execute_scenario_job_s"] = round(t1 - t0, 3)
results["hot_path_profile"] = summary.get("hot_path_profile")
results["trades_rows"] = int(len(res["trades"]))
print("hot path:", json.dumps(results["hot_path_profile"], indent=2), flush=True)

# ── PASS 2: cProfile call graph over execute_scenario_job ────────────────────
cfg_cold = replace(base, end_date=FRONTIER, profile_simulation_hot_path=False)
print("PASS 2: cProfile over execute_scenario_job (expect slower)...", flush=True)
job2 = make_job(cfg_cold)
pr = cProfile.Profile()
t2 = time.monotonic()
pr.enable()
rb.execute_scenario_job(job2, cfg_cold, candles, sim_obs, obs, news_cache)
pr.disable()
t3 = time.monotonic()
results["pass2_cprofile_wallclock_s"] = round(t3 - t2, 3)

pr.dump_stats(str(OUTDIR / "execute_scenario_job.pstats"))
for sort_key, fname in (("tottime", "hotspots_tottime.txt"),
                        ("cumtime", "hotspots_cumtime.txt")):
    buf = _io.StringIO()
    st = pstats.Stats(pr, stream=buf).sort_stats(sort_key)
    st.print_stats(40)
    (OUTDIR / fname).write_text(buf.getvalue())

# structured top-N by tottime
st = pstats.Stats(pr)
rows = []
for func, (cc, nc, tt, ct, callers) in st.stats.items():
    rows.append({"file": func[0], "line": func[1], "func": func[2],
                 "ncalls": nc, "tottime_s": round(tt, 4), "cumtime_s": round(ct, 4)})
rows.sort(key=lambda r: -r["tottime_s"])
results["cprofile_top30_by_tottime"] = rows[:30]

(OUTDIR / "cap3-hotpath.json").write_text(json.dumps(results, indent=2))
print("WROTE", OUTDIR / "cap3-hotpath.json")
