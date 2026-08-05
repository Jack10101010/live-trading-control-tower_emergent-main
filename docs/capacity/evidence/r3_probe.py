"""M-CAP-R3-1 — characterise the pending-check / metric-update discrepancy.

Read-only w.r.t. both repositories: monkeypatches counters onto the loaded
module at runtime and writes only into the scratchpad.

Hypothesis under test: the gap equals the number of pending items that FILL,
because the fill path (execution.py 2644..2954) is the only resolution branch
with no `_update_pending_metrics` call — which is semantically intended, since
the metric is "max distance away BEFORE fill".
"""
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

LUX = Path(sys.argv[2]) if len(sys.argv) > 2 else Path(
    "/Users/jack/Documents/Dev Projects/Lux-OB-Backtester")
sys.path.insert(0, str(LUX))
import os
os.chdir(LUX)

import pandas as pd
import scripts.run_backtest as rb
import strategy_core as core
import strategy_core.execution as ex

OUT = Path(sys.argv[1])
OUT.parent.mkdir(parents=True, exist_ok=True)

GOLDEN = LUX / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"
overrides, _ = rb.load_config_overrides(str(GOLDEN))
base = replace(rb.ACTIVE_CONFIG, **overrides)
cfg = replace(base, end_date="2026-06-19", profile_simulation_hot_path=True)

# ── counters ────────────────────────────────────────────────────────────────
counts = Counter()
_real_update = ex._update_pending_metrics


def counting_update(item, candle):
    counts["metric_updates"] += 1
    return _real_update(item, candle)


ex._update_pending_metrics = counting_update

frozen = LUX / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
print("loading candles...", flush=True)
candles_raw = pd.read_csv(frozen)

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
assert len(entry) == 1
mode = cfg.execution_modes[0]
job = {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
       "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}

print("running execute_scenario_job...", flush=True)
out = rb.execute_scenario_job(job, cfg, candles, sim_obs, obs, news_cache)
res = out["results"][0]
trades = res["trades"]
hot = res["summary"].get("hot_path_profile") or {}

pending_checks = int(hot.get("pending_checks", 0))
metric_updates = counts["metric_updates"]
gap = pending_checks - metric_updates

# ── attribute the gap ───────────────────────────────────────────────────────
outcomes = Counter(str(v) for v in trades["outcome"].tolist())
filled = int((trades["fill_time"].astype(str) != "").sum())
unfilled = int(len(trades) - filled)

report = {
    "execution_mode": mode,
    "pending_checks": pending_checks,
    "metric_updates": metric_updates,
    "gap": gap,
    "trades_total": int(len(trades)),
    "trades_filled": filled,
    "trades_unfilled": unfilled,
    "gap_equals_filled": gap == filled,
    "outcomes": dict(outcomes),
    "hot_path_profile": hot,
    "config_flags": {
        "execution_modes": list(cfg.execution_modes),
        "entry_models": list(cfg.entry_models),
        "reverse_touch_cancel_enabled": getattr(cfg, "reverse_touch_cancel_enabled", None),
        "news_pause_pending_orders": getattr(cfg, "news_pause_pending_orders", None),
        "news_cancel_if_touched_during_blackout": getattr(
            cfg, "news_cancel_if_touched_during_blackout", None),
        "be_enabled": getattr(cfg, "be_enabled", None),
        "min_ob_size_pips": cfg.min_ob_size_pips,
        "max_ob_size_pips": cfg.max_ob_size_pips,
    },
}
OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k: v for k, v in report.items()
                  if k not in ("hot_path_profile", "outcomes")}, indent=2))
print("outcomes:", json.dumps(dict(outcomes), indent=2))
