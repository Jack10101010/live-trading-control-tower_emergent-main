"""M-CAP-OPT-3 — four-mode canonical equivalence.

reference (both off) is the ORACLE. All four modes must produce a byte-identical
trade frame, identical hot-path counters, and identical values in the three
pending-metric columns.
"""
import hashlib, json, sys, time
from dataclasses import replace
from pathlib import Path

WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(WT)); import os; os.chdir(WT)
import pandas as pd, scripts.run_backtest as rb, strategy_core as core
import strategy_core.execution as ex

OUT = Path(sys.argv[1]); OUT.parent.mkdir(parents=True, exist_ok=True)

GOLDEN = WT/"generated_configs"/"d6cdae589b1e4c37a67763253c466067.json"
ov,_ = rb.load_config_overrides(str(GOLDEN))
cfg = replace(replace(rb.ACTIVE_CONFIG, **ov), end_date="2026-06-19",
              profile_simulation_hot_path=True)

print("building stages once...", flush=True)
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

def run(te, mp):
    ex.TE_GATE_ENABLED, ex.METRIC_PREFIX_ENABLED = te, mp
    job = {"kind":"entry","execution_mode":m,"scenario":e[0],
           "job_id":f"{m}:entry:{e[0]['key']}","label":e[0]["key"]}
    t=time.monotonic()
    out = rb.execute_scenario_job(job, cfg, c, sim, obs, nc)
    dt = time.monotonic()-t
    res = out["results"][0]; tr = res["trades"]
    return tr, (res["summary"].get("hot_path_profile") or {}), dt

MODES = [("reference", False, False), ("optionA", True, False),
         ("optionB", False, True), ("optionA+B", True, True)]
frames, report = {}, {"modes": {}}
for name, te, mp in MODES:
    print(f"running {name} (TE={te}, MP={mp})...", flush=True)
    tr, hot, dt = run(te, mp)
    b = tr.to_csv(index=False).encode()
    h = hashlib.sha256(b).hexdigest()
    frames[name] = tr
    report["modes"][name] = {"sha256": h, "rows": int(len(tr)),
                             "seconds": round(dt,2), "hot_path": hot}
    print(f"   sha={h[:16]}  rows={len(tr)}  {dt:.1f}s", flush=True)

oracle = "reference"
oh = report["modes"][oracle]["sha256"]
report["all_byte_identical"] = all(v["sha256"] == oh for v in report["modes"].values())
report["counters_identical"] = all(v["hot_path"] == report["modes"][oracle]["hot_path"]
                                   for v in report["modes"].values())

METRICS = ["max_distance_away_before_fill_price",
           "max_distance_away_before_fill_pips",
           "max_distance_away_before_fill_r"]
FIELDS = ["trade_id","base_trade_id","ob_id","detection_time","direction",
          "structure_tag","entry","stop","tp","fill_time","exit_time","outcome",
          "pnl_r","gross_r","risk","rr_multiple","trigger_time",
          "trigger_candle_index","armed_at","fill_candle_index",
          "fill_delay_candles"] + METRICS
field_mm = {}
ref = frames[oracle]
for name in frames:
    if name == oracle: continue
    mm = {}
    for f in FIELDS:
        if f in ref.columns and f in frames[name].columns:
            a = ref[f].astype(str).tolist(); b2 = frames[name][f].astype(str).tolist()
            d = sum(1 for x,y in zip(a,b2) if x != y)
            if d: mm[f] = d
    field_mm[name] = mm
report["field_mismatches_vs_reference"] = field_mm
report["any_field_mismatch"] = any(bool(v) for v in field_mm.values())

# per-outcome metric check, so a class-specific error cannot hide in aggregate
by_outcome = {}
for name in frames:
    if name == oracle: continue
    per = {}
    for oc in sorted(set(ref["outcome"].astype(str))):
        mask = ref["outcome"].astype(str) == oc
        d = sum(1 for x,y in zip(ref.loc[mask, METRICS[0]].astype(str),
                                 frames[name].loc[mask, METRICS[0]].astype(str)) if x != y)
        per[oc] = {"rows": int(mask.sum()), "metric_mismatches": d}
    by_outcome[name] = per
report["metric_by_outcome"] = by_outcome

OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k:v for k,v in report.items()
                  if k not in ("modes","metric_by_outcome")}, indent=2))
print("\nper-outcome metric mismatches (optionA+B):")
print(json.dumps(by_outcome.get("optionA+B", {}), indent=2))
ok = report["all_byte_identical"] and not report["any_field_mismatch"] and report["counters_identical"]
print("\nVERDICT:", "PASS" if ok else "FAIL")
