"""M-FLEET-2 — the configured instrument contract.

Navigation needs instrument identity. It previously derived that from FIXTURE
deployment records, so the pair menu was a view over invented operational
entities. `/api/instruments` reports the configured universe instead.

The delicate part is what this endpoint must NOT imply. A configured symbol is
not a deployment, not a broker subscription, not a fresh feed and not a tradable
instrument. These tests pin the claim boundary as tightly as the values.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
from fastapi.testclient import TestClient                           # noqa: E402

import fixture_preview_service
import server                                                       # noqa: E402

client = TestClient(server.app)


def _instruments() -> dict:
    r = client.get("/api/instruments")
    assert r.status_code == 200
    return r.json()


def test_reports_the_actual_runtime_configuration():
    d = _instruments()
    assert d["schemaVersion"] == 1
    assert d["provenance"] == "configuration"
    assert d["source"] == "RUNTIME_SYMBOLS"
    assert [i["symbol"] for i in d["instruments"]] == list(server.RUNTIME_SYMBOLS)


def test_is_not_derived_from_the_fixture_world(monkeypatch):
    """The whole point: an absent fixture must not change the instrument list.
    If it did, navigation would still be fixture-derived."""
    before = _instruments()

    class _NoWorld:
        available = False

        def get(self, key, default=None):
            return default

    fixture_preview_service.install_for_test(_NoWorld())
    assert _instruments() == before


def test_claims_nothing_operational():
    """A configured symbol asserts no deployment, subscription, freshness,
    eligibility, account or connection. The payload must carry no such field."""
    d = _instruments()
    forbidden = ("deployment", "account", "broker", "balance", "equity", "pnl",
                 "lane", "status", "executionMode", "freshness", "stale",
                 "connected", "subscribed", "tradable", "eligible")
    blob = str(d).lower()
    for key in forbidden:
        # The detail sentence names these to DISCLAIM them; only field keys count.
        for entry in d["instruments"]:
            assert key.lower() not in {k.lower() for k in entry}, \
                f"instrument entry carries an operational field: {key}"
    assert set().union(*[set(e) for e in d["instruments"]]) == {"symbol"}
    # The detail must explicitly disclaim the operational readings.
    detail = d["detail"].lower()
    for disclaimed in ("deployment", "broker subscription", "market-data",
                       "trade", "account", "connection"):
        assert disclaimed in detail, f"detail does not disclaim {disclaimed}"


def test_configuration_provenance_is_never_operational():
    """`configuration` must not be confusable with the operational provenance
    vocabulary the frontend gate uses (`live_mt5` / `mock-fixture`)."""
    import operational_projection as proj
    d = _instruments()
    assert d["provenance"] not in (proj.PROV_LIVE_MT5, proj.PROV_MOCK_FIXTURE,
                                   proj.PROV_ABSENT)


def test_endpoint_creates_no_records_of_any_other_kind():
    d = _instruments()
    for key in ("deployments", "accounts", "brokers", "nodes", "positions", "orders"):
        assert key not in d, f"/api/instruments manufactured {key}"
