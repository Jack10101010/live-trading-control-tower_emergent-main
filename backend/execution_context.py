"""ARCH-2/ARCH-3 — the canonical execution context.

One immutable model of everything needed to evaluate and execute an intent,
assembled ONCE at the execution boundary and consumed by BOTH the safety gate and
the orchestrator. No component downstream reconstructs safety state independently,
and no arbitrary mutable dictionary crosses into the safety-critical core
(capabilities travel as a frozenset of enabled capability names).

AUTHORITY SEPARATION (ARCH-3):

  * NODE-AUTHORITATIVE facts — the node owns broker/account safety and live
    arming (invariant I-7). They arrive as OBSERVED telemetry in the `node` fact
    group, never recomputed. The tower may derive *staleness* from timestamps but
    never invents node health or arming.
  * TOWER-DERIVED facts — operator identity (authenticated), the operator's
    COMMAND AUTHORIZATION (an immutable, scoped, bounded
    `command_authorization.AuthorizationGrant` — NOT a generic armed boolean and
    NOT node arming), execution mode, and the durable reconciliation posture.
  * BROKER-ADAPTER facts — active adapter kind, connection, capabilities.
  * PROVENANCE — every fact group records where it came from
    (`mock-synthetic` / `node-telemetry` / `durable-store` / `absent`), so a mock
    context is explicitly labelled and can never masquerade as live truth.

FAIL-CLOSED DEFAULTS: an unassembled/default context denies — observe mode, no
grant, unknown node, account unknown, no capabilities.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import command_authorization as ca
import execution_safety as safety
import security_config

# ── provenance labels ─────────────────────────────────────────────────────────
PROV_MOCK = "mock-synthetic"
PROV_NODE_TELEMETRY = "node-telemetry"
PROV_DURABLE_STORE = "durable-store"
PROV_ABSENT = "absent"


@dataclass(frozen=True)
class ReconciliationFacts:
    """Tower-derived reconciliation posture, from the canonical authority."""
    critical_unresolved: bool = False
    stale: bool = False
    last_run_id: str | None = None
    last_run_at: str | None = None


@dataclass(frozen=True)
class NodeFacts:
    """Node-authoritative facts, OBSERVED from canonical telemetry. Defaults are
    the absent/deny representation. The tower derives `stale` from the node's own
    published timestamp; everything else is carried verbatim."""
    instance_id: str | None = None
    health: str = safety.NODE_UNKNOWN            # node_client state vocabulary
    published_at: str | None = None
    age_seconds: float | None = None
    stale: bool = True                           # absent telemetry is stale
    arming_status: str | None = None             # node's own vocabulary, verbatim
    arming_armed: bool | None = None
    arming_expires_at: str | None = None
    account_fingerprint: str | None = None
    provenance: str = PROV_ABSENT


@dataclass(frozen=True)
class ExecutionContext:
    """The one immutable context for evaluating and executing an intent."""

    # -- identity / lineage ----------------------------------------------------
    command_id: str | None = None
    command_name: str | None = None       # canonical registry name
    correlation_id: str | None = None
    idempotency_key: str | None = None
    source: str = "operator"

    # -- targets ---------------------------------------------------------------
    deployment_id: str | None = None
    account_id: str | None = None
    instrument: str | None = None

    # -- tower-derived execution posture ---------------------------------------
    execution_mode: str = safety.MODE_OBSERVE
    operator: safety.OperatorAuthorization = field(
        default_factory=safety.OperatorAuthorization)
    #: Tower-side operator COMMAND AUTHORIZATION — an immutable, scoped, bounded
    #: grant (None = unauthorized). NOT node arming, and not a bare boolean.
    authorization: ca.AuthorizationGrant | None = None
    reconciliation: ReconciliationFacts = field(default_factory=ReconciliationFacts)
    #: Account-identity evaluation the assembler performed
    #: (execution_safety.ACCOUNT_* vocabulary). Default: evaluated-and-unknown.
    account_identity_state: str = safety.ACCOUNT_UNKNOWN

    # -- node-authoritative facts (observed, never recomputed) -----------------
    node: NodeFacts = field(default_factory=NodeFacts)

    # -- broker facts (adapter-reported) ---------------------------------------
    broker_kind: str | None = None
    broker_connection: str | None = None
    #: Enabled capability names as an immutable set — no mutable dict in the core.
    broker_capabilities: frozenset = frozenset()

    # -- provenance per fact group ---------------------------------------------
    provenance: tuple = field(default_factory=tuple)   # (("tower", ...), ("node", ...))

    # -- time ------------------------------------------------------------------
    observed_at: str | None = None
    expires_at: str | None = None

    #: UES: injected `(instrument) -> financial evaluator` factory, supplied by
    #: the composition root for a LIVE adapter. Kept as an opaque callable so
    #: this module imports no rails and owns no financial policy.
    financial_rails_factory: Any = None

    def _authorization_window(self, command_type: str | None,
                              now: datetime) -> safety.ArmingState:
        """Derive the safety-gate authorization window for THIS command from the
        grant: exact risk-class + scope matching, bounded lifetime, deny by
        default. No grant (or no coverage) = disarmed."""
        if self.authorization is None:
            return safety.ArmingState()
        risk_class = None
        if command_type is not None:
            risk_class = safety.classify(command_type)
        # The window only matters for execution-affecting commands (the safety
        # engine checks arming only there); derive coverage for that class.
        target_class = risk_class or safety.RISK_EXECUTION_AFFECTING
        scope = self.deployment_id or self.account_id
        if self.authorization.authorizes(risk_class=target_class, scope=scope, now=now):
            return safety.ArmingState(armed=True,
                                      armed_by=self.authorization.provider,
                                      expires_at=self.authorization.expires_at)
        return safety.ArmingState()

    def to_safety_context(self, command_type: str | None = None,
                          now: datetime | None = None,
                          instrument: str | None = None) -> safety.SafetyContext:
        """Derive the safety-gate view of THIS context (the gate and the
        orchestrator therefore evaluate identical facts). Stale node telemetry
        maps to the STALE node state — stale facts can never authorize.

        UES: `financial_rails_factory` — when the runtime supplied one — is
        invoked here with the command's instrument to produce the financial-rail
        evaluator. It is a callable injected by the composition root, so this
        module keeps importing nothing from `live/` and stays a pure derivation
        of facts it was handed.
        """
        clock = now or datetime.now(timezone.utc)
        node_health = self.node.health or safety.NODE_UNKNOWN
        if self.node.provenance == PROV_NODE_TELEMETRY and self.node.stale \
                and node_health == safety.NODE_HEALTHY:
            node_health = safety.NODE_STALE          # derived staleness, never invented health
        return safety.SafetyContext(
            mode=self.execution_mode,
            arming=self._authorization_window(command_type, clock),
            node=safety.NodeSafety(node_health),
            operator=self.operator,
            reconciliation=safety.ReconciliationSafety(
                critical_unresolved=self.reconciliation.critical_unresolved,
                stale=self.reconciliation.stale,
            ),
            account=safety.AccountSafety(self.account_identity_state),
            financial=self._financial_evaluator(instrument),
        )

    def _financial_evaluator(self, instrument: str | None):
        """The injected financial-rail evaluator for this command, or None.

        Total by construction: if the factory is absent or raises, this returns
        None, which applies NO financial gate. That is deliberate — the factory
        only exists on a host with live config, and a Control Tower running
        against the mock world must not be denied because rails it never needed
        could not be built. The refusal for a LIVE adapter whose rails failed to
        construct is made inside the evaluator itself, where it can be explicit.
        """
        factory = self.financial_rails_factory
        if factory is None:
            return None
        try:
            return factory(instrument)
        except Exception:                                       # noqa: BLE001
            return None

    def safe_view(self) -> dict:
        """Redaction-safe audit representation."""
        return {
            "commandId": self.command_id,
            "commandName": self.command_name,
            "correlationId": self.correlation_id,
            "idempotencyKey": self.idempotency_key,
            "source": self.source,
            "deploymentId": self.deployment_id,
            "accountId": self.account_id,
            "instrument": self.instrument,
            "executionMode": self.execution_mode,
            "operatorIdentified": self.operator.identified,
            "operatorConfirmed": self.operator.confirmed,
            "authorization": self.authorization.safe_view() if self.authorization else None,
            "accountIdentityState": self.account_identity_state,
            "reconciliation": {
                "criticalUnresolved": self.reconciliation.critical_unresolved,
                "stale": self.reconciliation.stale,
                "lastRunId": self.reconciliation.last_run_id,
                "lastRunAt": self.reconciliation.last_run_at,
            },
            "node": {
                "instanceId": self.node.instance_id,
                "health": self.node.health,
                "publishedAt": self.node.published_at,
                "ageSeconds": self.node.age_seconds,
                "stale": self.node.stale,
                "armingStatus": self.node.arming_status,
                "armingArmed": self.node.arming_armed,
                "armingExpiresAt": self.node.arming_expires_at,
                "accountFingerprint": security_config.redact_text(
                    self.node.account_fingerprint) if self.node.account_fingerprint else None,
                "provenance": self.node.provenance,
            },
            "brokerKind": self.broker_kind,
            "brokerConnection": self.broker_connection,
            "brokerCapabilities": sorted(self.broker_capabilities),
            "provenance": dict(self.provenance),
            "observedAt": self.observed_at,
            "expiresAt": self.expires_at,
        }


def capabilities_from_mapping(caps: Any) -> frozenset:
    """Freeze a capability mapping ({name: bool}) into the context's immutable
    representation: the set of ENABLED capability names. Anything malformed
    freezes to empty (fail-closed: no capability is assumed)."""
    if not isinstance(caps, dict):
        return frozenset()
    return frozenset(str(k) for k, v in caps.items() if v is True)
