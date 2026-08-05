"""CAP-5 hard-gate probe — empirical ob_id stability test.

Read-only. Imports the production engine and calls detect_order_blocks on
differently-windowed inputs. Writes nothing into either repository.

Three experiments:
  A. APPEND      — full frame vs frame truncated at the END.
                   Tests: does extending the window preserve existing ob_ids?
  B. WINDOW-START— full frame vs frame truncated at the START.
                   Tests: is ob_id window-relative?
  C. PARAMETER   — full frame, min_ob_size_pips perturbed.
                   Tests: is ob_id parameter-relative?
"""
import json
import sys
from pathlib import Path

LUX = Path("/Users/jack/Documents/Dev Projects/Lux-OB-Backtester")
sys.path.insert(0, str(LUX))
import os
os.chdir(LUX)

import pandas as pd
import scripts.run_backtest as rb
import strategy_core as core

OUT = Path(sys.argv[1])

# Golden config — exactly the path LiveConfig.golden_config_path resolves
# (live/config.py:15 GOLDEN_CONFIG_RELPATH), rooted at the Lux repo.
GOLDEN = LUX / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"
if not GOLDEN.exists():
    raise SystemExit(f"golden config not found: {GOLDEN}")

overrides, _ = rb.load_config_overrides(str(GOLDEN))
from dataclasses import replace
config = replace(rb.ACTIVE_CONFIG, **overrides)

frozen = LUX / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
print("loading candles...", flush=True)
candles_raw = pd.read_csv(frozen)
print("rows:", len(candles_raw), flush=True)

candles = rb.filter_date_range(candles_raw, config)
candles = core.prepare_candles_for_simulation(candles)
detection_full = rb.resample_candles(candles, config.detection_timeframe)
print("M15 detection bars:", len(detection_full), flush=True)


def detect(frame, *, min_pips=None):
    return core.detect_order_blocks(
        frame,
        swing_length=config.swing_length,
        ob_filter=config.ob_filter,
        pip_size=config.pip_size,
        min_ob_size_pips=config.min_ob_size_pips if min_pips is None else min_pips,
        max_ob_size_pips=config.max_ob_size_pips,
    )


def key(df):
    """Identity view: ob_id -> the OB's intrinsic coordinates."""
    return {int(r["ob_id"]): (str(r["origin_time"]), str(r["detection_time"]),
                              str(r["direction"]), round(float(r["top"]), 6),
                              round(float(r["bottom"]), 6))
            for _, r in df.iterrows()}


results = {}

print("A: full frame", flush=True)
full = detect(detection_full)
k_full = key(full)
results["full_ob_count"] = len(full)

# ── A. APPEND: truncate the END by 500 M15 bars (~5 days) ───────────────────
print("A: truncated-end frame", flush=True)
trunc_end = detection_full.iloc[:-500].reset_index(drop=True)
a = detect(trunc_end)
k_a = key(a)
shared = set(k_a) & set(k_full)
a_mismatch = [i for i in sorted(shared) if k_a[i] != k_full[i]]
results["A_append"] = {
    "truncated_ob_count": len(a),
    "shared_ids": len(shared),
    "mismatched_ids": len(a_mismatch),
    "first_mismatches": a_mismatch[:5],
    "prefix_stable": len(a_mismatch) == 0,
}

# ── B. WINDOW-START: drop the first 5000 M15 bars ───────────────────────────
print("B: truncated-start frame", flush=True)
trunc_start = detection_full.iloc[5000:].reset_index(drop=True)
b = detect(trunc_start)
k_b = key(b)
shared_b = set(k_b) & set(k_full)
b_mismatch = [i for i in sorted(shared_b) if k_b[i] != k_full[i]]
# also: does the same OB (by origin_time) keep its id?
by_origin_full = {v[0]: i for i, v in k_full.items()}
by_origin_b = {v[0]: i for i, v in k_b.items()}
common_origins = set(by_origin_full) & set(by_origin_b)
id_changed = [o for o in common_origins if by_origin_full[o] != by_origin_b[o]]
results["B_window_start"] = {
    "truncated_ob_count": len(b),
    "shared_ids": len(shared_b),
    "mismatched_ids": len(b_mismatch),
    "common_origin_times": len(common_origins),
    "same_ob_different_id": len(id_changed),
    "window_relative": len(id_changed) > 0 or len(b_mismatch) > 0,
}

# ── C. PARAMETER: perturb min_ob_size_pips ──────────────────────────────────
print("C: perturbed min_ob_size_pips", flush=True)
base_min = float(config.min_ob_size_pips or 0.0)
c = detect(detection_full, min_pips=base_min + 1.0)
k_c = key(c)
shared_c = set(k_c) & set(k_full)
c_mismatch = [i for i in sorted(shared_c) if k_c[i] != k_full[i]]
by_origin_c = {v[0]: i for i, v in k_c.items()}
common_origins_c = set(by_origin_full) & set(by_origin_c)
id_changed_c = [o for o in common_origins_c if by_origin_full[o] != by_origin_c[o]]
results["C_parameter"] = {
    "base_min_ob_size_pips": base_min,
    "perturbed_to": base_min + 1.0,
    "perturbed_ob_count": len(c),
    "shared_ids": len(shared_c),
    "mismatched_ids": len(c_mismatch),
    "same_ob_different_id": len(id_changed_c),
    "parameter_relative": len(id_changed_c) > 0 or len(c_mismatch) > 0,
}

OUT.write_text(json.dumps(results, indent=2))
print(json.dumps(results, indent=2))
