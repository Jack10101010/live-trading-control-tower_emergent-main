"""UI-14 — read-only Control-Tower -> node integration over the UI-13 transport.

This is the first *pull*: UI-2 is the node PUSHING telemetry to the tower
(`POST /api/live/ingest`); this module has the tower READ health and a telemetry
snapshot FROM the node, through `transport.default_transport()`. It is read-only —
health and telemetry only, no mutation, no command, no acknowledgement — and
disabled by default (the default transport is `NullTransport`, so the integration
reports `disabled` and touches nothing).

TRUTHFULNESS RULES (the reason this module exists):
  * Remote data is ALWAYS provenance `remote-node`. It is never relabelled local or
    fixture, and a failed request NEVER falls back to fixture data — a failure is a
    failure state, not a quiet substitution.
  * Freshness is judged by the shared UI-2 rule (`live_telemetry.observation`): a
    stale OR implausibly-future `published_at` is not healthy.
  * States are distinguished, not collapsed: disabled / healthy / degraded / stale
    / unauthorized / unreachable (the frontend adds a transient `connecting`).
  * No telemetry model is duplicated: a fetched snapshot is validated with the
    existing `live_telemetry.validate_snapshot` and summarised with the existing
    `observation()` envelope.
  * Errors are redaction-safe: every surfaced detail passes through
    `security_config.redact_text`, and the bearer token lives only inside the
    transport, never here.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import live_telemetry
import security_config
import transport as transport_mod

# ── the node's read-only endpoints (a future node server exposes these) ───────
NODE_HEALTH_PATH = "/health"
NODE_TELEMETRY_PATH = "/telemetry"

# ── integration states (stable; the frontend mirrors these) ───────────────────
STATE_DISABLED = "disabled"          # no real transport selected (the default)
STATE_CONNECTING = "connecting"      # frontend-only, shown while the query loads
STATE_HEALTHY = "healthy"            # reachable, authenticated, fresh telemetry
STATE_DEGRADED = "degraded"          # reachable+authenticated but telemetry unusable
STATE_STALE = "stale"                # reachable but telemetry is old / future-dated
STATE_UNAUTHORIZED = "unauthorized"  # the node rejected the credential (401/403)
STATE_UNREACHABLE = "unreachable"    # timeout / connection error

PROVENANCE = "remote-node"


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _classify_transport_failure(result) -> str:
    """Map a non-ok `TransportResult` onto an integration state."""
    reason = getattr(result, "reason", "")
    if reason == "http_status":
        detail = (getattr(result, "detail", "") or "")
        # 401/403 => the credential was rejected; other statuses => degraded.
        if "401" in detail or "403" in detail:
            return STATE_UNAUTHORIZED
        return STATE_DEGRADED
    if reason in ("timeout", "connection_error"):
        return STATE_UNREACHABLE
    # unexpected_content_type / malformed_response / response_too_large /
    # invalid_operation: the endpoint answered but not usably.
    return STATE_DEGRADED


def _empty(state: str, *, enabled: bool, now: datetime,
           reason: str | None = None, detail: str | None = None,
           health: dict | None = None) -> dict:
    """A result with NO telemetry. Never carries fixture data — absence is honest."""
    return {
        "enabled": enabled,
        "state": state,
        "provenance": PROVENANCE,
        "observedAt": _iso(now),
        "reason": reason,
        "detail": security_config.redact_text(detail) if detail else None,
        "health": health,
        "telemetry": None,
    }


def poll_node(*, transport=None, env: dict | None = None,
              now: datetime | None = None,
              stale_after_s: float = live_telemetry.DEFAULT_STALE_AFTER_S) -> dict:
    """Perform one read-only health + telemetry pull and classify the result.

    Never raises. `transport` may be injected (tests); otherwise the canonical
    `default_transport()` is used, which is `NullTransport` unless an operator has
    explicitly enabled and validly configured a real transport (UI-13).
    """
    clock = _now(now)
    tr = transport if transport is not None else transport_mod.default_transport(env=env)

    # Disabled: the default. No network, no fabrication.
    if isinstance(tr, transport_mod.NullTransport):
        return _empty(STATE_DISABLED, enabled=False, now=clock,
                      reason="transport_disabled")

    # 1) Health probe (read-only).
    try:
        health = tr.health()
    except Exception as exc:                       # a transport must not raise; defence
        return _empty(STATE_UNREACHABLE, enabled=True, now=clock,
                      reason="transport_error",
                      detail=security_config.redact_text(type(exc).__name__))
    health_view = {"ok": bool(getattr(health, "ok", False)),
                   "reason": getattr(health, "reason", "")}
    if not getattr(health, "ok", False):
        state = _classify_transport_failure(health)
        return _empty(state, enabled=True, now=clock,
                      reason=getattr(health, "reason", None),
                      detail=getattr(health, "detail", None), health=health_view)

    # 2) Telemetry snapshot (read-only). Health ok, so any failure here is degraded
    #    (reachable + authenticated) unless it is an auth/reachability failure.
    try:
        result = tr.request(NODE_TELEMETRY_PATH)
    except Exception as exc:
        return _empty(STATE_UNREACHABLE, enabled=True, now=clock,
                      reason="transport_error",
                      detail=security_config.redact_text(type(exc).__name__),
                      health=health_view)
    if not getattr(result, "ok", False):
        state = _classify_transport_failure(result)
        return _empty(state, enabled=True, now=clock,
                      reason=getattr(result, "reason", None),
                      detail=getattr(result, "detail", None), health=health_view)

    # 3) Validate the payload against the EXISTING telemetry contract. A payload the
    #    node sent that does not validate is DEGRADED, never silently accepted.
    payload = getattr(result, "payload", None)
    try:
        snapshot = live_telemetry.validate_snapshot(payload)
    except live_telemetry.TelemetryError as exc:
        return _empty(STATE_DEGRADED, enabled=True, now=clock,
                      reason="malformed_telemetry",
                      detail=security_config.redact_text(exc.reason), health=health_view)

    # 4) Freshness via the shared rule: stale OR future-dated is not healthy.
    obs = live_telemetry.observation(snapshot, now=clock, stale_after_s=stale_after_s)
    state = STATE_STALE if obs["stale"] else STATE_HEALTHY
    return {
        "enabled": True,
        "state": state,
        "provenance": PROVENANCE,
        "observedAt": _iso(clock),
        "reason": None,
        "detail": None,
        "health": health_view,
        # The freshness envelope only — the same value-free summary UI-2 exposes.
        # No secret can appear here: node telemetry is redacted at the contract by
        # UI-2's safe projections.
        "telemetry": {
            "available": True,
            "instanceId": snapshot.get("instance_id"),
            "schemaVersion": obs["schema_version"],
            "publishedAt": obs["published_at"],
            "ageSeconds": obs["age_seconds"],
            "staleAfterSeconds": obs["stale_after_seconds"],
            "stale": obs["stale"],
            "problem": None,
        },
    }
