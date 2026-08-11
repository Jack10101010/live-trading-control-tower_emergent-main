"""Population parity audit: every order block in a window, Python vs Pine.

    python -m tools.oracle.population_audit --start 2026-06-23 --end 2026-08-01 --write

WHY A POPULATION AND NOT A CASE
-------------------------------
Chasing one trade at a time explains one trade at a time. This runs the whole
window and buckets every disagreement by its FIRST divergent stage, so the
answer is "these N blocks differ, for these M reasons" rather than a story.

WHAT IS COMPARED
----------------
PYTHON AUTHORITY is the seam-enabled replay recording — production's own
`simulate_trades` output, not a re-derivation.

PINE ORACLE is the live layer's state machine, run through the transcription
that `backend/tests/test_oracle_live_lifecycle.py` keeps pinned to the fragment.
If the fragment drifts, that test fails before this audit can report anything.

BOTH SIDES ARE FED THE SAME BARS. That is deliberate: it removes the FEED as a
variable, which is the only way to attribute a divergence to a STAGE. The
consequence is stated rather than hidden — this audit CANNOT observe class A
(candle/feed) divergence, and says so in its own output.

TWO VOCABULARY TRAPS, both of which produced false mismatches on the first run
and are now derived from production rather than hand-written:

  * production reports the session LABEL ("NY PM"), Pine the cohort KEY
    ("ny_pm"). The map comes from `_SESSION_SCHEDULE`.
  * `resample_candles` returns a TIME COLUMN, not a time index. Feeding the
    regime replay a `reset_index()` row number made every panel date a row
    number, every state lookup miss, and every cell fall back to its BASE
    target — which put a plausible-looking 2.0R on almost every Pine row.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from tools.oracle import replay_prefill as rp
from tools.oracle import replay_regime
from tools.oracle import target_table as tt
from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

DEFAULT_OUT = (CT_ROOT / "artifacts" / "tradingview_oracle"
               / "population_audit.json")

#: Python lifecycle -> the Pine phases that mean the same thing.
EQUIV = {"PENDING": {"RESTING", "ARMED"}, "BLOCKED": {"BLOCKED"},
         "INVALIDATED": {"INVALIDATED"}, "WIN": {"WIN"}, "LOSS": {"LOSS"},
         "OPEN": {"OPEN"}, "BREAKEVEN": {"WIN", "LOSS", "OPEN"}}

#: First-divergence classes, as briefed.
CLASSES = {
    "A": "candle/feed", "B": "OB detection", "C": "structure/direction",
    "D": "invalidation", "E": "arm", "F": "fill/delay", "G": "fill session",
    "H": "fill market state", "I": "matrix eligibility", "J": "RR/TP",
    "K": "outcome", "L": "visual retention/rendering", "M": "other",
}


def _py_lifecycle(s: dict) -> str:
    """Production's result, in the audit's vocabulary."""
    if (s.get("reason") or "") == "invalidated_before_edge_entry":
        return "INVALIDATED"
    st, out = s["status"], s.get("outcome", "")
    if st == "COMPLETED":
        return {"WIN": "WIN", "LOSS": "LOSS"}.get(out, "BREAKEVEN")
    return {"BLOCKED": "BLOCKED", "PENDING": "PENDING",
            "FILLED": "OPEN"}.get(st, st)


def classify(r: dict, label_to_key: dict) -> tuple[str, str]:
    """(verdict, first-divergence class). UNDECIDABLE is neither a match nor a
    mismatch — it is the chart declining to claim what it cannot prove."""
    if r["py"] == "MISSING":
        return "MISSING_ON_PYTHON", "B"
    if r["pine"] == "UNDECIDABLE":
        return "UNDECIDABLE", "-"
    if r["pine"] in EQUIV.get(r["py"], set()):
        if (r["py_trade"] and r["pine_rr"] is not None
                and r["py_rr"] is not None
                and abs(r["pine_rr"] - r["py_rr"]) > 1e-9):
            return "MISMATCH", "J"
        if (r["py_trade"] and r["py_sess"] and r["pine_sess"]
                and label_to_key.get(r["py_sess"], r["py_sess"])
                != r["pine_sess"]):
            return "MISMATCH", "G"
        return "MATCH", "-"
    if "INVALIDATED" in (r["py"], r["pine"]):
        return "MISMATCH", "D"
    if r["py"] == "PENDING" or r["pine"] in ("RESTING", "ARMED"):
        return "MISMATCH", "F"
    if "BLOCKED" in (r["py"], r["pine"]):
        return "MISMATCH", "I"
    if {r["py"], r["pine"]} <= {"WIN", "LOSS", "OPEN", "BREAKEVEN"}:
        return "MISMATCH", "K"
    return "MISMATCH", "M"


def build(engine, cfg, start: str, end: str) -> dict:
    import pandas as pd

    sys.path.insert(0, str(CT_ROOT / "backend" / "tests"))
    from test_oracle_live_lifecycle import (ARMED, INVALID, LOSS, NEWS_UNKNOWN,
                                            NOTRADE, RESTING, TRADE, UNDECID,
                                            WIN, Block, Ctx, run)

    ext_path = (CT_ROOT / "artifacts" / "tradingview_oracle"
                / "execution_feed_extension.csv")
    ext = pd.read_csv(ext_path) if ext_path.is_file() else None
    prepared, obs, _news, seam = rp._load(engine, cfg, None, None,
                                          extension=ext)

    lo = pd.Timestamp(start, tz="UTC")
    hi = pd.Timestamp(end, tz="UTC")

    with _in_lux(Path(engine.lux_root)):
        from strategy_core.sessions import (_SESSION_OUTSIDE, _SESSION_SCHEDULE,
                                            _london_hour, _session_for_hour)
        label_to_key = {lab: key for _s, _e, lab, key in _SESSION_SCHEDULE}
        label_to_key[_SESSION_OUTSIDE[0]] = _SESSION_OUTSIDE[1]
        detection = engine.rb.resample_candles(prepared, cfg.detection_timeframe)
        assert "time" in detection.columns, detection.columns.tolist()
        _daily, panel = replay_regime.replay(
            engine.core, [str(t) for t in detection["time"]],
            detection["open"].tolist(), detection["high"].tolist(),
            detection["low"].tolist(), detection["close"].tolist(),
            None, "EURUSD")

    state_by_day = {r["date"]: (r.get("market_state"), bool(r.get("confirmed")))
                    for r in panel}
    if not any("-" in k for k in state_by_day):
        raise RuntimeError("panel dates are not dates — every state lookup "
                           "would silently miss and every cell would fall "
                           "back to its base target")

    table = tt.build(engine, cfg)
    bars = detection.set_index(pd.to_datetime(detection["time"], utc=True))

    rec = json.loads((CT_ROOT / "artifacts" / "tradingview_oracle"
                      / "replay_setups.json").read_text(encoding="utf-8"))
    py_by = {(s["detected_ms"], s["direction"], s["structure"]): s
             for s in rec["setups"]}

    pine_name = {RESTING: "RESTING", ARMED: "ARMED", TRADE: "OPEN",
                 NOTRADE: "BLOCKED", WIN: "WIN", LOSS: "LOSS",
                 INVALID: "INVALIDATED", UNDECID: "UNDECIDABLE"}

    class RealCtx(Ctx):
        def __init__(self, times, is_choch, is_long):
            self.t, self.choch, self.long = times, is_choch, is_long

        def _cell(self, i):
            ts = self.t[i]
            key = _session_for_hour(_london_hour(ts))[1]
            state, conf = state_by_day.get(ts.strftime("%Y-%m-%d"),
                                           (None, False))
            rr, _src, ok, _why = tt.resolve(
                table, key, "CHoCH" if self.choch else "BOS",
                "Long" if self.long else "Short", state, conf)
            return rr, ok, key, (state or "(warmup)")

        def __call__(self, i):
            rr, ok, _k, _s = self._cell(i)
            return rr, ok, NEWS_UNKNOWN

        def sess(self, i):
            return self._cell(i)[2]

        def state(self, i):
            return self._cell(i)[3]

    frame = pd.DataFrame(obs)
    frame["detection_time"] = pd.to_datetime(frame["detection_time"], utc=True)
    window = frame[(frame.detection_time >= lo) & (frame.detection_time < hi)]
    idx = list(bars.index)
    buf = float(cfg.stop_buffer_pips) * float(cfg.pip_size)

    rows = []
    for _, ob in window.iterrows():
        det = ob.detection_time
        first = next(i for i, t in enumerate(idx) if t >= det)
        sub = bars.iloc[first:]
        is_long = ob.direction == "bullish"
        is_choch = "choch" in str(ob.structure_tag).lower()
        o = run(Block(ob.top, ob.bottom, is_long, stop_buffer=buf),
                [(r.high, r.low) for r in sub.itertuples()],
                RealCtx(list(sub.index), is_choch, is_long))
        py = py_by.get((int(det.value // 1_000_000), 1 if is_long else 2,
                        2 if is_choch else 1))
        row = {
            "detected": det.strftime("%Y-%m-%d %H:%M"),
            "direction": "Long" if is_long else "Short",
            "structure": "CHoCH" if is_choch else "BOS",
            "top": float(ob.top), "bottom": float(ob.bottom),
            "py": _py_lifecycle(py) if py else "MISSING",
            "py_rr": (py or {}).get("rr"),
            "py_sess": (py or {}).get("session"),
            "py_state": (py or {}).get("market_state"),
            "py_trade": bool(py) and _py_lifecycle(py) in (
                "WIN", "LOSS", "OPEN", "BREAKEVEN"),
            "pine": pine_name[o.phase],
            "pine_rr": o.final.get("rr"),
            "pine_sess": o.final.get("sess") or "",
            "pine_state": o.final.get("state") or "",
            "pine_trade": o.phase in (TRADE, WIN, LOSS),
        }
        row["verdict"], row["cls"] = classify(row, label_to_key)
        rows.append(row)

    # ── METRIC A: what the chart ACTUALLY RENDERS ──────────────────────────
    #
    # Inside the replay's draw window production owns the lifecycle, and its
    # answer is by construction its own — so parity there is exact and the only
    # question is whether the detection matched. Beyond it the live layer owns
    # the lifecycle and Metric B applies.
    #
    # ── METRIC B: COUNTERFACTUAL M15 DECIDABILITY ──────────────────────────
    #
    # What the live layer WOULD infer if the recording did not exist. This is a
    # DIAGNOSTIC — a measurement of the information lost reproducing a 1-minute
    # execution model from 15-minute bars — and it does NOT describe what the
    # chart draws wherever replay authority is active. Reported separately for
    # exactly that reason.
    detected_ms = sorted(s["detected_ms"] for s in rec["setups"])
    draw_first = detected_ms[max(0, len(detected_ms) - 50)] if detected_ms else 0
    for r in rows:
        d = int(pd.Timestamp(r["detected"], tz="UTC").value // 1_000_000)
        r["rendered_by"] = ("REPLAY" if (rec["setups"] and d >= draw_first
                                         and r["py"] != "PENDING")
                            else "LIVE")

    by_py = Counter(r["py"] for r in rows)
    replay_owned = sum(1 for r in rows if r["rendered_by"] == "REPLAY")
    return {
        "schema": "tradingview-oracle-population-audit-v1",
        "window": {"start": start, "end": end},
        "seam": seam,
        "_basis": "Both sides read the SAME bars, so class A (candle/feed) "
                  "cannot be observed here and remains unproven.",
        "rows": rows,
        "totals": {
            "python_obs": sum(1 for r in rows if r["py"] != "MISSING"),
            "pine_obs": len(rows),
            "matched_detections": sum(1 for r in rows if r["py"] != "MISSING"),
            "missing_on_python": sum(1 for r in rows if r["py"] == "MISSING"),
            "missing_on_pine": 0,
            "lifecycle_exact": sum(1 for r in rows if r["verdict"] == "MATCH"),
            "undecidable": sum(1 for r in rows
                               if r["verdict"] == "UNDECIDABLE"),
            "mismatches": sum(1 for r in rows if r["verdict"] == "MISMATCH"),
            "by_python_lifecycle": {
                k: {"total": n,
                    "exact": sum(1 for r in rows
                                 if r["py"] == k and r["verdict"] == "MATCH"),
                    "undecidable": sum(1 for r in rows if r["py"] == k
                                       and r["verdict"] == "UNDECIDABLE"),
                    "mismatch": sum(1 for r in rows if r["py"] == k
                                    and r["verdict"] == "MISMATCH")}
                for k, n in by_py.items()},
            "mismatch_by_class": dict(Counter(
                r["cls"] for r in rows if r["verdict"] == "MISMATCH")),
        },
        "metric_a_rendered_authority": {
            "_what": "What the chart ACTUALLY draws. Inside the replay draw "
                     "window production owns the lifecycle and its answer is "
                     "authoritative by construction.",
            "replay_owned": replay_owned,
            "live_owned": len(rows) - replay_owned,
            "detections_matched": sum(1 for r in rows if r["py"] != "MISSING"),
            "detections_total": len(rows),
            "contradictions": sum(1 for r in rows
                                  if r["verdict"] == "MISMATCH"),
        },
        "metric_b_counterfactual_m15_decidability": {
            "_what": "DIAGNOSTIC ONLY. What the live layer WOULD infer with no "
                     "recording — a measurement of the information lost "
                     "reproducing a 1-minute execution model from 15-minute "
                     "bars. It does NOT describe what the chart renders "
                     "wherever replay authority is active.",
            "exact_from_m15": sum(1 for r in rows if r["verdict"] == "MATCH"),
            "undecidable_from_m15": sum(1 for r in rows
                                        if r["verdict"] == "UNDECIDABLE"),
            "contradictions": sum(1 for r in rows
                                  if r["verdict"] == "MISMATCH"),
        },
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2026-06-23")
    ap.add_argument("--end", default="2026-08-01")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)
    out = build(engine, cfg, args.start, args.end)

    t = out["totals"]
    hdr = (f"{'detected':17s} {'dir':5s} {'struct':6s} {'python':12s} "
           f"{'pine':12s} {'pyRR':>5s} {'pnRR':>5s} {'verdict':10s} cls")
    print(hdr)
    print("-" * len(hdr))
    for r in out["rows"]:
        print(f"{r['detected']:17s} {r['direction']:5s} {r['structure']:6s} "
              f"{r['py']:12s} {r['pine']:12s} "
              f"{'' if r['py_rr'] is None else format(r['py_rr'], '.2f'):>5s} "
              f"{'' if r['pine_rr'] is None else format(r['pine_rr'], '.2f'):>5s} "
              f"{r['verdict']:10s} {r['cls']}")

    print(f"\nPython OBs {t['python_obs']}   Pine OBs {t['pine_obs']}   "
          f"matched {t['matched_detections']}")
    print(f"exact {t['lifecycle_exact']}   undecidable {t['undecidable']}   "
          f"mismatch {t['mismatches']}   "
          f"missing py {t['missing_on_python']} / pine {t['missing_on_pine']}")
    for k, v in t["by_python_lifecycle"].items():
        print(f"  {k:12s} {v['total']:3d}  exact {v['exact']:3d}  "
              f"undecidable {v['undecidable']:3d}  mismatch {v['mismatch']:3d}")
    for k, n in sorted(t["mismatch_by_class"].items()):
        print(f"  class {k} ({CLASSES.get(k, '?')}): {n}")

    a = out["metric_a_rendered_authority"]
    b = out["metric_b_counterfactual_m15_decidability"]
    print("\nMETRIC A — what the chart RENDERS")
    print(f"  replay-owned {a['replay_owned']}   live-owned {a['live_owned']}"
          f"   detections {a['detections_matched']}/{a['detections_total']}"
          f"   contradictions {a['contradictions']}")
    print(f"METRIC B — counterfactual M15 decidability (DIAGNOSTIC)")
    print(f"  exact {b['exact_from_m15']}   undecidable "
          f"{b['undecidable_from_m15']}   contradictions {b['contradictions']}")
    print("  This is what the live layer WOULD infer with no recording. It does")
    print("  NOT describe what the chart draws where replay authority is active.")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=1) + "\n", encoding="utf-8")
        print(f"\nwritten: {args.out.relative_to(CT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
