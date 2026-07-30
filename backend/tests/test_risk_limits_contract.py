"""M-RISK-1 — the honest risk-limits contract.

`GET /api/risk/limits` must follow: real configuration, real telemetry, or
honestly unavailable — NEVER fixture-derived. These tests pin (a) the honest
response shape, (b) total immunity to fixture `fundedRules`, and (c) a
source-level guard forbidding WORLD/fixture access inside the route.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
import pytest                                                       # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

import server                                                       # noqa: E402

client = TestClient(server.app)


def _get(url: str) -> dict:
    r = client.get(url)
    assert r.status_code == 200
    return r.json()


# ── (a) honest contract shape ───────────────────────────────────────────────
def test_limits_response_is_honestly_unconfigured():
    d = _get("/api/risk/limits")
    assert d["schemaVersion"] == 1   # first VERSIONED form (old response was unversioned)
    assert d["configured"] is False
    assert d["source"] == "unconfigured"
    assert d["limits"] is None                       # no invented numbers, ever
    assert "fundedAccountRules" in d["unavailable"]
    assert "node telemetry" in d["detail"]           # points at the real source
    # No legacy numeric fields may leak back into the payload.
    for legacy in ("maxOpenTrades", "maxExposureLots", "maxDailyLoss",
                   "maxFloatingLoss", "minMarketConfidence"):
        assert legacy not in d, f"legacy fabricated field {legacy} returned"


def test_account_parameter_is_ignored_not_account_specific():
    base = _get("/api/risk/limits")
    fixture_acct = _get("/api/risk/limits?account=acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F")
    unknown_acct = _get("/api/risk/limits?account=acct_DOES_NOT_EXIST")
    assert base == fixture_acct == unknown_acct


# ── (b) fixture immunity: mutating fundedRules cannot alter the response ────
def test_fixture_funded_rules_cannot_influence_the_response(monkeypatch):
    before = _get("/api/risk/limits")
    # Poison every fixture account with absurd fundedRules. If ANY overlay path
    # survived, these values would surface (the old behaviour did exactly that).
    poisoned = []
    for acct in server.WORLD.get("accounts", []):
        poisoned.append(dict(acct, fundedRules={
            "maxLot": 99999.0, "dailyLossLimit": 123456.0, "maxDrawdown": 999999.0,
        }))
    # Patch through the public accessor the risk env uses for accounts.
    monkeypatch.setattr(server, "_account_by_id",
                        lambda aid: poisoned[0] if poisoned else None)
    after = _get("/api/risk/limits?account=acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F")
    assert after == before
    assert after["limits"] is None
    text = str(after)
    for poison in ("99999", "123456", "999999"):
        assert poison not in text, "fixture fundedRules leaked into the response"


# ── (c) source-level anti-regression guard ──────────────────────────────────
def test_route_source_never_touches_world_or_funded_rules():
    src = inspect.getsource(server.risk_limits)
    for forbidden in ("WORLD", "fundedRules", "_account_by_id",
                      "_risk_env", "_RISK_ENGINE.limits", "_limits_for"):
        # docstring may MENTION the history; strip it before scanning code.
        body = src.split('"""')[-1]
        assert forbidden not in body, \
            f"/api/risk/limits route code references {forbidden} — fixture path regressed"


def test_engine_advisory_defaults_are_not_exposed_as_limits():
    import risk_engine
    d = _get("/api/risk/limits")
    text = str(d)
    for key, value in risk_engine.DEFAULT_LIMITS.items():
        assert str(value) not in text, \
            f"engine advisory constant {key}={value} leaked into /risk/limits"
