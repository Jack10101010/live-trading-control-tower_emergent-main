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
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"


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
    yield
    # Belt-and-braces: if anything slipped past the patch (an import-order edge),
    # fail the offending test loudly instead of leaving a stray database.
    stray = BACKEND_DIR / "execution_state.db"
    assert not stray.exists(), (
        "a test created backend/execution_state.db — the execution store must be "
        "isolated to tmp_path (see _isolated_execution_store)")


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
