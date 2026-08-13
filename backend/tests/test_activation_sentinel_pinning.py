"""The FTMO-100k false positive, and the negative controls that keep the fix honest.

THE DEFECT
    `FIXTURE_SENTINEL_FIGURES` includes 100 000 — which is FTMO's standard demo
    and challenge account size. A genuine, correctly-pinned FTMO 100k account
    therefore tripped `fixture_figures_admitted` and `fixture_record_admitted`
    permanently, so the checker was RED on a correct activation. A checker that
    is red when everything is right teaches an operator to ignore it, which is
    worse than not having it.

THE FIX, AND ITS ONE DANGER
    A sentinel figure alone no longer proves contamination — but the exemption
    must not become a laundering route. It applies ONLY to a record that is
    authoritative, admitted, and carries BOTH the pinned fingerprint and the
    pinned server. Most of this module is the negative half: every way a record
    could try to claim the exemption without being the pinned account.
"""

from __future__ import annotations

import pytest

import activation_check as ac

PIN_ACCOUNT = "acctfp_9d15b57ed6c372a3"
PIN_SERVER = "FTMO-Demo"


def record(**kw):
    """An account as `/api/operations/accounts` projects it."""
    base = {"provenance": "node_mt5", "admitted": True,
            "accountFingerprint": PIN_ACCOUNT, "server": PIN_SERVER,
            "balance": 100000.0, "equity": 100000.0, "nodeId": "live-eurusd-golden-001"}
    base.update(kw)
    return base


def pinned(r):
    return ac._is_pinned_account(r, PIN_ACCOUNT, PIN_SERVER)


# ── the positive case: the genuine pinned FTMO 100k account ──────────────────

def test_the_genuine_pinned_ftmo_100k_account_is_exempt():
    """The whole point: a correct activation must not read as contamination."""
    assert pinned(record()) is True


@pytest.mark.parametrize("figure", [100000.0, 100412.0, 100000, 100412])
def test_every_sentinel_figure_is_exempt_on_the_pinned_account(figure):
    assert pinned(record(balance=figure, equity=figure)) is True


# ── negative controls: each must remain NOT exempt ───────────────────────────

def test_a_fixture_record_at_the_sentinel_figure_is_never_exempt():
    """The fixture world carries no fingerprint, so it can never match a pin."""
    assert pinned(record(provenance="mock-fixture", accountFingerprint=None)) is False
    assert pinned(record(provenance="fixture-node", accountFingerprint=None)) is False


def test_a_forged_node_mt5_label_without_the_pinned_fingerprint_is_not_exempt():
    """The attack the exemption must survive: right provenance, wrong identity."""
    assert pinned(record(accountFingerprint="acctfp_deadbeefdeadbeef")) is False


def test_the_wrong_server_is_not_exempt_even_with_the_right_fingerprint():
    assert pinned(record(server="SomeOther-Live")) is False


def test_a_missing_fingerprint_is_not_exempt():
    for missing in (None, "", 0):
        assert pinned(record(accountFingerprint=missing)) is False


@pytest.mark.parametrize("admitted", [False, "true", 1, "yes", 0, {}])
def test_a_record_not_admitted_is_not_exempt(admitted):
    """Admission fails closed on non-booleans, and the exemption inherits that:
    `_admission_permits` is reused rather than re-implemented, so `"false"`, `0`
    and `{}` cannot buy an exemption they could not buy admission with."""
    assert pinned(record(admitted=admitted)) is False


def test_a_null_admission_inherits_the_existing_gate_rather_than_tightening_it():
    """CHARACTERIZATION, not a preference. `_admission_permits` documents null as
    admitted ("an unpinned deployment has nothing to say"), and this fix does not
    touch admission policy — so a null-admitted PINNED record is exempt, exactly
    as it is admitted. Tightening it here would have silently changed the
    admission gate through the back door."""
    assert ac._admission_permits(None) is True
    assert pinned(record(admitted=None)) is True


def test_an_unknown_or_absent_provenance_is_not_exempt():
    for prov in ("absent", "unknown", None, "mock-fixture"):
        assert pinned(record(provenance=prov)) is False


def test_an_unpinned_deployment_grants_no_exemption_to_anything():
    """With no pins configured, the check is exactly as strict as before —
    otherwise an unpinned deployment would silently gain a bypass."""
    for acct, srv in ((None, PIN_SERVER), (PIN_ACCOUNT, None), (None, None), ("", "")):
        assert ac._is_pinned_account(record(), acct, srv) is False


# ── the two checks, end to end over a projected account list ─────────────────

def sentinel_findings(accounts, *, expected_account=PIN_ACCOUNT,
                      expected_server=PIN_SERVER):
    """Reproduce the predicate both checks share, over a whole projection."""
    admitted = [a for a in accounts
                if a.get("provenance") in ac.AUTHORITATIVE_PROVENANCE
                and ac._admission_permits(a.get("admitted"))]
    return [a for a in admitted
            if (a.get("balance") in ac.FIXTURE_SENTINEL_FIGURES
                or a.get("equity") in ac.FIXTURE_SENTINEL_FIGURES)
            and not ac._is_pinned_account(a, expected_account, expected_server)]


def test_the_live_projection_shape_produces_no_findings():
    """One pinned FTMO 100k account — exactly today's production projection."""
    assert sentinel_findings([record()]) == []


def test_a_duplicate_genuine_plus_fixture_account_still_fails():
    """The pinned account is exempt; the fixture beside it is not."""
    found = sentinel_findings([
        record(),
        record(provenance="node_mt5", accountFingerprint=None, server=None,
               balance=100000.0)])
    assert len(found) == 1


def test_a_fixture_equity_of_100412_still_fails():
    found = sentinel_findings([record(accountFingerprint="acctfp_other",
                                      balance=1234.0, equity=100412.0)])
    assert len(found) == 1


def test_a_non_sentinel_balance_is_never_a_finding():
    assert sentinel_findings([record(accountFingerprint="acctfp_other",
                                     balance=54321.0, equity=54321.0)]) == []


def test_the_exemption_is_the_only_change_to_the_sentinel_rule():
    """Without pins the new predicate is inert, so the pre-fix behaviour is
    recoverable exactly — the fix widens nothing else."""
    assert len(sentinel_findings([record()], expected_account=None,
                                 expected_server=None)) == 1
