"""LIVE-5A — the pre-trade preflight gate.

PURPOSE
    One place that answers: "is it safe to submit the first live order for this
    Recommendation right now?" It checks the eight conditions PART 15 requires
    and returns every failure at once, so an operator sees the complete picture
    rather than fixing one blocker at a time.

WHAT THIS IS NOT
    NOT a replacement for any existing protection. The execution pipeline's own
    gates — `execution_safety`, `command_authorization`, the execution-mode owner
    and the durable idempotency check — remain exactly as they were and run
    again when the order is actually submitted. This gate is ADVISORY and runs
    BEFORE them: it exists so a doomed submission is refused early with a clear
    reason, not so the real gates can be skipped.

    It therefore fails CLOSED: anything it cannot verify is a blocker, never an
    assumption that the condition holds.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

import market_runtime as mr
import recommendation_domain as rd
import runtime_supervisor as rsup

# ── blocker codes (stable, machine-readable) ─────────────────────────────────
BLOCK_RUNTIME_NOT_READY = "runtime_not_ready"
BLOCK_BROKER_DISCONNECTED = "broker_disconnected"
BLOCK_PROJECTION_STALE = "projection_stale"
BLOCK_EXECUTION_MODE = "execution_mode_forbids_execution"
BLOCK_AUTHORIZATION = "authorization_invalid"
BLOCK_SYMBOL_INACTIVE = "symbol_not_active"
BLOCK_RECOMMENDATION_MISSING = "recommendation_missing"
BLOCK_RECOMMENDATION_NOT_ACCEPTED = "recommendation_not_accepted"
BLOCK_RECOMMENDATION_EXPIRED = "recommendation_expired"
BLOCK_RECOMMENDATION_TERMS = "recommendation_terms_incomplete"
BLOCK_SCENARIO_MISSING = "scenario_missing"
BLOCK_SCENARIO_INVALID = "scenario_invalid"
BLOCK_INTENT_DUPLICATE = "intent_already_exists"
BLOCK_UNVERIFIABLE = "condition_unverifiable"

#: Scenario statuses that still permit a first entry. A scenario that already
#: closed, was invalidated or is already executing must not be re-entered.
SCENARIO_TRADEABLE = frozenset({
    "CREATED", "WATCHING", "QUALIFIED", "RECOMMENDED", "ACCEPTED",
})

#: The projection must be at least this fresh to authorize a trade. Stricter
#: than the dashboard's staleness threshold on purpose: looking at a slightly
#: old dashboard is fine, trading on one is not.
MAX_PROJECTION_AGE_S = 15.0


@dataclass(frozen=True)
class PreflightCheck:
    """One condition and its verdict. `detail` is safe to show an operator."""
    name: str
    passed: bool
    code: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict:
        return {"name": self.name, "passed": self.passed, "code": self.code,
                "detail": self.detail}


@dataclass(frozen=True)
class PreflightResult:
    """The complete verdict. `clear` only when EVERY check passed."""
    at: str
    recommendation_id: str
    checks: tuple = field(default_factory=tuple)

    @property
    def blockers(self) -> tuple:
        return tuple(c.code for c in self.checks if not c.passed and c.code)

    @property
    def clear(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    def as_dict(self) -> dict:
        return {
            "at": self.at, "recommendationId": self.recommendation_id,
            "clear": self.clear, "blockers": list(self.blockers),
            "checks": [c.as_dict() for c in self.checks],
        }


def evaluate(*, recommendation_id: str, now: str,
             runtime_snapshot=None, runtime_health=None,
             recommendation=None, scenario=None,
             execution_mode: str | None = None,
             mode_allows_execution: Callable[[str | None], bool] | None = None,
             authorization: dict | None = None,
             existing_intent_for: Callable[[str], Any] | None = None,
             ) -> PreflightResult:
    """Evaluate every precondition. Pure: no I/O, no clock, no broker read.

    Every input is passed in already-read, so this function is deterministic and
    a test can drive any combination of failures.
    """
    checks: list[PreflightCheck] = []

    # 1. Runtime is producing evidence at all.
    if runtime_snapshot is None or runtime_health is None:
        checks.append(PreflightCheck(
            "runtime", False, BLOCK_RUNTIME_NOT_READY,
            "the runtime has produced no tick, so nothing can be verified"))
    else:
        state = runtime_health.state
        ready = state in (rsup.CONNECTED, rsup.DEGRADED)
        checks.append(PreflightCheck(
            "runtime", ready,
            None if ready else BLOCK_RUNTIME_NOT_READY,
            f"runtime state is {state}"))

    market = getattr(runtime_snapshot, "market", None)
    heartbeat = getattr(market, "heartbeat", None)

    # 2. Broker connected.
    connected = bool(heartbeat and heartbeat.connection == mr.CONN_CONNECTED)
    checks.append(PreflightCheck(
        "broker_connected", connected,
        None if connected else BLOCK_BROKER_DISCONNECTED,
        f"connection is {getattr(heartbeat, 'connection', 'unknown')}"))

    # 3. Projection fresh enough to trade on.
    age = getattr(runtime_health, "projection_age_seconds", None)
    fresh = age is not None and age <= MAX_PROJECTION_AGE_S
    checks.append(PreflightCheck(
        "projection_fresh", fresh,
        None if fresh else BLOCK_PROJECTION_STALE,
        (f"projection age {age}s exceeds {MAX_PROJECTION_AGE_S}s"
         if age is not None else "projection age is unknown")))

    # 4. Execution mode permits execution. Delegated to the mode owner's own
    #    rule — this module does not re-implement what "allows execution" means.
    if mode_allows_execution is None:
        checks.append(PreflightCheck(
            "execution_mode", False, BLOCK_UNVERIFIABLE,
            "no execution-mode predicate supplied"))
    else:
        try:
            allowed = bool(mode_allows_execution(execution_mode))
        except Exception as exc:                                # noqa: BLE001
            allowed = False
            checks.append(PreflightCheck(
                "execution_mode", False, BLOCK_UNVERIFIABLE,
                f"mode check failed: {type(exc).__name__}"))
        else:
            checks.append(PreflightCheck(
                "execution_mode", allowed,
                None if allowed else BLOCK_EXECUTION_MODE,
                f"execution mode is {execution_mode}"))

    # 5. Authorization valid. Shape-checked only: the authorization plane
    #    remains the authority and re-checks at submission.
    auth_ok = bool(isinstance(authorization, dict)
                   and authorization.get("valid") is True)
    checks.append(PreflightCheck(
        "authorization", auth_ok,
        None if auth_ok else BLOCK_AUTHORIZATION,
        "no valid authorization grant is present" if not auth_ok else None))

    # 6. Recommendation is current, accepted and completely specified.
    if recommendation is None:
        checks.append(PreflightCheck(
            "recommendation", False, BLOCK_RECOMMENDATION_MISSING,
            recommendation_id))
    else:
        accepted = recommendation.status == rd.RecommendationStatus.ACCEPTED
        checks.append(PreflightCheck(
            "recommendation_accepted", accepted,
            None if accepted else BLOCK_RECOMMENDATION_NOT_ACCEPTED,
            f"status is {recommendation.status}"))
        expired = recommendation.past_due(now)
        checks.append(PreflightCheck(
            "recommendation_current", not expired,
            None if not expired else BLOCK_RECOMMENDATION_EXPIRED,
            f"expiry {recommendation.expiry_at} has passed" if expired else None))
        terms = recommendation.terms
        complete = (terms.execution.quantity is not None
                    and terms.risk.stop_loss is not None)
        checks.append(PreflightCheck(
            "recommendation_terms", complete,
            None if complete else BLOCK_RECOMMENDATION_TERMS,
            "quantity and stop loss are both required to size and protect an order"
            if not complete else None))

        # 7. Market symbol is actually live for THIS recommendation's instrument.
        sub = market.symbol(recommendation.instrument) if market else None
        active = bool(sub and sub.active
                      and sub.quote and sub.quote.availability(now) == mr.AVAILABLE)
        checks.append(PreflightCheck(
            "symbol_active", active,
            None if active else BLOCK_SYMBOL_INACTIVE,
            f"{recommendation.instrument} is not quoting a fresh price"))

    # 8. Scenario exists and is in a state that still permits an entry.
    if scenario is None:
        checks.append(PreflightCheck(
            "scenario", False, BLOCK_SCENARIO_MISSING,
            "the parent scenario could not be read"))
    else:
        valid = scenario.status in SCENARIO_TRADEABLE
        checks.append(PreflightCheck(
            "scenario_valid", valid,
            None if valid else BLOCK_SCENARIO_INVALID,
            f"scenario status is {scenario.status}"))

    # 9. No intent already exists for this Recommendation — the duplicate-
    #    submission guard. The execution store's idempotency remains the real
    #    enforcement; this catches the common case early and visibly.
    if existing_intent_for is None:
        checks.append(PreflightCheck(
            "intent_unique", False, BLOCK_UNVERIFIABLE,
            "no intent lookup supplied"))
    else:
        try:
            existing = existing_intent_for(recommendation_id)
        except Exception as exc:                                # noqa: BLE001
            checks.append(PreflightCheck(
                "intent_unique", False, BLOCK_UNVERIFIABLE,
                f"intent lookup failed: {type(exc).__name__}"))
        else:
            unique = not existing
            checks.append(PreflightCheck(
                "intent_unique", unique,
                None if unique else BLOCK_INTENT_DUPLICATE,
                f"intent {existing} already references this recommendation"
                if existing else None))

    return PreflightResult(at=str(now), recommendation_id=str(recommendation_id),
                           checks=tuple(checks))
