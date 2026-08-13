"""M-ENV-1 — the deployment-environment admissibility boundary.

THE PROBLEM THIS SOLVES
    Two production capabilities were selected by FAIL-OPEN defaults:

      * `broker_adapter.active_kind()` returned `"mock"` when
        `CONTROL_TOWER_BROKER_ADAPTER` was unset;
      * `MARKET_DATA_PROVIDER` unset resolved to `"fixture"`.

    A production deployment that forgot one environment variable therefore ran
    the mock broker and the fixture feed while every operator surface reported
    healthy. The failure was SILENT, which is the one failure mode this
    programme exists to eliminate.

WHAT THIS MODULE IS — AND IS NOT
    It is a SAFETY POSTURE, not a behaviour selector, and it introduces NO new
    runtime-mode enum. The tower already owns three distinct "mode" vocabularies
    and none of them means "which deployment is this":

      * `security_config.VAR_MODE` (`CONTROL_TOWER_MODE`) — the tower's own
        operating mode (`observe`);
      * `execution_mode` — the durable, operator-governed execution state
        (`observe` / `manual_live` / `halted`), derived from an append-only
        transition log;
      * `connection_policy` — the outbound-connection profile.

    Adding a fourth "mode" would collide with all three. So behaviour continues
    to be selected exactly where it already is — the per-capability variables
    `CONTROL_TOWER_BROKER_ADAPTER` and `MARKET_DATA_PROVIDER`. This module only
    declares which of their values are ADMISSIBLE, following the
    `connection_policy.APPROVED_PROFILES` idiom already proven here: deny by
    default, unknown values denied verbatim, nothing falls back.

WHY UNSET MEANS DEVELOPMENT
    Refusing to start on an unset variable would be loud, but it would also make
    every test, every `uvicorn --reload`, and every CI job carry an environment
    variable to do nothing — a large blast radius for a guarantee this design
    reaches anyway. Production is never a DEFAULT: it is only ever entered by an
    explicit, deliberate value. Forgetting the variable therefore yields a
    development instance that is VISIBLY non-live (RED provenance borders,
    honest `computed:false` contracts, `environment: development` on
    `/api/health`) — a visible failure rather than a silent one. Fail-closed is
    preserved WHERE IT MATTERS: once `production` is selected, every capability
    must be explicitly admissible or startup aborts.

    Note the asymmetry that makes this safe: an unknown value is NOT coerced to
    development. `CONTROL_TOWER_ENVIRONMENT=prod` is a typo, not a request for a
    development instance, and it is rejected.

THE MATRIX IS VERIFIED, NOT ASSUMED
    The admissible sets below are the repository's ACTUAL registries:
    `broker_adapter.known_kinds() == ("mock", "mt5")`, and the market-data
    engine registers exactly `fixture`, `replay`, `mock_live`, `mt5`. There is
    no `store` or `polygon` PROVIDER — those are candle SOURCES inside
    `data_service.DataService`, consumed by the fixture provider. `mt5` is
    consequently the only production-admissible feed today.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# ── the environment axis ──────────────────────────────────────────────────────
ENV_DEVELOPMENT = "development"
ENV_PRODUCTION = "production"
KNOWN_ENVIRONMENTS = frozenset({ENV_DEVELOPMENT, ENV_PRODUCTION})

#: Absent/blank resolves here. An UNKNOWN value does not — it is rejected.
DEFAULT_ENVIRONMENT = ENV_DEVELOPMENT

VAR_ENVIRONMENT = "CONTROL_TOWER_ENVIRONMENT"

#: The capability variables this module governs. It does not own their meaning —
#: it is named here so the admissibility check and the capability read agree on
#: exactly one spelling.
VAR_BROKER_ADAPTER = "CONTROL_TOWER_BROKER_ADAPTER"
VAR_MARKET_DATA_PROVIDER = "MARKET_DATA_PROVIDER"

# ── the verified capability registries (see module docstring) ────────────────
KNOWN_BROKER_ADAPTERS = frozenset({"mock", "mt5"})
KNOWN_MARKET_DATA_PROVIDERS = frozenset({"fixture", "replay", "mock_live", "mt5"})

# ── reason codes (stable, machine-readable, capability + value named) ────────
REASON_ENVIRONMENT_INVALID = "environment_invalid"
REASON_BROKER_UNSET = "broker_adapter_unset"
REASON_BROKER_INADMISSIBLE = "broker_adapter_inadmissible"
REASON_BROKER_UNKNOWN = "broker_adapter_unknown"
REASON_PROVIDER_UNSET = "market_provider_unset"
REASON_PROVIDER_INADMISSIBLE = "market_provider_inadmissible"
REASON_PROVIDER_UNKNOWN = "market_provider_unknown"
REASON_FIXTURE_FORBIDDEN = "fixture_world_activation_forbidden"


class EnvironmentViolation(RuntimeError):
    """Startup is refused. `reason` is the stable code; `str(exc)` is the exact
    `code: value` diagnostic an operator reads in the abort line."""

    def __init__(self, reason: str, value: str | None = None):
        self.reason = reason
        self.value = value
        super().__init__(f"{reason}: {value}" if value is not None else reason)


@dataclass(frozen=True)
class EnvironmentPolicy:
    """The immutable admissibility answer for one environment.

    Everything the boundary decides is DATA, not branching, so a later milestone
    widens a set rather than editing boot logic.
    """

    environment: str
    broker_adapters: frozenset
    market_data_providers: frozenset
    world_may_load: bool
    replay_permitted: bool

    @property
    def is_production(self) -> bool:
        return self.environment == ENV_PRODUCTION


#: Development admits everything the repository can construct — this is exactly
#: today's behaviour, preserved deliberately and bit-for-bit.
POLICY_DEVELOPMENT = EnvironmentPolicy(
    environment=ENV_DEVELOPMENT,
    broker_adapters=frozenset(KNOWN_BROKER_ADAPTERS),
    market_data_providers=frozenset(KNOWN_MARKET_DATA_PROVIDERS),
    world_may_load=True,
    replay_permitted=True,
)

#: Production admits only capabilities that reach a genuine external system.
POLICY_PRODUCTION = EnvironmentPolicy(
    environment=ENV_PRODUCTION,
    broker_adapters=frozenset({"mt5"}),
    market_data_providers=frozenset({"mt5"}),
    world_may_load=False,
    replay_permitted=False,
)

POLICIES: dict[str, EnvironmentPolicy] = {
    ENV_DEVELOPMENT: POLICY_DEVELOPMENT,
    ENV_PRODUCTION: POLICY_PRODUCTION,
}


# ── the ONE parser ────────────────────────────────────────────────────────────
def resolve() -> str:
    """The single canonical read of `CONTROL_TOWER_ENVIRONMENT`.

    No other module may parse this variable. Absent/blank -> development. An
    unrecognised value RAISES: coercing a typo to development would be a silent
    fallback, which is the class of defect this boundary exists to remove.
    """
    raw = (os.environ.get(VAR_ENVIRONMENT) or "").strip().lower()
    if not raw:
        return DEFAULT_ENVIRONMENT
    if raw not in KNOWN_ENVIRONMENTS:
        raise EnvironmentViolation(REASON_ENVIRONMENT_INVALID, raw)
    return raw


def policy(environment: str | None = None) -> EnvironmentPolicy:
    """The admissibility policy for `environment` (default: the resolved one)."""
    return POLICIES[environment or resolve()]


def is_production() -> bool:
    """True only when production was EXPLICITLY selected. Never inferred."""
    return resolve() == ENV_PRODUCTION


# ── capability reads (blank -> None, so "unset" stays distinguishable) ───────
def configured_broker_adapter() -> str | None:
    raw = (os.environ.get(VAR_BROKER_ADAPTER) or "").strip().lower()
    return raw or None


def configured_market_data_provider() -> str | None:
    raw = (os.environ.get(VAR_MARKET_DATA_PROVIDER) or "").strip().lower()
    return raw or None


# ── admissibility ─────────────────────────────────────────────────────────────
def _check(value: str | None, known: frozenset, admissible: frozenset,
           unset: str, unknown: str, inadmissible: str) -> None:
    """Deny by default, and say precisely WHICH denial applies.

    Order matters: unknown is reported before inadmissible so an operator who
    typed `mockk` is not told the value is a development-only capability.
    """
    if value is None:
        raise EnvironmentViolation(unset)
    if value not in known:
        raise EnvironmentViolation(unknown, value)
    if value not in admissible:
        raise EnvironmentViolation(inadmissible, value)


def require_broker_adapter_admissible(value: str | None,
                                      environment: str | None = None) -> None:
    """Raise unless `value` may be used as the broker adapter here.

    Callable on its own so a caller that only cares about the adapter cannot be
    made to fail for an unrelated capability if `validate`'s check order ever
    changes.
    """
    pol = policy(environment)
    if not pol.is_production:
        return
    _check(value, KNOWN_BROKER_ADAPTERS, pol.broker_adapters,
           REASON_BROKER_UNSET, REASON_BROKER_UNKNOWN, REASON_BROKER_INADMISSIBLE)


def require_market_data_provider_admissible(value: str | None,
                                            environment: str | None = None) -> None:
    """Raise unless `value` may be used as the market-data provider here."""
    pol = policy(environment)
    if not pol.is_production:
        return
    _check(value, KNOWN_MARKET_DATA_PROVIDERS, pol.market_data_providers,
           REASON_PROVIDER_UNSET, REASON_PROVIDER_UNKNOWN, REASON_PROVIDER_INADMISSIBLE)


def validate(environment: str, broker_adapter: str | None,
             market_data_provider: str | None) -> None:
    """Raise `EnvironmentViolation` on the first inadmissible capability.

    Development admits every known capability, so this is a no-op there beyond
    rejecting values the repository cannot construct at all.
    """
    require_broker_adapter_admissible(broker_adapter, environment)
    require_market_data_provider_admissible(market_data_provider, environment)


def enforce_startup() -> str:
    """Resolve the environment and validate every capability, ONCE, at boot.

    Called from `server.py` at module scope BEFORE the fixture world is loaded
    and before any provider is registered, so a rejected production
    configuration never reaches a state where a store could be written, a loop
    started, or a fixture-backed value served. Returns the resolved environment.
    """
    environment = resolve()
    validate(environment, configured_broker_adapter(),
             configured_market_data_provider())
    return environment


def require_fixture_activation_allowed() -> None:
    """Guard for any path that would ACTIVATE the fixture world.

    Additive defence behind `enforce_startup`: startup ordering already prevents
    this in `server.py`, but a future call site that loads the fixture from
    somewhere else fails loudly here instead of quietly succeeding.
    """
    if not policy().world_may_load:
        raise EnvironmentViolation(REASON_FIXTURE_FORBIDDEN, ENV_PRODUCTION)


def summarise_for_log(environment: str) -> str:
    """One startup line. Mirrors `cors_policy.summarise_for_log` in shape."""
    pol = POLICIES[environment]
    return (f"ENVIRONMENT resolved={pol.environment} "
            f"broker_adapters={len(pol.broker_adapters)} "
            f"market_data_providers={len(pol.market_data_providers)} "
            f"fixture_world_permitted={str(pol.world_may_load).lower()}")
