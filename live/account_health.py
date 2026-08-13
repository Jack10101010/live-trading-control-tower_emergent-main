"""Pure account-health model, policy, and evaluator (LX-1 Slice 7).

A runtime, per-OPEN-cycle capital & trade-permission gate: proves the connected
account has enough equity, is permitted to trade (account + expert/algo), and is
still in the expected currency BEFORE any OPEN is submitted. Distinct from the
Slice-6 identity primitive (allowlist/binding, verified at deploy/arming) — this
is sampled every cycle that has OPEN intents.

Purity: stdlib only; no MetaTrader5 import, no gateway/state/filesystem/network,
no order submission, no mutation. Deterministic from plain-scalar inputs. Evidence
is bounded, JSON-safe, and never contains credentials or raw SDK objects.

Config source-of-truth: ``LIVE_MIN_EQUITY`` is REQUIRED and has NO permissive
default — an empty/malformed value raises ``HealthConfigError`` when the policy is
built (``LiveConfig.health_policy()``), so a misconfiguration fails closed (blocks
OPENs) rather than silently disabling the capital floor.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

_MAX_CURRENCY_LEN = 8


class HealthConfigError(ValueError):
    """The account-health policy configuration is missing or malformed. Raised
    when the policy is built so a misconfiguration fails closed (never permissive)."""


@dataclass(frozen=True)
class AccountHealth:
    """Immutable, validated, JSON-safe account health — plain scalars only, no SDK
    object, no credentials."""
    currency: str
    balance: float
    equity: float
    free_margin: float
    trade_allowed: bool
    trade_expert: bool

    def to_dict(self) -> dict:
        return {"currency": self.currency, "balance": self.balance, "equity": self.equity,
                "free_margin": self.free_margin, "trade_allowed": self.trade_allowed,
                "trade_expert": self.trade_expert}


@dataclass(frozen=True)
class HealthPolicy:
    """Operator-frozen capital floor + expected currency. Trusted by the evaluator
    ONLY because it is constructed via ``LiveConfig.health_policy()``, which
    strictly validates ``min_equity`` (finite > 0) and the currency; a hand-forged
    policy with an invalid floor is outside that guaranteed path."""
    min_equity: float
    expected_currency: str


@dataclass(frozen=True)
class HealthVerdict:
    allowed: bool
    reasons: tuple
    evidence: dict

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons),
                "evidence": dict(self.evidence)}


def finite_nonneg(x):
    """A usable balance/equity/free-margin: numeric, finite, >= 0, never bool.
    Else None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)) and math.isfinite(x) and x >= 0:
        return float(x)
    return None


def parse_min_equity(raw) -> float:
    """Strictly parse LIVE_MIN_EQUITY: finite float, strictly > 0. Missing/empty/
    malformed/zero/negative/NaN/inf -> HealthConfigError (fail closed)."""
    if raw is None:
        raise HealthConfigError("LIVE_MIN_EQUITY is required (no default)")
    if isinstance(raw, bool):
        raise HealthConfigError("LIVE_MIN_EQUITY must be a number, not a bool")
    try:
        v = float(raw)
    except (TypeError, ValueError):
        raise HealthConfigError(f"invalid LIVE_MIN_EQUITY {raw!r} (not a number)")
    if not math.isfinite(v) or v <= 0:
        raise HealthConfigError(f"invalid LIVE_MIN_EQUITY {raw!r} (must be finite and > 0)")
    return v


def _policy_invalid(policy) -> bool:
    """S7-F1 hardening: boundary validation of the policy itself. The production
    path (``LiveConfig.health_policy()``) already validates, but this evaluator is
    composed for real money by the live-arming capstone — a forged policy with a
    NaN/zero/negative/bool ``min_equity`` would otherwise silently disable the
    equity floor (``equity < NaN`` is False), so it must fail closed here."""
    if not isinstance(policy, HealthPolicy):
        return True
    m = policy.min_equity
    if isinstance(m, bool) or not isinstance(m, (int, float)) \
            or not math.isfinite(m) or m <= 0:
        return True
    cur = policy.expected_currency
    if not isinstance(cur, str) or not cur.strip() or len(cur) > _MAX_CURRENCY_LEN \
            or not cur.isalpha() or cur != cur.upper():
        return True
    return False


def evaluate_health(health, policy: HealthPolicy) -> HealthVerdict:
    """Deterministic, fail-closed capital & permission evaluation. Reason codes are
    appended in a FIXED order; an unavailable (None) or non-``AccountHealth`` value
    never passes, and a malformed/forged policy fails closed as
    ``health_policy_invalid``. Equity equality at the floor is allowed
    (``equity >= min``). Evidence is bounded and JSON-safe."""
    if _policy_invalid(policy):
        return HealthVerdict(False, ("health_policy_invalid",), {})
    if not isinstance(health, AccountHealth):
        return HealthVerdict(False, ("account_health_unavailable",), {})
    reasons = []
    if health.trade_allowed is not True:
        reasons.append("trading_not_allowed")
    if health.trade_expert is not True:
        reasons.append("expert_trading_not_allowed")
    if health.currency != policy.expected_currency:
        reasons.append("currency_mismatch")
    if health.equity < policy.min_equity:                  # equality at the floor passes
        reasons.append("equity_floor")
    ev = {"currency": health.currency, "equity": health.equity, "balance": health.balance,
          "free_margin": health.free_margin, "trade_allowed": health.trade_allowed,
          "trade_expert": health.trade_expert, "min_equity": policy.min_equity,
          "expected_currency": policy.expected_currency}
    return HealthVerdict(len(reasons) == 0, tuple(reasons), ev)
