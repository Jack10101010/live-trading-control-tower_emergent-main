"""M-FLEET-1 — the fleet contract states its own provenance.

`/api/fleet` served deployment, broker and account records with no statement of
origin, so a client could not distinguish authored demonstration data from
operational truth: a fixture deployment tagged `executionMode: "live"` carrying a
$412 daily P/L was indistinguishable from a real one.

These tests pin (a) the provenance fields, (b) that the records are still served
UNCHANGED in development, (c) that counts are whatever the fixture actually
holds rather than a hard-coded total, and (d) that an absent fixture still
answers 501 rather than an empty 200.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import _fake_mt5                                                    # noqa: F401,E402
import pytest                                                       # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

import fixture_world                                                # noqa: E402
import fixture_preview_service
import server                                                       # noqa: E402

client = TestClient(server.app)
BACKEND = Path(__file__).resolve().parent.parent


def _fleet() -> dict:
    r = client.get("/api/fleet")
    assert r.status_code == 200
    return r.json()


# ── (a) provenance is stated in-band ────────────────────────────────────────
def test_fleet_declares_its_provenance():
    d = _fleet()
    assert d["schemaVersion"] == 1          # first VERSIONED form of this response
    assert d["provenance"] == "fixture"
    assert d["source"] == "development_fixture"
    # The detail must NAME the real alternative, not just say "not live".
    assert "/api/operations/accounts" in d["detail"]
    assert "/api/operations/nodes" in d["detail"]
    assert "not" in d["detail"].lower() and "live" in d["detail"].lower()


def test_provenance_is_never_silently_live():
    """The only value any current code path can produce is `fixture`. If a future
    change starts claiming `live`, it must do so deliberately — not by default."""
    src = (BACKEND / "server.py").read_text()
    assert 'FLEET_PROVENANCE_FIXTURE = "fixture"' in src
    body = src.split("async def fleet():")[1].split("@api_router")[0]
    code = body.split('\"\"\"')[-1]                    # drop the docstring
    assert '"live"' not in code, "the fleet route can claim live provenance"


# ── (b) the records themselves are unchanged ────────────────────────────────
def test_records_are_served_exactly_as_the_fixture_holds_them():
    d = _fleet()
    world = json.loads(fixture_preview_service.fixture_asset_path().read_text())   # M-WORLD-0
    assert [b["brokerId"] for b in d["brokers"]] == [b["brokerId"] for b in world["brokers"]]
    assert [a["accountId"] for a in d["accounts"]] == [a["accountId"] for a in world["accounts"]]
    assert ([x["deploymentId"] for x in d["deployments"]]
            == [x["deploymentId"] for x in world["deployments"]])


def test_counts_are_whatever_the_source_holds_not_a_hard_coded_total():
    """The UI derives its summary from these arrays. Nothing in the response may
    assert a count independently of the records, or the two could disagree."""
    d = _fleet()
    for key in ("deployments", "brokers", "accounts"):
        assert isinstance(d[key], list)
    assert "deploymentCount" not in d and "brokerCount" not in d and "accountCount" not in d
    world = json.loads(fixture_preview_service.fixture_asset_path().read_text())   # M-WORLD-0
    assert len(d["deployments"]) == len(world["deployments"])
    assert len(d["brokers"]) == len(world["brokers"])
    assert len(d["accounts"]) == len(world["accounts"])


# ── (c) absent source: 501, never an empty 200 ──────────────────────────────
class _NoWorld:
    """A world that is present-but-unavailable, matching the real absent-fixture
    object. `FixtureWorld` uses __slots__, so it cannot be monkeypatched in place."""
    available = False

    def get(self, key, default=None):
        return default


def test_absent_fixture_answers_501_not_an_empty_fleet(monkeypatch):
    fixture_preview_service.install_for_test(_NoWorld())
    r = client.get("/api/fleet")
    assert r.status_code == 501, "an empty 200 would read as 'no deployments'"
    body = r.json()
    assert body["code"] == fixture_world.CODE_FIXTURE_ABSENT
    # The FIX-2 middleware refuses the path before the route runs, so `surface`
    # is the request path; either identifies the same refused surface.
    assert body["surface"] in ("fleet", "/api/fleet")
    # No fabricated skeleton may accompany the refusal.
    for key in ("deployments", "brokers", "accounts"):
        assert key not in body


def test_no_fabricated_fallback_records_anywhere_in_the_route():
    """A silent fallback array is the failure mode this milestone exists to
    prevent: the route must never manufacture a deployment or account."""
    src = (BACKEND / "server.py").read_text()
    body = src.split("async def fleet():")[1].split("@api_router")[0]
    code = body.split('"""')[-1]                    # drop the docstring
    for forbidden in ("deploymentId", "accountId", "brokerId", "dailyPl", "balance"):
        assert forbidden not in code, \
            f"the fleet route constructs a {forbidden} literal — that is a fabricated record"
