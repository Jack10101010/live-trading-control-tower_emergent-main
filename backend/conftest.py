"""HARDEN-3 — durable-state isolation, established before ANY module imports.

WHY THIS FILE IS AT THE BACKEND ROOT AND NOT IN `tests/`
    pytest imports the rootdir `conftest.py` before test packages, and therefore
    before any test module imports `server`. That ordering is the whole point:
    `server` resolves `STATE_DIR` from the environment AT IMPORT, so the variable
    has to exist before the first import in every worker process — including
    xdist workers, which each import this file for themselves.

    `tests/conftest.py` was too late for that guarantee. It set the state root
    correctly, but a worker that imported `server` while collecting a module
    would already have computed the six database paths against the source tree.
    Symptom: a parallel run left `execution_state.db`, `scenario_state.db` and
    `trade_ledger.db` in `backend/`, and the stray-file guard then fired on every
    subsequent test — naming victims rather than the creator.

WHY IT MATTERS BEYOND TESTS
    Before HARDEN-3 the six durable stores were hardcoded into the source tree,
    so a running process wrote databases next to its own code and any second
    instance or background thread wrote to the same files regardless of who owned
    them. `CONTROL_TOWER_STATE_DIR` makes state location deployment
    configuration; this file is simply the test deployment's choice.
"""

from __future__ import annotations

import os
import tempfile

#: Set before `server` (or any store) can be imported. `setdefault` so an
#: explicit outer value — a developer inspecting a specific state directory —
#: still wins.
_STATE_DIR = os.environ.setdefault(
    "CONTROL_TOWER_STATE_DIR",
    tempfile.mkdtemp(prefix="ct-test-state-"))


def pytest_configure(config):
    """Re-derive the state paths AFTER import, if `server` is already loaded.

    Setting the environment variable is not sufficient on its own: `server`
    computes its six database paths at import, so any process that imported it
    before this file ran (the xdist controller during collection, for instance)
    would keep paths pointing at the source tree. Re-deriving here makes the
    isolation authoritative regardless of import order — which is what removed
    the last cross-worker leak.
    """
    import sys
    from pathlib import Path
    server = sys.modules.get("server")
    if server is None or not hasattr(server, "STATE_DIR"):
        return
    root = Path(_STATE_DIR)
    root.mkdir(parents=True, exist_ok=True)
    server.STATE_DIR = root
    for attr, filename in (("EVENTS_DB_PATH", "events.db"),
                           ("RUNTIME_DB_PATH", "runtime.db"),
                           ("EXECUTION_DB_PATH", "execution_state.db"),
                           ("SCENARIO_DB_PATH", "scenario_state.db"),
                           ("LEDGER_DB_PATH", "trade_ledger.db"),
                           ("RECOMMENDATION_DB_PATH", "recommendation_state.db")):
        if hasattr(server, attr):
            setattr(server, attr, root / filename)
