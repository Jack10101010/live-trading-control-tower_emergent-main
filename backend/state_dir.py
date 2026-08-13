"""FIX-1 — the durable-state root, prepared and proven at startup.

THE REGRESSION THIS REPAIRS
    HARDEN-3 made state location deployment configuration
    (`CONTROL_TOWER_STATE_DIR`) but never created the directory. Pointing it at a
    path that did not exist yet produced:

        sqlite3.OperationalError: unable to open database file
          -> execution store unavailable — broker-dispatched commands are denied
          -> scenario / ledger / recommendation stores unavailable

    Every durable store failed to open, execution was denied, and the ledger
    stayed empty — silently re-creating the defect that milestone set out to fix.

    It was invisible to 2,746 passing tests because the ONLY `mkdir` in the
    change lived in `backend/conftest.py`: the test harness created the
    precondition the production code required. The feature worked in tests and
    failed on first real use.

WHAT THIS MODULE GUARANTEES
    * the state root EXISTS before any store is constructed;
    * it is genuinely WRITABLE, proven by writing (a directory can exist and
      still be read-only, or be a file, or be a dangling symlink — `os.access`
      answers about permission bits, not about what will actually happen);
    * a failure is reported with what failed, why, and the likely fix, in the
      same shape the MT5 diagnostics use;
    * preparation is TOTAL — it never raises. A caller decides whether an
      unusable state root should stop startup or be reported and degraded.

WHY NOT JUST `mkdir(exist_ok=True)` AT THE DEFINITION SITE
    Because the interesting failures are the ones that survive a successful
    mkdir: a read-only mount, a path that exists as a file, a full disk. Those
    produce exactly the same opaque `unable to open database file` later, at the
    first store construction, far from the cause. Proving writability once, at
    the root, converts all of them into one accurate message.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

#: Deployment configuration for where durable state lives.
VAR_STATE_DIR = "CONTROL_TOWER_STATE_DIR"

# ── outcome codes (stable, machine-readable) ─────────────────────────────────
STATE_OK = "ok"
STATE_CREATED = "created"                 # ok, and this run created it
STATE_NOT_A_DIRECTORY = "state_dir_not_a_directory"
STATE_CREATE_FAILED = "state_dir_create_failed"
STATE_NOT_WRITABLE = "state_dir_not_writable"

#: Written and removed to prove writability. Named so an operator who finds a
#: leftover copy knows exactly what produced it.
_PROBE_NAME = ".control-tower-write-probe"


@dataclass(frozen=True)
class StateDirStatus:
    """The result of preparing the state root. `ok` is the only success."""
    path: str
    code: str
    ok: bool
    created: bool = False
    detail: str | None = None
    fix: str | None = None

    def as_dict(self) -> dict:
        # The path is deployment configuration an operator set themselves, so it
        # is reported in full — unlike a credential. Diagnostics need it to be
        # actionable ("it tried to write HERE").
        return {"path": self.path, "code": self.code, "ok": self.ok,
                "created": self.created, "detail": self.detail, "fix": self.fix}


def resolve(env: dict | None = None, *, default: Path) -> Path:
    """The configured state root, or `default` when unset/blank."""
    source = os.environ if env is None else env
    raw = source.get(VAR_STATE_DIR)
    candidate = str(raw).strip() if raw is not None else ""
    return Path(candidate) if candidate else Path(default)


def prepare(path: Path) -> StateDirStatus:
    """Create the state root if needed and PROVE it is writable.

    Total: never raises. Returns a status a caller can act on.
    """
    target = Path(path)
    created = False

    if target.exists() and not target.is_dir():
        return StateDirStatus(
            path=str(target), code=STATE_NOT_A_DIRECTORY, ok=False,
            detail=f"{target} exists but is not a directory",
            fix=(f"remove or rename that file, or point {VAR_STATE_DIR} at a "
                 "directory"))

    if not target.exists():
        try:
            target.mkdir(parents=True, exist_ok=True)
            created = True
        except OSError as exc:
            return StateDirStatus(
                path=str(target), code=STATE_CREATE_FAILED, ok=False,
                detail=f"{type(exc).__name__}: {exc.strerror or exc}",
                fix=(f"create {target} manually and grant the service write "
                     f"access, or set {VAR_STATE_DIR} to a writable location"))

    # Prove writability by WRITING. `os.access` reports permission bits, which
    # are not the same question: a read-only mount, an exhausted disk or a
    # container without the right uid all pass the bit check and fail the write.
    probe = target / _PROBE_NAME
    try:
        with open(probe, "w") as handle:
            handle.write("ok")
    except OSError as exc:
        return StateDirStatus(
            path=str(target), code=STATE_NOT_WRITABLE, ok=False, created=created,
            detail=f"{type(exc).__name__}: {exc.strerror or exc}",
            fix=(f"grant the service write access to {target} (a read-only "
                 "mount, a full disk or a uid mismatch all present this way)"))
    finally:
        try:
            probe.unlink()
        except OSError:
            pass          # a leftover probe is harmless; failing to remove it
                          # must not turn a healthy root into an unhealthy one.

    return StateDirStatus(path=str(target),
                          code=STATE_CREATED if created else STATE_OK,
                          ok=True, created=created)
