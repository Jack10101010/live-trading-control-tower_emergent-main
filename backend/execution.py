"""
Execution Orchestration layer (Phase 8).

Inserts a dedicated orchestrator between the Command Layer and the Broker:

    Command → ExecutionOrchestrator → Broker

The orchestrator owns ALL execution policy — validation, pre-checks, policy
evaluation, sequencing, dry-run, audit metadata, and dispatch. The broker ONLY
executes; the runtime overlay remains the source of runtime truth.

`execution.py` imports nothing from `server.py` and contains ZERO broker-specific
logic. The runtime injects an `ExecutionEnv` (read-only accessors) and a `dispatch`
callable (which routes to the broker or runtime control-plane); the orchestrator
never reaches into either.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Pipeline stages (Objective 2) — every stage is observable.
# ---------------------------------------------------------------------------

STAGE_RECEIVED = "received"
STAGE_VALIDATED = "validated"
STAGE_POLICY_CHECKED = "policy_checked"
STAGE_BROKER_DISPATCH = "broker_dispatch"
STAGE_BROKER_RESULT = "broker_result"
STAGE_RUNTIME_UPDATE = "runtime_update"
STAGE_AUDIT_EVENT = "audit_event"
STAGE_COMPLETED = "completed"


@dataclass
class ValidationResult:
    ok: bool
    code: str = ""
    message: str = ""


@dataclass
class PolicyResult:
    allowed: bool
    policy: str = ""
    reason: str = ""


@dataclass
class ExecutionResult:
    status: str  # "completed" | "rejected" (validation) | "denied" (policy)
    stages: list
    timeline: dict
    dryRun: bool = False
    before: dict | None = None
    after: dict | None = None
    stage: str = STAGE_COMPLETED
    reason: str = ""
    code: str = ""


# ---------------------------------------------------------------------------
# Execution environment — read-only accessors the rules/policies evaluate over.
# Injected by the runtime; keeps this module decoupled and broker-agnostic.
# ---------------------------------------------------------------------------

@dataclass
class ExecutionEnv:
    deployment: Callable[[str], dict | None]
    trade: Callable[[str], dict | None]
    order: Callable[[str], dict | None]
    active_package_version: Callable[[], Any]
    broker_connection: Callable[[], str]
    broker_capabilities: Callable[[], dict]


# ---------------------------------------------------------------------------
# Reusable validation rules (Objective 3). Broker performs NONE of this.
# Each rule: (payload, env) -> ValidationResult.
# ---------------------------------------------------------------------------

def v_deployment_exists(payload: dict, env: ExecutionEnv) -> ValidationResult:
    if env.deployment(payload.get("deploymentId") or ""):
        return ValidationResult(True)
    return ValidationResult(False, "deployment_not_found", "deployment does not exist")


def v_trade_exists(payload: dict, env: ExecutionEnv) -> ValidationResult:
    if env.trade(payload.get("tradeId") or ""):
        return ValidationResult(True)
    return ValidationResult(False, "trade_not_found", "trade does not exist")


def v_order_exists(payload: dict, env: ExecutionEnv) -> ValidationResult:
    if env.order(payload.get("orderId") or ""):
        return ValidationResult(True)
    return ValidationResult(False, "order_not_found", "order does not exist")


def v_broker_connected(payload: dict, env: ExecutionEnv) -> ValidationResult:
    if env.broker_connection() == "Connected":
        return ValidationResult(True)
    return ValidationResult(False, "broker_disconnected", "broker is not connected")


def v_package_active(payload: dict, env: ExecutionEnv) -> ValidationResult:
    if env.active_package_version() is not None:
        return ValidationResult(True)
    return ValidationResult(False, "no_active_package", "no active package")


def v_capability(cap: str) -> Callable[[dict, ExecutionEnv], ValidationResult]:
    def rule(payload: dict, env: ExecutionEnv) -> ValidationResult:
        if env.broker_capabilities().get(cap):
            return ValidationResult(True)
        return ValidationResult(False, f"missing_capability", f"broker lacks {cap}")
    return rule


# ---------------------------------------------------------------------------
# Reusable execution policies (Objective 4). Pure rule evaluation only.
# ---------------------------------------------------------------------------

class Policy:
    name = "policy"

    def evaluate(self, payload: dict, env: ExecutionEnv) -> PolicyResult:  # pragma: no cover
        return PolicyResult(True, self.name)


class CanPauseDeployment(Policy):
    name = "CanPauseDeployment"

    def evaluate(self, payload, env):
        d = env.deployment(payload.get("deploymentId") or "")
        if not d:
            return PolicyResult(False, self.name, "deployment missing")
        if d.get("status") == "Locked":
            return PolicyResult(False, self.name, "locked deployments cannot be paused")
        return PolicyResult(True, self.name)


class CanResumeDeployment(Policy):
    name = "CanResumeDeployment"

    def evaluate(self, payload, env):
        d = env.deployment(payload.get("deploymentId") or "")
        return PolicyResult(bool(d), self.name, "" if d else "deployment missing")


class CanCloseTrade(Policy):
    name = "CanCloseTrade"

    def evaluate(self, payload, env):
        t = env.trade(payload.get("tradeId") or "")
        if not t:
            return PolicyResult(False, self.name, "trade missing")
        if t.get("state") == "closed":
            return PolicyResult(False, self.name, "trade already closed")
        return PolicyResult(True, self.name)


class CanModifyOrder(Policy):
    name = "CanModifyOrder"

    def evaluate(self, payload, env):
        o = env.order(payload.get("orderId") or "")
        if not o:
            return PolicyResult(False, self.name, "order missing")
        if not env.broker_capabilities().get("supportsModify"):
            return PolicyResult(False, self.name, "broker lacks supportsModify")
        return PolicyResult(True, self.name)


class CanFlattenDeployment(Policy):
    name = "CanFlattenDeployment"

    def evaluate(self, payload, env):
        d = env.deployment(payload.get("deploymentId") or "")
        return PolicyResult(bool(d), self.name, "" if d else "deployment missing")


class CanRollbackPackage(Policy):
    name = "CanRollbackPackage"

    def evaluate(self, payload, env):
        if env.active_package_version() is None:
            return PolicyResult(False, self.name, "no active package to roll back")
        return PolicyResult(True, self.name)


# Command → (validations, policy). Uncovered commands pass through (validated only
# by the known-command vocabulary + the effect's own None-guards) so all existing
# commands keep working; the key operator commands get real orchestration.
COMMAND_SPEC: dict[str, tuple[list, Policy | None]] = {
    "PauseDeployment": ([v_deployment_exists], CanPauseDeployment()),
    "ResumeDeployment": ([v_deployment_exists], CanResumeDeployment()),
    "LockDeployment": ([v_deployment_exists], None),
    "UnlockDeployment": ([v_deployment_exists], None),
    "KillDeployment": ([v_deployment_exists], None),
    "FlattenDeployment": ([v_deployment_exists], CanFlattenDeployment()),
    "RedeployManifest": ([v_deployment_exists], None),
    "RollbackPackage": ([v_package_active], CanRollbackPackage()),
    "DeployPackage": ([v_package_active], None),
    "CloseTrade": ([v_trade_exists, v_broker_connected], CanCloseTrade()),
    "PartialClose": ([v_trade_exists], None),
    "ReduceTradeRisk": ([v_trade_exists], None),
    "SLToBE": ([v_trade_exists], None),
    "MoveTradeSL": ([v_trade_exists], None),
    "MoveTradeTP": ([v_trade_exists], None),
    "SetAutoManagement": ([v_trade_exists], None),
    "CancelOrder": ([v_order_exists], None),
    "ReduceOrderRisk": ([v_order_exists], CanModifyOrder()),
    "ConvertOrderToGhost": ([v_order_exists], None),
}


# ---------------------------------------------------------------------------
# Health metrics (Objective 7).
# ---------------------------------------------------------------------------

@dataclass
class ExecutionMetrics:
    executions: int = 0
    total_ms: float = 0.0
    broker_ms_total: float = 0.0
    validation_failures: int = 0
    policy_failures: int = 0
    last_execution: dict | None = None

    def record(self, name: str, result: ExecutionResult, now: str) -> None:
        self.executions += 1
        self.total_ms += result.timeline.get("totalMs", 0.0)
        self.broker_ms_total += result.timeline.get("brokerMs", 0.0)
        self.last_execution = {
            "command": name, "status": result.status, "dryRun": result.dryRun,
            "totalMs": result.timeline.get("totalMs", 0.0), "at": now,
        }

    def health(self, dry_run_enabled: bool) -> dict:
        avg = round(self.total_ms / self.executions, 3) if self.executions else 0.0
        broker_avg = round(self.broker_ms_total / self.executions, 3) if self.executions else 0.0
        return {
            "orchestratorHealthy": True,
            "executions": self.executions,
            "lastExecution": self.last_execution,
            "averageExecutionMs": avg,
            "brokerLatencyMs": broker_avg,
            "dryRunEnabled": dry_run_enabled,
            "validationFailures": self.validation_failures,
            "policyFailures": self.policy_failures,
        }


# ---------------------------------------------------------------------------
# Orchestrator (Objectives 1, 2, 5, 6).
# ---------------------------------------------------------------------------

class ExecutionOrchestrator:
    def __init__(self, *, dispatch: Callable[[str, dict, str, bool], tuple],
                 env_factory: Callable[[], ExecutionEnv], metrics: ExecutionMetrics):
        # dispatch(name, payload, now, dry_run) -> (before, after). The runtime
        # supplies it; the orchestrator neither knows nor cares whether it routes
        # to the broker or the runtime control-plane.
        self._dispatch = dispatch
        self._env_factory = env_factory
        self.metrics = metrics

    def _validate(self, name, payload, env) -> ValidationResult:
        for rule in COMMAND_SPEC.get(name, ([], None))[0]:
            res = rule(payload, env)
            if not res.ok:
                return res
        return ValidationResult(True)

    def _policy(self, name, payload, env) -> PolicyResult:
        policy = COMMAND_SPEC.get(name, ([], None))[1]
        if policy is None:
            return PolicyResult(True, "none")
        return policy.evaluate(payload, env)

    def execute(self, name: str, payload: dict, now: str, dry_run: bool = False) -> ExecutionResult:
        stages: list = [{"stage": STAGE_RECEIVED, "ok": True}]
        timeline: dict = {}
        env = self._env_factory()

        t = time.perf_counter()
        val = self._validate(name, payload, env)
        timeline["validationMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_VALIDATED, "ok": val.ok, "detail": val.message})
        if not val.ok:
            self.metrics.validation_failures += 1
            return ExecutionResult(status="rejected", stages=stages, timeline=timeline,
                                   dryRun=dry_run, stage=STAGE_VALIDATED, reason=val.message, code=val.code)

        t = time.perf_counter()
        pol = self._policy(name, payload, env)
        timeline["policyMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_POLICY_CHECKED, "ok": pol.allowed, "detail": pol.reason, "policy": pol.policy})
        if not pol.allowed:
            self.metrics.policy_failures += 1
            return ExecutionResult(status="denied", stages=stages, timeline=timeline,
                                   dryRun=dry_run, stage=STAGE_POLICY_CHECKED, reason=pol.reason, code=pol.policy)

        stages.append({"stage": STAGE_BROKER_DISPATCH, "ok": True, "dryRun": dry_run})
        t = time.perf_counter()
        before, after = self._dispatch(name, payload, now, dry_run)
        timeline["brokerMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_BROKER_RESULT, "ok": True})
        stages.append({"stage": STAGE_RUNTIME_UPDATE, "ok": True, "persisted": not dry_run})
        stages.append({"stage": STAGE_AUDIT_EVENT, "ok": True})
        stages.append({"stage": STAGE_COMPLETED, "ok": True})

        timeline["totalMs"] = round(sum(v for v in timeline.values()), 3)
        result = ExecutionResult(status="completed", stages=stages, timeline=timeline,
                                 dryRun=dry_run, before=before, after=after)
        self.metrics.record(name, result, now)
        return result
