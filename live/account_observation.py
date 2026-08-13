"""Read-only MT5 account observation, independent of the order path.

WHY THIS EXISTS
---------------
The historical UI-2 implementation recorded account facts on
`Executor.observed`, populated inside `Executor.apply(intents, ...)`. `apply()`
runs only on cycles that produced intents, so the Control Tower saw account data
only when the strategy happened to trade — and never at all on a node that has
never opened a position. That is the defect M-NODE-ACCT-1 removes, so this seam
deliberately has no relationship with intents, positions, bars or strategy work.

WHAT IT MAY DO
--------------
Read `terminal_info()` and `account_info()` through the ALREADY-CONNECTED
governed gateway, and nothing else. It never connects, never submits, modifies,
cancels or closes anything, never touches reconciliation or the ledger, and
never enables AutoTrading. Observation failure is contained and returned as an
unavailable observation — it can never propagate out and stop a trading cycle.

HONESTY RULES
-------------
`available` is a claim that this node really observed something, so it is only
ever true after a successful read. A failed refresh does not invent a timestamp
and does not silently keep presenting the last good sample as current: cached
data retains its ORIGINAL `observed_at`, and a failure marks the observation
unavailable while preserving the old sample's age for diagnostics. Identity and
health availability move independently, because a terminal can answer one and
not the other.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

#: How long a successful sample may be reused before a refresh is attempted.
#: Identity is slower because login/server/currency/trade_mode change only when
#: an operator switches account — polling it per cycle would be pure cost.
IDENTITY_TTL_S = 900.0
#: Health carries money values, which move constantly; a short bound keeps the
#: published figures meaningful without polling the terminal continuously.
HEALTH_TTL_S = 60.0
#: Upper bound on a single sample. The gateway call is local IPC, so anything
#: near this means the terminal is wedged and the cycle should not wait on it.
SAMPLE_TIMEOUT_S = 5.0
#: Sanitised error text length. Bounded so a broker message cannot bloat the
#: payload, and truncated rather than dropped so the reason stays diagnosable.
MAX_ERROR_CHARS = 160

STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_DEGRADED = "degraded"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _sanitise(text: object) -> str:
    """Bounded, credential-free error text.

    Terminal paths and login numbers can appear in SDK messages, so anything
    that looks like a filesystem path is dropped rather than published.
    """
    s = str(text or "").replace("\n", " ").strip()
    if "\\" in s or "://" in s:
        s = s.split("\\")[0].split("://")[0].strip() or "unavailable"
    return s[:MAX_ERROR_CHARS]


@dataclass(frozen=True)
class ObservedIdentity:
    """Attribute names are load-bearing.

    `telemetry.safe_identity` decides availability with `_carries(identity,
    ("login", "server", "currency", "trade_mode"))` — an object missing them is
    treated as NOT observed. This exposes exactly those names.
    """

    login: object
    server: object
    currency: object
    trade_mode: object


@dataclass(frozen=True)
class ObservedHealth:
    """Mirrors what `telemetry.safe_health` reads.

    `trade_allowed`/`trade_expert` here are BROKER permissions from
    `account_info`. The terminal AutoTrading toggle is deliberately absent —
    the canonical account contract has no field for it, and overloading a broker
    permission would let a locked terminal read as trade-enabled. It is carried
    on the observation itself for local diagnostics instead.
    """

    balance: object
    equity: object
    free_margin: object
    currency: object
    trade_allowed: object
    trade_expert: object


@dataclass(frozen=True)
class AccountObservation:
    """One immutable observation result."""

    sampled_at: str
    status: str = STATUS_UNAVAILABLE
    identity: ObservedIdentity | None = None
    health: ObservedHealth | None = None
    identity_observed_at: str | None = None
    health_observed_at: str | None = None
    fresh_or_cached: str = "fresh"
    latency_ms: float | None = None
    error: str | None = None
    #: Terminal AutoTrading, for LOCAL diagnostics only. Never published as a
    #: broker permission.
    terminal_trade_allowed: bool | None = None
    terminal_connected: bool | None = None

    def as_observed_mapping(self) -> dict:
        """The `observed` mapping `telemetry.build_snapshot` consumes.

        Only identity/health/observed_at are supplied. `market`,
        `fingerprint_matches`, `health_verdict` and `reconciled_at` are left
        absent rather than guessed: this seam observes the account, and inventing
        a verdict here would duplicate a judgement the rails own.
        """
        return {
            "identity": self.identity,
            "health": self.health,
            "health_verdict": None,
            "observed_at": self.health_observed_at or self.identity_observed_at,
        }

    def as_arm_binding(self) -> dict:
        """The SAME observation, projected for `SafetyRails` arm authorization.

        M-ARM-ACCOUNT-SOURCE-FIX-1. Before this existed the rails compared
        against a snapshot taken in `build()`, BEFORE the gateway was ever
        connected, which therefore read "not connected" and left the binding
        empty for the entire life of the process. Every OPEN was refused
        `arm_server_mismatch` while telemetry, which re-read the account over
        the live session, published `fingerprint_matches: true`. Two
        authorities for one fact, and the reassuring one was the one on screen.
        This method exists so there is exactly one observation and both
        consumers project from it.

        Returns `{}` when the account was not observed. That is deliberate and
        fail-closed: `authorize_open` compares `str(None)` against the bound
        server and refuses, so an unobservable account cannot open a position.
        Nothing here is ever defaulted from config or from the token — the
        configured account is what an operator can get wrong, and the token is
        the claim being tested, so neither may stand in for the observation.
        """
        if self.status == STATUS_UNAVAILABLE or self.identity is None:
            return {}
        return {"login": self.identity.login,
                "server": self.identity.server,
                "trade_mode": self.identity.trade_mode}


class AccountObserver:
    """Bounded-cadence, read-only account sampler.

    Deliberately owns its cache in process memory. Persisting account values into
    runtime state would make a stale figure survive a restart and reappear
    looking fresh.
    """

    def __init__(self, gateway, identity_ttl_s: float = IDENTITY_TTL_S,
                 health_ttl_s: float = HEALTH_TTL_S,
                 clock=None, monotonic=None):
        self._gateway = gateway
        self._identity_ttl = float(identity_ttl_s)
        self._health_ttl = float(health_ttl_s)
        self._clock = clock or _now
        self._monotonic = monotonic or time.monotonic
        self._identity: ObservedIdentity | None = None
        self._identity_at: str | None = None
        self._identity_mono: float | None = None
        self._health: ObservedHealth | None = None
        self._health_at: str | None = None
        self._health_mono: float | None = None

    # ── cadence ──────────────────────────────────────────────────────────────
    def _expired(self, stamp: float | None, ttl: float) -> bool:
        if stamp is None:
            return True
        return (self._monotonic() - stamp) >= ttl

    def observe(self) -> AccountObservation:
        """Sample if due, otherwise reuse. NEVER raises.

        Called from the publication path, which must remain fail-soft; a broker
        or SDK fault here must not reach the trading cycle.
        """
        started = self._monotonic()
        sampled_at = _iso(self._clock())
        need = self._expired(self._identity_mono, self._identity_ttl) or \
            self._expired(self._health_mono, self._health_ttl)

        if not need:
            return self._result(sampled_at, "cached", None, None,
                                (self._monotonic() - started) * 1000.0)

        ok, payload, err = False, None, None
        try:
            ok, payload = self._gateway.read_account_state()
            if not ok:
                err = _sanitise(payload)
                payload = None
        except Exception as exc:            # noqa: BLE001 — containment is the point
            ok, payload, err = False, None, _sanitise(f"{type(exc).__name__}: {exc}")

        latency_ms = (self._monotonic() - started) * 1000.0
        if not ok or not isinstance(payload, dict):
            # Failure does NOT clear the previous sample (it stays available for
            # age diagnostics) but it must not be re-stamped as newly observed.
            return self._result(sampled_at, "cached" if self._identity or self._health
                                else "fresh", err or "account state unavailable",
                                None, latency_ms, failed=True)

        return self._store(payload, sampled_at, latency_ms)

    # ── sampling ─────────────────────────────────────────────────────────────
    def _store(self, payload: dict, sampled_at: str, latency_ms: float) -> AccountObservation:
        acct = payload.get("account") or {}
        term = payload.get("terminal") or {}
        now_iso = _iso(self._clock())
        mono = self._monotonic()

        ident = ObservedIdentity(login=acct.get("login"), server=acct.get("server"),
                                 currency=acct.get("currency"),
                                 trade_mode=acct.get("trade_mode"))
        # A login/server pair is the minimum that makes an identity meaningful;
        # without it the fingerprint would be None and `available: true` would be
        # a claim about nothing.
        if ident.login is not None and ident.server is not None:
            self._identity, self._identity_at, self._identity_mono = ident, now_iso, mono

        health = ObservedHealth(
            balance=acct.get("balance"), equity=acct.get("equity"),
            free_margin=acct.get("free_margin"), currency=acct.get("currency"),
            trade_allowed=acct.get("trade_allowed"),
            trade_expert=acct.get("trade_expert"))
        if _finite(health.balance) and _finite(health.equity):
            self._health, self._health_at, self._health_mono = health, now_iso, mono

        return self._result(sampled_at, "fresh", None, term, latency_ms)

    def _result(self, sampled_at: str, fresh_or_cached: str, error: str | None,
                term: dict | None, latency_ms: float,
                failed: bool = False) -> AccountObservation:
        have_id, have_hl = self._identity is not None, self._health is not None
        if failed or not (have_id or have_hl):
            status = STATUS_UNAVAILABLE if not (have_id or have_hl) else STATUS_DEGRADED
        elif have_id and have_hl:
            status = STATUS_OK
        else:
            status = STATUS_DEGRADED
        return AccountObservation(
            sampled_at=sampled_at, status=status,
            # A failed refresh must not present the cached sample as CURRENT
            # truth, so identity/health are withheld from the published mapping
            # while their original observed_at is retained for diagnostics.
            identity=None if failed else self._identity,
            health=None if failed else self._health,
            identity_observed_at=self._identity_at,
            health_observed_at=self._health_at,
            fresh_or_cached=fresh_or_cached, latency_ms=round(latency_ms, 3),
            error=error,
            terminal_trade_allowed=None if not term else term.get("trade_allowed"),
            terminal_connected=None if not term else term.get("connected"))


def _finite(value: object) -> bool:
    """Reject NaN/Infinity before they can reach JSON, and booleans before they
    can be mistaken for money."""
    if isinstance(value, bool) or value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf"))
