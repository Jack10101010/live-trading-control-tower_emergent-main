"""Warm determinism: same process, pipeline run TWICE, hashes compared."""
import hashlib, json, sys, time
from pathlib import Path
CT = Path("/Users/jack/Documents/Dev Projects/ct-capacity-consolidate")
WT = Path("/Users/jack/Documents/Dev Projects/lux-cap-opt-2")
sys.path.insert(0, str(CT))
from live.config import LiveConfig
from live.runner import LiveRunner, LuxSession
import pandas as pd
GOLDEN = "b43e32489453ff8f7d664014a471e67b11413e185d8bc203f345014acdac2f6e"
s = LuxSession(WT); s.verify_engine()
cfg = LiveConfig(lux_root=WT)
candles = pd.read_csv(WT/"data"/"candles"/"EURUSD_1m_extended_2015_2026.csv")
r = LiveRunner(cfg, session=s)
out = []
for i in (1, 2):
    t = time.monotonic()
    tr = r.golden_pipeline(candles, "2026-06-19")
    f = tr.astype(str)
    h = hashlib.sha256(f.to_csv(index=False).encode()).hexdigest()
    out.append({"iteration": i, "seconds": round(time.monotonic()-t, 2),
                "rows": int(len(f)), "sha256": h, "matches_golden": h == GOLDEN})
    print(f"  warm iter {i}: {out[-1]['seconds']}s rows={out[-1]['rows']} "
          f"sha={h[:16]} golden={out[-1]['matches_golden']}", flush=True)
rep = {"label": "warm-same-process", "iterations": out,
       "deterministic_across_iterations": len({o["sha256"] for o in out}) == 1,
       "all_match_golden": all(o["matches_golden"] for o in out)}
Path(sys.argv[1]).write_text(json.dumps(rep, indent=2))
print(json.dumps({k:v for k,v in rep.items() if k!="iterations"}, indent=2))
sys.exit(0 if rep["deterministic_across_iterations"] and rep["all_match_golden"] else 1)
