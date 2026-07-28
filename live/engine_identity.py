"""Complete engine identity for the governed Lux production tree.

WHY THIS EXISTS
---------------
Lux's own `engine_version()` hashes exactly three files:

    src/execution.py · scripts/run_backtest.py · src/resume_support.py

That list was correct before Milestone 2. M2 relocated every strategy subsystem
into `strategy_core/`, leaving `src/execution.py` as a ~15-line re-export shim —
but the source list was never updated. Measured on the pinned tree, the
production import closure is 23 local modules and **only 3 are covered**: the
entire walk, order-block detection, swing detection, regime, policy, sessions,
news and the ghost tracker can all be modified while `engine_version` still
reports `5bb6372c…` and every existing gate passes.

This module closes that gap WITHOUT touching the frozen engine. Lux stays pinned
at its Golden commit and `engine_version()` keeps its existing external contract
(it is still computed, reported and gated); this is an ADDITIONAL, complete
identity computed over the governed file set.

DESIGN (hybrid: curated roots + verified import closure)
-------------------------------------------------------
Governed = every ``.py`` under `strategy_core/` and `src/`, plus the driver
`scripts/run_backtest.py`. Roots are curated (not an opaque whole-repo hash) so
docs, notes, outputs and research artefacts never perturb the identity; the set
is glob-derived (not a hand-list) so a NEW strategy module cannot silently
escape governance. `verify_loaded_modules()` additionally checks that what the
interpreter actually imported is a subset of what was hashed — catching import
shadowing, stray copies and modules loaded from an unexpected root.

Line endings are normalised to LF before hashing. The legacy algorithm hashes
raw bytes, which made its digest depend on the checkout's autocrlf setting — a
real defect this deployment already hit once. Normalising makes the identity
reproducible on Windows and macOS from the same commit.
"""

from __future__ import annotations

import hashlib
import json
import platform
import sys
from pathlib import Path

MANIFEST_SCHEMA = "engine-manifest-v1"

# Curated governed roots. Globs, so new modules are captured automatically.
GOVERNED_GLOBS = ("strategy_core/**/*.py", "src/**/*.py", "scripts/run_backtest.py")
# Excluded: self-test scripts that live inside src/ but are never imported by the
# production path (verified against the measured import closure).
EXCLUDED_SUFFIXES = ("_test.py",)
EXCLUDED_PARTS = ("__pycache__", ".venv", "site-packages")


def _canonical_rel(path: Path, root: Path) -> str:
    """POSIX-style path relative to the Lux root — identical on Windows and macOS."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def _normalised_bytes(path: Path) -> bytes:
    """File content with CRLF/CR normalised to LF, so the digest is checkout-agnostic."""
    return path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def file_digest(path: Path) -> str:
    return hashlib.sha256(_normalised_bytes(path)).hexdigest()


def governed_files(lux_root: Path) -> list[Path]:
    root = Path(lux_root).resolve()
    found: set[Path] = set()
    for pattern in GOVERNED_GLOBS:
        for p in root.glob(pattern):
            if not p.is_file():
                continue
            if p.name.endswith(EXCLUDED_SUFFIXES):
                continue
            if any(part in EXCLUDED_PARTS for part in p.parts):
                continue
            found.add(p.resolve())
    return sorted(found, key=lambda p: _canonical_rel(p, root))


def build_manifest(lux_root: Path) -> dict:
    """Deterministic manifest of every governed file: canonical path + digest.

    Fails closed on a path that escapes the Lux root or a duplicate canonical
    path — either would let two different trees produce the same identity.
    """
    root = Path(lux_root).resolve()
    entries, seen = [], set()
    for p in governed_files(root):
        rel = _canonical_rel(p, root)
        if rel in seen:
            raise ValueError(f"duplicate canonical path in manifest: {rel}")
        if ".." in rel.split("/"):
            raise ValueError(f"governed path escapes the Lux root: {rel}")
        seen.add(rel)
        entries.append({"path": rel, "sha256": file_digest(p)})
    return {"schema": MANIFEST_SCHEMA, "file_count": len(entries), "files": entries,
            "engine_manifest_id": _aggregate(entries)}


def _aggregate(entries: list[dict]) -> str:
    """Order-independent-by-construction aggregate (entries are already sorted)."""
    h = hashlib.sha256()
    for e in entries:
        h.update(e["path"].encode("utf-8")); h.update(b"\0")
        h.update(e["sha256"].encode("ascii")); h.update(b"\0")
    return h.hexdigest()


def manifest_id(lux_root: Path) -> str:
    return build_manifest(lux_root)["engine_manifest_id"]


def diff_manifests(expected: dict, actual: dict) -> dict:
    """Exactly which governed files were added, removed or modified."""
    exp = {e["path"]: e["sha256"] for e in expected.get("files", [])}
    act = {e["path"]: e["sha256"] for e in actual.get("files", [])}
    return {
        "missing": sorted(p for p in exp if p not in act),          # governed file gone
        "unexpected": sorted(p for p in act if p not in exp),       # new ungoverned module
        "modified": sorted(p for p in exp if p in act and exp[p] != act[p]),
    }


def verify(lux_root: Path, expected: dict | None) -> tuple[bool, str, dict]:
    """Compare the live tree against the approved manifest. Fails closed."""
    actual = build_manifest(lux_root)
    if not expected:
        return False, ("no approved engine manifest recorded — cannot certify the "
                       "engine tree"), actual
    if expected.get("schema") != MANIFEST_SCHEMA:
        return False, f"manifest schema {expected.get('schema')!r} != {MANIFEST_SCHEMA}", actual
    if expected.get("engine_manifest_id") == actual["engine_manifest_id"]:
        return True, f"{actual['file_count']} governed files match the approved manifest", actual
    d = diff_manifests(expected, actual)
    parts = []
    if d["modified"]:
        parts.append(f"MODIFIED {len(d['modified'])}: {', '.join(d['modified'][:5])}")
    if d["missing"]:
        parts.append(f"MISSING {len(d['missing'])}: {', '.join(d['missing'][:5])}")
    if d["unexpected"]:
        parts.append(f"UNEXPECTED {len(d['unexpected'])}: {', '.join(d['unexpected'][:5])}")
    return False, "engine tree differs from the approved manifest — " + "; ".join(parts), actual


def verify_loaded_modules(lux_root: Path, manifest: dict | None = None) -> tuple[bool, str]:
    """Every local Lux module the interpreter actually imported must be governed.

    Catches what a file digest cannot: import shadowing, a stray copy on
    sys.path, or a strategy module loaded from a different root than the one
    that was hashed.
    """
    root = Path(lux_root).resolve()
    manifest = manifest or build_manifest(root)
    governed = {e["path"] for e in manifest["files"]}
    stray = []
    for mod in list(sys.modules.values()):
        f = getattr(mod, "__file__", None)
        if not f:
            continue
        try:
            p = Path(f).resolve()
        except (OSError, ValueError):
            continue
        # is_relative_to, not a string prefix: a sibling "Lux-OB-Backtester2"
        # would string-match the root and be misreported as an engine module.
        if not p.is_relative_to(root):
            continue
        rel = _canonical_rel(p, root)
        if any(part in EXCLUDED_PARTS for part in p.parts):
            continue
        if rel not in governed:
            stray.append(rel)
    if stray:
        return False, ("local Lux modules loaded but NOT governed by the manifest: "
                       + ", ".join(sorted(set(stray))[:8]))
    return True, "all loaded local Lux modules are governed"


def environment_fingerprint() -> dict:
    """Dependency versions known to move governed floats (recorded, not gated).

    The project has measured a ~220 ULP `bbw_value` divergence between hosts
    from pandas/numpy differences alone, so the environment is provenance —
    Parity Policy v2 governs whether a difference is tolerable, not this record.
    """
    try:
        import numpy, pandas
        numpy_v, pandas_v = numpy.__version__, pandas.__version__
    except Exception:  # pragma: no cover - dependency import failure
        numpy_v = pandas_v = "unavailable"
    return {
        "python": sys.version.split()[0],
        "numpy": numpy_v,
        "pandas": pandas_v,
        "platform": platform.platform(),
        "machine": platform.machine(),
    }


def load_manifest(path: Path) -> dict | None:
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def render(manifest: dict, limit: int | None = None) -> str:
    """Human-readable manifest — an identity nobody can inspect is not governance."""
    lines = [f"schema={manifest['schema']}  files={manifest['file_count']}",
             f"engine_manifest_id={manifest['engine_manifest_id']}", ""]
    for e in manifest["files"][:limit]:
        lines.append(f"  {e['sha256'][:16]}  {e['path']}")
    if limit and manifest["file_count"] > limit:
        lines.append(f"  … {manifest['file_count'] - limit} more")
    return "\n".join(lines)


def main() -> int:  # pragma: no cover - operator utility
    import argparse
    from live.config import LiveConfig
    ap = argparse.ArgumentParser(description="Show or record the governed engine manifest.")
    ap.add_argument("--write", metavar="PATH", help="write the manifest JSON to PATH")
    ap.add_argument("--limit", type=int, default=None, help="limit files shown")
    args = ap.parse_args()
    cfg = LiveConfig()
    m = build_manifest(cfg.lux_root)
    print(render(m, args.limit))
    print("\nenvironment: " + json.dumps(environment_fingerprint()))
    if args.write:
        Path(args.write).write_text(json.dumps(m, indent=1))
        print(f"\nwritten: {args.write}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
