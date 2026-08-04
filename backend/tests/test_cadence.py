"""C5 — adaptive cadence. Fast unit tests only (never invokes the main loop).

Pins the locked transition table (defined over SEMANTIC outcomes), the literal
field mapping, saturation, reset precedence, purity/immutability (N6), the
sleep bounds (N1), and — via integration-lite real cycles — that the scheduler's
expected record fields match what live.main.cycle actually produces, with no
cadence identifier leaking into any persisted output.
"""

from __future__ import annotations

import copy
import dataclasses
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import main as live_main                                  # noqa: E402
from live.config import LiveConfig                                  # noqa: E402
from live.main import (CADENCE_BASE_SLEEP_S, CADENCE_MAX_IDLE_SLEEP_S,  # noqa: E402
                       _CadenceState, _cadence_transition)
from live.ops_log import OpsLog                                     # noqa: E402
from live.runner import LiveRunner                                  # noqa: E402

BASE = CADENCE_BASE_SLEEP_S
MAX = CADENCE_MAX_IDLE_SLEEP_S
ACTIVE = _CadenceState("active", BASE)
IDLE20 = _CadenceState("idle", 20.0)
IDLE40 = _CadenceState("idle", 40.0)
IDLE60 = _CadenceState("idle", MAX)
ALL_START_STATES = (ACTIVE, IDLE20, IDLE40, IDLE60)


def _rec(status="no_new_bar", appended=0, error="", frozen=False, **extra) -> dict:
    r = {"status": status, "bars_appended": appended, "error": error,
         "frozen": frozen, "boundary": "B", "intents": 0, "duration_s": 0.1,
         "reconcile_findings": [], "published": None,
         "stage_timings": {"runner_s": 0.1}, "phase_timings": {}}
    r.update(extra)
    return r


# ── transition table (every semantic outcome × every start state) ────────────
@pytest.mark.parametrize("start", ALL_START_STATES)
@pytest.mark.parametrize("record,label", [
    (_rec(status="ok"), "EVALUATION_OCCURRED (ok)"),
    (_rec(status="bootstrap"), "EVALUATION_OCCURRED (bootstrap)"),
    (_rec(appended=3), "BARS_APPENDED"),
    (_rec(error="RuntimeError: x"), "ERROR_OCCURRED"),
    (_rec(frozen=True), "FROZEN_OCCURRED"),
    (_rec(status="frozen_pending_recovery", frozen=True), "FROZEN status+flag"),
    (_rec(status="???unknown???"), "unknown outcome (fail-safe)"),
])
def test_reset_outcomes_from_every_state(start, record, label):
    out = _cadence_transition(start, record)
    assert out == _CadenceState("active", BASE), label


@pytest.mark.parametrize("start,expected_sleep", [
    (ACTIVE, 20.0), (IDLE20, 40.0), (IDLE40, 60.0), (IDLE60, 60.0)])
def test_idle_progression_from_every_state(start, expected_sleep):
    out = _cadence_transition(start, _rec())              # pure quiet idle
    assert out.mode == "idle" and out.sleep_s == expected_sleep


# ── saturation ───────────────────────────────────────────────────────────────
def test_saturation_sequence_exact():
    state = _CadenceState("active", BASE)
    sleeps = []
    for _ in range(5):
        state = _cadence_transition(state, _rec())
        sleeps.append(state.sleep_s)
    assert sleeps == [20.0, 40.0, 60.0, 60.0, 60.0]       # 10 -> 20 -> 40 -> 60 -> 60 -> 60


# ── reset precedence from deep idle ──────────────────────────────────────────
@pytest.mark.parametrize("record", [
    _rec(status="no_new_bar", appended=5),                # mixed: idle status + new bars
    _rec(status="no_new_bar", error="boom"),
    _rec(status="no_new_bar", frozen=True),
    _rec(status="ok"),                                    # evaluation
])
def test_reset_precedence_beats_idle(record):
    assert _cadence_transition(IDLE60, record) == _CadenceState("active", BASE)


# ── startup / restart ────────────────────────────────────────────────────────
def test_startup_and_restart_state_is_active_base():
    assert _CadenceState("active", CADENCE_BASE_SLEEP_S) == ACTIVE
    # simulated restart: a brand-new state object is exactly the startup state
    fresh = _CadenceState("active", CADENCE_BASE_SLEEP_S)
    assert fresh.mode == "active" and fresh.sleep_s == BASE
    # no persistence surface: the class carries only the two declared fields
    assert {f.name for f in dataclasses.fields(_CadenceState)} == {"mode", "sleep_s"}


# ── purity / immutability (N6) + bounds (N1) ─────────────────────────────────
def test_purity_identical_inputs_identical_outputs():
    rec = _rec()
    outs = {_cadence_transition(IDLE20, rec) for _ in range(5)}
    assert len(outs) == 1


def test_record_is_never_mutated():
    for record in [_rec(), _rec(status="ok"), _rec(appended=2), _rec(error="e"),
                   _rec(frozen=True), _rec(status="???")]:
        before = copy.deepcopy(record)
        for start in ALL_START_STATES:
            _cadence_transition(start, record)
        assert record == before                            # deep-equal, nested included


def test_cadence_state_is_immutable_and_not_input_object():
    out = _cadence_transition(ACTIVE, _rec())
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.sleep_s = 999.0
    with pytest.raises(dataclasses.FrozenInstanceError):
        out.mode = "x"
    assert out is not ACTIVE                               # never returns an input object


def test_sleep_bounds_hold_over_full_grid():
    records = [_rec(), _rec(status="ok"), _rec(status="bootstrap"), _rec(appended=1),
               _rec(error="e"), _rec(frozen=True), _rec(status="unknown"),
               _rec(status="no_new_bar", appended=9)]
    for start in ALL_START_STATES:
        for rec in records:
            out = _cadence_transition(start, rec)
            assert BASE <= out.sleep_s <= MAX              # N1
            assert out.mode in ("active", "idle")


# ── semantic mapping pinned against today's literals ─────────────────────────
def test_literal_mapping_pins():
    assert _cadence_transition(IDLE40, _rec(status="ok")).sleep_s == BASE
    assert _cadence_transition(IDLE40, _rec(status="bootstrap")).sleep_s == BASE
    assert _cadence_transition(IDLE40, _rec(status="no_new_bar")).mode == "idle"
    assert _cadence_transition(IDLE40, _rec(appended=1)).sleep_s == BASE
    assert _cadence_transition(IDLE40, _rec(error="x")).sleep_s == BASE
    assert _cadence_transition(IDLE40, _rec(frozen=True)).sleep_s == BASE
    assert _cadence_transition(IDLE40, _rec(status="not_a_status")).sleep_s == BASE
    # missing keys entirely -> fail-safe reset, never a crash (total function)
    assert _cadence_transition(IDLE40, {}) == _CadenceState("active", BASE)
    assert _cadence_transition(IDLE40, {"bars_appended": None}).sleep_s == BASE


# ── integration-lite: real cycle records through the real transition ─────────
HEADER = b"time,open,high,low,close,volume\n"


class _StubBridge:
    def poll_once(self):
        return {"ok": True, "appended": 0, "last_bar_time": None}


class _StubPublisher:
    def build_payload(self, *a, **k):
        return {}

    def publish(self, payload):
        return None


def _candles_bytes(n=40):
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=n,
                          freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")
    return HEADER + b"".join(f"{t},1.0,1.0,1.0,1.0,0\n".encode() for t in times)


def _trade_frame():
    cols = ["trade_id", "direction", "detection_time", "fill_time", "outcome", "entry", "stop", "tp"]
    return pd.DataFrame([{c: "" for c in cols} | {"trade_id": "L_1",
                        "outcome": "UNFILLED"}])[cols].astype(str)


def test_integration_lite_real_records_drive_transitions(tmp_path):
    cfg = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                     market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    cfg.ensure_dirs()
    (cfg.lux_root / "data" / "candles").mkdir(parents=True)
    (cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
     ).write_bytes(_candles_bytes())
    ops = OpsLog(cfg.state_dir)
    runner = LiveRunner(cfg, session=None, pipeline=lambda c, d: _trade_frame())

    rec1 = live_main.cycle(cfg, None, _StubBridge(), runner, None, _StubPublisher(), ops)
    rec2 = live_main.cycle(cfg, None, _StubBridge(), runner, None, _StubPublisher(), ops)

    # real record field names satisfy the scheduler's expectations
    for key in ("status", "bars_appended", "error", "frozen"):
        assert key in rec1 and key in rec2

    state = _CadenceState("active", CADENCE_BASE_SLEEP_S)
    state = _cadence_transition(state, rec1)              # bootstrap evaluation
    assert rec1["status"] == "bootstrap" and state == _CadenceState("active", BASE)
    state = _cadence_transition(state, rec2)              # quiet no_new_bar
    assert rec2["status"] == "no_new_bar" and state == _CadenceState("idle", 20.0)

    # no cadence identifier leaks into any persisted output
    blobs = [(cfg.state_dir / "runner_state.json").read_text(),
             (cfg.state_dir / "ops" / "cycles.jsonl").read_text(),
             (cfg.state_dir / "ops" / "heartbeat.json").read_text()]
    liveness_file = cfg.state_dir / "ops" / "liveness.json"
    if liveness_file.exists():
        blobs.append(liveness_file.read_text())
    for token in ("_CadenceState", "cadence", "next_sleep",
                  "CADENCE_BASE_SLEEP_S", "CADENCE_MAX_IDLE_SLEEP_S"):
        assert not any(token in blob for blob in blobs), token
