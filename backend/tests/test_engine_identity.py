"""Engine identity governance — proves the manifest catches what engine_version misses.

The governing fact these tests exist for: Lux's `engine_version()` hashes three
files, but M2 moved every strategy subsystem into `strategy_core/`. A measured
import closure of the production path is 23 local modules; 3 are covered. These
tests assert the manifest closes that gap and that the gates actually fire —
not merely that the code runs.

Filesystem tests build a synthetic Lux tree in tmp_path. The two that must touch
the real pinned tree mutate one file and restore its exact bytes in a finally
block, asserting the digest returns to its original value.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from live import engine_identity as ei

LUX_ROOT = Path(__file__).resolve().parents[2].parent / "Lux-OB-Backtester"
real_lux = pytest.mark.skipif(not LUX_ROOT.exists(), reason="pinned Lux tree not present")


def make_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
    return root


BASE = {
    "strategy_core/__init__.py": "from .execution import simulate_trades\n",
    "strategy_core/execution.py": "def simulate_trades():\n    return 1\n",
    "src/execution.py": "from strategy_core.execution import *\n",
    "src/config.py": "RISK = 1.0\n",
    "scripts/run_backtest.py": "ACTIVE_CONFIG = None\n",
}


# ── the core defect: strategy_core must be governed ─────────────────────────────
def test_manifest_governs_strategy_core(tmp_path):
    m = ei.build_manifest(make_tree(tmp_path, BASE))
    paths = {e["path"] for e in m["files"]}
    assert "strategy_core/execution.py" in paths
    assert "strategy_core/__init__.py" in paths
    assert len(paths) == len(BASE)


def test_strategy_core_edit_changes_identity(tmp_path):
    """The exact mutation engine_version cannot see."""
    root = make_tree(tmp_path, BASE)
    before = ei.manifest_id(root)
    (root / "strategy_core/execution.py").write_text("def simulate_trades():\n    return 2\n")
    assert ei.manifest_id(root) != before


def test_new_ungoverned_module_is_captured(tmp_path):
    """Globs, not a hand-list — a NEW strategy module cannot escape governance."""
    root = make_tree(tmp_path, BASE)
    before = ei.build_manifest(root)
    (root / "strategy_core/new_filter.py").write_text("def f():\n    return 0\n")
    after = ei.build_manifest(root)
    assert after["engine_manifest_id"] != before["engine_manifest_id"]
    assert ei.diff_manifests(before, after)["unexpected"] == ["strategy_core/new_filter.py"]


# ── determinism and portability ────────────────────────────────────────────────
def test_identity_is_deterministic(tmp_path):
    root = make_tree(tmp_path, BASE)
    assert ei.manifest_id(root) == ei.manifest_id(root)


def test_identity_survives_crlf_checkout(tmp_path):
    """Legacy engine_version hashes raw bytes, so autocrlf changed its digest —
    a defect this deployment already hit. The manifest must be checkout-agnostic."""
    lf = make_tree(tmp_path / "lf", BASE)
    crlf = tmp_path / "crlf"
    for rel, body in BASE.items():
        p = crlf / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body.replace("\n", "\r\n").encode())
    assert ei.manifest_id(lf) == ei.manifest_id(crlf)


def test_identity_is_path_independent(tmp_path):
    """Same content at a different absolute path yields the same identity."""
    a = make_tree(tmp_path / "deploy_a", BASE)
    b = make_tree(tmp_path / "somewhere" / "else" / "deploy_b", BASE)
    assert ei.manifest_id(a) == ei.manifest_id(b)


def test_ordering_is_stable_regardless_of_creation_order(tmp_path):
    a = make_tree(tmp_path / "a", BASE)
    b = make_tree(tmp_path / "b", dict(reversed(list(BASE.items()))))
    assert ei.manifest_id(a) == ei.manifest_id(b)
    assert [e["path"] for e in ei.build_manifest(a)["files"]] == \
           [e["path"] for e in ei.build_manifest(b)["files"]]


def test_path_and_content_are_bound_together(tmp_path):
    """Swapping two files' contents must change the identity — otherwise the
    digest is a content-set hash and renames are invisible."""
    root = make_tree(tmp_path, BASE)
    before = ei.manifest_id(root)
    a, b = root / "src/config.py", root / "scripts/run_backtest.py"
    a_body, b_body = a.read_text(), b.read_text()
    a.write_text(b_body); b.write_text(a_body)
    assert ei.manifest_id(root) != before


# ── exclusions ────────────────────────────────────────────────────────────────
def test_non_governed_files_do_not_perturb_identity(tmp_path):
    """Docs, outputs, notebooks and __pycache__ must not trip the gate."""
    root = make_tree(tmp_path, BASE)
    before = ei.manifest_id(root)
    for rel in ("README.md", "outputs/run.csv", "notes.txt",
                "src/__pycache__/config.cpython-312.pyc", "src/thing_test.py"):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
    assert ei.manifest_id(root) == before


# ── verify() semantics: fail closed ───────────────────────────────────────────
def test_verify_matches_approved_manifest(tmp_path):
    root = make_tree(tmp_path, BASE)
    ok, detail, _ = ei.verify(root, ei.build_manifest(root))
    assert ok and "5 governed files" in detail


def test_verify_fails_closed_without_a_manifest(tmp_path):
    ok, detail, _ = ei.verify(make_tree(tmp_path, BASE), None)
    assert not ok and "no approved engine manifest" in detail


def test_verify_rejects_unknown_schema(tmp_path):
    root = make_tree(tmp_path, BASE)
    stale = ei.build_manifest(root) | {"schema": "engine-manifest-v0"}
    ok, detail, _ = ei.verify(root, stale)
    assert not ok and "schema" in detail


def test_verify_names_the_modified_file(tmp_path):
    """An identity that fails without saying what changed is not diagnosable."""
    root = make_tree(tmp_path, BASE)
    approved = ei.build_manifest(root)
    (root / "strategy_core/execution.py").write_text("def simulate_trades():\n    return 99\n")
    ok, detail, _ = ei.verify(root, approved)
    assert not ok
    assert "MODIFIED" in detail and "strategy_core/execution.py" in detail


def test_verify_reports_a_deleted_governed_file(tmp_path):
    root = make_tree(tmp_path, BASE)
    approved = ei.build_manifest(root)
    (root / "src/config.py").unlink()
    ok, detail, _ = ei.verify(root, approved)
    assert not ok and "MISSING" in detail and "src/config.py" in detail


def test_diff_classifies_all_three_change_kinds(tmp_path):
    root = make_tree(tmp_path, BASE)
    approved = ei.build_manifest(root)
    (root / "src/config.py").unlink()
    (root / "strategy_core/execution.py").write_text("def simulate_trades():\n    return 3\n")
    (root / "strategy_core/extra.py").write_text("x = 1\n")
    d = ei.diff_manifests(approved, ei.build_manifest(root))
    assert d["missing"] == ["src/config.py"]
    assert d["modified"] == ["strategy_core/execution.py"]
    assert d["unexpected"] == ["strategy_core/extra.py"]


# ── loaded-module provenance ──────────────────────────────────────────────────
def test_loaded_module_check_flags_an_ungoverned_import(tmp_path, monkeypatch):
    """Catches what a file digest cannot: a module imported from an unhashed path."""
    root = make_tree(tmp_path, BASE)
    manifest = ei.build_manifest(root)
    rogue = root / "experiments" / "patch.py"
    rogue.parent.mkdir(parents=True, exist_ok=True)
    rogue.write_text("x = 1\n")

    class FakeModule:
        __file__ = str(rogue)

    monkeypatch.setitem(sys.modules, "_rogue_probe", FakeModule())
    ok, detail = ei.verify_loaded_modules(root, manifest)
    assert not ok and "experiments/patch.py" in detail


def test_loaded_module_check_passes_for_governed_tree(tmp_path):
    root = make_tree(tmp_path, BASE)
    ok, _ = ei.verify_loaded_modules(root, ei.build_manifest(root))
    assert ok


def test_loaded_module_check_ignores_foreign_roots(tmp_path):
    """Site-packages and CT's own modules are not Lux modules."""
    root = make_tree(tmp_path / "lux", BASE)
    ok, _ = ei.verify_loaded_modules(root, ei.build_manifest(root))
    assert ok  # pandas/pytest/live.* are all loaded right now


# ── structural guards ─────────────────────────────────────────────────────────
def test_empty_tree_yields_no_files(tmp_path):
    m = ei.build_manifest(tmp_path)
    assert m["file_count"] == 0 and m["files"] == []


def test_manifest_shape_is_stable(tmp_path):
    m = ei.build_manifest(make_tree(tmp_path, BASE))
    assert m["schema"] == ei.MANIFEST_SCHEMA
    assert m["file_count"] == len(m["files"])
    assert all(set(e) == {"path", "sha256"} for e in m["files"])
    assert all(len(e["sha256"]) == 64 for e in m["files"])


def test_render_is_human_readable(tmp_path):
    """An identity nobody can inspect is not governance."""
    m = ei.build_manifest(make_tree(tmp_path, BASE))
    text = ei.render(m)
    assert m["engine_manifest_id"] in text
    assert "strategy_core/execution.py" in text


def test_environment_fingerprint_records_parity_relevant_versions():
    env = ei.environment_fingerprint()
    assert set(env) == {"python", "numpy", "pandas", "platform", "machine"}
    assert env["pandas"] != "unavailable"


def test_load_manifest_returns_none_on_corrupt_file(tmp_path):
    bad = tmp_path / "m.json"
    bad.write_text("{not json")
    assert ei.load_manifest(bad) is None
    assert ei.load_manifest(tmp_path / "missing.json") is None


# ── the recorded manifest must describe the real pinned tree ──────────────────
@real_lux
def test_recorded_manifest_matches_the_pinned_tree():
    from live.config import ENGINE_MANIFEST_ID_EXPECTED, ENGINE_MANIFEST_PATH
    approved = ei.load_manifest(ENGINE_MANIFEST_PATH)
    assert approved is not None, "live/engine_manifest.json is missing or corrupt"
    assert approved["engine_manifest_id"] == ENGINE_MANIFEST_ID_EXPECTED
    ok, detail, _ = ei.verify(LUX_ROOT, approved)
    assert ok, detail


@real_lux
def test_recorded_manifest_covers_the_production_import_closure():
    """The 23 modules measured as actually imported by the production path."""
    approved = ei.load_manifest(
        Path(__file__).resolve().parents[2] / "live" / "engine_manifest.json")
    governed = {e["path"] for e in approved["files"]}
    closure = {
        "scripts/run_backtest.py", "src/config.py", "src/data_loader.py",
        "src/execution.py", "src/order_blocks.py", "src/portfolio_policy.py",
        "src/regime.py", "src/reports.py", "src/resample.py",
        "src/resume_support.py", "src/run_outputs.py",
        "strategy_core/__init__.py", "strategy_core/execution.py",
        "strategy_core/ghost_tracker.py", "strategy_core/news.py",
        "strategy_core/order_blocks.py", "strategy_core/policy.py",
        "strategy_core/ports.py", "strategy_core/regime.py",
        "strategy_core/scenario.py", "strategy_core/sessions.py",
        "strategy_core/swings.py", "strategy_core/types.py",
    }
    assert closure <= governed, f"ungoverned production modules: {sorted(closure - governed)}"


@real_lux
def test_blind_spot_is_closed_on_the_real_tree():
    """THE regression guard. Mutate a real strategy file: engine_version must
    stay blind (proving the defect is real) while the manifest must catch it.

    Restores the exact original bytes and asserts the digest returns."""
    from live.config import ENGINE_VERSION_EXPECTED
    target = LUX_ROOT / "strategy_core" / "execution.py"
    original = target.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    approved = ei.load_manifest(
        Path(__file__).resolve().parents[2] / "live" / "engine_manifest.json")
    try:
        target.write_bytes(original + b"\n# governance regression probe\n")
        ok, detail, _ = ei.verify(LUX_ROOT, approved)
        assert not ok, "manifest FAILED to detect a strategy_core edit"
        assert "strategy_core/execution.py" in detail
    finally:
        target.write_bytes(original)
    assert hashlib.sha256(target.read_bytes()).hexdigest() == before, "tree not restored"
    ok, detail, _ = ei.verify(LUX_ROOT, approved)
    assert ok, f"tree not restored: {detail}"
    # And the legacy digest is unchanged throughout — the blind spot is real.
    sys.path.insert(0, str(LUX_ROOT))
    from src.run_outputs import engine_version
    assert engine_version() == ENGINE_VERSION_EXPECTED


@real_lux
def test_startup_refuses_a_tampered_engine(monkeypatch):
    """verify_engine() must raise, not warn, on a manifest mismatch."""
    from live.runner import LuxSession
    session = LuxSession(LUX_ROOT)
    session.verify_engine()          # baseline: the pinned tree passes
    monkeypatch.setattr("live.runner.ENGINE_MANIFEST_ID_EXPECTED", "0" * 64)
    with pytest.raises(RuntimeError, match="engine manifest mismatch"):
        session.verify_engine()


@real_lux
def test_legacy_engine_version_gate_is_preserved():
    """The existing external contract is kept, not replaced."""
    from live.config import ENGINE_VERSION_EXPECTED
    from live.runner import LuxSession
    session = LuxSession(LUX_ROOT)
    assert session.engine_version == ENGINE_VERSION_EXPECTED
    assert session.engine_manifest_id == \
        json.loads(Path(ei.__file__).parent.joinpath("engine_manifest.json").read_text())[
            "engine_manifest_id"]
