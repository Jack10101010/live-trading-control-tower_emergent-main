"""M-NODE-ACCT-1B — how far an account observation is allowed to reach.

Adding account observation to the published envelope is only safe if it changes
the envelope in bounded, known ways. An adversarial sweep over the observation's
branches found that a fully-observed sample changes TWO blocks outside
`account`. Both turned out to be correct, and both are surprising enough that
they are pinned here rather than left to be rediscovered:

  * `runtime.open_eligibility.reasons` gains/loses `account_health_unavailable`.
    This is the fail-closed direction -- not knowing the account's health is
    itself a reason not to open -- so the coupling is deliberate.

  * `market.observed_at` carries the OBSERVATION timestamp even when
    `market.available` is false. This is the ported canonical behaviour
    (`live/telemetry.py` is byte-unchanged since the port at d5f1042), so the
    Mac already expects it; `available` is the authoritative gate, not the
    timestamp. Changing it would be contract drift, which is why this file
    asserts the behaviour instead of "fixing" it.

Everything else -- engine, reconciliation, risk, positions, execution, arming --
must be untouched. That is the property that makes publication safe: an account
read cannot perturb the node's reported trading state.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import telemetry as nt  # noqa: E402
from live.account_observation import (  # noqa: E402
    AccountObservation, ObservedHealth, ObservedIdentity)

AT = "2026-01-01T00:00:00Z"

IDENT = ObservedIdentity(login=999000111, server="Example-Demo",
                         currency="USD", trade_mode=0)
HEALTH = ObservedHealth(balance=100000.0, equity=99750.0, free_margin=98000.0,
                        currency="USD", trade_allowed=True, trade_expert=True)

#: Blocks an account observation must never influence.
UNTOUCHABLE = ("engine", "reconciliation", "risk", "positions", "execution",
               "arming")


class _Cfg:
    symbol = "EURUSD"
    max_open_positions = 1
    fixed_risk_lots = 0.1
    daily_loss_limit_r = 3.0
    mt5_server = "Example-Demo"


def _build(observed):
    return nt.build_snapshot(
        instance_id="blast-radius", runner_result={}, executor_result={},
        engine_version="test", mode="dry_run", config=_Cfg(), state={},
        arm_runtime=None, observed=observed, bridge=None)


def _obs(**kw):
    return AccountObservation(sampled_at=AT, **kw).as_observed_mapping()


BRANCHES = {
    "none": None,
    "unavailable": _obs(),
    "identity_only": _obs(identity=IDENT),
    "health_only": _obs(health=HEALTH),
    "full": _obs(identity=IDENT, health=HEALTH, health_observed_at=AT),
}


@pytest.mark.parametrize("branch", sorted(BRANCHES))
@pytest.mark.parametrize("block", UNTOUCHABLE)
def test_observation_cannot_perturb_trading_state(branch, block):
    """The core safety property of this milestone."""
    assert _build(BRANCHES[branch]).get(block) == _build(None).get(block), \
        f"observation branch {branch!r} altered {block!r}"


@pytest.mark.parametrize("branch", sorted(BRANCHES))
def test_every_branch_produces_a_complete_canonical_envelope(branch):
    snap = _build(BRANCHES[branch])
    missing = [k for k in nt.REQUIRED_TOP_LEVEL if k not in snap]
    assert not missing, f"branch {branch!r} omits {missing}"
    assert snap["schema_version"] == "ct.node-telemetry.v1"
    assert set(snap["account"]) == {"identity", "health"}, \
        "the account block shape is fixed by the Mac contract"


# ── coupling 1: eligibility is fail-closed on unknown health ─────────────────
def _reasons(snap):
    return snap["runtime"]["open_eligibility"]["reasons"]


@pytest.mark.parametrize("branch", ["none", "unavailable", "identity_only"])
def test_unknown_account_health_is_itself_a_reason_not_to_open(branch):
    assert "account_health_unavailable" in _reasons(_build(BRANCHES[branch]))


def test_known_health_clears_only_that_one_reason():
    """It must not clear mode_not_live or not_armed -- those are independent."""
    full = _reasons(_build(BRANCHES["full"]))
    assert "account_health_unavailable" not in full
    assert "mode_not_live" in full and "not_armed" in full


def test_eligibility_never_becomes_true_merely_because_health_arrived():
    for branch in BRANCHES:
        assert _build(BRANCHES[branch])["runtime"]["open_eligibility"]["eligible"] is False


# ── coupling 2: market.observed_at is the observation stamp ──────────────────
def test_market_observed_at_carries_the_observation_stamp_not_a_tick_time():
    """Pinned because it is surprising: the stamp is present while the market
    block itself is unavailable and every price field is None. `available` is
    the field a consumer must gate on."""
    snap = _build(BRANCHES["full"])
    market = snap["market"]
    assert market["observed_at"] == AT
    assert market["available"] is False
    for field in ("bid", "ask", "spread"):
        assert market[field] is None, \
            f"{field} must stay None -- a stamp is not a price"


def test_market_stays_unavailable_across_every_observation_branch():
    """An account read must never make the market look available."""
    for branch in BRANCHES:
        assert _build(BRANCHES[branch])["market"]["available"] is False


# ── no leakage of the raw login through any branch ───────────────────────────
@pytest.mark.parametrize("branch", sorted(BRANCHES))
def test_raw_login_never_appears_in_any_branch(branch):
    import json

    blob = json.dumps(_build(BRANCHES[branch]))
    assert str(IDENT.login) not in blob
    for banned in ("node_mt5", "live_mt5", "admitted", "admissionReasons",
                   "execution_authority"):
        assert banned not in blob


def test_unavailable_health_is_null_rather_than_zero():
    """A fabricated 0.0 balance is indistinguishable from a blown account."""
    health = _build(BRANCHES["unavailable"])["account"]["health"]
    assert health["available"] is False
    for field in ("balance", "equity", "free_margin"):
        assert health.get(field) is None, f"{field} was fabricated as a number"
