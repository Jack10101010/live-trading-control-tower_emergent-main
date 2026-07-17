"""P1 shadow report — aggregates the ops log into the promotion-decision report.

Usage (on the VPS, any time; read-only):
    python -m live.shadow_report [--state-dir <LIVE_STATE_DIR>]
        [--min-bars N] [--max-cycle-time S]

Promotion is EVIDENCE-gated, never calendar-gated: each gate reports PASS/FAIL
against observed data, and the recommendation is PROMOTE only when every gate
passes. Thresholds are configurable (CLI flag > environment > conservative
default):

  gate                        default            env override
  min_observed_bars           480 (~5 FX days)   SHADOW_MIN_OBSERVED_BARS
  max_cycle_time_s            600                SHADOW_MAX_CYCLE_TIME_S
  duplicate_intents == 0      fixed
  missed_bars == 0            fixed
  critical_reconciliation==0  fixed (warnings reported, gate on critical)
  runner_errors == 0          fixed (process crashes surface as missed bars)
  no_boundary_overrun         fixed (no recompute > 900s bar interval)

Writes `<state_dir>/ops/shadow-report.json` and prints a concise summary.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

from live.ops_log import OpsLog
from live.state import RunnerState

FIFTEEN = timedelta(minutes=15)
BAR_INTERVAL_S = 900.0                      # 15m bar — overrun boundary, not configurable
DEFAULT_MIN_OBSERVED_BARS = 480             # ~5 FX trading days of 15m bars
DEFAULT_MAX_CYCLE_TIME_S = 600.0            # headroom under the 900s interval


def gates_from_env(min_bars: int | None = None, max_cycle: float | None = None) -> dict:
    return {
        "min_observed_bars": int(min_bars if min_bars is not None
                                 else os.environ.get("SHADOW_MIN_OBSERVED_BARS",
                                                     DEFAULT_MIN_OBSERVED_BARS)),
        "max_cycle_time_s": float(max_cycle if max_cycle is not None
                                  else os.environ.get("SHADOW_MAX_CYCLE_TIME_S",
                                                      DEFAULT_MAX_CYCLE_TIME_S)),
    }


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))


def classify_gap(prev_boundary: datetime, cur_boundary: datetime) -> str:
    """A boundary step != 15m is a gap. Weekend/market-close spans are expected:
    FX closes Fri ~22:00 UTC and reopens Sun ~22:00 UTC (broker-dependent ±2h).
    Any gap whose span crosses Friday 20:00 UTC -> Sunday 22:00 UTC is treated
    as market-closed; everything else is a MISSED bar window."""
    cursor = prev_boundary
    while cursor < cur_boundary:
        wd, hour = cursor.weekday(), cursor.hour        # Mon=0 .. Sun=6
        in_weekend = (wd == 4 and hour >= 20) or wd == 5 or (wd == 6 and hour < 22)
        if not in_weekend:
            return "missed"
        cursor += FIFTEEN
    return "market_closed"


def build_report(state_dir: Path, gates_cfg: dict | None = None) -> dict:
    gates_cfg = gates_cfg or gates_from_env()
    ops = OpsLog(state_dir)
    cycles = [c for c in ops.read_cycles() if "cycle_start" in c]
    report: dict = {"state_dir": str(state_dir), "cycles": len(cycles),
                    "gates_config": gates_cfg}
    if not cycles:
        report.update({"verdict": "NO_DATA", "gates": [],
                       "recommendation": "remain P1 — no cycles logged"})
        return report

    first, last = _parse(cycles[0]["cycle_start"]), _parse(cycles[-1]["cycle_end"])
    observed = last - first
    report["window"] = {"first_cycle": str(first), "last_cycle": str(last),
                       "observed_days": round(observed.total_seconds() / 86400, 2)}

    # uptime (informational, not a gate — downtime materialises as missed bars)
    beats = sorted(_parse(c["cycle_end"]) for c in cycles)
    covered = timedelta(0)
    for a, b in zip(beats, beats[1:]):
        covered += min(b - a, timedelta(minutes=5))
    report["uptime_pct"] = round(min(100.0 * covered.total_seconds()
                                     / max(observed.total_seconds(), 1), 100.0), 2)

    # processed bars + recompute timing (only cycles that ran the pipeline)
    recompute = [c for c in cycles if c.get("status") in ("ok", "bootstrap")]
    boundaries, seen = [], set()
    for c in recompute:
        b = c.get("boundary")
        if b and b not in seen:
            seen.add(b)
            boundaries.append(_parse(b))
    durations = [float(c["duration_s"]) for c in recompute if c.get("duration_s")]
    max_cycle = max(durations) if durations else 0.0
    report["processed_bars"] = len(boundaries)
    report["cycle_time_s"] = {
        "avg": round(sum(durations) / len(durations), 1) if durations else None,
        "max": round(max_cycle, 1) if durations else None,
        "bar_interval": BAR_INTERVAL_S}

    # missed bars
    missed, closed = [], 0
    for a, b in zip(boundaries, boundaries[1:]):
        if b - a > FIFTEEN:
            if classify_gap(a + FIFTEEN, b) == "missed":
                missed.append({"from": str(a), "to": str(b),
                               "windows": int((b - a) / FIFTEEN) - 1})
            else:
                closed += 1
    report["missed_bars"] = missed
    report["market_closed_gaps"] = closed

    # duplicates, reconciliation, errors
    state = RunnerState(state_dir)
    ledger = state.data.get("ledger", {})
    dup_blocked = sum(1 for v in ledger.values()
                      if v.get("status") == "blocked"
                      and v.get("detail", {}).get("rail") == "duplicate_intent")
    report["intents"] = {"total_ledger": len(ledger), "duplicate_blocked": dup_blocked}
    critical = [f for c in cycles for f in (c.get("reconcile_findings") or [])
                if f.get("severity") == "critical"]
    warnings = [f for c in cycles for f in (c.get("reconcile_findings") or [])
                if f.get("severity") == "warning"]
    frozen_cycles = sum(1 for c in cycles if c.get("frozen"))
    errors = [c for c in cycles if c.get("error")]
    report["reconciliation"] = {"critical": len(critical), "warnings": len(warnings),
                                "first_critical": critical[:5], "frozen_cycles": frozen_cycles}
    report["errors"] = {"count": len(errors),
                        "first": [e.get("error", "")[:120] for e in errors[:5]]}

    # ── promotion gates: PASS/FAIL each, PROMOTE only if all pass ────────────
    def gate(name, passed, observed_v, threshold_v):
        return {"gate": name, "result": "PASS" if passed else "FAIL",
                "observed": observed_v, "threshold": threshold_v}

    gates = [
        gate("min_observed_bars", len(boundaries) >= gates_cfg["min_observed_bars"],
             len(boundaries), f">= {gates_cfg['min_observed_bars']}"),
        gate("duplicate_intents", dup_blocked == 0, dup_blocked, "== 0"),
        gate("missed_bars", len(missed) == 0, len(missed), "== 0"),
        gate("critical_reconciliation", len(critical) == 0 and frozen_cycles == 0,
             {"critical": len(critical), "frozen_cycles": frozen_cycles}, "== 0"),
        gate("runner_errors", len(errors) == 0, len(errors), "== 0"),
        gate("max_cycle_time", max_cycle < gates_cfg["max_cycle_time_s"],
             round(max_cycle, 1), f"< {gates_cfg['max_cycle_time_s']}s"),
        gate("no_boundary_overrun", max_cycle < BAR_INTERVAL_S,
             round(max_cycle, 1), f"< {BAR_INTERVAL_S}s (bar interval)"),
    ]
    report["gates"] = gates
    failed = [g["gate"] for g in gates if g["result"] == "FAIL"]
    if not failed:
        report["verdict"] = "ALL_GATES_PASS"
        report["recommendation"] = ("PROMOTE to P2 (demo-account paper trading) — "
                                    "all promotion gates pass")
    else:
        report["verdict"] = "GATES_FAILED"
        report["recommendation"] = f"remain P1 — failed gates: {', '.join(failed)}"
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--state-dir", default=os.environ.get("LIVE_STATE_DIR", "./live_state"))
    ap.add_argument("--min-bars", type=int, default=None,
                    help=f"override SHADOW_MIN_OBSERVED_BARS (default {DEFAULT_MIN_OBSERVED_BARS})")
    ap.add_argument("--max-cycle-time", type=float, default=None,
                    help=f"override SHADOW_MAX_CYCLE_TIME_S (default {DEFAULT_MAX_CYCLE_TIME_S})")
    args = ap.parse_args()
    state_dir = Path(args.state_dir)
    report = build_report(state_dir, gates_from_env(args.min_bars, args.max_cycle_time))
    out = Path(state_dir) / "ops" / "shadow-report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=1, default=str))
    for g in report.get("gates", []):
        print(f"{g['result']:4s} {g['gate']:26s} observed={g['observed']} (need {g['threshold']})")
    print(f"\n{report.get('verdict')}: {report.get('recommendation')}")
    print(f"report: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
