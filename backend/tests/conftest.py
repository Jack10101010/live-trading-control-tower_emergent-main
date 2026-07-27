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
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"


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
