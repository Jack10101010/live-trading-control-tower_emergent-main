"""
Risk Engine (Phase 12).

A standalone, READ-ONLY assessment layer inserted into the evaluation pipeline:

    Strategy → Decision → Risk Engine → Execution Orchestrator → Broker

The Risk Engine is the only owner of runtime trading PERMISSIONS. It evaluates a
proposed action against a set of read-only checks and returns a verdict —
`allow` / `warn` / `deny` — as an immutable `RiskAssessment`. It EXECUTES NOTHING
(no overlay writes, no commands, no events, no broker calls) and it never blocks
anything automatically; the Execution Orchestrator remains the sole executor.

`risk_engine.py` imports nothing from `server.py`. The runtime injects a read-only
`RiskEnv` (deployment / account / market snapshot / active-package hash / runtime
health / broker health). All risk *numbers* are REUSED from existing infrastructure
(deployment `riskState` aggregates, account `fundedRules`, the Market Data snapshot,
runtime + broker health) — the engine recomputes no risk math of its own.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Severity ladder. deny > warn > allow (worst check wins).
# ---------------------------------------------------------------------------

ALLOW = "allow"
WARN = "warn"
DENY = "deny"
_RANK = {ALLOW: 0, WARN: 1, DENY: 2}

CHECKS = [
    "max_open_trades", "max_exposure", "max_daily_loss", "max_floating_loss",
    "market_state_compat", "package_compat", "runtime_health", "broker_health",
]

# Engine-level defaults (NOT live account/broker limits — advisory ceilings that
# layer over the fixture's own `fundedRules`).
DEFAULT_LIMITS = {
    "maxOpenTrades": 5,
    "maxExposureLots": 20.0,   # falls back to account fundedRules.maxLot
    "maxDailyLoss": 5000.0,    # falls back to account fundedRules.dailyLossLimit
    "maxFloatingLoss": 5000.0,  # falls back to 0.5 × fundedRules.maxDrawdown
    "minMarketConfidence": 0.6,
}


# ---------------------------------------------------------------------------
# Immutable RiskAssessment (Objective 2).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RiskAssessment:
    timestamp: str
    deployment: str | None
    account: str | None
    symbol: str | None
    severity: str          # allow | warn | deny
    allowed: bool          # severity != deny
    reason: str
    checks: tuple          # ({name, severity, passed, detail, observed, limit})
    limits: dict           # the active limit set used
    metrics: dict          # the observed risk numbers (reused, not recomputed)


# ---------------------------------------------------------------------------
# Read-only environment injected by the runtime.
# ---------------------------------------------------------------------------

@dataclass
class RiskEnv:
    deployment: Callable[[str], dict | None]        # runtime-overlaid deployment (riskState aggregates)
    account: Callable[[str], dict | None]           # account incl. fundedRules
    market_snapshot: Callable[[str], dict | None]   # current immutable Market Data snapshot
    active_package_hash: Callable[[], str]
    runtime_health: Callable[[], dict]              # {runtimeDbHealthy, eventStoreHealthy, ...}
    broker_health: Callable[[], dict]              # {connection, ...}
    now: str


def _check(name: str, severity: str, detail: str, observed: Any, limit: Any) -> dict:
    return {"name": name, "severity": severity, "passed": severity != DENY,
            "detail": detail, "observed": observed, "limit": limit}


def _limits_for(account: dict | None) -> dict:
    """Active limit set: engine defaults overlaid with the fixture account's own
    fundedRules (reused, not invented)."""
    rules = (account or {}).get("fundedRules") or {}
    limits = dict(DEFAULT_LIMITS)
    if rules.get("maxLot") is not None:
        limits["maxExposureLots"] = float(rules["maxLot"])
    if rules.get("dailyLossLimit") is not None:
        limits["maxDailyLoss"] = float(rules["dailyLossLimit"])
    if rules.get("maxDrawdown") is not None:
        limits["maxFloatingLoss"] = round(float(rules["maxDrawdown"]) * 0.5, 2)
    return limits


# ---------------------------------------------------------------------------
# Risk Engine (Objectives 1, 3, 4, 5). Assessment only — executes nothing.
# ---------------------------------------------------------------------------

class RiskEngine:
    def __init__(self, metrics: "RiskMetrics"):
        self.metrics = metrics

    def limits(self, env: RiskEnv, account_id: str | None = None) -> dict:
        return _limits_for(env.account(account_id) if account_id else None)

    def assess(self, env: RiskEnv, deployment_id: str, symbol: str | None = None,
               proposed_command: dict | None = None) -> RiskAssessment:
        t0 = time.perf_counter()
        dep = env.deployment(deployment_id) or {}
        account_id = dep.get("accountId")
        account = env.account(account_id) if account_id else None
        sym = symbol or dep.get("pair")
        risk = dep.get("riskState") or {}
        limits = _limits_for(account)
        checks: list[dict] = []

        # 1. maximum open trades ------------------------------------------------
        open_trades = int(risk.get("openTrades") or 0)
        lim = int(limits["maxOpenTrades"])
        sev = DENY if open_trades > lim else (WARN if open_trades == lim else ALLOW)
        checks.append(_check("max_open_trades", sev, f"{open_trades}/{lim} open trades",
                             open_trades, lim))

        # 2. maximum exposure ---------------------------------------------------
        exposure = float(risk.get("exposure") or 0.0)
        lim = float(limits["maxExposureLots"])
        sev = DENY if exposure > lim else (WARN if exposure >= 0.9 * lim else ALLOW)
        checks.append(_check("max_exposure", sev, f"{exposure} / {lim} lots", exposure, lim))

        # 3. maximum daily loss (dailyPl is signed; loss is negative) -----------
        daily_pl = float(risk.get("dailyPl") or 0.0)
        lim = float(limits["maxDailyLoss"])
        sev = DENY if daily_pl <= -lim else (WARN if daily_pl <= -0.8 * lim else ALLOW)
        checks.append(_check("max_daily_loss", sev, f"dailyPl {daily_pl} vs -{lim}", daily_pl, -lim))

        # 4. maximum floating loss ---------------------------------------------
        floating_pl = float(risk.get("floatingPl") or 0.0)
        lim = float(limits["maxFloatingLoss"])
        sev = DENY if floating_pl <= -lim else (WARN if floating_pl <= -0.8 * lim else ALLOW)
        checks.append(_check("max_floating_loss", sev, f"floatingPl {floating_pl} vs -{lim}",
                             floating_pl, -lim))

        # 5. market-state compatibility (from the Market Data snapshot) ---------
        snap = env.market_snapshot(sym) if sym else None
        ms = (snap or {}).get("marketState") if snap else None
        if not ms:
            checks.append(_check("market_state_compat", WARN, "no market snapshot", None, None))
        else:
            conf = float(ms.get("confidence") or 0.0)
            confirmed = bool(ms.get("confirmed"))
            min_conf = float(limits["minMarketConfidence"])
            sev = ALLOW if (confirmed and conf >= min_conf) else WARN
            checks.append(_check("market_state_compat", sev,
                                 f"{ms.get('state')} @ {conf:.0%}{'' if confirmed else ' (unconfirmed)'}",
                                 {"state": ms.get("state"), "confidence": conf, "confirmed": confirmed},
                                 {"minConfidence": min_conf}))

        # 6. package compatibility (deployment pin vs active package) -----------
        dep_hash = dep.get("packageHash") or ""
        active_hash = env.active_package_hash() or ""
        sev = ALLOW if dep_hash == active_hash else DENY
        checks.append(_check("package_compat", sev,
                             "package matches active" if sev == ALLOW else "deployment pinned to a non-active package",
                             dep_hash[:24], active_hash[:24]))

        # 7. runtime health -----------------------------------------------------
        rh = env.runtime_health() or {}
        rt_ok = bool(rh.get("runtimeDbHealthy", True)) and bool(rh.get("eventStoreHealthy", True))
        checks.append(_check("runtime_health", ALLOW if rt_ok else DENY,
                             "runtime healthy" if rt_ok else "runtime degraded", rt_ok, True))

        # 8. broker health ------------------------------------------------------
        bh = env.broker_health() or {}
        conn = bh.get("connection")
        if conn in ("Connected",):
            sev = ALLOW
        elif conn in ("Degraded", "Reconnecting", "Connecting"):
            sev = WARN
        else:
            sev = DENY
        checks.append(_check("broker_health", sev, f"broker {conn}", conn, "Connected"))

        # --- overall verdict = worst check ------------------------------------
        severity = ALLOW
        for c in checks:
            if _RANK[c["severity"]] > _RANK[severity]:
                severity = c["severity"]
        allowed = severity != DENY
        worst = [c for c in checks if c["severity"] == severity and severity != ALLOW]
        reason = (worst[0]["detail"] if worst else "all checks passed")
        metrics = {
            "openTrades": open_trades, "exposure": exposure, "dailyPl": daily_pl,
            "floatingPl": floating_pl, "openRiskPct": risk.get("openRiskPct"),
            "ddBufferPct": risk.get("ddBufferPct"),
        }

        assessment = RiskAssessment(
            timestamp=env.now, deployment=deployment_id or None, account=account_id, symbol=sym,
            severity=severity, allowed=allowed, reason=reason, checks=tuple(checks),
            limits=limits, metrics=metrics,
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        self.metrics.record(assessment, duration_ms, env.now)
        return assessment

    def assess_decision(self, env: RiskEnv, decision: dict) -> RiskAssessment:
        """Assess one strategy Decision (its deployment + proposed candidate command)."""
        candidate = (decision.get("candidateCommands") or [None])[0]
        return self.assess(env, decision.get("deploymentId", ""), decision.get("pair"),
                           proposed_command=candidate)


# ---------------------------------------------------------------------------
# Metrics (Objective 5).
# ---------------------------------------------------------------------------

@dataclass
class RiskMetrics:
    assessments: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0
    allowed: int = 0
    warnings: int = 0
    denials: int = 0
    last_assessment: dict | None = None
    last_at: str | None = None
    last_severity: str | None = None

    def record(self, assessment: RiskAssessment, duration_ms: float, now: str) -> None:
        self.assessments += 1
        self.total_ms += duration_ms
        self.last_ms = duration_ms
        self.last_at = now
        self.last_severity = assessment.severity
        if assessment.severity == DENY:
            self.denials += 1
        elif assessment.severity == WARN:
            self.warnings += 1
        else:
            self.allowed += 1
        self.last_assessment = {
            "deployment": assessment.deployment, "symbol": assessment.symbol,
            "severity": assessment.severity, "allowed": assessment.allowed,
            "reason": assessment.reason, "at": now,
        }

    def health(self) -> dict:
        avg = round(self.total_ms / self.assessments, 3) if self.assessments else 0.0
        return {
            "riskHealthy": True,
            "assessments": self.assessments,
            "allowed": self.allowed,
            "warnings": self.warnings,
            "denials": self.denials,
            "averageAssessmentMs": avg,
            "lastAssessmentMs": self.last_ms,
            "lastAssessmentAt": self.last_at,
            "lastSeverity": self.last_severity,
            "lastAssessment": self.last_assessment,
        }
