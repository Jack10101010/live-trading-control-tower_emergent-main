"""M-CAP-REPIN-1 Phase 4/5 — Golden parity + identity-consumer validation.

Runs the canonical full-history pipeline through the REAL production entry point
(LuxSession + LiveRunner.golden_pipeline), so verify_engine, LuxSession binding
and the driver contract are all exercised — not just execute_scenario_job.

usage: repin_parity.py <out.json> <label>
"""
import hashlib, json, sys, time
from pathlib import Path

CT = Path("/Users/jack/Documents/Dev Projects/ct-capacity-consolidate")
WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(CT))

OUT = Path(sys.argv[1]); OUT.parent.mkdir(parents=True, exist_ok=True)
LABEL = sys.argv[2] if len(sys.argv) > 2 else "run"

from live.config import ENGINE_VERSION_EXPECTED, LiveConfig
from live.runner import LiveRunner, LuxSession, PhaseProfiler

GOLDEN_HASH = "b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e"
GOLDEN_ROWS = 2060
FRONTIER = "2026-06-19"

rep = {"label": LABEL, "pin": ENGINE_VERSION_EXPECTED}

# ── identity consumers ──────────────────────────────────────────────────────
t0 = time.monotonic()
session = LuxSession(WT)                      # LuxSession binding + chdir contract
rep["session_engine_version"] = session.engine_version
try:
    session.verify_engine()                   # THE GATE
    rep["verify_engine"] = "ACCEPTED"
except RuntimeError as exc:
    rep["verify_engine"] = f"REFUSED: {exc}"
rep["pin_matches_session"] = session.engine_version == ENGINE_VERSION_EXPECTED

# engine_source_manifest / manifest id, recomputed here too
from src.run_outputs import engine_source_manifest, engine_version, ENGINE_VERSION_POLICY
man = engine_source_manifest()
rep["policy"] = ENGINE_VERSION_POLICY
rep["governed_files"] = len(man)
rep["engine_version_recomputed"] = engine_version()
rep["manifest_id"] = hashlib.sha256(
    json.dumps(man, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

# input_revision must MOVE with engine identity
from live.runner import _input_revision_preimage
pre_old = _input_revision_preimage("aa", "bb",
    "5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be")
pre_new = _input_revision_preimage("aa", "bb", session.engine_version)
rep["input_revision_moves_with_engine"] = (
    hashlib.sha256(pre_old.encode()).hexdigest()
    != hashlib.sha256(pre_new.encode()).hexdigest())

# ── Golden parity through the production pipeline ───────────────────────────
cfg = LiveConfig(lux_root=WT)
import pandas as pd
frozen = WT / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
candles = pd.read_csv(frozen)
rep["dataset_rows"] = int(len(candles))
rep["dataset_sha256"] = hashlib.sha256(frozen.read_bytes()).hexdigest()

runner = LiveRunner(cfg, session=session)
prof = PhaseProfiler()
t1 = time.monotonic()
trades = runner.golden_pipeline(candles, FRONTIER, profiler=prof)
rep["pipeline_seconds"] = round(time.monotonic() - t1, 2)
rep["setup_seconds"] = round(t1 - t0, 2)

frame = trades.astype(str)
rep["rows"] = int(len(frame))
rep["sha256"] = hashlib.sha256(frame.to_csv(index=False).encode()).hexdigest()
rep["rows_match"] = rep["rows"] == GOLDEN_ROWS
rep["hash_match"] = rep["sha256"] == GOLDEN_HASH
rep["phase_timings"] = prof.snapshot()["phase_timings"]
rep["outcomes"] = {str(k): int(v) for k, v in frame["outcome"].value_counts().items()}

OUT.write_text(json.dumps(rep, indent=2))
print(json.dumps({k: v for k, v in rep.items() if k != "phase_timings"}, indent=2))
ok = rep["hash_match"] and rep["rows_match"] and rep["verify_engine"] == "ACCEPTED"
print("VERDICT:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
