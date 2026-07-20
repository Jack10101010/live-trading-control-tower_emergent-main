"""C1-C — standalone benchmark harness for Control Tower runtime performance.

Engineering / operator tool ONLY. Invoked exclusively via ``python -m live.benchmark``.
It drives the EXISTING deterministic ``golden_pipeline`` repeatedly with a C1-A
``PhaseProfiler`` and reports timing statistics. It never mutates trading state,
deterministic outputs, parity artifacts, or production runtime, and **nothing in
the production path imports this module** (verifiable: zero inbound imports).

Design commitments (per the approved C1-C spec):

* **Measurement boundary** — the measured interval is exactly ONE
  ``golden_pipeline(candles, frontier_date, profiler)`` call over an
  already-loaded candle frame. Candle loading, per-iteration frame copying,
  dataset hashing / row counting, environment collection, determinism trade
  hashing, and report writing are ALL outside the timer.
* **Candle-input integrity** — each measured iteration receives a *fresh copy*
  of the candle frame, produced OUTSIDE the timer, so no iteration can observe
  another's mutation. (Tests additionally assert ``golden_pipeline`` does not
  mutate its input.) No copying/reloading happens inside the measured interval.
* **Warm-up** — candles are loaded before warm-up; warm-up iterations prime
  runtime behaviour only and are excluded from raw samples and all statistics.
* **Determinism** — verification is ON by default (``--no-verify`` disables). The
  returned trade frame is hashed only AFTER the measured timer stops, so hashing
  can never contaminate timing. A mismatch aborts the run with the offending
  iteration; no normal PASS report is emitted.
* **Stable phase schema** — every measured iteration must expose identical phase
  names; a difference fails loudly (missing phases are never treated as zero).
* **Safe output** — output never defaults into a repository; it goes to a fresh
  OS temp directory unless ``--out`` is given, and any path inside the CT repo,
  the Lux repo, a golden dir, the dataset dir, or a live-state/market/ops dir is
  refused.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from live.config import ENGINE_VERSION_EXPECTED, LiveConfig
from live.runner import LiveRunner, LuxSession, PhaseProfiler

DEFAULT_FROZEN_RELPATH = ("data", "candles", "EURUSD_1m_extended_2015_2026.csv")
DEFAULT_FRONTIER_DATE = "2026-06-19"
DEFAULT_ITERATIONS = 10
DEFAULT_WARMUP = 1
P95_MIN_ITERATIONS = 20
P95_WARNING = "p95 is descriptive only with fewer than 20 measured iterations."
PERCENTILE_METHOD = ("linear interpolation between closest ranks: "
                     "rank = p/100 * (n-1), interpolate neighbours (numpy 'linear')")
MEASUREMENT_BOUNDARY = (
    "Measured interval = exactly ONE golden_pipeline(candles, frontier_date, "
    "profiler) call over an already-loaded candle frame. EXCLUDED from every "
    "measured timing: candle loading, the per-iteration fresh-copy of the candle "
    "frame, dataset hashing, row counting, environment collection, determinism "
    "trade-frame hashing, input-integrity checks, and JSON/Markdown/CSV writing.")


class BenchmarkError(RuntimeError):
    """Raised when a benchmark run cannot produce a trustworthy PASS report
    (determinism mismatch, unstable phase schema, or an iteration failure)."""


# ── statistics ───────────────────────────────────────────────────────────────
def percentile(values, p: float):
    """Linear-interpolation percentile. ``values`` unsorted; ``p`` in [0, 100].
    Returns a float, or None for empty input."""
    if not values:
        return None
    s = sorted(float(v) for v in values)
    if len(s) == 1:
        return s[0]
    rank = (p / 100.0) * (len(s) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def aggregate(samples) -> dict:
    """min/p50/p95/max/mean and SAMPLE standard deviation. ``stddev`` is null
    when n < 2 (identified by ``stddev_kind``)."""
    n = len(samples)
    if n == 0:
        return {"n": 0, "min": None, "p50": None, "p95": None, "max": None,
                "mean": None, "stddev": None, "stddev_kind": None}
    vals = [float(v) for v in samples]
    stddev = float(statistics.stdev(vals)) if n >= 2 else None
    return {"n": n, "min": min(vals), "p50": percentile(vals, 50),
            "p95": percentile(vals, 95), "max": max(vals),
            "mean": float(statistics.fmean(vals)), "stddev": stddev,
            "stddev_kind": ("sample" if n >= 2 else None)}


# ── identity / environment (all OUTSIDE the measured timer) ──────────────────
def sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git_head(repo_dir) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=str(repo_dir),
                             capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None
    except Exception:
        return None


def _ct_root() -> Path:
    return Path(__file__).resolve().parent.parent


def collect_environment(*, lux_root, session, dataset_path, dataset_rows,
                        iterations, warmup, label) -> dict:
    dpath = str(dataset_path) if dataset_path else None
    dsha = (sha256_file(dataset_path)
            if (dataset_path and Path(dataset_path).exists()) else None)
    return {
        "benchmark_timestamp": datetime.now(timezone.utc).isoformat(),
        "ct_commit": _git_head(_ct_root()),
        "lux_commit": _git_head(lux_root),
        "engine_version": getattr(session, "engine_version", None),
        "expected_engine_version": ENGINE_VERSION_EXPECTED,
        "dataset_path": dpath,
        "dataset_sha256": dsha,
        "dataset_rows": int(dataset_rows),
        "iterations": iterations,
        "warmup": warmup,
        "platform": _platform_str(),
        "machine": os.uname().machine if hasattr(os, "uname") else None,
        "processor": _processor_str(),
        "python_version": sys.version.split()[0],
        "measurement_boundary": MEASUREMENT_BOUNDARY,
        "percentile_method": PERCENTILE_METHOD,
        "label": label,
    }


def _platform_str() -> str:
    import platform
    return platform.platform()


def _processor_str() -> str:
    import platform
    return platform.processor() or platform.machine()


# ── measured core loop ───────────────────────────────────────────────────────
def _trade_hash(trades) -> str:
    """Content hash of the returned trade frame. Called ONLY after the measured
    timer has stopped, so it can never contaminate timing statistics."""
    return hashlib.sha256(trades.to_csv(index=False).encode("utf-8")).hexdigest()


def benchmark_pipeline(runner, candles, frontier_date, *, iterations, warmup,
                       verify) -> dict:
    """Drive exactly one ``golden_pipeline`` call per measured iteration.

    Each iteration (warm-up and measured) receives a FRESH candle-frame copy made
    OUTSIDE the timer. Only the ``golden_pipeline`` call is inside the timer. When
    ``verify`` is on, the returned trades are hashed AFTER the timer stops.
    """
    # Warm-up: primes runtime behaviour; excluded from samples and statistics.
    for _ in range(max(0, warmup)):
        frame = candles.copy()                       # outside the measured timer
        runner.golden_pipeline(frame, frontier_date, profiler=PhaseProfiler())

    raw_totals: list[float] = []
    raw_phases: list[dict] = []
    ref_phase_names = None
    ref_hash = None

    for i in range(iterations):
        frame = candles.copy()                       # fresh frame, OUTSIDE the timer
        profiler = PhaseProfiler()
        try:
            t0 = time.perf_counter()
            trades = runner.golden_pipeline(frame, frontier_date, profiler=profiler)
            elapsed = time.perf_counter() - t0        # measured interval ends here
        except Exception as exc:                      # an iteration failed → no PASS
            raise BenchmarkError(
                f"measured iteration {i} raised {type(exc).__name__}: {exc}") from exc

        phases = dict(profiler.snapshot()["phase_timings"])
        names = tuple(sorted(phases))
        if ref_phase_names is None:
            ref_phase_names = names
        elif names != ref_phase_names:               # stable-schema enforcement
            raise BenchmarkError(
                f"phase schema mismatch at measured iteration {i}: "
                f"expected {list(ref_phase_names)}, observed {list(names)}")

        raw_totals.append(elapsed)
        raw_phases.append(phases)

        if verify:                                    # hashing AFTER the timer
            h = _trade_hash(trades)
            if ref_hash is None:
                ref_hash = h
            elif h != ref_hash:
                raise BenchmarkError(
                    f"determinism mismatch at measured iteration {i}: "
                    f"trade hash {h} != iteration-0 hash {ref_hash}")

    return {"raw_totals": raw_totals, "raw_phases": raw_phases,
            "phase_names": list(ref_phase_names or []),
            "trade_hash": ref_hash, "verified": bool(verify)}


def aggregate_report(raw_totals, raw_phases, phase_names):
    total_stats = aggregate(raw_totals)
    # Every measured iteration is guaranteed (by stable-schema enforcement) to
    # carry exactly `phase_names`, so indexing here never fabricates a zero.
    per_phase = {name: aggregate([p[name] for p in raw_phases])
                 for name in phase_names}
    return total_stats, per_phase


def run_benchmark(*, lux_root, candles_path, frontier_date, iterations, warmup,
                  verify, label, runner=None, candles=None) -> dict:
    """Build (or accept) a runner, load candles once, run the measured loop, and
    assemble the report. ``runner``/``candles`` injection is the test seam; the
    CLI always uses the real ``LuxSession``/``LiveRunner`` path."""
    lux_root = Path(lux_root).resolve()
    if runner is None:
        session = LuxSession(lux_root)
        session.verify_engine()                       # refuse a mismatched engine
        cfg = LiveConfig(
            lux_root=lux_root,
            state_dir=Path(tempfile.mkdtemp(prefix="ct_bench_state_")),
            market_data_dir=Path(tempfile.mkdtemp(prefix="ct_bench_md_")),
            kill_file=Path(tempfile.mkdtemp(prefix="ct_bench_kill_")) / "KILL")
        runner = LiveRunner(cfg, session=session)
    else:
        session = getattr(runner, "session", None)

    if candles is None:
        candles = pd.read_csv(candles_path)           # loaded ONCE, outside the timer

    result = benchmark_pipeline(runner, candles, frontier_date,
                                iterations=iterations, warmup=warmup, verify=verify)
    total_stats, per_phase = aggregate_report(
        result["raw_totals"], result["raw_phases"], result["phase_names"])
    env = collect_environment(lux_root=lux_root, session=session,
                              dataset_path=candles_path, dataset_rows=len(candles),
                              iterations=iterations, warmup=warmup, label=label)
    warnings = [P95_WARNING] if len(result["raw_totals"]) < P95_MIN_ITERATIONS else []
    return {
        "environment": env,
        "measurement_boundary": MEASUREMENT_BOUNDARY,
        "percentile_method": PERCENTILE_METHOD,
        "warnings": warnings,
        "phase_names": result["phase_names"],
        "raw": {"totals_s": result["raw_totals"], "phases_s": result["raw_phases"]},
        "aggregate": {"total_s": total_stats, "per_phase_s": per_phase},
        "determinism": {
            "enabled": bool(verify),
            "verdict": ("PASS" if verify else "SKIPPED"),
            "trade_hash": result["trade_hash"],
        },
        "verdict": "PASS",
    }


# ── safe output resolution ───────────────────────────────────────────────────
def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def build_forbidden_roots(lux_root, dataset_path) -> list[tuple[str, Path]]:
    ct_root = _ct_root()
    default_cfg = LiveConfig()                         # resolves LIVE_STATE_DIR / MARKET_DATA_DIR
    roots = [
        ("control-tower-repo", ct_root),
        ("lux-repo", Path(lux_root).resolve()),
        ("golden-dir", (ct_root / "golden").resolve()),
        ("live-state-dir", Path(default_cfg.state_dir).resolve()),
        ("market-dir", Path(default_cfg.market_data_dir).resolve()),
        ("ops-dir", (Path(default_cfg.state_dir) / "ops").resolve()),
    ]
    if dataset_path:
        roots.append(("dataset-dir", Path(dataset_path).resolve().parent))
    return roots


def resolve_safe_out_dir(requested, forbidden) -> Path:
    """Return a safe absolute output dir. When ``requested`` is None, mint a fresh
    timestamped OS temp dir (outside every repository). Refuse (raise) any path
    inside a forbidden root."""
    if requested is None:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        target = Path(tempfile.mkdtemp(prefix=f"ct_benchmark_{stamp}_"))
    else:
        target = Path(requested).resolve()
    for name, root in forbidden:
        if target == root.resolve() or _is_within(target, root):
            raise ValueError(
                f"refusing to write benchmark output inside {name} ({root}); "
                f"resolved out={target}")
    target.mkdir(parents=True, exist_ok=True)
    return target


# ── output writers (all OUTSIDE the measured timer) ──────────────────────────
def _base_name(report) -> str:
    env = report["environment"]
    stamp = re.sub(r"[^0-9A-Za-z]", "", (env.get("benchmark_timestamp") or "run"))[:20]
    ctshort = (env.get("ct_commit") or "nocommit")[:7]
    return f"benchmark_{stamp}_{ctshort}"


def _fmt(x) -> str:
    return "—" if x is None else f"{x:.4f}"


def render_markdown(report) -> str:
    env = report["environment"]
    L = [f"# Control Tower Benchmark — {env['benchmark_timestamp']}", "",
         f"- **CT commit:** `{env['ct_commit']}`",
         f"- **Lux commit:** `{env['lux_commit']}`",
         f"- **Engine:** `{env['engine_version']}` (expected `{env['expected_engine_version']}`)",
         f"- **Dataset:** `{env['dataset_path']}` · sha256 `{env['dataset_sha256']}` · rows {env['dataset_rows']}",
         f"- **Iterations:** {env['iterations']} (warm-up {env['warmup']}, excluded from statistics)",
         f"- **Platform:** {env['platform']} · {env['machine']} · Python {env['python_version']}",
         f"- **Percentile method:** {report['percentile_method']}",
         f"- **Label:** {env['label']}", "",
         f"**Measurement boundary:** {report['measurement_boundary']}", ""]
    for w in report["warnings"]:
        L.append(f"> ⚠️ {w}")
    if report["warnings"]:
        L.append("")
    ts = report["aggregate"]["total_s"]
    L += ["| metric | n | min (s) | p50 (s) | p95 (s) | max (s) | mean (s) | stddev (s) |",
          "|---|---|---|---|---|---|---|---|",
          (f"| **total** | {ts['n']} | {_fmt(ts['min'])} | {_fmt(ts['p50'])} | "
           f"{_fmt(ts['p95'])} | {_fmt(ts['max'])} | {_fmt(ts['mean'])} | {_fmt(ts['stddev'])} |")]
    for name, s in report["aggregate"]["per_phase_s"].items():
        L.append(f"| {name} | {s['n']} | {_fmt(s['min'])} | {_fmt(s['p50'])} | "
                 f"{_fmt(s['p95'])} | {_fmt(s['max'])} | {_fmt(s['mean'])} | {_fmt(s['stddev'])} |")
    d = report["determinism"]
    L += ["", f"**Determinism:** {d['verdict']} (enabled={d['enabled']}, "
              f"trade_hash=`{d['trade_hash']}`)",
          f"**stddev kind:** {ts['stddev_kind']} (null when n<2)",
          f"**Overall verdict:** {report['verdict']}", ""]
    return "\n".join(L)


def render_csv(report) -> str:
    rows = ["iteration,metric,seconds"]
    totals = report["raw"]["totals_s"]
    phases = report["raw"]["phases_s"]
    for i, t in enumerate(totals):
        rows.append(f"{i},total,{t}")
        for name, sec in phases[i].items():
            rows.append(f"{i},{name},{sec}")
    return "\n".join(rows) + "\n"


def write_outputs(report, out_dir, want_csv: bool) -> dict:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base = _base_name(report)
    paths = {}
    json_path = out_dir / f"{base}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    paths["json"] = json_path
    md_path = out_dir / f"{base}.md"
    md_path.write_text(render_markdown(report))
    paths["markdown"] = md_path
    if want_csv:
        csv_path = out_dir / f"{base}.csv"
        csv_path.write_text(render_csv(report))
        paths["csv"] = csv_path
    return paths


# ── CLI ──────────────────────────────────────────────────────────────────────
def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="python -m live.benchmark",
        description="Standalone Control Tower runtime benchmark (engineering tool; "
                    "never affects trading behaviour, determinism, or parity).")
    ap.add_argument("--lux-root", default=os.environ.get("LUX_ROOT", "../Lux-OB-Backtester"))
    ap.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    ap.add_argument("--warmup", type=int, default=DEFAULT_WARMUP)
    ap.add_argument("--frontier-date", default=DEFAULT_FRONTIER_DATE)
    ap.add_argument("--candles", default=None,
                    help="candle CSV to benchmark (default: the frozen Lux dataset)")
    ap.add_argument("--out", default=None,
                    help="output dir (default: a fresh OS temp dir; never a repo/state/ops dir)")
    ap.add_argument("--csv", action="store_true", help="also emit per-iteration CSV")
    ap.add_argument("--no-verify", action="store_true",
                    help="disable cross-iteration determinism verification (ON by default)")
    ap.add_argument("--label", default=None)
    return ap.parse_args(argv)


def main(argv=None, *, _runner=None, _candles=None) -> int:
    """CLI entry. ``_runner``/``_candles`` are a test-only injection seam; the real
    CLI never sets them (so `python -m live.benchmark` always runs the real path)."""
    args = parse_args(argv)
    lux_root = Path(args.lux_root).resolve()
    dataset_path = (Path(args.candles).resolve() if args.candles
                    else lux_root.joinpath(*DEFAULT_FROZEN_RELPATH))
    forbidden = build_forbidden_roots(lux_root, dataset_path)
    try:
        out_dir = resolve_safe_out_dir(args.out, forbidden)
    except ValueError as exc:
        print(f"REFUSED: {exc}")
        return 2

    try:
        report = run_benchmark(
            lux_root=lux_root, candles_path=dataset_path,
            frontier_date=args.frontier_date, iterations=args.iterations,
            warmup=args.warmup, verify=(not args.no_verify), label=args.label,
            runner=_runner, candles=_candles)
    except Exception as exc:                           # no partial PASS report on failure
        fail = {"verdict": "FAIL", "error_type": type(exc).__name__, "error": str(exc),
                "at": datetime.now(timezone.utc).isoformat()}
        fail_path = out_dir / "benchmark_FAILED.json"
        fail_path.write_text(json.dumps(fail, indent=2))
        print(f"BENCHMARK FAILED ({type(exc).__name__}): {exc}")
        print(f"failure report: {fail_path}")
        return 1

    paths = write_outputs(report, out_dir, args.csv)
    for w in report["warnings"]:
        print(f"WARNING: {w}")
    print(f"VERDICT: {report['verdict']}  (determinism={report['determinism']['verdict']})")
    print(f"output dir: {out_dir}")
    for kind, p in paths.items():
        print(f"  {kind}: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
