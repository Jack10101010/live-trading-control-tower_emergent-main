"""UI-18 — the execution-control SAFETY BOUNDARY. Contract only; changes nothing.

This module defines the safety contract that MUST be satisfied before any operator
command could ever affect trading — and a single, pure, deny-by-default policy
evaluator that returns an immutable allow/deny decision. It executes nothing,
contacts no broker, opens no socket, persists nothing and is wired into no route.
Like UI-15, it is the contract-first, isolated definition of a high-risk capability;
enforcement (wiring the evaluator into a command path) is a separate, later,
individually-audited slice.

WHY A CONTRACT WITH NO ENFORCEMENT
    The gate that decides whether a command may touch trading is the single most
    dangerous piece of future logic in the system. Pinning its shape, its invariants
    and its fail-closed behaviour FIRST — in one pure function with an exhaustive
    test suite and no way to actually act — is how that gate is made trustworthy
    before it is ever given teeth.

THE CORE PROPERTY: FAIL CLOSED
    `evaluate()` is deny-by-default and wrapped so that ANY unexpected error yields a
    DENY, never an allow. There is no code path — malformed input, a raised
    exception, an unknown command, an unrecognised node state — that falls through to
    permission. Allow is returned only when every precondition is explicitly met.

DELIBERATELY NOT WIRED / NOT SUBMIT-ABLE
    The command classification below includes execution-affecting and emergency
    classes FOR FUTURE USE. This does NOT make them submit-able: UI-15's
    `ALLOWED_TYPES` remains the read-only trio, UI-16/UI-17 still transmit only those,
    and nothing here changes that. Classifying a command is not the same as enabling
    it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import security_config

# ── system execution mode (default OBSERVE — no execution is possible) ─────────
MODE_OBSERVE = "observe"          # the default and only safe mode: observation only
MODE_ACTIVE = "active"            # fixture/mock world: execution *may* be permitted
MODE_SUSPENDED = "suspended"      # deliberately halted; nothing execution-affecting allowed
# LIVE-3 governed modes (owned durably by execution_mode.py):
MODE_MANUAL_LIVE = "manual_live"  # explicitly authorized MANUAL operations only
MODE_HALTED = "halted"            # emergency: only flagged de-risking ops may run
KNOWN_MODES = frozenset({MODE_OBSERVE, MODE_ACTIVE, MODE_SUSPENDED,
                         MODE_MANUAL_LIVE, MODE_HALTED})

#: Modes in which a fully-gated non-read-only command may proceed at all.
_PERMISSIVE_MODES = frozenset({MODE_ACTIVE, MODE_MANUAL_LIVE})

# ── command risk classes ───────────────────────────────────────────────────────
RISK_READ_ONLY = "read_only"                 # observes only; never touches node state
RISK_OPERATIONAL = "operational"             # affects operation, not trades directly
RISK_EXECUTION_AFFECTING = "execution_affecting"   # can change trading/positions/orders
RISK_EMERGENCY = "emergency"                 # a safety STOP (kill/flatten/disarm)
KNOWN_RISK_CLASSES = frozenset({
    RISK_READ_ONLY, RISK_OPERATIONAL, RISK_EXECUTION_AFFECTING, RISK_EMERGENCY})

# ARCH-1: command→risk classification is no longer owned here. `classify()` delegates
# to the single canonical `command_registry`, which resolves aliases (the snake_case
# safety names like `close_position` map onto their canonical fixture command). This
# module still OWNS the risk-class constants and the `evaluate()` policy engine; it no
# longer keeps a second, drift-prone command list.

# Per-class requirements. These are the heart of the contract.
_REQUIRES_ACTIVE_MODE = frozenset({
    RISK_OPERATIONAL, RISK_EXECUTION_AFFECTING, RISK_EMERGENCY})
_REQUIRES_IDENTITY = frozenset({
    RISK_OPERATIONAL, RISK_EXECUTION_AFFECTING, RISK_EMERGENCY})
_REQUIRES_CONFIRMATION = frozenset({
    RISK_OPERATIONAL, RISK_EXECUTION_AFFECTING, RISK_EMERGENCY})
_REQUIRES_HEALTHY_NODE = frozenset({
    RISK_OPERATIONAL, RISK_EXECUTION_AFFECTING, RISK_EMERGENCY})
#: ONLY execution-affecting commands require the system to be armed. An emergency
#: STOP must never require arming (you must be able to halt without first arming);
#: it is still gated on identity, confirmation, active mode and a healthy node.
_REQUIRES_ARMING = frozenset({RISK_EXECUTION_AFFECTING})

# ── node safety states (aligned with UI-14 node_client + UI-1 connection) ──────
NODE_HEALTHY = "healthy"
NODE_DEGRADED = "degraded"
NODE_STALE = "stale"
NODE_UNAUTHORIZED = "unauthorized"
NODE_UNREACHABLE = "unreachable"
NODE_DISCONNECTED = "disconnected"
NODE_DISABLED = "disabled"
NODE_CONNECTING = "connecting"
NODE_UNKNOWN = "unknown"
#: Only a healthy node permits an execution-affecting or emergency command. Every
#: other state — including one this module does not recognise — denies.
_HEALTHY_NODE_STATES = frozenset({NODE_HEALTHY})

# ── stable reason codes (safe to display; never a value or a secret) ───────────
ALLOW = "allow"
ALLOW_READ_ONLY = "allow_read_only"
DENY_UNKNOWN_COMMAND = "unknown_command"
DENY_EXPIRED = "command_expired"
DENY_DUPLICATE = "duplicate_command"
DENY_MODE_NOT_ACTIVE = "execution_mode_not_active"
DENY_MODE_HALTED = "execution_halted"
DENY_IDENTITY_REQUIRED = "operator_identity_required"
DENY_CONFIRMATION_REQUIRED = "confirmation_required"
DENY_DISARMED = "system_disarmed"
DENY_ARMING_EXPIRED = "arming_expired"
DENY_NODE_NOT_HEALTHY = "node_not_healthy"
DENY_RECONCILIATION_REQUIRED = "reconciliation_unresolved"
DENY_ACCOUNT_MISMATCH = "account_identity_mismatch"
DENY_ACCOUNT_UNKNOWN = "account_identity_unknown"
DENY_POLICY_ERROR = "policy_evaluation_error"
#: UES: a FINANCIAL rail refused the command. The specific rail is appended
#: (`financial_rail_kill_switch`, `financial_rail_daily_loss_limit`, …) so an
#: operator sees which limit stopped them, not merely that something did.
DENY_FINANCIAL_RAIL = "financial_rail"

# ── ARCH-3: account-identity evaluation states ────────────────────────────────
#: `not_evaluated` — no account evaluation was performed (pure-engine callers /
#: legacy contexts). No account gate applies; every other gate still does.
#: The PRODUCTION context assembler always supplies an evaluated state.
ACCOUNT_NOT_EVALUATED = "not_evaluated"
ACCOUNT_MATCH = "match"
ACCOUNT_MISMATCH = "mismatch"
ACCOUNT_UNKNOWN = "unknown"          # evaluated but indeterminate (missing facts)

#: The specific node-state deny reason, so an operator sees WHY the node blocked it.
_NODE_DENY_REASON: dict[str, str] = {
    NODE_DEGRADED: "node_degraded",
    NODE_STALE: "node_stale",
    NODE_UNAUTHORIZED: "node_unauthorized",
    NODE_UNREACHABLE: "node_unreachable",
    NODE_DISCONNECTED: "node_disconnected",
    NODE_DISABLED: "node_disabled",
    NODE_CONNECTING: "node_connecting",
    NODE_UNKNOWN: "node_unknown",
}


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.astimezone(timezone.utc)


def classify(command_type: Any) -> str | None:
    """The risk class of a command type, or None if it is unknown. Unknown is never
    a class — it is the absence of one, and the evaluator denies it.

    Delegates to the single canonical `command_registry` (imported lazily so this
    module has no import-time dependency on the catalogue, keeping the policy engine a
    leaf). Aliases resolve: `classify("close_position")` → `CloseTrade`'s class."""
    if not isinstance(command_type, str):
        return None
    from command_registry import risk_class_of
    return risk_class_of(command_type)


# ── immutable contract value objects ──────────────────────────────────────────

@dataclass(frozen=True)
class OperatorAuthorization:
    """Who is asking, and whether they explicitly confirmed. Default: neither."""
    operator_ref: str | None = None
    confirmed: bool = False

    @property
    def identified(self) -> bool:
        return isinstance(self.operator_ref, str) and bool(self.operator_ref.strip())


@dataclass(frozen=True)
class ArmingState:
    """Disarmed by default. Arming is time-bounded and expires automatically."""
    armed: bool = False
    armed_by: str | None = None
    expires_at: str | None = None        # ISO-8601; required to be armed

    def is_active(self, now: datetime) -> bool:
        """Armed AND not yet expired. A missing/invalid/past expiry is NOT active."""
        if not self.armed:
            return False
        expires = _parse_iso(self.expires_at)
        return expires is not None and now < expires

    def is_expired(self, now: datetime) -> bool:
        """Armed but past its expiry window (distinct from never having been armed)."""
        if not self.armed:
            return False
        expires = _parse_iso(self.expires_at)
        return expires is not None and now >= expires


@dataclass(frozen=True)
class NodeSafety:
    """The tower's classification of node state. Default UNKNOWN — which denies."""
    state: str = NODE_UNKNOWN

    @property
    def healthy(self) -> bool:
        return self.state in _HEALTHY_NODE_STATES


@dataclass(frozen=True)
class ReconciliationSafety:
    """ARCH-2: the reconciliation posture the safety gate consults. Populated by
    the execution-context builder from the canonical reconciliation authority.
    The default represents "no critical discrepancy on record" — the CONTEXT
    BUILDER is responsible for supplying the real posture; a command evaluated
    with an unbuilt context is already denied by mode/arming/node defaults."""
    critical_unresolved: bool = False
    stale: bool = False


@dataclass(frozen=True)
class AccountSafety:
    """ARCH-3: the account-identity evaluation the context assembler performed.
    A MISMATCH denies every non-read-only command (a command aimed at the wrong
    account is never safe, including an emergency stop); UNKNOWN denies
    risk-relevant execution while still permitting a halt."""
    state: str = ACCOUNT_NOT_EVALUATED


@dataclass(frozen=True)
class FinancialRailVerdict:
    """One financial-rail outcome, injected from outside this module.

    This module owns AUTHORIZATION policy — mode, arming, identity, node health,
    account binding, reconciliation. It deliberately owns no financial policy and
    no risk arithmetic: those live in the rails the autonomous runner already
    trusts, and duplicating them here would create exactly the second source of
    truth this milestone exists to remove.
    """
    allowed: bool
    rail: str = ""
    detail: str | None = None


@dataclass(frozen=True)
class SafetyContext:
    """The environment a command is evaluated against. Every default is the safe one:
    observe mode, disarmed, unknown node, no operator.

    `financial` is the injected financial-rail evaluator: a callable taking the
    command type and returning a `FinancialRailVerdict`. It defaults to None,
    which applies NO financial gate — the same "not evaluated" convention the
    account gate uses, so pure-engine callers and existing tests are unaffected.
    The production assembler always supplies one when a live adapter is active.
    """
    mode: str = MODE_OBSERVE
    arming: ArmingState = field(default_factory=ArmingState)
    node: NodeSafety = field(default_factory=NodeSafety)
    operator: OperatorAuthorization = field(default_factory=OperatorAuthorization)
    reconciliation: ReconciliationSafety = field(default_factory=ReconciliationSafety)
    account: AccountSafety = field(default_factory=AccountSafety)
    financial: Any = None


@dataclass(frozen=True)
class CommandRequest:
    """The command being weighed. `duplicate` is supplied by the caller (the evaluator
    is pure and holds no history); `expires_at` is the command's own expiry."""
    command_type: str
    command_id: str | None = None
    expires_at: str | None = None
    duplicate: bool = False


@dataclass(frozen=True)
class SafetyDecision:
    """The immutable audit record of ONE evaluation. Redaction-safe by construction."""
    allowed: bool
    reason: str
    command_type: str
    risk_class: str | None
    command_id: str | None
    mode: str
    armed: bool
    node_state: str
    evaluated_at: str
    detail: str | None = None

    def safe_view(self) -> dict:
        """Value-free, redaction-safe projection for an audit log or diagnostic.
        Any free-text detail is passed through the UI-9 redactor as defence in depth."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "commandType": self.command_type,
            "riskClass": self.risk_class,
            "commandId": self.command_id,
            "mode": self.mode,
            "armed": self.armed,
            "nodeState": self.node_state,
            "evaluatedAt": self.evaluated_at,
            "detail": security_config.redact_text(self.detail) if self.detail else None,
        }


def _decision(request: CommandRequest, context: SafetyContext, risk_class: str | None,
              *, allowed: bool, reason: str, now: datetime,
              detail: str | None = None) -> SafetyDecision:
    return SafetyDecision(
        allowed=allowed,
        reason=reason,
        command_type=request.command_type if isinstance(request.command_type, str) else str(request.command_type),
        risk_class=risk_class,
        command_id=request.command_id,
        mode=context.mode,
        armed=bool(context.arming.armed),
        node_state=context.node.state,
        evaluated_at=_iso(now),
        detail=detail,
    )


def evaluate(request: CommandRequest, context: SafetyContext | None = None, *,
             now: datetime | None = None) -> SafetyDecision:
    """The single, pure, DENY-BY-DEFAULT policy evaluator.

    Returns an immutable `SafetyDecision`. It NEVER raises: any unexpected error is
    caught and converted to a `policy_evaluation_error` DENY, so a policy fault can
    never fall through to allow. Allow is returned only when every precondition for
    the command's risk class is explicitly satisfied.
    """
    clock = _now(now)
    ctx = context if context is not None else SafetyContext()
    try:
        risk_class = classify(request.command_type)

        # 1) Unknown commands are denied — an unknown command has no class and no rules.
        if risk_class is None:
            return _decision(request, ctx, None, allowed=False,
                             reason=DENY_UNKNOWN_COMMAND, now=clock)

        # 2) An expired command is denied for EVERY class (a stale instruction is never
        #    safe to act on), regardless of anything else.
        expires = _parse_iso(request.expires_at)
        if expires is not None and clock >= expires:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_EXPIRED, now=clock)

        # 3) Read-only commands remain allowed under existing rules (UI-15/16/17): no
        #    arming, identity, confirmation, mode or node-health requirement is added.
        #    An idempotent duplicate read is fine (UI-15 handles replay).
        if risk_class == RISK_READ_ONLY:
            return _decision(request, ctx, risk_class, allowed=True,
                             reason=ALLOW_READ_ONLY, now=clock)

        # --- everything below is a non-read-only (execution-relevant) command ---

        # 4) The system execution mode must PERMIT the command. observe /
        #    suspended / unknown deny everything non-read-only. LIVE-3: the
        #    HALTED mode denies everything EXCEPT the registry-flagged emergency
        #    de-risking operations (cancel pending order, close position) — and
        #    those still pass every remaining gate below; nothing is waived.
        if risk_class in _REQUIRES_ACTIVE_MODE and ctx.mode not in _PERMISSIVE_MODES:
            if ctx.mode == MODE_HALTED:
                from command_registry import is_halted_available
                if not is_halted_available(request.command_type):
                    return _decision(request, ctx, risk_class, allowed=False,
                                     reason=DENY_MODE_HALTED, now=clock)
            else:
                return _decision(request, ctx, risk_class, allowed=False,
                                 reason=DENY_MODE_NOT_ACTIVE, now=clock)

        # 5) A duplicate execution-relevant command denies safely (no double action).
        if request.duplicate:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_DUPLICATE, now=clock)

        # 6) Operator identity is required.
        if risk_class in _REQUIRES_IDENTITY and not ctx.operator.identified:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_IDENTITY_REQUIRED, now=clock)

        # 7) Explicit confirmation is required.
        if risk_class in _REQUIRES_CONFIRMATION and not ctx.operator.confirmed:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_CONFIRMATION_REQUIRED, now=clock)

        # 8) The node must be observably HEALTHY. Stale / degraded / disconnected /
        #    unreachable / unauthorized / disabled / unknown all deny, each with its
        #    own reason so the operator sees why.
        if risk_class in _REQUIRES_HEALTHY_NODE and not ctx.node.healthy:
            reason = _NODE_DENY_REASON.get(ctx.node.state, DENY_NODE_NOT_HEALTHY)
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=reason, now=clock)

        # 8a) ARCH-3 account-identity gate. A MISMATCH means every command is
        #     aimed at the wrong account — nothing non-read-only may proceed,
        #     including an emergency stop (halting an innocent account is harm).
        #     UNKNOWN (evaluated but indeterminate) denies execution-affecting
        #     commands while still permitting operational work and a halt.
        #     `not_evaluated` applies no gate (pure-engine callers); the production
        #     context assembler always supplies an evaluated state.
        if ctx.account.state == ACCOUNT_MISMATCH:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_ACCOUNT_MISMATCH, now=clock)
        if ctx.account.state == ACCOUNT_UNKNOWN and risk_class == RISK_EXECUTION_AFFECTING:
            return _decision(request, ctx, risk_class, allowed=False,
                             reason=DENY_ACCOUNT_UNKNOWN, now=clock)

        # 8b) ARCH-2 reconciliation gate: unresolved CRITICAL reconciliation
        #     discrepancies deny NEW RISK-INCREASING execution. Risk-reducing
        #     commands (close / cancel / risk-reduction — flagged in the canonical
        #     registry) stay available so an operator can always de-risk, and
        #     emergency stops are exempt by class. This is deliberately narrower
        #     than a generic arming rule so safe close/cancel is never accidentally
        #     blocked.
        if risk_class == RISK_EXECUTION_AFFECTING and ctx.reconciliation.critical_unresolved:
            from command_registry import is_risk_reducing
            if not is_risk_reducing(request.command_type):
                return _decision(request, ctx, risk_class, allowed=False,
                                 reason=DENY_RECONCILIATION_REQUIRED, now=clock)

        # 9) Execution-affecting commands require the system to be ARMED, and arming
        #    must not have expired. (Emergency stops are exempt from arming.)
        if risk_class in _REQUIRES_ARMING:
            if ctx.arming.is_expired(clock):
                return _decision(request, ctx, risk_class, allowed=False,
                                 reason=DENY_ARMING_EXPIRED, now=clock)
            if not ctx.arming.is_active(clock):
                return _decision(request, ctx, risk_class, allowed=False,
                                 reason=DENY_DISARMED, now=clock)

        # 9b) UES — FINANCIAL rails, evaluated by the SAME implementation the
        #     autonomous runner uses (`live.safety.SafetyRails`), injected rather
        #     than imported so this module stays pure and dependency-free.
        #
        #     Before this gate the Control Tower could submit a live order that
        #     the runner would have refused: the kill switch, daily-loss limit,
        #     max-open cap and symbol whitelist were enforced on the autonomous
        #     path only, while both paths reach the same broker submission call.
        #
        #     Deliberately LAST, immediately before allow. Every authorization
        #     reason above keeps its priority, so an unarmed or unidentified
        #     operator still sees that reason rather than a financial one — and
        #     the existing deny ordering is unchanged.
        #
        #     Which commands are gated is decided by the evaluator itself (it
        #     reads the canonical registry's `risk_reducing` flag), so closing or
        #     cancelling to REDUCE risk is never blocked by a financial limit.
        if risk_class == RISK_EXECUTION_AFFECTING and ctx.financial is not None:
            verdict = ctx.financial(request.command_type)
            if verdict is not None and not verdict.allowed:
                rail = getattr(verdict, "rail", "") or ""
                return _decision(
                    request, ctx, risk_class, allowed=False,
                    reason=(f"{DENY_FINANCIAL_RAIL}_{rail}" if rail
                            else DENY_FINANCIAL_RAIL),
                    now=clock, detail=getattr(verdict, "detail", None))

        # 10) Every precondition for this class is satisfied.
        return _decision(request, ctx, risk_class, allowed=True, reason=ALLOW, now=clock)

    except Exception as exc:                 # FAIL CLOSED: a fault is a DENY, never allow
        return SafetyDecision(
            allowed=False, reason=DENY_POLICY_ERROR,
            command_type=str(getattr(request, "command_type", "")),
            risk_class=None, command_id=getattr(request, "command_id", None),
            mode=getattr(ctx, "mode", MODE_OBSERVE),
            armed=False, node_state=getattr(getattr(ctx, "node", None), "state", NODE_UNKNOWN),
            evaluated_at=_iso(clock),
            detail=security_config.redact_text(type(exc).__name__))
