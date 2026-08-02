"""M-ACTIVATE-READINESS-2 — the adversarial certification of the activation gate.

WHY A SECOND SUITE RATHER THAN MORE OF THE FIRST

    The M-ACTIVATE-READINESS-1 suites prove the gate does the right thing on the
    conditions it was designed against. This one attacks it: table-driven
    lattices, hostile inputs per field, and a NEGATIVE CONTROL for every check
    whose only purpose is to fail.

    That distinction earned its place. Two of the original checker's checks were
    predicates that could never be true, and every test asserted PASS on clean
    data, so nothing noticed. **A check that has never been observed to fail has
    not been tested.**

WHAT THIS SUITE FOUND, AND WHAT IT CHANGED

    * `_finite_non_negative` REFUSED a negative balance. Negative equity is a
      real MT5 state after a stop-out gap, and refusing it blanked the whole
      account — inventing "nothing is known" out of "something alarming is
      known". Worse, the ingest boundary already rejects NaN/Infinity, so the
      non-negative clause's only reachable effect WAS that refusal.
    * The frontend and checker admission gates read `admitted === false` and
      `is not False`, so `"false"`, `0` and `{}` were all admitted — a contract
      violation waved through precisely because it was malformed.
    * `verdict()` trusted `entry["stale"]` while the projection recomputed the
      age from the arrival clock. Two authorities for one fact.
    * Two pins both unverifiable emitted the same reason code twice.
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for _p in (str(REPO_ROOT), str(BACKEND_DIR), str(BACKEND_DIR / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import activation_check                                            # noqa: E402
import activation_payloads as pay                                  # noqa: E402
import activation_policy as ap                                     # noqa: E402
import broker_provenance as bp                                     # noqa: E402
import node_provenance as np_                                      # noqa: E402
import operational_projection as op                                # noqa: E402


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _sources(entries, **over):
    base = dict(adapter_kind=lambda: "mock", connection_state=lambda: "Disconnected",
                node_entries=lambda: list(entries))
    base.update(over)
    return op.ProjectionSources(**base)


def _view(provenance, *, fingerprint="acctfp_A", balance=100.0, admitted=True,
          node_id=None, observed_at="2026-08-02T12:00:00Z", server="S", equity=None):
    return op.AccountOperationalView(
        account_fingerprint=fingerprint, balance=balance, equity=equity,
        provenance=provenance, admitted=admitted, node_id=node_id,
        observed_at=observed_at, server=server)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — the authority lattice, as a table
# ══════════════════════════════════════════════════════════════════════════════

#: (description, the thing offered as evidence, the authority it must NOT grant)
FORBIDDEN_PROMOTIONS = [
    ("adapter kind alone → live provenance",
     lambda: bp.for_local_adapter("mt5", observed=False), bp.PROV_LIVE_MT5),
    ("an unknown adapter kind → any broker truth",
     lambda: bp.for_local_adapter("node", observed=True), bp.PROV_NODE_MT5),
    ("no observation → node broker truth",
     lambda: bp.for_node_observation(observed=False), bp.PROV_NODE_MT5),
    ("node telemetry → broker truth",
     lambda: np_.PROV_NODE_TELEMETRY if bp.is_broker_truth(np_.PROV_NODE_TELEMETRY)
     else bp.PROV_ABSENT, np_.PROV_NODE_TELEMETRY),
    ("fixture node → node authority",
     lambda: np_.PROV_FIXTURE_NODE if np_.is_authoritative_node_observation(
         {"provenance": np_.PROV_FIXTURE_NODE}) else np_.PROV_ABSENT,
     np_.PROV_FIXTURE_NODE),
]


@pytest.mark.parametrize("label,produce,forbidden", FORBIDDEN_PROMOTIONS,
                         ids=[c[0] for c in FORBIDDEN_PROMOTIONS])
def test_lattice_has_no_forbidden_promotion(label, produce, forbidden):
    """Every edge the lattice must NOT contain, named and executed.

    Written as a table rather than prose because a lattice described in a
    document is a claim and a lattice enumerated in a parametrisation is a test.
    """
    assert produce() != forbidden, label


def test_lattice_storage_and_endpoint_names_grant_nothing():
    """Storage class and URL are not evidence. `durable-store` names where a
    record is KEPT; `/api/operations/*` names where it is SERVED."""
    for impostor in ("durable-store", "operations", "api", "runtime.db",
                     "node-telemetry", "fixture-node", "mock-fixture", "absent"):
        assert not bp.is_broker_truth(impostor), impostor


def test_lattice_execution_authority_is_unreachable():
    """Not "no input currently sets it" — no input CAN. It is a constant."""
    import ast
    tree = ast.parse((BACKEND_DIR / "activation_policy.py").read_text())
    assignments = []
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg == "execution_authority":
            assignments.append(node.value)
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "execution_authority":
            assignments.append(node.value)
    assert assignments, "the field vanished — this guard guards nothing"
    for value in assignments:
        assert isinstance(value, ast.Constant) and value.value is False, ast.dump(value)

    # And empirically, across every payload class this repository can build.
    for build in (pay.account_payload, pay.unavailable_account_payload,
                  pay.degraded_payload, pay.mismatched_account_payload,
                  pay.equity_absent_payload, pay.future_dated_payload):
        assert ap.verdict(pay.envelope(build())).execution_authority is False, build


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 + 7 — defaults, polarity and hostile numerics
# ══════════════════════════════════════════════════════════════════════════════

#: (value, admissible, produces a NEGATIVE-FIGURE warning)
NUMERIC_CASES = [
    (4211.5, True, False), (4211, True, False), (0, True, False), (0.0, True, False),
    (None, True, False),                       # unreported is not invalid
    (-1.0, True, True), (-0.01, True, True),   # REAL. A bad day, not a bad packet.
    (1e308, True, False),
    ("4211.5", False, False), ("", False, False), (True, False, False),
    (False, False, False), (float("nan"), False, False),
    (float("inf"), False, False), (float("-inf"), False, False),
    ([], False, False), ({}, False, False),
]


@pytest.mark.parametrize("value,admissible,warns", NUMERIC_CASES,
                         ids=[repr(c[0]) for c in NUMERIC_CASES])
def test_numeric_adversaries_on_balance(value, admissible, warns):
    """THE FIX THAT MATTERS MOST HERE IS THE NEGATIVE ONE.

    `-1.0` used to be refused with `numeric_invalid`, blanking the account. An
    account can genuinely go negative; the moment it does is the moment an
    operator needs the figure. Structural impossibility (NaN, Infinity, a
    string, a bool) is still a refusal.
    """
    payload = pay.account_payload()
    payload["account"]["health"]["balance"] = value
    entry = pay.envelope(payload)
    ok, reasons = ap.account_admissible(entry)
    assert ok is admissible, reasons
    if not admissible:
        assert ap.R_NUMERIC_INVALID in reasons
    negative_warnings = [w for w in ap.verdict(entry).warnings
                         if ap.R_NEGATIVE_FIGURE in w]
    assert bool(negative_warnings) is warns


def test_economic_oddities_are_warnings_not_refusals():
    """Named explicitly, because the line between "impossible" and "alarming"
    is the whole design. This module must not invent financial rules."""
    payload = pay.account_payload(balance=-500.0, equity=-620.0, free_margin=10.0)
    entry = pay.envelope(payload)
    admissible, reasons = ap.account_admissible(entry)
    assert admissible is True, reasons
    warnings = ap.verdict(entry).warnings
    assert any(ap.R_NEGATIVE_FIGURE in w for w in warnings)
    assert any(ap.R_FREE_MARGIN_EXCEEDS_EQUITY in w for w in warnings)
    # And the figures reach the surface rather than being suppressed.
    account = op.build_accounts(_sources([entry]), now=_now())[0]
    assert account.balance == -500.0
    assert account.provenance == bp.PROV_NODE_MT5


AVAILABILITY_CASES = [None, 0, 1, "", "true", "True", "false", [], {}, False]


@pytest.mark.parametrize("value", AVAILABILITY_CASES, ids=[repr(v) for v in AVAILABILITY_CASES])
def test_no_availability_value_except_literal_true_is_an_observation(value):
    payload = pay.canonical_payload(account_health=True, account_identity=True)
    payload["account"]["health"]["available"] = value
    payload["account"]["identity"]["available"] = value
    ok, reasons = ap.account_admissible(pay.envelope(payload))
    assert ok is False, value
    assert ap.R_ACCOUNT_NOT_OBSERVED in reasons


def test_refusal_reasons_are_deduplicated():
    """Two pins, one missing identity, one message. The operator reads these."""
    entry = pay.envelope(pay.canonical_payload(account_health=True,
                                               account_identity=False))
    _, reasons = ap.account_admissible(entry, expected_account="A", expected_server="B")
    assert reasons == (ap.R_ACCOUNT_IDENTITY_UNVERIFIABLE,)


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — the reconciliation source matrix, exhaustively
# ══════════════════════════════════════════════════════════════════════════════

LOCAL_SOURCES = {
    "absent": (),
    "mock": (_view(bp.PROV_MOCK_FIXTURE, fingerprint="acct_fx", balance=100000.0),),
    "genuine_same": (_view(bp.PROV_LIVE_MT5, balance=100.0),),
    "genuine_differs": (_view(bp.PROV_LIVE_MT5, balance=999.0),),
    "genuine_other_account": (_view(bp.PROV_LIVE_MT5, fingerprint="acctfp_B"),),
    "genuine_refused": (_view(bp.PROV_LIVE_MT5, admitted=False),),
}
NODE_SOURCES = {
    "absent": (),
    "admitted": (_view(bp.PROV_NODE_MT5, node_id="n1"),),
    "refused": (_view(bp.PROV_NODE_MT5, node_id="n1", admitted=False),),
}

#: (local, node) -> (admitted provenances in order, contradiction count)
EXPECTED_RECONCILIATION = {
    ("absent", "absent"): ([], 0),
    ("absent", "admitted"): (["node_mt5"], 0),
    ("absent", "refused"): (["node_mt5!"], 0),
    ("mock", "absent"): ([], 0),
    ("mock", "admitted"): (["node_mt5"], 0),
    ("mock", "refused"): (["node_mt5!"], 0),
    ("genuine_same", "absent"): (["live_mt5"], 0),
    ("genuine_same", "admitted"): (["node_mt5", "live_mt5"], 0),
    ("genuine_same", "refused"): (["node_mt5!", "live_mt5"], 0),
    ("genuine_differs", "absent"): (["live_mt5"], 0),
    ("genuine_differs", "admitted"): (["node_mt5", "live_mt5"], 1),
    ("genuine_differs", "refused"): (["node_mt5!", "live_mt5"], 0),
    ("genuine_other_account", "admitted"): (["node_mt5", "live_mt5"], 0),
    ("genuine_other_account", "absent"): (["live_mt5"], 0),
    ("genuine_other_account", "refused"): (["node_mt5!", "live_mt5"], 0),
    ("genuine_refused", "absent"): (["live_mt5!"], 0),
    ("genuine_refused", "admitted"): (["node_mt5", "live_mt5!"], 0),
    ("genuine_refused", "refused"): (["node_mt5!", "live_mt5!"], 0),
}


@pytest.mark.parametrize("key", sorted(EXPECTED_RECONCILIATION),
                         ids=lambda k: f"{k[0]}+{k[1]}")
def test_reconciliation_source_matrix(key):
    """Every combination, with the expected answer written down rather than
    computed by the code under test.

    Note what a `!` suffix means: the record is RETURNED and marked refused. A
    refused record is never deleted — an operator has to be able to see that a
    source is reporting and being turned away.
    """
    import copy
    local_key, node_key = key
    local, node = LOCAL_SOURCES[local_key], NODE_SOURCES[node_key]
    before = (copy.deepcopy(local), copy.deepcopy(node))

    admitted, warnings = ap.reconcile_account_sources(node, local)

    expected_provenances, expected_warnings = EXPECTED_RECONCILIATION[key]
    assert [a.provenance + ("" if a.admitted else "!") for a in admitted] \
        == expected_provenances
    assert len(warnings) == expected_warnings, warnings
    assert (local, node) == before, "reconciliation mutated its inputs"


def test_reconciliation_never_averages_or_prefers():
    """The property the matrix exists to protect, stated once on its own."""
    admitted, warnings = ap.reconcile_account_sources(
        NODE_SOURCES["admitted"], LOCAL_SOURCES["genuine_differs"])
    balances = sorted(a.balance for a in admitted)
    assert balances == [100.0, 999.0], "a source was dropped or a figure invented"
    assert warnings and ap.R_CONTRADICTORY_SOURCES in warnings[0]


def test_a_refused_record_never_produces_a_contradiction():
    """Otherwise the pin doing its job would raise an alarm every single time."""
    assert ap.account_contradictions(
        (_view(bp.PROV_NODE_MT5, node_id="n1", balance=1.0),
         _view(bp.PROV_NODE_MT5, node_id="n2", balance=2.0, admitted=False))) == ()


def test_unidentified_genuine_records_group_together():
    """The worse case, not the ignored one. Two sources reporting money for an
    account neither can name is more alarming than two naming the same one."""
    warnings = ap.account_contradictions(
        (_view(bp.PROV_NODE_MT5, fingerprint=None, node_id="n1", balance=1.0),
         _view(bp.PROV_NODE_MT5, fingerprint=None, node_id="n2", balance=2.0)))
    assert warnings and "none of them identified" in warnings[0]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — identity pinning, every configuration
# ══════════════════════════════════════════════════════════════════════════════

#: (label, kwargs, admissible for a full payload, admissible with no identity)
PIN_CASES = [
    ("neither pin", {}, True, True),
    ("account only", dict(expected_account=pay.EXPECTED_ACCOUNT), True, False),
    ("server only", dict(expected_server=pay.EXPECTED_SERVER), True, False),
    ("both", dict(expected_account=pay.EXPECTED_ACCOUNT,
                  expected_server=pay.EXPECTED_SERVER), True, False),
    ("empty-string pin is unpinned", dict(expected_account=""), True, True),
    ("wrong account", dict(expected_account="acctfp_OTHER"), False, False),
    ("wrong server", dict(expected_server="Other-Server"), False, False),
    ("server case differs", dict(expected_server="ftmo-demo"), False, False),
]


@pytest.mark.parametrize("label,kwargs,full_ok,noid_ok", PIN_CASES,
                         ids=[c[0] for c in PIN_CASES])
def test_identity_pin_matrix(label, kwargs, full_ok, noid_ok):
    """Case differences REFUSE, deliberately.

    A server string that differs by case is a different string, and the safe
    direction on an ambiguity about which account money belongs to is always
    refusal. Normalising would be a guess about broker naming.

    WHITESPACE IS NOT IN THIS TABLE, and that is a correction: an earlier row
    asserted that `" FTMO-Demo "` refuses, which is true of the function but
    unreachable through the only path that sets it — `expected_identity()`
    strips the environment variable, so a padded pin is admitted. Asserting a
    behaviour the runtime cannot exhibit is how a test comes to describe a
    system nobody has.
    """
    full = pay.envelope(pay.account_payload())
    no_identity = pay.envelope(pay.canonical_payload(account_health=True,
                                                     account_identity=False))
    assert ap.account_admissible(full, **kwargs)[0] is full_ok, label
    assert ap.account_admissible(no_identity, **kwargs)[0] is noid_ok, label


def test_identity_the_node_says_it_did_not_observe_is_not_identity(monkeypatch):
    """A node reporting `identity.available: false` while leaving values in the
    block had those values compared against the pin.

    The dangerous shape is a STALE fingerprint from a previous account that
    happens to match: it was admitted on the strength of a block the node had
    just said it did not read. `safe_identity` nulls those fields, so this was
    unreachable through the shipped node — an argument about the current
    publisher, not about the contract. The Mac does not rely on the node being
    self-consistent anywhere else, and now does not here either.
    """
    for stale_value in (pay.EXPECTED_ACCOUNT, "acctfp_SOMETHING_ELSE"):
        payload = pay.account_payload()
        payload["account"]["identity"]["available"] = False
        payload["account"]["identity"]["fingerprint"] = stale_value
        admissible, reasons = ap.account_admissible(
            pay.envelope(payload), expected_account=pay.EXPECTED_ACCOUNT)
        assert admissible is False, stale_value
        assert reasons == (ap.R_ACCOUNT_IDENTITY_UNVERIFIABLE,), reasons

    # The same for the server, and the health block still admits on its own
    # when nothing is pinned — the rule is about the PIN, not about hiding data.
    payload = pay.account_payload()
    payload["account"]["identity"]["available"] = False
    assert ap.account_admissible(pay.envelope(payload))[0] is True


def test_an_unpinned_deployment_is_a_documented_state_not_an_accident(monkeypatch):
    """DECIDED EXPLICITLY: unpinned admission IS permitted.

    Refusing everything until a pin is set would mean a first activation shows
    nothing with no way to discover the fingerprint to pin — the operator would
    have to read it off the VPS, which is exactly the improvised debugging this
    programme replaced. So an unpinned deployment admits, and the CHECKER says
    so out loud as WARN, never PASS, on every single run.

    The runbook's instruction to pin before the first payload is therefore
    advice backed by a visible warning, not a claim the code enforces.
    """
    monkeypatch.delenv(ap.VAR_EXPECTED_ACCOUNT, raising=False)
    monkeypatch.delenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, raising=False)
    monkeypatch.delenv(ap.VAR_EXPECTED_SERVER, raising=False)
    assert ap.expected_identity() == (None, None)
    assert ap.account_admissible(pay.envelope(pay.mismatched_account_payload()))[0] is True


def test_a_padded_pin_is_stripped_and_therefore_MATCHES(monkeypatch):
    """The behaviour the removed matrix row got backwards."""
    monkeypatch.setenv(ap.VAR_EXPECTED_SERVER, "  FTMO-Demo  ")
    monkeypatch.delenv(ap.VAR_EXPECTED_ACCOUNT, raising=False)
    monkeypatch.delenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, raising=False)
    _, server = ap.expected_identity()
    assert server == "FTMO-Demo"
    assert ap.account_admissible(pay.envelope(pay.account_payload()),
                                 expected_server=server)[0] is True


def test_pins_are_stripped_and_an_empty_pin_is_no_pin(monkeypatch):
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, "   ")
    monkeypatch.delenv(ap.VAR_EXECUTION_EXPECTED_ACCOUNT, raising=False)
    assert ap.expected_identity()[0] is None
    monkeypatch.setenv(ap.VAR_EXPECTED_ACCOUNT, "  acctfp_X  ")
    assert ap.expected_identity()[0] == "acctfp_X"


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 6 — one freshness authority
# ══════════════════════════════════════════════════════════════════════════════

def _entry_at(received_ago, *, flag):
    return pay.envelope(pay.account_payload(), stale=flag, received_ago=received_ago)


def test_the_verdict_cannot_be_told_it_is_fresh():
    """THE TWO-AUTHORITIES DEFECT.

    `verdict()` read `entry["stale"]` and believed it, while the projection
    recomputed the age from the arrival clock. In production they agree; that
    they agree is not a property. An envelope claiming freshness over an hour-old
    arrival now reads stale, because the verdict takes the more pessimistic of
    the two witnesses.
    """
    assert ap.verdict(_entry_at(3.0, flag=False)).stale is False
    assert ap.verdict(_entry_at(9000, flag=False)).stale is True, (
        "the flag overrode the arrival clock")
    assert ap.verdict(_entry_at(3.0, flag=True)).stale is True, (
        "a flag saying stale is believed without argument")


UNDATABLE_ARRIVALS = [None, "", "   ", "not-a-time", "2026-08-02T12:00:00", 12345, []]


@pytest.mark.parametrize("value", UNDATABLE_ARRIVALS, ids=[repr(v) for v in UNDATABLE_ARRIVALS])
def test_an_undatable_arrival_is_stale(value):
    """Including the timezone-naive one: an arrival the tower cannot place on a
    timeline is not one it may call current."""
    entry = pay.envelope(pay.account_payload(), stale=False)
    entry["received_at"] = value
    assert ap.recomputed_stale(entry) is True


def test_an_arrival_stamped_in_the_future_is_old_not_infinitely_fresh():
    future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
    entry = pay.envelope(pay.account_payload(), stale=False)
    entry["received_at"] = future.replace("+00:00", "Z")
    assert ap.recomputed_stale(entry) is True


def test_the_projection_and_the_verdict_agree_on_staleness():
    """The two witnesses, compared directly. This is the assertion that would
    have failed before the fix."""
    now = _now()
    # THE SAMPLE POINTS MATTER MORE THAN THE ASSERTION.
    #
    # The first version tried 3 s and 9000 s only. The two authorities used
    # DIFFERENT BUDGETS — the envelope's phase-aware 900 s against the account
    # projection's 120 s — so they could only ever disagree between those two
    # numbers, and both sample points sat outside that window. The test was
    # chosen, accidentally, so that it could not fail.
    for received_ago, flag in ((3.0, False), (119.0, False), (121.0, False),
                               (500.0, False), (899.0, False), (901.0, False),
                               (9000, False), (3.0, True), (9000, True)):
        entry = _entry_at(received_ago, flag=flag)
        projected = op.build_accounts(_sources([entry]), now=now)[0].freshness.stale
        assert ap.verdict(entry).stale == projected, (received_ago, flag)


def test_the_recomputation_uses_the_TIGHTER_of_the_two_budgets():
    """The node's envelope carries a 900 s phase-aware budget; the account
    projection judges the same arrival against 120 s. Taking the envelope's
    number alone made this recomputation an order of magnitude LESS pessimistic
    than the projection — in a function whose docstring promised the opposite.

    It also removes the node's influence: the phase budget derives from a cycle
    status the node supplies, so a node that never reports `no_new_bar` would
    otherwise buy itself the larger window.
    """
    entry = pay.envelope(pay.account_payload(), stale=False, received_ago=300)
    entry["stale_after_seconds"] = 900.0
    assert ap.recomputed_stale(entry) is True, "the 900s budget won"
    # And a caller that genuinely has the larger budget can say so.
    assert ap.recomputed_stale(entry, projection_budget_s=900.0) is False


def test_a_node_cannot_buy_itself_a_larger_freshness_budget():
    entry = pay.envelope(pay.account_payload(), stale=False, received_ago=300)
    for claimed in (900.0, 86400.0, 10 ** 9, "3600", None, True, -5):
        entry["stale_after_seconds"] = claimed
        assert ap.recomputed_stale(entry) is True, claimed


def test_the_node_card_and_its_own_freshness_block_agree():
    """`build_nodes` read the envelope flag while computing `freshness` from the
    arrival clock, so one card could print `lifecycleState: current` beside
    `freshness.stale: true` — from a single entry, with nothing to explain it."""
    now = _now()
    for received_ago in (3.0, 119.0, 121.0, 500.0, 9000.0):
        entry = pay.envelope(pay.account_payload(), stale=False,
                             received_ago=received_ago)
        node = op.build_nodes(_sources([entry]), now=now)[0]
        assert (node.lifecycle_state == np_.NODE_STALE) == node.freshness.stale, (
            received_ago, node.lifecycle_state, node.freshness.stale)


def test_a_locally_read_account_states_when_it_was_observed():
    """Without `observed_at` a local record silently opted out of every money
    comparison, which made the local-versus-node contradiction — matrix row 11 —
    unreachable in production while its unit test passed on a hand-built view."""
    now = _now()
    sources = _sources([], adapter_kind=lambda: "mt5",
                       account_snapshot=lambda: {"fingerprint": "acctfp_A",
                                                 "balance": 100.0,
                                                 "at": "2026-08-02T12:00:00Z"})
    local = op.build_accounts(sources, now=now)
    assert local[0].observed_at == "2026-08-02T12:00:00Z"


def test_row_11_fires_through_build_accounts_not_only_through_the_helper():
    """The matrix row, driven the way production drives it."""
    now = _now()
    node_entry = pay.envelope(pay.account_payload())
    observed_at = node_entry["snapshot"]["account"]["health"]["observed_at"]
    sources = _sources([node_entry], adapter_kind=lambda: "mt5",
                       account_snapshot=lambda: {
                           "fingerprint": pay.EXPECTED_ACCOUNT, "balance": 9999.0,
                           "server": pay.EXPECTED_SERVER, "at": observed_at})
    accounts = op.build_accounts(sources, now=now)
    assert len(accounts) == 2, [a.provenance for a in accounts]
    warnings = ap.account_contradictions(accounts)
    assert warnings and ap.R_CONTRADICTORY_SOURCES in warnings[0], accounts


CONTRADICTION_TIMING = [
    ("simultaneous", 0.0, 1), ("59s apart", 59.0, 1), ("61s apart", 61.0, 0),
    ("one hour apart", 3600.0, 0),
]


@pytest.mark.parametrize("label,gap,expected", CONTRADICTION_TIMING,
                         ids=[c[0] for c in CONTRADICTION_TIMING])
def test_money_is_only_compared_between_near_simultaneous_samples(label, gap, expected):
    base = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)
    warnings = ap.account_contradictions((
        _view(bp.PROV_NODE_MT5, node_id="n1", balance=1.0,
              observed_at=base.isoformat().replace("+00:00", "Z")),
        _view(bp.PROV_NODE_MT5, node_id="n2", balance=2.0,
              observed_at=(base + timedelta(seconds=gap)).isoformat().replace("+00:00", "Z")),
    ))
    assert len(warnings) == expected, warnings


def test_identity_facts_are_compared_whenever_the_samples_were_taken():
    """Two sources must never disagree about which SERVER an account is on, no
    matter how far apart they looked. Only money drifts."""
    warnings = ap.account_contradictions((
        _view(bp.PROV_NODE_MT5, node_id="n1", server="FTMO-Demo",
              observed_at="2026-08-02T12:00:00Z"),
        _view(bp.PROV_NODE_MT5, node_id="n2", server="Other-Live",
              observed_at="2026-08-02T18:00:00Z"),
    ))
    assert warnings and "server" in warnings[0]


# ══════════════════════════════════════════════════════════════════════════════
# PHASE 3 + 10 — the admission gate fails CLOSED on non-booleans
# ══════════════════════════════════════════════════════════════════════════════

ADMITTED_VALUES = [
    (True, True), (None, True),          # absent/null = not checked = admitted
    (False, False),
    ("false", False), ("true", False), (0, False), (1, False),
    ({}, False), ([], False), ("FALSE", False),
]


@pytest.mark.parametrize("value,permits", ADMITTED_VALUES, ids=[repr(v[0]) for v in ADMITTED_VALUES])
def test_the_checker_admission_gate_fails_closed_on_non_booleans(value, permits):
    """`is not False` waved through `"false"`, `0` and `{}` — a contract
    violation admitted BECAUSE it was malformed. The rule everywhere else in
    this programme is that the unrecognised fails closed."""
    assert activation_check._admission_permits(value) is permits


def test_the_frontend_and_backend_admission_gates_agree():
    """Two implementations of one rule, asserted equal rather than assumed.

    The TypeScript is read as source rather than executed: the property that
    matters is that it names the same three permitted forms.
    """
    gate = (REPO_ROOT / "frontend" / "src" / "lib" / "operationalProvenance.ts").read_text()
    assert "function admissionPermits" in gate
    assert "admitted === undefined || admitted === null" in gate
    assert "return admitted === true" in gate
    assert "admitted === false" not in gate.split("function admissionPermits")[1][:400], (
        "the fail-open comparison came back")
