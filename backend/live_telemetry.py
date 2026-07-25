"""Backend-side node-telemetry validation, normalization and freshness (UI-2).

Mirrors the node's contract (`live/telemetry.py`, schema `ct.node-telemetry.v1`)
WITHOUT importing it: the node and the Control Tower are separate deployables (the
node runs on a Windows VPS, the tower on the operator's machine), so the backend
parses node output the same way `ops_status.py` parses node files. A drift test
asserts the shared constants stay identical across the two modules.

Responsibilities:
  * validate an incoming v1 snapshot strictly (bounded, typed, sane);
  * normalize a LEGACY flat payload — the pinned VPS dry-run node still emits the
    pre-UI-2 shape and must keep publishing — into the v1 envelope, marked
    `legacy_source: true`, with every unknown field left null (never invented);
    RETIREMENT CONDITION: delete `is_legacy_payload`, `normalize_legacy` and their
    tests once the VPS node has been redeployed from a commit that includes
    `live/telemetry.py` and `GET /api/live/status` reports `legacy_source: false`
    for every instance. The adapter has no other reason to exist.
  * reject ambiguous payloads (a declared schema_version that does not validate)
    rather than guessing;
  * compute observation freshness from `published_at` at read time.

This module NEVER decides trading state. `clean`, `frozen`, `healthy`,
`eligible` and every reason code are the node's own conclusions, passed through
verbatim. The Control Tower must not recompute them (invariant I-7).
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

# Kept identical to live/telemetry.py — verified by a drift test.
SCHEMA_VERSION = "ct.node-telemetry.v1"
SUPPORTED_SCHEMA_VERSIONS = (SCHEMA_VERSION,)

MAX_POSITIONS = 50
MAX_INTENTS = 25
MAX_ATTEMPTS = 25
MAX_BLOCKS = 25
MAX_UNRESOLVED = 25
MAX_FINDINGS = 25
MAX_REASONS = 12
MAX_STR = 200
MAX_INSTANCE_ID = 64
MAX_SNAPSHOT_BYTES = 262_144          # 256 KiB — a summary, never a log

REQUIRED_TOP_LEVEL = (
    "schema_version", "instance_id", "published_at",
    "cycle", "runtime", "engine", "account", "arming", "market",
    "reconciliation", "risk", "positions", "execution",
)
_REQUIRED_MAPPINGS = ("cycle", "runtime", "engine", "account", "arming", "market",
                      "reconciliation", "risk", "execution")

# Freshness policy for the read side. Purely an observation age; it never implies
# anything about whether the node is healthy — the node says that itself.
DEFAULT_STALE_AFTER_S = 120.0


class TelemetryError(ValueError):
    """A snapshot was rejected. `reason` is a stable machine-readable code."""

    def __init__(self, reason: str, detail: str = ""):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail


def parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str) or not ts.strip():
        return None
    try:
        dt = datetime.fromisoformat(ts.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        return None
    return dt.astimezone(timezone.utc)


def _bounded_str(v: Any, limit: int) -> bool:
    return v is None or (isinstance(v, str) and len(v) <= limit)


def _finite(v: Any) -> bool:
    if v is None:
        return True
    if isinstance(v, bool):
        return False
    return isinstance(v, (int, float)) and math.isfinite(v)


def validate_snapshot(payload: Any) -> dict:
    """Strictly validate a v1 snapshot. Raises `TelemetryError` on any problem.

    Returns the payload unchanged on success — validation never rewrites node
    truth."""
    if not isinstance(payload, dict):
        raise TelemetryError("payload_not_object")
    version = payload.get("schema_version")
    if not isinstance(version, str) or not version:
        raise TelemetryError("schema_version_missing")
    if version not in SUPPORTED_SCHEMA_VERSIONS:
        raise TelemetryError("schema_version_unsupported", version[:64])

    missing = [k for k in REQUIRED_TOP_LEVEL if k not in payload]
    if missing:
        raise TelemetryError("required_field_missing", ",".join(sorted(missing))[:MAX_STR])

    instance_id = payload.get("instance_id")
    if not isinstance(instance_id, str) or not instance_id.strip():
        raise TelemetryError("instance_id_invalid")
    if len(instance_id) > MAX_INSTANCE_ID:
        raise TelemetryError("instance_id_too_long")

    if parse_iso(payload.get("published_at")) is None:
        raise TelemetryError("published_at_invalid")

    for key in _REQUIRED_MAPPINGS:
        if not isinstance(payload.get(key), dict):
            raise TelemetryError("section_not_object", key)

    for key, limit in (("positions", MAX_POSITIONS),):
        value = payload.get(key)
        if not isinstance(value, list):
            raise TelemetryError("section_not_list", key)
        if len(value) > limit:
            raise TelemetryError("list_too_long", key)

    # Position ITEMS are validated, not coerced. A malformed entry is rejected so a
    # garbage row can never be served as if it were an owned position; nothing here
    # substitutes a plausible-looking default for a broken one.
    for position in payload["positions"]:
        if not isinstance(position, dict):
            raise TelemetryError("position_not_object")
        if not _bounded_str(position.get("trade_id"), MAX_STR):
            raise TelemetryError("position_field_invalid", "trade_id")
        ticket = position.get("broker_ticket")
        if ticket is not None and (isinstance(ticket, bool) or not isinstance(ticket, int)):
            raise TelemetryError("position_field_invalid", "broker_ticket")
        if not _finite(position.get("volume")):
            raise TelemetryError("position_field_invalid", "volume")
        for field in ("symbol", "comment", "reconciliation_status", "local_status"):
            if not _bounded_str(position.get(field), MAX_STR):
                raise TelemetryError("position_field_invalid", field)

    # Reason lists stay bounded wherever the contract carries them.
    eligibility = payload["runtime"].get("open_eligibility")
    if isinstance(eligibility, dict):
        reasons = eligibility.get("reasons")
        if reasons is not None:
            if not isinstance(reasons, list):
                raise TelemetryError("section_not_list", "runtime.open_eligibility.reasons")
            if len(reasons) > MAX_REASONS:
                raise TelemetryError("list_too_long", "runtime.open_eligibility.reasons")

    execution = payload["execution"]
    for key, limit in (("cycle_intents", MAX_INTENTS),
                       ("pending_intents", MAX_INTENTS), ("attempts", MAX_ATTEMPTS),
                       ("blocks", MAX_BLOCKS), ("unresolved_sent", MAX_UNRESOLVED)):
        value = execution.get(key)
        if value is None:
            continue
        if not isinstance(value, list):
            raise TelemetryError("section_not_list", f"execution.{key}")
        if len(value) > limit:
            raise TelemetryError("list_too_long", f"execution.{key}")

    findings = payload["reconciliation"].get("findings")
    if findings is not None:
        if not isinstance(findings, list):
            raise TelemetryError("section_not_list", "reconciliation.findings")
        if len(findings) > MAX_FINDINGS:
            raise TelemetryError("list_too_long", "reconciliation.findings")

    for optional in ("deployment_id", "execution_node_id"):
        if not _bounded_str(payload.get(optional), MAX_INSTANCE_ID):
            raise TelemetryError("field_invalid", optional)

    risk = payload["risk"]
    for field in ("daily_realized_r", "fixed_risk_lots", "daily_loss_limit_r"):
        if not _finite(risk.get(field)):
            raise TelemetryError("numeric_invalid", f"risk.{field}")
    market = payload["market"]
    for field in ("bid", "ask", "spread", "tick_age_seconds"):
        if not _finite(market.get(field)):
            raise TelemetryError("numeric_invalid", f"market.{field}")
    health = payload["account"].get("health")
    if isinstance(health, dict):
        for field in ("balance", "equity", "free_margin"):
            if not _finite(health.get(field)):
                raise TelemetryError("numeric_invalid", f"account.health.{field}")
    return payload


# Sections that exist ONLY in v1. A versionless payload carrying any of them is
# not the deployed legacy shape — it is ambiguous, and adapting it would SILENTLY
# DISCARD real safety state (a v1 `runtime`/`arming`/`market` block would be
# replaced by "unavailable" nulls).
_V1_ONLY_SECTIONS = ("runtime", "engine", "account", "arming", "market", "risk", "cycle")
# Keys the deployed pre-UI-2 publisher actually emitted (verified against the
# payload builder at commit 83061ed and backend/tests/test_live_ingest.py).
_LEGACY_MARKERS = ("at", "mode", "engine_version", "deployment_profile", "data_seam",
                   "symbol", "runner")


def is_legacy_payload(payload: Any) -> bool:
    """Is this precisely the pre-UI-2 flat payload the pinned VPS node emits?

    Requires: no `schema_version`, a string `instance_id`, at least one key the old
    publisher really produced, and NO v1-only section. The last condition is what
    makes the adapter safe — see `_V1_ONLY_SECTIONS`."""
    if not isinstance(payload, dict) or "schema_version" in payload:
        return False
    if not isinstance(payload.get("instance_id"), str):
        return False
    if any(k in payload for k in _V1_ONLY_SECTIONS):
        return False
    return any(k in payload for k in _LEGACY_MARKERS)


def normalize_legacy(payload: dict) -> dict:
    """Adapt the LEGACY flat payload into the v1 envelope.

    Required because the pinned VPS dry-run node still emits the old shape and must
    not be redeployed. Every field the old shape does not carry stays null — the
    adapter never invents identity, health, arming, market or lineage it was not
    given. Marked `legacy_source: true` so the read side (and any future UI) can
    show that this instance predates the full contract."""
    runner = payload.get("runner") if isinstance(payload.get("runner"), dict) else {}
    execution = payload.get("execution") if isinstance(payload.get("execution"), dict) else {}
    reconcile = payload.get("reconciliation") if isinstance(payload.get("reconciliation"), dict) else {}
    published = payload.get("at")
    if parse_iso(published) is None:
        published = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    mode = payload.get("mode") if isinstance(payload.get("mode"), str) else None
    positions = payload.get("positions")
    positions = positions[:MAX_POSITIONS] if isinstance(positions, list) else []
    intents = payload.get("intents")
    intents = intents[:MAX_INTENTS] if isinstance(intents, list) else []
    return {
        "schema_version": SCHEMA_VERSION,
        "legacy_source": True,
        "instance_id": payload["instance_id"][:MAX_INSTANCE_ID],
        "deployment_id": None,
        "execution_node_id": None,
        "published_at": published,
        "cycle": {
            "sequence": None,
            "status": runner.get("status"),
            "last_boundary": runner.get("boundary"),
            "last_bar_time": (payload.get("bridge") or {}).get("last_bar_time")
                              if isinstance(payload.get("bridge"), dict) else None,
            "cycle_age_seconds": None,
            "trades_rows": runner.get("trades_rows"),
            "note": runner.get("note"),
        },
        "runtime": {
            "mode": mode,
            "submission_disabled": None,
            "kill_switch_active": None,
            "open_eligibility": {"eligible": None, "reasons": ["legacy_payload_no_eligibility"]},
        },
        "engine": {
            "strategy_family": None,
            "engine_version_expected": None,
            "engine_version_actual": payload.get("engine_version"),
            "config_fingerprint": None,
            "input_revision": None,
            "symbol": payload.get("symbol"),
            "timeframe": None,
            "deployment_profile": payload.get("deployment_profile"),
            "data_seam": payload.get("data_seam"),
        },
        "account": {
            "identity": {"available": False, "fingerprint": None, "server": None,
                         "currency": None, "trade_mode": None},
            "health": {"available": False, "healthy": None, "balance": None, "equity": None,
                       "free_margin": None, "trade_allowed": None, "trade_expert": None,
                       "observed_at": None, "reasons": ["legacy_payload_no_health"]},
        },
        "arming": {"status": "unarmed", "armed": False, "expires_at": None,
                   "probation_max_opens": None, "attempts_remaining": None,
                   "account_fingerprint": None, "fingerprint_matches": None,
                   "reason": "legacy_payload_no_arming"},
        "market": {"available": False, "symbol": payload.get("symbol"), "bid": None,
                   "ask": None, "spread": None, "tick_age_seconds": None,
                   "feed_healthy": None, "observed_at": None},
        "reconciliation": {
            "available": bool(reconcile),
            "clean": None,
            "frozen": bool(reconcile.get("frozen")) if reconcile else None,
            "snapshot_status": reconcile.get("snapshot_status"),
            "unresolved_sent_count": None,
            "expected_position_count": None,
            "observed_position_count": None,
            "recovery_required": None,
            "findings": (reconcile.get("findings") or [])[:MAX_FINDINGS]
                        if isinstance(reconcile.get("findings"), list) else [],
            "counts": reconcile.get("counts") if isinstance(reconcile.get("counts"), dict) else {},
            "last_completed_at": None,
        },
        "risk": {"daily_realized_r": None, "daily_date": None,
                 "open_mirror_count": None, "max_open_positions": None,
                 "fixed_risk_lots": None, "daily_loss_limit_r": None},
        "positions": positions,
        "execution": {
            "cycle_frozen": bool(execution.get("frozen")),
            "cycle_intents": intents,
            "pending_intents": [],
            "attempts": (execution.get("applied") or [])[:MAX_ATTEMPTS]
                        if isinstance(execution.get("applied"), list) else [],
            "blocks": (execution.get("blocked") or [])[:MAX_BLOCKS]
                      if isinstance(execution.get("blocked"), list) else [],
            "skipped_count": len(execution.get("skipped") or [])
                             if isinstance(execution.get("skipped"), list) else 0,
            "unresolved_sent": [],
            "ledger_counts": {},
            "drained": [],
        },
    }


def coerce_snapshot(payload: Any) -> tuple[dict, bool]:
    """Return `(validated_v1_snapshot, was_legacy)` or raise `TelemetryError`.

    Two ambiguity directions are both rejected rather than guessed:
      * declares a `schema_version` but fails validation -> not downgraded to the
        legacy adapter;
      * omits `schema_version` yet carries v1-only sections -> not gutted by the
        adapter (`schema_version_missing`), because that would drop real state.
    """
    if is_legacy_payload(payload):
        return validate_snapshot(normalize_legacy(payload)), True
    return validate_snapshot(payload), False


def observation(snapshot: dict, *, now: datetime | None = None,
                stale_after_s: float = DEFAULT_STALE_AFTER_S) -> dict:
    """Read-side freshness envelope. Pure; adds no node claims."""
    now = now or datetime.now(timezone.utc)
    published = parse_iso(snapshot.get("published_at"))
    age = None if published is None else max((now - published).total_seconds(), 0.0)
    return {
        "instance_id": snapshot.get("instance_id"),
        "schema_version": snapshot.get("schema_version"),
        "legacy_source": bool(snapshot.get("legacy_source")),
        "published_at": snapshot.get("published_at"),
        "observed_at": now.isoformat().replace("+00:00", "Z"),
        "age_seconds": None if age is None else round(age, 3),
        "stale": True if age is None else age > stale_after_s,
        "stale_after_seconds": stale_after_s,
        "snapshot": snapshot,
    }
