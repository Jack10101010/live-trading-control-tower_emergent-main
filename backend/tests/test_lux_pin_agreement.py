"""M-CAP-INTEGRATE-1 — structural guards for the deployment candidate.

Two failure modes this milestone actually hit, now pinned so they cannot recur:

1. **Pin drift.** `EXPECTED_COMMIT` is defined independently in
   `live/deploy_check.py` and `live/rehearsal.py`. During M-CAP-REPIN-1 the
   engine identity was re-pinned while these were not — caught only because
   `deploy_check preflight` happened to be run. Nothing enforced agreement.

2. **Capability deletion.** The first capacity candidate (`5fc8bdd`) descended
   from a lineage that predates M-NODE-ACCT-1, so deploying it would have
   DELETED `account_observation.py`, `status.py`, `lifecycle.py`, `world.py` and
   the telemetry contract. A future capacity integration could do the same.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import live.deploy_check as deploy_check                      # noqa: E402
import live.rehearsal as rehearsal                            # noqa: E402
from live.config import (ENGINE_MANIFEST_ID_EXPECTED,         # noqa: E402
                         ENGINE_MANIFEST_PATH,
                         ENGINE_VERSION_EXPECTED)


# ── 1. the two Lux commit pins must agree ────────────────────────────────────

def test_lux_commit_pins_agree():
    assert deploy_check.EXPECTED_COMMIT == rehearsal.EXPECTED_COMMIT, (
        "deploy_check and rehearsal pin different Lux commits — one was "
        "re-pinned without the other")


def test_lux_commit_pin_is_a_full_sha():
    pin = deploy_check.EXPECTED_COMMIT
    assert len(pin) == 40 and all(c in "0123456789abcdef" for c in pin), pin


def test_engine_identities_are_full_sha256():
    for value in (ENGINE_VERSION_EXPECTED, ENGINE_MANIFEST_ID_EXPECTED):
        assert len(value) == 64 and all(c in "0123456789abcdef" for c in value), value


def test_stored_manifest_matches_the_pinned_id():
    """The manifest FILE and the constant must describe the same tree — they are
    separately editable and nothing else checks they agree."""
    import json
    manifest = json.loads(Path(ENGINE_MANIFEST_PATH).read_text())
    assert manifest["engine_manifest_id"] == ENGINE_MANIFEST_ID_EXPECTED
    assert manifest["file_count"] == len(manifest["files"])


# ── 2. M-NODE-ACCT runtime modules must not be deleted ───────────────────────

# Every module the current production node needs to publish canonical account
# telemetry, own its lifecycle and report status. A capacity integration that
# removes any of these is a regression regardless of what it adds.
# Enumerated from the production lineage as it actually is — NOT copied from the
# capacity branch, which carries modules (e.g. account_identity.py) this lineage
# never had. Listing a non-existent module would make the guard fail for the
# wrong reason and train people to ignore it.
M_NODE_ACCT_RUNTIME = (
    "live/account_observation.py",
    "live/status.py",
    "live/lifecycle.py",
    "live/world.py",
    "live/telemetry.py",
    "live/publisher.py",
    "live/main.py",
    "live/engine_identity.py",
    "live/NODE-TELEMETRY-CONTRACT.md",
)


def test_m_node_acct_runtime_modules_are_present():
    missing = [rel for rel in M_NODE_ACCT_RUNTIME
               if not (REPO_ROOT / rel).is_file()]
    assert not missing, (
        f"M-NODE-ACCT runtime modules missing: {missing}. A capacity "
        f"integration must LAYER onto the production lineage, never replace it.")


def test_canonical_telemetry_schema_is_still_published():
    from live.telemetry import SCHEMA_VERSION
    assert SCHEMA_VERSION == "ct.node-telemetry.v1"


def test_account_observation_capability_is_still_declared():
    src = (REPO_ROOT / "live" / "telemetry.py").read_text()
    assert "account_observation" in src, (
        "the account_observation capability disappeared from telemetry")


def test_node_does_not_emit_authority_admission_or_provenance():
    """The node reports what it observed; the tower decides authority. If these
    ever appear in the node's payload the trust boundary has been inverted."""
    src = (REPO_ROOT / "live" / "telemetry.py").read_text()
    for forbidden in ('"admitted"', '"provenance"', '"authority"'):
        assert forbidden not in src, f"node emits {forbidden}"
