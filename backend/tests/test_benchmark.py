"""C1-C benchmark harness — fast unit tests only (the REAL benchmark, which runs
the ~minutes-long Golden pipeline, is NEVER executed here). A tiny fake runner
drives the loop so every requirement is covered in milliseconds.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import benchmark as bm                                    # noqa: E402
from live.runner import PhaseProfiler, _timed                       # noqa: E402


# ── fakes ─────────────────────────────────────────────────────────────────────
def _df(tag="a"):
    return pd.DataFrame({"trade_id": ["L_1"], "outcome": [tag]})


class FakeRunner:
    """Stands in for LiveRunner. Records into the supplied profiler using the
    real C1-A `_timed`/PhaseProfiler so profiler integration is exercised for
    real; returns a per-call frame so determinism/phase scenarios are testable."""

    def __init__(self, *, frames=None, phase_sets=None, raise_at=None,
                 mutate=False, engine_version="fake-engine"):
        self._frames = frames
        self._phase_sets = phase_sets
        self._raise_at = raise_at
        self._mutate = mutate
        self.calls = 0
        self.session = types.SimpleNamespace(engine_version=engine_version)

    def golden_pipeline(self, candles, frontier_date, profiler=None):
        i = self.calls
        self.calls += 1
        if self._raise_at is not None and i == self._raise_at:
            raise RuntimeError(f"boom at call {i}")
        if self._mutate:
            candles["_scratch"] = 1                       # mutate the frame we were handed
        names = (self._phase_sets[i] if self._phase_sets
                 else ("execute_scenario_job", "resample"))
        if profiler is not None:
            for nm in names:
                with _timed(profiler, nm):
                    pass
        if self._frames is None:
            return _df("const")
        return self._frames[i]


def _run(fake, *, iterations, warmup=0, verify=True, candles=None):
    return bm.run_benchmark(
        lux_root=REPO_ROOT, candles_path=None, frontier_date="2026-06-19",
        iterations=iterations, warmup=warmup, verify=verify, label="unit",
        runner=fake, candles=(candles if candles is not None else _df()))


# ── statistics + percentile ───────────────────────────────────────────────────
def test_percentile_linear_interpolation():
    vals = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
    assert bm.percentile(vals, 50) == pytest.approx(5.5)      # (n-1)*0.5=4.5 -> 5..6
    assert bm.percentile(vals, 95) == pytest.approx(9.55)     # (n-1)*0.95=8.55 -> 9..10
    assert bm.percentile([42.0], 95) == 42.0
    assert bm.percentile([], 50) is None


def test_aggregate_basic_sample_stddev():
    a = bm.aggregate([2, 4, 4, 4, 5, 5, 7, 9])
    assert a["n"] == 8 and a["min"] == 2.0 and a["max"] == 9.0
    assert a["mean"] == pytest.approx(5.0)
    assert a["stddev"] == pytest.approx(2.13809, rel=1e-4)     # SAMPLE stdev
    assert a["stddev_kind"] == "sample"


def test_aggregate_n1_stddev_null():
    a = bm.aggregate([3.0])
    assert a["n"] == 1 and a["stddev"] is None and a["stddev_kind"] is None
    assert a["min"] == a["max"] == a["p50"] == a["p95"] == 3.0


def test_aggregate_empty_input():
    a = bm.aggregate([])
    assert a["n"] == 0
    assert all(a[k] is None for k in ("min", "p50", "p95", "max", "mean", "stddev"))


# ── loop: warm-up, profiler integration, phase aggregation ────────────────────
def test_warmup_excluded_from_samples_and_stats():
    fake = FakeRunner()
    rep = _run(fake, iterations=3, warmup=2)
    assert fake.calls == 5                                     # 2 warm-up + 3 measured
    assert len(rep["raw"]["totals_s"]) == 3                    # only measured sampled
    assert rep["aggregate"]["total_s"]["n"] == 3


def test_profiler_integration_and_phase_aggregation():
    fake = FakeRunner()                                        # emits 2 phases per call
    rep = _run(fake, iterations=4, warmup=0)
    per = rep["aggregate"]["per_phase_s"]
    assert set(per) == {"execute_scenario_job", "resample"}
    assert all(per[name]["n"] == 4 for name in per)            # each measured iter counted
    assert len(rep["raw"]["phases_s"]) == 4


def test_stable_phase_enforcement_fails_loudly():
    fake = FakeRunner(phase_sets=[("a", "b"), ("a", "c")])     # iter 1 differs
    with pytest.raises(bm.BenchmarkError) as ei:
        _run(fake, iterations=2, warmup=0)
    msg = str(ei.value)
    assert "iteration 1" in msg and "['a', 'b']" in msg and "['a', 'c']" in msg


# ── determinism ───────────────────────────────────────────────────────────────
def test_determinism_success():
    fake = FakeRunner(frames=[_df("x")] * 3)
    rep = _run(fake, iterations=3, warmup=0, verify=True)
    assert rep["determinism"]["verdict"] == "PASS"
    assert rep["determinism"]["enabled"] is True
    assert rep["determinism"]["trade_hash"]


def test_determinism_mismatch_identifies_iteration():
    fake = FakeRunner(frames=[_df("x"), _df("x"), _df("DIFFERENT")])
    with pytest.raises(bm.BenchmarkError) as ei:
        _run(fake, iterations=3, warmup=0, verify=True)
    assert "iteration 2" in str(ei.value) and "determinism mismatch" in str(ei.value)


def test_hashing_outside_measured_interval(monkeypatch):
    # Count _trade_hash invocations; timing sample count must not depend on it.
    calls = {"n": 0}
    real = bm._trade_hash
    monkeypatch.setattr(bm, "_trade_hash", lambda t: (calls.__setitem__("n", calls["n"] + 1), real(t))[1])
    rep_on = _run(FakeRunner(frames=[_df("x")] * 3), iterations=3, warmup=0, verify=True)
    assert calls["n"] == 3                                     # hashed once per MEASURED iter
    calls["n"] = 0
    rep_off = _run(FakeRunner(), iterations=3, warmup=0, verify=False)
    assert calls["n"] == 0                                     # never hashed when verify off
    # sample counts identical regardless of verify → hashing is out-of-band
    assert len(rep_on["raw"]["totals_s"]) == len(rep_off["raw"]["totals_s"]) == 3


# ── candle-input integrity ────────────────────────────────────────────────────
def test_candle_mutation_protection():
    original = _df()
    before_cols = list(original.columns)
    fake = FakeRunner(mutate=True)                             # mutates the frame it receives
    _run(fake, iterations=3, warmup=1, candles=original)
    assert list(original.columns) == before_cols              # caller's frame untouched
    assert "_scratch" not in original.columns                 # each iter got a fresh copy


# ── output schema / markdown / csv ────────────────────────────────────────────
def test_output_schema_and_markdown(tmp_path):
    rep = _run(FakeRunner(), iterations=3, warmup=1, verify=True)
    paths = bm.write_outputs(rep, tmp_path, want_csv=False)
    data = json.loads(paths["json"].read_text())
    env = data["environment"]
    for k in ("benchmark_timestamp", "ct_commit", "lux_commit", "engine_version",
              "expected_engine_version", "dataset_path", "dataset_sha256",
              "dataset_rows", "iterations", "warmup", "platform", "machine",
              "processor", "python_version", "measurement_boundary",
              "percentile_method", "label"):
        assert k in env, k
    for k in ("warnings", "raw", "aggregate", "determinism", "verdict",
              "measurement_boundary", "percentile_method", "phase_names"):
        assert k in data, k
    assert "totals_s" in data["raw"] and "phases_s" in data["raw"]
    assert "total_s" in data["aggregate"] and "per_phase_s" in data["aggregate"]
    md = paths["markdown"].read_text()
    assert md.strip() and "Measurement boundary" in md and "Determinism" in md
    assert not paths.get("csv")


def test_optional_csv_generation(tmp_path):
    rep = _run(FakeRunner(), iterations=2, warmup=0)
    paths = bm.write_outputs(rep, tmp_path, want_csv=True)
    assert "csv" in paths and paths["csv"].exists()
    body = paths["csv"].read_text().splitlines()
    assert body[0] == "iteration,metric,seconds"
    assert any(line.startswith("0,total,") for line in body)
    assert any(",execute_scenario_job," in line for line in body)


def test_p95_warning_below_20_iterations():
    rep = _run(FakeRunner(), iterations=5, warmup=0)
    assert bm.P95_WARNING in rep["warnings"]
    rep20 = _run(FakeRunner(), iterations=20, warmup=0)
    assert rep20["warnings"] == []


# ── safe output-path rejection ────────────────────────────────────────────────
def test_unsafe_output_path_rejected(tmp_path):
    forbidden = bm.build_forbidden_roots(REPO_ROOT, REPO_ROOT / "data" / "x.csv")
    # inside the CT repo → refused
    with pytest.raises(ValueError):
        bm.resolve_safe_out_dir(str(REPO_ROOT / "golden" / "out"), forbidden)
    with pytest.raises(ValueError):
        bm.resolve_safe_out_dir(str(REPO_ROOT / "live_state" / "ops"), forbidden)
    # a genuinely outside dir → accepted and created
    safe = bm.resolve_safe_out_dir(str(tmp_path / "bench_out"), forbidden)
    assert safe.exists()
    # omitted → fresh OS temp dir, outside every forbidden root
    minted = bm.resolve_safe_out_dir(None, forbidden)
    assert minted.exists()
    for _name, root in forbidden:
        assert not bm._is_within(minted, root)


# ── failure handling via main() (no real pipeline) ────────────────────────────
def test_iteration_failure_writes_only_failed_report(tmp_path):
    fake = FakeRunner(raise_at=1)                             # measured iter 1 raises
    rc = bm.main(["--lux-root", str(tmp_path / "lux"), "--iterations", "3", "--warmup", "0",
                  "--out", str(tmp_path / "out")],
                 _runner=fake, _candles=_df())
    assert rc == 1
    out = tmp_path / "out"
    assert (out / "benchmark_FAILED.json").exists()
    fail = json.loads((out / "benchmark_FAILED.json").read_text())
    assert fail["verdict"] == "FAIL" and "iteration 1" in fail["error"]
    # NO normal PASS report emitted
    assert not list(out.glob("benchmark_2*.json")) and not list(out.glob("benchmark_*_*.json"))


def test_determinism_mismatch_via_main_no_pass_report(tmp_path):
    fake = FakeRunner(frames=[_df("x"), _df("y")])           # differ → mismatch
    rc = bm.main(["--lux-root", str(tmp_path / "lux"), "--iterations", "2", "--warmup", "0",
                  "--out", str(tmp_path / "out")],
                 _runner=fake, _candles=_df())
    assert rc == 1
    out = tmp_path / "out"
    assert (out / "benchmark_FAILED.json").exists()
    pass_reports = [p for p in out.glob("*.json") if p.name != "benchmark_FAILED.json"]
    assert pass_reports == []                                 # no partial success report


def test_main_success_writes_pass_reports(tmp_path):
    fake = FakeRunner()                                      # constant frame → deterministic
    rc = bm.main(["--lux-root", str(tmp_path / "lux"), "--iterations", "2", "--warmup", "1",
                  "--out", str(tmp_path / "out"), "--csv"],
                 _runner=fake, _candles=_df())
    assert rc == 0
    out = tmp_path / "out"
    assert list(out.glob("*.json")) and list(out.glob("*.md")) and list(out.glob("*.csv"))
    assert not (out / "benchmark_FAILED.json").exists()


def test_main_refuses_unsafe_out(tmp_path):
    rc = bm.main(["--lux-root", str(tmp_path), "--out", str(REPO_ROOT / "golden")],
                 _runner=FakeRunner(), _candles=_df())
    assert rc == 2                                            # refused before running
