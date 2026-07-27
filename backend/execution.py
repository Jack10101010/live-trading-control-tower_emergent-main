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
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Any, Callable

import command_registry as registry
import execution_safety as safety


# ---------------------------------------------------------------------------
# Pipeline stages (ARCH-1) — the canonical, explicit execution pipeline:
#   Validate -> Safety -> Resolve -> Broker Dispatch -> Broker Result -> Audit
# No stage may be skipped and no transition is implicit.
# ---------------------------------------------------------------------------

STAGE_RECEIVED = "received"
STAGE_VALIDATED = "validated"      # Validate: command known + structural pre-checks
STAGE_SAFETY = "safety"            # Safety:   execution_safety.evaluate() authorization
STAGE_RESOLVED = "resolved"        # Resolve:  per-command feasibility policy
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
    status: str  # "completed" | "rejected" (validate) | "denied" (safety/resolve)
    stages: list
    timeline: dict
    dryRun: bool = False
    before: dict | None = None
    after: dict | None = None
    stage: str = STAGE_COMPLETED
    reason: str = ""
    code: str = ""
    safety: dict | None = None      # the redaction-safe SafetyDecision.safe_view()


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


# Command → (validators, feasibility policy). This is the FEASIBILITY layer — the
# per-command structural pre-checks and business rules (does the entity exist, is the
# deployment locked, is the trade already closed). It is DISTINCT from the safety
# authorization gate (`execution_safety.evaluate()`), which every command now passes
# through regardless of whether it appears here.
#
# The command SET is owned by `command_registry`; this table only supplies feasibility
# LOGIC for the commands that need extra rules. A registry command with no entry here
# has "no additional feasibility constraint" (an explicit, named resolution — never a
# silent allow), and is still gated by the safety stage.
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

# ARCH-1 uniqueness/derivation guard: every feasibility entry MUST name a command the
# canonical registry knows (so this table can never drift into a second vocabulary),
# and every broker-dispatched command must be classified execution-relevant by the
# registry. Failing loudly at import prevents a stray or misspelled command name.
_unknown = sorted(n for n in COMMAND_SPEC if not registry.is_known(n))
if _unknown:
    raise ValueError(f"COMMAND_SPEC names not in command_registry: {_unknown}")
for _name in registry.broker_dispatched_names():
    if registry.risk_class_of(_name) == safety.RISK_READ_ONLY:
        raise ValueError(f"broker-dispatched command {_name!r} classified read_only")


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

#: Explicit, named feasibility resolution for a command with no extra business rule.
#: Using a named constant instead of `None`-means-allow makes "missing policy" a
#: visible, deliberate outcome rather than a silent fall-through (the Audit A defect).
_NO_FEASIBILITY_CONSTRAINT = "no_additional_feasibility_constraint"


def _parse_now(now: str) -> datetime:
    try:
        dt = datetime.fromisoformat(str(now).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return datetime.now(timezone.utc)


class ExecutionOrchestrator:
    """The single execution authority. Every command that could reach broker dispatch
    is driven through the same explicit pipeline; nothing bypasses it.

        Validate -> Safety -> Resolve -> Broker Dispatch -> Broker Result -> Audit

    `safety_context_factory` supplies the `execution_safety.SafetyContext` for the
    current environment. The orchestrator itself calls `execution_safety.evaluate()`
    (it is not injected), so the Safety stage cannot be swapped out for a permissive
    stand-in — only the CONTEXT it evaluates against is supplied by the runtime.
    """

    def __init__(self, *, dispatch: Callable[[str, dict, str, bool], tuple],
                 env_factory: Callable[[], ExecutionEnv], metrics: ExecutionMetrics,
                 safety_context_factory: Callable[[], safety.SafetyContext]):
        # dispatch(name, payload, now, dry_run) -> (before, after). The runtime
        # supplies it; the orchestrator neither knows nor cares whether it routes
        # to the broker or the runtime control-plane.
        self._dispatch = dispatch
        self._env_factory = env_factory
        self.metrics = metrics
        self._safety_context_factory = safety_context_factory

    # ── Validate ───────────────────────────────────────────────────────────────
    def _validate(self, name, payload, env) -> ValidationResult:
        # Unknown commands are rejected here (deny-by-default at the vocabulary).
        if not registry.is_known(name):
            return ValidationResult(False, "unknown_command", f"unknown command: {name}")
        for rule in COMMAND_SPEC.get(name, ([], None))[0]:
            res = rule(payload, env)
            if not res.ok:
                return res
        return ValidationResult(True)

    # ── Safety (the authorization gate — always consulted) ───────────────────────
    def _safety(self, name, now: str) -> "safety.SafetyDecision":
        ctx = self._safety_context_factory()
        request = safety.CommandRequest(command_type=name)
        return safety.evaluate(request, ctx, now=_parse_now(now))

    # ── Resolve (per-command feasibility) ────────────────────────────────────────
    def _resolve(self, name, payload, env) -> PolicyResult:
        policy = COMMAND_SPEC.get(name, ([], None))[1]
        if policy is None:
            # Explicit, named "no extra constraint" — NOT a silent allow. The safety
            # stage above has already authorized; this records that the command
            # carries no additional feasibility rule.
            return PolicyResult(True, _NO_FEASIBILITY_CONSTRAINT)
        return policy.evaluate(payload, env)

    def execute(self, name: str, payload: dict, now: str, dry_run: bool = False) -> ExecutionResult:
        stages: list = [{"stage": STAGE_RECEIVED, "ok": True}]
        timeline: dict = {}
        env = self._env_factory()

        # 1) Validate
        t = time.perf_counter()
        val = self._validate(name, payload, env)
        timeline["validationMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_VALIDATED, "ok": val.ok, "detail": val.message})
        if not val.ok:
            self.metrics.validation_failures += 1
            return ExecutionResult(status="rejected", stages=stages, timeline=timeline,
                                   dryRun=dry_run, stage=STAGE_VALIDATED, reason=val.message, code=val.code)

        # 2) Safety — every command passes through execution_safety.evaluate(). There
        #    is no code path to broker dispatch that skips this gate.
        t = time.perf_counter()
        decision = self._safety(name, now)
        timeline["safetyMs"] = round((time.perf_counter() - t) * 1000, 3)
        safety_view = decision.safe_view()
        stages.append({"stage": STAGE_SAFETY, "ok": decision.allowed,
                       "detail": decision.reason, "riskClass": decision.risk_class})
        if not decision.allowed:
            self.metrics.policy_failures += 1
            return ExecutionResult(status="denied", stages=stages, timeline=timeline,
                                   dryRun=dry_run, stage=STAGE_SAFETY,
                                   reason=decision.reason, code=decision.reason, safety=safety_view)

        # 3) Resolve — per-command feasibility (locked? already closed? capability?)
        t = time.perf_counter()
        pol = self._resolve(name, payload, env)
        timeline["resolveMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_RESOLVED, "ok": pol.allowed, "detail": pol.reason, "policy": pol.policy})
        if not pol.allowed:
            self.metrics.policy_failures += 1
            return ExecutionResult(status="denied", stages=stages, timeline=timeline,
                                   dryRun=dry_run, stage=STAGE_RESOLVED, reason=pol.reason,
                                   code=pol.policy, safety=safety_view)

        # 4) Broker Dispatch  5) Broker Result  6) Audit
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
                                 dryRun=dry_run, before=before, after=after, safety=safety_view)
        self.metrics.record(name, result, now)
        return result
