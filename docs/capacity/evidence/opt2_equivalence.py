"""M-CAP-OPT-2 — full canonical-dataset equivalence.

Runs execute_scenario_job twice IN ONE PROCESS over identical inputs, once with
TE_GATE_ENABLED=False (reference / oracle) and once True (optimised), and
compares the complete trades frames byte-for-byte plus every named field.

Reference mode is the oracle. Any mismatch is a stop condition.
"""
import hashlib
import json
import sys
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

GOLDEN = WT / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"
overrides, _ = rb.load_config_overrides(str(GOLDEN))
base = replace(rb.ACTIVE_CONFIG, **overrides)
cfg = replace(base, end_date="2026-06-19", profile_simulation_hot_path=True)

frozen = WT / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
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
mode = cfg.execution_modes[0]


def make_job():
    return {"kind": "entry", "execution_mode": mode, "scenario": entry[0],
            "job_id": f"{mode}:entry:{entry[0]['key']}", "label": entry[0]["key"]}


def run(gate: bool):
    ex.TE_GATE_ENABLED = gate
    out = rb.execute_scenario_job(make_job(), cfg, candles, sim_obs, obs, news_cache)
    res = out["results"][0]
    return res["trades"], (res["summary"].get("hot_path_profile") or {})


def frame_bytes(df):
    return df.to_csv(index=False).encode()


print("RUN 1/2: reference (TE_GATE_ENABLED=False)...", flush=True)
ref_trades, ref_hot = run(False)
print("RUN 2/2: optimised (TE_GATE_ENABLED=True)...", flush=True)
opt_trades, opt_hot = run(True)

ref_b, opt_b = frame_bytes(ref_trades), frame_bytes(opt_trades)
ref_h = hashlib.sha256(ref_b).hexdigest()
opt_h = hashlib.sha256(opt_b).hexdigest()

report = {
    "reference_rows": int(len(ref_trades)),
    "optimised_rows": int(len(opt_trades)),
    "reference_sha256": ref_h,
    "optimised_sha256": opt_h,
    "frames_byte_identical": ref_h == opt_h,
    "column_order_identical": list(ref_trades.columns) == list(opt_trades.columns),
    "hot_path_reference": ref_hot,
    "hot_path_optimised": opt_hot,
}

# Per-field comparison, exact, no tolerance.
FIELDS = ["trade_id", "base_trade_id", "ob_id", "detection_time", "direction",
          "structure_tag", "entry", "stop", "tp", "fill_time", "exit_time",
          "outcome", "pnl_r", "gross_r", "risk", "rr_multiple",
          "max_distance_away_before_fill_price",
          "max_distance_away_before_fill_pips",
          "max_distance_away_before_fill_r",
          "trigger_time", "trigger_candle_index", "armed_at",
          "fill_candle_index", "fill_delay_candles"]
field_report = {}
for f in FIELDS:
    if f in ref_trades.columns and f in opt_trades.columns:
        a = ref_trades[f].astype(str).tolist()
        b = opt_trades[f].astype(str).tolist()
        diff = sum(1 for x, y in zip(a, b) if x != y)
        field_report[f] = {"present": True, "mismatches": diff,
                           "len_equal": len(a) == len(b)}
    else:
        field_report[f] = {"present": False}
report["fields"] = field_report
report["any_field_mismatch"] = any(
    v.get("mismatches", 0) for v in field_report.values())

# The three metrics the accumulator populates — called out explicitly.
report["metric_columns_identical"] = all(
    field_report[f]["mismatches"] == 0
    for f in ("max_distance_away_before_fill_price",
              "max_distance_away_before_fill_pips",
              "max_distance_away_before_fill_r")
    if field_report[f].get("present"))

# Hot-path counter comparison: semantic counters must be IDENTICAL.
report["counters_identical"] = ref_hot == opt_hot
report["counter_delta"] = {k: opt_hot.get(k, 0) - ref_hot.get(k, 0)
                           for k in sorted(set(ref_hot) | set(opt_hot))}

OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k: v for k, v in report.items()
                  if k not in ("fields", "hot_path_reference", "hot_path_optimised")},
                 indent=2))
print("field mismatches:", {k: v.get("mismatches") for k, v in field_report.items()
                            if v.get("mismatches")})
print("VERDICT:", "PASS" if (report["frames_byte_identical"]
                             and not report["any_field_mismatch"]
                             and report["counters_identical"]) else "FAIL")
