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
from broker_adapter import ConnectionState
import order_lifecycle as lifecycle
from execution_store import StoreError


# ---------------------------------------------------------------------------
# Pipeline stages (ARCH-1, extended by ARCH-2) — the canonical, explicit pipeline:
#   Validate -> Safety -> Resolve -> Broker Dispatch -> Broker Result
#     -> Lifecycle Persistence -> Audit
# No stage may be skipped and no transition is implicit. For broker-dispatched
# commands the lifecycle stage is DURABLE (the execution store); a broker result
# cannot bypass it — if the store is unavailable the command is denied BEFORE
# dispatch, never executed without durability.
# ---------------------------------------------------------------------------

STAGE_RECEIVED = "received"
STAGE_VALIDATED = "validated"      # Validate: command known + structural pre-checks
STAGE_SAFETY = "safety"            # Safety:   execution_safety.evaluate() authorization
STAGE_RESOLVED = "resolved"        # Resolve:  per-command feasibility policy
STAGE_BROKER_DISPATCH = "broker_dispatch"
STAGE_BROKER_RESULT = "broker_result"
STAGE_LIFECYCLE = "lifecycle_persistence"  # ARCH-2: durable order-lifecycle record
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
    # LIVE-2: idempotent replay — True when a duplicate submission returned the
    # DURABLE outcome of the original intent instead of executing again.
    deduplicated: bool = False
    intent_id: str | None = None    # the durable intent this execution recorded


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
    if env.broker_connection() == ConnectionState.CONNECTED:   # canonical constant, not a literal
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


def v_market_order_payload(payload: dict, env: ExecutionEnv) -> ValidationResult:
    """LIVE-2 structural validation of a market-order payload. Mirrors the
    canonical `MarketOrderRequest` constraints so nothing malformed proceeds to
    intent creation (the request model re-validates at construction — defence in
    depth, single vocabulary)."""
    import math
    instrument = payload.get("instrument")
    if not (isinstance(instrument, str) and instrument.strip()):
        return ValidationResult(False, "invalid_instrument", "instrument is required")
    if payload.get("side") not in ("long", "short"):
        return ValidationResult(False, "invalid_side", "side must be long or short")
    qty = payload.get("quantity")
    if isinstance(qty, bool) or not isinstance(qty, (int, float)) \
            or not math.isfinite(qty) or qty <= 0:
        return ValidationResult(False, "invalid_quantity",
                                "quantity must be a finite positive number")
    for name in ("stopLoss", "takeProfit"):
        v = payload.get(name)
        if v is not None and (isinstance(v, bool) or not isinstance(v, (int, float))
                              or not math.isfinite(v) or v <= 0):
            return ValidationResult(False, "invalid_protective_level",
                                    f"{name} must be a finite positive price")
    return ValidationResult(True)


def v_approved_connection_profile(payload: dict, env: ExecutionEnv) -> ValidationResult:
    """LIVE-2: a live submission requires the active operating profile to be an
    APPROVED ConnectionPolicy profile. Unknown/unapproved profiles deny."""
    import connection_policy
    if connection_policy.active_profile() in connection_policy.APPROVED_PROFILES:
        return ValidationResult(True)
    return ValidationResult(False, "connection_profile_not_approved",
                            "the active connection profile is not approved for execution")


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
    # LIVE-2: the ONE executable broker operation. Structural payload validation,
    # broker connectivity, the explicit market-execution capability and an
    # APPROVED connection profile are all required BEFORE the safety gate even
    # sees the command; the safety gate then applies mode / identity /
    # confirmation / node / account / reconciliation / authorization.
    "SubmitMarketOrder": ([v_market_order_payload, v_broker_connected,
                           v_capability("supportsMarketExecution"),
                           v_approved_connection_profile], None),
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
                 safety_context_factory: Callable[[], Any],
                 store_factory: Callable[[], Any] | None = None):
        # dispatch(name, payload, now, dry_run) -> (before, after). The runtime
        # supplies it; the orchestrator neither knows nor cares whether it routes
        # to the broker or the runtime control-plane.
        #
        # safety_context_factory may return either the canonical ExecutionContext
        # (ARCH-2 — assembled once, consumed by safety AND orchestration) or a bare
        # SafetyContext (unit-test harnesses).
        #
        # store_factory returns the durable ExecutionStore (or None when
        # unavailable). ARCH-2: broker-dispatched commands REQUIRE the store — no
        # broker dispatch may occur that the durable lifecycle cannot record.
        self._dispatch = dispatch
        self._env_factory = env_factory
        self.metrics = metrics
        self._safety_context_factory = safety_context_factory
        self._store_factory = store_factory

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
        # ARCH-2/3: the canonical ExecutionContext is assembled ONCE at the boundary;
        # the safety gate consumes a view derived from that same context, including
        # the per-command authorization window (grant coverage is command-scoped).
        # A bare SafetyContext is accepted for unit-test harnesses.
        if hasattr(ctx, "to_safety_context"):
            ctx = ctx.to_safety_context(command_type=name, now=_parse_now(now))
        request = safety.CommandRequest(command_type=name)
        return safety.evaluate(request, ctx, now=_parse_now(now))

    # ── Lifecycle persistence (ARCH-2, broker-dispatched commands only) ──────────
    def _begin_intent(self, store, name: str, payload: dict, now: str,
                      command_id: str | None, idempotency_key: str | None):
        """Create + persist the durable OrderIntent for a broker-dispatched
        command, through `created -> validated`. Raises on any store failure."""
        kind = registry.intent_kind_of(name) or lifecycle.KIND_MODIFY
        intent = lifecycle.OrderIntent(
            intent_id=lifecycle.new_intent_id(),
            command_name=name,
            kind=kind,
            command_id=command_id,
            correlation_id=command_id,     # lineage: the command groups the records
            idempotency_key=idempotency_key,
            deployment_id=payload.get("deploymentId"),
            # LIVE-2: a submit-kind intent persists the full canonical request
            # facts (instrument/side/quantity/protective levels) — the durable
            # record IS the request of record.
            instrument=payload.get("instrument"),
            side=payload.get("side"),
            order_type="market" if kind == lifecycle.KIND_SUBMIT else None,
            quantity=payload.get("quantity", payload.get("size")),
            stop_loss=payload.get("stopLoss"),
            take_profit=payload.get("takeProfit"),
            source="operator",
            created_at=now,
            metadata={"payload": dict(payload)},
        )
        store.create_intent(intent, now=now)
        store.record_transition(intent.intent_id, lifecycle.VALIDATED, at=now,
                                reason="validation_passed")
        return intent

    # ── Resolve (per-command feasibility) ────────────────────────────────────────
    def _resolve(self, name, payload, env) -> PolicyResult:
        policy = COMMAND_SPEC.get(name, ([], None))[1]
        if policy is None:
            # Explicit, named "no extra constraint" — NOT a silent allow. The safety
            # stage above has already authorized; this records that the command
            # carries no additional feasibility rule.
            return PolicyResult(True, _NO_FEASIBILITY_CONSTRAINT)
        return policy.evaluate(payload, env)

    def execute(self, name: str, payload: dict, now: str, dry_run: bool = False,
                *, command_id: str | None = None,
                idempotency_key: str | None = None) -> ExecutionResult:
        stages: list = [{"stage": STAGE_RECEIVED, "ok": True}]
        timeline: dict = {}
        env = self._env_factory()

        # ARCH-2: broker-dispatched commands carry a durable order-intent lifecycle.
        broker_dispatched = registry.is_broker_dispatched(name)
        store = None
        intent = None

        # LIVE-2 IDEMPOTENCY: a duplicated broker-dispatched submission must
        # NEVER create two broker orders. The durable store is the authority
        # (restart-safe): if this idempotency key already produced an intent,
        # return that intent's DURABLE outcome deterministically — no second
        # dispatch, no re-validation of a decision already taken.
        if broker_dispatched and not dry_run and idempotency_key:
            dup_store = self._store_factory() if self._store_factory else None
            existing = None
            if dup_store is not None:
                try:
                    existing = dup_store.intent_by_idempotency_key(
                        idempotency_key, command_name=name)
                except Exception:
                    existing = None          # store trouble -> normal path denies later
            if existing is not None:
                stages.append({"stage": STAGE_LIFECYCLE, "ok": True,
                               "deduplicated": True,
                               "intentId": existing.get("intent_id")})
                stages.append({"stage": STAGE_COMPLETED, "ok": True})
                return ExecutionResult(
                    status="completed", stages=stages, timeline=timeline,
                    dryRun=dry_run, deduplicated=True,
                    intent_id=existing.get("intent_id"),
                    after={"intentId": existing.get("intent_id"),
                           "state": existing.get("state"),
                           "brokerRef": existing.get("broker_ref"),
                           "deduplicated": True})

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

        # 3b) ARCH-2 durability gate — immediately before dispatch: a
        #     broker-dispatched command whose lifecycle cannot be durably recorded
        #     is DENIED here, so no broker dispatch can ever occur that the durable
        #     store did not first record. (Dry-run commands execute no effect and
        #     persist no lifecycle.) The intent lifecycle therefore contains
        #     exactly the commands that were authorized; safety/feasibility
        #     denials are refused above and never reach the store.
        if broker_dispatched and not dry_run:
            store = self._store_factory() if self._store_factory else None
            if store is not None:
                try:
                    intent = self._begin_intent(store, name, payload, now,
                                                command_id, idempotency_key)
                except (StoreError, lifecycle.LifecycleError, ValueError) as exc:
                    store, intent = None, None
                    denial_detail = type(exc).__name__
            if store is None or intent is None:
                self.metrics.policy_failures += 1
                stages.append({"stage": STAGE_LIFECYCLE, "ok": False,
                               "detail": locals().get("denial_detail", "execution store unavailable")})
                return ExecutionResult(status="denied", stages=stages, timeline=timeline,
                                       dryRun=dry_run, stage=STAGE_LIFECYCLE,
                                       reason="execution_store_unavailable",
                                       code="execution_store_unavailable", safety=safety_view)

        # 4) Broker Dispatch  5) Broker Result  6) Lifecycle Persistence  7) Audit
        if intent is not None:
            # LIVE-2: the safety stage above is what authorized this intent — the
            # READY transition records that explicitly (`safety_allowed`).
            store.record_transition(intent.intent_id, lifecycle.READY, at=now,
                                    reason="safety_allowed")
            pending = lifecycle.PENDING_STATE_BY_KIND[intent.kind]
            store.record_transition(intent.intent_id, pending, at=now,
                                    reason="dispatching_to_adapter")
        stages.append({"stage": STAGE_BROKER_DISPATCH, "ok": True, "dryRun": dry_run})
        # LIVE-2: a submit-kind intent's canonical request needs the durable
        # intent id — thread it through the payload copy the dispatcher receives
        # (the dispatch signature stays unchanged; the original payload is not
        # mutated).
        dispatch_payload = payload
        if intent is not None and intent.kind == lifecycle.KIND_SUBMIT:
            dispatch_payload = {**payload, "intentId": intent.intent_id,
                                "idempotencyKey": idempotency_key,
                                "commandId": command_id}
        t = time.perf_counter()
        before, after = self._dispatch(name, dispatch_payload, now, dry_run)
        timeline["brokerMs"] = round((time.perf_counter() - t) * 1000, 3)
        stages.append({"stage": STAGE_BROKER_RESULT, "ok": True})
        if intent is not None:
            if intent.kind == lifecycle.KIND_SUBMIT:
                # LIVE-2: drive the market-order lifecycle from the canonical
                # BrokerResult the adapter answered (threaded through `after`).
                lifecycle_ok = self._market_order_lifecycle(
                    store, intent, after, now, broker_ms=timeline["brokerMs"])
            elif before is None and after is None:
                # The mock adapter completes synchronously: a (None, None)
                # result means the effect found nothing to act on -> failed.
                store.record_transition(intent.intent_id, lifecycle.FAILED, at=now,
                                        reason="no_effect",
                                        evidence="adapter reported no before/after state")
                lifecycle_ok = False
            else:
                confirmed = lifecycle.CONFIRMED_STATE_BY_KIND[intent.kind]
                store.record_transition(intent.intent_id, confirmed, at=now,
                                        reason="mock_adapter_confirmed",
                                        evidence="synchronous mock broker result")
                lifecycle_ok = True
            stages.append({"stage": STAGE_LIFECYCLE, "ok": lifecycle_ok,
                           "intentId": intent.intent_id})
        stages.append({"stage": STAGE_RUNTIME_UPDATE, "ok": True, "persisted": not dry_run})
        stages.append({"stage": STAGE_AUDIT_EVENT, "ok": True})
        stages.append({"stage": STAGE_COMPLETED, "ok": True})

        timeline["totalMs"] = round(sum(v for v in timeline.values()), 3)
        result = ExecutionResult(status="completed", stages=stages, timeline=timeline,
                                 dryRun=dry_run, before=before, after=after, safety=safety_view,
                                 intent_id=intent.intent_id if intent is not None else None)
        self.metrics.record(name, result, now)
        return result

    # ── LIVE-2: market-order lifecycle persistence ──────────────────────────────
    def _market_order_lifecycle(self, store, intent, after: dict | None, now: str,
                                *, broker_ms: float) -> bool:
        """Record the deterministic lifecycle outcome of ONE market-order
        dispatch from the canonical BrokerResult view in `after`:

            ok (filled/partial)      -> submitted -> acknowledged -> open
            rejected                 -> rejected            (broker refused; nothing created)
            not_submitted /
              unavailable /
              not_connected /
              connection_denied      -> failed              (order_send never ran)
            timeout                  -> unknown -> reconciliation_required
            communication_failed     -> unknown -> reconciliation_required

        Evidence is the acknowledgement JSON (+ measured latency); the broker
        ticket persists as broker_ref on the acknowledged/open transitions."""
        import json as _json
        view = after if isinstance(after, dict) else {}
        code = view.get("code")
        ack = view.get("ack") if isinstance(view.get("ack"), dict) else {}
        ref = view.get("brokerRef") or ack.get("broker_order_ticket") \
            or ack.get("broker_deal_ticket")
        evidence = _json.dumps({"ack": dict(sorted(ack.items())),
                                "brokerLatencyMs": broker_ms}, sort_keys=True)
        iid = intent.intent_id
        if view.get("ok") and code == "ok":
            store.record_transition(iid, lifecycle.SUBMITTED, at=now,
                                    reason="order_send_accepted", evidence=evidence)
            store.record_transition(iid, lifecycle.ACKNOWLEDGED, at=now,
                                    reason="broker_acknowledged", evidence=evidence,
                                    broker_ref=ref)
            store.record_transition(iid, lifecycle.OPEN, at=now,
                                    reason="position_open", evidence=evidence,
                                    broker_ref=ref)
            return True
        if code == "rejected":
            store.record_transition(iid, lifecycle.REJECTED, at=now,
                                    reason="broker_rejected", evidence=evidence)
            return False
        if code in ("timeout", "communication_failed"):
            # The broker MAY have acted — never guess; queue for reconciliation.
            store.record_transition(iid, lifecycle.UNKNOWN, at=now,
                                    reason=code, evidence=evidence, broker_ref=ref)
            store.record_transition(iid, lifecycle.RECONCILIATION_REQUIRED, at=now,
                                    reason="ambiguous_submission_outcome",
                                    evidence=evidence)
            return False
        # not_submitted / unavailable / not_connected / connection_denied /
        # anything unrecognised: order_send never ran — deterministic failure.
        store.record_transition(iid, lifecycle.FAILED, at=now,
                                reason="submission_failed", evidence=evidence)
        return False
