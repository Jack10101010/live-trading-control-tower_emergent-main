"""Pure account-identity model, policy, and verifier (LX-1 Slice 6).

Proves the connected MT5 account is the explicitly intended account BEFORE any
future live-arming gate can unlock execution. This module is preflight-only: it
performs no broker mutation, submits no order, and is never threaded into the
executor / rails / reconciliation. The future arming capstone consumes it.

Purity: stdlib only; no MetaTrader5 import, no gateway/state/filesystem/network.
Everything here is deterministic from plain-scalar inputs. Evidence is bounded
and JSON-safe and never contains credentials or raw SDK objects.

Config source-of-truth: the allowed logins and broker servers are REQUIRED and
have NO permissive default — an empty or malformed allowlist raises
``IdentityConfigError`` when the policy is built (at deploy-check preflight), so a
misconfiguration fails safely rather than silently matching everything.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

# Supported normalized trade modes (raw SDK enum ints are normalized to these).
TRADE_MODES = ("demo", "contest", "real")

_MAX_SERVER_LEN = 64
_MAX_CURRENCY_LEN = 8
_MAX_LOGINS = 64
_MAX_SERVERS = 64


class IdentityConfigError(ValueError):
    """The account-identity policy configuration is empty or malformed. Raised
    when the policy is built so a misconfiguration fails closed (never permissive)."""


@dataclass(frozen=True)
class AccountIdentity:
    """Immutable, validated, JSON-safe account identity — plain scalars only, no
    SDK object. Never carries password/credentials."""
    login: int
    server: str
    currency: str
    trade_mode: str
    balance: float
    equity: float

    def to_dict(self) -> dict:
        return {"login": self.login, "server": self.server, "currency": self.currency,
                "trade_mode": self.trade_mode, "balance": self.balance, "equity": self.equity}


@dataclass(frozen=True)
class IdentityPolicy:
    """Operator-frozen allowlist the connected account must satisfy."""
    allowed_logins: frozenset
    allowed_servers: frozenset
    expected_currency: str
    allowed_trade_modes: frozenset
    max_balance: float
    max_equity: float | None = None


@dataclass(frozen=True)
class AccountIdentityVerdict:
    allowed: bool
    reasons: tuple
    evidence: dict

    def to_dict(self) -> dict:
        return {"allowed": self.allowed, "reasons": list(self.reasons),
                "evidence": dict(self.evidence)}


# ── config parsing (strict; called when the policy is built) ────────────────────

def parse_login_set(raw) -> frozenset:
    """Comma-separated positive-integer logins. Empty/malformed/bool-like/
    non-positive -> IdentityConfigError. Deduplicated; whitespace-stripped."""
    if not isinstance(raw, str):
        raise IdentityConfigError("login allowlist must be a string")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        raise IdentityConfigError("empty login allowlist (LIVE_ALLOWED_LOGINS required)")
    if len(items) > _MAX_LOGINS:
        raise IdentityConfigError("too many logins in allowlist")
    out = set()
    for x in items:
        if x.lower() in ("true", "false"):
            raise IdentityConfigError(f"bool-like login {x!r}")
        try:
            v = int(x)
        except ValueError:
            raise IdentityConfigError(f"invalid login {x!r}")
        if v <= 0:
            raise IdentityConfigError(f"non-positive login {v}")
        out.add(v)
    return frozenset(out)


def parse_server_set(raw) -> frozenset:
    """Comma-separated non-empty bounded server names. Empty -> IdentityConfigError."""
    if not isinstance(raw, str):
        raise IdentityConfigError("server allowlist must be a string")
    items = [x.strip() for x in raw.split(",") if x.strip()]
    if not items:
        raise IdentityConfigError("empty server allowlist (LIVE_ALLOWED_SERVERS required)")
    if len(items) > _MAX_SERVERS:
        raise IdentityConfigError("too many servers in allowlist")
    for x in items:
        if len(x) > _MAX_SERVER_LEN:
            raise IdentityConfigError(f"server name too long ({len(x)} chars)")
    return frozenset(items)


def normalize_currency(raw) -> str:
    """Expected currency -> non-empty bounded uppercase. Malformed -> IdentityConfigError."""
    if not isinstance(raw, str):
        raise IdentityConfigError("expected currency must be a string")
    c = raw.strip().upper()
    if not c or len(c) > _MAX_CURRENCY_LEN or not c.isalpha():
        raise IdentityConfigError(f"malformed expected currency {raw!r}")
    return c


def parse_trade_mode_set(raw) -> frozenset:
    """Comma-separated subset of {demo, contest, real}. Empty/unsupported ->
    IdentityConfigError."""
    if not isinstance(raw, str):
        raise IdentityConfigError("trade-mode allowlist must be a string")
    modes = {x.strip().lower() for x in raw.split(",") if x.strip()}
    if not modes:
        raise IdentityConfigError("empty trade-mode allowlist")
    unsupported = modes - set(TRADE_MODES)
    if unsupported:
        raise IdentityConfigError(f"unsupported trade mode(s) {sorted(unsupported)}")
    return frozenset(modes)


# ── scalar validation (accessor helpers) ────────────────────────────────────────

def finite_nonneg(x):
    """A usable balance/equity: numeric, finite, >= 0, never bool. Else None."""
    if isinstance(x, bool) or x is None:
        return None
    if isinstance(x, (int, float)) and math.isfinite(x) and x >= 0:
        return float(x)
    return None


# ── pure verifier ───────────────────────────────────────────────────────────────

def verify_account_identity(identity, policy: IdentityPolicy) -> AccountIdentityVerdict:
    """Deterministic, fail-closed verification of a sampled identity against the
    policy. Reason codes are appended in a FIXED order; an unavailable (None) or
    non-``AccountIdentity`` value never passes. Evidence is bounded/JSON-safe."""
    if identity is None:
        return AccountIdentityVerdict(False, ("identity_unavailable",), {})
    if not isinstance(identity, AccountIdentity):
        return AccountIdentityVerdict(False, ("identity_malformed",), {})
    reasons = []
    if identity.login not in policy.allowed_logins:
        reasons.append("login_not_allowed")
    if identity.server not in policy.allowed_servers:
        reasons.append("server_not_allowed")
    if identity.currency != policy.expected_currency:
        reasons.append("currency_mismatch")
    if identity.trade_mode not in policy.allowed_trade_modes:
        reasons.append("trade_mode_not_allowed")
    if identity.balance > policy.max_balance:          # equality at ceiling is allowed
        reasons.append("balance_above_ceiling")
    if policy.max_equity is not None and identity.equity > policy.max_equity:
        reasons.append("equity_above_ceiling")
    return AccountIdentityVerdict(len(reasons) == 0, tuple(reasons), identity.to_dict())
