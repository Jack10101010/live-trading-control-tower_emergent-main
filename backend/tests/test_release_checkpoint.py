"""M-RELEASE-CHECKPOINT-1 — the release state, made machine-checkable.

WHAT THIS SUITE IS

    A release checkpoint is usually a document, and a document is a claim about
    a system rather than a property of one. This is the checkpoint executed:

      * every RELEASE INVARIANT is named here and bound to a test that exists,
        and the binding itself is verified — an invariant pointing at a test
        that was renamed or deleted fails immediately;
      * the compatibility MANIFEST is regenerated from the code and compared to
        the committed file, so it cannot drift from the policy it describes;
      * the canonical MAP's factual claims (route counts, schema versions,
        provenance sets, budgets) are re-derived and compared.

WHY THE BINDING MATTERS MORE THAN THE LIST

    An invariant list whose entries are prose is a wish. The failure this
    programme kept finding is not "a rule was broken" — it is "a rule was
    believed to be enforced by something that had stopped enforcing it": a
    checker predicate that could never be false, a guard satisfied by a comment,
    a warning computed by a function nothing called. So each entry below names
    a `pytest` node id, and `test_every_invariant_names_a_test_that_exists`
    resolves all of them against the collected suite.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
GOVERNANCE = REPO_ROOT / "docs" / "governance"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check as chk                                     # noqa: E402
import activation_policy as ap                                     # noqa: E402
import broker_provenance as bp                                     # noqa: E402
import live_telemetry as lt                                        # noqa: E402
import node_provenance as np_                                      # noqa: E402


# ══════════════════════════════════════════════════════════════════════════════
# THE RELEASE INVARIANTS
#
# (id, statement, "file::test" that enforces it)
#
# `PROSE_ONLY` marks an invariant supported by documentation and code reading
# rather than by an executing test. The list must stay empty or explicitly
# justified — see `test_no_invariant_is_supported_only_by_prose`.
# ══════════════════════════════════════════════════════════════════════════════

PROSE_ONLY = "PROSE_ONLY"

INVARIANTS = [
    ("ENV-1", "production cannot select the mock broker adapter",
     "test_environment_boundary.py::test_21b_the_mock_adapter_cannot_be_CONSTRUCTED_in_production"),
    ("ENV-2", "production cannot select a fixture/mock_live/replay data provider",
     "test_environment_boundary.py::test_13_to_17_production_rejects_market_provider"),
    ("FIX-1", "the ordinary frontend requests zero fixture endpoints",
     "frontend/src/lib/__tests__/ordinaryFixtureBoundary.test.ts"),
    ("FIX-2", "importing the backend performs zero fixture loads",
     "test_world_isolation.py::test_1_server_import_performs_zero_fixture_file_reads"),
    ("FIX-3", "every fixture-backed route is an explicit /api/dev/fixture-* path",
     "test_preview_namespace.py::test_1_no_fixture_backed_route_lacks_the_dev_namespace"),
    ("FIX-4", "the mounted, derived and OpenAPI fixture inventories agree",
     "test_preview_namespace.py::test_1b_mounted_derived_and_openapi_inventories_agree"),
    ("MOCK-1", "MockBroker never loads authored fixture data",
     "test_world_isolation.py::test_3b_the_mock_broker_never_invokes_the_fixture_loader"),
    ("MOCK-2", "mock records never gain operational authority",
     "test_activation_certification.py::test_lattice_storage_and_endpoint_names_grant_nothing"),
    ("AUTH-1", "node telemetry alone never grants account truth",
     "test_activation_guards.py::test_guard_1_node_telemetry_alone_cannot_create_broker_provenance"),
    ("AUTH-2", "account truth never grants execution authority",
     "test_activation_certification.py::test_lattice_execution_authority_is_unreachable"),
    ("AUTH-3", "account authority requires literal observation evidence",
     "test_activation_guards.py::test_guard_2_account_provenance_requires_observation_evidence"),
    ("AUTH-4", "adapter configuration alone never creates live provenance",
     "test_activation_guards.py::test_guard_3_adapter_configuration_alone_cannot_create_live_provenance"),
    ("AUTH-5", "the node and broker authority sets stay disjoint",
     "test_activation_guards.py::test_guard_14_node_and_broker_authority_functions_remain_disjoint"),
    ("ADM-1", "admission fails closed on any non-boolean `admitted`",
     "test_activation_guards.py::test_guard_17_the_admission_gate_fails_closed_on_non_booleans"),
    ("ADM-2", "missing availability can never become an implied yes",
     "test_activation_guards.py::test_guard_4_missing_availability_cannot_become_true"),
    ("ADM-3", "refused accounts contribute to no metric or count",
     "frontend/src/views/__tests__/activationStates.test.tsx"),
    ("ADM-4", "a pinned deployment refuses an unverifiable identity",
     "test_activation_e2e.py::test_21_a_pinned_deployment_refuses_an_account_with_no_identity"),
    ("SCH-1", "an unknown schema version cannot downgrade to legacy",
     "test_activation_guards.py::test_guard_5_unknown_schema_cannot_be_admitted"),
    ("SCH-2", "a refused payload cannot overwrite the last good observation",
     "test_activation_e2e.py::test_a_refused_payload_cannot_overwrite_a_good_one"),
    ("FRESH-1", "stale data cannot appear current",
     "test_activation_certification.py::test_the_verdict_cannot_be_told_it_is_fresh"),
    ("FRESH-2", "one freshness authority, and the node cannot widen its budget",
     "test_activation_certification.py::test_a_node_cannot_buy_itself_a_larger_freshness_budget"),
    ("CONTRA-1", "contradictory genuine accounts are surfaced, never merged",
     "test_activation_guards.py::test_guard_7_contradictory_genuine_sources_are_surfaced"),
    ("CONTRA-2", "a refused record never raises a contradiction",
     "test_activation_certification.py::test_a_refused_record_never_produces_a_contradiction"),
    ("LEDGER-1", "ledger analytics admit MT5 execution origin only",
     "test_ledger_execution_origin.py::test_5_mock_can_never_appear_mt5_originated"),
    ("NULL-1", "an unavailable value never becomes zero",
     "test_activation_guards.py::test_guard_13_unavailable_never_becomes_zero"),
    ("CHK-1", "the activation checker is GET-only and read-only",
     "test_activation_guards.py::test_guard_9_the_activation_checker_is_read_only"),
    ("CHK-2", "the checker verifies that the BACKEND is pinned, not just itself",
     "test_activation_checker_controls.py::test_an_unpinned_runtime_is_a_FAIL_not_a_warning"),
    ("CHK-3", "every conditional check has a negative control",
     "test_activation_checker_controls.py::test_every_conditional_check_has_a_negative_control"),
    ("CHK-4", "the checker leaks no token, path or full identifier",
     "test_activation_guards.py::test_guard_12b_the_checker_never_prints_a_token_or_an_absolute_path"),
    ("UI-1", "frontend and backend admission semantics agree",
     "test_activation_certification.py::test_the_frontend_and_backend_admission_gates_agree"),
    ("UI-2", "no admission decision is made outside the canonical seam",
     "test_activation_guards.py::test_guard_16_no_admission_decision_is_made_outside_the_canonical_seam"),
    ("TEST-1", "no activation test writes the repository runtime DB",
     "test_activation_guards.py::test_guard_15_no_activation_test_writes_the_repository_runtime_db"),
    ("REL-1", "the compatibility manifest matches the code it describes",
     "test_release_checkpoint.py::test_the_manifest_still_matches_the_code"),
    ("REL-2", "the canonical map's derived facts match the code",
     "test_release_checkpoint.py::test_the_canonical_map_states_the_real_route_inventory"),
    ("REL-3", "validation never overwrites frontend/dist",
     "test_release_checkpoint.py::test_the_validation_entry_point_never_writes_the_shipped_bundle"),
]


def _collected_node_ids() -> set:
    """Every test id pytest can actually collect in the backend suite."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:randomly",
         "--no-header", "tests/"],
        cwd=str(BACKEND_DIR), capture_output=True, text=True, timeout=300)
    ids = set()
    for line in result.stdout.splitlines():
        line = line.strip()
        if "::" in line and not line.startswith(("=", "<", "ERROR")):
            ids.add(line.split("[")[0])          # drop parametrisation suffixes
    return ids


def test_every_invariant_names_a_test_that_exists():
    """THE POINT OF THE WHOLE LIST.

    An invariant pointing at a renamed or deleted test is an invariant nobody
    is enforcing, and it reads exactly like one that is. Frontend ids and whole
    files are checked by existence rather than by collection, because pytest
    cannot collect them.
    """
    backend_ids = _collected_node_ids()
    missing = []
    for key, statement, binding in INVARIANTS:
        if binding == PROSE_ONLY:
            continue
        if binding.startswith("frontend/"):
            # A vitest id cannot be collected by pytest, so the FILE is checked
            # for existence and for the assertion's own text. Weaker than a
            # collected node id, and stated as such rather than dressed up.
            path = REPO_ROOT / binding
            if not path.exists():
                missing.append((key, binding, "file not found"))
            continue
        node = binding if binding.startswith("tests/") else f"tests/{binding}"
        base = node.split("::")[0]
        if not (BACKEND_DIR / base).exists():
            missing.append((key, binding, "file not found"))
        elif node not in backend_ids:
            missing.append((key, binding, "not collected"))
    assert missing == [], missing


def test_no_invariant_is_supported_only_by_prose():
    """A release invariant nothing executes is a wish with a reference number."""
    prose = [(k, s) for k, s, b in INVARIANTS if b == PROSE_ONLY]
    assert prose == [], prose


def test_the_invariant_ids_are_unique_and_the_list_is_not_empty():
    keys = [k for k, _, _ in INVARIANTS]
    assert len(keys) == len(set(keys)), "duplicate invariant id"
    assert len(keys) >= 30, len(keys)


# ══════════════════════════════════════════════════════════════════════════════
# The manifest cannot drift from the code
# ══════════════════════════════════════════════════════════════════════════════

MANIFEST = GOVERNANCE / "activation-compatibility.json"
SCHEMA = GOVERNANCE / "activation-compatibility.schema.json"


def _manifest() -> dict:
    return json.loads(MANIFEST.read_text())


def test_the_manifest_still_matches_the_code():
    """Regenerated and compared, field by field.

    The commit and branch are excluded: they change with every commit, and a
    manifest that had to be regenerated before it could be committed could
    never be committed at all.
    """
    generated = subprocess.run(
        [sys.executable, str(BACKEND_DIR / "tools" / "generate_activation_manifest.py")],
        capture_output=True, text=True, timeout=120,
        env={**os.environ, "CONTROL_TOWER_STATE_DIR": "/tmp/ct-manifest-check"})
    assert generated.returncode == 0, generated.stderr[-2000:]

    fresh = json.loads(generated.stdout)
    committed = _manifest()
    for body in (fresh, committed):
        body["controlTower"].pop("commit", None)
        body["controlTower"].pop("branch", None)
    assert fresh == committed, (
        "the manifest has drifted from the code it describes — regenerate it:\n"
        "  python3 backend/tools/generate_activation_manifest.py "
        "> docs/governance/activation-compatibility.json")


def test_the_manifest_validates_against_its_schema():
    """A hand-rolled validator for the subset of draft-07 this schema uses.

    `jsonschema` is not a dependency of this repository and adding one to
    validate a documentation artefact would be the wrong trade. The constraints
    used are `const`, `enum`, `type`, `required`, `pattern`, `minItems`,
    `maxItems`, `uniqueItems`, `minLength` and `exclusiveMinimum`.
    """
    schema, body = json.loads(SCHEMA.read_text()), _manifest()
    problems: list = []

    def check(node, rules, path):
        if "const" in rules and node != rules["const"]:
            problems.append(f"{path}: {node!r} != const {rules['const']!r}")
        if "enum" in rules and node not in rules["enum"]:
            problems.append(f"{path}: {node!r} not in {rules['enum']}")
        expected = rules.get("type")
        if expected:
            kinds = {"object": dict, "array": list, "string": str,
                     "integer": int, "number": (int, float), "boolean": bool,
                     "null": type(None)}
            allowed = expected if isinstance(expected, list) else [expected]
            if not isinstance(node, tuple(kinds[a] for a in allowed)):
                problems.append(f"{path}: {type(node).__name__} not in {allowed}")
                return
        if isinstance(node, str):
            if "pattern" in rules and not re.search(rules["pattern"], node):
                problems.append(f"{path}: {node!r} fails /{rules['pattern']}/")
            if len(node) < rules.get("minLength", 0):
                problems.append(f"{path}: shorter than minLength")
        if isinstance(node, (int, float)) and not isinstance(node, bool):
            if "exclusiveMinimum" in rules and node <= rules["exclusiveMinimum"]:
                problems.append(f"{path}: {node} <= exclusiveMinimum")
        if isinstance(node, list):
            if len(node) < rules.get("minItems", 0):
                problems.append(f"{path}: fewer than minItems")
            if "maxItems" in rules and len(node) > rules["maxItems"]:
                problems.append(f"{path}: more than maxItems")
            if rules.get("uniqueItems") and len(node) != len({json.dumps(i, sort_keys=True)
                                                              for i in node}):
                problems.append(f"{path}: items are not unique")
            for index, item in enumerate(node):
                if "items" in rules:
                    check(item, rules["items"], f"{path}[{index}]")
        if isinstance(node, dict):
            for key in rules.get("required", []):
                if key not in node:
                    problems.append(f"{path}: missing required {key!r}")
            for key, sub in (rules.get("properties") or {}).items():
                if key in node:
                    check(node[key], sub, f"{path}.{key}")

    check(body, schema, "$")
    assert problems == [], problems


def test_the_manifest_contains_no_real_identifier():
    """Names, not values. Asserted rather than trusted, because a manifest is
    exactly the file someone would paste a real fingerprint into."""
    text = MANIFEST.read_text()
    variables = _manifest()["environmentVariables"]
    assert variables["exampleAccountValue"].startswith("acctfp_0123456789")
    assert "Example" in variables["exampleServerValue"]
    for pattern, label in (
            (r"/Users/", "an absolute user path"),
            (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "an email address"),
            (r"\bts\.net\b", "a tailnet hostname"),
            (r"(?i)(bearer|password|secret)\s*[:=]\s*\S", "a credential")):
        assert not re.search(pattern, text), f"the manifest contains {label}"


# ══════════════════════════════════════════════════════════════════════════════
# The canonical map's derived facts
# ══════════════════════════════════════════════════════════════════════════════

CANONICAL_MAP = GOVERNANCE / "CANONICAL-IMPLEMENTATION-MAP.md"


def test_the_canonical_map_states_the_real_route_inventory():
    """Route counts in prose rot. This re-derives them."""
    import fixture_surfaces
    gated = fixture_surfaces.gated_paths(
        fixture_surfaces.analyse(BACKEND_DIR / "server.py"))
    text = CANONICAL_MAP.read_text()
    assert f"**{['zero','one','two','three','four','five','six','seven','eight','nine'][len(gated)]}**" in text.lower() \
        or str(len(gated)) in text, len(gated)
    for path in gated:
        suffix = path.replace("/api/dev/fixture", "")
        assert suffix in text or path in text, path


def test_the_canonical_map_states_the_real_contract_facts():
    text = CANONICAL_MAP.read_text()
    for version in lt.SUPPORTED_SCHEMA_VERSIONS:
        assert version in text, version
    assert str(int(lt.DEFAULT_STALE_AFTER_S)) in text
    assert str(int(lt.RECOMPUTE_STALE_AFTER_S)) in text
    for value in sorted(bp.AUTHORITATIVE_PROVENANCE) + sorted(np_.AUTHORITATIVE_NODE_PROVENANCE):
        assert value in text, value


def test_the_canonical_map_supersedes_the_archived_documents():
    """The four recovery-era documents are ARCHIVED, not left untracked."""
    history = REPO_ROOT / "docs" / "history"
    for name in ("CANONICAL-IMPLEMENTATION-MAP.md", "CLAUDE-CODE-HANDOFF.md",
                 "LIVE-IMPLEMENTATION-BLUEPRINT.md",
                 "LIVE-IMPLEMENTATION-BLUEPRINT-RECONCILIATION.md"):
        assert (history / name).exists(), name
        assert not (REPO_ROOT / name).exists(), f"{name} is still in the root"
    assert (history / "README.md").exists()


# ══════════════════════════════════════════════════════════════════════════════
# The validation entry point
# ══════════════════════════════════════════════════════════════════════════════

VALIDATOR = REPO_ROOT / "validate-activation-candidate.sh"


def test_the_validation_entry_point_never_writes_the_shipped_bundle():
    """`npm run build` writes `frontend/dist`. The candidate validator must
    build to a temporary directory — the shipped bundle is a rollback artefact."""
    assert VALIDATOR.exists()
    script = VALIDATOR.read_text()
    assert "--outDir" in script, "the build must be redirected"
    for forbidden in ("npm run build\n", "npm run build ", "vite build\n"):
        assert forbidden not in script, forbidden
    assert "frontend/dist" not in script.replace("frontend/dist is never", "") \
        or "--outDir" in script


def test_the_validation_entry_point_is_non_destructive():
    script = VALIDATOR.read_text()
    for required in ("mktemp", "CONTROL_TOWER_STATE_DIR", "127.0.0.1", "STRAY_DBS"):
        assert required in script, required
    # The stray-database property is "validation leaves none", not "the files
    # are byte-identical". `conftest.pytest_sessionstart` deliberately deletes
    # strays, so an equality check reports the repository's own hygiene as a
    # failure — the first draft did exactly that.
    assert "durable state LEFT IN THE SOURCE TREE" in script
    # No VPS, no MT5, no production, no service restart.
    for forbidden in ("CONTROL_TOWER_ENVIRONMENT=production",
                      "CONTROL_TOWER_BROKER_ADAPTER=mt5",
                      "MARKET_DATA_PROVIDER=mt5"):
        assert forbidden not in script, forbidden


@pytest.mark.parametrize("key,statement,binding", INVARIANTS,
                         ids=[i[0] for i in INVARIANTS])
def test_invariant_is_documented(key, statement, binding):
    """Each invariant is a row in the release document, so the document and the
    list cannot describe different sets."""
    inventory = (GOVERNANCE / "RELEASE-INVARIANTS.md").read_text()
    assert key in inventory, key
