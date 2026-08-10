"""Drift and safety tests for the TradingView oracle parity tooling (Phase 0.5).

These tests are the enforcement half of the design. The tooling can extract a
contract; these assert that it KEEPS extracting the right one, that nothing
silently becomes hand-maintained, and that the read-only safety contract holds.

They run against the DEPLOYED engine, so they also function as a live check that
the pin is intact.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import enums as enums_mod  # noqa: E402
from tools.oracle import fingerprint as fp  # noqa: E402
from tools.oracle.engine_access import (LIVE_ONLY_CONFIG_DELTAS,  # noqa: E402
                                        effective_policy_table, load_engine,
                                        resolve_config)
from tools.oracle.extract_contract import (DEFAULT_OUT, build_contract,  # noqa: E402
                                           canonical_json)
from tools.oracle.impact import IMPACT_MAP_PATH, analyse, load_map  # noqa: E402

CONTRACTS = CT_ROOT / "contracts"


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def contract():
    return build_contract(require_pin=True)


# ── safety: the tooling must not disturb the live node ────────────────────────

def test_loading_the_engine_restores_the_working_directory():
    """LuxSession chdirs by contract; a tool that did not restore would corrupt
    every later relative path in the same process."""
    before = os.getcwd()
    load_engine(require_pin=True)
    assert os.getcwd() == before


def test_tooling_never_touches_live_state_or_the_broker():
    """live_state/ is owned by the running node, which holds an OS lock on it.

    Checks CODE, not prose: a docstring explaining *why* live_state is off-limits
    must not itself trip the test. So this walks the AST for string literals and
    attribute access, ignoring comments and docstrings.
    """
    import ast
    for py in (CT_ROOT / "tools" / "oracle").glob("*.py"):
        tree = ast.parse(py.read_text(encoding="utf-8"))
        # Identity, not text: ast.get_docstring() returns CLEANED text, which
        # never equals the raw Constant value for an indented docstring.
        docstring_nodes = set()
        for n in ast.walk(tree):
            if not isinstance(n, (ast.Module, ast.FunctionDef,
                                  ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            body = getattr(n, "body", None) or []
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                docstring_nodes.add(id(body[0].value))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if id(node) in docstring_nodes:
                    continue
                assert "live_state" not in node.value, (
                    f"{py.name}:{node.lineno} uses a live_state path literal")
            if isinstance(node, ast.Attribute):
                assert node.attr not in ("state_dir", "kill_file", "live_segment_csv"), (
                    f"{py.name}:{node.lineno} reads {node.attr} — that is live-node state")
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                assert name not in ("MT5Gateway", "Executor", "LiveRunner",
                                    "CTPublisher", "AccountObserver"), (
                    f"{py.name}:{node.lineno} constructs {name}")


def test_contract_extraction_is_deterministic(contract):
    again = build_contract(require_pin=True)
    assert canonical_json(contract) == canonical_json(again)


# ── the engine pin ────────────────────────────────────────────────────────────

def test_engine_pin_is_verified(contract):
    assert contract["engine"]["pin_verified"] is True
    assert contract["engine"]["governed_file_count"] == 30


def test_fingerprint_excludes_the_live_only_delta(contract):
    """end_date advances with the frontier every cycle. Including it would
    invalidate the Pine build daily for no behavioural reason."""
    assert "end_date" in LIVE_ONLY_CONFIG_DELTAS
    assert "end_date" not in contract["fingerprint"]["inputs"]


def test_fingerprint_does_not_hash_the_environment(contract):
    """Dependency versions are provenance, not identity — a patch bump must not
    invalidate a build, but must stay visible for divergence triage."""
    inputs = contract["fingerprint"]["inputs"]
    for key in ("python", "numpy", "pandas", "platform", "machine"):
        assert key not in inputs
    assert contract["engine"]["environment_provenance"]["pandas"]


def test_fingerprint_is_reproducible_from_its_inputs(contract):
    f = contract["fingerprint"]
    again = fp.compose(**f["inputs"])
    assert again["oracle_engine_hash"] == f["oracle_engine_hash"]
    assert again["oracle_engine_id"] == f["oracle_engine_id"]


def test_fingerprint_changes_when_any_input_changes(contract):
    base = contract["fingerprint"]["inputs"]
    base_hash = contract["fingerprint"]["oracle_engine_hash"]
    for key in base:
        mutated = dict(base)
        mutated[key] = str(mutated[key]) + "-x"
        assert fp.compose(**mutated)["oracle_engine_hash"] != base_hash, (
            f"fingerprint is insensitive to {key}")


# ── extraction discipline ─────────────────────────────────────────────────────

def test_no_behavioural_value_is_hand_maintained(contract):
    """The whole point: production values are extracted, never transcribed."""
    assert contract["extraction_summary"]["manual_behavioural_values"] == 0
    assert enums_mod.SOURCE_MANUAL not in contract["extraction_summary"]["enums_by_source"]


def test_every_expected_config_field_exists(contract):
    assert contract["configuration"]["missing_expected_fields"] == []


def test_no_unsupported_feature_is_active(contract):
    """If this fails, the deployed engine gained a mechanism the oracle does not
    implement. The oracle is INCOMPATIBLE — do not ship it."""
    assert contract["unsupported_active_features"] == []


# ── enums ─────────────────────────────────────────────────────────────────────

def test_enums_cover_the_known_production_vocabulary(contract):
    e = contract["enums"]
    assert set(e["market_state"]["values"]) == {
        "Bull/Expand", "Bull/Compress", "Bull/Chop",
        "Bear/Expand", "Bear/Compress", "Bear/Chop"}
    assert set(e["session_key"]["values"]) == {
        "asia", "london", "lull", "newYork", "ny_pm", "outside"}
    assert set(e["structure_tag"]["values"]) == {"BOS", "CHoCH"}
    assert set(e["policy_regime"]["values"]) == {
        "LABEL", "STATE_ONLY", "DIRECTION_AWARE", "DISABLE"}


def test_outcome_enum_contains_the_production_live_blocks(contract):
    """The three block outcomes reachable under the deployed config."""
    outcomes = set(contract["enums"]["trade_outcome"]["values"])
    for required in ("STATE_BLOCKED", "COHORT_DISABLED", "REGIME_BLOCKED",
                     "INVALID", "UNFILLED", "OPEN", "NEWS_BLACKOUT",
                     "NEWS_FLATTEN", "NEWS_TOUCH_CANCEL"):
        assert required in outcomes, f"{required} missing from the scanned outcomes"


def test_win_loss_are_absent_from_the_scan_and_that_is_expected(contract):
    """WIN/LOSS/PROTECTION_EXIT are assigned from VARIABLES (`row["outcome"] =
    outcome`), not string literals, so the literal scanner cannot see them.

    This is asserted rather than silently tolerated: if a future refactor turns
    them into literals the scan will pick them up and this test will fail,
    prompting a deliberate update instead of a surprise.
    """
    scanned = set(contract["enums"]["trade_outcome"]["values"])
    assert "WIN" not in scanned and "LOSS" not in scanned
    # …but they MUST still reach Pine, via the live-sourced exit vocabulary.
    exited = set(contract["enums"]["engine_exited_outcome"]["values"])
    assert {"WIN", "LOSS", "PROTECTION_EXIT", "BE_EXIT", "NEWS_FLATTEN"} == exited


def test_ob_status_vocabulary_does_not_cover_the_block_outcomes(contract):
    """Documents a REAL production gap, so it cannot regress unnoticed.

    run_backtest._derive_ob_lifecycle_from_trade has no branch for the three
    block outcomes, so they fall through to 'unknown'. Measured on the committed
    golden run: 809 of 2080 order blocks (38.9%) land there, every one of them
    actually state_target_block. Pine therefore extends the vocabulary — see
    enums.ORACLE_STATUS_EXTENSIONS.

    If this test ever FAILS, production has gained proper statuses and the
    oracle extensions should be retired in favour of them.
    """
    statuses = set(contract["enums"]["ob_final_status"]["values"])
    assert "state_blocked" not in statuses
    assert "cohort_disabled" not in statuses
    assert "regime_blocked" not in statuses
    assert "unknown" in statuses
    ext = contract["oracle_status_extensions"]
    assert set(ext) == {"state_blocked", "cohort_disabled", "regime_blocked"}
    for name, meta in ext.items():
        assert meta["driver_status_would_be"] == "unknown"
        assert meta["justification"]


def test_unmapped_detection_flags_a_missing_pine_mapping(contract):
    """The rule that makes STEP 6 enforceable."""
    complete = {k: list(v["values"]) for k, v in contract["enums"].items()}
    assert enums_mod.unmapped_against(contract["enums"], complete) == {}

    incomplete = dict(complete)
    incomplete["market_state"] = ["Bull/Expand"]
    gaps = enums_mod.unmapped_against(contract["enums"], incomplete)
    assert "market_state" in gaps and "Bear/Chop" in gaps["market_state"]


# ── policy / cohorts ──────────────────────────────────────────────────────────

def test_include_disabled_override_neutralises_every_disable(contract):
    """With portfolio_include_disabled_cohorts=true, no cohort can block via
    DISABLE — so `portfolio_disabled` is unreachable and Pine must encode the
    EFFECTIVE table."""
    pp = contract["portfolio_policy"]
    assert pp["include_disabled_cohorts"] is True
    assert pp["neutralised_count"] > 0
    for row in pp["cohorts"]:
        assert row["effective_regime"] != "DISABLE"
    assert "DISABLE" not in pp["effective_blocking_regimes"]


def test_cohort_cells_are_accounted_for(contract):
    c = contract["cohorts"]
    assert c["actionable"] is True
    cells = sum(1 + len(x["state_overrides"] or {}) + len(x["elig_states"] or {})
                for x in c["cohorts"])
    assert c["addressable_cells"] == cells


# ── impact map ────────────────────────────────────────────────────────────────

def test_impact_map_covers_every_governed_file(contract):
    """A governed file with no impact-map entry falls to full revalidation. That
    is the safe default, but it should be a deliberate gap, not an oversight."""
    mapped = {p for e in load_map()["entries"] for p in e["production"]}
    governed = {f["path"] for f in contract["engine"]["governed_files"]}
    unmapped = sorted(governed - mapped)
    assert not unmapped, (
        "governed files with no impact-map entry (they will fail closed to "
        f"FULL_PARITY_REVALIDATION_REQUIRED): {unmapped}")


def test_impact_map_paths_all_exist(engine):
    missing = []
    for e in load_map()["entries"]:
        for rel in e["production"]:
            candidate = engine.lux_root / rel
            if not candidate.exists() and not (CT_ROOT / rel).exists():
                missing.append(rel)
    assert not missing, f"impact map names non-existent paths: {missing}"


def test_unmapped_governed_change_fails_closed():
    result = analyse(["strategy_core/some_new_module.py"], load_map())
    assert result["overall_category"] == "FULL_PARITY_REVALIDATION_REQUIRED"
    assert result["unmapped_governed_files"] == ["strategy_core/some_new_module.py"]


def test_out_of_scope_change_has_no_impact():
    result = analyse(["README.md", "Notes/whatever.md"], load_map())
    assert result["overall_category"] == "NO_ORACLE_IMPACT"


def test_algorithm_change_names_its_fixtures_and_stages():
    """order_blocks.py carries S2, S3, S4 and S5 surfaces, so a change there must
    name all of them plus their fixtures and tests."""
    result = analyse(["strategy_core/order_blocks.py"], load_map())
    assert result["overall_category"] == "ALGORITHM_PARITY_UPDATE"
    for stage in ("S2", "S3", "S4", "S5"):
        assert stage in result["stages"], f"{stage} missing from {result['stages']}"
    assert any(f.startswith("F-S3") for f in result["fixtures"]), result["fixtures"]
    assert any(f.startswith("F-S4") for f in result["fixtures"]), result["fixtures"]
    assert result["python_tests"], "an algorithm change must name its Python tests"


def test_stage_dependencies_declare_downstream_invalidation():
    """A change upstream must be traceable to everything it invalidates."""
    m = load_map()
    deps = m["stage_dependencies"]
    assert deps["S4"] == ["S1", "S3"]
    assert deps["S5"] == ["S1", "S2", "S3", "S4"]
    swing = next(e for e in m["entries"] if e["id"] == "swing-legs")
    assert "S4" in swing["invalidates_stages"]
    assert "S5" in swing["invalidates_stages"]


# ── committed artefacts ───────────────────────────────────────────────────────

def test_committed_contract_matches_a_fresh_extraction(contract):
    """The committed contract is derived state. If this fails, regenerate:
        python -m tools.oracle.extract_contract --write
    """
    if not DEFAULT_OUT.is_file():
        pytest.skip("no committed contract yet")
    assert DEFAULT_OUT.read_text(encoding="utf-8") == canonical_json(contract)


@pytest.mark.parametrize("name", [
    "tradingview_oracle_contract.schema.json",
    "tradingview_oracle_trace.schema.json",
    "tradingview_oracle_parity_manifest.schema.json",
    "tradingview_oracle_impact_map.json",
])
def test_contract_artifacts_are_valid_json(name):
    json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def test_schema_versions_agree_between_code_and_contract(contract):
    assert contract["contract_schema_version"] == fp.CONTRACT_SCHEMA_VERSION
    assert contract["trace_schema_version"] == fp.TRACE_SCHEMA_VERSION
    trace_schema = json.loads(
        (CONTRACTS / "tradingview_oracle_trace.schema.json").read_text(encoding="utf-8"))
    assert trace_schema["properties"]["header"]["properties"][
        "trace_schema_version"]["const"] == fp.TRACE_SCHEMA_VERSION
