"""M3 P0 — deterministic rehearsal gate (run on the operator Mac, ~15-20 min).

Proves the live runner's recompute-on-close path reproduces the frozen Golden
system byte-for-byte BEFORE any VPS deployment, and exercises frontier
mechanics on real Golden data. Four pipeline executions total:

  RUN A   truncated frozen data (final closed 15m window removed) -> bootstrap
  RUN B   full frozen data -> frontier diff -> intents (raw frame kept)
  RUN B2  full frozen data again -> determinism (raw byte equality with B)
  BASE    baseline pass on RUN B's captured inputs (for order_blocks parity)

Plus: byte parity vs the Golden reference (entry trades / baseline trades /
lifecycle order_blocks), summary metrics (exact float equality), restart/
resume, duplicate suppression, dry-run-never-submits, SKIP determinism.

Usage:
  python3 -m live.rehearsal --lux-root <Lux-OB-Backtester> \
      --golden-ref <golden/run-001/reference> --out <report dir>

Exit 0 = PASS (report + hashes written); exit 1 = FAIL, stopped at the first
divergence with bar/trade/field detail. No speculative patching.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from live.config import ENGINE_VERSION_EXPECTED, LiveConfig
from live.executor import Executor
from live.intents import diff_frontier
from live.mt5_gateway import MT5Gateway
from live.runner import LiveRunner, LuxSession, latest_closed_boundary
from live.state import RunnerState

# M-CAP-INTEGRATE-1: Lux candidate commit (was d978074a2ee938fb4803e50d419788c584d6ef57).
# Duplicated in live/deploy_check.py and live/rehearsal.py; the two MUST
# agree. backend/tests/test_lux_pin_agreement.py enforces it.
EXPECTED_COMMIT = "b96fa7aee7605dd2115117116f9c2eb9b6669c23"
EXPECTED = {
    "trades_sha": "ca925d544601ad243ba0909045dc00384eaea0e9d41977ab7189b8a8c1e9a964",
    "baseline_sha": "aa77176b54cc4f96acccf3d0540fe41ee3b6c252847ddd3bc65702fe180214c1",
    "order_blocks_sha": "bb67b6557a4a88fcfb1e83e6bf1558c8b3530ba9431a2e3e35d4d480078b7159",
    "rows": 2060, "decided": 635, "net_r": 292.6598432207228,
    "baseline_net_r": 145.38471612795792, "ob_rows": 2080,
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def first_divergence(ref_csv: Path, cand_csv: Path, key: str) -> dict:
    """Locate the first divergence: ordering, population, then field value."""
    a = pd.read_csv(ref_csv, dtype=str, keep_default_na=False)
    b = pd.read_csv(cand_csv, dtype=str, keep_default_na=False)
    if list(a.columns) != list(b.columns):
        return {"kind": "schema",
                "only_ref": [c for c in a.columns if c not in b.columns],
                "only_cand": [c for c in b.columns if c not in a.columns]}
    for i, (ka, kb) in enumerate(zip(a[key].tolist(), b[key].tolist())):
        if ka != kb:
            return {"kind": "ordering", "row_index": i, "ref_key": ka, "cand_key": kb}
    ia = {r[key]: r for r in a.to_dict("records")}
    ib = {r[key]: r for r in b.to_dict("records")}
    missing = [k for k in ia if k not in ib]
    extra = [k for k in ib if k not in ia]
    if missing or extra:
        return {"kind": "population", "missing_first": missing[:3], "extra_first": extra[:3]}
    for k in a[key].tolist():
        ra, rb_ = ia[k], ib[k]
        for col in a.columns:
            if ra[col] != rb_[col]:
                return {"kind": "value", "key": k, "field": col,
                        "ref": ra[col], "cand": rb_[col],
                        "bar_hint": ra.get("fill_time") or ra.get("detection_time", "")}
    return {"kind": "byte_only", "note": "row content identical; byte-level formatting diff"}


class CountingGateway(MT5Gateway):
    """Any order operation during rehearsal is a gate violation."""

    def __init__(self, config):
        super().__init__(config, sdk=None)
        self.order_ops = 0

    def _violate(self, *a, **k):  # pragma: no cover - must never run
        self.order_ops += 1
        return False, "REHEARSAL VIOLATION: order op attempted"

    open_position = _violate
    modify_position_sl = _violate
    close_position = _violate


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--lux-root", required=True)
    ap.add_argument("--golden-ref", required=True)
    ap.add_argument("--out", default="./rehearsal_out")
    args = ap.parse_args()
    # M-GOLDEN-CONTRACT-1: resolve EVERY path argument to absolute IMMEDIATELY.
    # LuxSession os.chdir()s into the Lux root (its documented driver contract),
    # so any relative path held past this point silently re-anchors — the
    # observed failure mode was a relative --golden-ref that resolved fine at
    # launch and failed ~1 hour later, after the pipeline, when the byte-parity
    # step first opened it from the changed cwd.
    lux = Path(args.lux_root).resolve()
    ref = Path(args.golden_ref).resolve()
    out = Path(args.out).resolve()
    # Fail-fast: the reference must be a directory containing every file the
    # byte-parity step will read, BEFORE any expensive work begins.
    _ref_required = (
        "trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv",
        "trades_allow_multi_position.csv",
        "order_blocks.csv",
    )
    if not ref.is_dir():
        print(f"FAIL  golden_ref_exists — not a directory: {ref}")
        return 1
    _missing = [n for n in _ref_required if not (ref / n).is_file()]
    if _missing:
        print(f"FAIL  golden_ref_complete — missing from {ref}: {_missing}")
        return 1
    if not lux.is_dir():
        print(f"FAIL  lux_root_exists — not a directory: {lux}")
        return 1
    out.mkdir(parents=True, exist_ok=True)
    report: dict = {"gate": "M3-P0-rehearsal", "at": datetime.now(timezone.utc).isoformat(),
                    "expected": EXPECTED, "checks": [], "hashes": {}, "verdict": "FAIL"}

    def finish(code: int) -> int:
        report["verdict"] = "PASS" if code == 0 else "FAIL"
        (out / "rehearsal-report.json").write_text(json.dumps(report, indent=1, default=str))
        print(f"\nVERDICT: {report['verdict']} — report at {out / 'rehearsal-report.json'}")
        return code

    def check(name, ok, detail=""):
        report["checks"].append({"check": name, "ok": bool(ok), "detail": str(detail)[:500]})
        print(("PASS  " if ok else "FAIL  ") + name + (f" — {detail}" if (detail and not ok) else ""))
        if not ok:
            raise SystemExit(finish(1))

    # ── 1. frozen identity ───────────────────────────────────────────────────
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=lux,
                          capture_output=True, text=True).stdout.strip()
    check("lux_commit", head == EXPECTED_COMMIT, head)
    session = LuxSession(lux)
    check("engine_version", session.engine_version == ENGINE_VERSION_EXPECTED, session.engine_version)

    tmp_holder = tempfile.TemporaryDirectory()
    tmp = Path(tmp_holder.name)
    cfg = LiveConfig(lux_root=lux, state_dir=tmp / "state",
                     market_data_dir=tmp / "md", kill_file=tmp / "KILL")
    cfg.ensure_dirs()
    check("golden_config_include_disabled",
          session.golden_config(cfg.golden_config_path, "2026-06-19")
                 .portfolio_include_disabled_cohorts is True)

    frozen = pd.read_csv(lux / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv")
    times = pd.to_datetime(frozen["time"])
    final_boundary = latest_closed_boundary(times.max())
    truncated = frozen[times < final_boundary].reset_index(drop=True)
    check("closed_bar_truncation", len(truncated) < len(frozen),
          f"{len(frozen)}->{len(truncated)} rows; frontier window starts {final_boundary}")

    # ── 2. RUN A — bootstrap on truncated data (ReplayFeed-equivalent step) ──
    provider = {"df": truncated}
    runner = LiveRunner(cfg, session=session, candles_provider=lambda: provider["df"])
    print("RUN A (bootstrap, truncated)…")
    ra = runner.run_once()
    check("runA_bootstrap", ra["status"] == "bootstrap", ra["status"])

    # ── 3. RUN B — full data; keep the RAW frame; drive the frontier diff ───
    print("RUN B (full frozen)…")
    artifacts: dict = {}
    raw_b = runner.golden_pipeline(frozen, "2026-06-19", artifacts=artifacts)
    provider["df"] = frozen
    runner._pipeline = lambda c, d: raw_b          # reuse — no recompute inside run_once
    rb_result = runner.run_once()
    check("runB_ok", rb_result["status"] == "ok", rb_result["status"])
    intents = rb_result["intents"]
    ids = [i.intent_id for i in intents]
    check("no_duplicate_intents", len(ids) == len(set(ids)), ids)
    report["frontier_intents"] = [i.to_dict() for i in intents]

    # SKIP/diff determinism on the real frames
    prev_frame = runner.state.load_prev_frame()
    redo1 = [i.to_dict() for i in diff_frontier(prev_frame, prev_frame, "X")]
    redo2 = [i.to_dict() for i in diff_frontier(prev_frame, prev_frame, "X")]
    check("diff_deterministic", redo1 == redo2 == [])

    # restart/resume: fresh runner instance, same state dir -> nothing new
    runner2 = LiveRunner(cfg, session=session, candles_provider=lambda: frozen)
    rr = runner2.run_once()
    check("restart_no_extra_intents", rr["status"] == "no_new_bar", rr.get("status"))

    # dry-run executor: never submits; replay fully suppressed
    gateway = CountingGateway(cfg)
    ex = Executor(cfg, runner.state, gateway)
    ex.apply(intents, today="2026-06-19")
    e2 = ex.apply(intents, today="2026-06-19")
    check("dry_run_never_submits", gateway.order_ops == 0, gateway.order_ops)
    check("replay_fully_suppressed",
          len(e2["applied"]) == 0 and all(b.get("rail") == "duplicate_intent"
                                          for b in e2["blocked"]), e2["blocked"][:2])

    # ── 4. RUN B2 — determinism ──────────────────────────────────────────────
    print("RUN B2 (determinism)…")
    raw_b2 = runner.golden_pipeline(frozen, "2026-06-19")
    check("pipeline_deterministic",
          raw_b.to_csv(index=False) == raw_b2.to_csv(index=False))

    # ── 5. BASELINE pass on RUN B's captured inputs ──────────────────────────
    print("BASELINE pass…")
    rbmod = session.rb
    base_mode = artifacts["config"].execution_modes[0]
    base_job = {"kind": "protection", "execution_mode": base_mode,
                "job_id": f"{base_mode}:protection:baseline", "label": "baseline",
                "scenario": {"mode": "baseline", "threshold_pct": None,
                             "buffer_pips": None, "key": "baseline"}}
    baseline_trades = rbmod.execute_scenario_job(
        base_job, artifacts["config"], artifacts["candles"],
        artifacts["simulation_obs"], artifacts["order_blocks"],
        artifacts["news_cache"])["results"][0]["trades"]

    # ── 6. byte parity vs the Golden oracle ──────────────────────────────────
    print("BYTE PARITY…")
    trades_path = out / "trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv"
    rbmod.save_scenario_csv(raw_b.copy(), trades_path, write_marker=False)
    baseline_path = out / "trades_allow_multi_position.csv"
    rbmod.save_scenario_csv(baseline_trades.copy(), baseline_path, write_marker=False)
    ob_frame = rbmod.enrich_order_blocks_with_lifecycle(
        artifacts["order_blocks"], baseline_trades,
        artifacts["candles"]["time"].iloc[-1] if len(artifacts["candles"]) else "")
    ob_path = out / "order_blocks.csv"
    ob_frame.to_csv(ob_path, index=False)

    for name, path, expected_sha, ref_name, key in (
            ("entry_trades", trades_path, EXPECTED["trades_sha"],
             "trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv", "trade_id"),
            ("baseline_trades", baseline_path, EXPECTED["baseline_sha"],
             "trades_allow_multi_position.csv", "trade_id"),
            ("order_blocks", ob_path, EXPECTED["order_blocks_sha"],
             "order_blocks.csv", "ob_id")):
        got = sha256(path)
        report["hashes"][name] = got
        if got != expected_sha:
            div = first_divergence(ref / ref_name, path, key)
            check(f"byte_parity_{name}", False, json.dumps(div)[:480])
        else:
            check(f"byte_parity_{name}", True)

    # ── 7. counts + summary metrics (exact) ──────────────────────────────────
    decided = int(raw_b["outcome"].astype(str).isin(["WIN", "LOSS"]).sum())
    check("candidate_rows_2060", len(raw_b) == EXPECTED["rows"], len(raw_b))
    check("decided_635", decided == EXPECTED["decided"], decided)
    check("ob_rows_2080", len(ob_frame) == EXPECTED["ob_rows"], len(ob_frame))
    entry_net = float(artifacts["summary"].get("net_r"))
    check("net_r_exact", entry_net == EXPECTED["net_r"], repr(entry_net))
    ref_summary = json.loads((ref / "summary.json").read_text())
    ref_net = ref_summary["entry_results"]["allow_multi_position"][
        "entry_triggered_edge_25p0_d3"]["net_r"]
    check("net_r_matches_reference_summary", entry_net == ref_net, f"{entry_net} vs {ref_net}")

    report["engine_version"] = session.engine_version
    report["lux_commit"] = head
    tmp_holder.cleanup()
    return finish(0)


if __name__ == "__main__":
    sys.exit(main())
