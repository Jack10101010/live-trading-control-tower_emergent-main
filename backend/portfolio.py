"""
Portfolio Engine (Phase 14).

A standalone, READ-ONLY capital-allocation layer inserted into the pipeline:

    Market Data → Strategy → Risk → Portfolio → Execution Orchestrator → Broker

The Portfolio Engine is the single owner of capital-allocation decisions. It
evaluates every APPROVED opportunity (a strategy Decision + its Risk Assessment)
and returns a verdict — `allocate` / `defer` / `reject` — as an immutable
`AllocationDecision`. It EXECUTES NOTHING (no overlay writes, no commands, no
broker calls, no position sizing) and it never places or moves capital; the
Execution Orchestrator remains the sole executor.

`portfolio.py` imports nothing from `server.py`. The runtime injects a read-only
`PortfolioEnv` (accounts, deployment lookup). All capital / exposure / risk
numbers are REUSED from existing infrastructure — account balance/equity +
`fundedRules`, deployment `riskState`, and the Phase-12 `RiskAssessment` — the
engine recomputes no exposure or risk math of its own.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Callable


# ---------------------------------------------------------------------------
# Verdicts.
# ---------------------------------------------------------------------------

ALLOCATE = "allocate"
DEFER = "defer"
REJECT = "reject"

# Engine-level allocation policy (NOT live capital rules — advisory ceilings that
# reuse the fixture's own account fundedRules where present).
DEFAULT_RISK_BUDGET_PCT = 0.02        # per-account risk budget per allocation slot
MAX_ALLOCATIONS_PER_PAIR = 1          # concentration: new positions per instrument / cycle
MAX_PAIR_CONCENTRATION_PCT = 0.25     # concentration: cap of total capital per instrument


# ---------------------------------------------------------------------------
# Immutable AllocationDecision (Objective 2).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AllocationDecision:
    timestamp: str
    deployment: str | None
    account: str | None
    symbol: str | None
    allocation: str          # allocate | defer | reject
    priority: int            # 1 = highest (best score)
    score: float
    reason: str
    capitalRequired: float
    capitalAvailable: float


# ---------------------------------------------------------------------------
# Read-only environment injected by the runtime.
# ---------------------------------------------------------------------------

@dataclass
class PortfolioEnv:
    accounts: Callable[[], list]                 # all accounts (balance/equity/fundedRules)
    account: Callable[[str], dict | None]        # account lookup by id
    deployment: Callable[[str], dict | None]     # runtime-overlaid deployment (riskState)
    now: str


def _risk_budget_pct(account: dict | None) -> float:
    rules = (account or {}).get("fundedRules") or {}
    mre = rules.get("maxRiskExposure")
    return float(mre) if mre is not None else DEFAULT_RISK_BUDGET_PCT


# ---------------------------------------------------------------------------
# Portfolio Engine (Objectives 1, 3, 4, 5). Allocation only — executes nothing.
# ---------------------------------------------------------------------------

class PortfolioEngine:
    def __init__(self, metrics: "PortfolioMetrics"):
        self.metrics = metrics
        self._last: list[dict] = []

    def total_capital(self, env: PortfolioEnv) -> float:
        return round(sum(float(a.get("equity") or a.get("balance") or 0.0) for a in env.accounts()), 2)

    def evaluate(self, env: PortfolioEnv, opportunities: list[dict]) -> dict:
        """opportunities: [{decision, assessment}] — a strategy Decision plus its Risk
        Assessment. Ranks by priority, allocates greedily within available capital,
        per-account budget, pair concentration and risk budget. Returns immutable
        AllocationDecisions + a portfolio summary. Executes nothing."""
        t0 = time.perf_counter()
        total = self.total_capital(env)
        # Per-account remaining capital (account allocation) — reuses account equity.
        available: dict[str, float] = {a.get("accountId"): float(a.get("equity") or a.get("balance") or 0.0)
                                       for a in env.accounts()}
        pair_capital: dict[str, float] = {}
        pair_count: dict[str, int] = {}
        allocated_total = 0.0

        # Priority order: best strategy score first (score reused, not recomputed).
        ranked = sorted(
            enumerate(opportunities),
            key=lambda io: (-float((io[1].get("decision") or {}).get("score") or 0.0),
                            (io[1].get("decision") or {}).get("deploymentId") or ""),
        )

        decisions: list[AllocationDecision] = []
        for priority, (_, opp) in enumerate(ranked, start=1):
            dec = opp.get("decision") or {}
            asr = opp.get("assessment") or {}
            dep_id = dec.get("deploymentId")
            sym = dec.get("pair")
            dep = env.deployment(dep_id) or {}
            account_id = dep.get("accountId")
            account = env.account(account_id) if account_id else None
            score = float(dec.get("score") or 0.0)
            budget_pct = _risk_budget_pct(account)
            equity = float((account or {}).get("equity") or (account or {}).get("balance") or 0.0)
            capital_required = round(equity * budget_pct, 2)
            acct_available = round(available.get(account_id, 0.0), 2)

            verdict, reason = ALLOCATE, "allocated within capital and concentration limits"
            if not asr.get("allowed", True):
                verdict, reason = REJECT, f"risk {asr.get('severity', 'deny')}: {asr.get('reason', 'not approved')}"
            elif dec.get("signal") == "rejected":
                verdict, reason = REJECT, "strategy signal rejected"
            elif pair_count.get(sym, 0) >= MAX_ALLOCATIONS_PER_PAIR:
                verdict, reason = DEFER, f"pair concentration: {sym} already allocated this cycle"
            elif pair_capital.get(sym, 0.0) + capital_required > MAX_PAIR_CONCENTRATION_PCT * total:
                verdict, reason = DEFER, f"pair capital cap for {sym}"
            elif capital_required > acct_available:
                verdict, reason = DEFER, "insufficient account capital"
            elif _risk_budget_exhausted(dep):
                verdict, reason = DEFER, "deployment drawdown budget exhausted"

            if verdict == ALLOCATE:
                available[account_id] = acct_available - capital_required
                pair_capital[sym] = pair_capital.get(sym, 0.0) + capital_required
                pair_count[sym] = pair_count.get(sym, 0) + 1
                allocated_total += capital_required

            decisions.append(AllocationDecision(
                timestamp=env.now, deployment=dep_id, account=account_id, symbol=sym,
                allocation=verdict, priority=priority, score=score, reason=reason,
                capitalRequired=capital_required, capitalAvailable=acct_available,
            ))

        allocated_total = round(allocated_total, 2)
        available_total = round(total - allocated_total, 2)
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        summary = {
            "totalCapital": total, "allocatedCapital": allocated_total,
            "availableCapital": available_total,
            "allocations": sum(1 for d in decisions if d.allocation == ALLOCATE),
            "deferred": sum(1 for d in decisions if d.allocation == DEFER),
            "rejected": sum(1 for d in decisions if d.allocation == REJECT),
        }
        self._last = [asdict(d) for d in decisions]
        self.metrics.record(summary, duration_ms, env.now)
        return {"decisions": self._last, "summary": summary, "health": self.metrics.health()}

    def last_decisions(self) -> list[dict]:
        return list(self._last)

    def status(self, env: PortfolioEnv) -> dict:
        return {
            "portfolioHealthy": True,
            "totalCapital": self.total_capital(env),
            "limits": {
                "defaultRiskBudgetPct": DEFAULT_RISK_BUDGET_PCT,
                "maxAllocationsPerPair": MAX_ALLOCATIONS_PER_PAIR,
                "maxPairConcentrationPct": MAX_PAIR_CONCENTRATION_PCT,
            },
            "lastDecisions": self._last,
            "metrics": self.metrics.health(),
        }


def _risk_budget_exhausted(deployment: dict) -> bool:
    """Reuses the deployment's own riskState drawdown buffer — no new risk math.
    True when there is no drawdown room left to fund a fresh allocation."""
    risk = deployment.get("riskState") or {}
    return float(risk.get("ddBufferPct") if risk.get("ddBufferPct") is not None else 100.0) <= 0.0


# ---------------------------------------------------------------------------
# Metrics (Objective 5).
# ---------------------------------------------------------------------------

@dataclass
class PortfolioMetrics:
    cycles: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0
    allocations: int = 0
    deferred: int = 0
    rejected: int = 0
    total_capital: float = 0.0
    allocated_capital: float = 0.0
    available_capital: float = 0.0
    last_at: str | None = None

    def record(self, summary: dict, duration_ms: float, now: str) -> None:
        self.cycles += 1
        self.total_ms += duration_ms
        self.last_ms = duration_ms
        self.last_at = now
        self.allocations += summary["allocations"]
        self.deferred += summary["deferred"]
        self.rejected += summary["rejected"]
        self.total_capital = summary["totalCapital"]
        self.allocated_capital = summary["allocatedCapital"]
        self.available_capital = summary["availableCapital"]

    def health(self) -> dict:
        avg = round(self.total_ms / self.cycles, 3) if self.cycles else 0.0
        util = round(self.allocated_capital / self.total_capital * 100.0, 2) if self.total_capital else 0.0
        return {
            "portfolioHealthy": True,
            "cycles": self.cycles,
            "allocations": self.allocations,
            "deferred": self.deferred,
            "rejected": self.rejected,
            "totalCapital": self.total_capital,
            "allocatedCapital": self.allocated_capital,
            "availableCapital": self.available_capital,
            "utilisationPct": util,
            "averageCycleMs": avg,
            "lastCycleMs": self.last_ms,
            "lastCycleAt": self.last_at,
        }
