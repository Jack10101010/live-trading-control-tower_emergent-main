"""M-CAP-REPIN-1 — the re-pinned gate accepts exactly ONE identity.

`verify_engine` is the last thing between a modified engine and a live account.
These tests pin that it accepts the candidate identity and NOTHING else — not
the old production value, not the intermediate governance-only or Option-A-only
values, not a missing or malformed one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live.config import ENGINE_VERSION_EXPECTED          # noqa: E402
from live.runner import LuxSession                       # noqa: E402

CANDIDATE = "559fcb66385e5e9fe757e61dfdc5e9c01d50abd358cdbb771105403d874a8c04"
OLD_PRODUCTION = "5bb6372c092cc65ae0d30c4a40bed26ed5e074aef2459de9e199b902849305be"
V2_NO_CHANGES = "2604fae01ae968e4e930073c0546a10e3969027818096a8092b2e200c02c46db"
GOVERNANCE_ONLY = "66a9f1642aa90842ca9ae052b89a2b4c2319b3deddc89fac005f405d06f64794"
OPTION_A_ONLY = "33e1a089705cf3a8c9601bd96d29d79d4172cb82432d8417294d47b3acfd27ec"


class _FakeSession:
    """Minimal stand-in exercising the real `verify_engine` logic."""
    def __init__(self, engine_version):
        self.engine_version = engine_version
    verify_engine = LuxSession.verify_engine


def test_pin_is_the_candidate_identity():
    assert ENGINE_VERSION_EXPECTED == CANDIDATE


def test_pin_is_no_longer_the_pre_repair_value():
    assert ENGINE_VERSION_EXPECTED != OLD_PRODUCTION


def test_candidate_identity_is_accepted():
    _FakeSession(CANDIDATE).verify_engine()          # must not raise


@pytest.mark.parametrize("rejected,label", [
    (OLD_PRODUCTION, "old production identity"),
    (V2_NO_CHANGES, "v2 policy with no repair and no options"),
    (GOVERNANCE_ONLY, "governance repair only"),
    (OPTION_A_ONLY, "repair + Option A only"),
])
def test_every_other_known_identity_is_rejected(rejected, label):
    with pytest.raises(RuntimeError, match="refusing to trade"):
        _FakeSession(rejected).verify_engine()


@pytest.mark.parametrize("bad", [
    "", None, "not-a-hash", "0" * 64,
    CANDIDATE.upper(),                 # case must matter
    CANDIDATE + "x", CANDIDATE[:-1],   # length must matter
    " " + CANDIDATE, CANDIDATE + " ",  # no incidental trimming
])
def test_malformed_or_missing_identity_is_rejected(bad):
    with pytest.raises(RuntimeError):
        _FakeSession(bad).verify_engine()


def test_the_gate_accepts_exactly_one_value_not_a_set():
    """Guards against anyone 'fixing' a deploy by widening the check."""
    accepted = []
    for candidate in (CANDIDATE, OLD_PRODUCTION, V2_NO_CHANGES,
                      GOVERNANCE_ONLY, OPTION_A_ONLY, "0" * 64):
        try:
            _FakeSession(candidate).verify_engine()
            accepted.append(candidate)
        except RuntimeError:
            pass
    assert accepted == [CANDIDATE], accepted


def test_rejection_message_names_both_values_for_diagnosis():
    with pytest.raises(RuntimeError) as exc:
        _FakeSession(OLD_PRODUCTION).verify_engine()
    msg = str(exc.value)
    assert OLD_PRODUCTION in msg and ENGINE_VERSION_EXPECTED in msg
