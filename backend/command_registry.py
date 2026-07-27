"""ARCH-1 — the single canonical command registry.

This module is the ONE authoritative catalogue of every command the Control Tower
knows about. Before ARCH-1 the same command set was declared five times, in five
shapes, that could silently drift:

  * `server.KNOWN_COMMANDS`          — the fixture control-plane vocabulary
  * `server._COMMAND_CATEGORY`       — per-command audit category
  * `broker.BROKER_COMMANDS`         — which commands dispatch to the broker
  * `execution.COMMAND_SPEC`         — validators + feasibility policy per command
  * `execution_safety._RISK_BY_COMMAND` — a *separate* snake_case risk vocabulary

Audit A showed the fixture vocabulary (PascalCase, e.g. `CloseTrade`) and the safety
vocabulary (snake_case, e.g. `close_position`) did not intersect, so naively wiring
the safety gate would have classified every real command as "unknown" and denied the
whole surface. This registry unifies them: one canonical entry per command, with
`aliases` connecting the snake_case safety names to their canonical PascalCase
equivalents.

OWNERSHIP (single owner for each concept):
  * command identity + canonical name + aliases  — here
  * risk classification                          — here (constants owned by execution_safety)
  * audit category (lifecycle metadata)          — here
  * which commands dispatch to the broker        — here (`broker_dispatched`)
  * required broker capability                   — here (`broker_capability`)
  * which surface may submit a command           — here (`surface`)

Every other module DERIVES from this registry; none re-declares a command list.

This module holds DATA only. It imports the read-only command constants from
`command_channel` (UI-15 owns that contract) and the risk-class constants from
`execution_safety` (which owns the policy engine); it imports no broker, no
transport, no server, and opens nothing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import command_channel as _cc
from execution_safety import (
    RISK_EMERGENCY,
    RISK_EXECUTION_AFFECTING,
    RISK_OPERATIONAL,
    RISK_READ_ONLY,
)

# ── surfaces: where (if anywhere) a command may be submitted ──────────────────
SURFACE_OPERATOR = "operator"   # UI-15/16/17 read-only operator channel
SURFACE_FIXTURE = "fixture"     # the fixture-world control plane (/api/commands/{name})
SURFACE_ABSTRACT = "abstract"   # classified for the safety vocabulary only; not submit-able
SURFACE_EXECUTION = "execution"  # LIVE-2: the dedicated execution surface
                                 # (POST /api/execution/market-order ONLY — the
                                 # fixture route rejects these as unknown)


@dataclass(frozen=True)
class CommandSpec:
    """One canonical command. Immutable."""
    canonical: str
    risk_class: str
    surface: str
    aliases: frozenset = field(default_factory=frozenset)
    category: str | None = None          # audit lifecycle category (fixture surface)
    broker_dispatched: bool = False      # does its effect route through the Broker?
    broker_capability: str | None = None  # BrokerCapability field required, if any
    # ARCH-2 lifecycle metadata:
    risk_reducing: bool = False          # closes/cancels/de-risks — allowed under
                                         # unresolved critical reconciliation
    intent_kind: str | None = None       # order_lifecycle KIND_* for broker commands
    # LIVE-3: available in the HALTED execution mode (emergency de-risking only —
    # every other gate still applies; nothing else runs while halted).
    halted_available: bool = False


# ── the catalogue ─────────────────────────────────────────────────────────────
# Read-only operator vocabulary: reuse UI-15's constants so the read-only contract
# is not duplicated here — the registry mirrors it, command_channel still owns it.
_OPERATOR: list[CommandSpec] = [
    CommandSpec(_cc.CMD_NOOP, RISK_READ_ONLY, SURFACE_OPERATOR),
    CommandSpec(_cc.CMD_REQUEST_HEALTH, RISK_READ_ONLY, SURFACE_OPERATOR),
    CommandSpec(_cc.CMD_REQUEST_TELEMETRY, RISK_READ_ONLY, SURFACE_OPERATOR),
]

# Abstract safety vocabulary: classified for `execution_safety` (and its tests) but
# submit-able through no surface. Declared for future use.
_ABSTRACT: list[CommandSpec] = [
    CommandSpec("request_reconcile", RISK_OPERATIONAL, SURFACE_ABSTRACT),
    CommandSpec("refresh_status", RISK_OPERATIONAL, SURFACE_ABSTRACT),
    CommandSpec("disarm", RISK_EMERGENCY, SURFACE_ABSTRACT),
]

# Fixture control-plane vocabulary (PascalCase). `aliases` map the snake_case safety
# names onto their canonical equivalents so `classify("close_position")` resolves to
# `CloseTrade`'s risk class. `category` is the audit lifecycle metadata.
_FIXTURE: list[CommandSpec] = [
    # Policy lifecycle — operational (touches drafts/policy, never trades)
    CommandSpec("CreateDraft", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("DiscardDraft", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("PromoteDraft", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("RunNativeValidation", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("ApproveRecommendation", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("RejectRecommendation", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("ApplyOverride", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    CommandSpec("RemoveOverride", RISK_OPERATIONAL, SURFACE_FIXTURE, category="policy"),
    # Package / deployment lifecycle
    CommandSpec("DeployPackage", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE, category="policy"),
    CommandSpec("RollbackPackage", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE, category="policy"),
    CommandSpec("PauseDeployment", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="system", aliases=frozenset({"pause_submission"}), risk_reducing=True),
    CommandSpec("ResumeDeployment", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="system", aliases=frozenset({"resume_submission"})),
    CommandSpec("KillDeployment", RISK_EMERGENCY, SURFACE_FIXTURE, category="system"),
    CommandSpec("FlattenDeployment", RISK_EMERGENCY, SURFACE_FIXTURE,
                category="system", aliases=frozenset({"flatten_all"})),
    CommandSpec("SetLaneMode", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE, category="system"),
    CommandSpec("LockDeployment", RISK_OPERATIONAL, SURFACE_FIXTURE, category="system"),
    CommandSpec("UnlockDeployment", RISK_OPERATIONAL, SURFACE_FIXTURE, category="system"),
    # Order management — broker-dispatched
    CommandSpec("CancelOrder", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="order", broker_dispatched=True, aliases=frozenset({"cancel_order"}),
                risk_reducing=True, intent_kind="cancel"),
    CommandSpec("ReduceOrderRisk", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="order", broker_dispatched=True, broker_capability="supportsModify",
                aliases=frozenset({"modify_order"}), risk_reducing=True, intent_kind="modify"),
    CommandSpec("ConvertOrderToGhost", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="order", broker_dispatched=True, risk_reducing=True, intent_kind="cancel"),
    # Trade management — broker-dispatched
    CommandSpec("CloseTrade", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, aliases=frozenset({"close_position"}),
                risk_reducing=True, intent_kind="close"),
    CommandSpec("SLToBE", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, risk_reducing=True, intent_kind="modify"),
    CommandSpec("MoveTradeSL", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, intent_kind="modify"),
    CommandSpec("MoveTradeTP", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, intent_kind="modify"),
    CommandSpec("PartialClose", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, risk_reducing=True, intent_kind="close"),
    CommandSpec("ReduceTradeRisk", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, risk_reducing=True, intent_kind="modify"),
    CommandSpec("SetAutoManagement", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE,
                category="trade", broker_dispatched=True, intent_kind="modify"),
    # Deployment manifest lifecycle
    CommandSpec("CloneManifest", RISK_OPERATIONAL, SURFACE_FIXTURE, category="system"),
    CommandSpec("ExportManifest", RISK_OPERATIONAL, SURFACE_FIXTURE, category="system"),
    CommandSpec("RestoreManifest", RISK_OPERATIONAL, SURFACE_FIXTURE, category="system"),
    CommandSpec("RedeployManifest", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE, category="system"),
    # Safety
    CommandSpec("GlobalKill", RISK_EMERGENCY, SURFACE_FIXTURE,
                category="risk", aliases=frozenset({"kill_switch"})),
    CommandSpec("PausePair", RISK_EXECUTION_AFFECTING, SURFACE_FIXTURE, category="risk"),
]

# LIVE-2: the execution surface — exactly ONE executable broker operation.
# Submit-able ONLY through the dedicated execution route; the fixture control
# plane (`/api/commands/{name}` = fixture_command_names()) rejects it as unknown,
# so no legacy surface can reach the live write.
_EXECUTION: list[CommandSpec] = [
    CommandSpec("SubmitMarketOrder", RISK_EXECUTION_AFFECTING, SURFACE_EXECUTION,
                category="order", broker_dispatched=True,
                broker_capability="supportsMarketExecution", intent_kind="submit"),
    # LIVE-3: the three manual-management operations. Modify is NOT risk_reducing
    # (it requires clean reconciliation and a proven non-risk-increasing change);
    # cancel and close are risk-reducing and are the ONLY commands available in
    # the halted mode (documented emergency semantics — all other gates apply).
    CommandSpec("ModifyPositionProtection", RISK_EXECUTION_AFFECTING, SURFACE_EXECUTION,
                category="trade", broker_dispatched=True,
                broker_capability="supportsModify", intent_kind="modify"),
    CommandSpec("CancelPendingOrder", RISK_EXECUTION_AFFECTING, SURFACE_EXECUTION,
                category="order", broker_dispatched=True,
                broker_capability="supportsCancelOrder", intent_kind="cancel",
                risk_reducing=True, halted_available=True),
    CommandSpec("ClosePosition", RISK_EXECUTION_AFFECTING, SURFACE_EXECUTION,
                category="trade", broker_dispatched=True,
                broker_capability="supportsClosePosition", intent_kind="close",
                risk_reducing=True, halted_available=True),
]

_ALL_SPECS: tuple[CommandSpec, ...] = tuple(_OPERATOR + _ABSTRACT + _FIXTURE + _EXECUTION)


def _build_index() -> tuple[dict, dict]:
    """Build the canonical index and the alias→canonical index, failing loudly on any
    duplicate. Called once at import; the result is the single source of truth."""
    by_canonical: dict[str, CommandSpec] = {}
    resolve: dict[str, str] = {}
    for spec in _ALL_SPECS:
        if spec.canonical in by_canonical:
            raise ValueError(f"duplicate canonical command name: {spec.canonical!r}")
        if not spec.risk_class:
            raise ValueError(f"command {spec.canonical!r} has no risk class")
        if spec.surface == SURFACE_FIXTURE and not spec.category:
            raise ValueError(f"fixture command {spec.canonical!r} has no audit category")
        by_canonical[spec.canonical] = spec
    # A canonical name resolves to itself; then aliases, checked for collisions.
    for name in by_canonical:
        resolve[name] = name
    for spec in _ALL_SPECS:
        for alias in spec.aliases:
            if alias in resolve:
                raise ValueError(
                    f"alias {alias!r} of {spec.canonical!r} collides with an existing "
                    f"command or alias ({resolve[alias]!r})")
            resolve[alias] = spec.canonical
    return by_canonical, resolve


REGISTRY, _RESOLVE = _build_index()


# ── public lookups (the only supported access) ────────────────────────────────

def resolve(name) -> str | None:
    """Canonical name for a command name or alias; None if unknown."""
    if not isinstance(name, str):
        return None
    return _RESOLVE.get(name)


def spec_of(name) -> CommandSpec | None:
    canonical = resolve(name)
    return REGISTRY.get(canonical) if canonical else None


def is_known(name) -> bool:
    return resolve(name) is not None


def risk_class_of(name) -> str | None:
    spec = spec_of(name)
    return spec.risk_class if spec else None


def category_of(name) -> str | None:
    spec = spec_of(name)
    return spec.category if spec else None


def requires_capability(name) -> str | None:
    spec = spec_of(name)
    return spec.broker_capability if spec else None


def is_broker_dispatched(name) -> bool:
    spec = spec_of(name)
    return bool(spec and spec.broker_dispatched)


def is_risk_reducing(name) -> bool:
    """ARCH-2: does this command reduce risk (close/cancel/de-risk)? Risk-reducing
    commands stay available under unresolved critical reconciliation so an
    operator can always de-risk; unknown commands answer False (fail closed)."""
    spec = spec_of(name)
    return bool(spec and spec.risk_reducing)


def is_halted_available(name) -> bool:
    """LIVE-3: may this command run in the HALTED execution mode? Only the two
    explicitly-flagged emergency de-risking operations answer True; unknown
    commands answer False (fail closed)."""
    spec = spec_of(name)
    return bool(spec and spec.halted_available)


def intent_kind_of(name) -> str | None:
    """ARCH-2: the order-lifecycle intent kind of a broker-dispatched command."""
    spec = spec_of(name)
    return spec.intent_kind if spec else None


def _names_for_surface(surface: str) -> frozenset:
    return frozenset(s.canonical for s in _ALL_SPECS if s.surface == surface)


def fixture_command_names() -> frozenset:
    """The PascalCase control-plane vocabulary (was `server.KNOWN_COMMANDS`)."""
    return _names_for_surface(SURFACE_FIXTURE)


def operator_command_names() -> frozenset:
    """The read-only operator vocabulary (mirrors `command_channel.ALLOWED_TYPES`)."""
    return _names_for_surface(SURFACE_OPERATOR)


def broker_dispatched_names() -> frozenset:
    """Commands whose effect routes through the Broker (was `broker.BROKER_COMMANDS`)."""
    return frozenset(s.canonical for s in _ALL_SPECS if s.broker_dispatched)


def execution_command_names() -> frozenset:
    """LIVE-2: the dedicated execution-surface vocabulary — exactly one command."""
    return _names_for_surface(SURFACE_EXECUTION)


def fixture_categories() -> dict:
    """Per-command audit category for the fixture surface (was `_COMMAND_CATEGORY`)."""
    return {s.canonical: s.category for s in _ALL_SPECS if s.surface == SURFACE_FIXTURE}
