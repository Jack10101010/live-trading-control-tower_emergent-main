"""M-STRATEGY-AUTHORITY-2 — the tracked strategy specification.

THE PRODUCTION STRATEGY IS THE COHORT x MARKET-STATE MATRIX. It lives in a
tracked, reviewed file:

    <lux_root>/strategy/production_strategy.v1.json

Before this module existed, the only copy of that specification was
``generated_configs/<hash>.json`` — which `.gitignore` excludes. The 62
eligibility blocks and 78 state-target cells that ARE the strategy had no
version history, no review trail, and no protection from `git clean`. The
engine identity guard covered the file's DIGEST, so a change was detected, but
nothing preserved its CONTENT.

Design, deliberately the simplest one that changes no behaviour: the runtime
keeps reading the generated path exactly as before, and this module proves that
path is byte-identical to the tracked authority. The authority is what a human
reviews; the generated copy is a cache. If they ever disagree, the node refuses
rather than trading an unreviewed strategy.

Fail-closed in every direction: a missing authority, a missing runtime copy, an
unreadable file or any digest difference all refuse. "Cannot prove they match"
is treated exactly like "they do not match".
"""

from __future__ import annotations

import hashlib
from pathlib import Path

#: Tracked, reviewed strategy specification, relative to the Lux root.
AUTHORITY_RELPATH = "strategy/production_strategy.v1.json"

REFUSE_MISSING_AUTHORITY = "strategy_authority_missing"
REFUSE_MISSING_RUNTIME = "strategy_runtime_config_missing"
REFUSE_MISMATCH = "strategy_authority_mismatch"
REFUSE_UNREADABLE = "strategy_authority_unreadable"


def _digest(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def authority_path(lux_root) -> Path:
    return Path(lux_root) / AUTHORITY_RELPATH


def authority_digest(lux_root) -> str | None:
    """sha256 of the tracked strategy specification, or None if unreadable.

    None is never "fine": every consumer treats it as a refusal reason.
    """
    if lux_root is None:
        return None
    return _digest(authority_path(lux_root))


def verify(lux_root, runtime_config_path) -> tuple[bool, str, str | None]:
    """(ok, detail, authority_digest).

    Proves the config the engine actually loads is byte-identical to the
    tracked authority. Returns the authority digest so callers can record it
    without hashing the file twice.
    """
    if lux_root is None:
        return False, f"{REFUSE_MISSING_AUTHORITY}: no lux_root", None
    a_path = authority_path(lux_root)
    if not a_path.exists():
        return False, (f"{REFUSE_MISSING_AUTHORITY}: {a_path} — the strategy "
                       "specification must be tracked, not generated-only"), None
    r_path = Path(runtime_config_path) if runtime_config_path else None
    if r_path is None or not r_path.exists():
        return False, f"{REFUSE_MISSING_RUNTIME}: {r_path}", None
    a_dig, r_dig = _digest(a_path), _digest(r_path)
    if a_dig is None or r_dig is None:
        return False, f"{REFUSE_UNREADABLE}: authority={a_dig} runtime={r_dig}", a_dig
    if a_dig != r_dig:
        return False, (f"{REFUSE_MISMATCH}: tracked {a_dig[:12]}… != runtime "
                       f"{r_dig[:12]}… — the engine would trade an unreviewed "
                       "strategy"), a_dig
    return True, f"strategy authority verified ({a_dig[:12]}…)", a_dig
