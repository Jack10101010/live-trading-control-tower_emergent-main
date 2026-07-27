"""LIVE-1 — live broker telemetry + reconciliation integration (via the app)."""

from __future__ import annotations

import sys
from types import SimpleNamespace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _fake_mt5                                                    # noqa: E402
import broker as broker_layer                                       # noqa: E402
import broker_adapter as ba                                         # noqa: E402
import reconciliation as rc                                         # noqa: E402
import server                                                       # noqa: E402
from live.mt5_gateway import MT5Gateway                             # noqa: E402

client = TestClient(server.app)


class _Cfg:
    mt5_login = None; mt5_password = None; mt5_server = None; broker_symbol = "EURUSD.r"


def _use_connected_mt5(monkeypatch, account=True):
    fake = _fake_mt5.FakeMT5()
    if account:
        fake._account = _fake_mt5.make_account()
    gw = MT5Gateway(_Cfg(), sdk=fake); gw.connect()
    adapter = broker_layer.MT5Adapter()
    adapter._gateway_cache = gw; adapter._gateway_loaded = True
    adapter._policy_denied_reason = None
    monkeypatch.setattr(broker_layer, "active_kind", lambda: "mt5")
    monkeypatch.setattr(broker_layer, "get_broker", lambda kind=None: adapter)
    return adapter


# ── telemetry ─────────────────────────────────────────────────────────────────

def test_mock_broker_telemetry_is_labelled_mock():
    body = client.get("/api/execution/state").json()["broker"]
    assert body["kind"] == "mock" and body["provenance"] == "mock-fixture"
    assert body["liveWriteCapable"] is False and body["readOnly"] is True


def test_live_mt5_telemetry_is_populated_and_provenanced(monkeypatch):
    _use_connected_mt5(monkeypatch)
    body = client.get("/api/execution/state").json()["broker"]
    assert body["kind"] == "mt5" and body["provenance"] == "live_mt5"
    assert body["connection"] == "Connected"
    assert body["account"]["available"] is True
    assert body["account"]["login_masked"] == "mt5_****0001"
    assert body["account"]["equity"] == 10_000.0
    assert body["openPositions"] == 0 and body["openOrders"] == 0
    # LIVE-2 (deliberate evolution): MT5 now carries EXACTLY ONE write
    # capability (market-order submission); the broker block reports it and
    # readOnly derives from it truthfully.
    assert body["liveWriteCapable"] is True
    assert body["readOnly"] is False
    # No invented values: reads report explicit codes.
    assert body["reads"]["accountSnapshot"] == "ok"


def test_live_telemetry_reports_unavailable_reads_explicitly(monkeypatch):
    _use_connected_mt5(monkeypatch, account=False)
    body = client.get("/api/execution/state").json()["broker"]
    assert body["account"]["available"] is False
    assert body["account"]["code"] == "unavailable"    # explicit, not zeros
    assert "balance" not in body["account"]            # nothing invented


def test_live_broker_read_opens_no_execution_path(monkeypatch):
    _use_connected_mt5(monkeypatch)
    # Readiness stays false and a fixture execution command still denies (mt5
    # context is deny-by-default; no permissive context for a non-mock adapter).
    state = client.get("/api/execution/state").json()
    assert state["readiness"]["tradingReady"] is False


# ── reconciliation consumes the live read ─────────────────────────────────────

def test_reconciliation_consumes_live_broker_reads(monkeypatch):
    adapter = _use_connected_mt5(monkeypatch)
    snap = adapter.reconcile_snapshot(server._broker_context({}, "2026-07-27T00:00:00Z"))
    run = rc.run_reconciliation(
        tower_intents=[], broker_orders=snap.data["orders"],
        broker_positions=snap.data["positions"],
        broker_account_identity=snap.data["accountIdentity"],
        expected_account_identity=snap.data["accountIdentity"],
        broker_snapshot_at="2026-07-27T00:00:00Z", now="2026-07-27T00:00:30Z")
    assert run.clean and run.recon_id.startswith("recon_")   # ids/classes preserved


def test_account_mismatch_against_live_read_is_a_hard_failure(monkeypatch):
    adapter = _use_connected_mt5(monkeypatch)
    snap = adapter.reconcile_snapshot(server._broker_context({}, "2026-07-27T00:00:00Z"))
    run = rc.run_reconciliation(
        tower_intents=[], broker_orders=[], broker_positions=[],
        broker_account_identity=snap.data["accountIdentity"] or "acctfp_live",
        expected_account_identity="acctfp_DIFFERENT",
        broker_snapshot_at="2026-07-27T00:00:00Z", now="2026-07-27T00:00:30Z")
    # If the fake reports no fingerprint, force the mismatch shape explicitly.
    if snap.data["accountIdentity"] is None:
        run = rc.run_reconciliation(
            tower_intents=[], broker_orders=[], broker_positions=[],
            broker_account_identity="acctfp_live",
            expected_account_identity="acctfp_DIFFERENT",
            broker_snapshot_at="2026-07-27T00:00:00Z", now="2026-07-27T00:00:30Z")
    assert run.failed and any(d.cls == rc.ACCOUNT_IDENTITY_MISMATCH
                              for d in run.discrepancies)


def test_reconciliation_is_still_read_only():
    from conftest import code_only
    code = code_only("reconciliation.py")
    for forbidden in ("submit_command", "cancel_order(", "modify_order(",
                      "submit_order", "close_position("):
        assert forbidden not in code
