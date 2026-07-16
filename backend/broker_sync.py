"""
Broker synchronisation & state reconciliation (Phase 7).

A single, READ-ONLY service that:
  • polls broker state (via the Phase-6 Broker interface),
  • builds a canonical runtime snapshot,
  • compares the two and produces STRUCTURED reconciliation results,
  • never mutates runtime and never repairs anything.

Fault injection lives HERE (dev-only toggles), not in the broker — the broker
abstraction from Phase 6 is untouched. The runtime remains authoritative; the
MockBroker remains the active broker; there is zero live trading.
"""
from __future__ import annotations

import copy
import time
from dataclasses import dataclass, asdict
from typing import Any


# ---------------------------------------------------------------------------
# Reconciliation vocabulary
# ---------------------------------------------------------------------------

class Severity:
    OK = "ok"
    WARNING = "warning"
    ERROR = "error"


# Detection types (Objective 3).
MISSING_POSITION = "missing_position"
UNEXPECTED_POSITION = "unexpected_position"
MISSING_ORDER = "missing_order"
UNEXPECTED_ORDER = "unexpected_order"
STATE_MISMATCH = "state_mismatch"
SIZE_MISMATCH = "size_mismatch"
SL_MISMATCH = "sl_mismatch"
TP_MISMATCH = "tp_mismatch"
EXECUTION_MODE_MISMATCH = "execution_mode_mismatch"
PACKAGE_MISMATCH = "package_mismatch"
SYMBOL_MISMATCH = "symbol_mismatch"
DUPLICATE_ENTITY = "duplicate_entity"
CONNECTION_DEGRADED = "connection_degraded"
STALE_PRICE = "stale_price"


@dataclass
class Finding:
    type: str
    severity: str
    entityId: str
    detail: str
    brokerValue: Any = None
    runtimeValue: Any = None


# Dev-only fault toggles (Objective 8). Applied to the BROKER snapshot only, so
# the runtime stays authoritative and the baseline (all-off) reconciles clean.
DEFAULT_FAULTS: dict[str, bool] = {
    "positionDisappears": False,
    "unexpectedPosition": False,
    "connectionDegraded": False,
    "stalePrices": False,
    "missingSL": False,
    "missingTP": False,
    "duplicateOrder": False,
    # extra toggles so every detection path is exercisable
    "sizeMismatch": False,
    "stateMismatch": False,
    "symbolMismatch": False,
    "executionModeMismatch": False,
    "packageMismatch": False,
}

_FAULTS: dict[str, bool] = dict(DEFAULT_FAULTS)


def get_faults() -> dict[str, bool]:
    return dict(_FAULTS)


def set_faults(patch: dict) -> dict[str, bool]:
    for k, v in (patch or {}).items():
        if k in _FAULTS:
            _FAULTS[k] = bool(v)
    return get_faults()


def clear_faults() -> dict[str, bool]:
    global _FAULTS
    _FAULTS = dict(DEFAULT_FAULTS)
    return get_faults()


# ---------------------------------------------------------------------------
# Canonical runtime snapshot (Objective 2) — the authoritative comparison basis.
# ---------------------------------------------------------------------------

@dataclass
class RuntimeViews:
    """Runtime data the reconciler needs, injected by the runtime so this module
    never imports server internals."""
    deployments: list
    positions: list       # normalized open positions (authoritative)
    orders: list          # normalized resting orders (authoritative)
    accounts: list
    active_package: dict
    runtime_health: dict
    connection_state: str
    event_seq: int
    now: str


def build_runtime_snapshot(v: RuntimeViews) -> dict:
    return {
        "deployments": [{"deploymentId": d.get("deploymentId"), "status": d.get("status"),
                         "executionMode": d.get("executionMode"), "packageHash": d.get("packageHash")}
                        for d in v.deployments],
        "positions": v.positions,
        "orders": v.orders,
        "accounts": v.accounts,
        "activePackage": v.active_package,
        "runtimeHealth": {k: v.runtime_health.get(k) for k in
                          ("overlayLoaded", "runtimeDbHealthy", "eventStoreHealthy", "overlayCount", "eventCount")},
        "connectionState": v.connection_state,
        "eventSeq": v.event_seq,
        "timestamp": v.now,
        "counts": {"positions": len(v.positions), "orders": len(v.orders),
                   "deployments": len(v.deployments), "accounts": len(v.accounts)},
    }


# ---------------------------------------------------------------------------
# Broker snapshot — what the broker reports (base = authoritative view, then any
# dev faults perturb it to simulate divergence).
# ---------------------------------------------------------------------------

def _apply_faults(positions: list, orders: list, connection: str, faults: dict) -> tuple[list, list, str, list[str]]:
    positions = copy.deepcopy(positions)
    orders = copy.deepcopy(orders)
    notes: list[str] = []

    if faults.get("positionDisappears") and positions:
        dropped = positions.pop(0)
        notes.append(f"position {dropped.get('id')} disappeared")
    if faults.get("unexpectedPosition"):
        positions.append({"id": "pos_broker_ghost", "canonicalSymbol": "EURUSD", "brokerSymbol": "EURUSD",
                          "side": "long", "size": 0.1, "entry": 1.0800, "sl": 1.0780, "tp": 1.0850,
                          "state": "managing", "deploymentId": None, "executionMode": "live", "packageHash": None})
    if faults.get("missingSL") and positions:
        positions[0]["sl"] = None
        notes.append(f"position {positions[0].get('id')} missing SL")
    if faults.get("missingTP") and positions:
        positions[0]["tp"] = None
    if faults.get("sizeMismatch") and positions:
        positions[0]["size"] = round((positions[0].get("size") or 0) * 2, 4)
    if faults.get("stateMismatch") and positions:
        positions[0]["state"] = "closing"
    if faults.get("symbolMismatch") and positions:
        positions[0]["canonicalSymbol"] = "EURUSD.wrong"
    if faults.get("executionModeMismatch") and positions:
        positions[0]["executionMode"] = "demo"
    if faults.get("packageMismatch") and positions:
        positions[0]["packageHash"] = "sha256:broker-divergent"
    if faults.get("stalePrices") and positions:
        positions[0]["entry"] = round((positions[0].get("entry") or 0) - 0.0025, 5)
        positions[0]["_stale"] = True
        notes.append(f"position {positions[0].get('id')} prices stale")
    if faults.get("duplicateOrder"):
        dup = {"id": "ord_dup", "canonicalSymbol": "EURUSD", "brokerSymbol": "EURUSD",
               "side": "long", "size": 0.2, "state": "pending", "deploymentId": None}
        orders.append(dict(dup))
        orders.append(dict(dup))

    conn = "Degraded" if faults.get("connectionDegraded") else connection
    return positions, orders, conn, notes


def build_broker_snapshot(base_positions: list, base_orders: list, accounts: list,
                          connection: str, capabilities: dict, faults: dict, now: str) -> dict:
    positions, orders, conn, notes = _apply_faults(base_positions, base_orders, connection, faults)
    return {
        "positions": positions,
        "orders": orders,
        "accounts": accounts,
        "connection": conn,
        "capabilities": capabilities,
        "timestamp": now,
        "faultNotes": notes,
        "counts": {"positions": len(positions), "orders": len(orders), "accounts": len(accounts)},
    }


# ---------------------------------------------------------------------------
# Reconciliation engine (Objective 3) — structured results only, no repair.
# ---------------------------------------------------------------------------

def _index(items: list) -> dict[str, list]:
    out: dict[str, list] = {}
    for it in items:
        out.setdefault(it.get("id"), []).append(it)
    return out


def _cmp_entity(kind: str, eid: str, b: dict, r: dict) -> list[Finding]:
    findings: list[Finding] = []

    def mismatch(field: str, ftype: str):
        bv, rv = b.get(field), r.get(field)
        if bv != rv:
            findings.append(Finding(ftype, Severity.WARNING, eid,
                                    f"{kind} {field} differs (broker={bv} runtime={rv})", bv, rv))

    mismatch("state", STATE_MISMATCH)
    mismatch("size", SIZE_MISMATCH)
    if kind == "position":
        mismatch("sl", SL_MISMATCH)
        mismatch("tp", TP_MISMATCH)
        mismatch("executionMode", EXECUTION_MODE_MISMATCH)
        mismatch("packageHash", PACKAGE_MISMATCH)
    mismatch("canonicalSymbol", SYMBOL_MISMATCH)
    if b.get("_stale"):
        findings.append(Finding(STALE_PRICE, Severity.WARNING, eid,
                                f"{kind} {eid} reporting stale prices", b.get("entry"), r.get("entry")))
    return findings


def reconcile(broker_snap: dict, runtime_snap: dict) -> dict:
    findings: list[Finding] = []

    for kind, missing_t, unexpected_t in (
        ("position", MISSING_POSITION, UNEXPECTED_POSITION),
        ("order", MISSING_ORDER, UNEXPECTED_ORDER),
    ):
        b_items = broker_snap["positions"] if kind == "position" else broker_snap["orders"]
        r_items = runtime_snap["positions"] if kind == "position" else runtime_snap["orders"]
        b_idx, r_idx = _index(b_items), _index(r_items)

        for eid, group in b_idx.items():
            if len(group) > 1:
                findings.append(Finding(DUPLICATE_ENTITY, Severity.ERROR, eid,
                                        f"broker reports {len(group)} {kind}s with id {eid}", len(group), 1))
        for eid in r_idx:
            if eid not in b_idx:
                findings.append(Finding(missing_t, Severity.ERROR, eid,
                                        f"{kind} {eid} in runtime but not reported by broker"))
            else:
                findings.extend(_cmp_entity(kind, eid, b_idx[eid][0], r_idx[eid][0]))
        for eid in b_idx:
            if eid not in r_idx:
                findings.append(Finding(unexpected_t, Severity.ERROR, eid,
                                        f"{kind} {eid} reported by broker but not in runtime"))

    if broker_snap.get("connection") != "Connected":
        findings.append(Finding(CONNECTION_DEGRADED, Severity.WARNING, broker_snap.get("connection", "?"),
                                f"broker connection is {broker_snap.get('connection')}", broker_snap.get("connection"), "Connected"))

    errors = sum(1 for f in findings if f.severity == Severity.ERROR)
    warnings = sum(1 for f in findings if f.severity == Severity.WARNING)
    status = Severity.ERROR if errors else (Severity.WARNING if warnings else Severity.OK)
    return {
        "findings": [asdict(f) for f in findings],
        "summary": {
            "status": status,
            "healthy": status == Severity.OK,
            "warnings": warnings,
            "errors": errors,
            "brokerPositions": broker_snap["counts"]["positions"],
            "runtimePositions": runtime_snap["counts"]["positions"],
            "brokerOrders": broker_snap["counts"]["orders"],
            "runtimeOrders": runtime_snap["counts"]["orders"],
        },
    }


# ---------------------------------------------------------------------------
# Sync orchestration
# ---------------------------------------------------------------------------

def run_sync(*, broker_positions: list, broker_orders: list, accounts: list, connection: str,
             capabilities: dict, views: RuntimeViews, faults: dict | None = None) -> dict:
    """Run one reconciliation cycle. Returns a structured result; caller appends
    the BotEvent and caches. Never mutates runtime."""
    faults = faults if faults is not None else get_faults()
    t0 = time.perf_counter()
    runtime_snap = build_runtime_snapshot(views)
    broker_snap = build_broker_snapshot(broker_positions, broker_orders, accounts, connection, capabilities, faults, views.now)
    recon = reconcile(broker_snap, runtime_snap)
    duration_ms = round((time.perf_counter() - t0) * 1000, 3)
    return {
        "brokerSnapshot": broker_snap,
        "runtimeSnapshot": runtime_snap,
        "reconciliation": recon,
        "status": recon["summary"]["status"],
        "durationMs": duration_ms,
        "at": views.now,
        "eventSeq": views.event_seq,
        "faults": {k: v for k, v in faults.items() if v},
    }
