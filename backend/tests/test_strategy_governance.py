"""M-STRATEGY-AUTHORITY-2 — the tracked strategy cannot drift silently.

Three surfaces, one rule: the strategy the engine trades must be the strategy a
human reviewed. Covers the authority/runtime equality proof, its place in the
identity witness, the bounded decision feed, and the fingerprint_matches
reporting fix.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
LUX = REPO.parent / "Lux-OB-Backtester"

from live import strategy_authority as sa  # noqa: E402
from live.decisions import (MAX_RECORDS, SCHEMA_VERSION,  # noqa: E402
                            build_decision_records)


# ── PART 1/4: authority vs runtime equality, fail-closed in every direction ──

def _tree(tmp_path, authority=b'{"a":1}', runtime=b'{"a":1}'):
    (tmp_path / "strategy").mkdir(parents=True, exist_ok=True)
    (tmp_path / "generated_configs").mkdir(parents=True, exist_ok=True)
    if authority is not None:
        (tmp_path / sa.AUTHORITY_RELPATH).write_bytes(authority)
    rt = tmp_path / "generated_configs" / "cfg.json"
    if runtime is not None:
        rt.write_bytes(runtime)
    return tmp_path, rt


def test_identical_authority_and_runtime_verify(tmp_path):
    root, rt = _tree(tmp_path)
    ok, detail, dig = sa.verify(root, rt)
    assert ok and "verified" in detail and len(dig) == 64


def test_one_byte_difference_refuses(tmp_path):
    root, rt = _tree(tmp_path, authority=b'{"a":1}', runtime=b'{"a":2}')
    ok, detail, _ = sa.verify(root, rt)
    assert not ok and sa.REFUSE_MISMATCH in detail
    assert "unreviewed strategy" in detail


def test_missing_authority_refuses(tmp_path):
    root, rt = _tree(tmp_path, authority=None)
    ok, detail, _ = sa.verify(root, rt)
    assert not ok and sa.REFUSE_MISSING_AUTHORITY in detail


def test_missing_runtime_config_refuses(tmp_path):
    root, rt = _tree(tmp_path, runtime=None)
    ok, detail, _ = sa.verify(root, rt)
    assert not ok and sa.REFUSE_MISSING_RUNTIME in detail


def test_absent_lux_root_refuses(tmp_path):
    ok, detail, _ = sa.verify(None, tmp_path / "x.json")
    assert not ok and sa.REFUSE_MISSING_AUTHORITY in detail


def test_authority_digest_is_none_when_unreadable(tmp_path):
    assert sa.authority_digest(tmp_path) is None      # nothing written yet


@pytest.mark.skipif(not LUX.exists(), reason="pinned Lux tree not present")
def test_the_REAL_deployment_authority_matches_the_running_config():
    """The production claim, not a fixture: the tracked strategy and the config
    the engine loads are byte-identical right now."""
    from live.config import LiveConfig
    cfg = LiveConfig()
    ok, detail, dig = sa.verify(cfg.lux_root, cfg.golden_config_path)
    assert ok, detail
    assert dig == sa.authority_digest(cfg.lux_root)


# ── PART 4: strategy digest is identity, so a change freezes the cycle ───────

def _frame():
    return pd.DataFrame([{"trade_id": "S_1", "ob_id": "7", "direction": "bearish",
                          "detection_time": "2024-01-02 10:00:00+00:00"}]).astype(str)


def _guard(state_dir, strategy_digest):
    from live.identity_guard import IdentityGuard
    return IdentityGuard(state_dir, config_digest="cfg", engine_version="v1",
                         policy_digest="pol", strategy_digest=strategy_digest)


def test_strategy_digest_is_recorded_in_the_witness(tmp_path):
    assert _guard(tmp_path, "abc123").verify_and_extend(_frame(), {})[0]
    w = json.loads((tmp_path / "identity_witness.json").read_text())
    assert w["strategy_digest"] == "abc123"


def test_changed_strategy_digest_freezes_the_cycle(tmp_path):
    assert _guard(tmp_path, "abc123").verify_and_extend(_frame(), {})[0]
    ok, detail = _guard(tmp_path, "def456").verify_and_extend(_frame(), {})
    assert not ok and "strategy_drift" in detail
    assert "IS the strategy" in detail


def test_unchanged_strategy_digest_is_accepted_on_restart(tmp_path):
    assert _guard(tmp_path, "abc123").verify_and_extend(_frame(), {})[0]
    assert _guard(tmp_path, "abc123").verify_and_extend(_frame(), {})[0]


# ── PART 5: bounded decision feed ───────────────────────────────────────────

def _decision_frame(n=3, **over):
    base = {"ob_id": "12", "trade_id": "L_1", "base_trade_id": "L_1",
            "detection_time": "2026-08-07 09:20:00+00:00", "direction": "bullish",
            "structure_tag": "bos_long", "fill_session": "London Lull",
            "portfolio_cohort_key": "EURUSD|lull|bos_long", "market_state": "Bull/Expand",
            "trend_state": "Bull", "volatility_state": "Expand", "chop_state": "No",
            "state_confirmed": "True", "state_eligibility": "", "regime_block_reason": "",
            "portfolio_decision_reason": "", "portfolio_confidence": "HIGH",
            "rr_multiple": "2.25", "entry": "1.1", "stop": "1.0", "tp": "1.3",
            "entry_model_key": "triggered_edge", "entry_depth_pct": "0",
            "news_blackout": "False", "news_blackout_event_time": "",
            "outcome": "WIN", "cancel_reason": "", "fill_time": "2026-08-07 09:37:00+00:00",
            "net_r": "2.25"}
    base.update(over)
    rows = []
    for i in range(n):
        r = dict(base)
        r["trade_id"] = f"L_{i+1}"
        r["detection_time"] = f"2026-08-0{i+1} 09:20:00+00:00"
        rows.append(r)
    return pd.DataFrame(rows).astype(str)


def test_decision_record_carries_the_fields_the_UI_needs():
    out = build_decision_records(_decision_frame(1))
    d = out["decisions"][0]
    for f in ("ob_id", "trade_id", "detection_time", "direction", "structure",
              "session", "cohort_key", "market_state", "trend_state",
              "volatility_state", "chop_state", "state_confirmed", "eligible",
              "target_rr", "entry", "stop", "tp", "action"):
        assert f in d, f"missing {f}"
    assert out["schema_version"] == SCHEMA_VERSION
    assert d["structure"] == "BOS" and d["action"] == "FILLED"
    # `eligible` was `action != "REFUSED"` under v1, which read `true` here.
    # Under v2 it answers "can this setup still open a position?", and this row
    # holds an open one — so it cannot open another. `true` is never published;
    # see `_eligible` in live/decisions.py.
    assert d["eligible"] is False
    assert d["lifecycle"] == "POSITION_OPEN" and d["terminal"] is False


def test_refusals_carry_a_reason_and_are_not_eligible():
    f = _decision_frame(1, outcome="STATE_BLOCKED", cancel_reason="state_target_block",
                        fill_time="")
    d = build_decision_records(f)["decisions"][0]
    assert d["action"] == "REFUSED" and d["eligible"] is False
    assert d["refusal_reason"] == "state_target_block"


def test_cohort_disabled_refusal_is_distinguished():
    f = _decision_frame(1, outcome="COHORT_DISABLED", cancel_reason="cohort_disabled",
                        fill_time="")
    d = build_decision_records(f)["decisions"][0]
    assert d["action"] == "REFUSED" and d["refusal_reason"] == "cohort_disabled"


def test_payload_is_bounded_and_reports_truncation():
    out = build_decision_records(_decision_frame(120), limit=10)
    assert out["count"] == 10 and out["truncated"] is True
    assert out["total_candidates"] == 120
    assert MAX_RECORDS <= 100


def test_ordering_is_deterministic_and_most_recent_first():
    f = _decision_frame(5)
    a = build_decision_records(f)["decisions"]
    b = build_decision_records(f)["decisions"]
    assert [d["trade_id"] for d in a] == [d["trade_id"] for d in b]
    assert a[0]["detection_time"] > a[-1]["detection_time"]


def test_no_authority_or_secret_field_can_appear():
    blob = json.dumps(build_decision_records(_decision_frame(3)))
    # The real account number is sourced from the environment, never written
    # here -- a guard that embeds the secret it protects fails its own rule.
    import os
    real = os.environ.get("MT5_LOGIN", "").strip()
    banned = ["node_mt5", "live_mt5", "admitted", "admissionReasons",
              "execution_authority", "login", "password"]
    if real.isdigit():
        banned.append(real)
    for banned in banned:
        assert banned not in blob, f"decision feed leaked {banned}"


def test_only_mapped_columns_are_published():
    """A new frame column must never reach telemetry by accident."""
    f = _decision_frame(1, )
    f["secret_new_column"] = "leak-me"
    blob = json.dumps(build_decision_records(f))
    assert "leak-me" not in blob and "secret_new_column" not in blob


def test_intent_id_is_attached_when_one_was_generated():
    from live.intents import OPEN_POSITION, OrderIntent
    i = OrderIntent(intent_id="abc123", action=OPEN_POSITION, trade_id="L_1",
                    side="long", frontier_bar="2026-08-07 09:30:00+00:00")
    d = build_decision_records(_decision_frame(1), intents=[i])["decisions"][0]
    assert d["intent_id"] == "abc123"


def test_empty_or_missing_frame_is_safe():
    out = build_decision_records(None)
    assert out["count"] == 0 and out["decisions"] == []


@pytest.mark.skipif(not LUX.exists(), reason="pinned Lux tree not present")
def test_session_triple_is_published_for_auditability():
    """UTC + London-local + tz, so an operator can CHECK the session, not trust it."""
    if str(LUX) not in sys.path:
        sys.path.insert(0, str(LUX))
    d = build_decision_records(_decision_frame(1))["decisions"][0]
    assert d.get("session_tz") == "Europe/London"
    assert d.get("session_local", "").startswith("2026-08-01T10:20")
    assert d.get("session_tz_abbrev") == "BST"


# ── PART 6: fingerprint_matches is reporting, never authority ───────────────

class _Ctx:
    def __init__(self, login, server):
        self.fingerprint = type("F", (), {"login": login, "server": server})()
        self.request_expires_at = "2099-01-01T00:00:00+00:00"
        self.probation_max_opens = 3


class _Arm:
    def __init__(self, login=111, server="FTMO-Demo"):
        self.context = _Ctx(login, server)
        self.remaining_attempts = 3
        self.disarmed = False


class _Ident:
    def __init__(self, login, server):
        self.login, self.server = login, server
        self.currency, self.trade_mode = "USD", 0


class _Cfg:
    symbol = "EURUSD"
    max_open_positions = 6
    fixed_risk_lots = 0.01
    daily_loss_limit_r = 5.0


def _snap(arm, identity):
    from live import telemetry as nt
    return nt.build_snapshot(
        instance_id="t", runner_result={}, executor_result={}, engine_version="v",
        mode="live", config=_Cfg(), state={}, arm_runtime=arm,
        observed={"identity": identity, "health": None, "health_verdict": None,
                  "observed_at": "2026-08-07T00:00:00Z"},
        bridge=None)


def test_matching_account_reports_true():
    snap = _snap(_Arm(login=111), _Ident(111, "FTMO-Demo"))
    assert snap["arming"]["fingerprint_matches"] is True
    assert "runtime_identity_not_evaluated" not in snap["runtime"]["open_eligibility"]["reasons"]


def test_mismatched_account_reports_false_and_blocks_eligibility():
    snap = _snap(_Arm(login=111), _Ident(999, "FTMO-Demo"))
    assert snap["arming"]["fingerprint_matches"] is False
    assert "runtime_identity_mismatch" in snap["runtime"]["open_eligibility"]["reasons"]


def test_unavailable_identity_stays_none_not_false():
    """'Not observed' is not evidence of a mismatch."""
    snap = _snap(_Arm(login=111), None)
    assert snap["arming"]["fingerprint_matches"] is None


def test_no_arm_reports_none():
    snap = _snap(None, _Ident(111, "FTMO-Demo"))
    assert snap["arming"]["fingerprint_matches"] is None
    assert snap["arming"]["armed"] is False


def test_telemetry_cannot_invent_authority():
    """The published record must not claim eligibility the rails did not grant."""
    snap = _snap(_Arm(login=111), _Ident(111, "FTMO-Demo"))
    assert snap["runtime"]["open_eligibility"]["eligible"] is False, \
        "telemetry granted eligibility on its own"
    blob = json.dumps(snap)
    for banned in ("node_mt5", "live_mt5", "admitted", "execution_authority"):
        assert banned not in blob


# ── decision-feed WIRING: additive, never altering the canonical contract ────

def _snapshot_with(decisions):
    from live import telemetry as nt
    return nt.build_snapshot(
        instance_id="t", runner_result={}, executor_result={}, engine_version="v",
        mode="dry_run", config=_Cfg(), state={}, arm_runtime=None, observed=None,
        bridge=None, decisions=decisions)


def test_decisions_block_is_absent_when_there_is_nothing_to_report():
    """A receiver that predates the block must see exactly the old payload."""
    for empty in (None, {}, {"decisions": []},
                  {"schema_version": "x", "decisions": []}):
        assert "decisions" not in _snapshot_with(empty)


def test_decisions_block_is_published_when_present():
    payload = build_decision_records(_decision_frame(2))
    snap = _snapshot_with(payload)
    assert snap["decisions"]["schema_version"] == SCHEMA_VERSION
    assert len(snap["decisions"]["decisions"]) == 2


def test_decisions_is_not_a_required_top_level_key():
    from live import telemetry as nt
    assert "decisions" not in nt.REQUIRED_TOP_LEVEL


def test_canonical_required_keys_are_unchanged_by_the_new_block():
    from live import telemetry as nt
    snap = _snapshot_with(build_decision_records(_decision_frame(1)))
    assert not [k for k in nt.REQUIRED_TOP_LEVEL if k not in snap]
    assert snap["schema_version"] == nt.SCHEMA_VERSION


def test_publishing_decisions_grants_no_authority():
    snap = _snapshot_with(build_decision_records(_decision_frame(3)))
    blob = json.dumps(snap)
    for banned in ("node_mt5", "live_mt5", "admitted", "admissionReasons",
                   "execution_authority", "provenance"):
        assert banned not in blob
    assert snap["runtime"]["open_eligibility"]["eligible"] is False


def test_runner_projects_decisions_without_publishing_the_frame():
    """The 211-column frame must never reach the payload."""
    src = (REPO / "live" / "runner.py").read_text(encoding="utf-8")
    assert "build_decision_records(trades_str" in src
    tsrc = (REPO / "live" / "telemetry.py").read_text(encoding="utf-8")
    assert "snapshot[\"decisions\"] = decisions" in tsrc


def test_decision_projection_failure_cannot_break_a_cycle():
    src = (REPO / "live" / "runner.py").read_text(encoding="utf-8")
    blk = src[src.index("from live.decisions import"):src.index('"status": "bootstrap"')]
    assert "except Exception" in blk, "projection must contain its own failure"
