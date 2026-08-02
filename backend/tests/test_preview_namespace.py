"""M-PREVIEW-DELETE-1 — no fixture-backed route wears an operational URL.

THE DEFECT
    Eighteen fixture-backed routes existed, and twelve of them sat on
    operational-looking paths: `/api/fleet`, `/api/trades`, `/api/packages`,
    `/api/recommendations`, `/api/deployments`, `/api/decisions/{id}`,
    `/api/policy/{i}/matrix`, `/api/portfolio/status`, `/api/risk/assessment`,
    `/api/runtime/active-package`, `/api/commands/{name}` and
    `/api/strategy/*`. Every one of them was gated and refused without the
    asset, so this was never an honesty defect — it was a standing invitation.
    An operational-looking URL is something a future consumer wires itself to.

WHAT THE AUDIT ACTUALLY FOUND
    Five of those twelve were not preview routes at all. `/api/commands/{name}`
    is the operator command dispatch, which appends immutable BotEvents;
    `/api/strategy/*`, `/api/runtime/active-package` and
    `/api/policy/{i}/matrix` are operational surfaces. They were classified
    fixture-backed by ONE leftover: `_active_package_runtime` defaulted to the
    fixture's promoted package when no runtime selection existed, and
    `_portfolio_env` handed the engine the fixture's accounts. Severing those
    two made five endpoints operational again rather than deleting them.

    Six more had no consumer at all and were deleted outright. Four were genuine
    previews and were renamed onto `/api/dev/*`.

    18 gated → 8, all under `/api/dev/`.
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fixture_preview_service as fps                                # noqa: E402
import fixture_surfaces                                              # noqa: E402
import server                                                        # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

client = TestClient(server.app)

#: Deleted outright: fixture-backed, operational-looking, zero consumers.
DELETED_ROUTES = (
    "/api/deployments",
    "/api/decisions/dec_anything",
    "/api/policy/EURUSD/matrix",
    "/api/portfolio/status",
    "/api/risk/assessment",
)

#: Renamed onto the explicit preview namespace.
RENAMED = {
    "/api/fleet": "/api/dev/fixture-fleet",
    "/api/trades": "/api/dev/fixture-trades",
    "/api/packages": "/api/dev/fixture-packages",
    "/api/recommendations": "/api/dev/fixture-recommendations",
}

#: Severed from the fixture and KEPT on their operational URLs, because that is
#: what they always were.
SEVERED_OPERATIONAL = (
    "/api/strategy/decisions",
    "/api/runtime/active-package",
)


def _child(script: str, tmp_path: Path, env_extra: dict | None = None):
    env = {**os.environ, "CONTROL_TOWER_STATE_DIR": str(tmp_path / "state")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          cwd=str(BACKEND_DIR), capture_output=True, text=True,
                          timeout=180, env=env)


def _mounted_paths() -> set:
    return {r.path for r in server.app.routes if hasattr(r, "path")}


# ══════════════════════════════════════════════════════════════════════════════
# 1–2  Every fixture route is explicitly named
# ══════════════════════════════════════════════════════════════════════════════

def test_1_no_fixture_backed_route_lacks_the_dev_namespace():
    """The whole milestone in one assertion, derived from the source."""
    gated = fixture_surfaces.gated_paths(
        fixture_surfaces.analyse(BACKEND_DIR / "server.py"))
    offenders = sorted(p for p in gated if "/api/dev/" not in p)
    assert offenders == [], offenders
    assert gated, "the analysis found no fixture routes at all — check it still works"


def test_1b_mounted_derived_and_openapi_inventories_agree():
    """THE RECONCILIATION GUARD.

    A route-count check found `RUNTIME-SOURCE-BOUNDARY.md` claiming "seven
    remain" while listing eight paths. That was not a typo: the eighth,
    `/api/dev/fixture-packages/active`, was MOUNTED but answering 404, because
    severing `_active_package_view()` for the five operational endpoints also
    broke the one preview route that legitimately needed the fixture's active
    package. The derived set said seven because a route that reaches no fixture
    is not fixture-backed — it was right, and the mounted table was the thing
    that had drifted.

    Three inventories therefore have to agree, and no count is hardcoded
    anywhere: the SET is the assertion, and any number in prose is derived from
    it. A hardcoded `== 8` would have to be edited by whoever breaks it, which
    is precisely the person least likely to notice.
    """
    mounted = {p for p in _mounted_paths() if "/api/dev/" in p}
    derived = fixture_surfaces.gated_paths(
        fixture_surfaces.analyse(BACKEND_DIR / "server.py"))
    schema_paths = {p for p in server.app.openapi().get("paths", {})
                    if "/dev/" in p}

    assert mounted == derived, {
        "mounted_not_derived": sorted(mounted - derived),
        "derived_not_mounted": sorted(derived - mounted)}
    assert mounted == schema_paths, {
        "mounted_not_in_openapi": sorted(mounted - schema_paths),
        "openapi_not_mounted": sorted(schema_paths - mounted)}


def test_1c_every_mounted_preview_route_actually_serves_the_fixture():
    """A mounted preview that 404s is worse than a deleted one: it advertises a
    capability in the route table and OpenAPI that does not exist.

    `/api/dev/fixture-packages/active` was exactly that for one commit.
    """
    fps.reset()
    try:
        broken = {}
        for path in sorted(p for p in _mounted_paths() if "/api/dev/" in p):
            response = client.get(path)
            if response.status_code != 200:
                broken[path] = response.status_code
        assert broken == {}, broken
    finally:
        fps.reset()


def test_2_every_fixture_route_names_itself_as_a_fixture():
    """`/api/dev/` alone is not enough: a reader scanning a route table should
    not have to know that `dev` implies authored records."""
    gated = fixture_surfaces.gated_paths(
        fixture_surfaces.analyse(BACKEND_DIR / "server.py"))
    for path in gated:
        assert "fixture" in path or "preview" in path, path


# ══════════════════════════════════════════════════════════════════════════════
# 3–5  Deleted routes stay deleted; renamed ones left no alias
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", DELETED_ROUTES)
def test_3_deleted_routes_are_gone_and_return_404(path):
    assert client.get(path).status_code == 404, path


@pytest.mark.parametrize("old,new", sorted(RENAMED.items()))
def test_4_renamed_routes_left_no_compatibility_alias(old, new):
    """No alias, no redirect. A compatibility shim would preserve exactly the
    operational-looking URL this milestone exists to retire."""
    response = client.get(old)
    assert response.status_code == 404, (old, response.status_code)
    assert old not in _mounted_paths()
    assert client.get(new).status_code == 200, new


def test_5_no_hidden_query_parameter_activates_fixture_mode():
    """A generic endpoint that switches between runtime and fixture records on a
    flag would reintroduce the problem invisibly."""
    for suspicious in ("?fixture=1", "?mode=fixture", "?preview=1", "?source=fixture"):
        body = str(client.get(f"/api/operations/accounts{suspicious}").json())
        assert "acct_01J8Z4K7M9P2R4T6V8X0Z2B4D6F" not in body, suspicious


# ══════════════════════════════════════════════════════════════════════════════
# 6–7  The severed operational routes are genuinely operational now
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("path", SEVERED_OPERATIONAL)
def test_6_severed_routes_keep_their_url_and_answer_honestly(path):
    """They were never previews. `/api/commands/{name}` appends immutable
    BotEvents; `/api/strategy/*` runs a real engine. One leftover fixture
    default made them look like fixture routes."""
    response = client.get(path)
    assert response.status_code == 200, (path, response.text)
    body = response.json()
    assert "100000" not in str(body), f"{path} still serves authored figures"


def test_7_the_command_surface_is_no_longer_fixture_classified():
    surfaces = fixture_surfaces.analyse(BACKEND_DIR / "server.py")
    for path in ("/api/commands/{name}", "/api/strategy/decisions",
                 "/api/strategy/evaluate", "/api/runtime/active-package",
                 "/api/policy/{instrument}/matrix"):
        assert path not in surfaces, f"{path} reaches the fixture again"


# ══════════════════════════════════════════════════════════════════════════════
# 8–11  Environment matrix
# ══════════════════════════════════════════════════════════════════════════════

def test_8_ordinary_requests_still_load_nothing(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        for p in ('/api/health', '/api/operations/summary', '/api/commands/Noop',
                  '/api/strategy/decisions', '/api/runtime/active-package'):
            c.get(p)
        print('LOADCOUNT=%d' % fps.load_count())
    """, tmp_path)
    assert "LOADCOUNT=0" in result.stdout, result.stdout + result.stderr


def test_9_previews_report_unavailable_when_the_asset_is_absent(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        codes = {p: c.get(p).status_code for p in
                 ('/api/dev/fixture-world', '/api/dev/fixture-fleet',
                  '/api/dev/fixture-packages', '/api/dev/fixture-trades',
                  '/api/dev/fixture-recommendations', '/api/dev/fixture-events')}
        print('CODES=%s' % sorted(set(codes.values())))
    """, tmp_path, env_extra={"FIXTURE_PREVIEW_ASSET": "/nonexistent/world.v1.json"})
    assert "CODES=[501]" in result.stdout, result.stdout + result.stderr


def test_10_production_refuses_every_preview_route(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        codes = {p: c.get(p).status_code for p in
                 ('/api/dev/fixture-world', '/api/dev/fixture-fleet',
                  '/api/dev/fixture-packages', '/api/dev/fixture-trades')}
        served = [p for p, code in codes.items() if code == 200]
        print('SERVED=%s' % served)
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5"})
    assert "SERVED=[]" in result.stdout, result.stdout + result.stderr


def test_11_deleted_routes_cannot_expose_fixture_data_in_production(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        codes = [c.get(p).status_code for p in
                 ('/api/deployments', '/api/portfolio/status', '/api/risk/assessment')]
        print('CODES=%s' % codes)
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5"})
    assert "CODES=[404, 404, 404]" in result.stdout, result.stdout + result.stderr


# ══════════════════════════════════════════════════════════════════════════════
# 12–15  Provenance, route table, determinism
# ══════════════════════════════════════════════════════════════════════════════

def test_12_every_preview_response_states_it_is_authored():
    """A preview that does not say so is indistinguishable from an operational
    response once it is pasted into a bug report."""
    fps.reset()
    try:
        for path in ("/api/dev/fixture-fleet", "/api/dev/fixture-events"):
            body = str(client.get(path).json()).lower()
            assert "fixture" in body, f"{path} carries no fixture provenance"
    finally:
        fps.reset()


def test_13_the_mounted_route_table_advertises_no_legacy_fixture_path():
    """OpenAPI is generated from this table, so a stale route would be
    advertised to anyone reading the schema."""
    mounted = _mounted_paths()
    for legacy in ("/api/fleet", "/api/trades", "/api/packages",
                   "/api/packages/active", "/api/recommendations",
                   "/api/deployments", "/api/deployments/{deployment_id}",
                   "/api/decisions/{decision_id}", "/api/policy/{instrument}/matrix",
                   "/api/portfolio/status", "/api/risk/assessment", "/api/world"):
        assert legacy not in mounted, legacy


def test_13b_the_openapi_schema_is_fixture_free_outside_the_dev_namespace():
    schema = server.app.openapi()
    fixture_paths = [p for p in schema.get("paths", {})
                     if "fixture" in p or "preview" in p]
    assert all("/dev/" in p for p in fixture_paths), fixture_paths


def test_14_repeated_preview_responses_are_deterministic():
    fps.reset()
    try:
        first = client.get("/api/dev/fixture-fleet").json()
        second = client.get("/api/dev/fixture-fleet").json()
        assert first == second
        assert fps.load_count() == 1, "one lazy load, cached thereafter"
    finally:
        fps.reset()


def test_15_a_preview_request_mutates_no_operational_store():
    """Preview and runtime must not share state in either direction."""
    fps.reset()
    try:
        before = client.get("/api/operations/summary").json()
        client.get("/api/dev/fixture-fleet")
        client.get("/api/dev/fixture-trades")
        after = client.get("/api/operations/summary").json()
        for snapshot in (before, after):
            # Clocks advance between two requests; that is time passing, not
            # state changing. Everything else must be identical.
            snapshot.pop("projectionTimestamp", None)
            snapshot.pop("freshness", None)
            for account in (snapshot.get("accounts") or []):
                account.pop("freshness", None)
            for node in (snapshot.get("nodes") or []):
                node.pop("freshness", None)
        assert before == after
    finally:
        fps.reset()
