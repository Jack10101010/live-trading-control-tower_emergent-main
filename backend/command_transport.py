"""UI-16 — a SAFE, READ-ONLY command transport.

This wires the UI-15 command CONTRACT (`command_channel`) onto the UI-13 authenticated
`Transport` (`transport`/`rest_transport`) so that the three read-only command types —
`noop`, `request_health`, `request_telemetry` — can be transmitted to the node and
their remote responses mapped back into the canonical UI-15 lifecycle. It adds NO new
command model (it reuses UI-15's envelope, acknowledgement, outcome and record), NO
execution authority, and NO operator control.

WHAT THIS IS ALLOWED TO DO
    * Transmit ONLY the read-only command vocabulary (`command_channel.ALLOWED_TYPES`).
      Every read-only type maps to a bounded GET against a node read endpoint
      (`/health`, `/telemetry`) — the transport itself is GET-only (UI-13), so a
      mutating request is not merely disallowed, it is unrepresentable here.
    * Map the single transport round-trip into the UI-15 lifecycle: an answered
      request is ACKNOWLEDGED (accepted) and then, distinctly, COMPLETED; a refusal
      is a rejection; an unusable answer is acknowledged-but-failed; an unreachable
      node leaves the command pending (never a fabricated success).

WHAT THIS DELIBERATELY DOES NOT DO
    * It is DISABLED BY DEFAULT: `default_command_transport()` uses
      `transport.default_transport()`, which is `NullTransport` unless an operator has
      explicitly enabled AND validly configured a real transport (UI-13). A disabled
      service transmits nothing.
    * NO retries (one attempt per submission — UI-13's single-attempt property).
    * NO mutation, NO pause/resume/arm/order/cancel/kill, NO execution path, NO VPS
      mutation. The allowlist is enforced twice (UI-15 `validate_envelope` and again
      here) and there is no code path that could construct a mutating request.
    * NO secret-bearing payloads (rejected by the UI-15 contract before send).
    * NO frontend surface. No API route is added by this slice; the service is a
      backend module, wired-ready but not exposed.

REDACTION
    Every failure detail that could reach a caller, log or audit view is passed
    through `security_config.redact_text`, and the bearer credential lives only inside
    the transport (UI-13), never here.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import command_channel as cc
import security_config
import transport as transport_mod

# ── the node's read-only endpoints (identical to UI-14 node_client) ───────────
# Kept as local constants so this module does not depend on the telemetry-polling
# module; a read-only command maps to exactly one of these bounded GET paths.
NODE_HEALTH_PATH = "/health"
NODE_TELEMETRY_PATH = "/telemetry"

# Which read-only endpoint each allowed command type dispatches to. A command type
# with no entry here cannot be transmitted — there is intentionally no mapping for
# any mutating verb, so one cannot be sent even if it somehow reached this layer.
_HEALTH_TYPES = frozenset({cc.CMD_NOOP, cc.CMD_REQUEST_HEALTH})
_TELEMETRY_TYPES = frozenset({cc.CMD_REQUEST_TELEMETRY})

# ── stable, display-safe reason codes for the transport step (never a value) ──
REASON_TRANSPORT_DISABLED = "command_transport_disabled"
REASON_DELIVERED = "delivered"              # the node received and answered the request
REASON_FULFILLED = "fulfilled"             # the answer was usable (completion)
REASON_REMOTE_REJECTED = "remote_rejected"  # the node refused (non-2xx)
REASON_REMOTE_MALFORMED = "remote_malformed_response"  # answered, but unusable
REASON_UNREACHABLE = "node_unreachable"    # no answer (connection error)
REASON_TIMEOUT = "node_timeout"            # no answer (timed out)
REASON_NOT_DISPATCHED = "not_dispatched"   # disabled service: nothing was sent

# Transport reasons (UI-13) that mean "the node answered, but not usably".
_MALFORMED_TRANSPORT_REASONS = frozenset({
    "unexpected_content_type", "malformed_response",
    "response_too_large", "invalid_operation",
})


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


class CommandTransportService:
    """Read-only command transport built from a UI-15 channel + a UI-13 transport.

    The service holds an internal `command_channel.CommandChannel` (default: the
    disabled `NullCommandChannel`) purely for validation, deterministic
    de-duplication, lifecycle bookkeeping and the audit ring — no command model is
    redefined. Dispatch is a single read-only GET through the injected transport.
    """

    def __init__(self, transport: transport_mod.Transport,
                 channel: cc.CommandChannel | None = None) -> None:
        self._transport = transport
        self._channel = channel if channel is not None else cc.NullCommandChannel()
        # command_id -> {"reason": str, "detail": str|None}; transport-step metadata
        # for commands that were NOT acknowledged (unreachable/timeout/disabled). Kept
        # beside the record rather than inside the frozen UI-15 model.
        self._dispatch: dict[str, dict] = {}
        self._sent: set[str] = set()

    # ── enablement ────────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        """True only when a real (non-null) transport is present. The default is a
        `NullTransport`, so the default service is disabled and transmits nothing."""
        return not isinstance(self._transport, transport_mod.NullTransport)

    # ── the four supported operations ─────────────────────────────────────────
    def submit(self, data: Any, *, now: datetime | None = None) -> cc.CommandRecord:
        """Validate, de-duplicate, enrol, and dispatch a read-only command ONCE.

        Raises `command_channel.CommandError` on any contract violation (unknown or
        mutating type, missing/invalid expiry, expired-before-send, missing
        idempotency key, secret-bearing payload, duplicate command id, oversized
        payload). Never raises for a transport failure — that is mapped into the
        lifecycle. No retry: exactly one attempt per fresh submission.
        """
        clock = _now(now)

        # UI-15 does validation + deterministic de-duplication + enrol-as-pending. An
        # idempotent replay returns the SAME record; a colliding command id raises.
        record = self._channel.submit(data, now=clock)

        # Idempotent replay of an already-dispatched command: return it unchanged.
        # NO second send — "no retry" and idempotency both forbid re-transmitting.
        if record.envelope.command_id in self._sent:
            return self.status(record.envelope.command_id) or record

        # Defence-in-depth allowlist: the transport step will only ever construct a
        # read-only GET. (validate_envelope already rejected anything else.)
        if record.envelope.command_type not in cc.ALLOWED_TYPES:      # pragma: no cover
            raise cc.CommandError(cc.REASON_UNKNOWN_TYPE, record.envelope.command_type)

        # Disabled service: record for audit, transmit NOTHING. Stays pending.
        if not self.enabled:
            self._dispatch[record.envelope.command_id] = {
                "reason": REASON_TRANSPORT_DISABLED, "detail": None}
            return record

        self._sent.add(record.envelope.command_id)
        result = self._transmit(record.envelope)
        return self._apply(record, result, clock)

    def acknowledge(self, command_id: str, *, accepted: bool, reason: str = "",
                    now: datetime | None = None) -> cc.CommandRecord:
        """Record an admission decision on a still-pending command (UI-15 semantics).

        Distinct from completion. On a live service the remote drives acknowledgement
        during `submit`; this remains available for a command left pending (e.g. one
        submitted while disabled) so a caller can locally accept or reject it."""
        return self._channel.acknowledge(command_id, accepted=accepted,
                                          reason=reason, now=now)

    def status(self, command_id: str) -> cc.CommandRecord | None:
        """Current lifecycle record for a command, or None if unknown."""
        return self._channel.status(command_id)

    def recent(self, limit: int = 20) -> list[cc.CommandRecord]:
        """Most-recent-first bounded list of command records."""
        return self._channel.recent(limit)

    # ── redaction-safe, value-free view (record + transport-step metadata) ─────
    def view(self, command_id: str) -> dict | None:
        """The UI-15 `safe_view` of a command plus the transport-step metadata
        (reason/detail for a command that was not acknowledged). Value-free and
        redaction-safe; suitable for an audit log or diagnostic surface."""
        record = self.status(command_id)
        if record is None:
            return None
        out = cc.safe_view(record)
        out["transport"] = self._dispatch.get(command_id)
        out["enabled"] = self.enabled
        return out

    def describe(self) -> str:
        state = "enabled" if self.enabled else "disabled"
        return f"CommandTransportService — read-only, {state}, single-attempt (no retry)"

    # ── internals ──────────────────────────────────────────────────────────────
    def _transmit(self, envelope: cc.CommandEnvelope) -> transport_mod.TransportResult:
        """One read-only GET for this command type. GET-only (UI-13), no retry."""
        if envelope.command_type in _HEALTH_TYPES:
            return self._transport.health()
        if envelope.command_type in _TELEMETRY_TYPES:
            return self._transport.request(NODE_TELEMETRY_PATH)
        # Unreachable: allowlist enforced above. Treat as an invalid, non-sent op.
        return transport_mod.TransportResult(         # pragma: no cover
            ok=False, available=False, reason="invalid_operation", detail="")

    def _apply(self, record: cc.CommandRecord, result: transport_mod.TransportResult,
               clock: datetime) -> cc.CommandRecord:
        """Map ONE transport result onto the UI-15 lifecycle.

        | transport result                         | acknowledgement | outcome    | state     |
        |-------------------------------------------|-----------------|------------|-----------|
        | ok (2xx + valid JSON)                     | accepted        | completed  | completed |
        | http_status (non-2xx; incl 401/403)       | rejected        | —          | rejected  |
        | unexpected/malformed/too-large/invalid_op | accepted         | failed     | failed    |
        | timeout / connection / unavailable        | — (none)         | —          | pending   |

        The last row is the truthful one: an unreachable node never acknowledged the
        command, so it is neither accepted, rejected nor completed — it remains
        pending (and will lazily expire). Nothing is ever fabricated as success.
        """
        command_id = record.envelope.command_id
        reason = getattr(result, "reason", "")
        detail = security_config.redact_text(getattr(result, "detail", "") or "") or None

        if getattr(result, "ok", False):
            # Delivered AND usable: acknowledge (accepted) THEN, distinctly, complete.
            self._channel.acknowledge(command_id, accepted=True,
                                      reason=REASON_DELIVERED, now=clock)
            self._dispatch[command_id] = {"reason": REASON_DELIVERED, "detail": None}
            return self._channel.record_outcome(command_id, succeeded=True,
                                                reason=REASON_FULFILLED, now=clock)

        if reason == "http_status":
            # The node answered with a refusal (non-2xx). That is a rejection.
            self._dispatch[command_id] = {"reason": REASON_REMOTE_REJECTED, "detail": detail}
            return self._channel.acknowledge(command_id, accepted=False,
                                             reason=REASON_REMOTE_REJECTED, now=clock)

        if reason in _MALFORMED_TRANSPORT_REASONS:
            # The node answered, so it is acknowledged (accepted) — but the answer was
            # unusable, so completion FAILS. Acknowledgement is distinct from completion.
            self._channel.acknowledge(command_id, accepted=True,
                                      reason=REASON_DELIVERED, now=clock)
            self._dispatch[command_id] = {"reason": REASON_REMOTE_MALFORMED, "detail": detail}
            return self._channel.record_outcome(command_id, succeeded=False,
                                                reason=REASON_REMOTE_MALFORMED,
                                                detail=detail, now=clock)

        # No answer at all (timeout / connection error / transport unavailable): the
        # node never acknowledged. Leave the command pending — truthful, and it will
        # lazily expire on its own. No retry is attempted. Return the record as it
        # stands at the submission clock (a later read may show it expired).
        step = REASON_TIMEOUT if reason == "timeout" else REASON_UNREACHABLE
        self._dispatch[command_id] = {"reason": step, "detail": detail}
        return record


def default_command_transport(*, transport: transport_mod.Transport | None = None,
                              env: dict | None = None) -> CommandTransportService:
    """The single wiring point. DISABLED by default.

    Uses `transport.default_transport()` unless a transport is injected (tests). That
    returns `NullTransport` — so the default service is disabled and sends nothing —
    unless an operator has explicitly enabled AND validly configured a real transport
    (UI-13). Never raises.
    """
    tr = transport if transport is not None else transport_mod.default_transport(env=env)
    return CommandTransportService(tr)
