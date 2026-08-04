"""M-CAP-GUARD-1 — runner-level refusal behaviour.

Proves that when the guard refuses continuity:
  * no intent is generated,
  * durable state is NOT advanced (boundary/revision/frame/key unchanged),
  * the C4 fast-idle memo is not primed,
  * the status is not "ok", so main.cycle() cannot reach executor.apply() —
    i.e. no order, ledger or broker path is reachable after refusal.
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

import live.main as main_mod                                        # noqa: E402
from live.config import LiveConfig                                  # noqa: E402
from live.runner import LiveRunner                                  # noqa: E402

HEADER = b"time,open,high,low,close,volume\n"


def _cfg(tmp_path) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md",
                   kill_file=tmp_path / "state" / "KILL")
    c.ensure_dirs()
    (c.lux_root / "data" / "candles").mkdir(parents=True, exist_ok=True)
    return c


def _candles_bytes(n=40, start="2026-07-17 09:00:00+00:00") -> bytes:
    times = pd.date_range(start, periods=n, freq="1min").strftime(
        "%Y-%m-%d %H:%M:%S+00:00")
    return HEADER + b"".join(f"{t},1.0,1.0,1.0,1.0,0\n".encode() for t in times)


def _write_frozen(cfg, payload: bytes) -> None:
    (cfg.lux_root / "data" / "candles"
     / "EURUSD_1m_extended_2015_2026.csv").write_bytes(payload)


COLS = ["trade_id", "direction", "detection_time", "fill_time", "outcome",
        "entry", "stop", "tp"]


def _frame(rows):
    return pd.DataFrame([{c: r.get(c, "") for c in COLS} for r in rows]).astype(str)


def _runner(cfg, frames):
    """Runner whose pipeline yields `frames` in order (last one repeats)."""
    state = {"i": 0}

    def pipeline(candles, frontier_date):
        f = frames[min(state["i"], len(frames) - 1)]
        state["i"] += 1
        return f

    return LiveRunner(cfg, session=None, pipeline=pipeline), state


def _durable(cfg) -> dict:
    return json.loads((cfg.state_dir / "runner_state.json").read_text())


# ── the rebase scenario ──────────────────────────────────────────────────────

def _rebase_setup(tmp_path):
    """Cycle 1 establishes L_1@t1; cycle 2 rebases so L_1 names a different OB."""
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    good = _frame([{"trade_id": "L_1", "direction": "bullish",
                    "detection_time": "t1", "fill_time": "", "outcome": ""}])
    rebased = _frame([{"trade_id": "L_1", "direction": "bullish",
                       "detection_time": "t2",      # same id, different OB
                       "fill_time": "2026-07-17 09:30:00+00:00", "outcome": ""}])
    r, _ = _runner(cfg, [good, rebased])
    assert r.run_once()["status"] == "bootstrap"
    _write_frozen(cfg, _candles_bytes(n=80))         # advance the boundary
    return cfg, r


def test_rebase_is_refused_and_emits_no_intents(tmp_path):
    cfg, r = _rebase_setup(tmp_path)
    out = r.run_once()
    assert out["status"] == "identity_drift_frozen"
    assert out["intents"] == []
    assert out["identity_guard"]["ok"] is False
    assert out["identity_guard"]["reason"] == "trade_id_coordinate_conflict"


def test_refusal_does_not_advance_durable_state(tmp_path):
    cfg, r = _rebase_setup(tmp_path)
    before = _durable(cfg)
    r.run_once()
    after = _durable(cfg)
    for key in ("last_boundary", "last_recomputed_input_revision",
                "prev_frame_hash", "prev_frame_identity_key"):
        assert after[key] == before[key], f"{key} advanced despite refusal"


def test_refusal_reserves_no_pending_intent_in_the_ledger(tmp_path):
    """The ledger is the broker-facing idempotency record. Nothing may enter it."""
    cfg, r = _rebase_setup(tmp_path)
    before = dict(_durable(cfg)["ledger"])
    r.run_once()
    assert _durable(cfg)["ledger"] == before


def test_refusal_does_not_prime_the_fast_idle_memo(tmp_path):
    """A refused evaluation must never certify a (revision, boundary) pair."""
    cfg, r = _rebase_setup(tmp_path)
    memo_before = r._fast_idle_memo
    r.run_once()
    assert r._fast_idle_memo is memo_before


def test_refusal_status_cannot_reach_executor_apply(tmp_path):
    """main.cycle() calls executor.apply ONLY on status == 'ok' with intents.
    This pins that the refusal status is inert for execution."""
    cfg, r = _rebase_setup(tmp_path)
    out = r.run_once()
    assert out["status"] != "ok"
    assert not (out.get("status") == "ok" and out.get("intents"))


def test_refusal_is_repeatable_not_self_clearing(tmp_path):
    """A refusal must not 'heal' by advancing state — it must keep refusing
    until the operator resolves it."""
    cfg, r = _rebase_setup(tmp_path)
    assert r.run_once()["status"] == "identity_drift_frozen"
    _write_frozen(cfg, _candles_bytes(n=120))
    assert r.run_once()["status"] == "identity_drift_frozen"


def test_vanished_prior_trade_is_refused(tmp_path):
    """The CLOSE-suppression hazard, end to end through the runner."""
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    good = _frame([
        {"trade_id": "L_1", "direction": "bullish", "detection_time": "t1",
         "fill_time": "2026-07-17 09:10:00+00:00", "outcome": ""},
        {"trade_id": "L_2", "direction": "bullish", "detection_time": "t2"}])
    lost = _frame([{"trade_id": "L_2", "direction": "bullish",
                    "detection_time": "t2"}])          # L_1 (OPEN) disappeared
    r, _ = _runner(cfg, [good, lost])
    r.run_once()
    _write_frozen(cfg, _candles_bytes(n=80))
    out = r.run_once()
    assert out["status"] == "identity_drift_frozen"
    assert out["identity_guard"]["reason"] == "prior_trade_id_absent_from_current_frame"


# ── normal operation is unaffected ───────────────────────────────────────────

def test_append_only_growth_still_produces_intents(tmp_path):
    """Negative control: the guard must not block legitimate continuity."""
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    first = _frame([{"trade_id": "L_1", "direction": "bullish",
                     "detection_time": "t1", "fill_time": "", "outcome": ""}])
    second = _frame([
        {"trade_id": "L_1", "direction": "bullish", "detection_time": "t1",
         "fill_time": "2026-07-17 09:30:00+00:00", "outcome": "",
         "entry": "1.0", "stop": "0.9", "tp": "1.2"},
        {"trade_id": "L_2", "direction": "bullish", "detection_time": "t2"}])
    r, _ = _runner(cfg, [first, second])
    assert r.run_once()["status"] == "bootstrap"
    _write_frozen(cfg, _candles_bytes(n=80))
    out = r.run_once()
    assert out["status"] == "ok", out.get("note")
    assert [i.action for i in out["intents"]] == ["OPEN_POSITION"]
    assert _durable(cfg)["prev_frame_identity_key"]


def test_identity_key_is_persisted_and_stable_across_cycles(tmp_path):
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    f = _frame([{"trade_id": "L_1", "direction": "bullish",
                 "detection_time": "t1"}])
    r, _ = _runner(cfg, [f])
    r.run_once()
    k1 = _durable(cfg)["prev_frame_identity_key"]
    assert k1
    _write_frozen(cfg, _candles_bytes(n=80))
    r.run_once()
    assert _durable(cfg)["prev_frame_identity_key"] == k1


def test_missing_stored_key_fails_closed(tmp_path):
    """Negative control: corrupt the durable identity metadata directly."""
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    f = _frame([{"trade_id": "L_1", "direction": "bullish",
                 "detection_time": "t1"}])
    r, _ = _runner(cfg, [f])
    r.run_once()
    r.state.data["prev_frame_identity_key"] = None       # simulate loss
    r.state.save()
    _write_frozen(cfg, _candles_bytes(n=80))
    out = r.run_once()
    assert out["status"] == "identity_drift_frozen"
    assert out["identity_guard"]["reason"] == "missing_prior_identity_key"


def test_corrupted_stored_key_fails_closed(tmp_path):
    """Negative control: a wrong (not merely absent) key must also refuse."""
    cfg = _cfg(tmp_path)
    _write_frozen(cfg, _candles_bytes())
    f = _frame([{"trade_id": "L_1", "direction": "bullish",
                 "detection_time": "t1"}])
    r, _ = _runner(cfg, [f])
    r.run_once()
    r.state.data["prev_frame_identity_key"] = "0" * 64
    r.state.save()
    _write_frozen(cfg, _candles_bytes(n=80))
    out = r.run_once()
    assert out["status"] == "identity_drift_frozen"
    assert out["identity_guard"]["reason"] == "identity_space_key_mismatch"


def test_cycle_records_refusal_without_calling_executor(tmp_path, monkeypatch):
    """End-to-end through main.cycle(): the executor's apply() is never called."""
    cfg, r = _rebase_setup(tmp_path)

    class _Bridge:
        def poll_once(self): return {"appended": 0, "last_bar_time": None}

    class _Executor:
        applied = 0
        arm_runtime = None
        observed = None
        def drain_pending(self): return None
        def account_closed_deals(self): return None
        def apply(self, intents, today=None):
            type(self).applied += 1
            return {}

    class _Publisher:
        def build_payload(self, *a, **k): return {}
        def publish(self, payload): return {"delivered": False}

    class _Ops:
        def cycle_start(self): return {"cycle_start": "x"}
        def cycle_end(self, record, **fields):
            record.update(fields); return record

    rec = main_mod.cycle(cfg, None, _Bridge(), r, _Executor(), _Publisher(), _Ops())
    assert _Executor.applied == 0
    assert rec["status"] == "identity_drift_frozen"
    assert rec["intents"] == 0
