"""M-CAP-OPT-3 Phase B6 — clean interleaved four-mode A/B.

Same method that corrected the invalid Option A measurement:
  * pre-stages built ONCE, reused by every iteration;
  * modes INTERLEAVED round-robin so monotonic drift hits all arms equally;
  * one warm-up round discarded;
  * trade-frame SHA-256 verified every single iteration, so no timing run can
    hide a divergence;
  * hot-path counters OFF (they cost ~16% and are not part of the dfd3e7d
    methodology); call counts are reported separately from instrumented runs.
"""
import hashlib, json, resource, statistics, sys, time
from dataclasses import replace
from pathlib import Path

WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(WT)); import os; os.chdir(WT)
import pandas as pd, scripts.run_backtest as rb, strategy_core as core
import strategy_core.execution as ex

OUT = Path(sys.argv[1]); OUT.parent.mkdir(parents=True, exist_ok=True)
ROUNDS = int(sys.argv[2]) if len(sys.argv) > 2 else 5

GOLDEN = WT/"generated_configs"/"d6cdae589b1e4c37a67763253c466067.json"
ov,_ = rb.load_config_overrides(str(GOLDEN))
cfg = replace(replace(rb.ACTIVE_CONFIG, **ov), end_date="2026-06-19",
              profile_simulation_hot_path=False)

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
e = [x for x in rb.entry_scenarios(cfg) if x["mode"]=="triggered_edge"][0]
m = cfg.execution_modes[0]

MODES = [("reference", False, False), ("optionA", True, False),
         ("optionB", False, True), ("optionA+B", True, True)]

def one(te, mp):
    ex.TE_GATE_ENABLED, ex.METRIC_PREFIX_ENABLED = te, mp
    job = {"kind":"entry","execution_mode":m,"scenario":e,
           "job_id":f"{m}:entry:{e['key']}","label":e["key"]}
    ru0 = resource.getrusage(resource.RUSAGE_SELF)
    t = time.monotonic()
    out = rb.execute_scenario_job(job, cfg, c, sim, obs, nc)
    dt = time.monotonic() - t
    ru1 = resource.getrusage(resource.RUSAGE_SELF)
    tr = out["results"][0]["trades"]
    h = hashlib.sha256(tr.to_csv(index=False).encode()).hexdigest()
    return {"seconds": dt,
            "cpu_user_s": ru1.ru_utime - ru0.ru_utime,
            "cpu_sys_s": ru1.ru_stime - ru0.ru_stime,
            "peak_rss_mib": ru1.ru_maxrss / (1024*1024),  # macOS reports bytes
            "sha256": h, "rows": int(len(tr))}

print("warm-up round (discarded)...", flush=True)
for name, te, mp in MODES:
    r = one(te, mp); print(f"  warmup {name}: {r['seconds']:.1f}s", flush=True)

samples = {n: [] for n,_,_ in MODES}
hashes, rows_seen, order = set(), set(), []
for rd in range(ROUNDS):
    for name, te, mp in MODES:
        r = one(te, mp)
        samples[name].append(r)
        hashes.add(r["sha256"]); rows_seen.add(r["rows"])
        order.append({"round": rd, "mode": name, **r})
        print(f"  round {rd} {name}: {r['seconds']:.1f}s "
              f"cpu={r['cpu_user_s']:.1f}s rss={r['peak_rss_mib']:.0f}MiB", flush=True)

assert len(hashes) == 1, f"OUTPUT DIVERGED DURING TIMING: {hashes}"
assert len(rows_seen) == 1, rows_seen

def stat(key, vals):
    xs = [v[key] for v in vals]
    return {"n": len(xs), "mean": statistics.mean(xs), "p50": statistics.median(xs),
            "min": min(xs), "max": max(xs),
            "stdev": statistics.stdev(xs) if len(xs) > 1 else None}

report = {"design": "interleaved round-robin, 1 warm-up round discarded, "
                    "counters OFF, hash verified every iteration",
          "rounds": ROUNDS, "trades_sha256": hashes.pop(), "rows": rows_seen.pop(),
          "modes": {n: {"seconds": stat("seconds", v),
                        "cpu_user_s": stat("cpu_user_s", v),
                        "peak_rss_mib": max(x["peak_rss_mib"] for x in v)}
                    for n, v in samples.items()},
          "samples": order}

ref = report["modes"]["reference"]["seconds"]["mean"]
a   = report["modes"]["optionA"]["seconds"]["mean"]
ab  = report["modes"]["optionA+B"]["seconds"]["mean"]
b   = report["modes"]["optionB"]["seconds"]["mean"]
report["savings_vs_reference_pct"] = {
    "optionA":   (ref-a)/ref*100, "optionB": (ref-b)/ref*100,
    "optionA+B": (ref-ab)/ref*100}
report["optionB_increment_over_optionA_pct"] = (a-ab)/a*100
report["optionB_increment_over_optionA_s"] = a-ab

OUT.write_text(json.dumps(report, indent=2))
print(json.dumps({k:v for k,v in report.items() if k != "samples"}, indent=2))
