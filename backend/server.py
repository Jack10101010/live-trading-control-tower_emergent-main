"""
Control Tower FastAPI stub.

Presentation-first: the frontend consumes `world.v1.json` via a fixture provider
today. This backend exposes route shapes that match the future data-repository
contract so hooks can flip from fixture → API without component churn.
"""
from fastapi import FastAPI, APIRouter, HTTPException, Request
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import asyncio
import os
import json
import sqlite3
import threading
import uuid
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import contextvars
import broker as broker_layer
import broker_sync as sync_layer
import execution as execution_layer
import strategy as strategy_layer
import scheduler as scheduler_layer
import market_data as market_data_layer
import data_service as data_service_layer
import risk_engine as risk_layer
import portfolio as portfolio_layer


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

logger = logging.getLogger(__name__)

# MongoDB connection is OPTIONAL — local dev runs fixture-only with no database.
# Only connect when MONGO_URL is set; never crash the server when it is absent.
client = None
db = None
mongo_url = os.environ.get('MONGO_URL')
if mongo_url:
    try:
        from motor.motor_asyncio import AsyncIOMotorClient
        client = AsyncIOMotorClient(mongo_url)
        db = client[os.environ.get('DB_NAME', 'control_tower')]
        logger.info("MongoDB connected.")
    except Exception as exc:  # pragma: no cover - depends on local env
        logger.warning("MongoDB unavailable (%s); running fixture-only.", exc)
else:
    logger.info("No MONGO_URL set; running fixture-only (no database).")

app = FastAPI(title="Control Tower API", version="0.1.0")
api_router = APIRouter(prefix="/api")


def _load_world() -> dict:
    """Load the frozen world.v1.json fixture from the repo.

    Later this becomes: `await db.world.find_one(...)` — but the caller shape
    (typed hooks) stays identical.
    """
    fixture_paths = [
        ROOT_DIR / 'fixtures' / 'world.v1.json',
        Path('/app/frontend/src/data/world.v1.json'),
        Path('/app/_fixtures/world.v1.json'),
    ]
    for p in fixture_paths:
        if p.exists():
            with open(p, 'r') as f:
                return json.load(f)
    raise FileNotFoundError("world.v1.json fixture not found")


WORLD = _load_world()


# ---------------------------------------------------------------------------
# Event store (Phase 4, Step 1): append-only BotEvent log backing the
# command → event → audit loop. SQLite via stdlib — no new dependency, file
# created lazily. Fixture events stay frozen in world.v1.json; operator
# actions append here and GET /api/events returns the merged stream.
# ---------------------------------------------------------------------------

EVENTS_DB_PATH = ROOT_DIR / 'events.db'
_events_lock = threading.Lock()

# Mirror of the frontend Command union (frontend/src/lib/commands.ts, Track B §5).
# Adding a command means adding it in BOTH places.
KNOWN_COMMANDS = {
    # Policy lifecycle
    "CreateDraft", "DiscardDraft", "PromoteDraft", "RunNativeValidation",
    "ApproveRecommendation", "RejectRecommendation", "ApplyOverride", "RemoveOverride",
    # Package / deployment
    "DeployPackage", "RollbackPackage", "PauseDeployment", "ResumeDeployment",
    "KillDeployment", "FlattenDeployment", "SetLaneMode",
    "LockDeployment", "UnlockDeployment",
    # Order management
    "CancelOrder", "ReduceOrderRisk", "ConvertOrderToGhost",
    # Trade management
    "CloseTrade", "SLToBE", "MoveTradeSL", "MoveTradeTP", "PartialClose",
    "ReduceTradeRisk", "SetAutoManagement",
    # Deployment Manifest
    "CloneManifest", "ExportManifest", "RestoreManifest", "RedeployManifest",
    # Safety
    "GlobalKill", "PausePair",
}

# Track B event categories: decision·order·trade·policy·risk·manual·system·error·broker
_COMMAND_CATEGORY = {
    "CreateDraft": "policy", "DiscardDraft": "policy", "PromoteDraft": "policy",
    "RunNativeValidation": "policy", "ApproveRecommendation": "policy",
    "RejectRecommendation": "policy", "ApplyOverride": "policy", "RemoveOverride": "policy",
    "DeployPackage": "policy", "RollbackPackage": "policy",
    "PauseDeployment": "system", "ResumeDeployment": "system", "KillDeployment": "system",
    "FlattenDeployment": "system", "SetLaneMode": "system",
    "LockDeployment": "system", "UnlockDeployment": "system",
    "CancelOrder": "order", "ReduceOrderRisk": "order", "ConvertOrderToGhost": "order",
    "CloseTrade": "trade", "SLToBE": "trade", "MoveTradeSL": "trade", "MoveTradeTP": "trade",
    "PartialClose": "trade", "ReduceTradeRisk": "trade", "SetAutoManagement": "trade",
    "CloneManifest": "system", "ExportManifest": "system", "RestoreManifest": "system",
    "RedeployManifest": "system",
    "GlobalKill": "risk", "PausePair": "risk",
}

_FIXTURE_MAX_SEQ = max((e.get("seq", 0) for e in WORLD.get("events", [])), default=0)


def _events_db() -> sqlite3.Connection:
    conn = sqlite3.connect(EVENTS_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_events (
            seq INTEGER PRIMARY KEY,
            event_id TEXT NOT NULL UNIQUE,
            idempotency_key TEXT UNIQUE,
            payload TEXT NOT NULL
        )
        """
    )
    return conn


def _stored_events() -> list[dict]:
    if not EVENTS_DB_PATH.exists():
        return []
    with _events_lock:
        conn = _events_db()
        try:
            rows = conn.execute("SELECT payload FROM bot_events ORDER BY seq ASC").fetchall()
        finally:
            conn.close()
    return [json.loads(r[0]) for r in rows]


def _all_events() -> list[dict]:
    """Merged, seq-ordered audit stream: frozen fixture events + appended ones."""
    evs = list(WORLD.get("events", [])) + _stored_events()
    evs.sort(key=lambda e: e.get("seq", 0))
    return evs


def _max_seq() -> int:
    """Current head seq across fixture + appended events."""
    stored = 0
    if EVENTS_DB_PATH.exists():
        with _events_lock:
            conn = _events_db()
            try:
                stored = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM bot_events").fetchone()[0]
            finally:
                conn.close()
    return max(_FIXTURE_MAX_SEQ, stored)


def _events_since(since: int, pair: str | None = None) -> list[dict]:
    """Events with seq strictly greater than `since` (the delta), oldest first."""
    evs = [e for e in _all_events() if e.get("seq", 0) > since]
    if pair:
        evs = [e for e in evs if e.get("scenarioKey") is None or e.get("scenarioKey", "").startswith(f"{pair}:")]
    return evs


def _find_event_by_idempotency(idempotency_key: str) -> dict | None:
    """Return the event previously stored under this idempotency key, if any.
    Used to short-circuit a retried command BEFORE its runtime effect re-applies."""
    if not idempotency_key or not EVENTS_DB_PATH.exists():
        return None
    with _events_lock:
        conn = _events_db()
        try:
            row = conn.execute(
                "SELECT payload FROM bot_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
    return json.loads(row[0]) if row else None


def _append_event(event: dict, idempotency_key: str | None) -> tuple[dict, bool]:
    """Append a BotEvent, assigning the next monotonic seq. Returns
    (event, deduplicated). A replayed idempotency key returns the original
    event untouched — the log is append-only and retry-safe (Track B §4.18).
    """
    with _events_lock:
        conn = _events_db()
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT payload FROM bot_events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return json.loads(row[0]), True
            max_stored = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM bot_events").fetchone()[0]
            event["seq"] = max(_FIXTURE_MAX_SEQ, max_stored) + 1
            conn.execute(
                "INSERT INTO bot_events (seq, event_id, idempotency_key, payload) VALUES (?, ?, ?, ?)",
                (event["seq"], event["eventId"], idempotency_key, json.dumps(event)),
            )
            conn.commit()
        finally:
            conn.close()
    return event, False


def _operator_id() -> str:
    ops = WORLD.get("operators", [])
    if ops and isinstance(ops[0], dict) and ops[0].get("operatorId"):
        return str(ops[0]["operatorId"])
    return "system"


def _active_package_hash() -> str:
    pkg = _active_package_view()
    if pkg:
        return pkg.get("packageHash", "")
    return ""


def _snake_upper(name: str) -> str:
    out = []
    for i, ch in enumerate(name):
        if ch.isupper() and i > 0 and (not name[i - 1].isupper() or (i + 1 < len(name) and name[i + 1].islower())):
            out.append("_")
        out.append(ch.upper())
    return "".join(out)


# ---------------------------------------------------------------------------
# Runtime State overlay (Phase 4, Step 3): mutable operator-driven state that
# LAYERS OVER the immutable Fixture World. The fixture (world.v1.json) is the
# authoritative bootstrap dataset and is NEVER mutated; commands write overlays
# here, and read endpoints merge fixture entities with their overlay on the way
# out. Persisted in SQLite so runtime state survives a backend restart.
#
# Overlay keys prefixed with "_" are internal bookkeeping (e.g. the pre-pause
# status remembered for Resume) and are stripped before the entity is returned.
# ---------------------------------------------------------------------------

RUNTIME_DB_PATH = ROOT_DIR / 'runtime.db'
_runtime_lock = threading.Lock()


def _runtime_db() -> sqlite3.Connection:
    conn = sqlite3.connect(RUNTIME_DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS runtime_overlay (
            kind TEXT NOT NULL,
            entity_id TEXT NOT NULL,
            overlay TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (kind, entity_id)
        )
        """
    )
    return conn


def _load_overlays(kind: str) -> dict[str, dict]:
    """All overlays for a kind, keyed by entity id. Empty before first write."""
    if not RUNTIME_DB_PATH.exists():
        return {}
    with _runtime_lock:
        conn = _runtime_db()
        try:
            rows = conn.execute(
                "SELECT entity_id, overlay FROM runtime_overlay WHERE kind = ?", (kind,)
            ).fetchall()
        finally:
            conn.close()
    return {eid: json.loads(ov) for eid, ov in rows}


def _get_overlay(kind: str, entity_id: str) -> dict:
    if not RUNTIME_DB_PATH.exists():
        return {}
    with _runtime_lock:
        conn = _runtime_db()
        try:
            row = conn.execute(
                "SELECT overlay FROM runtime_overlay WHERE kind = ? AND entity_id = ?",
                (kind, entity_id),
            ).fetchone()
        finally:
            conn.close()
    return json.loads(row[0]) if row else {}


# Dry-run guard (Phase 8): when an execution is a dry-run the effect code runs
# and computes before/after exactly as MockBroker would, but the overlay WRITE is
# skipped so nothing persists. Per-async-task via contextvar → concurrency-safe.
# This is a behavioural flag on the write, not a change to overlay ownership.
_DRY_RUN = contextvars.ContextVar("_dry_run", default=False)


def _put_overlay(kind: str, entity_id: str, overlay: dict) -> None:
    if _DRY_RUN.get():
        return
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    with _runtime_lock:
        conn = _runtime_db()
        try:
            conn.execute(
                "INSERT INTO runtime_overlay (kind, entity_id, overlay, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(kind, entity_id) DO UPDATE SET overlay = excluded.overlay, updated_at = excluded.updated_at",
                (kind, entity_id, json.dumps(overlay), now),
            )
            conn.commit()
        finally:
            conn.close()


def _visible(overlay: dict) -> dict:
    """Strip internal (`_`-prefixed) bookkeeping keys before overlaying an entity."""
    return {k: v for k, v in overlay.items() if not k.startswith("_")}


def _apply_deployment_overlay(dep: dict, overlay: dict | None) -> dict:
    if not overlay:
        return dep
    return {**dep, **_visible(overlay)}


def _apply_trade_overlay(trade: dict, overlay: dict | None) -> dict:
    if not overlay:
        return trade
    scalars = _visible(overlay)
    appends = overlay.get("_managementAppend") or []
    merged = {**trade, **scalars}
    if appends:
        merged["management"] = list(trade.get("management") or []) + appends
    return merged


# Trade states that represent live broker exposure (count toward openTrades),
# a resting order (openOrders), or neither (closed/cancelled/ghost).
_OPEN_TRADE_STATES = {"managing", "InTrade", "open", "triggered", "filled"}
_ORDER_STATES = {"pending"}


def _deployment_aggregates(dep_id: str, trades: list[dict]) -> dict:
    """Recompute a deployment's live aggregates from the CURRENT runtime trade
    view, so counts never go stale after a close/cancel/reduce."""
    dep_trades = [t for t in trades if t.get("deploymentId") == dep_id]
    open_trades = [t for t in dep_trades if t.get("state") in _OPEN_TRADE_STATES]
    pending = [t for t in dep_trades if t.get("state") in _ORDER_STATES]
    floating = round(sum((t.get("floatingPl") or 0) for t in open_trades), 2)
    open_risk = round(sum(abs(t.get("riskPct") or 0) for t in open_trades), 2)
    exposure = round(sum(abs(t.get("size") or 0) for t in open_trades), 4)
    return {
        "openTrades": len(open_trades),
        "openOrders": len(pending),
        "floatingPl": floating,
        "openRiskPct": open_risk,
        "exposure": exposure,
    }


def _apply_aggregates(dep: dict, trades: list[dict]) -> dict:
    """Overlay recomputed runtime aggregates onto a deployment's riskState.
    Overrides the fixture's openTrades/openOrders/floatingPl (which would
    otherwise be stale) and adds openRiskPct/exposure; leaves dailyPl and the
    drawdown/risk-budget fields untouched."""
    agg = _deployment_aggregates(dep.get("deploymentId", ""), trades)
    risk = {**(dep.get("riskState") or {}), **agg}
    return {**dep, "riskState": risk}


def _deployments_view() -> list[dict]:
    overlays = _load_overlays("deployment")
    trades = _live_trades_view()
    out = []
    for d in WORLD.get("deployments", []):
        merged = _apply_deployment_overlay(d, overlays.get(d.get("deploymentId")))
        out.append(_apply_aggregates(merged, trades))
    return out


def _live_trades_view() -> list[dict]:
    overlays = _load_overlays("trade")
    return [_apply_trade_overlay(t, overlays.get(t.get("tradeId"))) for t in WORLD.get("liveTrades", [])]


def _deployment_current(dep_id: str) -> dict | None:
    for d in WORLD.get("deployments", []):
        if d.get("deploymentId") == dep_id:
            return _apply_deployment_overlay(d, _get_overlay("deployment", dep_id))
    return None


def _trade_current(trade_id: str) -> dict | None:
    for t in WORLD.get("liveTrades", []):
        if t.get("tradeId") == trade_id:
            return _apply_trade_overlay(t, _get_overlay("trade", trade_id))
    return None


def _set_deployment_status(dep_id: str, status: str, now: str, remember_prev: bool = False,
                           extra: dict | None = None) -> None:
    current = _deployment_current(dep_id)
    overlay = _get_overlay("deployment", dep_id)
    # Runtime metadata: stamp when the status changed and when a command last touched it.
    patch: dict = {"status": status, "statusChangedAt": now, "lastCommandAt": now}
    if remember_prev and current and "_prevStatus" not in overlay:
        patch["_prevStatus"] = current.get("status")
    if extra:
        patch.update(extra)
    _put_overlay("deployment", dep_id, {**overlay, **patch})


# ---------------------------------------------------------------------------
# Phase 4, Step 4 — finish runtime ownership: orders, active-package selection,
# deployment runtime metadata, and a runtime-health model. Same overlay
# mechanism (fixture ⊕ overlay); the fixture is never mutated.
# ---------------------------------------------------------------------------

def _package_by_version(version) -> dict | None:
    for p in WORLD.get("packages", []):
        if p.get("version") == version:
            return p
    return None


def _fixture_active_package() -> dict | None:
    active = next((p for p in WORLD.get("packages", []) if p.get("status") == "active"), None)
    return active or (WORLD.get("packages") or [None])[0]


def _active_package_runtime() -> dict:
    """Runtime active-package record: current/previous version + activation meta.
    Defaults from the fixture (the promoted active package) when unset."""
    ov = _get_overlay("meta", "active_package")
    if ov.get("current") is not None:
        return ov
    active = _fixture_active_package() or {}
    return {
        "current": active.get("version"),
        "previous": active.get("parentVersion"),
        "activatedAt": active.get("promotedAt"),
        "source": "fixture",
    }


def _active_package_view() -> dict | None:
    """The package currently active at runtime (runtime selection ⊕ fixture default)."""
    rt = _active_package_runtime()
    return _package_by_version(rt.get("current")) or _fixture_active_package()


def _set_active_package(version, now: str, source: str) -> tuple[dict, dict]:
    rt = _active_package_runtime()
    old = rt.get("current")
    _put_overlay("meta", "active_package",
                 {"current": version, "previous": old, "activatedAt": now, "source": source})
    return {"activeVersion": old}, {"activeVersion": version, "source": source}


def _trade_by_order_id(order_id: str) -> dict | None:
    """Resolve an order id (brokerOrderId or tradeId) to its current (overlaid)
    trade record. Orders are pending trades in this model (Track B: an order is
    a resting trade)."""
    for t in WORLD.get("liveTrades", []):
        if t.get("brokerOrderId") == order_id or t.get("tradeId") == order_id:
            return _apply_trade_overlay(t, _get_overlay("trade", t["tradeId"]))
    return None


def _overlay_count() -> int:
    return sum(len(_load_overlays(k)) for k in ("deployment", "trade", "order", "meta"))


def _event_count() -> int:
    return len(WORLD.get("events", [])) + len(_stored_events())


def _runtime_health() -> dict:
    """Runtime-layer health (NOT broker health): overlay/event-store/runtime-DB
    liveness, and which runtime capabilities are enabled."""
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    runtime_ok, overlay_count = True, 0
    try:
        overlay_count = _overlay_count()
    except Exception:  # pragma: no cover - defensive
        runtime_ok = False
    events_ok, event_count = True, 0
    try:
        event_count = _event_count()
    except Exception:  # pragma: no cover - defensive
        events_ok = False
    brk = broker_layer.get_broker()
    return {
        "overlayLoaded": True,
        "runtimeDbHealthy": runtime_ok,
        "eventStoreHealthy": events_ok,
        "commandsEnabled": True,
        "persistenceEnabled": True,
        "replayStateAvailable": True,
        "overlayCount": overlay_count,
        "eventCount": event_count,
        "checkedAt": now,
        # Broker abstraction (Phase 6) — surfaced in the existing runtime-health view.
        "broker": {
            "kind": brk.kind,
            "brokerId": brk.broker_id,
            "connection": brk.connection().state,
            "capabilities": broker_layer.capability_dict(brk.capabilities()),
        },
        # Broker sync/reconciliation status (Phase 7).
        "sync": _sync_health(),
        # Execution orchestrator status (Phase 8).
        "orchestrator": _EXEC_METRICS.health(_DRY_RUN_MODE),
        # Strategy engine status (Phase 9).
        "strategy": _STRATEGY_METRICS.health(),
        # Scheduler status (Phase 10).
        "scheduler": _SCHEDULER.health(),
        # Market Data Engine status (Phase 11).
        "marketData": _MARKET_DATA_ENGINE.health(),
        # Risk Engine status (Phase 12).
        "risk": _RISK_METRICS.health(),
        # Portfolio Engine status (Phase 14).
        "portfolio": _PORTFOLIO_METRICS.health(),
    }


# ---------------------------------------------------------------------------
# Broker synchronisation & reconciliation (Phase 7) — READ-ONLY. Polls the broker
# via the Phase-6 interface, compares to the runtime snapshot, produces structured
# reconciliation results, appends one BotEvent per completed sync. Never repairs.
# ---------------------------------------------------------------------------

_SYNC_CACHE: dict | None = None
_SYNC_SEQ = 0
_LAST_SUCCESSFUL_SYNC_AT: str | None = None
_LAST_SYNC_SIGNATURE: tuple | None = None
_SYNC_CODE_BY_STATUS = {"ok": "BROKER_SYNC_OK", "warning": "BROKER_SYNC_WARNING", "error": "BROKER_SYNC_ERROR"}


def _sync_signature(result: dict) -> tuple:
    """A stable fingerprint of a reconciliation outcome (status + the set of
    findings). Used to append a BotEvent only when the outcome CHANGES, so a
    steady healthy poll doesn't flood the audit stream while transitions and
    every distinct problem are still recorded."""
    findings = result["reconciliation"]["findings"]
    return (result["status"], tuple(sorted((f["type"], f["entityId"]) for f in findings)))


def _normalized_broker_views():
    """Poll the active broker and normalise positions/orders to the reconciler's
    shape (enriched with each deployment's executionMode/packageHash)."""
    brk = broker_layer.get_broker()
    ctx = _broker_context({}, _now_iso())
    deps = {d.get("deploymentId"): d for d in _deployments_view()}

    def npos(p):
        dep = deps.get(p.get("deploymentId")) or {}
        return {"id": p.get("positionId"), "canonicalSymbol": p.get("canonicalSymbol"),
                "brokerSymbol": p.get("brokerSymbol"), "side": p.get("side"), "size": p.get("size"),
                "entry": p.get("entry"), "sl": p.get("sl"), "tp": p.get("tp"), "state": p.get("state"),
                "deploymentId": p.get("deploymentId"), "executionMode": dep.get("executionMode"),
                "packageHash": dep.get("packageHash")}

    def nord(o):
        return {"id": o.get("orderId"), "canonicalSymbol": o.get("canonicalSymbol"),
                "brokerSymbol": o.get("brokerSymbol"), "side": o.get("side"), "size": o.get("size"),
                "state": o.get("state"), "deploymentId": o.get("deploymentId")}

    return (
        [npos(p) for p in brk.positions(ctx)],
        [nord(o) for o in brk.orders(ctx)],
        brk.accounts(ctx),
        brk.connection().state,
        broker_layer.capability_dict(brk.capabilities()),
    )


def _run_broker_sync(append_event: bool = True) -> dict:
    global _SYNC_CACHE, _SYNC_SEQ, _LAST_SUCCESSFUL_SYNC_AT, _LAST_SYNC_SIGNATURE
    now = _now_iso()
    positions, orders, accounts, connection, capabilities = _normalized_broker_views()
    views = sync_layer.RuntimeViews(
        deployments=_deployments_view(), positions=positions, orders=orders, accounts=accounts,
        active_package=_active_package_runtime(),
        runtime_health={
            "overlayLoaded": True, "runtimeDbHealthy": True, "eventStoreHealthy": True,
            "overlayCount": _overlay_count(), "eventCount": _event_count(),
        },
        connection_state=connection, event_seq=_max_seq(), now=now,
    )
    result = sync_layer.run_sync(broker_positions=positions, broker_orders=orders, accounts=accounts,
                                 connection=connection, capabilities=capabilities, views=views)
    _SYNC_SEQ += 1
    result["syncSequence"] = _SYNC_SEQ
    status = result["status"]

    # Reduce audit noise: append a BotEvent only when the reconciliation outcome
    # changes (transition or a new/cleared finding), never on a steady healthy
    # poll. Reconciliation still runs every cycle; the cache + Runtime Health
    # sync fields update every cycle regardless.
    signature = _sync_signature(result)
    result["eventAppended"] = False
    if append_event and signature != _LAST_SYNC_SIGNATURE:
        summary = result["reconciliation"]["summary"]
        code = _SYNC_CODE_BY_STATUS.get(status, "BROKER_SYNC_OK")
        explanation = (f"Broker sync {status}: {summary['warnings']} warning(s), {summary['errors']} error(s), "
                       f"{summary['brokerPositions']} position(s), {summary['brokerOrders']} order(s) "
                       f"({result['durationMs']}ms).")
        event = {"eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0, "category": "broker",
                 "code": code, "humanExplanation": explanation, "scenarioKey": None,
                 "packageHash": _active_package_hash(), "who": "system", "causedBy": f"sync_{_SYNC_SEQ}",
                 "before": None, "after": {"durationMs": result["durationMs"], **summary}, "at": now}
        ev, _ = _append_event(event, None)
        _LAST_SYNC_SIGNATURE = signature
        result["eventAppended"] = True
        result["eventCode"] = code
        result["eventSeqAppended"] = ev["seq"]
    if status == "ok":
        _LAST_SUCCESSFUL_SYNC_AT = now
    _SYNC_CACHE = result
    return result


def _sync_health() -> dict:
    return {
        "lastSync": _SYNC_CACHE["at"] if _SYNC_CACHE else None,
        "lastSuccessfulSync": _LAST_SUCCESSFUL_SYNC_AT,
        "syncDuration": _SYNC_CACHE["durationMs"] if _SYNC_CACHE else None,
        "syncStatus": _SYNC_CACHE["status"] if _SYNC_CACHE else None,
        "syncSequence": _SYNC_SEQ,
    }


def _append_trade_management(trade_id: str, entry: dict, scalars: dict | None = None) -> None:
    overlay = _get_overlay("trade", trade_id)
    mgmt = list(overlay.get("_managementAppend") or [])
    mgmt.append(entry)
    new_overlay = {**overlay, "_managementAppend": mgmt}
    if scalars:
        new_overlay.update(scalars)
    _put_overlay("trade", trade_id, new_overlay)


def _mgmt_entry(kind: str, now: str, before: dict | None, after: dict | None, reason: str | None) -> dict:
    return {"at": now, "type": kind, "before": before, "after": after,
            "actor": _operator_id(), "reason": reason or "operator command"}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _broker_context(payload: dict, now: str) -> broker_layer.BrokerContext:
    """Build the injection context the active broker executes against. The runtime
    hands the broker only these primitives — the broker never reaches into server
    internals, and no broker-specific structure leaks back into the runtime."""
    return broker_layer.BrokerContext(
        now=now,
        reason=payload.get("reason"),
        payload=payload,
        operator_id=_operator_id(),
        trade_current=_trade_current,
        trade_by_order_id=_trade_by_order_id,
        append_trade_management=_append_trade_management,
        mgmt_entry=_mgmt_entry,
        close_trade=_close_trade,
        snake_upper=_snake_upper,
        live_trades=_live_trades_view,
        accounts=lambda: WORLD.get("accounts", []),
        brokers=lambda: WORLD.get("brokers", []),
    )


def _apply_command_effects(name: str, payload: dict, now: str) -> tuple[dict | None, dict | None]:
    """Apply a command. Trade/order EXECUTION is delegated to the active Broker
    (the single execution boundary); deployment/package lifecycle stays in the
    runtime control-plane. Returns (before, after) for the audit event."""
    reason = payload.get("reason")

    # Execution boundary: all trade/order operations go through the Broker interface
    # (MockBroker today — identical behaviour, zero live trading).
    if name in broker_layer.BROKER_COMMANDS:
        return broker_layer.get_broker().submit_command(name, _broker_context(payload, now))

    # --- Deployment lifecycle ---
    if name == "PauseDeployment":
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        before = {"status": cur.get("status")}
        _set_deployment_status(dep_id, "Paused", now, remember_prev=True)
        return before, {"status": "Paused"}

    if name == "ResumeDeployment":
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        overlay = _get_overlay("deployment", dep_id)
        prev = overlay.get("_prevStatus") or "Armed"
        before = {"status": cur.get("status")}
        new_overlay = {k: v for k, v in overlay.items() if k != "_prevStatus"}
        new_overlay["status"] = prev
        _put_overlay("deployment", dep_id, new_overlay)
        return before, {"status": prev}

    if name == "LockDeployment":
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        before = {"status": cur.get("status")}
        extra = {"lastAction": "Deployment locked by operator"}
        notes = payload.get("notes")
        if notes:
            extra["notes"] = notes
        _set_deployment_status(dep_id, "Locked", now, remember_prev=True, extra=extra)
        return before, {"status": "Locked"}

    if name == "UnlockDeployment":
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        overlay = _get_overlay("deployment", dep_id)
        prev = overlay.get("_prevStatus") or "Armed"
        before = {"status": cur.get("status")}
        new_overlay = {k: v for k, v in overlay.items() if k != "_prevStatus"}
        new_overlay.update({"status": prev, "statusChangedAt": now, "lastCommandAt": now,
                            "lastAction": "Deployment unlocked by operator"})
        _put_overlay("deployment", dep_id, new_overlay)
        return before, {"status": prev}

    if name == "RedeployManifest":
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        before = {"status": cur.get("status")}
        _set_deployment_status(dep_id, "Armed", now, extra={"lastAction": "Redeployed from manifest"})
        return before, {"status": "Armed", "lastAction": "Redeployed from manifest"}

    if name == "DeployPackage":
        dep_id = payload.get("deploymentId")
        version = payload.get("packageVersion") or _active_package_runtime().get("current")
        pkg = _package_by_version(version)
        pkg_before, pkg_after = _set_active_package(version, now, "deploy")
        if dep_id and _deployment_current(dep_id) and pkg:
            overlay = _get_overlay("deployment", dep_id)
            overlay.update({"packageHash": pkg.get("packageHash"), "lastCommandAt": now,
                            "lastAction": f"Deployed package v{version}"})
            _put_overlay("deployment", dep_id, overlay)
        return pkg_before, pkg_after

    if name == "RollbackPackage":
        dep_id = payload.get("deploymentId")
        version = payload.get("toVersion")
        if version is None:
            version = _active_package_runtime().get("previous")
        pkg = _package_by_version(version)
        pkg_before, pkg_after = _set_active_package(version, now, "rollback")
        if dep_id and _deployment_current(dep_id) and pkg:
            overlay = _get_overlay("deployment", dep_id)
            overlay.update({"packageHash": pkg.get("packageHash"), "lastCommandAt": now,
                            "lastAction": f"Rolled back to package v{version}"})
            _put_overlay("deployment", dep_id, overlay)
        return pkg_before, pkg_after

    if name in ("KillDeployment", "FlattenDeployment"):
        dep_id = payload.get("deploymentId")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur:
            return None, None
        before = {"status": cur.get("status")}
        action = "All working orders cancelled (kill)" if name == "KillDeployment" else "All open positions flattened"
        _set_deployment_status(dep_id, "Locked", now, extra={"lastAction": action})
        if name == "FlattenDeployment":
            # Closing positions is execution → go through the Broker interface.
            broker_layer.get_broker().flatten(dep_id, _broker_context(payload, now))
        return before, {"status": "Locked", "lastAction": action}

    if name == "SetLaneMode":
        dep_id = payload.get("deploymentId")
        mode = payload.get("executionMode")
        cur = _deployment_current(dep_id) if dep_id else None
        if not cur or not mode:
            return None, None
        before = {"executionMode": cur.get("executionMode")}
        overlay = _get_overlay("deployment", dep_id)
        _put_overlay("deployment", dep_id, {**overlay, "executionMode": mode})
        return before, {"executionMode": mode}

    if name == "PausePair":
        pair = payload.get("pair")
        if not pair:
            return None, None
        touched = []
        for d in WORLD.get("deployments", []):
            if d.get("pair") == pair:
                _set_deployment_status(d["deploymentId"], "Paused", now, remember_prev=True)
                touched.append(d["deploymentId"])
        return {"pair": pair}, {"status": "Paused", "deployments": touched}

    if name == "GlobalKill":
        touched = []
        for d in WORLD.get("deployments", []):
            _set_deployment_status(d["deploymentId"], "Locked", now, extra={"liveEnabled": False})
            touched.append(d["deploymentId"])
        return {"scope": "all"}, {"status": "Locked", "liveEnabled": False, "deployments": touched}

    # Trade/order execution is handled above via the Broker interface.
    return None, None


# ---------------------------------------------------------------------------
# Execution Orchestrator wiring (Phase 8). The orchestrator sits between the
# command endpoint and the dispatch (broker / runtime control-plane). It owns
# validation + policy + dry-run + timeline; it holds ZERO broker logic.
# ---------------------------------------------------------------------------

_DRY_RUN_MODE = False  # global operator toggle (per-command payload.dryRun also honoured)


def _dispatch_command(name: str, payload: dict, now: str, dry_run: bool) -> tuple:
    """Run the command's effect (routes to broker or runtime control-plane inside
    `_apply_command_effects`). Under dry-run the effect computes before/after but
    the overlay write is skipped (via the `_DRY_RUN` guard)."""
    token = _DRY_RUN.set(bool(dry_run))
    try:
        return _apply_command_effects(name, payload, now)
    finally:
        _DRY_RUN.reset(token)


def _execution_env() -> execution_layer.ExecutionEnv:
    brk = broker_layer.get_broker()
    return execution_layer.ExecutionEnv(
        deployment=_deployment_current,
        trade=_trade_current,
        order=_trade_by_order_id,
        active_package_version=lambda: _active_package_runtime().get("current"),
        broker_connection=lambda: brk.connection().state,
        broker_capabilities=lambda: broker_layer.capability_dict(brk.capabilities()),
    )


_EXEC_METRICS = execution_layer.ExecutionMetrics()
_ORCHESTRATOR = execution_layer.ExecutionOrchestrator(
    dispatch=_dispatch_command, env_factory=_execution_env, metrics=_EXEC_METRICS,
)


# ---------------------------------------------------------------------------
# Market Data Engine wiring (Phase 11). The SINGLE owner of market snapshots.
# Produces immutable MarketSnapshots; executes nothing. Providers receive
# read-only fixture callables (no server.py import). OHLC is DERIVED from the
# fixture regime components — no duplication of the frontend candle pipeline.
# ---------------------------------------------------------------------------

def _fixture_regime(pair: str) -> dict | None:
    return next((m for m in WORLD.get("marketStateSnapshots", []) if m.get("instrument") == pair), None)


def _replay_session_for(pair: str) -> dict | None:
    return next((s for s in WORLD.get("replaySessions", [])
                 if s.get("scope", {}).get("pair") == pair), None)


# MT5 read-only feed (Phase 13). Symbol universe + aliases mirror the broker's
# MT5 adapter (single source of alias truth) but are INJECTED — the market-data
# layer imports nothing from broker.py and never talks to the Broker abstraction.
_MT5_SYMBOLS = ["EURUSD", "GBPUSD", "XAUUSD"]
_MT5_ALIASES = {"EURUSD": "EURUSD.r", "GBPUSD": "GBPUSD.r", "XAUUSD": "XAUUSD.a"}
# Provider selection is by CONFIGURATION only (env var); MT5 is disabled by default.
_MD_PROVIDER = (os.environ.get("MARKET_DATA_PROVIDER") or "fixture").strip().lower()
_MT5_MD_ENABLED = _MD_PROVIDER == "mt5" or (os.environ.get("MT5_MARKET_DATA_ENABLED") or "").strip() in ("1", "true", "yes")

# Market Data Service (Phase 24, Architecture V1.2 §4.2): provider adapters
# (Polygon when POLYGON_API_KEY is set, else the owned historical store — real
# Dukascopy M1 canonical, higher TFs aggregated) behind one facade. The engine's
# active provider consumes the service via an injected source; nothing else
# knows where candles come from.
_DATA_SERVICE = data_service_layer.DataService()


def _service_candles(symbol: str, timeframe: str, count: int, end_iso: str,
                     start_iso: str | None = None) -> list:
    to_s = data_service_layer._iso_to_unix
    return _DATA_SERVICE.candles(
        symbol, timeframe, count,
        start_s=to_s(start_iso) if start_iso else None,
        end_s=to_s(end_iso) if end_iso else None,
    )


_MARKET_DATA_ENGINE = market_data_layer.MarketDataEngine(now_fn=_now_iso)
_MARKET_DATA_ENGINE.register(
    market_data_layer.FixtureProvider(_fixture_regime, candle_source=_service_candles), active=True)
_MARKET_DATA_ENGINE.register(market_data_layer.ReplayProvider(
    _fixture_regime, _replay_session_for, candle_source=_service_candles))
_MARKET_DATA_ENGINE.register(market_data_layer.MockLiveProvider(_fixture_regime))
_MARKET_DATA_ENGINE.register(market_data_layer.MT5MarketDataProvider(
    _fixture_regime, available_symbols=_MT5_SYMBOLS, aliases=_MT5_ALIASES, enabled=_MT5_MD_ENABLED))
# Config-driven active selection (no automatic switching). Falls back to fixture.
if _MD_PROVIDER in ("fixture", "replay", "mock_live", "mt5"):
    _MARKET_DATA_ENGINE.set_active(_MD_PROVIDER)


# ---------------------------------------------------------------------------
# Strategy Engine wiring (Phase 9). READ-ONLY signal evaluation → Decisions +
# candidate commands. Executes nothing (no overlay writes, no commands, no
# events). The Execution Orchestrator remains the sole execution owner.
#
# Phase 11: the strategy's market input now flows EXCLUSIVELY through the Market
# Data Engine's immutable snapshot (`market_state_view`) — the strategy consumes
# snapshots only and never reads runtime market state directly. `strategy.py` is
# unchanged; only this injection seam is re-pointed.
# ---------------------------------------------------------------------------

def _market_state(pair: str) -> dict | None:
    return _MARKET_DATA_ENGINE.market_state_view(pair)


def _active_matrix(pair: str) -> dict | None:
    pkg = _active_package_view() or {}
    return pkg.get("policy", {}).get("matrices", {}).get(pair)


def _strategy_env() -> strategy_layer.StrategyEnv:
    return strategy_layer.StrategyEnv(
        deployments=_deployments_view,
        market_state=_market_state,
        policy_matrix=_active_matrix,
        active_package_version=lambda: _active_package_runtime().get("current"),
        recommendations_for=lambda pair: [
            r for r in WORLD.get("recommendations", []) if r.get("scenarioKey", "").startswith(f"{pair}:")
        ],
        now=_now_iso(),
    )


_STRATEGY_REGISTRY = strategy_layer.StrategyRegistry()
_STRATEGY_REGISTRY.register(strategy_layer.PolicyAlignmentStrategy())
_STRATEGY_METRICS = strategy_layer.StrategyMetrics()
_STRATEGY_ENGINE = strategy_layer.StrategyEngine(_STRATEGY_REGISTRY, _STRATEGY_METRICS)
_STRATEGY_CACHE: dict | None = None
_STRATEGY_SEQ = 0


# ---------------------------------------------------------------------------
# Risk Engine wiring (Phase 12). READ-ONLY. The only owner of runtime trading
# PERMISSIONS: it assesses each proposed action (Decision) and returns
# allow/warn/deny. It executes nothing (no overlay writes, no commands, no broker
# calls). The Execution Orchestrator remains the sole executor. `risk_engine.py`
# imports nothing from server.py; risk numbers are reused from existing infra
# (deployment riskState, account fundedRules, Market Data snapshot, health).
# ---------------------------------------------------------------------------

def _account_by_id(account_id: str | None) -> dict | None:
    return next((a for a in WORLD.get("accounts", []) if a.get("accountId") == account_id), None)


def _risk_env() -> risk_layer.RiskEnv:
    return risk_layer.RiskEnv(
        deployment=_deployment_current,
        account=_account_by_id,
        market_snapshot=lambda sym: _MARKET_DATA_ENGINE.snapshot_dict(sym, market_data_layer.DEFAULT_TIMEFRAME),
        active_package_hash=_active_package_hash,
        runtime_health=lambda: {"runtimeDbHealthy": True, "eventStoreHealthy": True},
        broker_health=lambda: {"connection": broker_layer.get_broker().connection().state},
        now=_now_iso(),
    )


_RISK_METRICS = risk_layer.RiskMetrics()
_RISK_ENGINE = risk_layer.RiskEngine(_RISK_METRICS)
_RISK_CACHE: dict | None = None


# ---------------------------------------------------------------------------
# Portfolio Engine wiring (Phase 14). READ-ONLY. The single owner of capital-
# allocation decisions: over every risk-assessed Decision it returns
# allocate/defer/reject. It executes nothing (no overlay writes, no commands, no
# broker calls, no position sizing). The Execution Orchestrator remains the sole
# executor. `portfolio.py` imports nothing from server.py; capital/exposure/risk
# numbers are reused from existing infra (account equity + fundedRules,
# deployment riskState, the Risk Assessment).
# ---------------------------------------------------------------------------

def _portfolio_env() -> portfolio_layer.PortfolioEnv:
    return portfolio_layer.PortfolioEnv(
        accounts=lambda: WORLD.get("accounts", []),
        account=_account_by_id,
        deployment=_deployment_current,
        now=_now_iso(),
    )


_PORTFOLIO_METRICS = portfolio_layer.PortfolioMetrics()
_PORTFOLIO_ENGINE = portfolio_layer.PortfolioEngine(_PORTFOLIO_METRICS)
_PORTFOLIO_CACHE: dict | None = None


def _run_strategy_evaluation() -> dict:
    """Scheduler-invoked evaluation. Market Data → Strategy → Risk → Portfolio.
    Requests a fresh snapshot per pair, evaluates the strategy, runs the Risk Engine
    over each Decision, then the Portfolio Engine allocates capital across every
    risk-assessed opportunity (allocate/defer/reject). Every engine executes nothing."""
    global _STRATEGY_CACHE, _STRATEGY_SEQ, _RISK_CACHE, _PORTFOLIO_CACHE
    _STRATEGY_SEQ += 1
    for pair in {d.get("pair") for d in _deployments_view() if d.get("pair")}:
        _MARKET_DATA_ENGINE.snapshot(pair, market_data_layer.DEFAULT_TIMEFRAME)
    result = _STRATEGY_ENGINE.evaluate(_strategy_env(), _STRATEGY_SEQ)
    # Risk assessment sits between Decision and Portfolio — assess every Decision.
    env = _risk_env()
    decisions = result.get("decisions", [])
    assessments = [risk_layer.asdict(_RISK_ENGINE.assess_decision(env, d)) for d in decisions]
    result["risk"] = {"assessments": assessments, "health": _RISK_METRICS.health()}
    _RISK_CACHE = result["risk"]
    # Portfolio allocation sits between Risk and Execution — evaluate every opportunity.
    opportunities = [{"decision": d, "assessment": a} for d, a in zip(decisions, assessments)]
    result["portfolio"] = _PORTFOLIO_ENGINE.evaluate(_portfolio_env(), opportunities)
    _PORTFOLIO_CACHE = result["portfolio"]
    _STRATEGY_CACHE = result
    return result


# ---------------------------------------------------------------------------
# Scheduler wiring (Phase 10). Owns WHEN strategy evaluations occur. Invokes the
# Strategy Engine only (never evaluates itself, never executes commands). No
# threads/workers/cron — a request-driven clock + logical FIFO queue.
# ---------------------------------------------------------------------------

_SCHEDULER = scheduler_layer.Scheduler(evaluate_fn=_run_strategy_evaluation, now_fn=_now_iso)
_SCHEDULER.register_interval(1500)  # interval trigger fires each ~2s frontend tick


def _close_trade(trade_id: str, now: str, reason: str | None) -> tuple[dict, dict]:
    cur = _trade_current(trade_id) or {}
    before = {"state": cur.get("state")}
    close_price = cur.get("tp") if cur.get("tp") is not None else cur.get("entry")
    scalars = {
        "state": "closed",
        "closedAt": now,
        "closePrice": close_price,
        "realizedR": cur.get("currentR"),
        "protectionStatus": "closed",
    }
    after = {"state": "closed", "closePrice": close_price, "realizedR": cur.get("currentR")}
    _append_trade_management(trade_id, _mgmt_entry("CLOSE", now, before, after, reason or "closed at market"),
                             scalars=scalars)
    return before, after


@api_router.get("/")
async def root():
    return {
        "service": "Control Tower API",
        "version": "0.1.0",
        "fixtureVersion": WORLD.get("meta", {}).get("fixtureVersion"),
        "contractVersion": WORLD.get("meta", {}).get("contractVersion"),
    }


@api_router.get("/health")
async def health():
    return {"status": "ok", "asOf": WORLD.get("meta", {}).get("asOf")}


@api_router.get("/world")
async def world():
    """Return the full frozen world fixture. Used by the fixture provider fallback."""
    return WORLD


@api_router.get("/fleet")
async def fleet():
    return {
        "deployments": _deployments_view(),
        "brokers": WORLD.get("brokers", []),
        "accounts": WORLD.get("accounts", []),
        "asOf": WORLD.get("meta", {}).get("asOf"),
    }


@api_router.get("/deployments")
async def deployments():
    return _deployments_view()


@api_router.get("/deployments/{deployment_id}")
async def deployment(deployment_id: str):
    dep = _deployment_current(deployment_id)
    if dep is not None:
        return _apply_aggregates(dep, _live_trades_view())
    raise HTTPException(status_code=404, detail="Deployment not found")


@api_router.get("/packages")
async def packages():
    return WORLD.get("packages", [])


@api_router.get("/packages/active")
async def active_package():
    """The runtime-active package (runtime selection ⊕ fixture default). Deploy
    and Rollback change which version this returns without mutating any package."""
    pkg = _active_package_view()
    if pkg is not None:
        return pkg
    raise HTTPException(status_code=404, detail="No active package")


@api_router.get("/runtime/active-package")
async def runtime_active_package():
    """Runtime active-package record: {current, previous, activatedAt, source}."""
    return _active_package_runtime()


@api_router.get("/runtime/health")
async def runtime_health():
    """Runtime-layer health (overlay/event-store/runtime-DB liveness + enabled
    capabilities). Distinct from broker health."""
    return _runtime_health()


# ---------------------------------------------------------------------------
# Broker abstraction (Phase 6) — the runtime's only execution boundary. Reads
# reflect the active broker (MockBroker; MT5 is a non-connecting skeleton).
# ---------------------------------------------------------------------------

@api_router.get("/broker/status")
async def broker_status():
    """Active broker: kind, connection lifecycle state, and advertised capabilities."""
    brk = broker_layer.get_broker()
    return {
        "kind": brk.kind,
        "brokerId": brk.broker_id,
        "active": broker_layer.active_kind(),
        "connection": broker_layer.asdict(brk.connection()),
        "health": broker_layer.asdict(brk.health()),
        "capabilities": broker_layer.capability_dict(brk.capabilities()),
    }


@api_router.get("/broker/capabilities")
async def broker_capabilities():
    return broker_layer.capability_dict(broker_layer.get_broker().capabilities())


@api_router.get("/broker/accounts")
async def broker_accounts():
    brk = broker_layer.get_broker()
    return brk.accounts(_broker_context({}, _now_iso()))


@api_router.get("/broker/positions")
async def broker_positions():
    brk = broker_layer.get_broker()
    return brk.positions(_broker_context({}, _now_iso()))


@api_router.get("/broker/orders")
async def broker_orders():
    brk = broker_layer.get_broker()
    return brk.orders(_broker_context({}, _now_iso()))


@api_router.post("/broker/sync")
async def broker_sync():
    """Run one reconciliation cycle: poll broker, compare to runtime, produce
    structured results, append one BotEvent. Read-only — never repairs."""
    return _run_broker_sync(append_event=True)


@api_router.get("/broker/reconciliation")
async def broker_reconciliation():
    """The last reconciliation result (read-only). Runs one lazily if none yet."""
    if _SYNC_CACHE is None:
        return _run_broker_sync(append_event=False)
    return _SYNC_CACHE


@api_router.get("/broker/faults")
async def broker_get_faults():
    """Developer-only fault toggles (simulate broker divergence)."""
    return sync_layer.get_faults()


@api_router.post("/broker/faults")
async def broker_set_faults(request: Request):
    """Developer-only: set/clear simulated broker faults. Perturbs the broker
    snapshot only — the runtime stays authoritative."""
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if isinstance(patch, dict) and patch.get("clear"):
        return sync_layer.clear_faults()
    return sync_layer.set_faults(patch if isinstance(patch, dict) else {})


@api_router.get("/execution/health")
async def execution_health():
    """Execution-orchestrator health (also folded into /runtime/health)."""
    return _EXEC_METRICS.health(_DRY_RUN_MODE)


@api_router.get("/execution/dry-run")
async def get_dry_run():
    return {"enabled": _DRY_RUN_MODE}


@api_router.post("/execution/dry-run")
async def set_dry_run(request: Request):
    """Toggle global dry-run mode. When on, commands run the full pipeline (validate
    → policy → simulate) and still append audit events, but no overlay is persisted."""
    global _DRY_RUN_MODE
    try:
        body = await request.json()
    except Exception:
        body = {}
    _DRY_RUN_MODE = bool(body.get("enabled")) if isinstance(body, dict) else False
    return {"enabled": _DRY_RUN_MODE}


# ---------------------------------------------------------------------------
# Strategy Engine (Phase 9) — READ-ONLY. Evaluation only; executes nothing.
# ---------------------------------------------------------------------------

@api_router.get("/strategy/registry")
async def strategy_registry():
    return _STRATEGY_REGISTRY.enumerate()


@api_router.post("/strategy/evaluate")
async def strategy_evaluate():
    """Run one signal evaluation → Decisions + candidate commands + report.
    Read-only: no overlay writes, no commands, no events, no execution."""
    return _run_strategy_evaluation()


@api_router.get("/strategy/decisions")
async def strategy_decisions():
    """Last evaluation result (read-only). Runs one lazily if none yet."""
    if _STRATEGY_CACHE is None:
        return _run_strategy_evaluation()
    return _STRATEGY_CACHE


@api_router.get("/strategy/health")
async def strategy_health():
    return _STRATEGY_METRICS.health()


# ---------------------------------------------------------------------------
# Scheduler (Phase 10) — decides WHEN evaluations run; invokes the Strategy
# Engine only. Never executes commands. Read-only w.r.t. runtime state.
# ---------------------------------------------------------------------------

@api_router.get("/scheduler/status")
async def scheduler_status():
    return _SCHEDULER.status()


@api_router.get("/scheduler/queue")
async def scheduler_queue():
    return _SCHEDULER.queue()


@api_router.get("/scheduler/history")
async def scheduler_history():
    return _SCHEDULER.history()


@api_router.post("/scheduler/tick")
async def scheduler_tick():
    """Advance the scheduler clock: fire due interval jobs and drain the FIFO
    queue (one evaluation at a time). Returns the latest strategy result + status."""
    return _SCHEDULER.tick()


@api_router.post("/scheduler/trigger")
async def scheduler_trigger():
    """Manually enqueue + run one evaluation (Manual trigger)."""
    return _SCHEDULER.trigger_manual()


# ---------------------------------------------------------------------------
# Market Data Engine endpoints (Phase 11) — READ ONLY. The engine owns every
# snapshot; nothing here executes strategies or commands.
# ---------------------------------------------------------------------------

@api_router.get("/market-data/snapshot")
async def market_data_snapshot(symbol: str = "EURUSD", timeframe: str = market_data_layer.DEFAULT_TIMEFRAME,
                               provider: str | None = None):
    """Current immutable snapshot for a symbol/timeframe. With `provider`, renders a
    READ-ONLY preview from that named provider (e.g. MT5) without changing the active
    one. Otherwise returns the active snapshot, producing one on demand if needed."""
    if provider:
        snap = _MARKET_DATA_ENGINE.preview(provider, symbol, timeframe)
        if snap is None:
            raise HTTPException(status_code=404, detail=f"unknown or unavailable provider '{provider}'")
        return snap
    snap = _MARKET_DATA_ENGINE.snapshot_dict(symbol, timeframe)
    if snap is None:
        snap = market_data_layer.asdict(_MARKET_DATA_ENGINE.snapshot(symbol, timeframe))
    return snap


@api_router.get("/market-data/candles")
async def market_data_candles(symbol: str = "EURUSD", timeframe: str = market_data_layer.DEFAULT_TIMEFRAME,
                              count: int = 220, end: str | None = None, provider: str | None = None,
                              start: str | None = None):
    """OHLC candle series — the SINGLE candle pipeline for every chart. RANGE-first
    (Phase 24): pass `start` (+optional `end`) for an explicit range (historical
    scrolling); pass `end`+`count` for an end-anchored window; pass neither for the
    latest `count` bars from the live edge. `provider=replay` keeps sourcing from the
    ReplayProvider (unchanged). Read-only."""
    count = max(1, min(5000, count))
    # Live-edge default: the un-parameterised live chart path (no provider, no end)
    # must follow the CURRENT market time — not the fixture asOf, which would clamp
    # a live Polygon feed to 1 July. Explicitly-named providers (replay/mock_live/…)
    # keep the deterministic fixture anchor their tests and scenarios rely on.
    if end:
        end_iso = end
    elif provider:
        end_iso = WORLD.get("meta", {}).get("asOf") or _now_iso()
    else:
        end_iso = None  # engine substitutes real now → DataService live edge
    series = _MARKET_DATA_ENGINE.candles(symbol, timeframe, count, end_iso, provider, start)
    # Diagnostics (Phase 24 debug): which service provider actually answered, the
    # request id, and cache state — correlates frontend logs with backend logs.
    # Attached for the live path (provider=None) AND replay, since both route through
    # the DataService (_service_candles) so last_query is fresh + meaningful. Other
    # explicit providers (mt5, mock_live) bypass the service, so their last_query
    # would be stale — omit it.
    q = dict(_DATA_SERVICE.last_query) if provider in (None, "replay") else {}
    return {
        "symbol": symbol, "timeframe": timeframe,
        "provider": provider or _MARKET_DATA_ENGINE._active,
        "requestId": q.get("requestId"), "source": q.get("source"),
        "cacheHit": q.get("cacheHit"), "fellBack": q.get("fellBack"), "staleLive": q.get("staleLive"),
        "polygonStatus": q.get("polygonStatus"), "cacheAgeSeconds": q.get("cacheAgeSeconds"),
        "lastSuccessAt": q.get("lastSuccessAt"), "lastFailedAt": q.get("lastFailedAt"),
        "providerNote": q.get("providerNote"),
        "start": start, "end": end_iso or "live", "count": len(series), "candles": series,
    }


@api_router.get("/market-data/m1-window")
async def market_data_m1_window(symbol: str = "EURUSD", start: str = "", end: str = ""):
    """M1 inspector (Phase 24): the canonical M1 bars inside a parent-bar window —
    click an M15 candle, see its underlying M1 candles. Read-only."""
    to_s = data_service_layer._iso_to_unix
    s, e = to_s(start), to_s(end)
    if not s or not e or e <= s:
        raise HTTPException(status_code=422, detail="start and end (ISO) required, end > start")
    bars = _DATA_SERVICE.m1_window(symbol, s, e)
    return {"symbol": symbol, "timeframe": "M1", "start": start, "end": end,
            "count": len(bars), "candles": bars}


@api_router.get("/market-data/service")
async def market_data_service_status():
    """Market Data Service status: active source, provider availability, store
    coverage + gap report (Architecture V1.2 §4.2). Read-only."""
    return _DATA_SERVICE.status()


@api_router.get("/market-data/history")
async def market_data_history(limit: int = 25):
    return _MARKET_DATA_ENGINE.history(limit)


@api_router.get("/market-data/providers")
async def market_data_providers():
    return _MARKET_DATA_ENGINE.provider_status()


@api_router.get("/market-data/symbols")
async def market_data_symbols(provider: str | None = None):
    """Available symbols for a provider (defaults to the active one). Read-only."""
    return {"provider": provider or _MARKET_DATA_ENGINE._active,
            "symbols": _MARKET_DATA_ENGINE.symbols(provider)}


@api_router.get("/market-data/quote")
async def market_data_quote(symbol: str = "EURUSD", timeframe: str = market_data_layer.DEFAULT_TIMEFRAME,
                            provider: str = "mt5"):
    """Current bid/ask/spread from the MT5 read-only feed (Objective 2). Read-only;
    never places a trade and never touches the Broker abstraction."""
    p = _MARKET_DATA_ENGINE.provider(provider)
    if p is None or not hasattr(p, "quote"):
        raise HTTPException(status_code=404, detail=f"provider '{provider}' has no quote feed")
    return p.quote(symbol, timeframe, _now_iso())


# ---------------------------------------------------------------------------
# Risk Engine endpoints (Phase 12) — READ ONLY. The engine assesses proposed
# actions (allow/warn/deny); it executes nothing and blocks nothing automatically.
# ---------------------------------------------------------------------------

@api_router.get("/risk/assessment")
async def risk_assessment(deployment: str | None = None):
    """Assess a deployment's proposed action (or every deployment if unscoped).
    Read-only — produces RiskAssessment(s); nothing is executed or blocked."""
    env = _risk_env()
    if deployment:
        return risk_layer.asdict(_RISK_ENGINE.assess(env, deployment))
    deps = _deployments_view()
    return {"assessments": [risk_layer.asdict(_RISK_ENGINE.assess(env, d.get("deploymentId", "")))
                            for d in deps],
            "health": _RISK_METRICS.health()}


@api_router.get("/risk/health")
async def risk_health():
    return _RISK_METRICS.health()


@api_router.get("/risk/limits")
async def risk_limits(account: str | None = None):
    """Active limit set (engine defaults overlaid with the fixture account fundedRules)."""
    return _RISK_ENGINE.limits(_risk_env(), account)


# ---------------------------------------------------------------------------
# Portfolio Engine endpoints (Phase 14) — READ ONLY. The engine allocates capital
# across approved opportunities (allocate/defer/reject); it executes nothing.
# ---------------------------------------------------------------------------

@api_router.get("/portfolio/status")
async def portfolio_status():
    """Portfolio config + total capital + last allocation decisions + metrics."""
    return _PORTFOLIO_ENGINE.status(_portfolio_env())


@api_router.get("/portfolio/allocations")
async def portfolio_allocations():
    """The most recent AllocationDecision set (one per opportunity). Read-only."""
    return _PORTFOLIO_ENGINE.last_decisions()


@api_router.get("/portfolio/metrics")
async def portfolio_metrics():
    return _PORTFOLIO_METRICS.health()


@api_router.get("/operator/preferences")
async def get_operator_preferences() -> dict[str, Any]:
    """The single operator-preferences model, persisted in the runtime overlay
    (`meta/operator_prefs`). View state, not trading truth — no audit event."""
    return _get_overlay("meta", "operator_prefs")


@api_router.put("/operator/preferences")
async def put_operator_preferences(request: Request) -> dict[str, Any]:
    """Merge-update operator preferences. Shallow-merge so a partial patch keeps
    unrelated keys. Persisted to the overlay; survives restart."""
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if not isinstance(patch, dict):
        raise HTTPException(status_code=400, detail="Preferences body must be an object")
    current = _get_overlay("meta", "operator_prefs")
    merged = {**current, **patch}
    _put_overlay("meta", "operator_prefs", merged)
    return merged


@api_router.get("/policy/{instrument}/matrix")
async def policy_matrix(instrument: str, version: int | None = None):
    """Return the (representative) policy matrix stored in the fixture.
    The frontend fixture provider expands it to full 24×6=144 cells.
    """
    pkgs = WORLD.get("packages", [])
    if version is not None:
        pkg = next((p for p in pkgs if p.get("version") == version), None)
    else:
        pkg = next((p for p in pkgs if p.get("status") == "active"), None)
    if not pkg:
        raise HTTPException(status_code=404, detail="Package not found")
    matrices = pkg.get("policy", {}).get("matrices", {})
    if instrument not in matrices:
        raise HTTPException(status_code=404, detail=f"No matrix for {instrument}")
    return matrices[instrument]


@api_router.get("/trades")
async def trades(pair: str | None = None, lane: str | None = None):
    lt = _live_trades_view()
    gt = WORLD.get("ghostTrades", [])
    bi = WORLD.get("blockedIntents", [])
    if pair:
        lt = [t for t in lt if t.get("scenarioKey", "").startswith(f"{pair}:")]
        gt = [t for t in gt if t.get("scenarioKey", "").startswith(f"{pair}:")]
        bi = [t for t in bi if t.get("scenarioKey", "").startswith(f"{pair}:")]
    if lane:
        lt = [t for t in lt if t.get("lane") == lane]
        bi = [t for t in bi if t.get("lane") == lane]
    return {"live": lt, "ghost": gt, "blocked": bi}


@api_router.get("/broker-health")
async def broker_health():
    return {"brokers": WORLD.get("brokers", []), "health": WORLD.get("brokerHealth", [])}


@api_router.get("/edge-monitor")
async def edge_monitor():
    return WORLD.get("edgeMonitor", {})


@api_router.get("/system-confidence")
async def system_confidence():
    return WORLD.get("systemConfidence", {})


@api_router.get("/recommendations")
async def recommendations():
    return WORLD.get("recommendations", [])


@api_router.get("/decisions/{decision_id}")
async def decision(decision_id: str):
    for d in WORLD.get("decisionChains", []):
        if d.get("decisionId") == decision_id:
            return d
    raise HTTPException(status_code=404, detail="Decision chain not found")


@api_router.get("/events")
async def events(pair: str | None = None):
    """Merged audit stream: frozen fixture events + appended operator events,
    ordered by monotonic seq (newest seq last)."""
    evs = _all_events()
    if pair:
        evs = [e for e in evs if e.get("scenarioKey") is None or e.get("scenarioKey", "").startswith(f"{pair}:")]
    return evs


@api_router.get("/events/live")
async def events_live(since: int = 0, pair: str | None = None, timeout: float = 25.0):
    """Realtime read side (Phase 5) — long-poll delta channel. Returns events with
    `seq > since` as soon as any exist (checked every 0.5s), or an empty delta
    after `timeout` seconds. `seq` is the source of truth: the client tracks the
    last seq it applied and re-requests from there, so reconnects replay cleanly.

    Response: {events: [...delta], seq: <max delta seq or since>, head: <server head seq>}.
    `head < since` signals the client its cursor is stale (e.g. the event log was
    reset) and it should resync a fresh snapshot. Transport-agnostic: plain HTTP GET;
    no WebSocket, no broker, no auth.
    """
    timeout = max(1.0, min(60.0, timeout))
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        head = _max_seq()
        if head < since:
            # Client cursor ahead of server head → stale (log reset). Tell it to resync.
            return {"events": [], "seq": head, "head": head, "stale": True}
        delta = _events_since(since, pair)
        if delta:
            return {"events": delta, "seq": delta[-1].get("seq", since), "head": head, "stale": False}
        if asyncio.get_event_loop().time() >= deadline:
            return {"events": [], "seq": since, "head": head, "stale": False}
        await asyncio.sleep(0.5)


@api_router.post("/commands/{name}")
async def run_command(name: str, request: Request) -> dict[str, Any]:
    """Command endpoint (still broker-free). Validates the command against the
    known vocabulary, appends an immutable BotEvent to the append-only store,
    and returns the created event. Live broker execution is a later phase
    behind hard rails; the mock here is the execution, not the audit trail.
    """
    if name not in KNOWN_COMMANDS:
        raise HTTPException(status_code=400, detail=f"Unknown command: {name}")
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # Idempotency: if this exact command already ran, return its original event
    # WITHOUT re-applying the runtime effect (retry-safe — a replayed
    # PartialClose must not halve size twice).
    idem_key = request.headers.get("Idempotency-Key")
    replayed = _find_event_by_idempotency(idem_key) if idem_key else None
    if replayed is not None:
        return {
            "ok": True,
            "commandId": replayed.get("causedBy"),
            "acceptedAt": replayed.get("at"),
            "mode": "mock",
            "deduplicated": True,
            "echo": {"name": name, "payload": payload},
            "events": [replayed],
        }

    command_id = f"cmd_{uuid.uuid4().hex[:20]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    reason = payload.get("reason")

    # Route through the Execution Orchestrator: validate → policy → dispatch
    # (broker / runtime control-plane) → runtime overlay update. The orchestrator
    # owns execution policy; dispatch owns the mock execution.
    dry_run = bool(payload.get("dryRun")) or _DRY_RUN_MODE
    result = _ORCHESTRATOR.execute(name, payload, now, dry_run)
    if result.status in ("rejected", "denied"):
        raise HTTPException(status_code=422, detail={
            "status": result.status, "stage": result.stage, "reason": result.reason,
            "code": result.code, "timeline": result.timeline, "stages": result.stages,
        })

    explanation = f"Operator dispatched {name}{' [dry-run]' if dry_run else ''} (mock execution — no broker action)."
    if reason:
        explanation += f" Reason: {reason}"

    event = {
        "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}",
        "seq": 0,  # assigned inside _append_event
        "category": _COMMAND_CATEGORY.get(name, "manual"),
        "code": f"{_snake_upper(name)}_ACCEPTED",
        "humanExplanation": explanation,
        "scenarioKey": payload.get("scenarioKey"),
        "packageHash": payload.get("packageHash") or _active_package_hash(),
        "who": _operator_id(),
        "causedBy": command_id,
        "before": result.before,
        "after": result.after,
        "at": now,
    }
    event, deduplicated = _append_event(event, request.headers.get("Idempotency-Key"))
    if not deduplicated:
        logger.info("Command %s: %s %s → event %s seq=%s", result.status, name, payload, event["eventId"], event["seq"])
    return {
        "ok": True,
        "commandId": event["causedBy"] if deduplicated else command_id,
        "acceptedAt": event["at"] if deduplicated else now,
        "mode": "mock",
        "deduplicated": deduplicated,
        "dryRun": dry_run,
        "echo": {"name": name, "payload": payload},
        "events": [event],
        "execution": {
            "status": result.status,
            "dryRun": result.dryRun,
            "timeline": result.timeline,
            "stages": result.stages,
        },
    }


@api_router.post("/runtime/reset")
async def runtime_reset() -> dict[str, Any]:
    """Clear all runtime overlays, reverting reads to the pristine fixture.
    The append-only event log is NOT cleared (audit history is immutable)."""
    count = 0
    if RUNTIME_DB_PATH.exists():
        with _runtime_lock:
            conn = _runtime_db()
            try:
                count = conn.execute("SELECT COUNT(*) FROM runtime_overlay").fetchone()[0]
                conn.execute("DELETE FROM runtime_overlay")
                conn.commit()
            finally:
                conn.close()
    return {"ok": True, "clearedOverlays": count}


@api_router.get("/feature-flags")
async def feature_flags() -> dict[str, Any]:
    return {
        "ghostTrading": True,
        "replay": True,
        "edgeMonitor": True,
        "researchForward": True,
        "brokerHealth": True,
        "notifications": True,
        "analytics": True,
        "newsIntegration": False,
        "experimentalFeatures": True,
        "aiRecommendations": True,
        "commandPalette": True,
        "whyWorkflow": True,
        "decisionChainInspector": True,
        "accountsProtection": True,
        "charts": True,
        "versionHistory": True,
        "packageComparison": True,
    }


app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=os.environ.get('CORS_ORIGINS', '*').split(','),
    allow_methods=["*"],
    allow_headers=["*"],
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)


@app.on_event("shutdown")
async def shutdown_db_client():
    if client is not None:
        client.close()
