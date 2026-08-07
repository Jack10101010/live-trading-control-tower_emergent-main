"""M-OB-ID-GUARD — identity continuity is proven, not assumed.

The failure being guarded: trade_id comes from sequential numbering over a
window- and parameter-relative detection set, while diff_frontier keys every
broker transition on trade_id equality. A rebased identity space makes the
diff manufacture transitions that never happened — and each one is an order.

These tests drive the guard through a real LiveRunner with an injected
pipeline, so refusal is proven at the integration point (no intents, no
boundary advance, executor-gating status), not just at the module level.
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

from live.identity_guard import (ANCHOR_FIELDS, SCHEMA, IdentityGuard,  # noqa: E402
                                 config_digest)
from live.runner import LiveRunner  # noqa: E402


# ── fixtures ─────────────────────────────────────────────────────────────────

def frame(rows):
    """Minimal engine-output frame: identity anchors + diffable state."""
    return pd.DataFrame([{
        "trade_id": r[0], "ob_id": r[1], "direction": r[2],
        "detection_time": r[3], "outcome": r[4] if len(r) > 4 else "PENDING",
        "entry": "1.1", "stop": "1.0", "tp": "1.3", "fill_time": "",
    } for r in rows]).astype(str)


BASE = [("S_1", "7", "bearish", "2024-01-02 10:00:00+00:00"),
        ("L_2", "9", "bullish", "2024-01-03 11:15:00+00:00")]
EXTENDED = BASE + [("S_3", "12", "bearish", "2024-01-04 09:30:00+00:00")]


class Cfg:
    def __init__(self, root):
        self.state_dir = root
        self.market_data_dir = root / "md"
        self.golden_config_path = None
    def ensure_dirs(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        (self.state_dir / "frames").mkdir(exist_ok=True)


def make_runner(tmp_path, frames, clock_offset_cycles: int = 0):
    """Runner whose pipeline yields successive frames and whose candles
    provider advances one boundary per call. `clock_offset_cycles` lets a
    second runner over the same state_dir start past the committed boundary
    (otherwise its first cycle short-circuits at no_new_bar)."""
    seq = {"i": clock_offset_cycles}
    def candles():
        seq["i"] += 1
        t = pd.Timestamp("2026-01-05 10:00:00+00:00") + pd.Timedelta(minutes=15 * seq["i"])
        return pd.DataFrame({"time": [str(t)]})
    frames_iter = iter(frames)
    last = {}
    def pipeline(candles_raw, frontier_date):
        try:
            last["f"] = next(frames_iter)
        except StopIteration:
            pass
        return last["f"]
    return LiveRunner(Cfg(tmp_path), session=None, pipeline=pipeline,
                      candles_provider=candles)


def run(runner):
    r = runner.run_once(defer_commit=False)
    return r


# ── bootstrap ────────────────────────────────────────────────────────────────

def test_zero_exposure_bootstrap_happens_exactly_once(tmp_path):
    runner = make_runner(tmp_path, [frame(BASE), frame(BASE)])
    r1 = run(runner)
    assert r1["status"] == "bootstrap"          # first frame = baseline
    w = json.loads((tmp_path / "identity_witness.json").read_text())
    assert w["schema"] == SCHEMA and len(w["anchors"]) == 2
    assert "bootstrapped_at" in w
    first_boot = w["bootstrapped_at"]
    r2 = run(runner)
    assert r2["status"] == "ok"
    w2 = json.loads((tmp_path / "identity_witness.json").read_text())
    assert w2["bootstrapped_at"] == first_boot, "bootstrap must not repeat"


def test_missing_witness_with_durable_state_fails_closed(tmp_path):
    runner = make_runner(tmp_path, [frame(BASE), frame(BASE)])
    run(runner)
    # simulate live history: a ledger entry exists, then the witness is lost
    runner.state.data["ledger"]["abc123"] = {"status": "sent"}
    (tmp_path / "identity_witness.json").unlink()
    r = run(runner)
    assert r["status"] == "identity_frozen" and r["frozen"] is True
    assert "witness_missing_with_durable_state" in r["error"]
    assert r["intents"] == []


# ── continuity accepted ──────────────────────────────────────────────────────

def test_append_only_extension_is_accepted_and_witnessed(tmp_path):
    runner = make_runner(tmp_path, [frame(BASE), frame(EXTENDED)])
    run(runner)
    r = run(runner)
    assert r["status"] == "ok"
    w = json.loads((tmp_path / "identity_witness.json").read_text())
    assert set(w["anchors"]) == {"S_1", "L_2", "S_3"}


def test_evolving_non_anchor_fields_do_not_refuse(tmp_path):
    """outcome/fill legitimately change; identity anchors do not."""
    decided = frame([BASE[0][:4] + ("WIN",), BASE[1]])
    runner = make_runner(tmp_path, [frame(BASE), decided])
    run(runner)
    assert run(runner)["status"] == "ok"


# ── drift refused ────────────────────────────────────────────────────────────

def _boot_then(tmp_path, second_frame):
    runner = make_runner(tmp_path, [frame(BASE), second_frame])
    run(runner)
    return runner, run(runner)


def test_window_start_drift_refused(tmp_path):
    moved = frame([("S_1", "7", "bearish", "2024-02-01 08:00:00+00:00"),
                   ("L_2", "9", "bullish", "2024-02-02 09:15:00+00:00")])
    runner, r = _boot_then(tmp_path, moved)
    assert r["status"] == "identity_frozen"
    # both window_start and anchors moved; either refusal is fail-closed,
    # but the reason must name identity, not crash
    assert "drift refused" in r["error"]


def test_detector_parameter_drift_refused_via_config_digest(tmp_path):
    cfg_file = tmp_path / "golden.json"
    cfg_file.write_text('{"p": 1}')
    g = IdentityGuard(tmp_path, config_digest=config_digest(cfg_file),
                      engine_version="v1")
    ok, _ = g.verify_and_extend(frame(BASE), {"ledger": {}, "mirror": {}})
    assert ok
    cfg_file.write_text('{"p": 2}')
    g2 = IdentityGuard(tmp_path, config_digest=config_digest(cfg_file),
                       engine_version="v1")
    ok, detail = g2.verify_and_extend(frame(BASE), {"ledger": {}, "mirror": {}})
    assert not ok and "config_drift" in detail


def test_engine_version_drift_refused(tmp_path):
    g = IdentityGuard(tmp_path, config_digest=None, engine_version="v1")
    assert g.verify_and_extend(frame(BASE), {})[0]
    g2 = IdentityGuard(tmp_path, config_digest=None, engine_version="v2")
    ok, detail = g2.verify_and_extend(frame(BASE), {})
    assert not ok and "engine_drift" in detail


def test_same_trade_id_different_meaning_refused(tmp_path):
    remapped = frame([("S_1", "7", "bearish", "2024-01-02 10:00:00+00:00"),
                      ("L_2", "31", "bullish", "2024-01-03 11:15:00+00:00")])
    runner, r = _boot_then(tmp_path, remapped)
    assert r["status"] == "identity_frozen"
    assert "anchor_changed" in r["error"] and "L_2" in r["error"]


def test_vanished_prior_trade_id_refused(tmp_path):
    runner, r = _boot_then(tmp_path, frame([BASE[0]]))   # L_2 gone
    assert r["status"] == "identity_frozen"
    assert "trade_id_vanished" in r["error"] and "L_2" in r["error"]


def test_malformed_witness_fails_closed(tmp_path):
    runner = make_runner(tmp_path, [frame(BASE), frame(BASE)])
    run(runner)
    (tmp_path / "identity_witness.json").write_text("{not json")
    r = run(runner)
    assert r["status"] == "identity_frozen"
    assert "witness_malformed" in r["error"]


# ── refusal reaches nothing downstream ───────────────────────────────────────

def test_refusal_advances_no_durable_state(tmp_path):
    runner = make_runner(tmp_path, [frame(BASE), frame([BASE[0]]), frame([BASE[0]])])
    r1 = run(runner)
    committed = runner.state.data["last_boundary"]
    r = run(runner)
    assert r["status"] == "identity_frozen"
    assert runner.state.data["last_boundary"] == committed, \
        "boundary advanced on refusal — the cycle would never retry"
    assert runner._pending_commit is None, "refusal staged a commit"
    # and the frozen cycle retries (and re-refuses) rather than being absorbed
    r3 = run(runner)
    assert r3["status"] == "identity_frozen"


def test_refusal_status_is_the_executor_gate(tmp_path):
    """live.main applies intents only on status 'ok' — pin that contract and
    that a frozen result carries no intents to apply."""
    src = (REPO_ROOT / "live" / "main.py").read_text(encoding="utf-8")
    assert 'runner_result.get("status") == "ok" and runner_result.get("intents")' in src
    runner, r = _boot_then(tmp_path, frame([BASE[0]]))
    assert r["status"] != "ok" and r["intents"] == []


def test_guard_runs_before_frame_staging_in_run_once():
    """Order in the source is load-bearing: verify_and_extend must precede
    load_prev_frame/diff_frontier/store_frame in run_once."""
    src = (REPO_ROOT / "live" / "runner.py").read_text(encoding="utf-8")
    body = src[src.index("def run_once"):src.index("def _engine_time_string")]
    assert body.index("verify_and_extend") < body.index("load_prev_frame")
    assert body.index("verify_and_extend") < body.index("diff_frontier")
    assert body.index("verify_and_extend") < body.index("store_frame")


def test_after_bootstrap_missing_witness_still_guards_after_restart(tmp_path):
    """A NEW runner instance (process restart) must see the same witness and
    keep refusing drift — the witness is durable, not in-memory."""
    runner = make_runner(tmp_path, [frame(BASE)])
    run(runner)
    runner2 = make_runner(tmp_path, [frame([BASE[0]])],       # L_2 vanished
                          clock_offset_cycles=1)
    r = run(runner2)
    assert r["status"] == "identity_frozen"
    assert "trade_id_vanished" in r["error"]


def test_frame_without_anchor_columns_fails_closed(tmp_path):
    """A frame that cannot state its own identity cannot prove continuity."""
    bad = pd.DataFrame([{"trade_id": "S_1", "outcome": "PENDING"}]).astype(str)
    g = IdentityGuard(tmp_path, config_digest=None, engine_version="v1")
    ok, detail = g.verify_and_extend(bad, {})
    assert not ok and "anchor_columns_missing" in detail
    assert not (tmp_path / "identity_witness.json").exists(), \
        "refusal must not bootstrap a witness"
