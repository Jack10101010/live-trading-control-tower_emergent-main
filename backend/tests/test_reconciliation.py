"""LX-1 Slice 4 — pure reconciliation model tests (live/reconciliation.py).

Deterministic, no gateway/executor/state/broker. Drives normalization, matching,
volume classification and report serialization over plain input values.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

import pytest                                              # noqa: E402
from live import reconciliation as RC                      # noqa: E402
from live.reconciliation import ReconOutcome as O          # noqa: E402
import _fake_mt5 as F                                      # noqa: E402

MAGIC = 77001
SYM = "EURUSD"


def _pos(**kw):
    base = {"ticket": 100, "symbol": SYM, "magic": MAGIC, "volume": 0.01, "comment": "c"}
    base.update(kw)
    return base


# ── snapshot / position normalization ───────────────────────────────────────────

def test_valid_dict_entry_normalizes():
    bp = RC.normalize_position(_pos(ticket=555, volume=0.02, comment="tag"))
    assert bp is not None and bp.ticket == 555 and bp.symbol == SYM
    assert bp.magic == MAGIC and bp.volume == 0.02 and bp.comment == "tag"


def test_valid_sdk_like_object_normalizes():
    bp = RC.normalize_position(F.make_position(555, 0, 0.02, comment="tag", magic=MAGIC))
    assert bp is not None and bp.ticket == 555 and bp.volume == 0.02 and bp.comment == "tag"


def test_integer_valued_float_ticket_accepted():
    assert RC.normalize_position(_pos(ticket=555.0)).ticket == 555


@pytest.mark.parametrize("bad", [
    {"ticket": None}, {"ticket": 0}, {"ticket": -3}, {"ticket": 1.5}, {"ticket": 2.25},
    {"ticket": float("nan")}, {"ticket": float("inf")}, {"ticket": True}, {"ticket": "5"},
])
def test_bad_ticket_rejected(bad):
    assert RC.normalize_position(_pos(**bad)) is None


@pytest.mark.parametrize("bad", [{"symbol": ""}, {"symbol": "  "}, {"symbol": None}, {"symbol": 5}])
def test_bad_symbol_rejected(bad):
    assert RC.normalize_position(_pos(**bad)) is None


@pytest.mark.parametrize("bad", [{"magic": None}, {"magic": True}, {"magic": 1.5},
                                 {"magic": "77001"}, {"magic": float("nan")}])
def test_bad_magic_rejected(bad):
    assert RC.normalize_position(_pos(**bad)) is None


def test_magic_zero_is_valid_foreign():
    assert RC.normalize_position(_pos(magic=0)).magic == 0


@pytest.mark.parametrize("bad", [
    {"volume": None}, {"volume": 0.0}, {"volume": -0.01}, {"volume": float("nan")},
    {"volume": float("inf")}, {"volume": True}, {"volume": "0.01"},
])
def test_bad_volume_rejected(bad):
    assert RC.normalize_position(_pos(**bad)) is None


def test_missing_symbol_field_rejected():
    assert RC.normalize_position({"ticket": 1, "magic": MAGIC, "volume": 0.01}) is None


def test_missing_comment_defaults_empty():
    bp = RC.normalize_position({"ticket": 1, "symbol": SYM, "magic": MAGIC, "volume": 0.01})
    assert bp is not None and bp.comment == ""


def test_snapshot_valid_all_entries():
    status, positions = RC.normalize_snapshot({"positions": [_pos(ticket=1), _pos(ticket=2)]})
    assert status == "ok" and len(positions) == 2


def test_snapshot_empty_is_ok_not_unreadable():
    status, positions = RC.normalize_snapshot({"positions": []})
    assert status == "ok" and positions == []


@pytest.mark.parametrize("snap", [None, "boom", 5, {"positions": None}, {"positions": "x"}, {}])
def test_malformed_top_level_snapshot_unreadable(snap):
    status, positions = RC.normalize_snapshot(snap)
    assert status == "unreadable" and positions == []


def test_one_malformed_entry_poisons_snapshot():
    # A single malformed entry -> whole snapshot UNREADABLE (never a valid subset).
    status, positions = RC.normalize_snapshot({"positions": [_pos(ticket=1), _pos(ticket=0)]})
    assert status == "unreadable" and positions == []


def test_normalization_never_raises_on_junk():
    for junk in (None, 5, "x", [], {}, object()):
        assert RC.normalize_position(junk) is None


# ── matching ────────────────────────────────────────────────────────────────────

def _bp(**kw):
    return RC.normalize_position(_pos(**kw))


def test_match_requires_magic_symbol_and_comment():
    positions = [_bp(ticket=1, comment="TAG"), _bp(ticket=2, comment="OTHER")]
    assert [p.ticket for p in RC.match_candidates(positions, MAGIC, SYM, "TAG")] == [1]


def test_no_match_on_symbol_mismatch():
    positions = [_bp(ticket=1, symbol="GBPUSD", comment="TAG")]
    assert RC.match_candidates(positions, MAGIC, SYM, "TAG") == []


def test_no_match_on_magic_mismatch():
    positions = [_bp(ticket=1, magic=999, comment="TAG")]
    assert RC.match_candidates(positions, MAGIC, SYM, "TAG") == []


def test_no_match_magic_only_or_symbol_only():
    # same magic different symbol, and same symbol different magic — neither matches
    positions = [_bp(ticket=1, symbol="GBPUSD", comment="TAG"),
                 _bp(ticket=2, magic=999, comment="TAG")]
    assert RC.match_candidates(positions, MAGIC, SYM, "TAG") == []


def test_multiple_matches_returned_for_caller_to_freeze():
    positions = [_bp(ticket=1, comment="TAG"), _bp(ticket=2, comment="TAG")]
    assert len(RC.match_candidates(positions, MAGIC, SYM, "TAG")) == 2


# ── volume classification ───────────────────────────────────────────────────────

def test_classify_full_exact():
    assert RC.classify_fill(0.01, 0.01) == (O.MATCHED_FULL, 0.0)


def test_classify_full_clean_decimal_no_float_error():
    # 0.02 vs 0.02 must not trip a binary-float drift.
    assert RC.classify_fill(0.02, 0.02)[0] is O.MATCHED_FULL
    assert RC.classify_fill(0.07, 0.07)[0] is O.MATCHED_FULL


def test_classify_partial_below_expected():
    out, remaining = RC.classify_fill(0.007, 0.02)
    assert out is O.MATCHED_PARTIAL and remaining == pytest.approx(0.013) and remaining > 0


def test_classify_partial_remaining_never_negative():
    out, remaining = RC.classify_fill(0.01, 0.02)
    assert out is O.MATCHED_PARTIAL and remaining >= 0


def test_classify_overfill_is_ambiguous():
    assert RC.classify_fill(0.05, 0.02) == (O.AMBIGUOUS, None)


def test_classify_tiny_valid_volume():
    assert RC.classify_fill(0.001, 0.001)[0] is O.MATCHED_FULL


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf"), True])
def test_classify_malformed_expected_is_ambiguous(bad):
    assert RC.classify_fill(0.01, bad) == (O.AMBIGUOUS, None)


@pytest.mark.parametrize("bad", [None, 0.0, -1.0, float("nan"), float("inf"), True])
def test_classify_malformed_broker_is_unreadable(bad):
    assert RC.classify_fill(bad, 0.01) == (O.UNREADABLE, None)


# ── existing-mirror matched classification ──────────────────────────────────────

def test_matched_consistent_full_no_freeze():
    out, freeze, _ = RC.classify_matched(0.01, 0.01, "confirmed")
    assert out is O.MATCHED_FULL and freeze is False


def test_matched_consistent_partial_no_freeze():
    out, freeze, _ = RC.classify_matched(0.007, 0.007, "partial")
    assert out is O.MATCHED_PARTIAL and freeze is False


def test_matched_drift_below_freezes():
    out, freeze, reason = RC.classify_matched(0.005, 0.01, "confirmed")
    assert out is O.AMBIGUOUS and freeze is True and "drift" in reason


def test_matched_drift_above_freezes():
    out, freeze, _ = RC.classify_matched(0.05, 0.01, "confirmed")
    assert out is O.AMBIGUOUS and freeze is True


def test_matched_no_baseline_freezes_insufficient_evidence():
    # D-S4-A1: a mirrored position with no usable volume baseline must NOT be
    # declared a clean match — insufficient evidence -> AMBIGUOUS + freeze.
    out, freeze, reason = RC.classify_matched(0.01, None, None)
    assert out is O.AMBIGUOUS and freeze is True and "insufficient" in reason


@pytest.mark.parametrize("bad", [None, 0.0, -0.01, True, "0.01", float("nan"),
                                 float("inf"), float("-inf")])
def test_matched_malformed_baseline_freezes(bad):
    # D-S4-A1: any non-finite-positive recorded baseline -> AMBIGUOUS + freeze.
    out, freeze, _ = RC.classify_matched(0.01, bad, "confirmed")
    assert out is O.AMBIGUOUS and freeze is True


# ── D-S4-A2: comment normalization never raises ─────────────────────────────────

class _HostileComment:
    def __str__(self):
        raise RuntimeError("hostile __str__")


def test_hostile_comment_str_does_not_raise_position_unreadable():
    r = RC.normalize_position(_pos(comment=_HostileComment()))
    assert r is None                                    # unreadable, not an exception


def test_hostile_comment_poisons_snapshot_no_raise():
    status, positions = RC.normalize_snapshot({"positions": [_pos(ticket=1),
                                                             _pos(ticket=2, comment=_HostileComment())]})
    assert status == "unreadable" and positions == []


def test_bytes_comment_rejected_not_fuzzy_matched():
    # bytes are rejected as unreadable — never decoded into a string that could
    # accidentally equal a valid intent-identity comment.
    assert RC.normalize_position(_pos(comment=b"TAG")) is None


def test_none_comment_is_empty_not_a_valid_identity():
    bp = RC.normalize_position(_pos(comment=None))
    assert bp is not None and bp.comment == ""
    # an empty comment can never be a unique SENT match against a real tag
    assert RC.match_candidates([bp], MAGIC, SYM, "TAG") == []


def test_unicode_comment_preserved_stable():
    bp = RC.normalize_position(_pos(comment="münster–Ω–🚀"))
    assert bp is not None and bp.comment == "münster–Ω–🚀"


def test_excessively_long_comment_non_throwing_no_prefix_adoption():
    long = "x" * 10000
    bp = RC.normalize_position(_pos(comment=long))
    assert bp is not None and bp.comment == long
    # exact-equality matching: a long comment never prefix-matches a short tag
    assert RC.match_candidates([bp], MAGIC, SYM, "x") == []


# ── report serialization ────────────────────────────────────────────────────────

def _finding(outcome=O.MATCHED_FULL, **kw):
    return RC.ReconFinding(outcome=outcome, **kw)


def test_finding_to_dict_is_enum_free_and_serializable():
    d = _finding(O.MATCHED_PARTIAL, trade_id="T1", broker_ticket=5, remaining_volume=0.01).to_dict()
    assert d["outcome"] == "matched_partial" and not isinstance(d["outcome"], O)
    json.dumps(d)   # JSON-safe


def test_report_to_dict_backward_compatible_keys():
    r = RC.ReconciliationReport()
    r.add("warning", "missing_position", "trade T1 absent")
    r.record(_finding(O.MISSING, trade_id="T1"))
    d = r.to_dict()
    assert set(d) >= {"frozen", "findings"}          # pre-slice keys preserved
    assert set(d) >= {"outcomes", "snapshot_status", "counts"}   # additive
    assert d["frozen"] is False and d["findings"][0]["code"] == "missing_position"


def test_report_record_sets_frozen_from_finding():
    r = RC.ReconciliationReport()
    r.record(_finding(O.ORPHAN, broker_ticket=9, requires_freeze=True))
    assert r.frozen is True


def test_report_counts_and_deterministic_ordering():
    r = RC.ReconciliationReport()
    for t in (1, 2, 3):
        r.record(_finding(O.MATCHED_FULL, broker_ticket=t))
    r.record(_finding(O.MISSING, trade_id="Tx"))
    d = r.to_dict()
    assert d["counts"] == {"matched_full": 3, "missing": 1}
    assert [o["broker_ticket"] for o in d["outcomes"][:3]] == [1, 2, 3]   # insertion order


def test_report_no_raw_sdk_object_or_secret_retained():
    sdk_pos = F.make_position(5, 0, 0.01, comment="c", magic=MAGIC)
    bp = RC.normalize_position(sdk_pos)
    r = RC.ReconciliationReport()
    r.record(_finding(O.MATCHED_FULL, broker_ticket=bp.ticket, broker_volume=bp.volume))
    blob = json.dumps(r.to_dict())
    assert "SimpleNamespace" not in blob and "password" not in blob and "://" not in blob


def test_report_json_serializable_full_roundtrip():
    r = RC.ReconciliationReport()
    r.snapshot_status = "ok"
    r.record(_finding(O.MATCHED_FULL, trade_id="T1", broker_ticket=5, broker_volume=0.01))
    r.record(_finding(O.FOREIGN, broker_ticket=9, broker_magic=0))
    restored = json.loads(json.dumps(r.to_dict()))
    assert restored["outcomes"][1]["outcome"] == "foreign"
