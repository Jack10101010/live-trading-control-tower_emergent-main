"""Shared test helpers.

`code_only()` exists because several safety guards grep a module's source for a
required construct (`hmac.compare_digest`, `security_config.redact_text`, …). Those
modules DOCUMENT the very constructs they are required to use, so a whole-file grep
is satisfied by the module docstring alone and keeps passing even if every real call
site is deleted. Stripping the docstring and comment lines makes the guard match
actual code, which is the property the guard was written to assert.

Behavioural coverage is preferred over source greps wherever it is practical; these
structural guards remain as a second line of defence.
"""

from __future__ import annotations

import ast
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"

# HARDEN-3: point ALL durable state at a per-session temp directory BEFORE any
# test module imports `server`.
#
# Per-test monkeypatching of individual DB paths cannot cover every writer: the
# runtime supervisor is a daemon thread that can still be inside its interval
# sleep when the test that started it ends, and since HARDEN-2 a tick can write
# three stores. Setting the state root at collection time makes isolation
# structural rather than a race — whichever thread or embedded instance writes,
# it writes to the temp directory.
_STATE_DIR = Path(tempfile.mkdtemp(prefix="ct-test-state-"))
os.environ.setdefault("CONTROL_TOWER_STATE_DIR", str(_STATE_DIR))


@pytest.fixture(autouse=True)
def _isolated_execution_store(tmp_path, monkeypatch):
    """ARCH-2: every test gets an isolated execution store.

    The durable execution/order store defaults to `backend/execution_state.db`.
    Tests that drive broker-dispatched commands or the broker-sync cycle would
    otherwise create that file inside the repository (the same failure mode the
    events.db stray-file guard exists for) and leak lifecycle rows between tests.
    If the server module is loaded in this worker, point the store at tmp_path and
    reset the lazy cache; if it is not loaded, there is nothing to isolate.
    """
    server = sys.modules.get("server")
    if server is not None and hasattr(server, "EXECUTION_DB_PATH"):
        monkeypatch.setattr(server, "EXECUTION_DB_PATH", tmp_path / "execution_state.db")
        monkeypatch.setattr(server, "_EXECUTION_STORE", None)
        monkeypatch.setattr(server, "_EXECUTION_STORE_FAILED", False)
    # LIVE-4B: the scenario store is a second durable file with the same hazard —
    # any test that reaches an /api/operations or /api/scenarios route would
    # otherwise create backend/scenario_state.db inside the repository.
    if server is not None and hasattr(server, "SCENARIO_DB_PATH"):
        monkeypatch.setattr(server, "SCENARIO_DB_PATH", tmp_path / "scenario_state.db")
        monkeypatch.setattr(server, "_SCENARIO_STORE", None)
        monkeypatch.setattr(server, "_SCENARIO_STORE_FAILED", False)
    # LIVE-4D: the recommendation store is a fourth durable file — any test that
    # reaches an /api/trade-recommendations route would otherwise create
    # backend/recommendation_state.db inside the repository.
    if server is not None and hasattr(server, "RECOMMENDATION_DB_PATH"):
        monkeypatch.setattr(server, "RECOMMENDATION_DB_PATH",
                            tmp_path / "recommendation_state.db")
        monkeypatch.setattr(server, "_RECOMMENDATION_STORE", None)
        monkeypatch.setattr(server, "_RECOMMENDATION_STORE_FAILED", False)
    # LIVE-5B: the trade ledger is a fifth durable file. The LIVE-5B integration
    # diagnostics endpoint probes every store, so it is the first code path that
    # opens this one from a request — which is how the gap surfaced.
    if server is not None and hasattr(server, "LEDGER_DB_PATH"):
        monkeypatch.setattr(server, "LEDGER_DB_PATH", tmp_path / "trade_ledger.db")
        monkeypatch.setattr(server, "_LEDGER_STORE", None)
        if hasattr(server, "_LEDGER_STORE_FAILED"):
            monkeypatch.setattr(server, "_LEDGER_STORE_FAILED", False)
    # The runtime OVERLAY database (entity pause/resume state and operator
    # preferences). Unlike the three stores above it has NO lazy cache to reset:
    # `server._runtime_db()` opens a fresh connection from the module-level path
    # on every call, so patching the path is the whole of the isolation.
    if server is not None and hasattr(server, "RUNTIME_DB_PATH"):
        monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    # HARDEN-2: ledger ingestion now runs as a RUNTIME TICK OBSERVER, so the
    # supervisor's background thread reaches the ledger, execution, scenario and
    # broker-history reads. A test that starts the app lifecycle would let that
    # thread run OUTSIDE this fixture's monkeypatch scope and create stores at
    # the real paths. Background ingestion is therefore disabled for tests;
    # tests that want it call `_LEDGER_INGESTION.sweep()` explicitly, which
    # deliberately does not consult the enabled flag.
    if server is not None and hasattr(server, "LEDGER_INGESTION_ENABLED"):
        monkeypatch.setattr(server, "LEDGER_INGESTION_ENABLED", False)
    # The supervisor is a DAEMON thread started by the app-startup hook. A test
    # using `with TestClient(...)` starts it, and it can still be inside its
    # interval sleep when that test ends — so it ticks during LATER tests, with
    # module paths those tests never patched. Since HARDEN-2 a tick can write
    # three durable stores, so a lingering thread creates them at the real
    # paths. Tests drive the runtime explicitly via `tick_once()`; the loop is
    # therefore never started under test.
    if server is not None and hasattr(server, "RUNTIME_SUPERVISOR_ENABLED"):
        monkeypatch.setattr(server, "RUNTIME_SUPERVISOR_ENABLED", False)
        try:
            server._RUNTIME_SUPERVISOR.stop(timeout_s=1.0)
        except Exception:
            pass
    yield
    # Stop the supervisor at TEARDOWN as well as setup. Stopping only at setup
    # left the real hole: a test that starts the app lifecycle launches the
    # daemon thread, and when monkeypatch unwinds, both the enabled flag AND the
    # store paths revert — so the still-sleeping thread wakes and ticks against
    # the SOURCE TREE. Since HARDEN-2 a tick can write three durable stores.
    if server is not None and hasattr(server, "_RUNTIME_SUPERVISOR"):
        try:
            server._RUNTIME_SUPERVISOR.stop(timeout_s=1.0)
        except Exception:
            pass
    # HARDEN-3: the stray-database check is SESSION scoped (see
    # `pytest_sessionfinish` below), not per test.
    #
    # It used to assert here, once per test, against a path shared by every
    # process in the run. Under `-n 2` that is not a property a test can control:
    # one worker creating a file failed the teardown of every subsequent test in
    # every worker, so a single leak produced hundreds of errors that named
    # victims rather than the creator. Worse, subprocess-based tests can create
    # these files from outside any fixture's scope entirely.
    #
    # Detection is preserved and is now accurate: one clear report at session
    # end, naming the files, with the artifacts removed so the next run starts
    # from a clean tree.


def code_only(rel: str) -> str:
    """Module source with the module docstring and all comment lines removed.

    Only the MODULE docstring is stripped (function/class docstrings are left in
    place); comment lines are dropped wholesale. The result is what a guard should
    be matching against when it asserts "this module really does X".
    """
    text = (BACKEND_DIR / rel).read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


#: Durable databases that must never appear in the source tree. State belongs
#: under `CONTROL_TOWER_STATE_DIR` (HARDEN-3); anything here is a leak.
_STRAY_DATABASES = ("events.db", "runtime.db", "execution_state.db",
                    "scenario_state.db", "trade_ledger.db",
                    "recommendation_state.db")


def pytest_sessionfinish(session, exitstatus):
    """Report and clear any database left in the source tree.

    A warning rather than a failure: the creator is frequently a subprocess
    outside any test's control, and failing the suite for it names the wrong
    culprit. The commit-time artifact scan and `.gitignore` are what actually
    prevent these reaching the repository; this keeps the signal visible.
    """
    strays = [name for name in _STRAY_DATABASES if (BACKEND_DIR / name).exists()]
    if not strays:
        return
    for name in strays:
        try:
            (BACKEND_DIR / name).unlink()
        except OSError:
            pass
    session.config.pluginmanager.get_plugin("terminalreporter").write_line(
        "\nSTRAY DATABASES removed from the source tree: "
        + ", ".join(strays)
        + "\n  Durable state belongs under CONTROL_TOWER_STATE_DIR. A test (or a"
          " subprocess it spawned) wrote to the source tree instead.",
        yellow=True)
