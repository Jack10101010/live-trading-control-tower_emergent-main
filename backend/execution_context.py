"""ARCH-2 — the canonical execution context.

One immutable model of everything needed to evaluate and execute an intent,
assembled ONCE at the execution boundary and consumed by BOTH the safety gate and
the orchestrator. No component downstream reconstructs safety state independently,
and no arbitrary mutable dictionary crosses into the safety-critical core
(capabilities travel as a frozenset of enabled capability names).

AUTHORITY SEPARATION (resolves the tower/node arming ambiguity found in Audit A):

  * NODE-AUTHORITATIVE facts — the node owns broker/account safety and live
    arming (invariant I-7). The context carries them under `node_*` fields
    (`node_health`, `node_account_identity`) exactly as observed, never recomputed.
  * TOWER-DERIVED facts — the tower owns who is asking and whether the tower
    itself authorizes the command. These are named precisely:
      - `operator` — operator identity + explicit confirmation
        (`execution_safety.OperatorAuthorization`)
      - `command_authorization` — the tower-side, time-bounded authorization
        window (`execution_safety.ArmingState`). This is COMMAND AUTHORIZATION,
        not node arming: it never asserts anything about the node's arming
        session, account fingerprint or broker-side safety, and it is never
        populated from node telemetry as if it were the node's arming state.
  * RECONCILIATION — tower-derived, from the canonical reconciliation authority.
    Critical unresolved discrepancies deny new risk-increasing execution.

FAIL-CLOSED DEFAULTS: an unassembled/default context denies — observe mode,
unauthorized, unknown node, no capabilities. Stale or missing required facts map
to their denying representation (e.g. missing node health -> NODE_UNKNOWN).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import execution_safety as safety
import security_config


@dataclass(frozen=True)
class ReconciliationFacts:
    """Tower-derived reconciliation posture, from the canonical authority."""
    critical_unresolved: bool = False
    stale: bool = False
    last_run_id: str | None = None
    last_run_at: str | None = None


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
    #: Tower-side, time-bounded COMMAND authorization window. NOT node arming.
    command_authorization: safety.ArmingState = field(
        default_factory=safety.ArmingState)
    reconciliation: ReconciliationFacts = field(default_factory=ReconciliationFacts)

    # -- node-authoritative facts (observed, never recomputed) -----------------
    node_health: str = safety.NODE_UNKNOWN
    node_account_identity: str | None = None

    # -- broker facts (adapter-reported) ---------------------------------------
    broker_kind: str | None = None
    broker_connection: str | None = None
    #: Enabled capability names as an immutable set — no mutable dict in the core.
    broker_capabilities: frozenset = frozenset()

    # -- time ------------------------------------------------------------------
    observed_at: str | None = None
    expires_at: str | None = None

    def to_safety_context(self) -> safety.SafetyContext:
        """Derive the safety-gate view of THIS context. The gate and the
        orchestrator therefore evaluate identical facts — the context is the
        single assembly point."""
        return safety.SafetyContext(
            mode=self.execution_mode,
            arming=self.command_authorization,
            node=safety.NodeSafety(self.node_health or safety.NODE_UNKNOWN),
            operator=self.operator,
            reconciliation=safety.ReconciliationSafety(
                critical_unresolved=self.reconciliation.critical_unresolved,
                stale=self.reconciliation.stale,
            ),
        )

    def safe_view(self) -> dict:
        """Redaction-safe audit representation. Value-free where a value could be
        sensitive; the operator reference is masked (identity is proven by the
        authorization object, not displayed)."""
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
            "commandAuthorizationActive": bool(self.command_authorization.armed),
            "reconciliation": {
                "criticalUnresolved": self.reconciliation.critical_unresolved,
                "stale": self.reconciliation.stale,
                "lastRunId": self.reconciliation.last_run_id,
                "lastRunAt": self.reconciliation.last_run_at,
            },
            "nodeHealth": self.node_health,
            "nodeAccountIdentity": security_config.redact_text(
                self.node_account_identity) if self.node_account_identity else None,
            "brokerKind": self.broker_kind,
            "brokerConnection": self.broker_connection,
            "brokerCapabilities": sorted(self.broker_capabilities),
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
