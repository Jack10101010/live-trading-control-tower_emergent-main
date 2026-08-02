"""M-WORLD-ISOLATE-1 — the ONE way to reach the development fixture world.

WHAT WAS WRONG WITH THE OLD SHAPE

    `server.py` executed at module scope:

        WORLD = fixture_world.load(FIXTURE_SEARCH_PATHS)

    Importing the backend therefore opened and parsed `world.v1.json`,
    allocated 22 collections of authored records, and bound them to a
    module-global that ANY module could reach with `import server;
    server.WORLD`. Ordinary operational endpoints, ordinary tests, and any
    future call site got fixture data by accident rather than by asking.

    HARDEN-1 made that load survivable when the file is missing, and M-ENV-1
    made it impossible in production. Neither made it EXPLICIT. A development
    process still paid for it on every start, and — more importantly — the only
    thing standing between an ordinary route and authored records was a
    convention that nobody had written down.

THE RULE THIS MODULE ENFORCES

    Fixture data is reached by CALLING FOR IT, never by importing something.

    There is no module-global world object here. `get_world()` is a function, it
    is lazy, it refuses in production, and every caller of it is greppable. If a
    future ordinary endpoint wants fixture records it has to type the name of
    this module, which is exactly the moment a reviewer should stop it.

WHY NOT A LAZY PROXY ON `server.WORLD`

    Because a lazy proxy preserves the accident. `server.WORLD.get("accounts")`
    would keep working, keep reading like ordinary code, and keep loading the
    fixture on first touch from anywhere. The point is not to defer the cost —
    it is to make the dependency VISIBLE. The global is gone.

DEFENSIVE COPIES

    `collection()` returns a deep copy. Preview handlers, and the tests that
    exercise them, have historically mutated the records they were handed; with
    one shared global that mutation leaked into every later reader and made
    test order significant. A copy per call costs microseconds on a 30 KiB
    fixture and removes an entire class of cross-test coupling.
"""
from __future__ import annotations

import copy
import os
import logging
import threading
from pathlib import Path
from typing import Any

import fixture_world

logger = logging.getLogger(__name__)

#: Stable codes a caller can branch on without parsing prose.
CODE_NOT_PERMITTED = "fixture_preview_not_permitted"
CODE_UNAVAILABLE = fixture_world.CODE_FIXTURE_ABSENT


class FixturePreviewUnavailable(RuntimeError):
    """The fixture world cannot be provided, with a machine-readable reason.

    Raised rather than returning an empty world, because every caller of this
    module is an EXPLICIT preview surface: it asked for authored records by
    name. Handing it a silently-empty world would let a preview page render as
    though the fixture were present and empty, which is the exact confusion
    `FixtureWorld.available` exists to prevent.
    """

    def __init__(self, code: str, detail: str):
        super().__init__(detail)
        self.code = code
        self.detail = detail


# Guarded by `_LOCK` so concurrent first requests cannot each start a load and
# race to publish a different instance. The load itself is idempotent, but two
# instances would defeat the defensive-copy invariant for anything holding a
# reference across requests.
_LOCK = threading.Lock()
_WORLD: fixture_world.FixtureWorld | None = None
_SEARCH_PATHS: tuple = ()
_LOAD_COUNT = 0


# ── M-WORLD-0: the ONE place that knows where the authored fixture lives ──────
#
# `server.FIXTURE_SEARCH_PATHS` held three candidates — the backend package copy
# plus two hardcoded `/app/...` container paths that exist on no machine in this
# repository. A search list is how a "temporary" old-path fallback becomes
# permanent: nobody can tell which entry actually answered, so no entry can
# safely be removed.
#
# There is now ONE default and ONE override. No search, no fallback, and
# deliberately no path inside `backend/` — the runtime package must not be able
# to satisfy a fixture read even by accident.

#: Override for tests and local preview tooling. One variable, not a family.
VAR_FIXTURE_ASSET = "FIXTURE_PREVIEW_ASSET"

#: The repository-relative default: a DEV ASSET directory, outside every runtime
#: package, alongside the design documents the fixture was authored with
#: (`brief.md`, `contracts.md`). Derived from this module's own location so it
#: does not depend on the working directory a process happened to start in.
_REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FIXTURE_ASSET = _REPO_ROOT / "_fixtures" / "world.v1.json"


def fixture_asset_path() -> Path:
    """Where the authored fixture world lives. Performs NO I/O.

    Resolving a path is not reading a file, and this function is safe to call
    from the request boundary that runs on every `/api` call.
    """
    override = (os.environ.get(VAR_FIXTURE_ASSET) or "").strip()
    return Path(override) if override else DEFAULT_FIXTURE_ASSET


def configure(search_paths=None) -> None:
    """Record where the fixture may live. Performs NO I/O.

    Retained so `server.py` and the existing preview tests can pin a path
    explicitly. With no argument it resolves the canonical dev asset.
    """
    global _SEARCH_PATHS
    _SEARCH_PATHS = (tuple(search_paths) if search_paths is not None
                     else (fixture_asset_path(),))


def search_paths() -> tuple:
    """The paths this service will search. The single source of truth.

    `server._fixture_file_present()` used its own module constant, so a caller
    that reconfigured the service left the availability probe and the loader
    disagreeing about where the fixture lives — the probe would report present
    while the loader searched somewhere else.
    """
    return _SEARCH_PATHS or (fixture_asset_path(),)


def reset() -> None:
    """Drop the cached world. The deterministic seam tests need.

    Without this, a test that poisons the fixture leaves the poison in place for
    every later test in the process, and a test that loads it leaves a
    fixture-shaped world behind for tests asserting absence.
    """
    global _WORLD, _LOAD_COUNT
    with _LOCK:
        _WORLD = None
        _LOAD_COUNT = 0


def install_for_test(world: fixture_world.FixtureWorld) -> None:
    """Publish an explicitly-constructed world, bypassing the loader.

    The supported way for a test to supply fixture data: build a `FixtureWorld`
    with exactly the records the test needs and hand it over. This replaces
    `monkeypatch.setattr(server, "WORLD", ...)`, which mutated a global that
    other tests read, and which no longer exists.
    """
    global _WORLD
    with _LOCK:
        _WORLD = world


def load_count() -> int:
    """How many times the file has actually been read this process.

    Exists so a guard can assert ZERO after an ordinary startup — a property no
    amount of reading the source can establish, because the interesting failure
    is an import side effect somewhere else entirely.
    """
    return _LOAD_COUNT


def is_loaded() -> bool:
    return _WORLD is not None


def peek() -> fixture_world.FixtureWorld | None:
    """The cached world, or None. NEVER triggers a load.

    For the request boundary, which runs on every `/api` call and must not be
    able to pull the fixture into memory as a side effect of answering a health
    check.
    """
    return _WORLD


def optional_world() -> fixture_world.FixtureWorld:
    """The fixture world for an INCIDENTAL reader, degrading instead of raising.

    A handful of surfaces read the fixture for metadata only — `/api/health`
    reports `meta.asOf`, `/api/market-data/candles` uses it as a seam marker —
    and their payload comes from a production source. Those must keep answering
    when the fixture is absent, because refusing them would hide genuine data;
    that is the over-refusal half of the FIX-2 lesson.

    Distinct from `get_world()` on purpose. An explicit preview asked for
    authored records and must be told when it cannot have them; an incidental
    reader asked for an optional garnish and must not fail over its absence.

    IT DOES NOT LOAD. An earlier draft delegated to `get_world()`, and a guard
    immediately caught the consequence: `GET /api/health` reads the fixture's
    `meta.asOf`, so the first health check pulled all 22 collections into memory
    and undid the isolation. An incidental reader by definition has no
    production source for the field it is garnishing, so the honest answer when
    nothing is loaded is that the field is absent — not to spend a file read
    discovering an authored timestamp.

    Consequence, stated plainly: `/api/health` now reports `fixture.asOf: null`
    until something explicitly loads a preview. That is more truthful than
    before, where an ordinary health endpoint reported a fixture file's authored
    timestamp as though it were system metadata.
    """
    cached = _WORLD
    return cached if cached is not None else fixture_world.unavailable_world()


def get_world() -> fixture_world.FixtureWorld:
    """The fixture world, loading it on first request.

    Raises `FixturePreviewUnavailable` when the environment forbids fixture
    activation (production), or when the fixture file is absent or unreadable.
    Never returns a partially-loaded or silently-empty world.
    """
    global _WORLD, _LOAD_COUNT
    cached = _WORLD
    if cached is not None:
        return cached
    with _LOCK:
        if _WORLD is not None:                  # another thread won the race
            return _WORLD
        # PRODUCTION REFUSAL FIRST, before any path is even examined. The
        # environment boundary owns this decision; `fixture_world.load` also
        # enforces it, and both are kept because a future edit that reorders
        # this function must not be able to open a file in production.
        import environment
        try:
            environment.require_fixture_activation_allowed()
        except Exception as exc:
            raise FixturePreviewUnavailable(
                CODE_NOT_PERMITTED,
                "fixture previews are not available in this environment: "
                f"{type(exc).__name__}") from exc

        # `search_paths()`, not the raw tuple: a process that never called
        # `configure()` must still resolve the canonical dev asset rather
        # than searching an empty list and reporting 'Searched: (none)'.
        paths = search_paths()
        world = fixture_world.load(paths)
        _LOAD_COUNT += 1
        if not world.available:
            # A missing or corrupt fixture is LOCAL to the preview: it must not
            # be cached, so fixing the file and retrying works without a
            # restart, and a later test asserting the loaded case is not poisoned
            # by an earlier one asserting absence.
            # File NAMES only. An absolute path discloses host layout, and
            # this repository already holds that line (`status()` reports
            # `path.name`). Widening it for debugging convenience would
            # trade a redaction property for a stack trace.
            searched = ", ".join(Path(p).name for p in paths) or "(none)"
            raise FixturePreviewUnavailable(
                CODE_UNAVAILABLE,
                "the development fixture world could not be read. Searched: "
                f"{searched}. This affects development previews only; no "
                "operational surface depends on it.")
        _WORLD = world
        logger.info("fixture world loaded for an explicit development preview "
                    "(%d collections)", len(list(world.keys())))
        return world


def collection(key: str, default: Any = None) -> Any:
    """One fixture collection, DEEP-COPIED.

    Callers get their own copy, so a preview handler cannot mutate the shared
    world and no request can influence a later one.
    """
    value = get_world().get(key, default)
    return copy.deepcopy(value)


def status() -> dict:
    """Redaction-safe description for diagnostics. Does NOT trigger a load.

    A status endpoint that loaded the fixture in order to report on it would
    defeat the entire milestone — the first health check would undo the
    isolation.
    """
    world = _WORLD
    return {
        "loaded": world is not None,
        "loadCount": _LOAD_COUNT,
        "searchPathNames": [Path(p).name for p in _SEARCH_PATHS],
        **(world.status() if world is not None else
           {"available": False, "source": None, "collections": []}),
    }
