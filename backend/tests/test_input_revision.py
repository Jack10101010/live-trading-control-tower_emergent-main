"""C3 — InputRevision / InputSnapshot / LR-1 gate. Fast unit tests only.

Covers the locked identity semantics (canonical preimage, byte-derived revision,
TOCTOU-free capture, immutability), the assembly contract, the full boundary ×
revision truth table, and the single-atomic-commit guarantee.
"""

from __future__ import annotations

import dataclasses
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.config import LiveConfig                                     # noqa: E402
from live.runner import (INPUT_REVISION_VERSION, LIVE_ABSENT, InputSnapshot,  # noqa: E402
                         LiveRunner, _input_revision_preimage, assemble_candles,
                         capture_input_snapshot, snapshot_from_bytes)
from live.state import LEDGER_PENDING, RunnerState                     # noqa: E402

HEADER = b"time,open,high,low,close,volume\n"
ENGINE = "abc123"


def _cfg(tmp_path) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    c.ensure_dirs()
    return c


def _candles_bytes(n=40, start="2026-07-17 09:00:00+00:00", close="1.0") -> bytes:
    times = pd.date_range(start, periods=n, freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")
    return HEADER + b"".join(f"{t},1.0,1.0,1.0,{close},0\n".encode() for t in times)


def _frame(rows):
    cols = ["trade_id", "direction", "fill_time", "outcome", "entry", "stop", "tp"]
    return pd.DataFrame([{c: r.get(c, "") for c in cols} for r in rows]).astype(str)


def _runner(cfg, snapshot, frames):
    calls = {"n": 0}

    def pipeline(candles, frontier_date):
        frame = frames[min(calls["n"], len(frames) - 1)]
        calls["n"] += 1
        return frame

    return LiveRunner(cfg, session=None, pipeline=pipeline, input_provider=lambda: snapshot)


# ── identity: determinism & sensitivity ──────────────────────────────────────
def test_identical_bytes_and_engine_produce_identical_revision():
    b = _candles_bytes()
    a = snapshot_from_bytes(b, None, engine_version=ENGINE)
    c = snapshot_from_bytes(b, None, engine_version=ENGINE)
    assert a.input_revision == c.input_revision


def test_repeated_construction_is_deterministic():
    b, live = _candles_bytes(), _candles_bytes(n=5, start="2026-07-17 10:00:00+00:00")
    revs = {snapshot_from_bytes(b, live, engine_version=ENGINE).input_revision
            for _ in range(5)}
    assert len(revs) == 1


def test_one_frozen_byte_change_changes_revision():
    a = snapshot_from_bytes(_candles_bytes(close="1.0"), None, engine_version=ENGINE)
    b = snapshot_from_bytes(_candles_bytes(close="1.1"), None, engine_version=ENGINE)
    assert a.input_revision != b.input_revision


def test_one_live_byte_change_changes_revision():
    frozen = _candles_bytes()
    l1 = _candles_bytes(n=5, start="2026-07-17 10:00:00+00:00", close="1.0")
    l2 = _candles_bytes(n=5, start="2026-07-17 10:00:00+00:00", close="1.2")
    assert (snapshot_from_bytes(frozen, l1, engine_version=ENGINE).input_revision
            != snapshot_from_bytes(frozen, l2, engine_version=ENGINE).input_revision)


def test_absent_live_is_deterministic():
    b = _candles_bytes()
    assert (snapshot_from_bytes(b, None, engine_version=ENGINE).input_revision
            == snapshot_from_bytes(b, None, engine_version=ENGINE).input_revision)


def test_absent_live_differs_from_actual_live_stream():
    frozen = _candles_bytes()
    live = _candles_bytes(n=5, start="2026-07-17 10:00:00+00:00")
    assert (snapshot_from_bytes(frozen, None, engine_version=ENGINE).input_revision
            != snapshot_from_bytes(frozen, live, engine_version=ENGINE).input_revision)


# ── canonical preimage ───────────────────────────────────────────────────────
def test_canonical_preimage_exact_format_and_ordering():
    pre = _input_revision_preimage("f" * 64, "l" * 64, ENGINE)
    assert pre == (f"version={INPUT_REVISION_VERSION}\n"
                   f"frozen_sha256={'f' * 64}\n"
                   f"live_sha256={'l' * 64}\n"
                   f"engine_version={ENGINE}")
    assert pre.split("\n")[0].startswith("version=")          # fixed field order
    assert [line.split("=", 1)[0] for line in pre.split("\n")] == [
        "version", "frozen_sha256", "live_sha256", "engine_version"]


def test_canonical_preimage_has_no_trailing_newline():
    assert not _input_revision_preimage("f" * 64, LIVE_ABSENT, ENGINE).endswith("\n")


def test_engine_version_is_stripped_and_lowercased():
    b = _candles_bytes()
    base = snapshot_from_bytes(b, None, engine_version="abc123").input_revision
    assert snapshot_from_bytes(b, None, engine_version="  ABC123  ").input_revision == base
    assert snapshot_from_bytes(b, None, engine_version="AbC123").input_revision == base
    assert snapshot_from_bytes(b, None, engine_version="different").input_revision != base


def test_absent_live_uses_the_literal_sentinel():
    assert LIVE_ABSENT == "absent"
    assert "live_sha256=absent" in _input_revision_preimage("f" * 64, LIVE_ABSENT, ENGINE)


# ── assembly contract ────────────────────────────────────────────────────────
def test_bytes_parsing_matches_assemble_candles_path_contract(tmp_path):
    frozen_b = _candles_bytes(n=30)
    live_b = _candles_bytes(n=5, start="2026-07-17 09:30:00+00:00", close="2.0")
    fp, lp = tmp_path / "frozen.csv", tmp_path / "live.csv"
    fp.write_bytes(frozen_b)
    lp.write_bytes(live_b)
    from_path = assemble_candles(fp, lp)
    from_bytes = snapshot_from_bytes(frozen_b, live_b, engine_version=ENGINE).candles
    pd.testing.assert_frame_equal(from_path, from_bytes)


def test_frozen_wins_and_live_after_frozen_end_unchanged():
    frozen_b = _candles_bytes(n=30)                                   # 09:00..09:29
    live_b = _candles_bytes(n=10, start="2026-07-17 09:25:00+00:00", close="9.9")
    out = snapshot_from_bytes(frozen_b, live_b, engine_version=ENGINE).candles
    times = pd.to_datetime(out["time"])
    assert len(out) == 30 + 5                                         # only 09:30..09:34 kept
    assert times.is_monotonic_increasing
    overlap = out[times <= pd.Timestamp("2026-07-17 09:29:00+00:00")]
    assert set(overlap["close"].astype(str)) == {"1.0"}               # frozen rows win


# ── TOCTOU / capture ─────────────────────────────────────────────────────────
def test_captured_bytes_remain_the_source_after_the_path_changes(tmp_path):
    fp, lp = tmp_path / "frozen.csv", tmp_path / "live.csv"
    fp.write_bytes(_candles_bytes(n=20))
    snap = capture_input_snapshot(fp, lp, engine_version=ENGINE)      # live absent
    rows_at_capture, rev_at_capture = len(snap.candles), snap.input_revision
    fp.write_bytes(_candles_bytes(n=99, close="7.7"))                 # mutate AFTER capture
    assert len(snap.candles) == rows_at_capture                       # snapshot unaffected
    assert snap.input_revision == rev_at_capture
    assert capture_input_snapshot(fp, lp, engine_version=ENGINE).input_revision != rev_at_capture


def test_capture_handles_absent_live_segment(tmp_path):
    fp, lp = tmp_path / "frozen.csv", tmp_path / "missing.csv"
    fp.write_bytes(_candles_bytes(n=10))
    snap = capture_input_snapshot(fp, lp, engine_version=ENGINE)
    assert len(snap.candles) == 10
    assert snap.input_revision == snapshot_from_bytes(
        _candles_bytes(n=10), None, engine_version=ENGINE).input_revision


# ── immutability ─────────────────────────────────────────────────────────────
def test_input_snapshot_attributes_cannot_be_reassigned():
    snap = snapshot_from_bytes(_candles_bytes(n=5), None, engine_version=ENGINE)
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.input_revision = "x" * 64
    with pytest.raises(dataclasses.FrozenInstanceError):
        snap.candles = pd.DataFrame()
    assert set(f.name for f in dataclasses.fields(InputSnapshot)) == {"candles", "input_revision"}


# ── LR-1 truth table ─────────────────────────────────────────────────────────
def _bootstrap(cfg, snap):
    f0 = _frame([{"trade_id": "L_1", "direction": "bullish", "outcome": "UNFILLED"}])
    r = _runner(cfg, snap, [f0])
    return r.run_once()


def test_bootstrap_state_evaluates(tmp_path):
    cfg = _cfg(tmp_path)
    out = _bootstrap(cfg, snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE))
    assert out["status"] == "bootstrap"


def test_unchanged_boundary_unchanged_revision_skips(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)
    _bootstrap(cfg, snap)
    again = _runner(cfg, snap, [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])]).run_once()
    assert again["status"] == "no_new_bar"


def test_unchanged_boundary_changed_revision_evaluates(tmp_path):
    cfg = _cfg(tmp_path)
    base = _candles_bytes()
    _bootstrap(cfg, snapshot_from_bytes(base, None, engine_version=ENGINE))
    # Same final timestamp (same boundary) but corrected content -> new revision.
    corrected = base.replace(b",1.0,0\n", b",1.5,0\n")
    corrected_snap = snapshot_from_bytes(corrected, None, engine_version=ENGINE)
    assert corrected_snap.input_revision != snapshot_from_bytes(
        base, None, engine_version=ENGINE).input_revision
    out = _runner(cfg, corrected_snap,
                  [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])]).run_once()
    assert out["status"] != "no_new_bar"           # correction forces re-evaluation


def test_changed_boundary_unchanged_revision_evaluates(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)
    _bootstrap(cfg, snap)
    st = RunnerState(cfg.state_dir)                # stored revision kept, boundary differs
    st.data["last_boundary"] = "2020-01-01 00:00:00+00:00"
    st.save()
    out = _runner(cfg, snap, [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])]).run_once()
    assert out["status"] != "no_new_bar"


def test_changed_boundary_changed_revision_evaluates(tmp_path):
    cfg = _cfg(tmp_path)
    _bootstrap(cfg, snapshot_from_bytes(_candles_bytes(n=40), None, engine_version=ENGINE))
    later = snapshot_from_bytes(_candles_bytes(n=80), None, engine_version=ENGINE)
    out = _runner(cfg, later, [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])]).run_once()
    assert out["status"] != "no_new_bar"


def test_missing_stored_revision_evaluates(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)
    _bootstrap(cfg, snap)
    raw = json.loads((cfg.state_dir / "runner_state.json").read_text())
    raw.pop("last_recomputed_input_revision")      # simulate a pre-C3 state file
    (cfg.state_dir / "runner_state.json").write_text(json.dumps(raw))
    out = _runner(cfg, snap, [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])]).run_once()
    assert out["status"] != "no_new_bar"


# ── persistence ──────────────────────────────────────────────────────────────
def test_revision_survives_state_save_and_load(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)
    _bootstrap(cfg, snap)
    assert RunnerState(cfg.state_dir).data["last_recomputed_input_revision"] == snap.input_revision


def test_store_frame_requires_input_revision(tmp_path):
    st = RunnerState(_cfg(tmp_path).state_dir)
    with pytest.raises(TypeError):
        st.store_frame(_frame([{"trade_id": "L_1"}]), "2026-07-17 10:00:00+00:00")


def test_store_frame_updates_boundary_and_revision_together(tmp_path):
    st = RunnerState(_cfg(tmp_path).state_dir)
    st.store_frame(_frame([{"trade_id": "L_1"}]), "B-1", "r" * 64)
    assert st.data["last_boundary"] == "B-1"
    assert st.data["last_recomputed_input_revision"] == "r" * 64


def test_pipeline_exception_persists_neither_boundary_nor_revision(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)

    def boom(candles, frontier_date):
        raise RuntimeError("pipeline exploded")

    runner = LiveRunner(cfg, session=None, pipeline=boom, input_provider=lambda: snap)
    with pytest.raises(RuntimeError, match="pipeline exploded"):
        runner.run_once()
    reloaded = RunnerState(cfg.state_dir).data
    assert reloaded["last_boundary"] is None
    assert reloaded["last_recomputed_input_revision"] is None


def test_pending_intents_and_boundary_revision_share_one_save(tmp_path):
    cfg = _cfg(tmp_path)
    snap1 = snapshot_from_bytes(_candles_bytes(n=40), None, engine_version=ENGINE)
    f0 = _frame([{"trade_id": "L_1", "direction": "bullish", "outcome": "UNFILLED"}])
    f1 = _frame([{"trade_id": "L_1", "direction": "bullish", "fill_time": "t",
                  "outcome": "OPEN", "entry": "1.1", "stop": "1.09", "tp": "1.12"}])
    _runner(cfg, snap1, [f0]).run_once()                       # bootstrap
    snap2 = snapshot_from_bytes(_candles_bytes(n=80), None, engine_version=ENGINE)
    out = _runner(cfg, snap2, [f1]).run_once()
    assert out["status"] == "ok" and out["intents"]
    data = RunnerState(cfg.state_dir).data                     # one atomic commit holds all three
    assert data["last_boundary"] and data["last_recomputed_input_revision"] == snap2.input_revision
    assert any(e["status"] == LEDGER_PENDING for e in data["ledger"].values())


def test_restart_same_boundary_and_revision_yields_no_extra_intents(tmp_path):
    cfg = _cfg(tmp_path)
    snap = snapshot_from_bytes(_candles_bytes(), None, engine_version=ENGINE)
    _bootstrap(cfg, snap)
    fresh = _runner(cfg, snap, [_frame([{"trade_id": "L_1", "outcome": "OPEN"}])])
    out = fresh.run_once()                                     # brand-new instance, same state dir
    assert out["status"] == "no_new_bar" and not out.get("intents")
