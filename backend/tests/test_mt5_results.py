"""LX-1 Slice 3 — pure MT5 result-classifier tests (live/mt5_results.py).

Deterministic, no gateway/executor/broker. Drives the classifier over plain
evidence dicts (as the gateway would after extract_evidence) plus the exception
and not-submitted constructors. Uses the fake sdk's real-valued retcode
constants via build_retcode_map, so the mapping matches production semantics.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from live import mt5_results as R                        # noqa: E402
from live.mt5_results import MT5SubmitDisposition as Disp  # noqa: E402
import _fake_mt5 as F                                    # noqa: E402

RC = R.build_retcode_map(F.FakeMT5())     # real-valued retcode map


def _classify(result, req=0.02):
    return R.classify(R.extract_evidence(result), req, RC)


# ── full fill ─────────────────────────────────────────────────────────────────

def test_full_fill_filled_with_evidence():
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE, order=555, deal=777, price=1.101, volume=0.02))
    assert r.disposition is Disp.FILLED and r.freeze is False and r.retryable is False
    assert r.broker_order_ticket == 555 and r.broker_deal_ticket == 777 and r.price == 1.101
    assert r.filled_volume == 0.02 and r.remaining_volume == 0.0


def test_full_fill_requested_equals_filled():
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.02), req=0.02)
    assert r.disposition is Disp.FILLED and r.remaining_volume == 0.0


def test_done_missing_volume_is_ambiguous_not_filled(tmp_path=None):
    # D-S3-1: DONE with a ticket but NO filled volume is insufficient evidence.
    r = _classify(SimpleNamespace(retcode=F.TRADE_RETCODE_DONE, order=1), req=0.02)  # no .volume
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and r.retryable is False
    assert r.retcode == F.TRADE_RETCODE_DONE and r.filled_volume is None   # never assumed=requested


# ── partial fill ──────────────────────────────────────────────────────────────

def test_partial_fill_below_requested():
    import pytest
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=556, volume=0.007), req=0.02)
    assert r.disposition is Disp.PARTIALLY_FILLED and r.freeze is True and r.retryable is False
    assert r.filled_volume == 0.007 and r.remaining_volume == pytest.approx(0.013)


def test_partial_not_promoted_to_full():
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=1, volume=0.007), req=0.02)
    assert r.disposition is Disp.PARTIALLY_FILLED and r.disposition is not Disp.FILLED


def test_filled_greater_than_requested_anomaly_clamps_remaining():
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.05), req=0.02)
    assert r.disposition is Disp.FILLED and r.filled_volume == 0.05 and r.remaining_volume == 0.0


# ── rejects ───────────────────────────────────────────────────────────────────

import pytest as _pytest                                # noqa: E402


@_pytest.mark.parametrize("code_name", [
    "TRADE_RETCODE_REJECT", "TRADE_RETCODE_INVALID_VOLUME", "TRADE_RETCODE_INVALID_PRICE",
    "TRADE_RETCODE_INVALID_STOPS", "TRADE_RETCODE_MARKET_CLOSED", "TRADE_RETCODE_TRADE_DISABLED",
    "TRADE_RETCODE_NO_MONEY", "TRADE_RETCODE_INVALID_FILL", "TRADE_RETCODE_REQUOTE",
])
def test_deterministic_rejects(code_name):
    code = getattr(F, code_name)
    r = _classify(F.make_result(code))
    assert r.disposition is Disp.REJECTED and r.freeze is False and r.retryable is False
    assert r.retcode == code and r.broker_order_ticket in (0, None)   # no fabricated fill


# ── ambiguous ─────────────────────────────────────────────────────────────────

@_pytest.mark.parametrize("code_name", ["TRADE_RETCODE_TIMEOUT", "TRADE_RETCODE_CONNECTION",
                                        "TRADE_RETCODE_PLACED"])
def test_ambiguous_retcodes(code_name):
    r = _classify(F.make_result(getattr(F, code_name)))
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and r.retryable is False


def test_none_result_is_ambiguous():
    r = _classify(None)
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True


def test_unknown_retcode_is_ambiguous_and_frozen():
    r = _classify(F.make_result(99999))
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and "unknown" in r.diagnostic


def test_result_without_retcode_is_ambiguous():
    r = R.classify(R.extract_evidence(SimpleNamespace(order=1)), 0.02, RC)  # no retcode attr
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True


# ── exception / not-submitted ─────────────────────────────────────────────────

def test_exception_typed_frozen():
    r = R.from_exception(RuntimeError("dropped"), requested_volume=0.02)
    assert r.disposition is Disp.EXCEPTION and r.freeze is True and r.retryable is False
    assert "RuntimeError" in r.diagnostic and "dropped" in r.diagnostic
    assert r.broker_order_ticket is None   # no fabricated evidence


def test_not_submitted_distinct_and_not_frozen():
    r = R.not_submitted("normalization_rejected: volume_below_min (x)", requested_volume=0.02)
    assert r.disposition is Disp.NOT_SUBMITTED and r.freeze is False and r.submitted is False
    assert "volume_below_min" in r.diagnostic


# ── evidence integrity ────────────────────────────────────────────────────────

def test_extract_evidence_is_plain_scalars_no_object_retained():
    result = F.make_result(F.TRADE_RETCODE_DONE, order=5, deal=6, volume=0.02, price=1.1,
                           bid=1.0, ask=1.2, comment="c", request_id=9, retcode_external=3)
    ev = R.extract_evidence(result)
    assert isinstance(ev, dict) and set(ev) >= {"retcode", "order", "deal", "volume", "price"}
    r = R.classify(ev, 0.02, RC)
    assert r.raw_evidence == ev and r.raw_evidence is not ev  # copied, plain data


def test_result_is_immutable_and_serializable():
    r = _classify(F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.02))
    try:
        r.disposition = Disp.REJECTED       # frozen dataclass
        assert False, "MT5SubmitResult must be immutable"
    except Exception:
        pass
    import json
    json.dumps(r.to_ledger_detail())        # ledger detail is JSON-serializable


def test_no_secrets_in_ledger_detail():
    d = _classify(F.make_result(F.TRADE_RETCODE_DONE, order=1, volume=0.02)).to_ledger_detail()
    assert set(d) == {"disposition", "retcode", "order", "deal", "requested_volume",
                      "filled_volume", "remaining_volume", "price", "comment", "diagnostic"}


# ── D-S3-1: success retcode with insufficient evidence -> AMBIGUOUS + freeze ──

@_pytest.mark.parametrize("code", [F.TRADE_RETCODE_DONE, F.TRADE_RETCODE_DONE_PARTIAL])
@_pytest.mark.parametrize("vol", [None, 0.0, -1.0, float("nan"), float("inf")])
def test_success_with_bad_volume_is_ambiguous(code, vol):
    kw = {} if vol is None else {"volume": vol}
    r = _classify(SimpleNamespace(retcode=code, order=5, **kw), req=0.02)
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and r.retryable is False
    assert r.disposition not in (Disp.FILLED, Disp.PARTIALLY_FILLED)
    assert r.retcode == code and r.broker_order_ticket == 5   # present evidence preserved


@_pytest.mark.parametrize("code", [F.TRADE_RETCODE_DONE, F.TRADE_RETCODE_DONE_PARTIAL])
@_pytest.mark.parametrize("ticket", [None, 0, -3, 1.5, 2.25])
def test_success_with_bad_ticket_is_ambiguous(code, ticket):
    kw = {} if ticket is None else {"order": ticket}
    r = _classify(SimpleNamespace(retcode=code, volume=0.02, **kw), req=0.02)
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and r.retryable is False
    assert "insufficient evidence" in r.diagnostic


def test_success_with_ticket_and_volume_still_fills():
    assert _classify(F.make_result(F.TRADE_RETCODE_DONE, order=7, volume=0.02)).disposition is Disp.FILLED
    assert _classify(F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=7, volume=0.007)).disposition is Disp.PARTIALLY_FILLED


def test_usable_ticket_helper():
    assert R.usable_ticket(5) and R.usable_ticket(5.0) and R.usable_ticket(1.0)   # integer-valued
    assert not R.usable_ticket(1.5) and not R.usable_ticket(2.25)                 # D-S3-2: fractional rejected
    assert not R.usable_ticket(0) and not R.usable_ticket(-1) and not R.usable_ticket(None)
    assert not R.usable_ticket(float("nan")) and not R.usable_ticket(float("inf"))
    assert not R.usable_ticket(True) and not R.usable_ticket("5")


@_pytest.mark.parametrize("code", [F.TRADE_RETCODE_DONE, F.TRADE_RETCODE_DONE_PARTIAL])
def test_fractional_ticket_is_ambiguous(code):
    r = _classify(SimpleNamespace(retcode=code, order=1.5, volume=0.02), req=0.02)
    assert r.disposition is Disp.AMBIGUOUS and r.freeze is True and r.retryable is False
