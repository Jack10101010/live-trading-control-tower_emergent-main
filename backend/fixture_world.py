"""HARDEN-1 — the fixture world, behind an explicit boundary.

THE PROBLEM THIS SOLVES
    `world.v1.json` was a HARD IMPORT-TIME DEPENDENCY of the backend:
    `server.py` executed `WORLD = _load_world()` at module scope, and
    `_load_world()` ended in `raise FileNotFoundError`. Deleting the fixture
    directory made the process fail to start — verified empirically. A
    production trading backend that cannot boot without a test fixture is not a
    production backend.

WHAT CHANGED
    Loading is now TOTAL: an absent fixture yields an empty world and an
    `available == False` flag instead of an exception. The backend boots.

WHAT DID NOT CHANGE
    When the fixture IS present, every reader sees exactly what it saw before.
    This preserves the existing demonstration/development surface bit-for-bit
    while removing the hard dependency — the compatibility half of the change.

THE RULE THIS BOUNDARY ENFORCES
    A caller must decide what an ABSENT fixture means for its domain. There are
    exactly two honest answers:

      * this domain has a production source -> use it;
      * this domain has none -> say so (`unavailable(...)`), with a machine
        readable code.

    What a caller may never do is return `[]` or `0` and let the UI read that as
    "nothing is happening". An empty list from a missing fixture and an empty
    list from a real, healthy, idle system are different facts, and this module
    exists so they can be told apart.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Stable code returned by every surface that has no production source yet.
CODE_FIXTURE_ABSENT = "fixture_world_unavailable"

#: The message an operator sees. It states the cause and the consequence, and
#: deliberately does NOT suggest re-adding the fixture as a fix — the fixture is
#: a development convenience, not a production dependency.
DETAIL_FIXTURE_ABSENT = (
    "this surface is backed by the development fixture world, which is not "
    "present. It has no production source yet, so no data can be reported. "
    "This is not a claim that the underlying system is idle or empty."
)


class FixtureWorld:
    """An explicitly-optional fixture source.

    `available` is the whole point: it lets a caller distinguish "the fixture is
    missing" from "the fixture is present and this collection is empty".
    """

    __slots__ = ("_data", "_path", "_available")

    def __init__(self, data: dict | None, path: Path | None = None):
        self._data = data if isinstance(data, dict) else {}
        self._path = path
        self._available = isinstance(data, dict)

    @property
    def available(self) -> bool:
        return self._available

    @property
    def path(self) -> Path | None:
        return self._path

    def get(self, key: str, default: Any = None) -> Any:
        """Dict-compatible read, so existing call sites are unchanged."""
        return self._data.get(key, default)

    def __getitem__(self, key: str) -> Any:
        return self._data[key]

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __bool__(self) -> bool:
        return self._available

    def keys(self):
        return self._data.keys()

    def as_dict(self) -> dict:
        return dict(self._data)

    def status(self) -> dict:
        """Redaction-safe description for diagnostics. Reports the file NAME
        only — never the absolute path, which discloses host layout."""
        return {
            "available": self._available,
            "source": (self._path.name if self._path else None),
            "collections": sorted(self._data.keys()) if self._available else [],
        }


def unavailable_world() -> FixtureWorld:
    """An explicitly-empty world, named so the intent is unmistakable.

    M-ENV-1 uses this in production INSTEAD of loading: `available` is False, so
    every fixture-backed surface reports `CODE_FIXTURE_ABSENT` through the
    existing FIX-2 boundary rather than serving fabricated data. It is the same
    object the loader already returns when no fixture file exists, so production
    exercises a path this repository has shipped and tested for some time — not
    a new one invented for the boundary.
    """
    return FixtureWorld(None, None)


def load(search_paths) -> FixtureWorld:
    """Load the first fixture that exists. TOTAL for I/O: never raises on a
    missing or malformed file (a corrupt development fixture must not stop a
    process from starting).

    M-ENV-1: it DOES raise when called in production. Loading the fixture world
    is an activation, and an activation that cannot legitimately happen must
    fail loudly rather than quietly succeed. `server.py` never reaches this call
    in production — the guard exists so a FUTURE call site cannot reintroduce
    fixture data through a side door.
    """
    import environment                       # local: keeps this module dependency-light
    environment.require_fixture_activation_allowed()
    for candidate in search_paths:
        path = Path(candidate)
        if not path.exists():
            continue
        try:
            with open(path, "r") as handle:
                data = json.load(handle)
        except (OSError, ValueError) as exc:
            logger.warning(
                "fixture world at %s could not be read (%s); continuing without it",
                path.name, type(exc).__name__)
            return FixtureWorld(None, path)
        if not isinstance(data, dict):
            logger.warning("fixture world at %s is not an object; ignoring",
                           path.name)
            return FixtureWorld(None, path)
        return FixtureWorld(data, path)
    logger.info(
        "no fixture world found; fixture-backed surfaces will report "
        "%s. This is expected in a production deployment.", CODE_FIXTURE_ABSENT)
    return FixtureWorld(None, None)


def unavailable(surface: str, *, extra: dict | None = None) -> dict:
    """The canonical body for a surface with no production source.

    Explicit and machine-readable — never an empty collection that a caller
    could mistake for a healthy, idle system.
    """
    body = {
        "error": "unavailable",
        "code": CODE_FIXTURE_ABSENT,
        "surface": surface,
        "detail": DETAIL_FIXTURE_ABSENT,
    }
    if extra:
        body.update(extra)
    return body
