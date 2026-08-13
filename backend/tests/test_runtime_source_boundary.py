"""M-WORLD-0 — the runtime backend package contains no authored fixture data.

WHAT MOVED, AND WHY IT IS AN ARCHITECTURAL CHANGE RATHER THAN A TIDY-UP

    `backend/fixtures/world.v1.json` sat INSIDE the runtime package. Every
    packaging mechanism that copies `backend/` recursively — a Dockerfile, an
    rsync, a wheel with `package_data` — would have carried 22 collections of
    authored accounts, brokers, trades, packages, decisions and an operator
    identity into a production artefact. Not loading it in production is a
    RUNTIME property enforced by `environment`; not shipping it is a STRUCTURAL
    property, and only the second survives someone adding a new loader.

    The byte-identical copy already at `_fixtures/world.v1.json` — the repository's
    established design-asset directory, holding the fixture alongside the
    `brief.md` and `contracts.md` it was authored with — is now the only copy.

WHAT DELIBERATELY REMAINS RUNTIME SOURCE
    `mock_broker_data.py` — a TEST DOUBLE, stamped `mock-fixture`, inadmissible
    to every authority gate, and required for the mock adapter to work with no
    dev assets present at all. It is code, not authored demonstration data, and
    the distinction is the whole reason M-MOCK-DECOUPLE-1 came first.
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
import server                                                        # noqa: E402
from fastapi.testclient import TestClient                            # noqa: E402

client = TestClient(server.app)

#: The exact tree a production artefact would carry. Everything under it must be
#: code or configuration — never authored demonstration records.
RUNTIME_SOURCE_TREE = BACKEND_DIR

#: The one place authored fixture data is allowed to live.
DEV_ASSET_DIR = REPO_ROOT / "_fixtures"


def _child(script: str, tmp_path: Path, env_extra: dict | None = None):
    env = {**os.environ, "CONTROL_TOWER_STATE_DIR": str(tmp_path / "state")}
    env.update(env_extra or {})
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)],
                          cwd=str(BACKEND_DIR), capture_output=True, text=True,
                          timeout=180, env=env)


# ══════════════════════════════════════════════════════════════════════════════
# The move, and the absence of any way back
# ══════════════════════════════════════════════════════════════════════════════

def test_the_old_backend_fixture_path_does_not_exist():
    assert not (BACKEND_DIR / "fixtures" / "world.v1.json").exists()
    assert not (BACKEND_DIR / "fixtures").exists(), (
        "an empty `backend/fixtures/` invites the file back")


def test_exactly_one_authored_fixture_copy_exists_in_the_repository():
    """Three copies were once required to stay byte-identical by hand (see
    ENGINEERING-HANDOFF). Copies drift; one copy cannot."""
    copies = [p for p in REPO_ROOT.rglob("world.v1.json")
              if "node_modules" not in str(p) and ".git" not in str(p)]
    assert copies == [DEV_ASSET_DIR / "world.v1.json"], [str(p) for p in copies]


def test_the_runtime_source_tree_holds_no_authored_fixture_data():
    """A machine-checkable inventory, not a hand-maintained list."""
    offenders = []
    for path in RUNTIME_SOURCE_TREE.rglob("*"):
        if not path.is_file() or "__pycache__" in str(path):
            continue
        if path.suffix in (".db", ".pyc"):
            continue
        if path.suffix == ".json":
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert offenders == [], (
        "authored data files inside the runtime package: " + ", ".join(offenders))


def test_no_source_refers_to_the_old_backend_fixture_path():
    """No fallback, no 'try the old place too'. A search list is how a temporary
    fallback becomes permanent — nobody can tell which entry answered."""
    import io
    import tokenize

    def code_only(text: str) -> str:
        """Comments and docstrings stripped. A guard that scans its own prose
        fires on the very explanation of what it is guarding — this one did."""
        out = []
        try:
            for tok in tokenize.generate_tokens(io.StringIO(text).readline):
                if tok.type in (tokenize.COMMENT, tokenize.STRING):
                    continue
                out.append(tok.string)
        except (tokenize.TokenError, IndentationError):
            return text
        return " ".join(out)

    offenders = []
    for path in list(BACKEND_DIR.rglob("*.py")):
        if "__pycache__" in str(path):
            continue
        # This guard must NAME the old path in order to look for it.
        if path.name == "test_runtime_source_boundary.py":
            continue
        for line in code_only(path.read_text(errors="ignore")).splitlines():
            stripped = line.strip()
            if stripped.startswith("#") or stripped.startswith("*"):
                continue                      # prose recording the move is fine
            # Match the OLD path specifically. `_fixtures/world.v1.json` is the
            # new dev asset and must not trip a guard about the old one — the
            # first draft of this check matched both and fired on the fix.
            if ("backend/fixtures/world" in line
                    or 'BACKEND_DIR / "fixtures"' in line
                    or 'BACKEND / "fixtures"' in line):
                offenders.append(f"{path.name}: {stripped[:70]}")
    assert offenders == [], offenders


def test_the_resolver_never_points_inside_the_runtime_package():
    resolved = fps.fixture_asset_path().resolve()
    assert not str(resolved).startswith(str(BACKEND_DIR.resolve())), resolved
    assert resolved == (DEV_ASSET_DIR / "world.v1.json").resolve()


def test_there_is_exactly_one_fixture_path_variable():
    """A family of competing variables is how two of them end up disagreeing."""
    assert fps.VAR_FIXTURE_ASSET == "FIXTURE_PREVIEW_ASSET"
    source = (BACKEND_DIR / "fixture_preview_service.py").read_text()
    tree = ast.parse(source)
    env_reads = set()
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "get"
                and getattr(getattr(node.func, "value", None), "attr", None) == "environ"
                and node.args and isinstance(node.args[0], ast.Name)):
            env_reads.add(node.args[0].id)
    assert env_reads <= {"VAR_FIXTURE_ASSET"}, sorted(env_reads)


# ══════════════════════════════════════════════════════════════════════════════
# Packaging: structural exclusion, not "production never calls it"
# ══════════════════════════════════════════════════════════════════════════════

def test_the_dev_asset_directory_is_outside_every_runtime_package():
    """This repository has no Dockerfile, wheel or CI artefact definition, so
    there is no packaging manifest to amend. The boundary is therefore defined
    STRUCTURALLY: a production artefact is `backend/` (the Control Tower) or
    `live/` (the VPS node), and the dev asset is in neither.

    Stated precisely rather than inventing a deployment system to exclude from.
    """
    for package in (BACKEND_DIR, REPO_ROOT / "live"):
        assert not str(DEV_ASSET_DIR.resolve()).startswith(str(package.resolve()))
    assert DEV_ASSET_DIR.name.startswith("_"), (
        "the underscore marks it as non-package by convention")


def test_copying_the_runtime_package_carries_no_authored_fixture(tmp_path):
    """The property a packaging step would actually exercise: copy `backend/`
    the way any naive artefact build does, and check what came along."""
    import shutil
    staged = tmp_path / "artifact"
    shutil.copytree(BACKEND_DIR, staged,
                    ignore=shutil.ignore_patterns("__pycache__", "*.db", "tests"))
    carried = [str(p.relative_to(staged)) for p in staged.rglob("world.v1.json")]
    assert carried == [], carried
    assert (staged / "mock_broker_data.py").exists(), (
        "the test double IS runtime source and must travel with the package")


# ══════════════════════════════════════════════════════════════════════════════
# The runtime works with no dev asset at all
# ══════════════════════════════════════════════════════════════════════════════

def test_the_whole_ordinary_surface_works_with_the_asset_absent(tmp_path):
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps, broker_adapter
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        # ORDINARY routes. M-PREVIEW-DELETE-1 moved `/api/strategy/decisions`
        # INTO this list: severing the portfolio env and the active-package
        # fixture fallback made it a genuine operational route with an honest
        # unavailable contract. The gated example is now a dev preview.
        ordinary = ['/api/health','/api/live-runtime','/api/live/status',
                    '/api/live/connection','/api/operations/nodes',
                    '/api/operations/accounts','/api/operations/positions',
                    '/api/operations/orders','/api/operations/summary',
                    '/api/feature-flags','/api/instruments','/api/broker-health',
                    '/api/operator/identity','/api/integration/diagnostics',
                    '/api/strategy/decisions']
        bad = [(p, c.get(p).status_code) for p in ordinary
               if c.get(p).status_code >= 500]
        gated = c.get('/api/dev/fixture-world')
        broker_adapter.get_adapter('mock')
        print('BAD=%s GATED=%d GATEDCODE=%s LOADCOUNT=%d'
              % (bad, gated.status_code, gated.json().get('code'), fps.load_count()))
    """, tmp_path, env_extra={"FIXTURE_PREVIEW_ASSET": "/nonexistent/world.v1.json"})
    assert "BAD=[]" in result.stdout, result.stdout + result.stderr
    # M-PREVIEW-DELETE-1: `/api/strategy/decisions` is no longer fixture-backed
    # at all — it is an operational route with an honest unavailable contract,
    # so it belongs in the ordinary list above. The gated example is now an
    # explicit dev preview, which refuses when the asset is absent.
    assert "GATED=501" in result.stdout, result.stdout
    assert "GATEDCODE=fixture_world_unavailable" in result.stdout, result.stdout
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_the_override_is_honoured_with_no_fallback_to_the_repository_copy(tmp_path):
    """An override that silently fell back would make the missing-asset matrix
    untestable — and would be the old search list in a new costume."""
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import fixture_preview_service as fps
        print('RESOLVED=%s' % fps.fixture_asset_path())
        try:
            fps.get_world()
            print('LOADED_ANYWAY')
        except fps.FixturePreviewUnavailable as exc:
            print('REFUSED=%s' % exc.code)
    """, tmp_path, env_extra={"FIXTURE_PREVIEW_ASSET": "/nonexistent/world.v1.json"})
    assert "RESOLVED=/nonexistent/world.v1.json" in result.stdout, result.stdout
    assert "REFUSED=" in result.stdout, result.stdout + result.stderr
    assert "LOADED_ANYWAY" not in result.stdout


def test_an_explicit_override_loads_only_that_asset(tmp_path):
    import json
    alt = tmp_path / "alt.json"
    alt.write_text(json.dumps({"accounts": [{"accountId": "OVERRIDE_ONLY"}]}))
    result = _child(f"""
        import sys; sys.path.insert(0, '.')
        import fixture_preview_service as fps
        w = fps.get_world()
        print('COLLECTIONS=%d FIRST=%s'
              % (len(list(w.keys())), w.get('accounts')[0]['accountId']))
    """, tmp_path, env_extra={"FIXTURE_PREVIEW_ASSET": str(alt)})
    assert "COLLECTIONS=1" in result.stdout, result.stdout + result.stderr
    assert "FIRST=OVERRIDE_ONLY" in result.stdout, result.stdout


# ══════════════════════════════════════════════════════════════════════════════
# Previews still work, from the new location
# ══════════════════════════════════════════════════════════════════════════════

def test_preview_routes_still_serve_the_relocated_asset():
    fps.reset()
    try:
        response = client.get("/api/dev/fixture-world")
        assert response.status_code == 200
        assert len(response.json()) == 22, "all 22 authored collections"
        assert fps.load_count() == 1, "exactly one lazy load"
    finally:
        fps.reset()


def test_a_corrupt_dev_asset_breaks_only_the_preview(tmp_path):
    broken = tmp_path / "world.v1.json"
    broken.write_text("{ not json ]")
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import server, fixture_preview_service as fps
        from fastapi.testclient import TestClient
        c = TestClient(server.app)
        ordinary = c.get('/api/health').status_code
        preview = c.get('/api/dev/fixture-world').status_code
        print('ORDINARY=%d PREVIEW=%d CACHED=%s'
              % (ordinary, preview, fps.is_loaded()))
    """, tmp_path, env_extra={"FIXTURE_PREVIEW_ASSET": str(broken)})
    assert "ORDINARY=200" in result.stdout, result.stdout + result.stderr
    assert "PREVIEW=501" in result.stdout, result.stdout
    # A failed load must not be cached: fixing the file and retrying has to work
    # without restarting the process.
    assert "CACHED=False" in result.stdout, result.stdout


def test_production_still_refuses_the_asset_even_when_it_is_present(tmp_path):
    """Relocation changes WHERE the file is, not WHETHER production may read it.
    Physical presence must remain irrelevant."""
    result = _child("""
        import sys; sys.path.insert(0, '.')
        import fixture_preview_service as fps
        print('EXISTS=%s' % fps.fixture_asset_path().exists())
        try:
            fps.get_world()
            print('LOADED_IN_PRODUCTION')
        except fps.FixturePreviewUnavailable as exc:
            print('REFUSED=%s LOADCOUNT=%d' % (exc.code, fps.load_count()))
    """, tmp_path, env_extra={
        "CONTROL_TOWER_ENVIRONMENT": "production",
        "CONTROL_TOWER_BROKER_ADAPTER": "mt5",
        "MARKET_DATA_PROVIDER": "mt5"})
    assert "EXISTS=True" in result.stdout, result.stdout + result.stderr
    assert f"REFUSED={fps.CODE_NOT_PERMITTED}" in result.stdout, result.stdout
    assert "LOADCOUNT=0" in result.stdout, result.stdout


def test_concurrent_preview_requests_produce_one_coherent_load():
    """Two first-requests must not each start a load and publish rival worlds."""
    import threading
    fps.reset()
    try:
        results: list = []
        barrier = threading.Barrier(8)

        def hit():
            barrier.wait()
            results.append(fps.get_world())

        threads = [threading.Thread(target=hit) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert len(results) == 8
        assert all(w is results[0] for w in results), "rival world instances"
        assert fps.load_count() == 1, fps.load_count()
    finally:
        fps.reset()
