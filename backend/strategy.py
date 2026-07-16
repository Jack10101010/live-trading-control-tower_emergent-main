"""
Strategy Engine (Phase 9).

Introduces signal evaluation ahead of the command layer:

    Strategy → Signal → Decision → Command → ExecutionOrchestrator → Broker

The StrategyEngine owns evaluation. It produces immutable Decision objects and
CANDIDATE commands, and it EXECUTES NOTHING — no overlay writes, no commands, no
events. The Execution Orchestrator remains the sole execution owner.

`strategy.py` imports nothing from `server.py`; the runtime injects a read-only
`StrategyEnv`. Everything here is a pure read-model over runtime + policy state.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Evaluation pipeline stages (Objective 2) — every stage is observable.
# ---------------------------------------------------------------------------

PIPELINE = [
    "market_snapshot",
    "strategy_evaluation",
    "policy_evaluation",
    "decision",
    "recommendation",
    "command_candidate",
    "execution_request",  # optional — the engine only PROPOSES; never dispatches
]

CONFIDENCE_THRESHOLD = 0.6


# ---------------------------------------------------------------------------
# Immutable Decision object (Objective 3).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Decision:
    decisionId: str
    deploymentId: str
    pair: str
    policyCell: str | None
    marketState: str | None
    score: float
    confidence: float
    signal: str          # "fired" | "rejected" | "hold"
    reasoning: str
    policyFailures: tuple
    recommendations: tuple
    candidateCommands: tuple  # ({name, payload, rationale}) — NEVER executed
    evaluatedAt: str


# ---------------------------------------------------------------------------
# Read-only environment injected by the runtime.
# ---------------------------------------------------------------------------

@dataclass
class StrategyEnv:
    deployments: Callable[[], list]
    market_state: Callable[[str], dict | None]
    policy_matrix: Callable[[str], dict | None]
    active_package_version: Callable[[], Any]
    recommendations_for: Callable[[str], list]
    now: str


# ---------------------------------------------------------------------------
# Strategy interface + one concrete strategy.
# ---------------------------------------------------------------------------

class Strategy:
    strategy_id = "strategy"
    name = "Strategy"

    def evaluate(self, deployment: dict, env: StrategyEnv, seq: int) -> Decision:  # pragma: no cover
        raise NotImplementedError


class PolicyAlignmentStrategy(Strategy):
    """Evaluates whether a deployment's pair market state aligns with an eligible,
    high-confidence policy cell, and PROPOSES (never dispatches) control commands."""
    strategy_id = "policy_alignment"
    name = "Policy Alignment"

    def evaluate(self, deployment: dict, env: StrategyEnv, seq: int) -> Decision:
        pair = deployment.get("pair", "")
        dep_id = deployment.get("deploymentId", "")
        did = f"eval_{seq}_{dep_id[-6:]}"
        ms = env.market_state(pair)
        matrix = env.policy_matrix(pair)
        failures: list[str] = []

        if not ms:
            return Decision(did, dep_id, pair, None, None, 0.0, 0.0, "rejected",
                            f"No market-state snapshot for {pair}", ("no_market_state",), (), (), env.now)
        state = ms.get("state")
        confidence = float(ms.get("confidence") or 0.0)
        if not matrix:
            return Decision(did, dep_id, pair, None, state, 0.0, confidence, "rejected",
                            f"No policy matrix for {pair}", ("no_policy_matrix",), (), (), env.now)

        # Find an eligible policy cell whose market state matches the current state.
        cells = matrix.get("cells", {})
        match = None
        for key, cell in cells.items():
            if cell.get("marketState") == state:
                match = (key, cell)
                if cell.get("eligibility", {}).get("resolvedAllowed"):
                    break
        if match is None:
            return Decision(did, dep_id, pair, None, state, 0.1, confidence, "rejected",
                            f"No policy cell for market state {state}", ("no_matching_cell",), (), (), env.now)

        cell_key, cell = match
        eligible = bool(cell.get("eligibility", {}).get("resolvedAllowed"))
        confirmed = bool(ms.get("confirmed"))
        if not eligible:
            failures.append("cell_not_eligible")
        if confidence < CONFIDENCE_THRESHOLD:
            failures.append("low_confidence")
        if not confirmed:
            failures.append("state_unconfirmed")

        # Score blends confidence, eligibility and evidence sample size.
        sample = cell.get("evidence", {}).get("sampleSize") or 0
        score = round(confidence * (1.0 if eligible else 0.3) * (1.0 if confirmed else 0.7)
                      * min(1.0, 0.7 + sample / 200.0), 4)

        recos = tuple(
            {"recommendationId": r.get("recommendationId"), "proposedChange": r.get("proposedChange"),
             "status": r.get("status")}
            for r in env.recommendations_for(pair)
            if r.get("scenarioKey", "").startswith(f"{pair}:")
        )

        status = deployment.get("status")
        candidates: list[dict] = []
        if not failures:
            signal = "fired"
            reasoning = (f"{state} @ {confidence:.0%} aligns with eligible cell {cell_key} "
                         f"(target {cell.get('target', {}).get('rr')}R, {sample} samples).")
            if status in ("Paused", "Waiting", "Locked"):
                candidates.append({"name": "ResumeDeployment", "payload": {"deploymentId": dep_id},
                                   "rationale": "aligned signal — propose resume"})
            else:
                # Aligned and already running → hold (no candidate command).
                signal = "hold"
        else:
            signal = "rejected"
            reasoning = f"Signal rejected for {cell_key}: {', '.join(failures)}."
            if status in ("Armed", "InTrade"):
                candidates.append({"name": "PauseDeployment", "payload": {"deploymentId": dep_id},
                                   "rationale": "misaligned signal — propose pause"})

        return Decision(did, dep_id, pair, cell_key, state, score, confidence, signal, reasoning,
                        tuple(failures), recos, tuple(candidates), env.now)


# ---------------------------------------------------------------------------
# Strategy registry (Objective 4).
# ---------------------------------------------------------------------------

class StrategyRegistry:
    def __init__(self):
        self._strategies: dict[str, Strategy] = {}

    def register(self, strategy: Strategy) -> None:
        self._strategies[strategy.strategy_id] = strategy

    def unregister(self, strategy_id: str) -> None:
        self._strategies.pop(strategy_id, None)

    def enumerate(self) -> list[dict]:
        return [{"strategyId": s.strategy_id, "name": s.name} for s in self._strategies.values()]

    def lookup(self, strategy_id: str) -> Strategy | None:
        return self._strategies.get(strategy_id)

    def active(self) -> Strategy | None:
        return next(iter(self._strategies.values()), None)


# ---------------------------------------------------------------------------
# Metrics (Objective 6).
# ---------------------------------------------------------------------------

@dataclass
class StrategyMetrics:
    evaluations: int = 0
    total_ms: float = 0.0
    last_evaluation_at: str | None = None
    last_evaluation_ms: float = 0.0
    last_strategy: str | None = None

    def record(self, strategy_name: str, duration_ms: float, now: str) -> None:
        self.evaluations += 1
        self.total_ms += duration_ms
        self.last_evaluation_ms = duration_ms
        self.last_evaluation_at = now
        self.last_strategy = strategy_name

    def health(self) -> dict:
        avg = round(self.total_ms / self.evaluations, 3) if self.evaluations else 0.0
        return {
            "strategyHealthy": True,
            "evaluations": self.evaluations,
            "averageEvaluationMs": avg,
            "lastEvaluationMs": self.last_evaluation_ms,
            "lastEvaluationAt": self.last_evaluation_at,
            "lastStrategy": self.last_strategy,
        }


# ---------------------------------------------------------------------------
# Strategy Engine (Objectives 1, 2, 5). Evaluation only — executes nothing.
# ---------------------------------------------------------------------------

class StrategyEngine:
    def __init__(self, registry: StrategyRegistry, metrics: StrategyMetrics):
        self.registry = registry
        self.metrics = metrics

    def evaluate(self, env: StrategyEnv, seq: int) -> dict:
        t0 = time.perf_counter()
        strat = self.registry.active()
        decisions: list[Decision] = []
        if strat is not None:
            for i, dep in enumerate(env.deployments()):
                decisions.append(strat.evaluate(dep, env, seq * 100 + i))
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        name = strat.name if strat else "none"
        self.metrics.record(name, duration_ms, env.now)

        fired = sum(1 for d in decisions if d.signal == "fired")
        held = sum(1 for d in decisions if d.signal == "hold")
        rejected = sum(1 for d in decisions if d.signal == "rejected")
        policy_failures = sum(len(d.policyFailures) for d in decisions)
        candidate_total = sum(len(d.candidateCommands) for d in decisions)

        return {
            "strategyId": strat.strategy_id if strat else None,
            "strategyName": name,
            "decisions": [asdict(d) for d in decisions],
            # Evaluation report (Objective 5) — why fired/rejected, policy failures, confidence, timeline.
            "report": {
                "evaluated": len(decisions),
                "fired": fired,
                "held": held,
                "rejected": rejected,
                "policyFailures": policy_failures,
                "candidateCommands": candidate_total,
                "executed": 0,  # the engine NEVER executes
                "pipeline": PIPELINE,
                "durationMs": duration_ms,
            },
            "metrics": self.metrics.health(),
            "at": env.now,
        }
