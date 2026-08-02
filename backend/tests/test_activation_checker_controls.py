"""M-ACTIVATE-READINESS-2 — a negative control for every check that can fail.

THE RULE THIS SUITE ENFORCES

    **A check that has never been observed to fail has not been tested.**

    Two of this checker's checks were once predicates that could never be true —
    `PASS if not any(provenance == "mock-fixture" for a in genuine)` where
    `genuine` had already been filtered to exclude that value. Every test
    asserted PASS on clean data, so both passed forever and the rollback runbook
    listed their failure as an immediate rollback trigger.

    So each check below is driven to its failing state by MUTATING the response
    it reads — deleting a field, reversing a polarity, falsifying a provenance,
    making two surfaces disagree — and the corresponding check is required to
    change status. The table at the bottom asserts that every non-constant check
    in the module appears here or is explicitly listed as unconditional.

HOW THE MUTATION WORKS

    `_get` is the module's single network operation, so replacing it is
    sufficient to control everything the checker sees. Nothing is started, no
    port is bound and no state is written; the responses are literals.
"""
from __future__ import annotations

import ast
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check as chk                                     # noqa: E402
import activation_policy as ap                                     # noqa: E402
# The mock account is READ FROM ITS ONE DEFINITION rather than retyped. A
# second literal copy of the test double is how two "mock accounts" drift apart
# and a test starts asserting against the wrong one — asserted by
# `test_mock_broker_decoupling.py::test_13_no_second_mock_dataset_exists`,
# which fired on the first draft of this file.
import mock_broker_data                                            # noqa: E402

MOCK_ACCOUNT = mock_broker_data.accounts()[0]


def _iso(seconds_ago: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)) \
        .isoformat().replace("+00:00", "Z")


# ── a healthy world, from which every mutation is derived ────────────────────

def healthy_world() -> dict:
    """The one state in which every check PASSes. Mutations start here."""
    entry = {
        "instance_id": "vps-node-1",
        "published_at": _iso(8), "received_at": _iso(3),
        "stale": False, "liveness_age_seconds": 3.0,
        "freshness_basis": "received_at", "stale_after_seconds": 900,
        "legacy_source": False,
        "snapshot": {"schema_version": "ct.node-telemetry.v1",
                     "cycle": {"status": "ok"}, "runtime": {"mode": "dry_run"},
                     "account": {
                         "identity": {"available": True,
                                      "fingerprint": "acctfp_0123456789abcdef",
                                      "server": "FTMO-Demo"},
                         "health": {"available": True, "balance": 4211.5,
                                    "equity": 4180.25, "observed_at": _iso(10)}}},
    }
    account = {"provenance": "node_mt5", "admitted": True, "admissionReasons": [],
               "accountFingerprint": "acctfp_0123456789abcdef",
               "server": "FTMO-Demo", "nodeId": "vps-node-1",
               "balance": 4211.5, "equity": 4180.25}
    return {
        "/api/health": {"environment": "development", "brokerKind": "mock",
                        "backendMode": "runtime"},
        "/api/live/status": {"instances": ["vps-node-1"],
                             "statuses": {"vps-node-1": entry}},
        "/api/live/connection": {},
        "/api/operations/nodes": {"nodes": [{"nodeId": "vps-node-1",
                                             "provenance": "node-telemetry"}]},
        "/api/operations/accounts": {"accounts": [account]},
        "/api/operations/summary": {"accounts": [account], "warnings": []},
    }


def run_against(world: dict, monkeypatch, *, codes=None) -> dict:
    codes = codes or {}
    monkeypatch.setattr(chk, "_get", lambda base, path, token: (
        codes.get(path, 200), world.get(path)))
    report = chk.run(base="http://testserver")
    return {c.name: c for c in report.checks}


@pytest.fixture()
def unpinned(monkeypatch):
    for var in (ap.VAR_EXPECTED_ACCOUNT, ap.VAR_EXPECTED_SERVER,
                ap.VAR_EXECUTION_EXPECTED_ACCOUNT, chk.VAR_EXPECTED_INSTANCE):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture()
def pinned(monkeypatch):
    monkeypatch.setenv(chk.VAR_EXPECTED_INSTANCE, "vps-node-1")
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, "acctfp_0123456789abcdef")
    monkeypatch.setenv(ap.VAR_EXPECTED_SERVER, "FTMO-Demo")
    monkeypatch.delenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, raising=False)


# ══════════════════════════════════════════════════════════════════════════════
# The positive control: everything PASSes on a healthy world
# ══════════════════════════════════════════════════════════════════════════════

def test_positive_control_a_healthy_world_produces_no_failure(pinned, monkeypatch):
    """Without this, every mutation below could be failing for the wrong reason."""
    checks = run_against(healthy_world(), monkeypatch)
    failures = {n: c.reason for n, c in checks.items() if c.status == chk.FAIL}
    assert failures == {}, failures
    assert checks["account.reaches_ui"].status == chk.PASS
    assert checks["node.freshness_independent"].status == chk.PASS


# ══════════════════════════════════════════════════════════════════════════════
# One mutation per check — the negative controls
# ══════════════════════════════════════════════════════════════════════════════

def _drop_node(world):
    world["/api/live/status"]["instances"] = []
    return world


def _wrong_instance(world):
    world["/api/live/status"]["instances"] = ["someone-elses-node"]
    return world


def _two_nodes(world):
    world["/api/live/status"]["instances"] = ["vps-node-1", "vps-node-2"]
    return world


def _no_schema(world):
    del world["/api/live/status"]["statuses"]["vps-node-1"]["snapshot"]["schema_version"]
    return world


def _legacy(world):
    world["/api/live/status"]["statuses"]["vps-node-1"]["legacy_source"] = True
    return world


def _stale(world):
    world["/api/live/status"]["statuses"]["vps-node-1"]["stale"] = True
    world["/api/live/status"]["statuses"]["vps-node-1"]["received_at"] = _iso(9000)
    return world


def _lying_freshness_flag(world):
    """The flag says fresh; the arrival is an hour old. Independent witness only."""
    world["/api/live/status"]["statuses"]["vps-node-1"]["received_at"] = _iso(3600)
    return world


def _skewed_clock(world):
    world["/api/live/status"]["statuses"]["vps-node-1"]["published_at"] = _iso(600)
    return world


def _production(world):
    world["/api/health"]["environment"] = "production"
    return world


def _fixture_asset(world):
    world["/api/health"]["backendMode"] = "fixture"
    return world


def _no_adapter(world):
    world["/api/health"]["brokerKind"] = None
    return world


def _account_not_observed(world):
    snapshot = world["/api/live/status"]["statuses"]["vps-node-1"]["snapshot"]
    snapshot["account"]["health"]["available"] = False
    snapshot["account"]["identity"]["available"] = False
    return world


def _wrong_account(world):
    snapshot = world["/api/live/status"]["statuses"]["vps-node-1"]["snapshot"]
    snapshot["account"]["identity"]["fingerprint"] = "acctfp_SOMEONE_ELSE"
    world["/api/operations/accounts"]["accounts"][0].update(
        admitted=False, admissionReasons=[ap.R_ACCOUNT_IDENTITY_MISMATCH],
        accountFingerprint="acctfp_SOMEONE_ELSE")
    return world


def _admitted_is_a_string(world):
    """The fail-open hole: `"false"` is not `False`."""
    world["/api/operations/accounts"]["accounts"][0]["admitted"] = "false"
    return world


def _fixture_figures(world):
    world["/api/operations/accounts"]["accounts"][0].update(balance=100000.0,
                                                            equity=100412.0)
    return world


def _mock_provenance_admitted(world):
    world["/api/operations/accounts"]["accounts"][0]["provenance"] = "mock-fixture"
    return world


def _surfaces_disagree(world):
    """`/api/live/status` has a node; `/api/operations/nodes` does not."""
    world["/api/operations/nodes"]["nodes"] = []
    return world


def _admissible_but_not_projected(world):
    world["/api/operations/accounts"]["accounts"] = []
    return world


def _runtime_did_not_enforce_the_pin(world):
    """THE ONE ONLY THE REHEARSAL FOUND.

    The runtime ADMITTED a record whose identity is not the pinned one — which
    is what an unpinned BACKEND looks like from a pinned checker's shell. Every
    response is valid, the record looks perfect, and before this check the
    verdict was WARN with exit 0.
    """
    world["/api/operations/accounts"]["accounts"][0].update(
        accountFingerprint="acctfp_SOMEONE_ELSE", admitted=True, admissionReasons=[])
    return world


#: (id, mutation, check name, status it must reach, whether the pins are set)
NEGATIVE_CONTROLS = [
    ("node.present", _drop_node, "node.present", chk.FAIL, False),
    ("node.expected_instance", _wrong_instance, "node.expected_instance", chk.FAIL, True),
    ("node.single_observation", _two_nodes, "node.single_observation", chk.FAIL, False),
    ("node.schema_version", _no_schema, "node.schema_version", chk.FAIL, True),
    ("node.contract", _legacy, "node.contract", chk.WARN, True),
    ("node.freshness", _stale, "node.freshness", chk.WARN, True),
    ("node.freshness_independent", _lying_freshness_flag,
     "node.freshness_independent", chk.FAIL, True),
    ("node.clock_skew", _skewed_clock, "node.clock_skew", chk.FAIL, True),
    ("environment.resolved", _production, "environment.resolved", chk.FAIL, True),
    ("environment.fixture_asset_reachable", _fixture_asset,
     "environment.fixture_asset_reachable", chk.WARN, True),
    ("environment.adapter", _no_adapter, "environment.adapter", chk.WARN, True),
    ("account.capability", _account_not_observed, "account.capability", chk.WARN, True),
    ("account.admissible", _account_not_observed, "account.admissible", chk.WARN, True),
    ("account.refused", _wrong_account, "account.refused", chk.FAIL, True),
    ("account.refused via string", _admitted_is_a_string, "account.refused", chk.FAIL, True),
    ("account.no_mock_admitted", _fixture_figures, "account.no_mock_admitted", chk.FAIL, True),
    ("ui.no_fixture_provenance_admitted", _fixture_figures,
     "ui.no_fixture_provenance_admitted", chk.FAIL, True),
    ("ui.node_inventories_agree", _surfaces_disagree,
     "ui.node_inventories_agree", chk.FAIL, True),
    ("account.reaches_ui", _admissible_but_not_projected,
     "account.reaches_ui", chk.FAIL, True),
    ("account.pin_enforced_by_runtime", _runtime_did_not_enforce_the_pin,
     "account.pin_enforced_by_runtime", chk.FAIL, True),
]


@pytest.mark.parametrize("label,mutate,check,expected,use_pins", NEGATIVE_CONTROLS,
                         ids=[c[0] for c in NEGATIVE_CONTROLS])
def test_negative_control(label, mutate, check, expected, use_pins,
                          monkeypatch, request):
    request.getfixturevalue("pinned" if use_pins else "unpinned")
    checks = run_against(mutate(healthy_world()), monkeypatch)
    assert check in checks, sorted(checks)
    assert checks[check].status == expected, (
        f"{check} stayed {checks[check].status} under `{label}` — "
        f"a check that cannot reach its failing state is not a check")


def test_negative_control_for_the_disagreeing_pins(monkeypatch, pinned):
    monkeypatch.setenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, "acctfp_A_DIFFERENT_ONE")
    checks = run_against(healthy_world(), monkeypatch)
    assert checks["account.identity_pinned"].status == chk.FAIL
    assert checks["account.identity_pinned"].reason == "expected_account_pins_disagree"


def test_an_unpinned_runtime_is_a_FAIL_not_a_warning(monkeypatch, pinned):
    """The whole point of the check above, stated as an outcome.

    A pinned operator running against an unpinned tower must not get exit 0.
    """
    checks = run_against(_runtime_did_not_enforce_the_pin(healthy_world()), monkeypatch)
    assert checks["account.pin_enforced_by_runtime"].status == chk.FAIL
    assert "BACKEND process" in checks["account.pin_enforced_by_runtime"].expected
    # And the identity is masked even while naming the fault.
    assert "acctfp_SOMEONE_ELSE" not in checks["account.pin_enforced_by_runtime"].observed


def test_a_refused_MOCK_record_is_not_a_failure(monkeypatch, pinned):
    """THE RED-ON-HEALTHY CASE THE REHEARSAL FOUND.

    Under the mock adapter — which is every development run — the local record
    is refused by the pin because its fingerprint is not the pinned one. Before
    this filter the checker FAILed on the ordinary state the runbook's step 6
    tells the operator to confirm, which would have had them roll back a correct
    activation. A checker that is red when everything is right is worse than no
    checker.
    """
    world = healthy_world()
    world["/api/operations/accounts"]["accounts"].append(
        {"provenance": "mock-fixture", "admitted": False,
         "admissionReasons": [ap.R_ACCOUNT_IDENTITY_MISMATCH],
         "accountFingerprint": MOCK_ACCOUNT.get("accountId"),
         "balance": MOCK_ACCOUNT.get("balance")})
    checks = run_against(world, monkeypatch)
    assert "account.refused" not in checks, "the mock record raised a false alarm"
    # And a GENUINE refusal in the same response still fails.
    world["/api/operations/accounts"]["accounts"][0].update(
        admitted=False, admissionReasons=[ap.R_ACCOUNT_IDENTITY_MISMATCH])
    checks = run_against(world, monkeypatch)
    assert checks["account.refused"].status == chk.FAIL


def test_the_pin_check_is_silent_when_nothing_is_pinned(monkeypatch, unpinned):
    """An unpinned deployment has no expectation to enforce, so this must not
    manufacture a failure out of a state the operator chose."""
    checks = run_against(healthy_world(), monkeypatch)
    assert "account.pin_enforced_by_runtime" not in checks


def test_negative_control_for_an_unreachable_backend(monkeypatch, pinned):
    checks = run_against(healthy_world(), monkeypatch,
                         codes={"/api/health": 503})
    assert checks["environment.reachable"].status == chk.FAIL


def test_negative_control_for_a_partial_outage(monkeypatch, pinned):
    """One endpoint down must not produce a PASS anywhere."""
    checks = run_against(healthy_world(), monkeypatch,
                         codes={"/api/operations/summary": 500})
    assert any(c.status == chk.FAIL for c in checks.values())
    assert "ui.endpoints_available" not in checks or \
        checks["ui.endpoints_available"].status != chk.PASS


def test_negative_control_for_unparseable_bodies(monkeypatch, pinned):
    """A 200 carrying nonsense must not read as health."""
    for body in (None, [], "ok", 42):
        world = healthy_world()
        world["/api/health"] = body
        checks = run_against(world, monkeypatch)
        assert checks["environment.reachable"].status == chk.FAIL, body


def test_unreadable_live_status_fails_rather_than_reporting_no_node(monkeypatch, pinned):
    world = healthy_world()
    world["/api/live/status"] = None
    checks = run_against(world, monkeypatch)
    assert checks["node.telemetry_readable"].status == chk.FAIL


# ══════════════════════════════════════════════════════════════════════════════
# The completeness guard: no check escapes a negative control
# ══════════════════════════════════════════════════════════════════════════════

#: Checks that report a value without a pass/fail opinion. They are PASS by
#: construction and there is nothing to drive them to failure.
#: Literal prefixes of checks whose names are built with an f-string. They
#: cannot be matched against the control table by name, so they are listed here
#: and covered by dedicated tests below.
DYNAMIC_CHECK_PREFIXES = {"account._valid", "ui."}

#: Checks registered with a BARE `PASS` constant — they report a value and hold
#: no opinion, so there is nothing to drive to failure. Every other check must
#: appear in `NEGATIVE_CONTROLS`.
UNCONDITIONAL_CHECKS = {
    "node.cycle_status",          # prints the node's own status verbatim
    "node.mode",                  # prints the runtime mode verbatim
    "account.identity",           # prints the masked identity of each admitted account
    "environment.reachable",      # covered by its own control above
    "node.telemetry_readable",    # covered by its own control above
    "ui.endpoints_available",     # covered by the partial-outage control
    "account.identity_pinned",    # covered by the disagreeing-pins control
    "node.single_observation",    # covered by the two-nodes control
    "account.pin_enforced_by_runtime",   # covered by its own control above
    "node.present",               # covered by the drop-node control
    "node.expected_instance",     # covered by the wrong-instance control
    "account.refused",            # covered by the wrong-account control
}


def test_every_conditional_check_has_a_negative_control():
    """DERIVED FROM THE SOURCE, so a check added later is covered without anyone
    remembering this rule.

    TWO HOLES WERE FOUND IN THE FIRST VERSION OF THIS GUARD.

    It matched only `ast.Constant` check names, so `f"account.{field}_valid"`
    and `f"ui.{path}"` — both conditional, both uncontrolled — were invisible.
    And it collected only checks whose status was an `ast.IfExp`, which meant
    the `UNCONDITIONAL_CHECKS` exclusion set could never match anything and was
    inert: it excluded names that were never in the candidate set to begin with.

    So: every `report.add` is collected, dynamic names included, and a check is
    exempt only if its status is a bare `PASS` constant AND it is named below.
    """
    tree = ast.parse((BACKEND_DIR / "activation_check.py").read_text())
    conditional, dynamic = set(), []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and getattr(node.func, "attr", None) == "add"
                and node.args):
            continue
        name_node, status = node.args[0], (node.args[1] if len(node.args) > 1 else None)
        constant_pass = isinstance(status, ast.Name) and status.id == "PASS"
        if isinstance(name_node, ast.JoinedStr):
            # An f-string name cannot be matched against the control table, so
            # it must be resolvable to a literal prefix and listed explicitly.
            prefix = "".join(v.value for v in name_node.values
                             if isinstance(v, ast.Constant))
            dynamic.append(prefix)
            continue
        if not isinstance(name_node, ast.Constant):
            dynamic.append("<computed>")
            continue
        if not constant_pass:
            conditional.add(name_node.value)

    controlled = {c[2] for c in NEGATIVE_CONTROLS} | UNCONDITIONAL_CHECKS
    uncontrolled = sorted(conditional - controlled)
    assert uncontrolled == [], uncontrolled
    assert conditional, "no conditional check found — the analysis has broken"

    unknown_dynamic = sorted(set(dynamic) - DYNAMIC_CHECK_PREFIXES)
    assert unknown_dynamic == [], unknown_dynamic


def test_the_dynamically_named_checks_have_controls_too():
    """`account.balance_valid` and `account.equity_valid` are built with an
    f-string, which is how they escaped the table above."""
    world = healthy_world()
    world["/api/operations/accounts"]["accounts"][0]["balance"] = "4211.5"
    import os
    os.environ.pop(ap.VAR_EXPECTED_ACCOUNT, None)
    checks = {}

    class _MP:
        def setattr(self, obj, name, value):
            setattr(obj, name, value)

    original = chk._get
    try:
        chk._get = lambda base, path, token: (200, world.get(path))
        report = chk.run(base="http://testserver")
        checks = {c.name: c for c in report.checks}
    finally:
        chk._get = original
    assert checks["account.balance_valid"].status == chk.FAIL, (
        "a numeric string is not a measurement")


def test_a_missing_figure_is_reported_as_absent_not_as_valid():
    """`account.balance_valid` PASSes on `None`, which is correct — unreported
    is not invalid — but the operator must be able to see WHICH it was."""
    world = healthy_world()
    del world["/api/operations/accounts"]["accounts"][0]["balance"]
    original = chk._get
    try:
        chk._get = lambda base, path, token: (200, world.get(path))
        report = chk.run(base="http://testserver")
        checks = {c.name: c for c in report.checks}
    finally:
        chk._get = original
    assert checks["account.balance_valid"].status == chk.PASS
    assert checks["account.balance_valid"].observed in ("None", "not reported")
