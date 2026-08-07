"""M-GOLDEN-CONTRACT-1 — suite-level safety guards.

1. The live Lux source tree must be byte-identical before and after the whole
   test session. Any test that mutates it — even one that intends to restore —
   fails the session loudly instead of racing a concurrently-starting node.

2. Candidate validation must collect only tracked tests. Untracked test files
   silently joining a deployment validation is how six unrelated
   ``test_oracle_*`` files produced 128 spurious failures on the VPS.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
LUX_ROOT = REPO_ROOT.parent / "Lux-OB-Backtester"


def _governed_digests() -> dict[str, str] | None:
    if not LUX_ROOT.exists():
        return None
    try:
        import sys
        if str(REPO_ROOT) not in sys.path:
            sys.path.insert(0, str(REPO_ROOT))
        from live.engine_identity import governed_files
        return {str(p.relative_to(LUX_ROOT)):
                hashlib.sha256(p.read_bytes()).hexdigest()
                for p in governed_files(LUX_ROOT)}
    except Exception:
        return None            # guard must never break collection itself


@pytest.fixture(scope="session", autouse=True)
def lux_source_tree_is_never_mutated():
    before = _governed_digests()
    yield
    after = _governed_digests()
    if before is None or after is None:
        return
    changed = sorted(k for k in before
                     if after.get(k) != before[k]) + \
              sorted(set(after) - set(before))
    assert not changed, (
        f"TEST SUITE MUTATED THE LIVE LUX TREE: {changed}. "
        f"Tests must operate on tmp_path mirrors, never the source root.")


def _untracked_test_files() -> list[str]:
    tests_dir = Path(__file__).resolve().parent
    try:
        tracked = set(subprocess.run(
            ["git", "ls-files", "--", str(tests_dir)],
            capture_output=True, text=True, cwd=REPO_ROOT,
            timeout=30).stdout.split())
    except (OSError, subprocess.TimeoutExpired):
        return []               # cannot determine ⇒ do not block collection
    tracked_names = {Path(t).name for t in tracked}
    on_disk = {p.name for p in tests_dir.glob("test_*.py")}
    return sorted(on_disk - tracked_names)


def pytest_configure(config):
    """Report — and under CANDIDATE_VALIDATION=1, refuse — untracked test files.

    Runs in ``pytest_configure`` on the CONTROLLER only (xdist workers carry
    ``workerinput``): raising from a collection hook inside a worker crashes the
    xdist protocol as an INTERNALERROR instead of a named refusal — observed
    firsthand when this guard first flagged its own not-yet-committed files.

    Developers running normally get a warning line, so wider local experiments
    stay possible. The deployment validation command exports
    CANDIDATE_VALIDATION=1, which turns pollution into a hard, named error.
    """
    import os
    if hasattr(config, "workerinput"):
        return                  # worker: controller already decided
    untracked = _untracked_test_files()
    if not untracked:
        return
    message = (f"untracked test files present in backend/tests: {untracked} — "
               f"these are NOT part of the candidate suite")
    if os.environ.get("CANDIDATE_VALIDATION") == "1":
        raise pytest.UsageError("CANDIDATE_VALIDATION=1: " + message)
    print(f"\nWARNING: {message}")


def with_identity_anchors(rows):
    """Give test frames the identity anchor columns real engine frames always
    carry (M-OB-ID-GUARD requires them on every frame). Deterministic defaults
    derived from trade_id keep identity CONTINUOUS across successive frames."""
    import hashlib
    out = []
    for r in rows:
        r = dict(r)
        tid = str(r.get("trade_id", "X_0"))
        stable = int(hashlib.md5(tid.encode()).hexdigest()[:6], 16)
        r.setdefault("ob_id", str(stable % 1000))
        r.setdefault("detection_time", f"2026-07-0{(stable % 9) + 1} 08:00:00+00:00")
        out.append(r)
    return out
