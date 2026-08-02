"""M-ACTIVATE-READINESS-1 — what a received observation is allowed to become.

WHY THIS IS NOT ONE `is_live()` BOOLEAN

    Activation is not a switch. A received observation can earn any subset of
    six INDEPENDENT permissions, and collapsing them is how a heartbeat comes to
    imply a balance:

        node identity/health      the node is running and this is its state
        node MT5 observation      the node says something about its terminal
        node-observed account     a specific account was read, by that node
        broker operational truth  that account may drive operator surfaces
        analytics input           that account may drive derived performance
        execution authority       orders may be dispatched

    Each is a separate function here, and each may be false while a lower one is
    true. `verdict()` returns all six; nothing returns a single verb.

WHAT EARNS WHAT

    Node telemetry NEVER grants broker truth. A node publishing perfectly says
    nothing about whether its terminal is readable — the node samples account
    state only on cycles containing an OPEN, so `available: false` is the
    ORDINARY case for a healthy node.

    Account data NEVER grants execution authority. Execution is governed by
    `execution_safety` / `command_authorization`, which this module does not
    import and must not influence.

    Provenance is earned from validated observation, never from configuration.
    That rule was established by M-MT5-READ-1 after an environment variable
    alone was found sufficient to stamp `live_mt5` on an all-null account.

THE FOUR PAYLOAD CLASSES, DISTINGUISHED

    Verified empirically against `live_telemetry`, not assumed from docs:

        old VPS              `legacy_source: true` (the flat pre-UI-2 shape)
        new, unavailable     validated v1, `account.*.available: false`
        new, valid           validated v1, `account.health.available: true`
        malformed / unknown  REJECTED AT INGEST, never stored

    Malformed payloads cannot masquerade as legacy: `is_legacy_payload` requires
    the ABSENCE of every v1-only section, so a broken v1 payload is rejected
    rather than gutted by the adapter.

    A MISSING `available` key reads as `None`, and every check here tests
    `is True`. Absence can never become an implied yes.

THE ONE GAP, DOCUMENTED RATHER THAN PAPERED OVER

    There is no positive CAPABILITY marker. A new VPS that supports account
    observation but has not sampled one is indistinguishable from a new VPS
    whose build never samples: both publish `available: false`.

    This is an operational inconvenience, not a safety hole — the Mac reports
    "no account observation" in both cases, which is true of both. It is
    recorded as a SHOULD in `docs/governance/VPS-CONTRACT-M-NODE-ACCT-1.md`
    (`account.capability`), and until it arrives `capability_signal()` returns
    UNKNOWN rather than guessing.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import broker_provenance as brokerprov
import live_telemetry
import node_provenance as nodeprov

# ── payload classes ──────────────────────────────────────────────────────────
CLASS_LEGACY = "legacy_node"                 # pre-UI-2 flat payload, adapted
CLASS_CANONICAL = "canonical_node"           # validated ct.node-telemetry.v1
CLASS_UNUSABLE = "unusable"                  # malformed / unknown version

# ── capability signal (no positive marker exists yet) ────────────────────────
CAPABILITY_OBSERVED = "observed"             # an account WAS sampled
CAPABILITY_UNKNOWN = "unknown"               # cannot tell: supports-but-idle
CAPABILITY_LEGACY_ABSENT = "legacy_absent"   # old build; cannot sample at all

# ── refusal reason codes (stable; the checker prints these) ──────────────────
R_NO_OBSERVATION = "no_observation"
R_UNUSABLE_PAYLOAD = "unusable_payload"
R_ACCOUNT_NOT_OBSERVED = "account_not_observed"
R_ACCOUNT_IDENTITY_MISMATCH = "account_identity_mismatch"
R_ACCOUNT_SERVER_MISMATCH = "account_server_mismatch"
R_NUMERIC_INVALID = "numeric_invalid"
R_CONTRADICTORY_SOURCES = "contradictory_account_sources"
#: A pinned deployment received an account observation that carries NO identity
#: to compare against the pin. Refused, because "cannot be checked" is not
#: "passed the check" — see `account_admissible`.
R_ACCOUNT_IDENTITY_UNVERIFIABLE = "account_identity_unverifiable"
R_STALE = "observation_stale"
R_NODE_DEGRADED = "node_degraded"
R_MT5_CONTRADICTION = "mt5_observation_contradicts_account"

#: The environment variable naming the account this deployment expects. When
#: unset, identity matching is NOT enforced — but the checker reports that as a
#: WARN, because an unpinned deployment cannot detect an account switch.
VAR_EXPECTED_ACCOUNT = "CONTROL_TOWER_EXPECTED_ACCOUNT"
VAR_EXPECTED_SERVER = "CONTROL_TOWER_EXPECTED_SERVER"
#: The EXECUTION side's pre-existing pin (`server.py`). Read as a fallback so a
#: deployment that pinned execution does not silently leave the admission gate
#: unpinned — two names for one fact is how one of them ends up unset.
VAR_EXECUTION_EXPECTED_ACCOUNT = "NODE_EXPECTED_ACCOUNT_FINGERPRINT"


@dataclass(frozen=True)
class ActivationVerdict:
    """Six independent permissions plus the evidence behind each refusal.

    Deliberately NOT reducible to a boolean. A caller that wants one has to
    name which permission it means, which is the moment it should think.
    """
    payload_class: str = CLASS_UNUSABLE
    capability: str = CAPABILITY_UNKNOWN

    node_identity_visible: bool = False
    node_mt5_observation_visible: bool = False
    node_account_admissible: bool = False
    broker_truth_admissible: bool = False
    analytics_input_admissible: bool = False
    #: ALWAYS False here. Execution is governed elsewhere and this module has no
    #: opinion; the field exists so a reader can see it was considered.
    execution_authority: bool = False

    stale: bool = False
    degraded: bool = False
    reasons: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "payloadClass": self.payload_class,
            "capability": self.capability,
            "nodeIdentityVisible": self.node_identity_visible,
            "nodeMt5ObservationVisible": self.node_mt5_observation_visible,
            "nodeAccountAdmissible": self.node_account_admissible,
            "brokerTruthAdmissible": self.broker_truth_admissible,
            "analyticsInputAdmissible": self.analytics_input_admissible,
            "executionAuthority": self.execution_authority,
            "stale": self.stale,
            "degraded": self.degraded,
            "reasons": list(self.reasons),
            "warnings": list(self.warnings),
        }


def classify_payload(entry) -> str:
    """Which of the four payload classes this stored observation belongs to.

    Operates on the READ-SIDE envelope, so a payload that failed ingest never
    reaches here — `/api/live/ingest` rejects malformed and unknown-version
    bodies with 4xx and cannot overwrite the last valid snapshot.
    """
    if not isinstance(entry, dict):
        return CLASS_UNUSABLE
    snapshot = entry.get("snapshot")
    if not isinstance(snapshot, dict):
        return CLASS_UNUSABLE
    if snapshot.get("schema_version") not in live_telemetry.SUPPORTED_SCHEMA_VERSIONS:
        return CLASS_UNUSABLE
    if entry.get("legacy_source") or snapshot.get("legacy_source"):
        return CLASS_LEGACY
    return CLASS_CANONICAL


def capability_signal(entry) -> str:
    """Whether this node CAN report an account — as far as can be told.

    Returns UNKNOWN for a canonical payload with no account sampled, because
    the contract carries no positive capability marker. Guessing "supported"
    would let an operator conclude the VPS milestone had landed when it had
    not; guessing "unsupported" would send them debugging a node that is simply
    idle. UNKNOWN is the only honest answer, and the checker prints it as WARN.
    """
    payload_class = classify_payload(entry)
    if payload_class == CLASS_LEGACY:
        return CAPABILITY_LEGACY_ABSENT
    if payload_class == CLASS_UNUSABLE:
        return CAPABILITY_UNKNOWN
    account = _account_of(entry)
    health = account.get("health") if isinstance(account.get("health"), dict) else {}
    identity = account.get("identity") if isinstance(account.get("identity"), dict) else {}
    if health.get("available") is True or identity.get("available") is True:
        return CAPABILITY_OBSERVED
    return CAPABILITY_UNKNOWN


def _account_of(entry) -> dict:
    snapshot = entry.get("snapshot") if isinstance(entry, dict) else None
    if not isinstance(snapshot, dict):
        return {}
    account = snapshot.get("account")
    return account if isinstance(account, dict) else {}


def _finite_non_negative(value) -> bool:
    """A money figure must be a real, finite, non-negative number.

    A bool is not a number. NaN/Infinity are not measurements. A negative
    balance is possible on some accounts but a negative EQUITY with a positive
    balance is not coherent, so the caller checks the pair, not just the value.
    """
    import math
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value >= 0


def account_admissible(entry, *, expected_account=None, expected_server=None
                       ) -> tuple[bool, tuple]:
    """May this node's account observation become broker truth?

    Returns `(admissible, reasons)`. Every refusal names itself; the checker and
    the UI both print the code rather than a generic failure.
    """
    reasons: list[str] = []
    if classify_payload(entry) != CLASS_CANONICAL:
        return False, (R_UNUSABLE_PAYLOAD,)

    account = _account_of(entry)
    health = account.get("health") if isinstance(account.get("health"), dict) else {}
    identity = account.get("identity") if isinstance(account.get("identity"), dict) else {}

    # `is True`, never truthiness: a missing key reads None and must not pass.
    if not (health.get("available") is True or identity.get("available") is True):
        return False, (R_ACCOUNT_NOT_OBSERVED,)

    # Identity binding. An account SWITCH mid-deployment is the condition this
    # exists to catch — same shape, different money.
    #
    # ABSENT IDENTITY IS A REFUSAL, NOT A PASS. The first version of this read
    # `if expected_account and fingerprint and fingerprint != expected_account`,
    # which meant a node publishing `health.available: true` with
    # `identity.available: false` delivered real balances into a fully pinned
    # deployment with `admitted: True` and no reasons at all — the exact
    # condition the pin exists to catch, defeated by omitting a block. When a
    # pin is set, an unverifiable identity fails closed.
    fingerprint = identity.get("fingerprint")
    if expected_account:
        if not fingerprint:
            reasons.append(R_ACCOUNT_IDENTITY_UNVERIFIABLE)
        elif fingerprint != expected_account:
            reasons.append(R_ACCOUNT_IDENTITY_MISMATCH)
    server = identity.get("server")
    if expected_server:
        if not server:
            reasons.append(R_ACCOUNT_IDENTITY_UNVERIFIABLE)
        elif server != expected_server:
            reasons.append(R_ACCOUNT_SERVER_MISMATCH)

    if health.get("available") is True:
        balance, equity = health.get("balance"), health.get("equity")
        # Equity absent while balance exists is NOT a refusal — the projection
        # reports equity as null and renders "—". It IS a refusal when a value
        # is present but is not a real measurement.
        for value in (balance, equity, health.get("free_margin")):
            if value is not None and not _finite_non_negative(value):
                reasons.append(R_NUMERIC_INVALID)
                break

    return (not reasons), tuple(reasons)


def expected_identity() -> tuple:
    """The account this deployment is pinned to, from the environment.

    Returns `(account, server)`, either of which may be None — an UNPINNED
    deployment, which is the default and is not an error. It is, however, a
    deployment that cannot detect an account switch, which is why the checker
    reports an unpinned expectation as WARN and never as PASS.

    Read on every call rather than captured at import, so the projection follows
    the process environment instead of whatever it happened to be when the
    module was first loaded.
    """
    import os
    account = (os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip() or None
    if account is None:
        account = (os.environ.get(VAR_EXECUTION_EXPECTED_ACCOUNT) or "").strip() or None
    server = (os.environ.get(VAR_EXPECTED_SERVER) or "").strip() or None
    return account, server


def expected_account_pins_disagree() -> bool:
    """Both account pins are set and name DIFFERENT accounts.

    A deployment in this state believes it is pinned twice and is pinned to two
    things. The checker reports it as a FAIL; nothing about the runtime can
    resolve it, because neither name is more correct than the other.
    """
    import os
    a = (os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip()
    b = (os.environ.get(VAR_EXECUTION_EXPECTED_ACCOUNT) or "").strip()
    return bool(a) and bool(b) and a != b


def verdict(entry, *, expected_account=None, expected_server=None) -> ActivationVerdict:
    """The full six-permission verdict for one stored observation."""
    payload_class = classify_payload(entry)
    if payload_class == CLASS_UNUSABLE:
        return ActivationVerdict(payload_class=payload_class,
                                 reasons=(R_UNUSABLE_PAYLOAD,))

    snapshot = entry.get("snapshot") or {}
    stale = bool(entry.get("stale", True))
    degraded_reasons = nodeprov.degraded_reasons(snapshot)
    lifecycle = nodeprov.classify_lifecycle(snapshot, stale=stale)

    admissible, account_reasons = account_admissible(
        entry, expected_account=expected_account, expected_server=expected_server)

    mt5 = nodeprov.mt5_connection_observation(snapshot)
    warnings: list[str] = []
    # The node claiming an MT5 read while supplying no account, or supplying an
    # account while reporting its broker snapshot unreadable, is a genuine
    # contradiction. Surface it; do not pick a side.
    if mt5 == "observed" and not admissible and R_ACCOUNT_NOT_OBSERVED in account_reasons:
        warnings.append(R_MT5_CONTRADICTION)
    if mt5 == "unreachable" and admissible:
        warnings.append(R_MT5_CONTRADICTION)

    reasons = list(account_reasons)
    if stale:
        reasons.append(R_STALE)
    if degraded_reasons:
        reasons.append(R_NODE_DEGRADED)

    return ActivationVerdict(
        payload_class=payload_class,
        capability=capability_signal(entry),
        # A node that published at all is visible. Staleness and degradation are
        # STATES OF A VISIBLE NODE, never reasons to hide it — M-NODE-READ-1.
        node_identity_visible=True,
        node_mt5_observation_visible=mt5 is not None,
        node_account_admissible=admissible,
        # Broker truth requires an admissible account. Node health alone never
        # reaches this line.
        broker_truth_admissible=admissible,
        # Stale genuine data may still drive analytics (it is real, just old);
        # a degraded node's account may not, because the node is telling us its
        # own view of the world is compromised.
        analytics_input_admissible=admissible and not degraded_reasons,
        execution_authority=False,
        stale=stale,
        degraded=bool(degraded_reasons),
        reasons=tuple(reasons),
        warnings=tuple(warnings) + tuple(degraded_reasons),
    )


def reconcile_account_sources(node_accounts, local_accounts) -> tuple[tuple, tuple]:
    """Choose between node-relayed and locally-read accounts — or refuse to.

    Returns `(admitted, warnings)`.

    PRECEDENCE IS BY AUTHORITY, NOT LOCALITY (M-NODE-READ-1). A local read wins
    only if it is genuine broker truth; `mock-fixture` is not, so it never
    shadows a genuine node observation.

    CONTRADICTIONS ARE NOT RESOLVED. When a local MT5 read and a node MT5 read
    describe the SAME account with different money, both are kept and a warning
    is raised. Averaging them would invent a third figure nobody observed;
    silently preferring one would hide that the system has two answers. The
    operator needs to know the sources disagree far more than they need a
    number.
    """
    genuine_local = tuple(a for a in local_accounts
                          if brokerprov.is_broker_truth(getattr(a, "provenance", None)))
    if not genuine_local:
        return tuple(node_accounts), ()

    admitted = tuple((*node_accounts, *genuine_local))
    # ONE implementation of the contradiction rule, not two. This used to derive
    # its own warnings here while `build_summary` derived different ones from the
    # projected records; two implementations of one rule is how they stop
    # agreeing. Both now call `account_contradictions`.
    return admitted, account_contradictions(admitted)


#: Two observations of one account taken this far apart are not comparable.
#: Equity moves continuously with open positions and balance moves whenever a
#: trade closes, so a difference between distant samples is DRIFT, not
#: disagreement.
MAX_COMPARABLE_OBSERVATION_GAP_S = 60.0

#: Facts about an account's IDENTITY. Two genuine sources must never disagree
#: about these regardless of when they looked.
_IDENTITY_LABELS = ("server", "currency")
#: Facts about an account's MONEY. Compared only between near-simultaneous
#: observations. `equity` is deliberately ABSENT: it changes on every tick that
#: moves an open position, so comparing it across observers would raise a
#: permanent warning on a correct two-node deployment — the alarm fatigue this
#: module's own docstring warns about.
_MONEY_LABELS = ("balance",)


def _observation_gap(group) -> float | None:
    """Seconds between the earliest and latest `observed_at` in a group.

    None when any member did not state when it looked, which is treated as
    "cannot tell" — money is then not compared, because a comparison that
    cannot be dated cannot distinguish disagreement from drift.
    """
    from datetime import datetime
    stamps = []
    for account in group:
        raw = getattr(account, "observed_at", None)
        if not isinstance(raw, str) or not raw.strip():
            return None
        try:
            stamps.append(datetime.fromisoformat(raw.strip().replace("Z", "+00:00")))
        except ValueError:
            return None
    if any(s.tzinfo is None for s in stamps):
        return None
    return (max(stamps) - min(stamps)).total_seconds()


def account_contradictions(accounts) -> tuple:
    """Warnings derived from the accounts that were ACTUALLY PROJECTED.

    Deliberately computed from the output rather than passed along a call chain,
    for two reasons.

    It catches contradictions that source reconciliation never sees. Two NODES
    reporting the same account with different balances (matrix case 14) never
    reach the local-versus-node comparison at all — both are node-relayed, both
    are genuine, and the reconciler has no opinion about them. Derived from the
    output, that case is the same shape as any other and needs no separate rule.

    And it cannot drift from what the operator is looking at. A warning computed
    beside the records it describes stays true when the projection changes; one
    threaded through a call chain quietly stops matching.

    Only admitted `is_broker_truth` records participate: a mock record
    disagreeing with a genuine one is not a contradiction, and neither is a
    record the identity pin already refused — both are the gate working, and
    warning about them would fire every time it did its job.
    """
    genuine = [a for a in accounts
               if brokerprov.is_broker_truth(getattr(a, "provenance", None))
               and getattr(a, "admitted", True)]
    grouped: dict = {}
    for account in genuine:
        # An unidentified account groups with the other unidentified ones rather
        # than with nothing. Two genuine sources reporting money for an account
        # nobody can name is MORE alarming than two naming the same one, so
        # dropping them would have hidden the worse case.
        key = getattr(account, "account_fingerprint", None) or "<unidentified>"
        grouped.setdefault(key, []).append(account)

    out = []
    for fingerprint, group in sorted(grouped.items()):
        if len(group) < 2:
            continue
        sources = ",".join(sorted(getattr(a, "node_id", None) or "local" for a in group))
        if fingerprint == "<unidentified>":
            out.append(
                f"{R_CONTRADICTORY_SOURCES}: {len(group)} genuine sources "
                f"({sources}) report money for an account none of them identified")
            continue

        gap = _observation_gap(group)
        comparable = _IDENTITY_LABELS + (
            _MONEY_LABELS if gap is not None and gap <= MAX_COMPARABLE_OBSERVATION_GAP_S
            else ())
        for label in comparable:
            values = {getattr(a, label, None) for a in group}
            values.discard(None)             # unreported is not disagreement
            if len(values) > 1:
                when = "" if label in _IDENTITY_LABELS else f" within {gap:.0f}s"
                out.append(
                    f"{R_CONTRADICTORY_SOURCES}: {len(group)} genuine sources "
                    f"({sources}) report different {label} for the same account{when}")
    return tuple(out)


def node_and_broker_authority_are_disjoint() -> bool:
    """A property, asserted at runtime as well as in tests.

    The two admitted provenance sets must never intersect. If they ever do, a
    heartbeat has become a balance and every downstream gate is compromised.
    """
    return not (nodeprov.AUTHORITATIVE_NODE_PROVENANCE
                & {brokerprov.PROV_LIVE_MT5, brokerprov.PROV_NODE_MT5})
