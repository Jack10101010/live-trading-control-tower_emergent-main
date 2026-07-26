"""UI-12 — the canonical remote-transport contract. ARCHITECTURE ONLY.

This module defines the SHAPE a future Control-Tower -> node transport must take,
and ships the default `NullTransport` that stands in until a real transport is
deliberately built. It contains no networking of any kind and none is reachable
from here: no socket, no HTTP client, no WebSocket, no polling, no retry, no
authentication, no TLS, no VPS address. Importing or exercising anything in this
file cannot open a connection.

WHY NETWORKING IS ABSENT ON PURPOSE
    The Control Tower is observational (invariant I-7) and the live node stays
    autonomous when the tower is unreachable (I-10). Remote connectivity is a
    later, separately-audited slice gated by the UI-9 security prerequisites
    (TLS, a private network path, authenticated transport). Defining the interface
    now — with a fail-predictably default — lets every future caller depend on one
    stable seam instead of an ad-hoc client, and lets review see the intended shape
    before any wire code exists.

THE DEFAULT IS NullTransport, EVERYWHERE
    `default_transport()` is the single wiring point. It returns `NullTransport`
    unconditionally in this slice: there is no real transport to select, and
    `security_config.is_active()` is hard-disabled. A future slice adds a real
    implementation HERE, at one place, so nothing ever references a concrete
    transport inline.

    NullTransport never attempts communication. Every operation returns a typed,
    non-raising result whose reason is `transport_unavailable`. It is safe to call
    from anywhere, including code paths that must never block or fail.

RELATION TO UI-9
    UI-9 declared a typing-only `security_config.TransportAdapter` Protocol
    (`describe()`) as a forward placeholder. `Transport` here is its canonical
    expansion and `NullTransport` satisfies that Protocol, so existing references
    stay valid.
"""

from __future__ import annotations

import abc
import os
from dataclasses import dataclass, field
from typing import Any

# ── explicit activation (UI-13) ──────────────────────────────────────────────
# The ONLY switch that permits selecting a real transport. Absent/blank/malformed
# leaves the default NullTransport. This is deliberately separate from UI-9's
# security-baseline `is_active()` (which stays hard-disabled): this flag governs
# adapter SELECTION, and even when set, a real transport is chosen only if the
# security configuration also validates (see rest_transport.select_rest_transport).
VAR_TRANSPORT_ENABLED = "CONTROL_TOWER_TRANSPORT_ENABLED"
_TRUTHY = frozenset({"1", "true", "yes", "on"})

# ── stable reason codes (safe to display; never a value or a secret) ──────────
REASON_UNAVAILABLE = "transport_unavailable"
REASON_NOT_CONNECTED = "transport_not_connected"

# The one message the default surfaces. Deliberately generic and actionable
# without implying a fault: there simply is no transport yet.
UNAVAILABLE_DETAIL = (
    "No remote transport is configured. UI-12 ships NullTransport by default; "
    "remote connectivity is a later slice gated by the UI-9 security prerequisites."
)

# Transport kinds. Only "null" exists today; the others name the vocabulary a
# future slice would implement, so classification stays stable. None is built.
KIND_NULL = "null"
KNOWN_KINDS = (KIND_NULL, "https", "websocket")


@dataclass(frozen=True)
class ConnectionResult:
    """Outcome of `connect()`. Never raises; `connected` is the whole answer."""
    connected: bool
    reason: str
    detail: str = ""


@dataclass(frozen=True)
class TransportResult:
    """Outcome of `health()` or `request()`.

    `available` states whether the transport could carry the operation at all;
    `ok` states whether the operation itself succeeded. A transport that is not
    present is `available=False, ok=False` — the two are distinct so a caller can
    tell "no transport" from "transport present but the request failed". No field
    ever carries a credential or a raw wire payload the caller did not supply.
    """
    ok: bool
    available: bool
    reason: str
    detail: str = ""
    payload: Any = None


class Transport(abc.ABC):
    """The canonical remote-transport interface.

    A transport is the ONLY way a future Control Tower would reach the node, so it
    is kept behind this boundary and never coupled to one wire format. Every method
    is defined to be TOTAL and NON-RAISING: failures are returned as results, not
    exceptions, because the callers (observability surfaces) must never be taken
    down by transport state.

    `stream()` is deliberately NOT part of the contract: today the node publishes
    periodic snapshots (a request/response shape), not a continuous stream, so a
    streaming operation would be speculative surface. It can be added when a real
    need appears, alongside the implementation that justifies it.
    """

    #: Short, stable identifier for the concrete transport (e.g. "null").
    kind: str = KIND_NULL

    @abc.abstractmethod
    def connect(self) -> ConnectionResult:
        """Establish the transport. Returns a result; never raises, never blocks
        indefinitely in a real implementation."""

    @abc.abstractmethod
    def disconnect(self) -> None:
        """Tear down an established transport. Idempotent; never raises."""

    @abc.abstractmethod
    def health(self) -> TransportResult:
        """Report whether the transport can currently carry traffic. Observation
        only — must not itself mutate remote state."""

    @abc.abstractmethod
    def request(self, operation: str, payload: Any = None) -> TransportResult:
        """Perform one request/response operation. `operation` names it; `payload`
        is caller-supplied. Returns a result; never raises."""

    @abc.abstractmethod
    def close(self) -> None:
        """Release any resources. Idempotent; safe to call more than once, and
        after `disconnect()`. Never raises."""

    def describe(self) -> str:
        """One-line, value-free description. Satisfies UI-9's `TransportAdapter`
        Protocol so existing references remain valid."""
        return f"{self.kind} transport"

    def status(self) -> dict:
        """Value-free status for diagnostics. Classifications only — no endpoint,
        no credential, no payload."""
        health = self.health()
        return {
            "kind": self.kind,
            "available": health.available,
            "reason": health.reason,
        }


class NullTransport(Transport):
    """The default transport: present in the object graph, absent on the wire.

    Every operation returns `transport_unavailable` without touching a socket. It
    exists so callers always have a real object to hold and a predictable result to
    branch on, instead of `None` checks scattered through the code. It is the
    correct behaviour today, not a stub: there is no remote transport, so reporting
    exactly that is the truthful answer.
    """

    kind = KIND_NULL

    def connect(self) -> ConnectionResult:
        return ConnectionResult(connected=False, reason=REASON_UNAVAILABLE,
                                detail=UNAVAILABLE_DETAIL)

    def disconnect(self) -> None:
        return None                       # nothing was ever connected

    def health(self) -> TransportResult:
        return TransportResult(ok=False, available=False, reason=REASON_UNAVAILABLE,
                               detail=UNAVAILABLE_DETAIL)

    def request(self, operation: str, payload: Any = None) -> TransportResult:
        # The operation name is echoed so a caller/log can see WHAT was attempted;
        # the payload is never inspected, stored or transmitted.
        return TransportResult(ok=False, available=False, reason=REASON_UNAVAILABLE,
                               detail=f"{UNAVAILABLE_DETAIL} (operation: {operation})")

    def close(self) -> None:
        return None

    def describe(self) -> str:
        return "NullTransport — no networking; every operation reports transport unavailable"


def default_transport(config: Any = None, env: dict | None = None) -> Transport:
    """The single wiring point for a Control-Tower -> node transport.

    Returns `NullTransport` unless ALL of the following hold, in which case it
    returns the real `RestTransport` (UI-13):

      1. `CONTROL_TOWER_TRANSPORT_ENABLED` is explicitly truthy, AND
      2. the UI-9 security configuration validates with no errors, `NODE_TRANSPORT`
         is `https`, and a `NODE_ENDPOINT` + `NODE_API_TOKEN` are present.

    Missing, invalid or disabled configuration returns `NullTransport`. There is no
    implicit localhost or remote fallback: absence yields the null default, never a
    guessed endpoint. Keeping selection in this one function is what makes "the
    default is NullTransport everywhere" a guarantee.

    `rest_transport` is imported LAZILY so this module (and every default caller)
    stays free of any networking import until a real transport is deliberately
    requested. Never raises.
    """
    source = os.environ if env is None else env
    raw = source.get(VAR_TRANSPORT_ENABLED)
    if not (isinstance(raw, str) and raw.strip().lower() in _TRUTHY):
        return NullTransport()
    try:
        from rest_transport import select_rest_transport
        return select_rest_transport(source, config) or NullTransport()
    except Exception:                     # selection must never break startup
        return NullTransport()


#: The default instance callers can share. Stateless, so a singleton is safe.
NULL_TRANSPORT: Transport = NullTransport()
