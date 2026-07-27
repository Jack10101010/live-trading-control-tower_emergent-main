"""LIVE-4B — the canonical SCENARIO domain.

A Scenario is the parent object of the trading lineage. Every future
recommendation, intent, order, position, deal and ledger entry must be
traceable back to exactly ONE Scenario:

    Scenario -> Recommendation -> Intent -> Order -> Position -> Deals -> Ledger

WHAT THIS MODULE IS
  * the immutable Scenario model, its identity, its explicit lifecycle, and the
    append-only event vocabulary that records lifecycle movement.

WHAT THIS MODULE IS NOT
  * a strategy engine. It generates NO signals, qualifies NOTHING automatically,
    and creates NO scenarios on its own. Every scenario and every transition is
    an explicit, caller-supplied fact.
  * an execution owner. It holds no adapter, submits nothing, and never touches
    the execution store.

RELATIONSHIPS ARE EXPLICIT
  Links are stored as identifier tuples on the Scenario itself
  (`linked_intent_ids`, `linked_order_ids`, `linked_position_ids`). Nothing is
  inferred and nothing is reconstructed in reverse: if a link was not recorded,
  it does not exist.

DETERMINISM
  `ScenarioId.derive()` hashes the natural key, so the same trading setup always
  yields the same identity. Every model serializes with sorted keys and is
  JSON-safe, so a projection over scenarios is byte-stable.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

SCHEMA_VERSION = "ct.scenario.v1"

SCENARIO_PREFIX = "scn_"

#: The fixture world's scenario key convention, confirmed across every entry:
#:     instrument:session:structure:direction:entryModel
#: e.g. "EURUSD:london:BOS:long:BullExpand"
SCENARIO_KEY_PARTS = ("instrument", "session", "structure", "direction", "entry_model")

DIRECTIONS = frozenset({"long", "short"})

MAX_METADATA_BYTES = 4 * 1024
MAX_TAGS = 16


class ScenarioError(ValueError):
    """An invalid scenario operation. `reason` is a stable machine code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _sorted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sorted(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_sorted(v) for v in obj]
    return obj


# ── identity ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScenarioId:
    """A deterministic, immutable scenario identity.

    `derive()` hashes the natural key (instrument/session/structure/direction/
    entry model + an explicit discriminator), so the same setup on the same
    trading day always produces the same id — replay and rebuild are stable."""
    value: str

    def __post_init__(self):
        if not (isinstance(self.value, str) and self.value.startswith(SCENARIO_PREFIX)):
            raise ScenarioError("invalid_scenario_id",
                                f"scenario id must start with {SCENARIO_PREFIX!r}")
        if not re.fullmatch(rf"{SCENARIO_PREFIX}[0-9a-f]{{16}}", self.value):
            raise ScenarioError("invalid_scenario_id",
                                "scenario id must be scn_ + 16 lowercase hex chars")

    def __str__(self) -> str:
        return self.value

    @staticmethod
    def derive(*, instrument: str, session: str, structure: str, direction: str,
               entry_model: str, discriminator: str = "") -> "ScenarioId":
        """Deterministic identity from the natural key. `discriminator` separates
        two otherwise-identical setups (e.g. a trading date or sequence)."""
        for name, v in (("instrument", instrument), ("session", session),
                        ("structure", structure), ("direction", direction),
                        ("entry_model", entry_model)):
            if not (isinstance(v, str) and v.strip()):
                raise ScenarioError("invalid_natural_key", f"{name} is required")
        if direction not in DIRECTIONS:
            raise ScenarioError("invalid_direction", str(direction))
        natural = "|".join((instrument.strip(), session.strip(), structure.strip(),
                            direction.strip(), entry_model.strip(),
                            str(discriminator).strip()))
        digest = hashlib.sha256(natural.encode("utf-8")).hexdigest()[:16]
        return ScenarioId(f"{SCENARIO_PREFIX}{digest}")

    @staticmethod
    def from_key(scenario_key: str, *, discriminator: str = "") -> "ScenarioId":
        """Derive identity from the fixture world's `scenarioKey` convention."""
        parts = parse_scenario_key(scenario_key)
        return ScenarioId.derive(discriminator=discriminator, **parts)


def parse_scenario_key(scenario_key: Any) -> dict:
    """Parse `instrument:session:structure:direction:entryModel`. Anything that
    does not match the convention raises — no partial guessing."""
    if not isinstance(scenario_key, str):
        raise ScenarioError("invalid_scenario_key", "scenario key must be a string")
    parts = scenario_key.split(":")
    if len(parts) != len(SCENARIO_KEY_PARTS) or not all(p.strip() for p in parts):
        raise ScenarioError("invalid_scenario_key",
                            f"expected {':'.join(SCENARIO_KEY_PARTS)}")
    parsed = dict(zip(SCENARIO_KEY_PARTS, (p.strip() for p in parts)))
    if parsed["direction"] not in DIRECTIONS:
        raise ScenarioError("invalid_direction", parsed["direction"])
    return parsed


# ── lifecycle ─────────────────────────────────────────────────────────────────

class ScenarioStatus:
    CREATED = "CREATED"
    WATCHING = "WATCHING"
    QUALIFIED = "QUALIFIED"
    RECOMMENDED = "RECOMMENDED"
    ACCEPTED = "ACCEPTED"
    INTENT_CREATED = "INTENT_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    POSITION_OPEN = "POSITION_OPEN"
    MANAGING = "MANAGING"
    POSITION_CLOSED = "POSITION_CLOSED"
    COMPLETED = "COMPLETED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


ALL_STATUSES = frozenset({
    ScenarioStatus.CREATED, ScenarioStatus.WATCHING, ScenarioStatus.QUALIFIED,
    ScenarioStatus.RECOMMENDED, ScenarioStatus.ACCEPTED, ScenarioStatus.INTENT_CREATED,
    ScenarioStatus.ORDER_SUBMITTED, ScenarioStatus.POSITION_OPEN, ScenarioStatus.MANAGING,
    ScenarioStatus.POSITION_CLOSED, ScenarioStatus.COMPLETED, ScenarioStatus.INVALIDATED,
    ScenarioStatus.EXPIRED, ScenarioStatus.CANCELLED, ScenarioStatus.REJECTED,
})

#: Terminal statuses — no transition may leave them.
TERMINAL_STATUSES = frozenset({
    ScenarioStatus.COMPLETED, ScenarioStatus.INVALIDATED, ScenarioStatus.EXPIRED,
    ScenarioStatus.CANCELLED, ScenarioStatus.REJECTED,
})

#: Statuses counted as ACTIVE (a live scenario the operator should see).
ACTIVE_STATUSES = frozenset(ALL_STATUSES - TERMINAL_STATUSES)

#: Abandonment is reachable from any non-terminal status — a setup can always be
#: invalidated, expire, or be cancelled by an operator.
_ABANDON = (ScenarioStatus.INVALIDATED, ScenarioStatus.EXPIRED, ScenarioStatus.CANCELLED)

#: The EXPLICIT forward-progress table. A pair absent here is illegal, full stop.
_FORWARD: dict[str, tuple] = {
    ScenarioStatus.CREATED: (ScenarioStatus.WATCHING, ScenarioStatus.QUALIFIED),
    ScenarioStatus.WATCHING: (ScenarioStatus.QUALIFIED,),
    ScenarioStatus.QUALIFIED: (ScenarioStatus.RECOMMENDED,),
    ScenarioStatus.RECOMMENDED: (ScenarioStatus.ACCEPTED, ScenarioStatus.REJECTED),
    ScenarioStatus.ACCEPTED: (ScenarioStatus.INTENT_CREATED,),
    ScenarioStatus.INTENT_CREATED: (ScenarioStatus.ORDER_SUBMITTED,),
    ScenarioStatus.ORDER_SUBMITTED: (ScenarioStatus.POSITION_OPEN,),
    ScenarioStatus.POSITION_OPEN: (ScenarioStatus.MANAGING, ScenarioStatus.POSITION_CLOSED),
    ScenarioStatus.MANAGING: (ScenarioStatus.MANAGING, ScenarioStatus.POSITION_CLOSED),
    ScenarioStatus.POSITION_CLOSED: (ScenarioStatus.COMPLETED,),
    ScenarioStatus.COMPLETED: (),
}


def allowed_transitions(status: str) -> frozenset:
    """Every legal next status from `status`. Terminal statuses allow none."""
    if status in TERMINAL_STATUSES:
        return frozenset()
    forward = _FORWARD.get(status, ())
    return frozenset(forward) | frozenset(_ABANDON)


ALLOWED_TRANSITIONS = frozenset(
    (src, dst) for src in ALL_STATUSES for dst in allowed_transitions(src))


# ── outcome ───────────────────────────────────────────────────────────────────

class ScenarioOutcome:
    """How a scenario ENDED. `PENDING` while it is still active."""
    PENDING = "PENDING"
    TRADED = "TRADED"              # reached COMPLETED through a real position
    ABANDONED = "ABANDONED"        # invalidated / expired / cancelled
    DECLINED = "DECLINED"          # explicitly rejected at recommendation

    @staticmethod
    def of(status: str) -> str:
        if status == ScenarioStatus.COMPLETED:
            return ScenarioOutcome.TRADED
        if status == ScenarioStatus.REJECTED:
            return ScenarioOutcome.DECLINED
        if status in (ScenarioStatus.INVALIDATED, ScenarioStatus.EXPIRED,
                      ScenarioStatus.CANCELLED):
            return ScenarioOutcome.ABANDONED
        return ScenarioOutcome.PENDING


# ── the canonical Scenario ────────────────────────────────────────────────────

@dataclass(frozen=True)
class Scenario:
    """The immutable parent object of one trading lineage."""
    scenario_id: str
    instrument: str
    session: str
    structure: str
    direction: str
    entry_model: str
    status: str = ScenarioStatus.CREATED
    node_id: str | None = None
    account_fingerprint: str | None = None
    timeframe: str | None = None
    created_at: str = ""
    updated_at: str = ""
    expires_at: str | None = None
    linked_recommendation_id: str | None = None
    linked_intent_ids: tuple = field(default_factory=tuple)
    linked_order_ids: tuple = field(default_factory=tuple)
    linked_position_ids: tuple = field(default_factory=tuple)
    tags: tuple = field(default_factory=tuple)
    metadata: dict = field(default_factory=dict)
    provenance: str = "operator"

    def __post_init__(self):
        if not (isinstance(self.scenario_id, str)
                and re.fullmatch(rf"{SCENARIO_PREFIX}[0-9a-f]{{16}}", self.scenario_id)):
            raise ScenarioError("invalid_scenario_id", str(self.scenario_id))
        for name in ("instrument", "session", "structure", "entry_model"):
            v = getattr(self, name)
            if not (isinstance(v, str) and v.strip()):
                raise ScenarioError("invalid_natural_key", f"{name} is required")
        if self.direction not in DIRECTIONS:
            raise ScenarioError("invalid_direction", str(self.direction))
        if self.status not in ALL_STATUSES:
            raise ScenarioError("unknown_status", str(self.status))
        if len(self.tags) > MAX_TAGS:
            raise ScenarioError("too_many_tags", str(len(self.tags)))
        try:
            encoded = json.dumps(self.metadata)
        except (TypeError, ValueError) as exc:
            raise ScenarioError("metadata_not_json_safe") from exc
        if len(encoded.encode("utf-8")) > MAX_METADATA_BYTES:
            raise ScenarioError("metadata_too_large")

    # -- derived -------------------------------------------------------------
    @property
    def active(self) -> bool:
        return self.status in ACTIVE_STATUSES

    @property
    def outcome(self) -> str:
        return ScenarioOutcome.of(self.status)

    def age_seconds(self, now: str) -> float | None:
        start, ref = _parse(self.created_at), _parse(now)
        if start is None or ref is None:
            return None
        return round((ref - start).total_seconds(), 3)

    def is_expired(self, now: str) -> bool:
        expires, ref = _parse(self.expires_at), _parse(now)
        return expires is not None and ref is not None and ref >= expires

    def scenario_key(self) -> str:
        return ":".join((self.instrument, self.session, self.structure,
                         self.direction, self.entry_model))

    def as_dict(self) -> dict:
        return _sorted({
            "scenarioId": self.scenario_id,
            "nodeId": self.node_id,
            "accountFingerprint": self.account_fingerprint,
            "instrument": self.instrument,
            "session": self.session,
            "timeframe": self.timeframe,
            "structure": self.structure,
            "entryModel": self.entry_model,
            "direction": self.direction,
            "status": self.status,
            "outcome": self.outcome,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "expiresAt": self.expires_at,
            "linkedRecommendationId": self.linked_recommendation_id,
            "linkedIntentIds": list(self.linked_intent_ids),
            "linkedOrderIds": list(self.linked_order_ids),
            "linkedPositionIds": list(self.linked_position_ids),
            "scenarioKey": self.scenario_key(),
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
            "provenance": self.provenance,
            "schemaVersion": SCHEMA_VERSION,
        })


def new_scenario(*, instrument: str, session: str, structure: str, direction: str,
                 entry_model: str, created_at: str, discriminator: str = "",
                 **rest) -> Scenario:
    """Construct a scenario with a DERIVED deterministic identity. Explicit
    only — nothing in this repository calls it automatically."""
    sid = ScenarioId.derive(instrument=instrument, session=session,
                            structure=structure, direction=direction,
                            entry_model=entry_model, discriminator=discriminator)
    rest.setdefault("updated_at", created_at)
    return Scenario(scenario_id=sid.value, instrument=instrument, session=session,
                    structure=structure, direction=direction, entry_model=entry_model,
                    created_at=created_at, **rest)


# ── transitions ───────────────────────────────────────────────────────────────

def transition(scenario: Scenario, to_status: str, *, at: str,
               reason: str) -> Scenario:
    """Return a NEW scenario in `to_status`. Pure: the input is untouched.

    Raises `ScenarioError` when the target is unknown, the source is terminal,
    the pair is not in the explicit table, the reason is missing, or the
    timestamp would move backwards."""
    if to_status not in ALL_STATUSES:
        raise ScenarioError("unknown_status", str(to_status))
    if scenario.status in TERMINAL_STATUSES:
        raise ScenarioError("terminal_status_immutable",
                            f"{scenario.status} is terminal")
    if (scenario.status, to_status) not in ALLOWED_TRANSITIONS:
        raise ScenarioError("invalid_transition",
                            f"{scenario.status} -> {to_status}")
    if not (isinstance(reason, str) and reason.strip()):
        raise ScenarioError("reason_required")
    new_at, prev = _parse(at), _parse(scenario.updated_at)
    if new_at is None:
        raise ScenarioError("invalid_timestamp", str(at))
    if prev is not None and new_at < prev:
        raise ScenarioError("non_monotonic_timestamp",
                            f"{at} precedes {scenario.updated_at}")
    return replace(scenario, status=to_status, updated_at=at)


def link(scenario: Scenario, *, at: str, recommendation_id: str | None = None,
         intent_id: str | None = None, order_id: str | None = None,
         position_id: str | None = None) -> Scenario:
    """Record an EXPLICIT relationship. Idempotent: linking the same identifier
    twice leaves the scenario unchanged (no duplicate ids). Nothing is inferred."""
    changes: dict = {}
    if recommendation_id:
        if scenario.linked_recommendation_id and \
                scenario.linked_recommendation_id != recommendation_id:
            raise ScenarioError("recommendation_already_linked",
                                scenario.linked_recommendation_id)
        changes["linked_recommendation_id"] = recommendation_id
    for value, attr in ((intent_id, "linked_intent_ids"),
                        (order_id, "linked_order_ids"),
                        (position_id, "linked_position_ids")):
        if not value:
            continue
        current = getattr(scenario, attr)
        if value not in current:
            changes[attr] = tuple(sorted(current + (value,)))
    if not changes:
        return scenario
    return replace(scenario, updated_at=at, **changes)


# ── events (append-only vocabulary) ───────────────────────────────────────────

class ScenarioEventType:
    CREATED = "ScenarioCreated"
    WATCHING = "ScenarioWatching"
    QUALIFIED = "ScenarioQualified"
    RECOMMENDED = "ScenarioRecommended"
    ACCEPTED = "ScenarioAccepted"
    REJECTED = "ScenarioRejected"
    INTENT_LINKED = "ScenarioIntentLinked"
    ORDER_LINKED = "ScenarioOrderLinked"
    POSITION_LINKED = "ScenarioPositionLinked"
    RECOMMENDATION_LINKED = "ScenarioRecommendationLinked"
    ORDER_SUBMITTED = "ScenarioOrderSubmitted"
    POSITION_OPEN = "ScenarioPositionOpen"
    MANAGING = "ScenarioManaging"
    POSITION_CLOSED = "ScenarioPositionClosed"
    INVALIDATED = "ScenarioInvalidated"
    EXPIRED = "ScenarioExpired"
    COMPLETED = "ScenarioCompleted"
    CANCELLED = "ScenarioCancelled"


#: Which event records arrival at which status (used to rebuild from history).
STATUS_EVENT = {
    ScenarioStatus.CREATED: ScenarioEventType.CREATED,
    ScenarioStatus.WATCHING: ScenarioEventType.WATCHING,
    ScenarioStatus.QUALIFIED: ScenarioEventType.QUALIFIED,
    ScenarioStatus.RECOMMENDED: ScenarioEventType.RECOMMENDED,
    ScenarioStatus.ACCEPTED: ScenarioEventType.ACCEPTED,
    ScenarioStatus.REJECTED: ScenarioEventType.REJECTED,
    ScenarioStatus.INTENT_CREATED: ScenarioEventType.INTENT_LINKED,
    ScenarioStatus.ORDER_SUBMITTED: ScenarioEventType.ORDER_SUBMITTED,
    ScenarioStatus.POSITION_OPEN: ScenarioEventType.POSITION_OPEN,
    ScenarioStatus.MANAGING: ScenarioEventType.MANAGING,
    ScenarioStatus.POSITION_CLOSED: ScenarioEventType.POSITION_CLOSED,
    ScenarioStatus.COMPLETED: ScenarioEventType.COMPLETED,
    ScenarioStatus.INVALIDATED: ScenarioEventType.INVALIDATED,
    ScenarioStatus.EXPIRED: ScenarioEventType.EXPIRED,
    ScenarioStatus.CANCELLED: ScenarioEventType.CANCELLED,
}

ALL_EVENT_TYPES = frozenset(
    v for k, v in vars(ScenarioEventType).items() if not k.startswith("_")
    and isinstance(v, str))


@dataclass(frozen=True)
class ScenarioEvent:
    """One immutable, timestamped, serializable scenario fact."""
    scenario_id: str
    event_type: str
    at: str
    reason: str = ""
    data: dict = field(default_factory=dict)
    sequence: int | None = None

    def __post_init__(self):
        if self.event_type not in ALL_EVENT_TYPES:
            raise ScenarioError("unknown_event_type", str(self.event_type))
        if _parse(self.at) is None:
            raise ScenarioError("invalid_timestamp", str(self.at))
        try:
            json.dumps(self.data)
        except (TypeError, ValueError) as exc:
            raise ScenarioError("event_data_not_json_safe") from exc

    def as_dict(self) -> dict:
        return _sorted({
            "scenarioId": self.scenario_id, "eventType": self.event_type,
            "at": self.at, "reason": self.reason, "data": dict(self.data),
            "sequence": self.sequence,
        })


# ── summary ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ScenarioSummary:
    """Deterministic aggregate over a set of scenarios."""
    total: int = 0
    active: int = 0
    by_status: dict = field(default_factory=dict)
    by_instrument: dict = field(default_factory=dict)
    by_outcome: dict = field(default_factory=dict)

    @staticmethod
    def of(scenarios) -> "ScenarioSummary":
        by_status: dict = {}
        by_instrument: dict = {}
        by_outcome: dict = {}
        active = 0
        items = list(scenarios)
        for s in items:
            by_status[s.status] = by_status.get(s.status, 0) + 1
            by_instrument[s.instrument] = by_instrument.get(s.instrument, 0) + 1
            by_outcome[s.outcome] = by_outcome.get(s.outcome, 0) + 1
            if s.active:
                active += 1
        return ScenarioSummary(total=len(items), active=active,
                               by_status=dict(sorted(by_status.items())),
                               by_instrument=dict(sorted(by_instrument.items())),
                               by_outcome=dict(sorted(by_outcome.items())))

    def as_dict(self) -> dict:
        return _sorted({
            "total": self.total, "active": self.active,
            "byStatus": dict(self.by_status),
            "byInstrument": dict(self.by_instrument),
            "byOutcome": dict(self.by_outcome),
        })


def rebuild(events) -> Scenario | None:
    """Deterministically rebuild a scenario from its append-only event history.

    The CREATED event carries the full construction payload; later events replay
    status transitions and links in sequence order. Rebuilding the same history
    always yields the same scenario — that is what makes the store restart-safe."""
    ordered = sorted(events, key=lambda e: (e.sequence if e.sequence is not None else 0))
    scenario: Scenario | None = None
    for event in ordered:
        if event.event_type == ScenarioEventType.CREATED:
            payload = dict(event.data)
            scenario = Scenario(
                scenario_id=event.scenario_id,
                instrument=payload["instrument"], session=payload["session"],
                structure=payload["structure"], direction=payload["direction"],
                entry_model=payload["entry_model"],
                status=ScenarioStatus.CREATED,
                node_id=payload.get("node_id"),
                account_fingerprint=payload.get("account_fingerprint"),
                timeframe=payload.get("timeframe"),
                created_at=event.at, updated_at=event.at,
                expires_at=payload.get("expires_at"),
                tags=tuple(payload.get("tags") or ()),
                metadata=dict(payload.get("metadata") or {}),
                provenance=payload.get("provenance", "operator"))
            continue
        if scenario is None:
            raise ScenarioError("history_without_creation", event.scenario_id)
        data = dict(event.data)
        if event.event_type in (ScenarioEventType.INTENT_LINKED,
                                ScenarioEventType.ORDER_LINKED,
                                ScenarioEventType.POSITION_LINKED,
                                ScenarioEventType.RECOMMENDATION_LINKED):
            scenario = link(scenario, at=event.at,
                            recommendation_id=data.get("recommendation_id"),
                            intent_id=data.get("intent_id"),
                            order_id=data.get("order_id"),
                            position_id=data.get("position_id"))
        status = data.get("status")
        if status and status != scenario.status:
            scenario = transition(scenario, status, at=event.at,
                                  reason=event.reason or "replay")
    return scenario
