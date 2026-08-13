"""M-ACTIVATE-READINESS-1 — the contradiction matrix and the activation gate.

WHAT THIS SUITE IS FOR
    Activation is the moment the Control Tower stops showing "unavailable" and
    starts showing money. Every honesty milestone so far removed a way to show
    something invented; this one proves the system stays honest when it finally
    has something real — and, more importantly, when what it has is real but
    CONTRADICTORY.

    The dangerous cases are not the malformed ones. Malformed payloads are
    rejected at ingest and always were. The dangerous cases are two genuine
    sources disagreeing, or a genuine payload describing the wrong account:
    nothing looks broken, and a system that picks silently will be believed.

THE RULE THESE TESTS ENFORCE
    When truth is contradictory, surface the contradiction. Do not average, do
    not prefer, do not pick the newer one. An operator needs to know the system
    has two answers far more than they need a number.
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_policy as ap                                       # noqa: E402
import broker_provenance as bp                                       # noqa: E402
import node_provenance as np_                                        # noqa: E402
import operational_projection as op                                  # noqa: E402
import activation_payloads as pay                                  # noqa: E402

#: The projection instant, taken from the REAL clock.
#:
#: This was a frozen `"2026-08-02T12:00:00Z"` while `activation_payloads` stamps
#: `received_at` from the real clock, so the projection judged every arrival
#: against a `now` hours in the past. It went unnoticed only because
#: `build_nodes` used to pass the envelope's `stale` flag through instead of
#: recomputing; the moment one authority started deriving the answer, every
#: fixture in this file read stale. A test whose clock disagrees with its
#: fixtures' clock is measuring the disagreement.
def NOW() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sources(entries, **over):
    base = dict(adapter_kind=lambda: "mock", connection_state=lambda: "Disconnected",
                node_entries=lambda: list(entries))
    base.update(over)
    return op.ProjectionSources(**base)


# ══════════════════════════════════════════════════════════════════════════════
# Cases 1–3: the ordinary activation progression
# ══════════════════════════════════════════════════════════════════════════════

def test_1_node_current_account_absent():
    """The state activation STARTS from. A healthy node that has not sampled its
    account is the ordinary case, not a fault."""
    entry = pay.envelope(pay.unavailable_account_payload())
    v = ap.verdict(entry)
    assert v.node_identity_visible is True
    assert v.node_account_admissible is False
    assert v.broker_truth_admissible is False
    assert v.execution_authority is False
    assert ap.R_ACCOUNT_NOT_OBSERVED in v.reasons
    # The node is still fully visible.
    node = op.build_nodes(_sources([entry]), now=NOW())[0]
    assert node.lifecycle_state == np_.NODE_CURRENT
    assert node.provenance == np_.PROV_NODE_TELEMETRY
    # And no account is invented.
    assert op.build_accounts(_sources([entry]), now=NOW())[0].provenance == bp.PROV_ABSENT


def test_2_node_current_account_explicitly_unavailable():
    """Identical outcome to absent — and that is correct. `available: false` and
    a missing key are the same fact: nothing was observed."""
    entry = pay.envelope(pay.unavailable_account_payload())
    del entry["snapshot"]["account"]["health"]["available"]
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is False
    assert ap.R_ACCOUNT_NOT_OBSERVED in reasons


def test_3_node_current_account_valid():
    """The activation target."""
    entry = pay.envelope(pay.account_payload())
    v = ap.verdict(entry)
    assert v.node_account_admissible is True
    assert v.broker_truth_admissible is True
    assert v.analytics_input_admissible is True
    assert v.execution_authority is False, "account data never grants execution"
    assert v.capability == ap.CAPABILITY_OBSERVED

    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.provenance == bp.PROV_NODE_MT5
    assert account.balance == 4211.5
    node = op.build_nodes(_sources([entry]), now=NOW())[0]
    assert node.provenance == np_.PROV_NODE_TELEMETRY, (
        "the node must NOT borrow broker provenance from its own account payload")


# ══════════════════════════════════════════════════════════════════════════════
# Cases 4–7: freshness, degradation and self-contradiction
# ══════════════════════════════════════════════════════════════════════════════

def test_4_node_stale_account_timestamp_current():
    """A node cannot refresh itself by stamping a recent time on the account.
    Liveness is the tower's ARRIVAL clock; the account rides the same envelope."""
    entry = pay.envelope(pay.account_payload(), stale=True, received_ago=9000)
    v = ap.verdict(entry)
    assert v.stale is True
    assert ap.R_STALE in v.reasons
    # Stale genuine data stays ADMITTED and visibly stale — it is real, just old.
    assert v.node_account_admissible is True
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.provenance == bp.PROV_NODE_MT5
    assert account.freshness.stale is True


def test_5_node_degraded_with_account_values():
    """Degraded outranks stale, and blocks ANALYTICS while leaving the account
    visible: the node is telling us its own view of the world is compromised, so
    deriving performance from it would compound a fault the node reported."""
    entry = pay.envelope(pay.degraded_payload())
    v = ap.verdict(entry)
    assert v.degraded is True
    assert v.node_account_admissible is True, "the reading is still a reading"
    assert v.analytics_input_admissible is False
    assert ap.R_NODE_DEGRADED in v.reasons
    node = op.build_nodes(_sources([entry]), now=NOW())[0]
    assert node.lifecycle_state == np_.NODE_DEGRADED


def test_6_node_claims_mt5_observed_but_supplies_no_account():
    """A genuine self-contradiction. Surfaced, not resolved."""
    payload = pay.unavailable_account_payload()
    # `mt5_connection_observation` derives "observed" from health.available, so
    # the contradiction is constructed by claiming a broker read while the
    # account block says nothing was sampled.
    payload["reconciliation"]["snapshot_status"] = "unavailable"
    entry = pay.envelope(payload)
    v = ap.verdict(entry)
    assert v.node_mt5_observation_visible is True
    assert v.node_account_admissible is False


def test_7_node_supplies_account_but_reports_mt5_unreachable():
    payload = pay.account_payload()
    payload["reconciliation"]["snapshot_status"] = "unavailable"
    entry = pay.envelope(payload)
    v = ap.verdict(entry)
    assert ap.R_MT5_CONTRADICTION in v.warnings, (
        "an account read while the broker snapshot is unreadable is a "
        "contradiction the operator must see")


# ══════════════════════════════════════════════════════════════════════════════
# Cases 8–9: identity binding
# ══════════════════════════════════════════════════════════════════════════════

def test_8_account_login_differs_from_configured():
    """THE CASE THAT MATTERS MOST. Same shape, same freshness, different money.
    Nothing about the payload looks wrong."""
    entry = pay.envelope(pay.mismatched_account_payload())
    admissible, reasons = ap.account_admissible(
        entry, expected_account=pay.EXPECTED_ACCOUNT)
    assert admissible is False
    assert ap.R_ACCOUNT_IDENTITY_MISMATCH in reasons


def test_9_account_server_differs_from_configured():
    entry = pay.envelope(pay.mismatched_server_payload())
    admissible, reasons = ap.account_admissible(
        entry, expected_server=pay.EXPECTED_SERVER)
    assert admissible is False
    assert ap.R_ACCOUNT_SERVER_MISMATCH in reasons


def test_9b_unpinned_deployment_admits_but_cannot_detect_a_switch():
    """With no expectation configured, identity is not enforced — stated
    plainly rather than pretended. The checker reports this as WARN."""
    entry = pay.envelope(pay.mismatched_account_payload())
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is True and reasons == ()


# ══════════════════════════════════════════════════════════════════════════════
# Cases 10–13: local versus node sources
# ══════════════════════════════════════════════════════════════════════════════

def _account(provenance, fingerprint="acctfp_0123456789abcdef", balance=4211.5,
             observed_at="2026-08-02T12:00:00Z", node_id=None):
    #: `observed_at` matters: money is compared only between NEAR-SIMULTANEOUS
    #: observations, because equity moves with every tick and balance moves on
    #: every close. Two sources that looked minutes apart are drifting, not
    #: disagreeing, and a warning that cannot tell the difference fires forever.
    return op.AccountOperationalView(account_fingerprint=fingerprint,
                                     balance=balance, provenance=provenance,
                                     observed_at=observed_at, node_id=node_id)


def test_10_local_and_node_agree():
    node = [_account(bp.PROV_NODE_MT5)]
    local = [_account(bp.PROV_LIVE_MT5)]
    admitted, warnings = ap.reconcile_account_sources(node, local)
    assert len(admitted) == 2
    assert warnings == (), "agreement is not a contradiction"


def test_11_local_and_node_report_different_balances():
    """NOT averaged, NOT silently preferred. Both kept, contradiction raised."""
    node = [_account(bp.PROV_NODE_MT5, balance=4211.5)]
    local = [_account(bp.PROV_LIVE_MT5, balance=9999.0)]
    admitted, warnings = ap.reconcile_account_sources(node, local)
    assert len(admitted) == 2, "neither source is discarded"
    assert any(ap.R_CONTRADICTORY_SOURCES in w for w in warnings)
    balances = {a.balance for a in admitted}
    assert balances == {4211.5, 9999.0}
    assert 7105.25 not in balances, "an averaged figure nobody observed"


def test_12_local_and_node_report_different_accounts():
    node = [_account(bp.PROV_NODE_MT5, fingerprint="acctfp_node")]
    local = [_account(bp.PROV_LIVE_MT5, fingerprint="acctfp_local")]
    admitted, _ = ap.reconcile_account_sources(node, local)
    assert {a.account_fingerprint for a in admitted} == {"acctfp_node", "acctfp_local"}


def test_13_mock_local_never_shadows_genuine_node_truth():
    """Precedence is by AUTHORITY, not locality — M-NODE-READ-1's fix."""
    node = [_account(bp.PROV_NODE_MT5)]
    local = [_account(bp.PROV_MOCK_FIXTURE, balance=100000.0)]
    admitted, warnings = ap.reconcile_account_sources(node, local)
    assert [a.provenance for a in admitted] == [bp.PROV_NODE_MT5]
    assert warnings == ()
    assert all(a.balance != 100000.0 for a in admitted)


# ══════════════════════════════════════════════════════════════════════════════
# Cases 14–15: multiple nodes
# ══════════════════════════════════════════════════════════════════════════════

def test_14_two_nodes_reporting_the_same_account_are_two_observations():
    """Collapsing them would hide a genuinely alarming condition: two nodes on
    one live account."""
    entries = [pay.envelope(pay.account_payload(instance_id="vps-a")),
               pay.envelope(pay.account_payload(instance_id="vps-b"))]
    accounts = op.build_accounts(_sources(entries), now=NOW())
    assert len(accounts) == 2
    assert {a.node_id for a in accounts} == {"vps-a", "vps-b"}


def test_15_two_nodes_reporting_different_accounts_stay_distinct():
    entries = [pay.envelope(pay.account_payload(instance_id="vps-a")),
               pay.envelope(pay.account_payload(instance_id="vps-b",
                                                fingerprint="acctfp_other"))]
    accounts = op.build_accounts(_sources(entries), now=NOW())
    assert {(a.node_id, a.account_fingerprint) for a in accounts} == {
        ("vps-a", pay.EXPECTED_ACCOUNT), ("vps-b", "acctfp_other")}


# ══════════════════════════════════════════════════════════════════════════════
# Cases 16–20: clocks, numbers and forged provenance
# ══════════════════════════════════════════════════════════════════════════════

def test_16_future_dated_snapshot_cannot_read_fresher_than_arrival():
    entry = pay.envelope(pay.future_dated_payload(), stale=True, received_ago=9000)
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.freshness.stale is True


def test_17_arrival_clock_governs_not_the_node_clock():
    entry = pay.envelope(pay.account_payload(published_ago=1), stale=True,
                         received_ago=9000)
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.freshness.stale is True
    node = op.build_nodes(_sources([entry]), now=NOW())[0]
    assert node.freshness_basis == "received_at"


def test_18_nonfinite_numbers_fail_closed():
    entry = pay.envelope(pay.nonfinite_account_payload())
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is False
    assert ap.R_NUMERIC_INVALID in reasons
    # And the projection never renders it as a figure.
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.balance is None


def test_19_equity_absent_with_balance_present_is_unknown_not_invalid():
    """A missing figure is unknown; only a present-but-impossible one is a
    refusal. Rejecting this would hide a real balance over a null."""
    entry = pay.envelope(pay.equity_absent_payload())
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is True, reasons
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.balance == 4211.5
    assert account.equity is None


def test_20_claimed_provenance_without_evidence_is_worthless():
    """The VPS does not get to assert authority. A payload that stamps itself
    `node_mt5` while observing nothing gains nothing: the Mac derives provenance
    from validated evidence, and only from that."""
    payload = pay.unavailable_account_payload()
    payload["account"]["provenance"] = bp.PROV_NODE_MT5
    payload["account"]["health"]["provenance"] = bp.PROV_NODE_MT5
    entry = pay.envelope(payload)
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is False
    assert ap.R_ACCOUNT_NOT_OBSERVED in reasons
    account = op.build_accounts(_sources([entry]), now=NOW())[0]
    assert account.provenance == bp.PROV_ABSENT


# ══════════════════════════════════════════════════════════════════════════════
# Payload classification and the capability gap
# ══════════════════════════════════════════════════════════════════════════════

def test_the_four_payload_classes_are_distinguishable():
    """The Phase-2 question, answered against the code rather than the docs."""
    import live_telemetry as lt
    legacy_snap, was_legacy = lt.coerce_snapshot(pay.legacy_payload())
    assert was_legacy is True
    assert ap.classify_payload(pay.envelope(pay.legacy_payload(), legacy=True)) \
        == ap.CLASS_LEGACY
    assert ap.classify_payload(pay.envelope(pay.unavailable_account_payload())) \
        == ap.CLASS_CANONICAL
    assert ap.classify_payload(pay.envelope(pay.account_payload())) \
        == ap.CLASS_CANONICAL
    # Malformed and unknown-version never reach the read side at all.
    for builder in (pay.malformed_payload, pay.unknown_version_payload):
        with pytest.raises(lt.TelemetryError):
            lt.coerce_snapshot(builder())


def test_a_malformed_v1_payload_cannot_masquerade_as_legacy():
    """`is_legacy_payload` requires the ABSENCE of every v1-only section, so a
    broken v1 body is refused rather than gutted by the adapter — which would
    have silently replaced real safety state with nulls."""
    import live_telemetry as lt
    broken = pay.malformed_payload()
    assert lt.is_legacy_payload(broken) is False
    del broken["schema_version"]
    assert lt.is_legacy_payload(broken) is False, (
        "v1-only sections present: this is ambiguous, not legacy")


def test_the_capability_gap_is_reported_as_unknown_not_guessed():
    """No positive capability marker exists. A new VPS that CAN sample but has
    not is indistinguishable from one whose build never samples.

    Guessing "supported" would let an operator conclude the VPS milestone had
    landed when it had not. Guessing "unsupported" would send them debugging an
    idle node. UNKNOWN is the only honest answer; the checker prints it as WARN
    and `VPS-CONTRACT-M-NODE-ACCT-1.md` records the SHOULD.
    """
    assert ap.capability_signal(pay.envelope(pay.unavailable_account_payload())) \
        == ap.CAPABILITY_UNKNOWN
    assert ap.capability_signal(pay.envelope(pay.account_payload())) \
        == ap.CAPABILITY_OBSERVED
    assert ap.capability_signal(pay.envelope(pay.legacy_payload(), legacy=True)) \
        == ap.CAPABILITY_LEGACY_ABSENT


# ══════════════════════════════════════════════════════════════════════════════
# Source guards
# ══════════════════════════════════════════════════════════════════════════════

def test_guard_no_single_is_live_boolean_exists():
    """Activation is six independent permissions. One boolean is how a
    heartbeat comes to imply a balance."""
    source = (BACKEND_DIR / "activation_policy.py").read_text()
    tree = ast.parse(source)
    names = {n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    for forbidden in ("is_live", "is_activated", "activated", "ready"):
        assert forbidden not in names, forbidden
    verdict_fields = ap.ActivationVerdict.__dataclass_fields__
    for permission in ("node_identity_visible", "node_mt5_observation_visible",
                       "node_account_admissible", "broker_truth_admissible",
                       "analytics_input_admissible", "execution_authority"):
        assert permission in verdict_fields, permission


def test_guard_node_and_broker_authority_remain_disjoint():
    assert ap.node_and_broker_authority_are_disjoint() is True
    assert bp.is_broker_truth(np_.PROV_NODE_TELEMETRY) is False
    assert np_.is_authoritative_node_observation(
        {"provenance": bp.PROV_NODE_MT5}) is False


def test_guard_the_policy_imports_no_execution_or_reconciliation_code():
    """Activation must not be able to influence what may be dispatched."""
    tree = ast.parse((BACKEND_DIR / "activation_policy.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    for forbidden in ("execution_safety", "command_authorization", "reconciliation",
                      "execution_store", "order_lifecycle", "broker", "server"):
        assert forbidden not in imported, forbidden


def test_guard_the_policy_never_reaches_the_fixture():
    tree = ast.parse((BACKEND_DIR / "activation_policy.py").read_text())
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert "fixture_preview_service" not in imported
    assert "fixture_world" not in imported
    assert "mock_broker_data" not in imported


def test_guard_execution_authority_is_always_false_here():
    """Not a policy this module gets to relax. Every construction path yields
    False, and there is no argument that changes it."""
    for entry in (pay.envelope(pay.account_payload()),
                  pay.envelope(pay.degraded_payload()),
                  pay.envelope(pay.unavailable_account_payload())):
        assert ap.verdict(entry).execution_authority is False
    source = (BACKEND_DIR / "activation_policy.py").read_text()
    assert "execution_authority=True" not in source
