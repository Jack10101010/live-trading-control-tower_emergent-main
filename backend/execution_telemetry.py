"""ARCH-2 — canonical execution telemetry: a DERIVED read model.

One function builds the execution read model entirely from durable/canonical
sources — the execution store, the active adapter, and the reconciliation
posture. Nothing here is independently mutated, cached, or invented:

  * the mock adapter is explicitly labelled (`provenance: "mock-fixture"`)
  * stale / absent / degraded remain distinct — absence of evidence is reported
    as absence, never upgraded to health
  * readiness is DERIVED from named gates; there is no constant `tradingReady`.
    In the mock-only world every readiness answer is honestly False, with the
    failing gates listed by name.
"""

from __future__ import annotations

from typing import Any

import order_lifecycle as ol

SCHEMA_VERSION = "ct.execution-telemetry.v1"

#: Lifecycle states surfaced as "pending" work (not yet terminal).
_PENDING_STATES = tuple(sorted(
    (ol.ALL_STATES - ol.TERMINAL_STATES) - {ol.RECONCILED}))
_ACTIVE_ORDER_STATES = tuple(sorted(ol.IN_FLIGHT_STATES))


def readiness_gates(*, adapter_kind: str | None, adapter_connection: str | None,
                    reconciliation: dict | None, node_healthy: bool,
                    store_available: bool, extra: dict | None = None) -> dict:
    """The explicit, named gates readiness derives from. Every gate is a fact
    with a truthful source; `tradingReady` is their conjunction and can never be
    a constant."""
    recon = reconciliation or {}
    gates = {
        "liveAdapterActive": bool(adapter_kind) and adapter_kind != "mock",
        "adapterConnected": adapter_connection == "Connected",
        "executionStoreAvailable": bool(store_available),
        "reconciliationClean": not recon.get("criticalUnresolved", True)
                               and not recon.get("stale", True),
        "nodeHealthy": bool(node_healthy),
    }
    # ARCH-3: additional named gates (operator auth, command authorization,
    # account identity, connection profile, ...). Each is a named boolean; the
    # conjunction stays the only readiness answer.
    for name, value in (extra or {}).items():
        gates[str(name)] = bool(value)
    return gates


def trading_ready(gates: dict) -> bool:
    """Derived readiness: the conjunction of every named gate. In the mock-only
    ARCH-2 world `liveAdapterActive` is False, so this is False — truthfully,
    not by hard-coding."""
    return all(bool(v) for v in gates.values())


def build(*, store, adapter_kind: str | None, adapter_connection: str | None,
          adapter_provenance: str, account_identity: dict | None,
          execution_mode: str, reconciliation_posture: dict | None,
          node_healthy: bool, now: str,
          recent_limit: int = 25) -> dict:
    """Assemble the canonical execution read model. Pure derivation; any store
    failure is reported as degraded state, never masked."""
    store_available = store is not None
    counts: dict = {}
    pending: list = []
    active_orders: list = []
    recent_fills: list = []
    recent_failures: list = []
    degraded_detail = None
    if store is not None:
        try:
            counts = store.counts_by_state()
            pending = [_intent_view(r) for r in store.intents_by_state(
                tuple(s for s in _PENDING_STATES), limit=recent_limit)]
            active_orders = [_intent_view(r) for r in store.intents_by_state(
                _ACTIVE_ORDER_STATES, limit=recent_limit)]
            recent_fills = [_intent_view(r) for r in store.intents_by_state(
                (ol.FILLED, ol.MODIFIED, ol.CANCELLED, ol.CLOSED), limit=recent_limit)]
            recent_failures = [_intent_view(r) for r in store.intents_by_state(
                (ol.REJECTED, ol.FAILED, ol.EXPIRED, ol.SAFETY_DENIED), limit=recent_limit)]
        except Exception as exc:            # degraded is a distinct, reported state
            store_available = False
            degraded_detail = f"execution store unreadable: {type(exc).__name__}"

    recon = reconciliation_posture or {"criticalUnresolved": True, "stale": True,
                                       "lastRunId": None, "lastRunAt": None,
                                       "detail": "no reconciliation posture supplied"}
    gates = readiness_gates(adapter_kind=adapter_kind,
                            adapter_connection=adapter_connection,
                            reconciliation=recon, node_healthy=node_healthy,
                            store_available=store_available)
    return {
        "schemaVersion": SCHEMA_VERSION,
        "observedAt": now,
        "adapter": {
            "kind": adapter_kind,
            "connection": adapter_connection,
            # The mock is never presentable as a live broker.
            "provenance": adapter_provenance,
        },
        "account": account_identity,        # value-free identity status from the adapter
        "executionMode": execution_mode,
        "store": {"available": store_available, "degradedDetail": degraded_detail,
                  "countsByState": counts},
        "pendingIntents": pending,
        "activeOrders": active_orders,
        "recentFills": recent_fills,
        "recentFailures": recent_failures,
        "reconciliation": recon,
        "availability": {
            # Execution through the canonical pipeline is available only when the
            # store can record it; the DENIAL reasons are explicit.
            "executionAvailable": store_available,
            "denialReasons": [name for name, ok in gates.items() if not ok],
        },
        "readiness": {
            "tradingReady": trading_ready(gates),   # derived — never a constant
            "gates": gates,
        },
    }


def _intent_view(row: dict) -> dict:
    """Value-free projection of a stored intent row (metadata already redacted at
    persistence time; it is deliberately NOT surfaced here at all)."""
    return {
        "intentId": row.get("intent_id"),
        "commandId": row.get("command_id"),
        "commandName": row.get("command_name"),
        "kind": row.get("kind"),
        "state": row.get("state"),
        "instrument": row.get("instrument"),
        "deploymentId": row.get("deployment_id"),
        "brokerRef": row.get("broker_ref"),
        "createdAt": row.get("created_at"),
        "updatedAt": row.get("updated_at"),
        "source": row.get("source"),
    }
