"""M-ACTIVATE-READINESS-1 — offline end-to-end activation, and the checker.

WHAT THIS PROVES
    The full receive path, exercised the way the VPS will exercise it: POST a
    synthetic snapshot to `/api/live/ingest`, then read every operational
    surface and assert what an operator would see. No VPS, no MT5, no
    production state — an isolated temporary state directory and the in-process
    test client.

    The sequence deliberately mirrors the activation runbook step for step, so
    a green run here means the runbook's expected states are the real ones.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check                                             # noqa: E402
import activation_payloads as pay                                   # noqa: E402
import activation_policy as ap                                      # noqa: E402
import broker_provenance as bp                                      # noqa: E402
import node_provenance as np_                                       # noqa: E402
import server                                                       # noqa: E402
from fastapi.testclient import TestClient                           # noqa: E402

client = TestClient(server.app)


@pytest.fixture()
def isolated_observations(tmp_path, monkeypatch):
    """A throwaway observation store. The repository runtime DB is never touched.

    That every ingesting test in this file takes it is asserted statically by
    `test_activation_guards.py::test_guard_15...`, and the existing HARDEN-3
    conftest pins `CONTROL_TOWER_STATE_DIR` for the whole session.
    """
    monkeypatch.setattr(server, "RUNTIME_DB_PATH", tmp_path / "runtime.db")
    prior = dict(server._LIVE_STATUS)
    server._LIVE_STATUS.clear()
    server._LIVE_GATE.clear()
    # TWO stores, both isolated. `_RUNTIME_OVERLAY` resolves its path through a
    # callable reading `RUNTIME_DB_PATH`, so repointing that name is sufficient
    # to send every durable write to the throwaway file — the repository's
    # `runtime.db` is never opened.
    try:
        yield
    finally:
        server._LIVE_STATUS.clear()
        server._LIVE_STATUS.update(prior)
        server._LIVE_GATE.clear()


def _ingest(payload: dict):
    return client.post("/api/live/ingest", json=payload)


def _accounts():
    return client.get("/api/operations/accounts").json()["accounts"]


def _nodes():
    return client.get("/api/operations/nodes").json()["nodes"]


def _admitted_accounts():
    return [a for a in _accounts() if bp.is_broker_truth(a["provenance"])]


# ══════════════════════════════════════════════════════════════════════════════
# The activation sequence, in the runbook's order
# ══════════════════════════════════════════════════════════════════════════════

def test_step_2_3_canonical_node_payload_becomes_a_visible_node(isolated_observations):
    assert _ingest(pay.canonical_payload()).status_code == 200
    nodes = _nodes()
    assert len(nodes) == 1
    node = nodes[0]
    assert node["nodeId"] == "vps-node-1"
    assert node["provenance"] == np_.PROV_NODE_TELEMETRY
    assert node["lifecycleState"] == np_.NODE_CURRENT
    # The node card must not borrow the tower's own state.
    for borrowed in ("adapter", "broker", "connectionState", "executionMode",
                     "reconciliationState", "accountFingerprintMasked"):
        assert node[borrowed] is None, borrowed


def test_step_4_5_unavailable_account_admits_no_account_card(isolated_observations):
    assert _ingest(pay.unavailable_account_payload()).status_code == 200
    assert _admitted_accounts() == [], "no account may be invented from a heartbeat"
    node = _nodes()[0]
    assert node["lifecycleState"] == np_.NODE_CURRENT, "the node is still healthy"
    assert node["mt5Observation"] is None, (
        "not sampling is not a disconnection — it is the ordinary case")


def test_step_6_7_valid_account_observation_becomes_node_mt5(isolated_observations):
    assert _ingest(pay.account_payload()).status_code == 200

    accounts = _admitted_accounts()
    assert len(accounts) == 1
    account = accounts[0]
    assert account["provenance"] == bp.PROV_NODE_MT5
    assert account["balance"] == 4211.5
    assert account["nodeId"] == "vps-node-1"
    # Unpublished figures stay null. `null is not zero` survives activation.
    for absent in ("margin", "marginLevel", "leverage", "unrealizedPnL",
                   "realizedPnLToday", "openRisk"):
        assert account[absent] is None, absent

    node = _nodes()[0]
    assert node["provenance"] == np_.PROV_NODE_TELEMETRY, (
        "the node must not gain broker provenance from its own account payload")
    assert node["mt5Observation"] == "observed"

    summary = client.get("/api/operations/summary").json()
    assert any(a["provenance"] == bp.PROV_NODE_MT5 for a in summary["accounts"])


def test_step_8_9_stale_and_degraded_stay_honest(isolated_observations):
    assert _ingest(pay.degraded_payload()).status_code == 200
    node = _nodes()[0]
    assert node["lifecycleState"] == np_.NODE_DEGRADED
    assert node["degradedReasons"], "the node's own failure reasons must be shown"
    # A degraded node is still a LIVE card — real data about real trouble.
    assert node["provenance"] == np_.PROV_NODE_TELEMETRY


def test_step_10_11_mock_local_account_cannot_shadow_node_truth(isolated_observations):
    """The mock adapter is active throughout this suite, so its fixture account
    exists the whole time. Genuine node truth must win."""
    assert _ingest(pay.account_payload()).status_code == 200
    accounts = _accounts()
    genuine = [a for a in accounts if bp.is_broker_truth(a["provenance"])]
    assert [a["provenance"] for a in genuine] == [bp.PROV_NODE_MT5]
    assert all(a["balance"] != 100000.0 for a in genuine), (
        "the mock's invented balance shadowed genuine node truth")


def test_step_12_13_removing_the_observation_returns_to_unavailable(isolated_observations):
    assert _ingest(pay.account_payload()).status_code == 200
    assert len(_admitted_accounts()) == 1

    # The observation is removed from BOTH stores, because every read merges the
    # durable snapshot table under the in-memory cache. Clearing only the cache
    # is how a "the node went silent" test silently keeps observing — the first
    # draft of this test did exactly that and passed for the wrong reason.
    #
    # Note this is REMOVAL, not silence. A node that merely stops publishing
    # keeps its last snapshot and goes visibly STALE (test 4) — stale genuine
    # data is still genuine. Only removal returns the surface to unavailable.
    server._LIVE_STATUS.clear()
    server._RUNTIME_OVERLAY.clear(server._LIVE_SNAPSHOT_KIND)
    accounts = _accounts()
    assert [a for a in accounts if bp.is_broker_truth(a["provenance"])] == [], (
        "with the genuine source gone the UI must return to unavailable, "
        "NOT fall back to the mock record")
    assert _nodes()[0]["provenance"] == np_.PROV_ABSENT


# ══════════════════════════════════════════════════════════════════════════════
# Identity pinning — the wrong account is refused END TO END
# ══════════════════════════════════════════════════════════════════════════════

def test_a_pinned_deployment_refuses_a_genuine_reading_of_the_wrong_account(
        isolated_observations, monkeypatch):
    """THE CONDITION THIS MILESTONE EXISTS FOR.

    The payload is perfect: canonical schema, fresh, healthy node, a real MT5
    terminal read. It is simply not this deployment's account. Provenance stays
    `node_mt5` — the observation IS genuine and relabelling it would be a lie
    about where it came from — and `admitted` goes false with a named reason.
    """
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    assert _ingest(pay.mismatched_account_payload()).status_code == 200

    accounts = _accounts()
    relayed = [a for a in accounts if a["provenance"] == bp.PROV_NODE_MT5]
    assert len(relayed) == 1, "the record is REPORTED, not hidden"
    assert relayed[0]["admitted"] is False
    assert ap.R_ACCOUNT_IDENTITY_MISMATCH in relayed[0]["admissionReasons"]
    # The balance still travels: an operator must be able to see WHAT is being
    # reported in order to recognise which account it is.
    assert relayed[0]["balance"] == 4211.5


def test_the_same_payload_is_admitted_when_the_pin_matches(isolated_observations,
                                                            monkeypatch):
    """The other half of the proof — otherwise the test above would pass with a
    gate that refuses everything."""
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    monkeypatch.setenv(ap.VAR_EXPECTED_SERVER, pay.EXPECTED_SERVER)
    assert _ingest(pay.account_payload()).status_code == 200
    admitted = _admitted_accounts()
    assert len(admitted) == 1
    assert admitted[0]["admitted"] is True
    assert admitted[0]["admissionReasons"] == []


def test_an_unpinned_deployment_admits_and_does_not_pretend_to_have_checked(
        isolated_observations, monkeypatch):
    monkeypatch.delenv(ap.VAR_EXPECTED_ACCOUNT, raising=False)
    monkeypatch.delenv(ap.VAR_EXPECTED_SERVER, raising=False)
    assert _ingest(pay.mismatched_account_payload()).status_code == 200
    # Admitted, because nothing was checked. The CHECKER is what says so out
    # loud, as WARN — see `account.identity_pinned`.
    assert len(_admitted_accounts()) == 1


# ══════════════════════════════════════════════════════════════════════════════
# Contradiction — two genuine sources, surfaced not resolved
# ══════════════════════════════════════════════════════════════════════════════

def test_two_nodes_reporting_the_same_account_differently_raise_a_warning(
        isolated_observations):
    """Matrix case 14. Both readings are genuine and both are kept: averaging
    would invent a third figure nobody observed."""
    assert _ingest(pay.account_payload(instance_id="vps-node-1")).status_code == 200
    assert _ingest(pay.account_payload(instance_id="vps-node-2",
                                       balance=9999.0)).status_code == 200

    admitted = _admitted_accounts()
    assert len(admitted) == 2, "neither reading is discarded"
    assert {a["balance"] for a in admitted} == {4211.5, 9999.0}

    warnings = client.get("/api/operations/summary").json()["warnings"]
    contradictions = [w for w in warnings if ap.R_CONTRADICTORY_SOURCES in w]
    assert contradictions, warnings
    assert "vps-node-1,vps-node-2" in contradictions[0], (
        "the warning must name WHICH sources disagree")


def test_14b_samples_taken_far_apart_are_drift_not_disagreement(isolated_observations):
    """The false positive that would have made the warning worthless.

    Balance moves whenever a trade closes and equity moves on every tick that
    moves an open position. Two observers who looked minutes apart will report
    different figures on a perfectly healthy deployment, and a warning that
    fires on that fires forever — which is the same as not existing.
    """
    assert _ingest(pay.account_payload(instance_id="vps-node-1")).status_code == 200
    far = pay.account_payload(instance_id="vps-node-2", balance=9999.0)
    far["account"]["health"]["observed_at"] = pay.iso(3600)     # an hour earlier
    assert _ingest(far).status_code == 200

    assert len(_admitted_accounts()) == 2, "both readings are still shown"
    warnings = client.get("/api/operations/summary").json()["warnings"]
    assert [w for w in warnings if ap.R_CONTRADICTORY_SOURCES in w] == [], warnings


def test_21_a_pinned_deployment_refuses_an_account_with_no_identity(
        isolated_observations, monkeypatch):
    """THE HOLE AN INDEPENDENT REVIEW FOUND.

    The pin originally read `if expected and fingerprint and fingerprint != expected`,
    so a node publishing `health.available: true` with `identity.available: false`
    delivered real balances into a fully pinned deployment with `admitted: True`
    and no reasons at all. The condition the pin exists to catch, defeated by
    omitting a block.
    """
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    payload = pay.canonical_payload(account_health=True, account_identity=False)
    assert _ingest(payload).status_code == 200

    relayed = [a for a in _accounts() if a["provenance"] == bp.PROV_NODE_MT5]
    assert len(relayed) == 1, "the record is reported, not hidden"
    assert relayed[0]["admitted"] is False
    assert ap.R_ACCOUNT_IDENTITY_UNVERIFIABLE in relayed[0]["admissionReasons"]
    assert _admitted_accounts() == [] or all(
        a["admitted"] is False for a in _admitted_accounts())


def test_two_nodes_reporting_the_same_account_identically_raise_nothing(
        isolated_observations):
    """Agreement is not a contradiction. A warning that fires on agreement is a
    warning an operator learns to ignore."""
    assert _ingest(pay.account_payload(instance_id="vps-node-1")).status_code == 200
    assert _ingest(pay.account_payload(instance_id="vps-node-2")).status_code == 200
    warnings = client.get("/api/operations/summary").json()["warnings"]
    assert [w for w in warnings if ap.R_CONTRADICTORY_SOURCES in w] == []


def test_two_nodes_reporting_DIFFERENT_accounts_raise_nothing(isolated_observations):
    """Matrix case 15. Two nodes on two accounts is a legitimate deployment, not
    a disagreement — the grouping is by account, not by node count."""
    assert _ingest(pay.account_payload(instance_id="vps-node-1")).status_code == 200
    assert _ingest(pay.account_payload(instance_id="vps-node-2",
                                       fingerprint="acctfp_fedcba9876543210",
                                       balance=9999.0)).status_code == 200
    warnings = client.get("/api/operations/summary").json()["warnings"]
    assert [w for w in warnings if ap.R_CONTRADICTORY_SOURCES in w] == []


def test_a_refused_account_cannot_raise_a_contradiction(isolated_observations,
                                                         monkeypatch):
    """A rejected record disagreeing with an admitted one is not a contradiction
    — it is the gate working. Counting it would produce an alarming warning
    every time the pin did its job."""
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    monkeypatch.setenv(ap.VAR_EXPECTED_SERVER, pay.EXPECTED_SERVER)
    assert _ingest(pay.account_payload(instance_id="vps-node-1")).status_code == 200
    # Same account fingerprint, DIFFERENT server, and the server is pinned — so
    # node 2 is refused and its disagreeing balance must not become a warning.
    assert _ingest(pay.mismatched_server_payload(
        instance_id="vps-node-2", balance=9999.0)).status_code == 200
    warnings = client.get("/api/operations/summary").json()["warnings"]
    assert [w for w in warnings if ap.R_CONTRADICTORY_SOURCES in w] == []


# ══════════════════════════════════════════════════════════════════════════════
# Ingest refuses what it should
# ══════════════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize("builder,expected", [
    (pay.malformed_payload, 400),
    (pay.unknown_version_payload, 400),
])
def test_malformed_and_unknown_version_are_refused_at_ingest(
        builder, expected, isolated_observations):
    response = _ingest(builder())
    assert response.status_code == expected, response.text
    assert _nodes()[0]["provenance"] == np_.PROV_ABSENT, (
        "a refused payload must not become a node")


def test_a_refused_payload_cannot_overwrite_a_good_one(isolated_observations):
    """The property that makes a broken publisher degrade to visible staleness
    rather than to plausible-looking wrong state."""
    assert _ingest(pay.account_payload()).status_code == 200
    assert _ingest(pay.malformed_payload()).status_code == 400
    assert len(_admitted_accounts()) == 1, "the good observation survived"


def test_nonfinite_numbers_never_reach_a_surface(isolated_observations):
    """`Infinity` is not JSON, but Python's own encoder emits it by default and
    Python's own decoder accepts it — so a Python publisher can put a non-finite
    float on the wire without any library complaining. It is sent as a raw body
    here for exactly that reason: `json=` would have refused to encode it and
    the test would have proved nothing about the server."""
    body = json.dumps(pay.nonfinite_account_payload())          # allow_nan=True
    assert "Infinity" in body, "the condition under test must survive encoding"
    response = client.post("/api/live/ingest", content=body,
                           headers={"Content-Type": "application/json"})

    # The behaviour is STRONGER than "the surface shows null": the whole
    # snapshot is refused at the boundary, named field and all. I expected
    # per-field coercion and the server was stricter — recorded here rather than
    # relaxed, because a payload containing an impossible number is a publisher
    # fault, and accepting the rest of it would be trusting the same producer.
    assert response.status_code == 400, response.text
    assert "numeric_invalid" in response.text
    assert "account.health.balance" in response.text, (
        "a refusal an operator cannot locate is barely better than silence")
    assert _admitted_accounts() == []


# ══════════════════════════════════════════════════════════════════════════════
# The checker
# ══════════════════════════════════════════════════════════════════════════════

def test_the_checker_reports_fail_when_no_node_has_published(isolated_observations,
                                                             monkeypatch):
    """Run against the in-process app rather than a socket: same code path,
    no port, no risk of hitting a real tower."""
    monkeypatch.setattr(activation_check, "_get",
                        lambda base, path, token: (200, client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    assert report.verdict == activation_check.FAIL
    assert any(c.reason == "no_node_has_published" for c in report.failed)


def test_the_checker_passes_the_environment_and_node_checks(isolated_observations,
                                                            monkeypatch):
    _ingest(pay.account_payload())
    monkeypatch.setattr(activation_check, "_get",
                        lambda base, path, token: (200, client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    by_name = {c.name: c for c in report.checks}
    assert by_name["environment.resolved"].status == activation_check.PASS
    # Presence of the dev asset is a WARN, not a FAIL: it is readable in every
    # developer checkout, including the one an activation is run from.
    assert by_name["environment.fixture_asset_reachable"].status == activation_check.WARN
    assert by_name["ui.no_fixture_provenance_admitted"].status == activation_check.PASS
    assert by_name["node.contract"].status == activation_check.PASS
    assert by_name["ui.node_inventories_agree"].status == activation_check.PASS
    assert by_name["account.reaches_ui"].status == activation_check.PASS
    # Unpinned expectations are WARN, never PASS: an unpinned deployment cannot
    # detect an account switch, and saying PASS would imply it could.
    assert by_name["account.identity_pinned"].status == activation_check.WARN


def test_the_checker_FAILS_on_a_refused_account(isolated_observations, monkeypatch):
    """The loudest condition the checker can find. FAIL, not WARN: an operator
    who reads past this is looking at an account that is not theirs."""
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    _ingest(pay.mismatched_account_payload())
    monkeypatch.setattr(activation_check, "_get",
                        lambda base, path, token: (200, client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    refusals = [c for c in report.failed if c.name == "account.refused"]
    assert refusals, [c.name for c in report.checks]
    assert ap.R_ACCOUNT_IDENTITY_MISMATCH in refusals[0].reason
    assert report.verdict == activation_check.FAIL
    # The identifier is masked even while naming the fault.
    assert "acctfp_SOMEONE_ELSES" not in refusals[0].observed


def test_the_checker_CAN_fail_its_anti_fixture_checks(monkeypatch):
    """THE TEST THAT SHOULD HAVE EXISTED FIRST.

    `account.no_mock_admitted` and `ui.no_fixture_provenance_admitted` were both
    written as `PASS if not any(... == "mock-fixture" for a in genuine)` where
    `genuine` had already been filtered to exclude `mock-fixture`. The predicate
    was unsatisfiable: neither check could FAIL, while the rollback runbook
    listed both failures as IMMEDIATE rollback triggers.

    Asserting PASS on clean data cannot catch that — only feeding contamination
    and demanding a FAIL can. Every check whose whole purpose is to fail needs
    one of these.
    """
    # NOTE the provenance. A `mock-fixture` record cannot be admitted by
    # construction — asking whether one was is the unanswerable question the
    # original check asked. The answerable one is whether the fixture's invented
    # FIGURES reached a record that passed the gate, so the contamination is
    # modelled as exactly that: laundered figures under a genuine provenance.
    contaminated = {"accounts": [
        {"provenance": bp.PROV_NODE_MT5, "admitted": True,
         "balance": 100000.0, "equity": 100412.0, "accountFingerprint": "acct_fixture_1"},
    ]}
    monkeypatch.setattr(activation_check, "_get", lambda base, path, token: (
        200, contaminated if "accounts" in path else client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    by_name = {c.name: c for c in report.checks}
    assert by_name["account.no_mock_admitted"].status == activation_check.FAIL
    assert by_name["ui.no_fixture_provenance_admitted"].status == activation_check.FAIL


def test_the_checker_flags_the_fixture_sentinel_whatever_provenance_it_wears():
    """The invented 100000 / 100412 are what an operator would believe. A record
    carrying them past the UI gate is contamination even if it has managed to
    acquire a respectable provenance on the way."""
    laundered = [{"provenance": bp.PROV_NODE_MT5, "admitted": True,
                  "balance": 100000.0, "equity": 100412.0}]
    report = activation_check.Report()
    import unittest.mock as mock
    with mock.patch.object(activation_check, "_get",
                           lambda base, path, token: (200, {"accounts": laundered,
                                                            "nodes": []})):
        activation_check.check_ui_contract(report, "http://testserver", None)
    check = {c.name: c for c in report.checks}["ui.no_fixture_provenance_admitted"]
    assert check.status == activation_check.FAIL
    assert "100000" in check.observed


def test_the_checker_refuses_a_non_http_base():
    """`urlopen` accepts `file://`, and the bearer token goes to whatever host is
    named. A mistyped `--base` must not be how the credential leaves."""
    for bad in ("file:///etc/passwd", "ftp://host/x", "127.0.0.1:8000", ""):
        with pytest.raises(ValueError):
            activation_check._safe_base(bad)
    assert activation_check._safe_base("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"


def test_the_checker_FAILS_when_the_two_account_pins_disagree(isolated_observations,
                                                               monkeypatch):
    """Two environment variables name the expected account. Set to different
    values, the deployment is pinned twice to two different accounts."""
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, pay.EXPECTED_ACCOUNT)
    monkeypatch.setenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, "acctfp_SOMETHING_ELSE")
    _ingest(pay.account_payload())
    monkeypatch.setattr(activation_check, "_get",
                        lambda base, path, token: (200, client.get(path).json()))
    report = activation_check.run(base="http://testserver")
    assert any(c.reason == "expected_account_pins_disagree" for c in report.failed)


def test_the_execution_pin_is_used_when_the_activation_pin_is_unset(monkeypatch):
    """One fact, two names. A deployment that pinned execution must not find the
    admission gate silently unpinned."""
    monkeypatch.delenv(ap.VAR_EXPECTED_ACCOUNT, raising=False)
    monkeypatch.setenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, "acctfp_from_execution")
    assert ap.expected_identity()[0] == "acctfp_from_execution"
    assert ap.expected_account_pins_disagree() is False


def test_the_checker_FAILS_on_clock_skew_beyond_the_budget():
    """Matrix row 17, which had no test until an independent review noticed the
    document claiming "each with a test" and this one having none.

    Skew does not make the data wrong; it makes every freshness VERDICT
    unreliable, which is worse — the surface keeps saying `current` and nobody
    can tell whether it is.
    """
    def entry(skew_s):
        return {"instances": ["vps-node-1"], "statuses": {"vps-node-1": {
            "published_at": pay.iso(skew_s), "received_at": pay.iso(0),
            "stale": False, "liveness_age_seconds": 1.0,
            "freshness_basis": "received_at", "stale_after_seconds": 900,
            "legacy_source": False,
            "snapshot": {"schema_version": "ct.node-telemetry.v1",
                         "cycle": {"status": "ok"}, "runtime": {"mode": "dry_run"}},
        }}}

    for skew, expected in ((5.0, activation_check.PASS),
                           (activation_check.MAX_CLOCK_SKEW_S + 80, activation_check.FAIL)):
        report = activation_check.Report()
        import unittest.mock as mock
        with mock.patch.object(activation_check, "_get",
                               lambda base, path, token, _s=skew: (200, entry(_s))):
            activation_check.check_node(report, "http://testserver", None, "vps-node-1")
        check = {c.name: c for c in report.checks}["node.clock_skew"]
        assert check.status == expected, (skew, check)


def test_the_checker_masks_identifiers():
    assert activation_check._mask("acctfp_0123456789abcdef") == "acctfp_0…cdef"
    assert activation_check._mask("short") == "…"
    assert activation_check._mask(None) == ""


def test_the_checker_exit_code_is_non_zero_only_on_fail():
    report = activation_check.Report()
    report.add("a", activation_check.PASS)
    assert report.verdict == activation_check.PASS
    report.add("b", activation_check.WARN)
    assert report.verdict == activation_check.WARN
    report.add("c", activation_check.FAIL)
    assert report.verdict == activation_check.FAIL


# ══════════════════════════════════════════════════════════════════════════════
# Isolation
#
# The checker's own guards (read-only, no execution imports, no mutating verb,
# nothing sensitive printed) live in `test_activation_guards.py` with the other
# thirteen, so all fifteen are read in one place.
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_the_repository_runtime_db_is_never_written(isolated_observations):
    repo_db = BACKEND_DIR / "runtime.db"
    before = repo_db.stat().st_mtime if repo_db.exists() else None
    _ingest(pay.canonical_payload())
    after = repo_db.stat().st_mtime if repo_db.exists() else None
    assert before == after, "an activation test wrote the repository runtime DB"
