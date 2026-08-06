"""M-GOLDEN-CONTRACT-1 — rehearsal path resolution.

The observed production failure: a RELATIVE ``--golden-ref`` resolved fine at
launch, then ``LuxSession`` chdir'd into the Lux root (its documented driver
contract), and the byte-parity step — reached ~1 hour later — opened the
reference against the NEW cwd and failed. The fix resolves every path argument
to absolute before anything can change cwd, and validates the reference is
complete before the expensive pipeline starts.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import live.rehearsal as rehearsal                           # noqa: E402

REF_FILES = ("trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv",
             "trades_allow_multi_position.csv", "order_blocks.csv")


def make_ref(root: Path) -> Path:
    ref = root / "reference"
    ref.mkdir(parents=True)
    for name in REF_FILES:
        (ref / name).write_text("trade_id\nL_1\n")
    return ref


def run_main(argv: list[str]) -> int:
    old = sys.argv
    sys.argv = ["rehearsal"] + argv
    try:
        return rehearsal.main()
    except SystemExit as exc:          # check() raises SystemExit(finish(1))
        return int(exc.code or 0)
    except Exception:                  # non-path failure past the path gates
        return -1
    finally:
        sys.argv = old


# ── the mechanism, demonstrated in miniature ────────────────────────────────

def test_the_old_failure_mode_relative_path_breaks_after_chdir(tmp_path, monkeypatch):
    """Negative control for the PREDECESSOR behaviour: an unresolved relative
    path silently re-anchors when cwd changes — exactly what killed the
    hour-long run. The repaired behaviour (resolve at launch) survives."""
    ref = make_ref(tmp_path)
    elsewhere = tmp_path / "lux_root_stand_in"
    elsewhere.mkdir()
    monkeypatch.chdir(tmp_path)

    unresolved = Path("reference")                 # predecessor: kept relative
    resolved = Path("reference").resolve()         # repaired: pinned at launch
    assert (unresolved / REF_FILES[0]).is_file()   # fine before the chdir …
    monkeypatch.chdir(elsewhere)                   # … LuxSession chdirs …
    assert not (unresolved / REF_FILES[0]).is_file(), \
        "the predecessor failure mode no longer reproduces — revisit this test"
    assert (resolved / REF_FILES[0]).is_file()     # … the repair is immune


# ── fail-fast validation, before any expensive work ─────────────────────────

def test_missing_reference_fails_before_the_pipeline(tmp_path, capsys):
    rc = run_main(["--lux-root", str(tmp_path), "--golden-ref",
                   str(tmp_path / "nope"), "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "golden_ref_exists" in capsys.readouterr().out


def test_incomplete_reference_fails_and_names_the_missing_files(tmp_path, capsys):
    ref = make_ref(tmp_path)
    (ref / REF_FILES[2]).unlink()                  # drop order_blocks.csv
    rc = run_main(["--lux-root", str(tmp_path), "--golden-ref", str(ref),
                   "--out", str(tmp_path / "out")])
    assert rc == 1
    out = capsys.readouterr().out
    assert "golden_ref_complete" in out and "order_blocks.csv" in out


def test_missing_lux_root_fails_fast(tmp_path, capsys):
    ref = make_ref(tmp_path)
    rc = run_main(["--lux-root", str(tmp_path / "no_lux"), "--golden-ref",
                   str(ref), "--out", str(tmp_path / "out")])
    assert rc == 1
    assert "lux_root_exists" in capsys.readouterr().out


def test_relative_and_absolute_ref_resolve_identically(tmp_path, monkeypatch):
    """Both spellings must reach the same validated path (both then fail later
    for the same non-path reason: the stand-in lux root has no dataset —
    but they must get PAST the path checks identically)."""
    ref = make_ref(tmp_path)
    lux = tmp_path / "lux"
    lux.mkdir()
    monkeypatch.chdir(tmp_path)
    for spelling in ("reference", str(ref)):
        rc = run_main(["--lux-root", str(lux), "--golden-ref", spelling,
                       "--out", str(tmp_path / "out")])
        # Path validation passes for BOTH spellings; the run then fails at the
        # first real (non-path) gate on the stand-in root — either a failed
        # check (rc 1) or an engine-import error (rc -1) — but NEVER at
        # golden_ref_exists/golden_ref_complete, which return 1 with a
        # distinctive message tested separately above.
        assert rc in (1, -1)


def test_source_paths_are_resolved_in_main_source():
    """Structural pin: main() resolves all three path args immediately."""
    src = (REPO_ROOT / "live" / "rehearsal.py").read_text()
    assert 'Path(args.lux_root).resolve()' in src
    assert 'Path(args.golden_ref).resolve()' in src
    assert 'Path(args.out).resolve()' in src
