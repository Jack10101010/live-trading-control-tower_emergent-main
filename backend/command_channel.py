"""UI-15 — the operator command-channel CONTRACT. Backend-only, sends nothing.

This module defines the immutable shape of a future operator command and the
lifecycle it moves through. It does NOT send, dispatch, execute, persist or
transport anything. The only implementation shipped here is `NullCommandChannel`:
disabled, in-memory, and inert — it records envelopes for query/audit and moves
them through lifecycle states, but never uses the transport, opens a socket,
touches node or execution state, or writes to disk.

WHY A CONTRACT WITH NO ACTION
    A command channel is the highest-risk future capability (it is the only path
    that could ever change node state), so its shape, validation and audit model
    are pinned FIRST, in isolation, before any dispatch code exists. Everything a
    real channel must enforce — bounded JSON-safe payloads, required expiry,
    required idempotency, unknown-type rejection, no secrets, deterministic
    de-duplication, acknowledgement-distinct-from-completion, an immutable audit
    record — is defined and tested here with nothing on the wire.

DELIBERATELY NON-MUTATING VOCABULARY
    The allowed command types are read-only requests only (`noop`,
    `request_health`, `request_telemetry`). There is NO pause, resume, arm, close,
    order or any execution control in the vocabulary, and this contract's allowlist
    is what would reject one. State-changing commands are out of scope and must be
    a separate, explicitly-audited slice.

REDACTION
    Payloads are validated to contain no secret-bearing keys and are only ever
    surfaced through `safe_view`, which masks values via the UI-9 redaction
    helpers. No credential can enter a command by contract.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import security_config

# ── contract identity ─────────────────────────────────────────────────────────
SCHEMA_VERSION = "ct.command.v1"

# Read-only request vocabulary ONLY. No execution / pause / resume / order control.
CMD_NOOP = "noop"
CMD_REQUEST_HEALTH = "request_health"
CMD_REQUEST_TELEMETRY = "request_telemetry"
ALLOWED_TYPES = frozenset({CMD_NOOP, CMD_REQUEST_HEALTH, CMD_REQUEST_TELEMETRY})

# ── lifecycle states ──────────────────────────────────────────────────────────
STATE_PENDING = "pending"        # accepted into the channel, not yet acknowledged
STATE_ACCEPTED = "accepted"      # acknowledged and admitted (ack, not completion)
STATE_REJECTED = "rejected"      # acknowledged and refused (terminal)
STATE_EXPIRED = "expired"        # expiry passed before completion (terminal)
STATE_COMPLETED = "completed"    # final outcome: succeeded (terminal)
STATE_FAILED = "failed"          # final outcome: failed (terminal)
TERMINAL_STATES = frozenset({STATE_REJECTED, STATE_EXPIRED, STATE_COMPLETED, STATE_FAILED})

# ── stable reason codes (safe to display; never a value) ──────────────────────
REASON_NOT_OBJECT = "envelope_not_object"
REASON_SCHEMA = "unsupported_schema"
REASON_UNKNOWN_TYPE = "unknown_command_type"
REASON_IDEMPOTENCY_REQUIRED = "idempotency_key_required"
REASON_EXPIRY_REQUIRED = "expiry_required"
REASON_EXPIRY_INVALID = "expiry_invalid"
REASON_ALREADY_EXPIRED = "already_expired"
REASON_TIMESTAMP_INVALID = "timestamp_invalid"
REASON_PAYLOAD_NOT_OBJECT = "payload_not_object"
REASON_PAYLOAD_TOO_LARGE = "payload_too_large"
REASON_PAYLOAD_NOT_JSON = "payload_not_json_safe"
REASON_PAYLOAD_SECRET = "payload_contains_secret"
REASON_FIELD_TOO_LONG = "field_too_long"
REASON_DUPLICATE_COMMAND_ID = "duplicate_command_id"
REASON_CHANNEL_DISABLED = "command_channel_disabled"
REASON_INVALID_TRANSITION = "invalid_state_transition"
REASON_NOT_FOUND = "command_not_found"
REASON_OK = "ok"

# ── bounds ────────────────────────────────────────────────────────────────────
MAX_STR = 200
MAX_PAYLOAD_BYTES = 8 * 1024          # a command is an instruction, never a log
MAX_TTL_S = 3600.0                    # an operator command may not live > 1 hour
CLOCK_SKEW_S = 120.0                  # tolerated future skew for requested_at
MAX_RECENT = 100                      # bounded in-memory audit ring


def new_command_id() -> str:
    """Collision-resistant id. uuid4 hex behind a stable prefix."""
    return f"cmd_{uuid.uuid4().hex}"


def _parse_iso(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _bounded_str(value: Any, limit: int = MAX_STR) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= limit)


def _contains_secret(node: Any, depth: int = 0) -> bool:
    """True if any key at any depth looks secret-bearing (UI-9 hints)."""
    if depth > 6:
        return True                    # pathological nesting: refuse rather than scan forever
    if isinstance(node, dict):
        for key, value in node.items():
            if security_config.is_secret_key(key) or _contains_secret(value, depth + 1):
                return True
    elif isinstance(node, (list, tuple)):
        return any(_contains_secret(v, depth + 1) for v in node)
    return False


# ── immutable envelope + lifecycle records ────────────────────────────────────

@dataclass(frozen=True)
class CommandEnvelope:
    """An operator command as submitted. Immutable once built."""
    schema_version: str
    command_id: str
    command_type: str
    idempotency_key: str
    requested_at: str
    expires_at: str
    target: str | None = None
    operator_ref: str | None = None
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class Acknowledgement:
    """Admission decision. Distinct from completion: acknowledging a command says
    it was received and (not) admitted, not that it did anything."""
    command_id: str
    accepted: bool
    reason: str
    acknowledged_at: str


@dataclass(frozen=True)
class Outcome:
    """Final result. Only reached after acceptance; never conflated with ack."""
    command_id: str
    state: str                         # STATE_COMPLETED | STATE_FAILED
    reason: str
    detail: str | None
    completed_at: str


@dataclass(frozen=True)
class CommandRecord:
    """The immutable audit representation of a command's whole life."""
    envelope: CommandEnvelope
    state: str
    created_at: str
    updated_at: str
    acknowledgement: Acknowledgement | None = None
    outcome: Outcome | None = None


class CommandError(ValueError):
    """A submission was rejected. `reason` is a stable code; `detail` is safe."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def validate_envelope(data: Any, *, now: datetime) -> CommandEnvelope:
    """Strictly validate a submission. Raises `CommandError`; never partially
    accepts. Generates a `command_id` when one is not supplied."""
    if not isinstance(data, dict):
        raise CommandError(REASON_NOT_OBJECT)

    if data.get("schema_version") != SCHEMA_VERSION:
        raise CommandError(REASON_SCHEMA, str(data.get("schema_version"))[:64])

    command_type = data.get("command_type")
    if command_type not in ALLOWED_TYPES:
        raise CommandError(REASON_UNKNOWN_TYPE, str(command_type)[:64])

    idempotency_key = data.get("idempotency_key")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise CommandError(REASON_IDEMPOTENCY_REQUIRED)
    if len(idempotency_key) > MAX_STR:
        raise CommandError(REASON_FIELD_TOO_LONG, "idempotency_key")

    for field_name in ("target", "operator_ref", "command_id"):
        if not _bounded_str(data.get(field_name)):
            raise CommandError(REASON_FIELD_TOO_LONG, field_name)

    requested = _parse_iso(data.get("requested_at"))
    if requested is None:
        raise CommandError(REASON_TIMESTAMP_INVALID, "requested_at")
    # A requested_at implausibly in the future is not a valid submission (clock
    # skew is tolerated up to a bound, mirroring the UI-2/UI-14 freshness rule).
    if (requested - now).total_seconds() > CLOCK_SKEW_S:
        raise CommandError(REASON_TIMESTAMP_INVALID, "requested_at_future")

    # Expiry is REQUIRED and must be a real, future, bounded window.
    if not data.get("expires_at"):
        raise CommandError(REASON_EXPIRY_REQUIRED)
    expires = _parse_iso(data.get("expires_at"))
    if expires is None:
        raise CommandError(REASON_EXPIRY_INVALID, "unparseable")
    if expires <= requested:
        raise CommandError(REASON_EXPIRY_INVALID, "not_after_requested")
    if (expires - requested).total_seconds() > MAX_TTL_S:
        raise CommandError(REASON_EXPIRY_INVALID, "ttl_too_long")
    if expires <= now:
        raise CommandError(REASON_ALREADY_EXPIRED)

    payload = data.get("payload", {})
    if payload is None:
        payload = {}
    if not isinstance(payload, dict):
        raise CommandError(REASON_PAYLOAD_NOT_OBJECT)
    try:
        encoded = json.dumps(payload)
    except (TypeError, ValueError):
        raise CommandError(REASON_PAYLOAD_NOT_JSON)
    if len(encoded.encode("utf-8")) > MAX_PAYLOAD_BYTES:
        raise CommandError(REASON_PAYLOAD_TOO_LARGE)
    if _contains_secret(payload):
        raise CommandError(REASON_PAYLOAD_SECRET)

    command_id = data.get("command_id") or new_command_id()
    return CommandEnvelope(
        schema_version=SCHEMA_VERSION,
        command_id=command_id,
        command_type=command_type,
        idempotency_key=idempotency_key,
        requested_at=data["requested_at"],
        expires_at=data["expires_at"],
        target=data.get("target"),
        operator_ref=data.get("operator_ref"),
        payload=dict(payload),
    )


# ── the channel interface ─────────────────────────────────────────────────────

class CommandChannel:
    """Interface for submit / acknowledge / query / list. Sends nothing itself."""

    enabled: bool = False

    def submit(self, data: Any, *, now: datetime | None = None) -> CommandRecord:
        raise NotImplementedError

    def acknowledge(self, command_id: str, *, accepted: bool, reason: str = "",
                    now: datetime | None = None) -> CommandRecord:
        raise NotImplementedError

    def status(self, command_id: str) -> CommandRecord | None:
        raise NotImplementedError

    def recent(self, limit: int = 20) -> list[CommandRecord]:
        raise NotImplementedError

    def describe(self) -> str:
        return "command channel"


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


class NullCommandChannel(CommandChannel):
    """The default: DISABLED and in-memory-null.

    It validates and records envelopes so the contract, de-duplication and audit
    representation can be exercised, but it NEVER dispatches, transports, executes,
    or persists. Records live only in a bounded in-process ring and vanish on
    restart. A submitted command never leaves `pending` on its own — there is no
    dispatcher — and acknowledgement/outcome transitions only happen if a caller
    (or a test) drives them explicitly.
    """

    enabled = False

    def __init__(self, max_records: int = MAX_RECENT):
        self._records: dict[str, CommandRecord] = {}
        self._by_idempotency: dict[str, str] = {}
        self._order: list[str] = []
        self._max = max_records

    # -- submit -------------------------------------------------------------
    def submit(self, data: Any, *, now: datetime | None = None) -> CommandRecord:
        clock = _now(now)
        envelope = validate_envelope(data, now=clock)   # raises CommandError on any problem

        # Deterministic de-duplication: an identical idempotency key returns the
        # SAME record. A different command reusing a live key, or a colliding
        # command_id, is a deterministic rejection — never a silent overwrite.
        existing_id = self._by_idempotency.get(envelope.idempotency_key)
        if existing_id is not None:
            return self._records[existing_id]           # idempotent replay
        if envelope.command_id in self._records:
            raise CommandError(REASON_DUPLICATE_COMMAND_ID, envelope.command_id)

        record = CommandRecord(envelope=envelope, state=STATE_PENDING,
                               created_at=_iso(clock), updated_at=_iso(clock))
        self._store(record)
        return record

    # -- acknowledge (admission; distinct from completion) ------------------
    def acknowledge(self, command_id: str, *, accepted: bool, reason: str = "",
                    now: datetime | None = None) -> CommandRecord:
        clock = _now(now)
        record = self._records.get(command_id)
        if record is None:
            raise CommandError(REASON_NOT_FOUND, command_id)
        current = self._effective_state(record, clock)
        if current != STATE_PENDING:
            # Only a pending command may be acknowledged. Everything else (already
            # acknowledged, expired, terminal) is an invalid transition.
            raise CommandError(REASON_INVALID_TRANSITION, current)
        ack = Acknowledgement(command_id=command_id, accepted=accepted,
                              reason=reason or (REASON_OK if accepted else "rejected"),
                              acknowledged_at=_iso(clock))
        new_state = STATE_ACCEPTED if accepted else STATE_REJECTED
        updated = replace(record, state=new_state, acknowledgement=ack,
                          updated_at=_iso(clock))
        self._records[command_id] = updated
        return updated

    def record_outcome(self, command_id: str, *, succeeded: bool, reason: str = "",
                       detail: str | None = None, now: datetime | None = None) -> CommandRecord:
        """Completion is DISTINCT from acknowledgement: only an ACCEPTED command may
        complete. NullCommandChannel never calls this itself (it dispatches
        nothing); it exists so the contract's completion transition is testable."""
        clock = _now(now)
        record = self._records.get(command_id)
        if record is None:
            raise CommandError(REASON_NOT_FOUND, command_id)
        current = self._effective_state(record, clock)
        if current != STATE_ACCEPTED:
            raise CommandError(REASON_INVALID_TRANSITION, current)
        state = STATE_COMPLETED if succeeded else STATE_FAILED
        outcome = Outcome(command_id=command_id, state=state,
                          reason=reason or (REASON_OK if succeeded else "failed"),
                          detail=security_config.redact_text(detail) if detail else None,
                          completed_at=_iso(clock))
        updated = replace(record, state=state, outcome=outcome, updated_at=_iso(clock))
        self._records[command_id] = updated
        return updated

    # -- query --------------------------------------------------------------
    def status(self, command_id: str) -> CommandRecord | None:
        record = self._records.get(command_id)
        if record is None:
            return None
        # Reflect expiry lazily at read time without mutating stored history until
        # a transition is actually recorded.
        effective = self._effective_state(record, _now(None))
        return record if effective == record.state else replace(record, state=effective)

    def recent(self, limit: int = 20) -> list[CommandRecord]:
        limit = max(0, min(int(limit), self._max))
        ids = self._order[-limit:] if limit else []
        return [self._records[i] for i in reversed(ids)]

    def describe(self) -> str:
        return "NullCommandChannel — disabled, in-memory, dispatches nothing"

    # -- internals ----------------------------------------------------------
    def _effective_state(self, record: CommandRecord, now: datetime) -> str:
        if record.state in TERMINAL_STATES or record.state == STATE_ACCEPTED:
            return record.state
        expires = _parse_iso(record.envelope.expires_at)
        if record.state == STATE_PENDING and expires is not None and now >= expires:
            return STATE_EXPIRED
        return record.state

    def _store(self, record: CommandRecord) -> None:
        cid = record.envelope.command_id
        self._records[cid] = record
        self._by_idempotency[record.envelope.idempotency_key] = cid
        self._order.append(cid)
        while len(self._order) > self._max:
            evicted = self._order.pop(0)
            old = self._records.pop(evicted, None)
            if old is not None:
                self._by_idempotency.pop(old.envelope.idempotency_key, None)


def default_command_channel() -> CommandChannel:
    """The single wiring point. Returns `NullCommandChannel` unconditionally in
    UI-15: there is no real channel, no dispatch, and no transport. A future,
    separately-audited slice would select a concrete channel here."""
    return NullCommandChannel()


# ── redaction-safe diagnostics ────────────────────────────────────────────────

def safe_view(record: CommandRecord) -> dict:
    """Value-free audit view. The payload is masked with the UI-9 mapping redactor
    (defence-in-depth; secrets are already rejected at submit), and no field can
    carry a credential."""
    env = record.envelope
    return {
        "schemaVersion": env.schema_version,
        "commandId": env.command_id,
        "commandType": env.command_type,
        "idempotencyKey": env.idempotency_key,
        "target": env.target,
        "operatorRef": env.operator_ref,
        "requestedAt": env.requested_at,
        "expiresAt": env.expires_at,
        "state": record.state,
        "createdAt": record.created_at,
        "updatedAt": record.updated_at,
        "acknowledged": record.acknowledgement is not None,
        "accepted": record.acknowledgement.accepted if record.acknowledgement else None,
        "completed": record.outcome is not None,
        "outcomeState": record.outcome.state if record.outcome else None,
        "payload": security_config.redact_mapping(env.payload),
    }
