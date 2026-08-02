"""M-EDGE-1 — edge/performance is honestly NOT computed.

`/api/edge-monitor` previously served the WORLD fixture's authored track record
(expectancy 0.39R, win rate 33.5%, edge drift "-0.02R vs research", policy
health "green", research/ghost-vs-live comparisons). No edge or performance
model exists, and the REAL plane deliberately refuses to compute these figures
(`operational_projection.LedgerOperationalSummary`,
`trade_ledger_domain.LedgerSummaryTotals`: "NO win rate, no expectancy, no
drawdown, no equity curve"). Publishing fixture versions of exactly those
metrics created a second, fictional authority — these tests keep it severed.
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
from fastapi.testclient import TestClient                           # noqa: E402

import fixture_preview_service
import server                                                       # noqa: E402

client = TestClient(server.app)

#: Every metric name the fixture published. None may reappear.
FORBIDDEN_FIELDS = ("expectancyR", "expectancy", "winRate", "edgeDrift",
                    "policyHealth", "researchVsLive", "ghostVsLive",
                    "signals", "score", "band", "health",
                    "distributionDrift", "featureDrift", "forwardTestHealth",
                    "operatorConfidence", "futureCandidates", "metrics")

#: The authored values an operator could have sized or promoted against.
FIXTURE_VALUES = ("0.39", "0.335", "-0.02R vs research", "green",
                  "+0.06R", "ghost +0.10R", "on-track", "high")


def _get() -> dict:
    r = client.get("/api/edge-monitor")
    assert r.status_code == 200
    return r.json()


def test_the_endpoint_reports_no_edge_performance_model():
    d = _get()
    assert d["schemaVersion"] == 1      # first VERSIONED form (was unversioned)
    assert d["computed"] is False
    assert d["reason"] == "no_edge_performance_model"
    assert "not computed" in d["detail"]
    assert "trade ledger" in d["detail"]        # points at the eventual real source


def test_no_fabricated_metric_field_is_returned():
    d = _get()
    for field in FORBIDDEN_FIELDS:
        assert field not in d, f"fabricated metric field {field} returned"


def test_no_authored_fixture_value_can_leak():
    text = str(_get())
    for value in FIXTURE_VALUES:
        assert value not in text, f"fixture edge value {value!r} leaked"


class _OverriddenWorld:
    """Wraps the real FixtureWorld (which is an object, not a dict) and swaps
    ONLY `edgeMonitor`. Every other key and attribute — including `.available`,
    which the fixture-surface middleware reads — passes straight through."""

    _MISSING = object()

    def __init__(self, real, payload):
        self._real, self._payload = real, payload

    def get(self, key, default=None):
        if key == "edgeMonitor":
            return default if self._payload is self._MISSING else self._payload
        return self._real.get(key, default)

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_poisoning_the_fixture_cannot_influence_the_response(monkeypatch):
    """If ANY fixture path survived, these absurd values would surface — which
    is precisely what the previous implementation did."""
    before = _get()
    poisoned = {"instrument": "EURUSD", "asOf": "2026-07-01T09:14:00Z",
                "metrics": {"expectancyR": 99.9, "winRate": 0.999,
                            "edgeDrift": "+42R vs research", "policyHealth": "perfect"}}
    fixture_preview_service.install_for_test(
        _OverriddenWorld(fixture_preview_service.get_world(), poisoned))
    after = _get()
    assert after == before
    text = str(after)
    for poison in ("99.9", "0.999", "+42R", "perfect"):
        assert poison not in text, "poisoned fixture leaked into the response"


def test_removing_the_fixture_key_entirely_changes_nothing(monkeypatch):
    before = _get()
    fixture_preview_service.install_for_test(_OverriddenWorld(
        fixture_preview_service.get_world(), _OverriddenWorld._MISSING))
    assert _get() == before


def test_route_source_never_reads_world_or_edge_monitor():
    """Narrow by construction: only this route's own source is scanned, so
    unrelated fixture code elsewhere in server.py cannot trip it."""
    src = inspect.getsource(server.edge_monitor)
    body = src.split('"""')[-1]                 # docstring may cite the history
    for forbidden in ("WORLD", "edgeMonitor"):
        assert forbidden not in body, \
            f"/api/edge-monitor route code references {forbidden} — fixture path regressed"
