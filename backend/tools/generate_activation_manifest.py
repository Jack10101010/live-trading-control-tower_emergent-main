"""Generate `docs/governance/activation-compatibility.json` FROM THE CODE.

    python3 backend/tools/generate_activation_manifest.py > docs/governance/activation-compatibility.json

WHY GENERATED AND NOT WRITTEN
    A compatibility manifest is a promise to another machine. Typed by hand it
    becomes a description of what someone believed on the day they typed it;
    `test_release_checkpoint.py` regenerates it and asserts the committed file
    still matches, so it cannot drift from the policy it describes without a
    test failing.

Reads nothing but this repository. No network, no state, no VPS.
"""
import json, os, subprocess, sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
REPO = BACKEND.parent
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("CONTROL_TOWER_STATE_DIR", "/tmp/ct-manifest-state")
import activation_check as chk, activation_policy as ap, broker_provenance as bp
import live_telemetry as lt, node_provenance as np_

def git(*args) -> str:
    """Ask git, and tolerate not being able to.

    `CONTROL_TOWER_GIT_DIR` exists because this repository is a worktree whose
    gitdir lives outside it; a sandbox that cannot resolve it should still be
    able to generate the rest of the manifest rather than fail entirely.
    """
    command = ["git"]
    gitdir = os.environ.get("CONTROL_TOWER_GIT_DIR")
    if gitdir:
        command += ["--git-dir", gitdir, "--work-tree", str(REPO)]
    try:
        result = subprocess.run(command + list(args), cwd=str(REPO),
                                capture_output=True, text=True, timeout=15)
        return result.stdout.strip() or "unknown"
    except Exception:                                            # noqa: BLE001
        return "unknown"

manifest = {
  "$schema": "./activation-compatibility.schema.json",
  "manifestVersion": 1,
  "generatedBy": "backend/tools/generate_activation_manifest.py",
  "controlTower": {
    "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
    "commit": git("rev-parse", "HEAD"),
    "admissionPolicyModule": "backend/activation_policy.py",
    "activationCheckerModule": "backend/activation_check.py",
    "checkerChecks": 0,
  },
  "nodeTelemetry": {
    "acceptedSchemaVersions": list(lt.SUPPORTED_SCHEMA_VERSIONS),
    "unknownVersionBehaviour": "refused_at_ingest_http_400",
    "legacyPayloadBehaviour": "adapted_with_legacy_source_true_and_null_unknowns",
    "malformedCanMasqueradeAsLegacy": False,
    "refusalOverwritesLastGoodSnapshot": False,
  },
  "capabilityMarker": {
    "required": False,
    "presentInContract": False,
    "macBehaviourWhenAbsent": ap.CAPABILITY_UNKNOWN,
    "checkerStatusWhenAbsent": "WARN",
    "requestedFieldName": "capabilities",
    "requestedValue": "account_observation",
    "note": "additive; the Mac ignores it until a Mac-side milestone consumes it",
  },
  "accountObservation": {
    "requiredFields": [
      "account.identity.available", "account.identity.fingerprint",
      "account.identity.server",
      "account.health.available",
    ],
    "optionalFields": [
      "account.identity.currency", "account.identity.trade_mode",
      "account.health.balance", "account.health.equity",
      "account.health.free_margin", "account.health.currency",
      "account.health.trade_allowed", "account.health.trade_expert",
      "account.health.observed_at", "account.health.reasons",
      "account.health.healthy",
    ],
    "availabilityComparison": "is True",
    "nullMeaning": "not_reported",
    "zeroMeaning": "a_measurement",
    "negativeValuesAdmitted": True,
    "negativeValueWarning": ap.R_NEGATIVE_FIGURE,
    "nonFiniteBehaviour": "whole_snapshot_refused_numeric_invalid",
    "identityRequiredWhenPinned": True,
    "identityAbsentWhenPinnedReason": ap.R_ACCOUNT_IDENTITY_UNVERIFIABLE,
  },
  "provenance": {
    "brokerTruth": sorted(bp.AUTHORITATIVE_PROVENANCE),
    "nodeTruth": sorted(np_.AUTHORITATIVE_NODE_PROVENANCE),
    "nonOperational": [bp.PROV_MOCK_FIXTURE, np_.PROV_FIXTURE_NODE, bp.PROV_ABSENT],
    "suppliedByVps": False,
    "derivedFrom": "validated_observation_evidence",
    "setsAreDisjoint": ap.node_and_broker_authority_are_disjoint(),
  },
  "freshness": {
    "basis": "received_at",
    "idleBudgetSeconds": lt.DEFAULT_STALE_AFTER_S,
    "recomputeBudgetSeconds": lt.RECOMPUTE_STALE_AFTER_S,
    "budgetClampedToProjection": True,
    "maxClockSkewSeconds": chk.MAX_CLOCK_SKEW_S,
    "maxComparableObservationGapSeconds": ap.MAX_COMPARABLE_OBSERVATION_GAP_S,
    "authority": "activation_policy.recomputed_stale",
    "futureTimestampBehaviour": "treated_as_old",
    "undatableArrivalBehaviour": "stale",
  },
  "environmentVariables": {
    "expectedNode": chk.VAR_EXPECTED_INSTANCE,
    "expectedAccount": ap.VAR_EXPECTED_ACCOUNT,
    "expectedServer": ap.VAR_EXPECTED_SERVER,
    "expectedAccountExecutionFallback": ap.VAR_EXECUTION_EXPECTED_ACCOUNT,
    "apiToken": chk.VAR_TOKEN,
    "stateDir": "CONTROL_TOWER_STATE_DIR",
    "environment": "CONTROL_TOWER_ENVIRONMENT",
    "note": "NAMES ONLY. No value in this file is a real identifier.",
    "exampleAccountValue": "acctfp_0123456789abcdef",
    "exampleServerValue": "Example-Demo",
    "mustBeSetOnTheBackendProcess": True,
  },
  "environmentExpectations": {
    "environment": "development",
    "productionPermitted": False,
    "brokerAdapterOnMac": "mock",
    "macMayClaimLocalMt5": False,
  },
  "refusalReasonCodes": sorted(v for k, v in vars(ap).items()
                               if k.startswith("R_") and isinstance(v, str)),
  "incompatibleLegacyBehaviours": [
    "pre-UI-2 flat payload: node visible, account observation impossible",
    "ct.node-telemetry.v2 or any unknown version: refused 400, no downgrade",
    "a provenance field supplied by the node: ignored, never honoured",
  ],
  "unresolvedPlaceholders": [
    {"id": "vps.commit", "description": "the VPS commit implementing M-NODE-ACCT-1",
     "value": None},
    {"id": "capability.marker",
     "description": "no positive capability signal exists in the contract",
     "value": None},
  ],
  "minimumFutureVpsCommit": None,
}
manifest["controlTower"]["checkerChecks"] = len({
    n.args[0].value for n in __import__("ast").walk(
        __import__("ast").parse((BACKEND / "activation_check.py").read_text()))
    if isinstance(n, __import__("ast").Call)
    and getattr(n.func, "attr", None) == "add" and n.args
    and isinstance(n.args[0], __import__("ast").Constant)})
print(json.dumps(manifest, indent=2, sort_keys=False))
