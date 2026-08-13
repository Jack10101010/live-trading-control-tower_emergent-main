"""HARDEN-1 — the canonical operator identity source.

WHY THIS EXISTS
    Before this module, `server._operator_id()` returned
    `WORLD["operators"][0]["operatorId"]` — the first entry of a TEST FIXTURE —
    falling back to the literal string `"system"`. Every audit record, every
    broker context and every safety `operator_ref` therefore traced its actor
    back to `world.v1.json`. An audit trail whose actor is fabricated from a
    fixture is worse than one with no actor at all, because it looks credible.

WHAT THIS PROVIDES
    A single, explicit answer to "who is acting?", sourced in priority order
    from things that are actually true:

      1. the identity the caller ASSERTED for this request (LIVE-4E already
         resolves and validates this for decisions);
      2. the operator this deployment is CONFIGURED to run as
         (`CONTROL_TOWER_OPERATOR_ID`);
      3. nothing — reported as `UNATTRIBUTED`.

    `UNATTRIBUTED` is a first-class answer, not a failure. An action taken with
    no established operator is a real occurrence and the audit trail should say
    so plainly rather than inventing a plausible name.

WHAT IT DELIBERATELY DOES NOT DO
    It does not authenticate. The API boundary is a single shared token with no
    users, roles or sessions (`auth_policy`'s own docstring says so), and this
    module does not pretend otherwise: `assurance()` reports whether the identity
    was merely asserted or backed by an authenticating boundary, and callers
    record that alongside the id.
"""

from __future__ import annotations

import os
import re

#: The environment variable naming the operator this process runs as.
VAR_OPERATOR_ID = "CONTROL_TOWER_OPERATOR_ID"

#: The explicit "no operator could be established" answer. Chosen to be obvious
#: in a log or a ledger row — nobody mistakes it for a person's id.
UNATTRIBUTED = "UNATTRIBUTED"

#: Same shape rule the LIVE-4E decision gate enforces, so an id accepted here is
#: accepted there. Bounded and charset-limited: an operator id ends up in audit
#: records and log lines, and must not be able to smuggle markup or newlines.
OPERATOR_PATTERN = re.compile(r"[A-Za-z0-9_.:@-]{3,64}")

#: Never accepted as an operator id — these indicate a caller passing a
#: credential where an identifier belongs.
_SECRET_MARKERS = ("bearer", "password", "secret", "token", "apikey", "api_key")

# ── assurance vocabulary (mirrors recommendation_authorization) ───────────────
ASSURANCE_AUTHENTICATED = "authenticated"
ASSURANCE_ASSERTED = "asserted"
ASSURANCE_NONE = "none"


def is_valid(candidate) -> bool:
    """Whether a value is usable as an operator id at all."""
    if candidate is None:
        return False
    text = str(candidate).strip()
    if not text or not OPERATOR_PATTERN.fullmatch(text):
        return False
    return not any(marker in text.lower() for marker in _SECRET_MARKERS)


def configured_operator(env: dict | None = None) -> str | None:
    """The operator this deployment is configured to run as, or None."""
    source = os.environ if env is None else env
    raw = source.get(VAR_OPERATOR_ID)
    candidate = str(raw).strip() if raw is not None else ""
    return candidate if is_valid(candidate) else None


def resolve(asserted=None, *, env: dict | None = None) -> str:
    """The acting operator id.

    Priority: asserted-for-this-request, then deployment configuration, then
    `UNATTRIBUTED`. Never returns a fixture value and never invents a name.
    """
    if is_valid(asserted):
        return str(asserted).strip()
    return configured_operator(env) or UNATTRIBUTED


def assurance(operator_id: str, *, boundary_authenticated: bool = False) -> str:
    """How much the id is worth. Recorded next to it so no reader over-trusts it."""
    if operator_id == UNATTRIBUTED:
        return ASSURANCE_NONE
    return ASSURANCE_AUTHENTICATED if boundary_authenticated else ASSURANCE_ASSERTED


def is_attributed(operator_id) -> bool:
    """Whether an action can be attributed to anyone at all."""
    return bool(operator_id) and operator_id != UNATTRIBUTED
