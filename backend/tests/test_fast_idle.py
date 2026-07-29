"""C4 — strict Fast Idle. Fast unit tests only.

Verifies the process-local memo fast path: it may only CONFIRM a skip the C3
gate would produce (identical bytes + validated stored pair), never construct a
DataFrame, and every uncertainty falls back to the byte-for-byte C3 slow path.

Parse-avoidance is observed by spying on ``live.runner._assemble_from_bytes`` —
the single designated assembly implementation mandated by C3 — not incidental
internals.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import live.runner as runner_mod                                    # noqa: E402
from live.config import LiveConfig                                  # noqa: E402
from live.runner import (LiveRunner, _FastIdleMemo,                 # noqa: E402
                         snapshot_from_bytes)
from live.state import RunnerState                                  # noqa: E402

HEADER = b"time,open,high,low,close,volume\n"


def _cfg(tmp_path) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    c.ensure_dirs()
    (c.lux_root / "data" / "candles").mkdir(parents=True, exist_ok=True)
    return c


def _candles_bytes(n=40, start="2026-07-17 09:00:00+00:00", close="1.0") -> bytes:
    times = pd.date_range(start, periods=n, freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")
    return HEADER + b"".join(f"{t},1.0,1.0,1.0,{close},0\n".encode() for t in times)


def _write_frozen(cfg, payload: bytes) -> None:
    (cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv").write_bytes(payload)


def _frame(rows):
    cols = ["trade_id", "direction", "fill_time", "outcome", "entry", "stop", "tp"]
    return pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows]).astype(str)


def _prod_runner(cfg):
    """Production-path runner (NO input_provider) with a counting fake pipeline."""
    calls = {"n": 0}

    def pipeline(candles, frontier_date):
        calls["n"] += 1
        return _frame([{"trade_id": "L_1", "direction": "bullish", "outcome": "UNFILLED"}])

    r = LiveRunner(cfg, session=None, pipeline=pipeline)
    return r, calls


@pytest.fixture
def parse_spy(monkeypatch):
    """Counting wrapper around the single assembly implementation."""
    counter = {"n": 0}
    real = runner_mod._assemble_from_bytes

    def spy(frozen_bytes, live_bytes):
        counter["n"] += 1
        return real(frozen_bytes, live_bytes)

    monkeypatch.setattr(runner_mod, "_assemble_from_bytes", spy)
    return counter


# ── slow-path first, fast-path after (cases 1, 3, 4, 5, 27) ──────────────────
def test_first_cycle_uses_slow_path_then_fast_path(tmp_path, parse_spy):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    r, pipeline_calls = _prod_runner(cfg)
    out1 = r.run_once()                                  # bootstrap: slow path
    assert out1["status"] == "bootstrap"
    assert parse_spy["n"] == 1 and pipeline_calls["n"] == 1
    out2 = r.run_once()                                  # identical input: fast path
    assert out2["status"] == "no_new_bar"
    assert parse_spy["n"] == 1                           # NO parse on the fast path
    assert pipeline_calls["n"] == 1                      # NO pipeline on the fast path


def test_fast_path_never_constructs_dataframe(tmp_path, parse_spy):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    r, _ = _prod_runner(cfg)
    r.run_once()
    before = parse_spy["n"]
    for _ in range(3):                                   # repeated fast skips
        assert r.run_once()["status"] == "no_new_bar"
    assert parse_spy["n"] == before                      # zero parses across all skips


# ── memo priming (cases 2, 15, 16, 18) ───────────────────────────────────────
def test_evaluation_primes_memo_only_after_save(tmp_path):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    r, _ = _prod_runner(cfg)
    assert r._fast_idle_memo is None
    r.run_once()
    memo = r._fast_idle_memo
    assert memo is not None
    st = RunnerState(cfg.state_dir)                      # reload durable state
    assert memo.input_revision == st.data["last_recomputed_input_revision"]
    assert memo.boundary == st.data["last_boundary"]


def test_pipeline_exception_does_not_prime_memo(tmp_path):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())

    def boom(candles, frontier_date):
        raise RuntimeError("boom")

    r = LiveRunner(cfg, session=None, pipeline=boom)
    with pytest.raises(RuntimeError):
        r.run_once()
    assert r._fast_idle_memo is None


def test_save_exception_does_not_prime_memo(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    r, _ = _prod_runner(cfg)
    monkeypatch.setattr(r.state, "save", lambda: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError):
        r.run_once()
    assert r._fast_idle_memo is None                     # never claims undurable state


def test_slow_path_no_new_bar_primes_memo(tmp_path, parse_spy):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    r1, _ = _prod_runner(cfg)
    r1.run_once()                                        # bootstrap + save
    r2, _ = _prod_runner(cfg)                            # restart: fresh memo
    assert r2._fast_idle_memo is None
    before = parse_spy["n"]
    assert r2.run_once()["status"] == "no_new_bar"       # slow path validates + primes
    assert parse_spy["n"] == before + 1                  # parsed (slow path)
    assert r2._fast_idle_memo is not None
    assert r2.run_once()["status"] == "no_new_bar"       # now fast
    assert parse_spy["n"] == before + 1                  # no further parse


# ── fallback matrix (cases 7-14, 17) ─────────────────────────────────────────
def _primed(tmp_path, payload=None):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, payload if payload is not None else _candles_bytes())
    r, pipeline_calls = _prod_runner(cfg)
    r.run_once()                                         # bootstrap; memo primed
    return cfg, r, pipeline_calls


def test_captured_revision_mismatch_takes_slow_path(tmp_path, parse_spy):
    cfg, r, calls = _primed(tmp_path)
    _write_frozen(cfg, _candles_bytes(close="9.9"))      # frozen-byte correction
    before = parse_spy["n"]
    out = r.run_once()
    assert out["status"] != "no_new_bar"                 # evaluated (correction detected)
    assert parse_spy["n"] == before + 1 and calls["n"] == 2


def test_state_revision_mismatch_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r.state.data["last_recomputed_input_revision"] = "x" * 64
    before = parse_spy["n"]
    out = r.run_once()                                   # memo.rev != state.rev → slow path
    assert parse_spy["n"] == before + 1
    assert out["status"] != "no_new_bar"                 # C3 gate: revision differs → evaluate


def test_state_boundary_mismatch_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r.state.data["last_boundary"] = "2020-01-01 00:00:00+00:00"
    before = parse_spy["n"]
    out = r.run_once()                                   # memo.boundary != state → slow path
    assert parse_spy["n"] == before + 1
    assert out["status"] != "no_new_bar"                 # C3 gate: boundary differs → evaluate


def test_memo_revision_mismatch_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r._fast_idle_memo = _FastIdleMemo(input_revision="y" * 64,
                                      boundary=r._fast_idle_memo.boundary)
    before = parse_spy["n"]
    out = r.run_once()
    assert parse_spy["n"] == before + 1                  # slow path
    assert out["status"] == "no_new_bar"                 # C3 outcome unchanged: skip


def test_memo_boundary_mismatch_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r._fast_idle_memo = _FastIdleMemo(input_revision=r._fast_idle_memo.input_revision,
                                      boundary="WRONG")
    before = parse_spy["n"]
    out = r.run_once()
    assert parse_spy["n"] == before + 1                  # slow path
    assert out["status"] == "no_new_bar"


def test_absent_memo_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r._fast_idle_memo = None
    before = parse_spy["n"]
    assert r.run_once()["status"] == "no_new_bar"
    assert parse_spy["n"] == before + 1


def test_absent_stored_revision_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r.state.data["last_recomputed_input_revision"] = None
    before = parse_spy["n"]
    out = r.run_once()
    assert parse_spy["n"] == before + 1
    assert out["status"] != "no_new_bar"                 # C3: unknown revision → evaluate


def test_absent_stored_boundary_takes_slow_path(tmp_path, parse_spy):
    cfg, r, _ = _primed(tmp_path)
    r.state.data["last_boundary"] = None
    before = parse_spy["n"]
    out = r.run_once()
    assert parse_spy["n"] == before + 1
    assert out["status"] != "no_new_bar"


def test_restart_new_runner_has_empty_memo_and_uses_slow_path(tmp_path, parse_spy):
    cfg, _, _ = _primed(tmp_path)
    fresh, _ = _prod_runner(cfg)
    assert fresh._fast_idle_memo is None
    before = parse_spy["n"]
    assert fresh.run_once()["status"] == "no_new_bar"
    assert parse_spy["n"] == before + 1                  # honest slow-path validation


# ── response equality (case 6) ───────────────────────────────────────────────
def test_fast_response_equals_slow_response(tmp_path):
    cfg, r, _ = _primed(tmp_path)
    fast = r.run_once()                                  # fast path (memo primed)
    fresh, _ = _prod_runner(cfg)                         # empty memo → slow path
    slow = fresh.run_once()
    assert fast == slow == {"status": "no_new_bar", "boundary": fast["boundary"]}


# ── correction / change detection (cases 20-23) ──────────────────────────────
def test_different_bytes_same_boundary_still_evaluates(tmp_path):
    base = _candles_bytes()
    cfg, r, calls = _primed(tmp_path, base)
    _write_frozen(cfg, base.replace(b",1.0,0\n", b",1.5,0\n"))   # same times, new values
    out = r.run_once()
    assert out["status"] != "no_new_bar" and calls["n"] == 2     # correction evaluated


def test_engine_version_change_takes_slow_path(tmp_path, monkeypatch):
    cfg, r, calls = _primed(tmp_path)
    monkeypatch.setattr(r, "_engine_version", lambda: "different-engine")
    out = r.run_once()                                   # revision differs → evaluate
    assert out["status"] != "no_new_bar" and calls["n"] == 2


def test_live_byte_correction_takes_slow_path(tmp_path):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes(n=30))                       # 09:00..09:29
    live1 = _candles_bytes(n=10, start="2026-07-17 09:30:00+00:00", close="1.0")
    cfg.live_segment_csv.write_bytes(live1)
    r, calls = _prod_runner(cfg)
    r.run_once()                                                   # bootstrap
    assert r.run_once()["status"] == "no_new_bar"                  # fast skip
    cfg.live_segment_csv.write_bytes(live1.replace(b",1.0,0\n", b",1.2,0\n"))
    out = r.run_once()                                             # live correction
    assert out["status"] != "no_new_bar" and calls["n"] == 2


# ── property test: every fast skip satisfies the C3 skip predicate (case 24) ─
def test_property_fast_decisions_equal_slow_decisions(tmp_path):
    """Drive the same deterministic scenario sequence through a memo-enabled
    runner and through always-fresh runners (memo never primed → pure C3 slow
    path) over separate identical state dirs; statuses must match step-for-step."""
    base = _candles_bytes(n=40)
    scenarios = [
        ("unchanged", base),
        ("unchanged", base),
        ("frozen-corrected", base.replace(b",1.0,0\n", b",1.4,0\n")),
        ("unchanged", base.replace(b",1.0,0\n", b",1.4,0\n")),
        ("boundary-advance", _candles_bytes(n=80).replace(b",1.0,0\n", b",1.4,0\n")),
        ("unchanged", _candles_bytes(n=80).replace(b",1.0,0\n", b",1.4,0\n")),
    ]
    cfg_fast = _cfg(tmp_path / "fast")
    cfg_slow = _cfg(tmp_path / "slow")
    fast_runner, _ = _prod_runner(cfg_fast)
    results_fast, results_slow = [], []
    for _label, payload in scenarios:
        _write_frozen(cfg_fast, payload)
        results_fast.append(fast_runner.run_once()["status"])
        _write_frozen(cfg_slow, payload)
        slow_runner, _ = _prod_runner(cfg_slow)          # fresh instance: no memo, pure C3
        results_slow.append(slow_runner.run_once()["status"])
    assert results_fast == results_slow
    # durable state converged identically (modulo timestamps/paths)
    a = RunnerState(cfg_fast.state_dir).data
    b = RunnerState(cfg_slow.state_dir).data
    assert a["last_boundary"] == b["last_boundary"]
    assert a["last_recomputed_input_revision"] == b["last_recomputed_input_revision"]


# ── provider policy (case 19) ────────────────────────────────────────────────
def test_provider_backed_runs_never_use_fast_path(tmp_path, monkeypatch):
    calls = {"capture": 0}
    real = runner_mod._capture_bytes
    monkeypatch.setattr(runner_mod, "_capture_bytes",
                        lambda *a, **k: (calls.__setitem__("capture", calls["capture"] + 1),
                                         real(*a, **k))[1])
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version="injected")
    r = LiveRunner(cfg, session=None,
                   pipeline=lambda c, d: _frame([{"trade_id": "L_1", "outcome": "UNFILLED"}]),
                   input_provider=lambda: snap)
    r.run_once()                                          # bootstrap via provider
    out = r.run_once()                                    # identical provider snapshot
    assert out["status"] == "no_new_bar"                  # C3 slow-path skip
    assert calls["capture"] == 0                          # fast-path machinery never engaged


# ── invariants: schema + saves (cases 25, 26) ────────────────────────────────
def test_no_durable_state_schema_change(tmp_path):
    """The durable key set, pinned exactly.

    MS-A replaced the legacy `daily` single bucket — which could hold one day and
    reset itself, so a delayed prior-day close erased the current day — with
    `realized_r_by_date`, a bounded per-date map. That was the one approved
    schema change; the guard is updated to the new canonical shape and keeps its
    original strictness (exact set equality), so any FURTHER key still fails.
    """
    cfg, r, _ = _primed(tmp_path)
    r.run_once()                                          # fast skip
    raw = json.loads((cfg.state_dir / "runner_state.json").read_text())
    assert set(raw.keys()) == {"last_boundary", "last_recomputed_input_revision",
                               "prev_frame_hash", "prev_frame_file", "ledger",
                               "mirror", "realized_r_by_date", "updated_at"}
    assert "daily" not in raw          # the legacy bucket is gone, not shadowed


def test_no_additional_save(tmp_path, monkeypatch):
    cfg, r, _ = _primed(tmp_path)
    saves = {"n": 0}
    real_save = r.state.save
    monkeypatch.setattr(r.state, "save",
                        lambda: (saves.__setitem__("n", saves["n"] + 1), real_save())[1])
    assert r.run_once()["status"] == "no_new_bar"         # fast skip
    assert saves["n"] == 0                                # zero saves on skip
    _write_frozen(cfg, _candles_bytes(n=80))              # boundary advance
    r.run_once()
    assert saves["n"] == 1                                # exactly the ONE existing save
