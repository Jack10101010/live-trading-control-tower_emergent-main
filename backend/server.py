"""
Control Tower FastAPI stub.

Presentation-first: the frontend consumes `world.v1.json` via a fixture provider
today. This backend exposes route shapes that match the future data-repository
contract so hooks can flip from fixture → API without component churn.
"""
from fastapi import FastAPI, APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
import asyncio
import os
import json
import sqlite3
import threading
import uuid
import logging
from datetime import datetime, timedelta, timezone
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
# L1A frozen Operational Status Model. Flat import matching every sibling above —
# the app runs as `uvicorn server:app` from backend/, so this is the ONE module
# object for L1A in the process (no dual identity via a package-form import).
import connection_state as connection_layer
import live_telemetry
import auth_policy
import cors_policy
import node_client
import command_channel
import command_transport
import command_registry
import command_authorization
import execution_mode as execution_mode_layer
import connection_policy
import transport as transport_layer
import environment as environment_layer
import execution_safety
import execution_context as execution_context_layer
import execution_store as execution_store_layer
import execution_telemetry as execution_telemetry_layer
import order_lifecycle as order_lifecycle_layer
import operational_projection as projection_layer
import scenario_domain as scenario_layer
import scenario_store as scenario_store_layer
import broker_history as broker_history_layer
import trade_ledger_domain as ledger_domain
import trade_ledger_store as ledger_store_layer
import trade_reconstruction as reconstruction_layer
import live_pipeline as live_pipeline_layer
import live_preflight as live_preflight_layer
import fixture_surfaces
import fixture_world
import ledger_ingestion as ledger_ingestion_layer
import market_runtime as market_runtime_layer
import operator_identity
import mt5_diagnostics as mt5_diagnostics_layer
import recommendation_authorization as recommendation_auth_layer
import recommendation_decision_service as recommendation_decision_layer
import recommendation_domain as recommendation_layer
import recommendation_service as recommendation_service_layer
import recommendation_store as recommendation_store_layer
import runtime_overlay as runtime_overlay_layer
import runtime_supervisor as runtime_supervisor_layer
import state_dir
import reconciliation as reconciliation_layer
import ops_status as ops_status_layer
import security_config
import ops_journal as ops_journal_layer
import ops_notifier as ops_notifier_layer


ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# ── L1B Dashboard API configuration ───────────────────────────────────────────
# Where the live node writes its operational outputs. Same environment variables
# and defaults as live/config.py, so the operator configures one set. Resolved at
# import; never derived from request input (no path traversal surface).
OPS_STATE_DIR = Path(os.environ.get("LIVE_STATE_DIR", "./live_state")).resolve()
OPS_MARKET_DATA_DIR = Path(
    os.environ.get("MARKET_DATA_DIR", "./live_state/market_data")).resolve()
OPS_KILL_FILE = Path(os.environ.get("LIVE_KILL_FILE", "./live_state/KILL")).resolve()

logger = logging.getLogger(__name__)

# ── M-ENV-1: the deployment-environment boundary ─────────────────────────────
# THE FIRST THING THIS PROCESS DECIDES, and the only place the environment is
# resolved. It runs BEFORE the Mongo connection, BEFORE the fixture world is
# loaded and BEFORE any market-data provider is registered, so a rejected
# production configuration never reaches a state where a store could be opened,
# a background loop started, or a fixture-backed value served. An inadmissible
# capability raises `EnvironmentViolation`, which propagates out of module
# import — uvicorn exits non-zero and no route is ever mounted.
#
# `.env` is loaded above, so a variable set there is honoured here.
#
# The resolved environment is LOGGED further down, beside the CORS and auth
# summaries, because `logging.basicConfig` has not run yet at this point — a
# line emitted here is silently dropped (verified against a real uvicorn boot).
# Resolution stays here; only the reporting is deferred.
ENVIRONMENT = environment_layer.enforce_startup()
_ENV_POLICY = environment_layer.policy(ENVIRONMENT)

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
        # UI-9: redacted. A driver exception can echo the connection URI, and a
        # Mongo URI embeds credentials (mongodb://user:pass@host).
        logger.warning("MongoDB unavailable (%s); running fixture-only.",
                       security_config.redact_text(exc))
else:
    logger.info("No MONGO_URL set; running fixture-only (no database).")

app = FastAPI(title="Control Tower API", version="0.1.0")
api_router = APIRouter(prefix="/api")


#: HARDEN-1: the fixture world is now an EXPLICITLY OPTIONAL source.
#:
#: This used to be `raise FileNotFoundError` at module scope, which made a
#: development fixture a hard boot dependency of the whole backend — deleting
#: `backend/fixtures/` stopped the process from starting at all. Loading is now
#: total: an absent or malformed fixture yields an empty world with
#: `available == False`, and surfaces that have no production source say so
#: explicitly via `fixture_world.unavailable(...)` rather than returning an
#: empty collection that reads as "nothing is happening".
#:
#: When the fixture IS present every reader sees exactly what it saw before.
FIXTURE_SEARCH_PATHS = (
    ROOT_DIR / 'fixtures' / 'world.v1.json',
    Path('/app/frontend/src/data/world.v1.json'),
    Path('/app/_fixtures/world.v1.json'),
)

# M-ENV-1: in production the fixture world is never ACTIVATED. It is not loaded
# and then ignored — the load call is not made at all, so no fixture bytes enter
# the process. The result is the SAME unavailable world the repository already
# produces when `world.v1.json` is absent, which means the proven FIX-2 boundary
# below (`_fixture_surface_boundary`) refuses every fixture-backed surface with
# an honest 501 instead of serving fabricated operational data. That is why a
# production process can start while the remaining WORLD-backed endpoints exist:
# they answer "no production source" rather than lying, and each is retired by
# its own eradication milestone.
WORLD = (fixture_world.load(FIXTURE_SEARCH_PATHS) if _ENV_POLICY.world_may_load
         else fixture_world.unavailable_world())


def _fixture_available() -> bool:
    """Whether the development fixture world is present."""
    return bool(WORLD.available)


def _fixture_unavailable(surface: str, extra: dict | None = None) -> JSONResponse:
    """A 501 for a surface backed only by the fixture world.

    501 (not 404, not 200-with-empty) because the resource is not implemented
    against a production source — the honest status for "this exists as a
    concept but has no real backing yet"."""
    return JSONResponse(
        content=fixture_world.unavailable(surface, extra=extra),
        status_code=501, headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# Event store (Phase 4, Step 1): append-only BotEvent log backing the
# command → event → audit loop. SQLite via stdlib — no new dependency, file
# created lazily. Fixture events stay frozen in world.v1.json; operator
# actions append here and GET /api/events returns the merged stream.
# ---------------------------------------------------------------------------

#: HARDEN-3: where durable state lives.
#:
#: Every store previously hardcoded its file into the SOURCE TREE
#: (`ROOT_DIR / '*.db'`), which meant a running process wrote databases next to
#: its own code — and any second instance, embedded server or background thread
#: wrote to the same six files no matter who owned them. State location is
#: deployment configuration, so it is now a single env-overridable root that
#: DEFAULTS to the previous behaviour (unchanged for existing deployments).
#: FIX-1: the root is CREATED and its writability PROVEN before any store is
#: constructed. HARDEN-3 introduced the variable but never made the directory,
#: so pointing it at a not-yet-existing path made every durable store fail to
#: open — execution denied, ledger empty — with only an opaque
#: "unable to open database file" to go on. Preparation is total; the status is
#: recorded and reported by diagnostics rather than crashing the import.
STATE_DIR = state_dir.resolve(default=ROOT_DIR)
STATE_DIR_STATUS = state_dir.prepare(STATE_DIR)
if not STATE_DIR_STATUS.ok:
    logger.error(
        "durable state root unusable (%s): %s — %s",
        STATE_DIR_STATUS.code, STATE_DIR_STATUS.detail, STATE_DIR_STATUS.fix)

EVENTS_DB_PATH = STATE_DIR / 'events.db'
_events_lock = threading.Lock()

# ARCH-1: the command vocabulary and per-command audit categories are DERIVED from the
# single canonical `command_registry` — no longer re-declared here. `KNOWN_COMMANDS`
# is the fixture-surface control-plane vocabulary; `_COMMAND_CATEGORY` its audit
# categories. The frontend Command union (frontend/src/lib/commands.ts) still mirrors
# this set; keeping it in sync remains a frontend concern (not changed by this slice).
KNOWN_COMMANDS = command_registry.fixture_command_names()

# Track B event categories: decision·order·trade·policy·risk·manual·system·error·broker
_COMMAND_CATEGORY = command_registry.fixture_categories()

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


# P-2 retention: only the newest EVENTS_RETENTION_MAX stored rows are kept —
# pruned oldest-first inside _append_event's transaction. Frozen fixture events
# live in WORLD, not this table, and are never pruned. Pruning removes each
# row's idempotency key with it: keys older than the retained window no longer
# deduplicate (accepted P-2 consequence). Sequence numbers are never renumbered
# or reused; MAX(seq) stays monotonic across ordinary pruning.
EVENTS_RETENTION_MAX = 10000
# Defensive ceiling for GET /api/events `limit` (framework-validated).
EVENTS_PAGE_LIMIT_MAX = 1000

# Once-per-seq malformed-payload warning suppression (process-local; bounded by
# the retention cap since pruned rows can no longer be read).
_MALFORMED_WARNED: set[int] = set()


class EventStoreIntegrityError(RuntimeError):
    """A row the store MUST honour (e.g. an idempotency match) is undecodable.
    Raised instead of pretending the row does not exist, so deduplication can
    never silently degrade into replay."""


def _decode_event_row(seq: int, payload: str) -> dict | None:
    """Decode one stored payload; on malformed data, warn once per seq and
    return None so ordinary readers skip the row instead of failing. Malformed
    means (audit defect D2): invalid JSON, non-object JSON, missing eventId,
    and a payload seq that is missing, non-int (bool included), or inconsistent
    with the stored row's seq column — so no invalid seq can ever reach
    sorting or the API response."""
    try:
        ev = json.loads(payload)
        if not isinstance(ev, dict) or "eventId" not in ev:
            raise ValueError("payload is not an event object")
        payload_seq = ev.get("seq")
        if (not isinstance(payload_seq, int) or isinstance(payload_seq, bool)
                or payload_seq != seq):
            raise ValueError("payload seq missing, non-integer, or inconsistent with row seq")
        return ev
    except Exception:
        if seq not in _MALFORMED_WARNED:
            _MALFORMED_WARNED.add(seq)
            logger.warning("bot_events row seq=%s has malformed payload; skipping", seq)
        return None


def _stored_events_since(since_seq: int = 0, limit: int | None = None) -> list[dict]:
    """Indexed stored-row read: rows with seq > since_seq, ascending, decoded
    tolerantly (malformed rows are skipped and warned once). With a limit, the
    fetch loop refills past skipped malformed rows so a page is never silently
    under-full while more valid rows exist."""
    if not EVENTS_DB_PATH.exists():
        return []
    out: list[dict] = []
    cursor = since_seq
    while True:
        want = None if limit is None else (limit - len(out))
        with _events_lock:
            conn = _events_db()
            try:
                if want is None:
                    rows = conn.execute(
                        "SELECT seq, payload FROM bot_events WHERE seq > ? ORDER BY seq ASC",
                        (cursor,)).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT seq, payload FROM bot_events WHERE seq > ? ORDER BY seq ASC LIMIT ?",
                        (cursor, want)).fetchall()
            finally:
                conn.close()
        for seq, payload in rows:
            ev = _decode_event_row(seq, payload)
            if ev is not None:
                out.append(ev)
        if want is None or len(out) >= limit or len(rows) < want:
            return out
        cursor = rows[-1][0]


def _stored_events() -> list[dict]:
    return _stored_events_since(0, None)


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
    """Events with seq strictly greater than `since` (the delta), oldest first.
    Stored rows come from the indexed seq-filtered read (no full-history scan
    per polling iteration); fixture events are filtered in Python (three rows)."""
    fixtures = [e for e in WORLD.get("events", []) if e.get("seq", 0) > since]
    evs = fixtures + _stored_events_since(since, None)
    evs.sort(key=lambda e: e.get("seq", 0))
    if pair:
        evs = [e for e in evs if e.get("scenarioKey") is None or e.get("scenarioKey", "").startswith(f"{pair}:")]
    return evs


def _decode_idempotent_row(idempotency_key: str, row: tuple) -> dict:
    """Strict decode for an idempotency MATCH: the key exists, so the store must
    honour it. A malformed matching payload raises EventStoreIntegrityError —
    never 'not found' — so the caller's action is NOT re-executed and the
    failure surfaces through existing route failure semantics."""
    seq, payload = row
    ev = _decode_event_row(seq, payload)
    if ev is None:
        logger.error(
            "bot_events integrity: idempotency key %r matches seq=%s but its "
            "payload is malformed; refusing to treat as absent", idempotency_key, seq)
        raise EventStoreIntegrityError(
            f"idempotent event record seq={seq} is undecodable")
    return ev


def _find_event_by_idempotency(idempotency_key: str) -> dict | None:
    """Return the event previously stored under this idempotency key, if any.
    Used to short-circuit a retried command BEFORE its runtime effect re-applies.
    A matching row with a malformed payload raises EventStoreIntegrityError
    (deduplication must never degrade into replay). Keys pruned by retention
    are genuinely absent and return None."""
    if not idempotency_key or not EVENTS_DB_PATH.exists():
        return None
    with _events_lock:
        conn = _events_db()
        try:
            row = conn.execute(
                "SELECT seq, payload FROM bot_events WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        finally:
            conn.close()
    return _decode_idempotent_row(idempotency_key, row) if row else None


def _append_event(event: dict, idempotency_key: str | None) -> tuple[dict, bool]:
    """Append a BotEvent, assigning the next monotonic seq, then prune stored
    rows beyond EVENTS_RETENTION_MAX — insert and prune commit atomically in
    one transaction (a failure rolls both back). Returns (event, deduplicated).
    A replayed idempotency key returns the original event untouched, without
    inserting or pruning; a malformed matching row raises
    EventStoreIntegrityError rather than replaying (Track B §4.18)."""
    with _events_lock:
        conn = _events_db()
        try:
            if idempotency_key:
                row = conn.execute(
                    "SELECT seq, payload FROM bot_events WHERE idempotency_key = ?",
                    (idempotency_key,),
                ).fetchone()
                if row:
                    return _decode_idempotent_row(idempotency_key, row), True
            max_stored = conn.execute("SELECT COALESCE(MAX(seq), 0) FROM bot_events").fetchone()[0]
            event["seq"] = max(_FIXTURE_MAX_SEQ, max_stored) + 1
            conn.execute(
                "INSERT INTO bot_events (seq, event_id, idempotency_key, payload) VALUES (?, ?, ?, ?)",
                (event["seq"], event["eventId"], idempotency_key, json.dumps(event)),
            )
            # Retention: delete everything at or below the (CAP+1)-th newest seq,
            # keeping exactly the newest EVENTS_RETENTION_MAX rows. Index-only
            # (seq primary key); payloads are never loaded to decide pruning.
            conn.execute(
                "DELETE FROM bot_events WHERE seq <= COALESCE("
                "(SELECT seq FROM bot_events ORDER BY seq DESC LIMIT 1 OFFSET ?), -1)",
                (EVENTS_RETENTION_MAX,),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    return event, False


def _operator_id() -> str:
    """HARDEN-1: the acting operator, from a REAL source.

    This used to return `WORLD["operators"][0]["operatorId"]` — the first entry
    of a test fixture — falling back to the literal `"system"`. Every audit
    record, broker context and safety `operator_ref` therefore attributed itself
    to a fixture. It now resolves from deployment configuration
    (`CONTROL_TOWER_OPERATOR_ID`) and reports `UNATTRIBUTED` when no operator can
    be established, which is a truthful answer rather than a plausible one."""
    return operator_identity.resolve()


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

RUNTIME_DB_PATH = STATE_DIR / 'runtime.db'

# HARDEN-3: the overlay store is now OWNED by `runtime_overlay`, which holds the
# schema, the connection handling and the process lock. It was the only durable
# store in the repository with no module of its own — the 5,167-line HTTP module
# owned a database, which is also why the test-isolation guard missed it longest.
#
# The wrappers below are thin delegation so the existing ~19 call sites are
# unchanged. The path is read through a callable so tests can repoint
# `RUNTIME_DB_PATH` exactly as before.
_RUNTIME_OVERLAY = runtime_overlay_layer.RuntimeOverlayStore(
    path_fn=lambda: RUNTIME_DB_PATH)


def _load_overlays(kind: str) -> dict[str, dict]:
    """All overlays for a kind, keyed by entity id. Empty before first write."""
    return _RUNTIME_OVERLAY.load(kind)


def _get_overlay(kind: str, entity_id: str) -> dict:
    return _RUNTIME_OVERLAY.get(kind, entity_id)


# Dry-run guard (Phase 8): when an execution is a dry-run the effect code runs
# and computes before/after exactly as MockBroker would, but the overlay WRITE is
# skipped so nothing persists. Per-async-task via contextvar → concurrency-safe.
# This is a behavioural flag on the write, not a change to overlay ownership.
_DRY_RUN = contextvars.ContextVar("_dry_run", default=False)


def _put_overlay(kind: str, entity_id: str, overlay: dict) -> None:
    """Persist an overlay unless this execution is a dry run.

    The dry-run guard stays HERE, not in the store: suppressing a write is a
    decision about simulated broker effects, and the store must not be in the
    business of second-guessing its callers."""
    if _DRY_RUN.get():
        return
    _RUNTIME_OVERLAY.put(kind, entity_id, overlay)


# ── UI-2: durable latest node-telemetry snapshot store ───────────────────────
# Deliberately NOT `_put_overlay`: that helper honours the `_DRY_RUN` contextvar
# and silently skips writes, which is correct for simulated broker effects and
# wrong for observed node facts — a dry-run execution must never make real
# telemetry vanish. Same table, dedicated kind, its own read/write pair.
#: FIX-4: re-exported from the store that owns it. HARDEN-3 left a duplicate
#: literal here, giving the same concept two sources of truth.
_LIVE_SNAPSHOT_KIND = runtime_overlay_layer.LIVE_SNAPSHOT_KIND


def _put_live_snapshot(instance_id: str, record: dict) -> bool:
    """Persist the latest VALID snapshot for an instance. Returns False if the
    write failed — persistence is best-effort and must never fail an ingest."""
    try:
        # Deliberately the STORE's put, not `_put_overlay`: the latter honours
        # the dry-run guard, and a dry-run execution must never make observed
        # node telemetry vanish.
        _RUNTIME_OVERLAY.put(_LIVE_SNAPSHOT_KIND, instance_id, record)
        return True
    except Exception:
        logger.exception("live snapshot persist failed for %s", instance_id)
        return False


def _load_live_snapshots() -> dict[str, dict]:
    """Every persisted snapshot record, keyed by instance id. Unreadable or
    non-conforming rows are skipped rather than surfaced as fake state."""
    try:
        rows = _load_overlays(_LIVE_SNAPSHOT_KIND)
    except Exception:
        logger.exception("live snapshot load failed")
        return {}
    out: dict[str, dict] = {}
    for instance_id, record in rows.items():
        if isinstance(record, dict) and isinstance(record.get("snapshot"), dict):
            out[instance_id] = record
    return out


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
    """Fixture events + retained stored rows, via SQL COUNT (payloads are not
    loaded or decoded; malformed rows still count as stored rows)."""
    stored = 0
    if EVENTS_DB_PATH.exists():
        with _events_lock:
            conn = _events_db()
            try:
                stored = conn.execute("SELECT COUNT(*) FROM bot_events").fetchone()[0]
            finally:
                conn.close()
    return len(WORLD.get("events", [])) + stored


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
# P-1B broker-sync transition gate: (signature, anchor_at). `signature` is the
# canonical reconciliation fingerprint from _sync_signature; `anchor_at` is the
# sync timestamp at which that signature was seeded or last advanced. The
# idempotency key derives from the PRIOR anchor, so an equivalent retry of an
# unadvanced transition reconstructs the identical key regardless of the new
# sync timestamp; the gate advances only after the append inserts or
# confirmed-deduplicates. A None gate (process start / backend restart) seeds
# SILENTLY — a first observation has no from-state and is not a transition.
_BROKER_SYNC_GATE: tuple[tuple, str] | None = None
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
    global _SYNC_CACHE, _SYNC_SEQ, _LAST_SUCCESSFUL_SYNC_AT, _BROKER_SYNC_GATE
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
    if append_event and _BROKER_SYNC_GATE is None:
        # Silent first observation (process start / backend restart): seed the
        # gate, emit nothing — a first sighting is not a transition. The lazily
        # triggered append_event=False path never touches the gate, preserving
        # GET /broker/reconciliation's non-emitting behaviour exactly.
        _BROKER_SYNC_GATE = (signature, now)
    elif append_event and signature != _BROKER_SYNC_GATE[0]:
        prior_sig, anchor_at = _BROKER_SYNC_GATE
        summary = result["reconciliation"]["summary"]
        code = _SYNC_CODE_BY_STATUS.get(status, "BROKER_SYNC_OK")
        explanation = (f"Broker sync {status}: {summary['warnings']} warning(s), {summary['errors']} error(s), "
                       f"{summary['brokerPositions']} position(s), {summary['brokerOrders']} order(s) "
                       f"({result['durationMs']}ms).")
        # Canonical transition states: exactly the signature's own data —
        # status plus the deterministically sorted findings identity.
        before_state = {"status": prior_sig[0], "findings": [list(f) for f in prior_sig[1]]}
        after_state = {"status": signature[0], "findings": [list(f) for f in signature[1]]}
        # Deterministic idempotency key: code + prior/new transition state +
        # the PRIOR anchor. Canonical JSON (sorted keys, no whitespace) — no
        # process-randomized hashing, no emission timestamp. A retry of the
        # same unadvanced transition reproduces this key byte-for-byte; a
        # genuine recurrence follows a gate advance and mints a new key.
        idem_key = "broker|" + json.dumps(
            [code, before_state, after_state, anchor_at],
            sort_keys=True, separators=(",", ":"))
        event = {"eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0, "category": "broker",
                 "code": code, "humanExplanation": explanation, "scenarioKey": None,
                 "packageHash": _active_package_hash(), "who": "system",
                 "causedBy": f"broker_sync:{anchor_at}",
                 "before": before_state, "after": after_state, "at": now}
        ev, _ = _append_event(event, idem_key)
        # Advance only after the append inserted or confirmed-deduplicated; an
        # exception above propagates (route failure semantics unchanged) and
        # leaves the gate at (prior_sig, anchor_at) so the retry's key matches.
        _BROKER_SYNC_GATE = (signature, now)
        result["eventAppended"] = True
        result["eventCode"] = code
        result["eventSeqAppended"] = ev["seq"]
    if status == "ok":
        _LAST_SUCCESSFUL_SYNC_AT = now
    _SYNC_CACHE = result

    # ARCH-2: delegate to the CANONICAL reconciliation authority on the same
    # cadence (no new mutating route). The fixture-view sync above drives the
    # existing UI panels; the canonical run compares durable tower intent state
    # against the adapter's coherent snapshot and persists an immutable
    # reconciliation record that feeds the execution safety gate. A store or
    # snapshot failure is logged and skipped — the fixture sync result is
    # unaffected, and safety fails closed via `reconciliation.safety_posture`.
    try:
        store = _execution_store()
        if store is not None:
            snap = broker_layer.get_broker().reconcile_snapshot(_broker_context({}, now))
            if snap.ok and isinstance(snap.data, dict):
                run = reconciliation_layer.run_reconciliation(
                    tower_intents=store.intents_by_state(limit=1000),
                    broker_orders=snap.data.get("orders", []),
                    broker_positions=snap.data.get("positions", []),
                    broker_account_identity=snap.data.get("accountIdentity"),
                    # LIVE-1: when an expected fingerprint is configured it is the
                    # authority (mismatch = hard failure); the mock's self-match
                    # behaviour is preserved when unset.
                    expected_account_identity=(os.environ.get(VAR_EXPECTED_ACCOUNT, "").strip()
                                               or snap.data.get("accountIdentity")),
                    broker_snapshot_at=snap.data.get("at"), now=now,
                )
                reconciliation_layer.persist(store, run)
                # LIVE-3: confirmation pass — acknowledged manual-management
                # operations reach their terminal modified/cancelled/closed
                # state ONLY here, when the fresh snapshot observes the change;
                # ambiguous (frozen) operations are resolved with evidence.
                reconciliation_layer.confirm_operations(store, snap.data, now=now)
    except Exception:
        logger.exception("canonical reconciliation pass failed (fixture sync unaffected)")

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

    # LIVE-2: the ONE executable broker operation. Build the canonical immutable
    # request from the orchestrator-threaded payload (the durable intent id
    # arrived via `intentId`) and call the adapter's single write. The canonical
    # BrokerResult crosses back as a plain view in `after`; the orchestrator's
    # market-order lifecycle branch persists the outcome.
    if name == "SubmitMarketOrder":
        request = broker_layer.MarketOrderRequest(
            intent_id=payload.get("intentId") or "",
            instrument=payload.get("instrument") or "",
            side=payload.get("side") or "",
            quantity=payload.get("quantity"),
            stop_loss=payload.get("stopLoss"),
            take_profit=payload.get("takeProfit"),
            comment=payload.get("comment"),
            correlation_id=payload.get("commandId"),
            idempotency_key=payload.get("idempotencyKey"),
            execution_mode=("mock-fixture" if broker_layer.active_kind() == "mock"
                            else "live"),
        )
        result = broker_layer.get_broker().submit_market_order(
            request, _broker_context(payload, now))
        return None, {"ok": result.ok, "code": result.code, "detail": result.detail,
                      "brokerRef": result.broker_ref,
                      "ack": result.data if isinstance(result.data, dict) else None}

    # LIVE-3: the three manual-management operations — canonical immutable
    # requests, adapter dispatch, canonical OperationAck back through `after`.
    if name in ("ModifyPositionProtection", "CancelPendingOrder", "ClosePosition"):
        # The route is entity-addressed; resolve the instrument from the FRESH
        # observed entity when the payload does not carry one (the validator has
        # already proven the entity exists in a fresh snapshot).
        instrument = payload.get("instrument")
        if not instrument:
            snap = _fresh_broker_snapshot() or {}
            ref = str(payload.get("positionRef") or payload.get("orderRef") or "")
            for entry in (snap.get("positions") or []) + (snap.get("orders") or []):
                if str(entry.get("positionId") or entry.get("orderId") or "") == ref:
                    instrument = entry.get("canonicalSymbol") or entry.get("symbol")
                    break
        common = dict(intent_id=payload.get("intentId") or "",
                      instrument=instrument or "",
                      correlation_id=payload.get("commandId"),
                      idempotency_key=payload.get("idempotencyKey"),
                      execution_mode=("mock-fixture" if broker_layer.active_kind() == "mock"
                                      else "live"),
                      reason=payload.get("reason"))
        if name == "ModifyPositionProtection":
            request = broker_layer.ModifyPositionProtectionRequest(
                position_ref=payload.get("positionRef") or "",
                stop_loss=payload.get("stopLoss"),
                take_profit=payload.get("takeProfit"), **common)
            result = broker_layer.get_broker().modify_position_protection(
                request, _broker_context(payload, now))
        elif name == "CancelPendingOrder":
            request = broker_layer.CancelPendingOrderRequest(
                order_ref=payload.get("orderRef") or "", **common)
            result = broker_layer.get_broker().cancel_pending_order(
                request, _broker_context(payload, now))
        else:
            request = broker_layer.ClosePositionRequest(
                position_ref=payload.get("positionRef") or "",
                quantity=payload.get("quantity"), **common)
            result = broker_layer.get_broker().close_position(
                request, _broker_context(payload, now))
        return None, {"ok": result.ok, "code": result.code, "detail": result.detail,
                      "brokerRef": result.broker_ref,
                      "ack": result.data if isinstance(result.data, dict) else None}

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


def _fresh_broker_snapshot() -> dict | None:
    """LIVE-3: one FRESH canonical broker snapshot for entity-operation
    validation. A failed read answers None (which DENIES the operation)."""
    try:
        snap = broker_layer.get_broker().reconcile_snapshot(_broker_context({}, _now_iso()))
        return snap.data if snap.ok and isinstance(snap.data, dict) else None
    except Exception:
        return None


def _execution_env() -> execution_layer.ExecutionEnv:
    brk = broker_layer.get_broker()
    return execution_layer.ExecutionEnv(
        deployment=_deployment_current,
        trade=_trade_current,
        order=_trade_by_order_id,
        active_package_version=lambda: _active_package_runtime().get("current"),
        broker_connection=lambda: brk.connection().state,
        broker_capabilities=lambda: broker_layer.capability_dict(brk.capabilities()),
        broker_snapshot=_fresh_broker_snapshot,
    )


# ---------------------------------------------------------------------------
# ARCH-2: the durable execution store (lazy, fail-closed) and the canonical
# execution context (assembled ONCE per command at this boundary).
# ---------------------------------------------------------------------------

EXECUTION_DB_PATH = STATE_DIR / 'execution_state.db'
_EXECUTION_STORE: execution_store_layer.ExecutionStore | None = None
_EXECUTION_STORE_FAILED = False


def _execution_store() -> execution_store_layer.ExecutionStore | None:
    """The single durable execution/order store. Created LAZILY on first use;
    restart recovery runs exactly once at creation (deterministic, fail-closed —
    see order_lifecycle.recovery_plan). Returns None when the store is
    unavailable/corrupt, in which case the orchestrator DENIES broker-dispatched
    commands rather than executing without durability."""
    global _EXECUTION_STORE, _EXECUTION_STORE_FAILED
    if _EXECUTION_STORE is not None:
        return _EXECUTION_STORE
    if _EXECUTION_STORE_FAILED:
        return None
    try:
        store = execution_store_layer.ExecutionStore(EXECUTION_DB_PATH)
        store.recover(now=_now_iso())
        _EXECUTION_STORE = store
        return store
    except Exception:
        logger.exception("execution store unavailable — broker-dispatched commands will be denied")
        _EXECUTION_STORE_FAILED = True
        return None


# LIVE-4B: the durable SCENARIO store. Its own file and its own schema version —
# it owns scenarios only and never touches execution tables. Lazy + fail-soft:
# an unavailable store makes scenario reads explicitly unavailable and changes
# NO execution behaviour (scenarios are not on any execution path).
SCENARIO_DB_PATH = STATE_DIR / 'scenario_state.db'
_SCENARIO_STORE: scenario_store_layer.ScenarioStore | None = None
_SCENARIO_STORE_FAILED = False


def _scenario_store() -> scenario_store_layer.ScenarioStore | None:
    global _SCENARIO_STORE, _SCENARIO_STORE_FAILED
    if _SCENARIO_STORE is not None:
        return _SCENARIO_STORE
    if _SCENARIO_STORE_FAILED:
        return None
    try:
        _SCENARIO_STORE = scenario_store_layer.ScenarioStore(SCENARIO_DB_PATH)
        return _SCENARIO_STORE
    except Exception:
        logger.exception("scenario store unavailable — scenario reads report unavailable")
        _SCENARIO_STORE_FAILED = True
        return None


# LIVE-4C: the durable TRADE LEDGER store. Its own file and schema version; it
# owns ledger facts only and never touches execution or scenario tables. Lazy +
# fail-soft: an unavailable ledger makes ledger reads explicitly unavailable and
# changes NO execution behaviour (the ledger is never an execution authority).
LEDGER_DB_PATH = STATE_DIR / 'trade_ledger.db'
_LEDGER_STORE: ledger_store_layer.TradeLedgerStore | None = None
_LEDGER_STORE_FAILED = False


def _ledger_store() -> ledger_store_layer.TradeLedgerStore | None:
    global _LEDGER_STORE, _LEDGER_STORE_FAILED
    if _LEDGER_STORE is not None:
        return _LEDGER_STORE
    if _LEDGER_STORE_FAILED:
        return None
    try:
        _LEDGER_STORE = ledger_store_layer.TradeLedgerStore(LEDGER_DB_PATH)
        return _LEDGER_STORE
    except Exception:
        logger.exception("trade ledger store unavailable — ledger reads report unavailable")
        _LEDGER_STORE_FAILED = True
        return None


# LIVE-4D: the durable RECOMMENDATION store + the ONE write service. Own file
# and schema version; owns proposals and decisions only. The service can never
# submit an order, grant authorization or change execution mode — accepting a
# Recommendation records a decision and stops.
RECOMMENDATION_DB_PATH = STATE_DIR / 'recommendation_state.db'
_RECOMMENDATION_STORE: recommendation_store_layer.RecommendationStore | None = None
_RECOMMENDATION_STORE_FAILED = False


def _recommendation_store() -> recommendation_store_layer.RecommendationStore | None:
    global _RECOMMENDATION_STORE, _RECOMMENDATION_STORE_FAILED
    if _RECOMMENDATION_STORE is not None:
        return _RECOMMENDATION_STORE
    if _RECOMMENDATION_STORE_FAILED:
        return None
    try:
        _RECOMMENDATION_STORE = recommendation_store_layer.RecommendationStore(
            RECOMMENDATION_DB_PATH)
        return _RECOMMENDATION_STORE
    except Exception:
        logger.exception("recommendation store unavailable — reads report unavailable")
        _RECOMMENDATION_STORE_FAILED = True
        return None


def _scenario_reader(scenario_id: str):
    store = _scenario_store()
    if store is None:
        return None
    try:
        return store.get_scenario(scenario_id)
    except Exception:
        return None


_RECOMMENDATION_SERVICE = recommendation_service_layer.RecommendationService(
    store_fn=lambda: _recommendation_store(),
    scenario_reader=_scenario_reader,
    now_iso_fn=lambda: _now_iso(),
    execution_mode_fn=lambda: _EXECUTION_MODE.current_mode(),
)


def scenario_identity_for_key(scenario_key: str):
    """The ONE scenario identity rule, reused by the fixture adapter: parse the
    canonical `scenarioKey` and derive the same ScenarioId the domain would."""
    parsed = scenario_layer.parse_scenario_key(scenario_key)
    scenario_id = scenario_layer.ScenarioId.derive(**parsed).value
    return scenario_id, parsed["instrument"], parsed["direction"]


# ARCH-3: the tower-owned mock authorization provider. EXPLICITLY MOCK — it
# refuses to issue a grant for any non-mock adapter, so the permissive fixture
# authorization structurally cannot survive adapter activation.
_MOCK_AUTHORIZATION = command_authorization.MockAuthorizationProvider(
    active_adapter_kind_fn=lambda: broker_layer.active_kind(),
    operator_ref_fn=_operator_id,
)

#: The account fingerprint the tower EXPECTS the node to report (`acctfp_...`).
#: Used to derive the account-identity match state for real node telemetry.
VAR_EXPECTED_ACCOUNT = "NODE_EXPECTED_ACCOUNT_FINGERPRINT"

# LIVE-3: per-request operator confirmation, threaded to the context assembler
# without widening the orchestrator's nullary factory seam. Routes set it around
# orchestrator execution; default False (unconfirmed) — fail closed.
_CONFIRMED_CTX = contextvars.ContextVar("ct_operator_confirmed", default=False)


def _live3_account_state() -> str:
    """Account-identity state for grant issuance / activation gates (derived
    from node telemetry vs the configured expectation — same authority the
    execution context uses)."""
    return _account_identity_state(_node_facts())


# LIVE-3: the DURABLE local operator authorization provider (real, non-mock).
_DURABLE_AUTHORIZATION = command_authorization.DurableOperatorAuthorizationProvider(
    store_fn=lambda: _execution_store(),
    active_adapter_kind_fn=lambda: broker_layer.active_kind(),
    account_state_fn=_live3_account_state,
    profile_ok_fn=lambda: connection_policy.active_profile()
        in connection_policy.APPROVED_PROFILES,
    now_iso_fn=lambda: _now_iso(),
)


def _manual_live_gates() -> dict:
    """The manual_live activation gate set — every gate a named, derived fact."""
    now = datetime.now(timezone.utc)
    node = _node_facts()
    brk = None
    caps = {}
    try:
        brk = broker_layer.get_broker()
        caps = broker_layer.capability_dict(brk.capabilities())
    except Exception:
        pass
    recon = reconciliation_layer.safety_posture(_execution_store())
    grant = _DURABLE_AUTHORIZATION.applicable_grant(
        now=now, account_fingerprint=(os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip() or None,
        adapter_kind=broker_layer.active_kind())
    return {
        "operatorAuthenticated": (not _AUTH_POLICY.enabled) or _AUTH_POLICY.enforcing,
        "approvedConnectionProfile": connection_policy.active_profile()
            in connection_policy.APPROVED_PROFILES,
        "adapterIsMt5": broker_layer.active_kind() == "mt5",
        "adapterWriteCapable": bool(caps.get("supportsLiveWrite")),
        "adapterConnected": bool(brk and brk.connection().state == "Connected"),
        "accountIdentityMatch": _live3_account_state() == execution_safety.ACCOUNT_MATCH,
        "nodeTelemetryFresh": node.provenance == execution_context_layer.PROV_NODE_TELEMETRY
                              and not node.stale,
        "reconciliationClean": not recon["criticalUnresolved"] and not recon["stale"],
        "validScopedGrant": grant is not None,
        "executionStoreAvailable": _execution_store() is not None,
    }


# LIVE-3: the ONE durable execution-mode owner. No strategy code path calls its
# transition API; no environment variable feeds it.
_EXECUTION_MODE = execution_mode_layer.ExecutionModeOwner(
    store_fn=lambda: _execution_store(),
    now_iso_fn=lambda: _now_iso(),
    gates_fn=_manual_live_gates,
)


def _node_facts() -> execution_context_layer.NodeFacts:
    """Node-authoritative facts OBSERVED from the newest validated telemetry
    snapshot (durable + hot cache). The tower derives staleness from the node's
    own published timestamp; health/arming/account are carried verbatim. Absent
    telemetry returns the absent/deny representation."""
    now = datetime.now(timezone.utc)
    records = _load_live_snapshots()
    records.update({k: v for k, v in _LIVE_STATUS.items()
                    if isinstance(v, dict) and isinstance(v.get("snapshot"), dict)})
    if not records:
        return execution_context_layer.NodeFacts()
    entries = [_live_status_entry(iid, rec, now) for iid, rec in records.items()]
    # Newest by the node's own publish time.
    entry = max(entries, key=lambda e: e.get("published_at") or "")
    snap = entry.get("snapshot") or {}
    arming = snap.get("arming") or {}
    account = snap.get("account") or {}
    identity = (account.get("identity") or {})
    stale = bool(entry.get("stale", True))
    return execution_context_layer.NodeFacts(
        instance_id=entry.get("instance_id"),
        health=(execution_safety.NODE_HEALTHY if not stale else execution_safety.NODE_STALE),
        published_at=entry.get("published_at"),
        age_seconds=entry.get("age_seconds"),
        stale=stale,
        arming_status=arming.get("status"),
        arming_armed=arming.get("armed"),
        arming_expires_at=arming.get("expires_at"),
        account_fingerprint=identity.get("fingerprint"),
        provenance=execution_context_layer.PROV_NODE_TELEMETRY,
    )


def _account_identity_state(node: execution_context_layer.NodeFacts) -> str:
    """Derive the account-identity match state. Mock world: the mock account is
    its own expectation (labelled mock provenance). Real telemetry: compare the
    node-reported fingerprint against the configured expectation; missing either
    side is UNKNOWN (which denies risk-relevant execution); a difference is a
    MISMATCH (which denies everything non-read-only)."""
    if broker_layer.active_kind() == "mock" and node.provenance != execution_context_layer.PROV_NODE_TELEMETRY:
        return execution_safety.ACCOUNT_MATCH        # mock-vs-mock, explicitly synthetic
    expected = (os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip()
    reported = node.account_fingerprint
    if not expected or not reported:
        return execution_safety.ACCOUNT_UNKNOWN
    if node.stale:
        return execution_safety.ACCOUNT_UNKNOWN     # stale facts cannot authorize
    return (execution_safety.ACCOUNT_MATCH if reported == expected
            else execution_safety.ACCOUNT_MISMATCH)


def _financial_rails_factory(instrument: str | None):
    """UES — the per-command financial-rail evaluator for a LIVE adapter.

    A one-line delegation on purpose. Every `live.*` import lives behind
    `manual_financial_rails`, so this composition root names one module and the
    adapter boundary (structurally enforced: `server.py` may not import from
    `live.`) stays intact.
    """
    import manual_financial_rails
    return manual_financial_rails.factory_for(
        datetime.now(timezone.utc))(instrument)


def _execution_context() -> execution_context_layer.ExecutionContext:
    """The canonical ExecutionContext, assembled ONCE at the execution boundary.
    The safety gate and the orchestrator both consume this one assembly.

    ARCH-3: every fact group records its PROVENANCE. The mock world's context is
    EXPLICITLY LABELLED mock: its operator authorization is a grant issued by the
    MockAuthorizationProvider (which refuses non-mock adapters), and its synthetic
    node health carries `mock-synthetic` provenance. Against any non-mock adapter
    no grant exists and the context denies by default — the permissive fixture
    context structurally cannot survive adapter activation.

    Node-authoritative facts stay observed (I-7): when real telemetry exists it is
    carried verbatim with derived staleness; the tower never invents node health
    or arming.
    """
    brk = broker_layer.get_broker()
    caps = execution_context_layer.capabilities_from_mapping(
        broker_layer.capability_dict(brk.capabilities()))
    recon = reconciliation_layer.safety_posture(_execution_store())
    recon_facts = execution_context_layer.ReconciliationFacts(
        critical_unresolved=recon["criticalUnresolved"], stale=recon["stale"],
        last_run_id=recon["lastRunId"], last_run_at=recon["lastRunAt"])
    now = datetime.now(timezone.utc)
    grant = _MOCK_AUTHORIZATION.current_grant(now)   # None for any non-mock adapter

    real_node = _node_facts()
    if broker_layer.active_kind() != "mock":
        # LIVE-3: the live context consumes the DURABLE governed facts —
        # execution mode from the one mode owner (observe unless an operator
        # explicitly activated manual_live this process), the applicable grant
        # from the durable provider (deterministic selection; None denies), and
        # per-request operator confirmation (context-var; default False).
        expected_fp = (os.environ.get(VAR_EXPECTED_ACCOUNT) or "").strip() or None
        durable_grant = _DURABLE_AUTHORIZATION.applicable_grant(
            now=now, account_fingerprint=expected_fp,
            adapter_kind=broker_layer.active_kind())
        return execution_context_layer.ExecutionContext(
            execution_mode=_EXECUTION_MODE.current_mode(),
            operator=execution_safety.OperatorAuthorization(
                operator_ref=_operator_id(),
                confirmed=bool(_CONFIRMED_CTX.get())),
            authorization=durable_grant,
            reconciliation=recon_facts,
            account_identity_state=_account_identity_state(real_node),
            node=real_node,
            broker_kind=broker_layer.active_kind(),
            broker_connection=brk.connection().state,
            broker_capabilities=caps,
            provenance=(("tower", "durable-governance"),
                        ("node", real_node.provenance),
                        ("broker", broker_layer.active_kind()),
                        ("reconciliation", execution_context_layer.PROV_DURABLE_STORE)),
            observed_at=_now_iso(),
            # UES: a non-mock adapter can reach a real terminal, so manual
            # commands are judged by the SAME financial rails the autonomous
            # runner uses. Injected only here: the mock world has no live state,
            # no kill file and no account to protect.
            financial_rails_factory=_financial_rails_factory,
        )

    # Mock world: real telemetry is used when a node has actually published;
    # otherwise the synthetic mock node facts are used and labelled as such.
    node = real_node if real_node.provenance == execution_context_layer.PROV_NODE_TELEMETRY \
        else execution_context_layer.NodeFacts(
            instance_id="mock-fixture",
            health=execution_safety.NODE_HEALTHY,
            stale=False,
            provenance=execution_context_layer.PROV_MOCK,
        )
    return execution_context_layer.ExecutionContext(
        source="operator",
        execution_mode=execution_safety.MODE_ACTIVE,
        operator=execution_safety.OperatorAuthorization(operator_ref=_operator_id(),
                                                        confirmed=True),
        authorization=grant,
        reconciliation=recon_facts,
        account_identity_state=_account_identity_state(node),
        node=node,
        broker_kind="mock",
        broker_connection=brk.connection().state,
        broker_capabilities=caps,
        provenance=(("tower", "mock-authorization"),
                    ("node", node.provenance),
                    ("broker", "mock-fixture"),
                    ("reconciliation", execution_context_layer.PROV_DURABLE_STORE)),
        observed_at=_now_iso(),
    )


def _safety_context() -> execution_safety.SafetyContext:
    """The safety-gate view of the canonical execution context. Kept as a named
    seam (and for its ARCH-1 guarantees); it derives from `_execution_context()`
    so there is exactly ONE assembly of safety-relevant facts."""
    return _execution_context().to_safety_context()


_EXEC_METRICS = execution_layer.ExecutionMetrics()
_ORCHESTRATOR = execution_layer.ExecutionOrchestrator(
    dispatch=_dispatch_command, env_factory=_execution_env, metrics=_EXEC_METRICS,
    safety_context_factory=_execution_context,
    store_factory=_execution_store,
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
# M-ENV-1: the variable is read through the environment module so exactly one
# module parses it. The `"fixture"` fallback is a DEVELOPMENT default and is
# unreachable in production, where an unset provider already aborted startup.
_MD_PROVIDER = environment_layer.configured_market_data_provider() or "fixture"
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
# M-ENV-1: an inadmissible provider is not merely left inactive — it is never
# REGISTERED, so no later `set_active` call, diagnostic, or future code path can
# reach it. In development the admissible set is every known provider, so the
# registration order and the active default are byte-for-byte what they were.
for _provider in (
    market_data_layer.FixtureProvider(_fixture_regime, candle_source=_service_candles),
    market_data_layer.ReplayProvider(
        _fixture_regime, _replay_session_for, candle_source=_service_candles),
    market_data_layer.MockLiveProvider(_fixture_regime),
    market_data_layer.MT5MarketDataProvider(
        _fixture_regime, available_symbols=_MT5_SYMBOLS, aliases=_MT5_ALIASES,
        enabled=_MT5_MD_ENABLED),
):
    if _provider.provider_id in _ENV_POLICY.market_data_providers:
        _MARKET_DATA_ENGINE.register(
            _provider, active=_provider.provider_id == market_data_layer.FixtureProvider.provider_id)
# Config-driven active selection (no automatic switching). `set_active` returns
# False for an unregistered provider, leaving the registered default in place —
# in production only admissible providers exist, so there is nothing to fall
# back TO that startup validation has not already approved.
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
async def health(request: Request):
    """Truthful PROCESS health (UI-0) — deliberately NOT trading readiness.

    The old response returned the frozen fixture `meta.asOf` as `asOf`, which read
    as a freshness timestamp and was not one. Server time is now the real clock;
    the fixture's timestamp is still reported but nested under `fixture` where its
    meaning is unambiguous. `brokerKind` is the ACTIVE broker implementation (mock
    today) and must never be read as MT5 connectivity — `liveNodeConnected` is the
    only statement about a real execution node, and it is true only when a node has
    actually published telemetry to this process."""
    # ARCH-3: /api/health is the ONLY public route. When operator authentication
    # is enforcing and the caller presents no valid credential, the body shrinks to
    # minimum safe liveness — no node instance ids, no broker kind, no fixture or
    # contract versions, no telemetry hints. The rich body requires the credential
    # (or a deliberately-unauthenticated local deployment, where auth is off).
    if _AUTH_POLICY.enforcing and not _AUTH_POLICY.verify(
            request.headers.get(auth_policy.AUTH_HEADER)):
        return {"status": "ok", "scope": "process", "serverTime": _now_iso()}
    node_instances = sorted(_LIVE_STATUS)
    live_node_connected = bool(node_instances)
    broker_kind = broker_layer.active_kind()
    # ARCH-2: tradingReady is DERIVED from the canonical readiness gates, never a
    # constant. In the mock-only world it is False because the named gates
    # (liveAdapterActive among them) are genuinely not met.
    try:
        gates = execution_telemetry_layer.readiness_gates(
            adapter_kind=broker_kind,
            adapter_connection=broker_layer.get_broker().connection().state,
            reconciliation=reconciliation_layer.safety_posture(_execution_store()),
            node_healthy=live_node_connected,
            store_available=_execution_store() is not None,
        )
        trading_ready = execution_telemetry_layer.trading_ready(gates)
    except Exception:                     # a health probe must never 500 on derivation
        trading_ready = False
    return {
        "status": "ok",
        "scope": "process",              # process liveness, not trading readiness
        "serverTime": _now_iso(),        # real clock — never the fixture asOf
        # M-ENV-1: the resolved deployment environment. Additive and unmistakable
        # — a development process says so, on the surface the UI already reads.
        "environment": ENVIRONMENT,
        # DERIVED, not asserted: `backendMode` was the constant "fixture", which
        # would be a fabrication in a process where the fixture world is not
        # active at all.
        "backendMode": "fixture" if WORLD.available else "runtime",
        "brokerKind": broker_kind,       # active broker impl; "mock" != MT5 connected
        "liveNodeConnected": live_node_connected,
        "tradingReady": trading_ready,   # ARCH-2: derived from named gates (False in the mock world)
        "dataSources": {
            "world": "fixture" if WORLD.available else "unavailable",
            "broker": "mock" if broker_kind == "mock" else broker_kind,
            "nodeTelemetry": "available" if live_node_connected else "unavailable",
        },
        "nodeTelemetry": {
            "connected": live_node_connected,
            "instances": node_instances,
        },
        "fixture": {
            "available": bool(WORLD),
            "version": WORLD.get("meta", {}).get("fixtureVersion"),
            "contractVersion": WORLD.get("meta", {}).get("contractVersion"),
            "asOf": WORLD.get("meta", {}).get("asOf"),   # the FIXTURE's time, not now
        },
    }


@api_router.get("/world")
async def world():
    """Return the full frozen world fixture. Used by the fixture provider fallback."""
    if not _fixture_available():
        return _fixture_unavailable("world")
    return WORLD.as_dict()


@api_router.get("/fleet")
async def fleet():
    if not _fixture_available():
        return _fixture_unavailable("fleet")
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
    if not _fixture_available():
        return _fixture_unavailable("packages")
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


#: Broker READ surfaces whose empty result is only truthful when the adapter is
#: actually connected.
#:
#: AUDIT FINDING (this milestone): with the MT5 adapter selected and
#: DISCONNECTED, `/api/broker/positions` answered `200 []`. A dashboard reads
#: that as "broker connected, no open positions"; the truth is "unreachable — we
#: do not know". The previous classification called this "genuine broker truth",
#: which was wrong: it is Class B (UNAVAILABLE) reported as Class A (TRUTHFUL
#: EMPTY). An empty position list is one of the most consequential lies this API
#: can tell, because it is indistinguishable from flat.
def _broker_read_unavailable(surface: str, connection) -> JSONResponse:
    """503 for a broker read that cannot be answered truthfully."""
    return JSONResponse(
        status_code=503,
        headers={"Cache-Control": "no-store"},
        content={
            "error": "unavailable",
            "code": "broker_unavailable",
            "surface": surface,
            "adapter": broker_layer.active_kind(),
            "connection": getattr(connection, "state", "Unknown"),
            "detail": (
                "the broker adapter is not connected, so this surface cannot be "
                "reported. This is NOT a claim that the account is flat or that "
                "there are no open positions — the broker was not reachable. "
                + (getattr(connection, "detail", "") or "")).strip(),
        })


def _broker_connection():
    try:
        return broker_layer.get_broker().connection()
    except Exception:                                           # noqa: BLE001
        return None


def _broker_is_connected(connection) -> bool:
    return getattr(connection, "state", None) == "Connected"


@api_router.get("/broker/accounts")
async def broker_accounts():
    connection = _broker_connection()
    if not _broker_is_connected(connection):
        return _broker_read_unavailable("broker/accounts", connection)
    brk = broker_layer.get_broker()
    return brk.accounts(_broker_context({}, _now_iso()))


@api_router.get("/broker/positions")
async def broker_positions():
    connection = _broker_connection()
    if not _broker_is_connected(connection):
        return _broker_read_unavailable("broker/positions", connection)
    brk = broker_layer.get_broker()
    return brk.positions(_broker_context({}, _now_iso()))


@api_router.get("/broker/orders")
async def broker_orders():
    connection = _broker_connection()
    if not _broker_is_connected(connection):
        return _broker_read_unavailable("broker/orders", connection)
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


# ARCH-2: synthetic fault injection is TEST-ONLY and DISABLED BY DEFAULT. The
# audit found this route could permanently corrupt reconciliation truth in a
# production process; it now 404s unless an operator explicitly enables it. The
# flag is read per-request so tests can enable it without a restart.
_FAULT_INJECTION_VAR = "BROKER_FAULT_INJECTION_ENABLED"


def _fault_injection_enabled() -> bool:
    raw = os.environ.get(_FAULT_INJECTION_VAR, "")
    return isinstance(raw, str) and raw.strip().lower() in ("1", "true", "yes", "on")


def _fault_injection_gate() -> None:
    if not _fault_injection_enabled():
        raise HTTPException(status_code=404, detail={
            "error": "disabled", "code": "fault_injection_disabled",
            "detail": "synthetic broker fault injection is test-only and disabled by default"})


@api_router.get("/broker/faults")
async def broker_get_faults():
    """TEST-ONLY fault toggles (simulate broker divergence). Disabled by default."""
    _fault_injection_gate()
    return sync_layer.get_faults()


@api_router.post("/broker/faults")
async def broker_set_faults(request: Request):
    """TEST-ONLY: set/clear simulated broker faults. Disabled by default; perturbs
    the broker snapshot only — the runtime stays authoritative."""
    _fault_injection_gate()
    try:
        patch = await request.json()
    except Exception:
        patch = {}
    if isinstance(patch, dict) and patch.get("clear"):
        return sync_layer.clear_faults()
    return sync_layer.set_faults(patch if isinstance(patch, dict) else {})


@api_router.get("/execution/state")
def execution_state():
    """ARCH-2 — the canonical execution read model (derived, read-only).

    Everything here derives from durable/canonical sources: the execution store,
    the active adapter, and the persisted reconciliation posture. The mock adapter
    is explicitly labelled; readiness is derived from named gates (never a
    constant); stale/absent/degraded stay distinct. Machine-readable failure code
    on error; never a raw 500 body."""
    try:
        store = _execution_store()
        brk = broker_layer.get_broker()
        identity = brk.account_identity()
        ctx = _execution_context()
        now_dt = datetime.now(timezone.utc)
        content = execution_telemetry_layer.build(
            store=store,
            adapter_kind=broker_layer.active_kind(),
            adapter_connection=brk.connection().state,
            adapter_provenance="mock-fixture" if broker_layer.active_kind() == "mock" else "live-capable",
            account_identity=identity.data if identity.ok else None,
            execution_mode=ctx.execution_mode,
            reconciliation_posture=reconciliation_layer.safety_posture(store),
            node_healthy=bool(_LIVE_STATUS),
            now=_now_iso(),
        )
        # ARCH-3: the full explicit gate set — every gate independently named.
        content["readiness"]["gates"].update({
            "operatorAuthenticated": _AUTH_POLICY.enforcing,
            "commandAuthorizationActive": bool(
                ctx.authorization and ctx.authorization.is_active(now_dt)),
            "accountIdentityMatch": ctx.account_identity_state == execution_safety.ACCOUNT_MATCH,
            "nodeArmingObserved": bool(ctx.node.arming_armed),
            "approvedConnectionProfile": connection_policy.active_profile()
                in connection_policy.APPROVED_PROFILES,
        })
        content["readiness"]["tradingReady"] = execution_telemetry_layer.trading_ready(
            content["readiness"]["gates"])
        content["availability"]["denialReasons"] = [
            name for name, ok in content["readiness"]["gates"].items() if not ok]
        content["context"] = ctx.safe_view()
        # LIVE-1: the live broker read block — derived at request time from the
        # active adapter's canonical reads. Explicit unavailability, no invented
        # values, provenance stated (live_mt5 vs mock-fixture).
        bctx = _broker_context({}, _now_iso())
        acct_read = brk.account_snapshot(bctx)
        recon_read = brk.reconcile_snapshot(bctx)
        exec_read = brk.recent_executions(bctx)
        content["broker"] = {
            "kind": broker_layer.active_kind(),
            "connection": brk.connection().state,
            "provenance": ("live_mt5" if broker_layer.active_kind() == "mt5"
                           else "mock-fixture"),
            # LIVE-2: the MT5 adapter carries EXACTLY ONE write capability
            # (market-order submission); readOnly derives from that fact rather
            # than being asserted. The mock remains live-write-incapable.
            "readOnly": not bool(broker_layer.capability_dict(
                brk.capabilities()).get("supportsLiveWrite")),
            "liveWriteCapable": bool(broker_layer.capability_dict(
                brk.capabilities()).get("supportsLiveWrite")),
            "account": ({"available": True, **acct_read.data}
                        if acct_read.ok and isinstance(acct_read.data, dict)
                        else {"available": False, "code": acct_read.code,
                              "detail": acct_read.detail}),
            "openPositions": (len(recon_read.data.get("positions", []))
                              if recon_read.ok else None),
            "openOrders": (len(recon_read.data.get("orders", []))
                           if recon_read.ok else None),
            "recentExecutions": (len(exec_read.data) if exec_read.ok else None),
            "reads": {
                "accountSnapshot": acct_read.code,
                "reconcileSnapshot": recon_read.code,
                "recentExecutions": exec_read.code,
            },
            "observedAt": _now_iso(),
        }
        # LIVE-2: the market-order execution read model — DERIVED entirely from
        # the durable store (provenance explicit; nothing invented; unavailable
        # states explicit).
        content["marketOrder"] = _market_order_telemetry(store)
        # LIVE-3: governed mode, authorization summary, in-flight operations,
        # entity locks — all derived, acknowledgement vs confirmation distinct.
        content["governance"] = _governance_telemetry(store, ctx, now_dt)
        return JSONResponse(content=content, headers={"Cache-Control": "no-store"})
    except Exception:
        logger.exception("execution state read model failed")
        return JSONResponse(status_code=500,
                            content={"error": "unavailable",
                                     "code": "execution_state_unavailable",
                                     "detail": None},
                            headers={"Cache-Control": "no-store"})


def _governance_telemetry(store, ctx, now_dt) -> dict:
    """LIVE-3 — the derived governance/operations read model. Every fact comes
    from the durable store or the one mode owner; acknowledged (broker said yes)
    and confirmed (reconciliation observed it) are DISTINCT."""
    out: dict = {
        "executionMode": _EXECUTION_MODE.current_mode(),
        "modeProvenance": "durable-store",
        "authorization": None,
        "operations": {"available": store is not None},
        "provenance": ("mock-fixture" if broker_layer.active_kind() == "mock"
                       else "live_mt5"),
    }
    grant = (ctx.authorization if ctx and ctx.authorization
             and ctx.authorization.is_active(now_dt) else None)
    if grant is not None:
        out["authorization"] = {"active": True, "summary": grant.safe_view(),
                                "expiresAt": grant.expires_at}
    else:
        out["authorization"] = {"active": False}
    if store is None:
        return out
    try:
        op_rows = [r for r in store.intents_by_state(limit=200)
                   if r.get("command_name") in ("ModifyPositionProtection",
                                                "CancelPendingOrder", "ClosePosition")]
        def views(rows):
            return [{"intentId": r.get("intent_id"), "command": r.get("command_name"),
                     "state": r.get("state"), "brokerRef": r.get("broker_ref"),
                     "updatedAt": r.get("updated_at")} for r in rows]
        in_flight = [r for r in op_rows if r.get("state") in
                     (order_lifecycle_layer.MODIFY_PENDING,
                      order_lifecycle_layer.CANCEL_PENDING,
                      order_lifecycle_layer.CLOSE_PENDING)]
        acknowledged = [r for r in op_rows
                        if r.get("state") == order_lifecycle_layer.ACKNOWLEDGED]
        confirmed = [r for r in op_rows if r.get("state") in
                     (order_lifecycle_layer.MODIFIED, order_lifecycle_layer.CANCELLED,
                      order_lifecycle_layer.CLOSED)]
        recon_required = [r for r in op_rows if r.get("state") in
                          (order_lifecycle_layer.UNKNOWN,
                           order_lifecycle_layer.RECONCILIATION_REQUIRED)]
        failed = [r for r in op_rows if r.get("state") in
                  (order_lifecycle_layer.REJECTED, order_lifecycle_layer.FAILED)]
        last = op_rows[0] if op_rows else None
        last_view = None
        if last is not None:
            transitions = store.transitions_of(last["intent_id"])
            final = transitions[-1] if transitions else {}
            latency = None
            try:
                latency = json.loads(final.get("evidence") or "{}").get("brokerLatencyMs")
            except (ValueError, TypeError):
                pass
            last_view = {**views([last])[0], "finalReason": final.get("reason"),
                         "latencyMs": latency}
        out["operations"] = {
            "available": True,
            "inFlight": views(in_flight),
            "awaitingAcknowledgementConfirmation": views(acknowledged),
            "reconciliationRequired": views(recon_required),
            "confirmed": len(confirmed),
            "failures": len(failed),
            "lastOperation": last_view,
            "entityLocks": [{"entityRef": l.get("entity_ref"),
                             "intentId": l.get("intent_id"),
                             "operation": l.get("operation"),
                             "acquiredAt": l.get("acquired_at")}
                            for l in store.active_entity_locks()],
            "provenance": "durable-store",
        }
    except Exception as exc:
        out["operations"] = {"available": False, "code": "operations_read_failed",
                             "detail": type(exc).__name__}
    return out


def _market_order_telemetry(store) -> dict:
    """LIVE-2 — the derived market-order read model. Every fact comes from the
    durable execution store (provenance `durable-store`); a missing store or a
    failed read is reported explicitly, never papered over with zeros."""
    if store is None:
        return {"available": False, "code": "execution_store_unavailable",
                "provenance": "durable-store"}
    try:
        rows = [r for r in store.intents_by_state(limit=200)
                if r.get("kind") == order_lifecycle_layer.KIND_SUBMIT]
        pending = [r for r in rows if r.get("state") in
                   (order_lifecycle_layer.SUBMITTING, order_lifecycle_layer.SUBMITTED)]
        awaiting_recon = [r for r in rows if r.get("state") in
                          (order_lifecycle_layer.UNKNOWN,
                           order_lifecycle_layer.RECONCILIATION_REQUIRED)]
        open_orders = [r for r in rows if r.get("state") == order_lifecycle_layer.OPEN]
        failures = [r for r in rows if r.get("state") in
                    (order_lifecycle_layer.REJECTED, order_lifecycle_layer.FAILED)]
        last = rows[0] if rows else None      # intents_by_state orders newest first
        last_view = None
        if last is not None:
            transitions = store.transitions_of(last["intent_id"])
            final = transitions[-1] if transitions else {}
            latency = None
            ack_status = None
            try:
                evidence = json.loads(final.get("evidence") or "{}")
                latency = evidence.get("brokerLatencyMs")
                ack_status = (evidence.get("ack") or {}).get("status")
            except (ValueError, TypeError):
                pass                          # evidence absent/non-JSON -> explicit None
            last_view = {
                "intentId": last.get("intent_id"),
                "instrument": last.get("instrument"),
                "side": last.get("side"),
                "quantity": last.get("quantity"),
                "state": last.get("state"),
                "brokerTicket": last.get("broker_ref"),
                "ackStatus": ack_status,
                "finalReason": final.get("reason"),
                "latencyMs": latency,
                "createdAt": last.get("created_at"),
                "updatedAt": last.get("updated_at"),
            }
        return {
            "available": True,
            "pendingSubmissions": len(pending),
            "awaitingReconciliation": len(awaiting_recon),
            "activeMarketOrders": len(open_orders),
            "submissionFailures": len(failures),
            "lastSubmission": last_view,
            "lastBrokerTicket": last_view.get("brokerTicket") if last_view else None,
            "provenance": "durable-store",
        }
    except Exception as exc:
        return {"available": False, "code": "market_order_read_failed",
                "detail": type(exc).__name__, "provenance": "durable-store"}


@api_router.post("/execution/market-order")
async def submit_market_order(request: Request) -> dict[str, Any]:
    """LIVE-2 — the ONLY submission surface for the ONE executable broker
    operation. Flows exclusively through the canonical pipeline
    (Validate -> Safety -> Resolve -> Durability -> Broker Dispatch -> Lifecycle
    -> Audit); there is no other route to `submit_market_order` and the fixture
    control plane rejects the command as unknown.

    An Idempotency-Key header is REQUIRED: a duplicated submission returns the
    original intent's durable outcome and never creates a second broker order.
    """
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    idem_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idem_key:
        raise HTTPException(status_code=422, detail={
            "status": "rejected", "stage": "received",
            "reason": "an Idempotency-Key header is required for market orders",
            "code": "idempotency_key_required"})

    command_id = f"cmd_{uuid.uuid4().hex[:20]}"
    now = _now_iso()
    # LIVE-3: thread per-request operator confirmation to the live context.
    token = _CONFIRMED_CTX.set(payload.get("confirm") is True)
    try:
        result = _ORCHESTRATOR.execute("SubmitMarketOrder", payload, now,
                                       dry_run=False, command_id=command_id,
                                       idempotency_key=idem_key)
    finally:
        _CONFIRMED_CTX.reset(token)

    intent_row = None
    if result.intent_id:
        store = _execution_store()
        if store is not None:
            try:
                intent_row = store.get_intent(result.intent_id)
            except Exception:
                intent_row = None

    if result.status in ("rejected", "denied"):
        # A refused live submission is itself an auditable fact.
        _append_event({
            "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0,
            "category": "order", "code": "SUBMIT_MARKET_ORDER_DENIED",
            "humanExplanation": (f"Market-order submission refused at stage "
                                 f"{result.stage}: {result.code}."),
            "who": _operator_id(), "causedBy": command_id,
            "before": None, "after": {"stage": result.stage, "code": result.code},
            "at": now,
        }, None)
        raise HTTPException(status_code=422, detail={
            "status": result.status, "stage": result.stage, "reason": result.reason,
            "code": result.code, "timeline": result.timeline, "stages": result.stages,
            "safety": result.safety,
        })

    ack = (result.after or {}).get("ack") if isinstance(result.after, dict) else None
    state = intent_row.get("state") if intent_row else None
    broker_ref = intent_row.get("broker_ref") if intent_row else None
    if not result.deduplicated:
        _append_event({
            "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0,
            "category": "order", "code": "SUBMIT_MARKET_ORDER_RESULT",
            "humanExplanation": (
                f"Market order {payload.get('side')} {payload.get('quantity')} "
                f"{payload.get('instrument')} -> lifecycle {state or 'unrecorded'}"
                f"{f' (ticket {broker_ref})' if broker_ref else ''}."),
            "who": _operator_id(), "causedBy": command_id,
            "before": None,
            "after": {"intentId": result.intent_id, "state": state,
                      "brokerRef": broker_ref},
            "at": now,
        }, idem_key)
        logger.info("Market order %s: intent=%s state=%s ref=%s",
                    result.status, result.intent_id, state, broker_ref)
    return {
        "ok": bool(state == order_lifecycle_layer.OPEN),
        "commandId": command_id,
        "intentId": result.intent_id,
        "lifecycleState": state,
        "brokerTicket": broker_ref,
        "acknowledgement": ack,
        "deduplicated": result.deduplicated,
        "latencyMs": result.timeline.get("brokerMs"),
        "timeline": result.timeline,
        "acceptedAt": now,
    }


async def _run_entity_operation(command: str, reference: str, request: Request,
                                ref_key: str) -> dict[str, Any]:
    """LIVE-3 — the ONE shared runner for the three manual-management routes.
    Requires an Idempotency-Key and explicit confirmation; flows exclusively
    through the canonical orchestrator; appends denial/result audit events."""
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload[ref_key] = reference

    idem_key = (request.headers.get("Idempotency-Key") or "").strip()
    if not idem_key:
        raise HTTPException(status_code=422, detail={
            "status": "rejected", "stage": "received",
            "reason": "an Idempotency-Key header is required",
            "code": "idempotency_key_required"})
    if payload.get("confirm") is not True:
        raise HTTPException(status_code=422, detail={
            "status": "rejected", "stage": "received",
            "reason": "explicit confirmation (confirm: true) is required",
            "code": "confirmation_required"})

    command_id = f"cmd_{uuid.uuid4().hex[:20]}"
    now = _now_iso()
    token = _CONFIRMED_CTX.set(True)
    try:
        result = _ORCHESTRATOR.execute(command, payload, now, dry_run=False,
                                       command_id=command_id,
                                       idempotency_key=idem_key)
    finally:
        _CONFIRMED_CTX.reset(token)

    intent_row = None
    if result.intent_id:
        store = _execution_store()
        if store is not None:
            try:
                intent_row = store.get_intent(result.intent_id)
            except Exception:
                intent_row = None

    if result.status in ("rejected", "denied"):
        _append_event({
            "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0,
            "category": "order", "code": f"{_snake_upper(command)}_DENIED",
            "humanExplanation": (f"{command} on {reference} refused at stage "
                                 f"{result.stage}: {result.code}."),
            "who": _operator_id(), "causedBy": command_id,
            "before": None, "after": {"stage": result.stage, "code": result.code},
            "at": now,
        }, None)
        raise HTTPException(status_code=422, detail={
            "status": result.status, "stage": result.stage, "reason": result.reason,
            "code": result.code, "timeline": result.timeline, "stages": result.stages,
            "safety": result.safety,
        })

    ack = (result.after or {}).get("ack") if isinstance(result.after, dict) else None
    state = intent_row.get("state") if intent_row else None
    broker_ref = intent_row.get("broker_ref") if intent_row else None
    if not result.deduplicated:
        _append_event({
            "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0,
            "category": "order", "code": f"{_snake_upper(command)}_RESULT",
            "humanExplanation": (f"{command} on {reference} -> lifecycle "
                                 f"{state or 'unrecorded'}"
                                 f"{f' (ref {broker_ref})' if broker_ref else ''}."),
            "who": _operator_id(), "causedBy": command_id,
            "before": None,
            "after": {"intentId": result.intent_id, "state": state,
                      "brokerRef": broker_ref},
            "at": now,
        }, idem_key)
    return {
        # `ok` = the broker ACKNOWLEDGED. Confirmation is reconciliation's —
        # the terminal modified/cancelled/closed state arrives only after a
        # fresh snapshot observes the change.
        "ok": state == order_lifecycle_layer.ACKNOWLEDGED
              or state in (order_lifecycle_layer.MODIFIED,
                           order_lifecycle_layer.CANCELLED,
                           order_lifecycle_layer.CLOSED),
        "commandId": command_id,
        "intentId": result.intent_id,
        "lifecycleState": state,
        "brokerRef": broker_ref,
        "acknowledgement": ack,
        "reconciliationConfirmed": state in (order_lifecycle_layer.MODIFIED,
                                             order_lifecycle_layer.CANCELLED,
                                             order_lifecycle_layer.CLOSED),
        "deduplicated": result.deduplicated,
        "latencyMs": result.timeline.get("brokerMs"),
        "acceptedAt": now,
    }


@api_router.post("/execution/positions/{reference}/protection")
async def modify_position_protection_route(reference: str, request: Request):
    """LIVE-3 — modify SL/TP of ONE position (non-risk-increasing only)."""
    return await _run_entity_operation("ModifyPositionProtection", reference,
                                       request, "positionRef")


@api_router.post("/execution/orders/{reference}/cancel")
async def cancel_pending_order_route(reference: str, request: Request):
    """LIVE-3 — cancel ONE pending order."""
    return await _run_entity_operation("CancelPendingOrder", reference,
                                       request, "orderRef")


@api_router.post("/execution/positions/{reference}/close")
async def close_position_route(reference: str, request: Request):
    """LIVE-3 — close ONE position in full."""
    return await _run_entity_operation("ClosePosition", reference,
                                       request, "positionRef")


# ── LIVE-3: durable operator authorization (issue / inspect / revoke) ─────────

@api_router.get("/authorization/grants")
def list_grants():
    grants = [g.safe_view() for g in _DURABLE_AUTHORIZATION.grants()]
    return JSONResponse(content={"grants": grants, "provider": "local-operator",
                                 "observedAt": _now_iso()},
                        headers={"Cache-Control": "no-store"})


@api_router.post("/authorization/grants")
async def issue_grant(request: Request):
    """Issue ONE durable scoped grant. Confirmation required; issuance denies
    unless the profile is approved, the adapter is MT5 and the account identity
    is verified (machine-readable reasons)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        grant = _DURABLE_AUTHORIZATION.issue(
            operator_ref=_operator_id(),
            ttl_seconds=body.get("ttlSeconds", 3600),
            scopes=set(body.get("scopes") or []),
            account_scope=str(body.get("accountScope") or ""),
            risk_classes=set(body.get("riskClasses")
                             or [execution_safety.RISK_EXECUTION_AFFECTING]),
            commands=set(body["commands"]) if body.get("commands") else None,
            max_quantity=body.get("maxQuantity"),
            reason=body.get("reason") or "",
            confirmed=body.get("confirm") is True,
            now=datetime.now(timezone.utc))
    except command_authorization.GrantIssuanceError as exc:
        return JSONResponse(status_code=422,
                            content={"error": "grant_refused", "code": exc.reason},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(content={"issued": True, "grant": grant.safe_view()},
                        headers={"Cache-Control": "no-store"})


@api_router.post("/authorization/grants/{authorization_id}/revoke")
async def revoke_grant(authorization_id: str, request: Request):
    try:
        body = await request.json()
    except Exception:
        body = {}
    try:
        found = _DURABLE_AUTHORIZATION.revoke(
            authorization_id, operator_ref=_operator_id(),
            detail=(body or {}).get("reason") or "operator revocation")
    except command_authorization.GrantIssuanceError as exc:
        return JSONResponse(status_code=422,
                            content={"error": "revocation_refused", "code": exc.reason},
                            headers={"Cache-Control": "no-store"})
    if not found:
        return JSONResponse(status_code=404,
                            content={"error": "grant_not_found", "code": "grant_not_found"},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(content={"revoked": True, "authorizationId": authorization_id},
                        headers={"Cache-Control": "no-store"})


# ── LIVE-3: execution-mode governance (view / transition) ─────────────────────

@api_router.get("/execution/mode")
def get_execution_mode():
    return JSONResponse(content={
        "mode": _EXECUTION_MODE.current_mode(),
        "history": _EXECUTION_MODE.history(limit=20),
        "manualLiveGates": _manual_live_gates(),
        "observedAt": _now_iso(),
    }, headers={"Cache-Control": "no-store"})


@api_router.post("/execution/mode")
async def set_execution_mode(request: Request):
    """Transition the governed execution mode. Confirmation + reason required;
    manual_live requires EVERY activation gate healthy; observe/halted are
    always reachable by an authenticated, confirming operator."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    try:
        outcome = _EXECUTION_MODE.transition(
            str(body.get("mode") or ""),
            operator_ref=_operator_id(),
            reason=str(body.get("reason") or ""),
            confirmed=body.get("confirm") is True)
    except execution_mode_layer.ModeTransitionError as exc:
        return JSONResponse(status_code=422,
                            content={"error": "mode_transition_refused",
                                     "code": exc.reason, "detail": exc.detail},
                            headers={"Cache-Control": "no-store"})
    return JSONResponse(content={"transitioned": True, **outcome},
                        headers={"Cache-Control": "no-store"})


# ---------------------------------------------------------------------------
# LIVE-4A — the canonical OPERATIONAL PROJECTION surface.
#
# `_projection_sources()` binds the projection owner to the already-authoritative
# read APIs. The projection module imports nothing from here; the runtime hands
# it read-only callables, so it can never write, execute, or persist.
#
# Every route below is READ-ONLY and DERIVED: no handler aggregates anything
# itself — each one calls the projection owner and serializes the result.
# ---------------------------------------------------------------------------

def _node_observation_entries() -> list:
    """Node telemetry observation envelopes (durable + hot cache), newest first."""
    try:
        now = datetime.now(timezone.utc)
        records = _load_live_snapshots()
        records.update({k: v for k, v in _LIVE_STATUS.items()
                        if isinstance(v, dict) and isinstance(v.get("snapshot"), dict)})
        entries = [_live_status_entry(iid, rec, now) for iid, rec in records.items()]
        return sorted(entries, key=lambda e: e.get("published_at") or "", reverse=True)
    except Exception:
        logger.exception("node observation entries unavailable")
        return []


def _projection_sources() -> projection_layer.ProjectionSources:
    """Bind the projection owner to canonical read APIs. Each callable is
    individually guarded: a source that cannot be read reports UNAVAILABLE
    rather than failing the whole projection or inventing a value."""
    store = _execution_store()

    def _intents() -> list:
        try:
            return store.intents_by_state(limit=500) if store else []
        except Exception:
            return []

    def _transitions(intent_id: str) -> list:
        try:
            return store.transitions_of(intent_id) if store and intent_id else []
        except Exception:
            return []

    def _locks() -> list:
        try:
            return store.active_entity_locks() if store else []
        except Exception:
            return []

    def _latest_recon() -> dict | None:
        try:
            return store.latest_reconciliation() if store else None
        except Exception:
            return None

    def _authorization() -> dict | None:
        try:
            ctx = _execution_context()
            grant = ctx.authorization
            if grant is None or not grant.is_active(datetime.now(timezone.utc)):
                return None
            return grant.safe_view()
        except Exception:
            return None

    def _account_snapshot() -> dict | None:
        try:
            r = broker_layer.get_broker().account_snapshot(_broker_context({}, _now_iso()))
            return r.data if r.ok and isinstance(r.data, dict) else None
        except Exception:
            return None

    def _connection() -> str:
        try:
            return broker_layer.get_broker().connection().state
        except Exception:
            return "Disconnected"

    def _scenarios():
        """LIVE-4B: canonical scenarios, or None when the store is unavailable
        (explicitly unavailable — never an empty 'no scenarios' answer)."""
        sstore = _scenario_store()
        if sstore is None:
            return None
        try:
            return sstore.list_scenarios(limit=500)
        except Exception:
            return None

    return projection_layer.ProjectionSources(
        broker_snapshot=_fresh_broker_snapshot,
        account_snapshot=_account_snapshot,
        adapter_kind=lambda: broker_layer.active_kind(),
        connection_state=_connection,
        node_entries=_node_observation_entries,
        intents=_intents,
        transitions=_transitions,
        reconciliation_posture=lambda: reconciliation_layer.safety_posture(store),
        latest_reconciliation=_latest_recon,
        execution_mode=lambda: _EXECUTION_MODE.current_mode(),
        authorization=_authorization,
        entity_locks=_locks,
        scenarios=_scenarios,
    )


# ═════════════════════════════════════════════════════════════════════════════
# LIVE-5A — THE ONE RUNTIME REFRESH OWNER.
#
# Audited before building: no refresh owner existed. Every `/api/operations/*`
# handler called `_projection_sources()`, whose `broker_snapshot` binding is
# `_fresh_broker_snapshot()` — a SYNCHRONOUS broker read on the request path. The
# browser polls fourteen queries, so the broker was read fourteen times a cycle
# with no shared notion of freshness.
#
# From LIVE-5A there is exactly ONE component that reads the broker on a cadence:
# `_RUNTIME_SUPERVISOR`. The live dashboard serves its CACHED snapshot and reads
# no broker at all. The pre-existing per-request reads on the legacy operations
# routes are deliberately left alone — removing them is a behaviour change to
# LIVE-4A surfaces that this slice does not need and must not risk.
#
# CADENCE: one tick every RUNTIME_TICK_SECONDS (default 5s), single daemon
# thread, non-overlapping (see runtime_supervisor.tick_once).
# ═════════════════════════════════════════════════════════════════════════════

RUNTIME_TICK_SECONDS = float(os.environ.get("RUNTIME_TICK_SECONDS") or 5.0)
#: The loop runs by default so the dashboard is live out of the box. It only
#: READS, so enabling it cannot cause a trade; set to "0" to disable.
RUNTIME_SUPERVISOR_ENABLED = (os.environ.get("RUNTIME_SUPERVISOR_ENABLED")
                              or "1").strip() not in ("0", "false", "no", "off")
#: OFF by default. Automatic Scenario/Recommendation production is opt-in: a
#: process that starts proposing trades the moment it boots is exactly the
#: autonomy LIVE-5A must not introduce.
LIVE_PRODUCER_ENABLED = (os.environ.get("LIVE_PRODUCER_ENABLED") or "").strip() \
    in ("1", "true", "yes", "on")
#: The symbols the runtime polls. Same universe the market-data layer uses.
RUNTIME_SYMBOLS = tuple(_MT5_SYMBOLS)
RUNTIME_TIMEFRAME = "M15"


def _runtime_quote(symbol: str) -> dict | None:
    """One symbol's newest price for the runtime.

    Prefers a GENUINE tick (`live_tick`). Falls back to the provider's `quote()`
    only when no real tick exists, and marks it as derived so the projection
    never presents a fixture-derived price as a live one.
    """
    tick = _MARKET_DATA_ENGINE.live_tick(symbol)
    if isinstance(tick, dict):
        return tick
    provider = _MARKET_DATA_ENGINE.active_provider()
    if provider is None:
        return None
    quote = None
    if hasattr(provider, "quote"):
        try:
            quote = provider.quote(symbol, RUNTIME_TIMEFRAME, _now_iso())
        except Exception:
            quote = None
    if isinstance(quote, dict) and quote.get("bid") is not None:
        return {**quote, "at": quote.get("timestamp") or quote.get("at"),
                "provider": quote.get("provider") or provider.provider_id,
                "detail": "derived from provider quote; not a broker tick"}
    # MOCK / fixture path (PART 14): derive bid/ask from the provider's own
    # snapshot so MOCK and LIVE produce an IDENTICALLY SHAPED projection. The
    # `detail` and `provider` fields keep it honest — a derived price is never
    # presented as a broker tick.
    # `snapshot()` BUILDS a reading; `snapshot_dict()` only returns a cached one
    # and is None until something has built it, so the builder is what we call.
    try:
        snapshot = _MARKET_DATA_ENGINE.snapshot(symbol, RUNTIME_TIMEFRAME)
    except Exception:
        return None
    if snapshot is None:
        return None
    close = (snapshot.ohlc or {}).get("close")
    spread = snapshot.spread
    if close is None or spread is None:
        return None
    return {"symbol": symbol, "bid": round(close - spread / 2, 5),
            "ask": round(close + spread / 2, 5), "last": close,
            "spread": spread, "at": snapshot.timestamp or snapshot.asOf,
            "provider": snapshot.provider or provider.provider_id,
            "detail": "derived from fixture snapshot; not a broker tick"}


def _bar_open_iso(bar: dict) -> str | None:
    """A bar's open time as ISO-8601 UTC.

    The market-data layer returns MT5-style bars whose `time` is a UTC EPOCH
    integer, not a string. Normalizing here is what keeps every timestamp
    crossing the runtime boundary ISO UTC, so no local-time arithmetic can creep
    in downstream.
    """
    raw = bar.get("openedAt") or bar.get("timestamp") or bar.get("time")
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        try:
            return (datetime.fromtimestamp(int(raw), timezone.utc)
                    .isoformat().replace("+00:00", "Z"))
        except (ValueError, OSError, OverflowError):
            return None
    return str(raw)


def _runtime_candles(symbol: str, count: int = 3) -> list:
    """The newest COMPLETED candles, newest last, as market_runtime models.

    `market_data.candles()` already returns completed bars only; the close time
    is derived by the one rule in `market_runtime.candle_close_time`.
    """
    try:
        bars = _MARKET_DATA_ENGINE.candles(symbol, RUNTIME_TIMEFRAME,
                                          count, _now_iso()) or []
    except Exception:
        return []
    out = []
    for bar in bars:
        opened = _bar_open_iso(bar)
        closed = (bar.get("closedAt")
                  or market_runtime_layer.candle_close_time(opened, RUNTIME_TIMEFRAME))
        out.append(market_runtime_layer.SymbolCandle(
            symbol=symbol, timeframe=RUNTIME_TIMEFRAME,
            open=bar.get("open"), high=bar.get("high"),
            low=bar.get("low"), close=bar.get("close"),
            opened_at=opened, closed_at=closed,
            provider=_MARKET_DATA_ENGINE.active_provider().provider_id))
    return sorted([c for c in out if c.closed_at], key=lambda c: str(c.closed_at))


def _runtime_newest_candle(symbol: str) -> dict | None:
    bars = _runtime_candles(symbol)
    if not bars:
        return None
    newest = bars[-1]
    return {"open": newest.open, "high": newest.high, "low": newest.low,
            "close": newest.close, "openedAt": newest.opened_at,
            "closedAt": newest.closed_at, "timeframe": newest.timeframe,
            "provider": newest.provider}


def _broker_symbol_for(symbol: str) -> str | None:
    try:
        return _MARKET_DATA_ENGINE.active_provider().to_broker_symbol(symbol)
    except Exception:
        return None


_MARKET_RUNTIME = market_runtime_layer.MarketRuntime(
    symbols=RUNTIME_SYMBOLS, now_iso_fn=_now_iso,
    quote_fn=_runtime_quote, candle_fn=_runtime_newest_candle,
    connection_fn=lambda: broker_layer.get_broker().connection().state,
    provider_fn=lambda: (_MARKET_DATA_ENGINE.active_provider().provider_id
                         if _MARKET_DATA_ENGINE.active_provider() else None),
    adapter_kind_fn=lambda: broker_layer.active_kind(),
    broker_symbol_fn=_broker_symbol_for, timeframe=RUNTIME_TIMEFRAME)

_LIVE_PRODUCER = live_pipeline_layer.LivePipelineProducer(
    scenario_store_fn=_scenario_store,
    recommendation_service_fn=lambda: _RECOMMENDATION_SERVICE,
    now_iso_fn=_now_iso,
    history_fn=lambda symbol: _runtime_candles(symbol),
    node_id_fn=lambda: None,
    enabled_fn=lambda: LIVE_PRODUCER_ENABLED,
    logger=logger)

_RUNTIME_SUPERVISOR = runtime_supervisor_layer.RuntimeSupervisor(
    market=_MARKET_RUNTIME,
    broker_snapshot_fn=_fresh_broker_snapshot,
    account_fn=lambda: _guarded_account_snapshot(),
    server_time_fn=lambda: None,
    now_iso_fn=_now_iso, interval_s=RUNTIME_TICK_SECONDS,
    on_tick=lambda snapshot: _observe_tick(snapshot),
    # LIVE-5B: safe automatic recovery. `connect()` is a READ-PATH repair —
    # it opens a terminal session and places no order — so automating it cannot
    # cause a trade. Anything that could affect a position stays manual.
    reconnect_fn=lambda: _reconnect_broker(),
    logger=logger)


#: HARDEN-2: the ledger's missing ownership point. Ingestion is a READ plus a
#: write to the ledger's own store — it places no order and touches no execution
#: state — so it runs by default. Leaving it unowned is what produced six
#: endpoints serving a permanently empty ledger.
LEDGER_INGESTION_ENABLED = (os.environ.get("LEDGER_INGESTION_ENABLED")
                            or "1").strip() not in ("0", "false", "no", "off")
LEDGER_INGESTION_INTERVAL_S = float(
    os.environ.get("LEDGER_INGESTION_INTERVAL_S")
    or ledger_ingestion_layer.DEFAULT_INTERVAL_S)

_LEDGER_INGESTION = ledger_ingestion_layer.LedgerIngestionService(
    refresh_fn=lambda: refresh_trade_ledger(),
    now_iso_fn=_now_iso, interval_s=LEDGER_INGESTION_INTERVAL_S,
    enabled_fn=lambda: LEDGER_INGESTION_ENABLED, logger=logger)


def _observe_tick(snapshot) -> None:
    """Fan the runtime tick out to its observers.

    Each observer is independently guarded: a failing producer must not stop
    ledger ingestion, and neither may break the tick. The supervisor already
    runs this outside its lock and swallows exceptions; this second layer keeps
    the two observers isolated from EACH OTHER.
    """
    for name, observer in (("live_producer", _LIVE_PRODUCER.on_tick),
                           ("ledger_ingestion", _LEDGER_INGESTION.on_tick)):
        try:
            observer(snapshot)
        except Exception:                                       # noqa: BLE001
            logger.exception("runtime tick observer %s failed", name)


def _reconnect_broker() -> tuple:
    """Attempt to restore the broker link. Returns `(ok, detail)`."""
    try:
        connection = broker_layer.get_broker().connect()
        return (connection.state == "Connected",
                connection.detail or connection.state)
    except Exception as exc:                                # noqa: BLE001
        return False, type(exc).__name__


def _guarded_account_snapshot() -> dict | None:
    try:
        result = broker_layer.get_broker().account_snapshot(
            _broker_context({}, _now_iso()))
        return result.data if result.ok and isinstance(result.data, dict) else None
    except Exception:
        return None


def _projection_response(payload: dict, status_code: int = 200) -> JSONResponse:
    return JSONResponse(content=payload, status_code=status_code,
                        headers={"Cache-Control": "no-store"})


def _operational_summary() -> projection_layer.OperationalSummary:
    return projection_layer.build_summary(_projection_sources(), now=_now_iso())


@api_router.get("/operations/summary")
def operations_summary():
    """LIVE-4A — the ONE whole-system operational projection."""
    try:
        return _projection_response(_operational_summary().as_dict())
    except Exception:
        logger.exception("operational projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/nodes")
def operations_nodes():
    try:
        sources = _projection_sources()
        nodes = projection_layer.build_nodes(sources, now=_now_iso())
        return _projection_response({"nodes": [n.as_dict() for n in nodes],
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("node projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/accounts")
def operations_accounts():
    try:
        sources = _projection_sources()
        accounts = projection_layer.build_accounts(sources, now=_now_iso())
        return _projection_response({"accounts": [a.as_dict() for a in accounts],
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("account projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/orders")
def operations_orders():
    try:
        sources = _projection_sources()
        orders = projection_layer.build_orders(sources, now=_now_iso())
        return _projection_response({"orders": [o.as_dict() for o in orders],
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("order projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/positions")
def operations_positions():
    try:
        sources = _projection_sources()
        positions = projection_layer.build_positions(sources, now=_now_iso())
        return _projection_response({"positions": [p.as_dict() for p in positions],
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("position projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/node/{node_id}")
def operations_node(node_id: str):
    try:
        nodes = projection_layer.build_nodes(_projection_sources(), now=_now_iso())
        match = next((n for n in nodes if n.node_id == node_id), None)
        if match is None:
            return _projection_response(
                {"error": "not_found", "code": "node_not_projected",
                 "nodeId": node_id}, 404)
        return _projection_response(match.as_dict())
    except Exception:
        logger.exception("node projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/order/{intent_id}")
def operations_order(intent_id: str):
    try:
        orders = projection_layer.build_orders(_projection_sources(), now=_now_iso())
        match = next((o for o in orders
                      if intent_id in (o.intent_id, o.broker_order_reference)), None)
        if match is None:
            return _projection_response(
                {"error": "not_found", "code": "order_not_projected",
                 "orderId": intent_id}, 404)
        return _projection_response(match.as_dict())
    except Exception:
        logger.exception("order projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


@api_router.get("/operations/position/{reference}")
def operations_position(reference: str):
    try:
        positions = projection_layer.build_positions(_projection_sources(), now=_now_iso())
        match = next((p for p in positions
                      if p.broker_position_reference == reference), None)
        if match is None:
            return _projection_response(
                {"error": "not_found", "code": "position_not_projected",
                 "positionId": reference}, 404)
        return _projection_response(match.as_dict())
    except Exception:
        logger.exception("position projection failed")
        return _projection_response(
            {"error": "unavailable", "code": "projection_unavailable"}, 503)


# ── LIVE-4B: the canonical Scenario read surface (read-only, derived) ────────

def _scenario_views(*, active_only: bool = False, instrument: str | None = None,
                    session: str | None = None, node_id: str | None = None,
                    status: str | None = None) -> tuple:
    """Project scenarios through the ONE projection owner. Filtering happens at
    the store (deterministic ordering) and the projection shapes the view."""
    sstore = _scenario_store()
    if sstore is None:
        return None
    statuses = None
    if active_only:
        statuses = tuple(sorted(scenario_layer.ACTIVE_STATUSES))
    elif status:
        statuses = (status,)
    scenarios = sstore.list_scenarios(statuses=statuses, instrument=instrument,
                                      session=session, node_id=node_id, limit=500)
    sources = projection_layer.ProjectionSources(scenarios=lambda: scenarios)
    return projection_layer.build_scenarios(sources, now=_now_iso())


def _scenario_unavailable() -> JSONResponse:
    return _projection_response(
        {"error": "unavailable", "code": "scenario_store_unavailable",
         "scenarios": [], "projectionTimestamp": _now_iso()}, 503)


@api_router.get("/scenarios")
def list_scenarios(instrument: str | None = None, session: str | None = None,
                   nodeId: str | None = None, status: str | None = None):
    """All scenarios, newest first. Read-only and derived."""
    try:
        views = _scenario_views(instrument=instrument, session=session,
                                node_id=nodeId, status=status)
        if views is None:
            return _scenario_unavailable()
        sstore = _scenario_store()
        return _projection_response({
            "scenarios": [v.as_dict() for v in views],
            "summary": sstore.summary().as_dict() if sstore else None,
            "projectionTimestamp": _now_iso(),
        })
    except Exception:
        logger.exception("scenario listing failed")
        return _scenario_unavailable()


@api_router.get("/scenarios/active")
def list_active_scenarios():
    """Only scenarios in a non-terminal status."""
    try:
        views = _scenario_views(active_only=True)
        if views is None:
            return _scenario_unavailable()
        return _projection_response({"scenarios": [v.as_dict() for v in views],
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("active scenario listing failed")
        return _scenario_unavailable()


@api_router.get("/scenarios/history")
def scenario_history(scenarioId: str | None = None, limit: int = 200):
    """The append-only scenario event history (one scenario, or the newest
    events across all scenarios). Immutable facts only."""
    try:
        sstore = _scenario_store()
        if sstore is None:
            return _scenario_unavailable()
        events = (sstore.history(scenarioId, limit=limit) if scenarioId
                  else sstore.all_events(limit=limit))
        return _projection_response({
            "scenarioId": scenarioId,
            "events": [e.as_dict() for e in events],
            "projectionTimestamp": _now_iso(),
        })
    except Exception:
        logger.exception("scenario history failed")
        return _scenario_unavailable()


@api_router.get("/scenarios/{scenario_id}")
def get_scenario(scenario_id: str):
    """One scenario, with its append-only history."""
    try:
        sstore = _scenario_store()
        if sstore is None:
            return _scenario_unavailable()
        scenario = sstore.get_scenario(scenario_id)
        if scenario is None:
            return _projection_response(
                {"error": "not_found", "code": "scenario_not_found",
                 "scenarioId": scenario_id}, 404)
        sources = projection_layer.ProjectionSources(scenarios=lambda: [scenario])
        view = projection_layer.build_scenarios(sources, now=_now_iso())[0]
        return _projection_response({
            **view.as_dict(),
            "history": [e.as_dict() for e in sstore.history(scenario_id)],
        })
    except Exception:
        logger.exception("scenario detail failed")
        return _scenario_unavailable()


# ── LIVE-4C: the canonical Trade Ledger (read-only surface + read-side service) ─

def _broker_history_snapshot(*, days: int = 7) -> broker_history_layer.BrokerHistorySnapshot:
    """One read of broker history through the ACTIVE adapter. Read-only: this
    never submits, routes or mutates anything."""
    now_iso = _now_iso()
    try:
        kind = broker_layer.active_kind()
        brk = broker_layer.get_broker()
        if kind == "mt5":
            gateway = getattr(brk, "_gateway", None)
            if gateway is None:
                return broker_history_layer.unavailable_snapshot(
                    at=now_iso, detail="MT5 gateway unavailable",
                    provenance=broker_history_layer.PROV_LIVE_MT5)
            end = datetime.now(timezone.utc)
            return broker_history_layer.read_mt5_history(
                gateway, at=now_iso, window_from=end - timedelta(days=days),
                window_to=end, symbol_to_canonical=brk.to_canonical)
        # Mock/fixture world: deterministic history from closed fixture trades.
        closed = [t for t in _live_trades_view()
                  if t.get("closedAt") or t.get("closePrice")]
        snap = _fresh_broker_snapshot() or {}
        open_ids = [str(p.get("positionId")) for p in (snap.get("positions") or [])
                    if p.get("positionId")]
        return broker_history_layer.read_mock_history(
            closed, at=now_iso,
            account_currency=(snap.get("accounts") or [{}])[0].get("baseCurrency"),
            account_fingerprint=snap.get("accountIdentity"),
            open_position_ids=open_ids)
    except Exception:
        logger.exception("broker history read failed")
        return broker_history_layer.unavailable_snapshot(
            at=now_iso, detail="broker history read failed")


def refresh_trade_ledger(*, days: int = 7) -> dict:
    """LIVE-4C read-side ingestion service.

    Deliberately NOT an HTTP mutation endpoint and NOT an autonomous loop: it
    is a narrowly scoped internal service, callable from tests and future
    orchestration. It performs broker READS only and writes ledger facts —
    never execution state, never a broker command."""
    store = _ledger_store()
    if store is None:
        return {"ingested": 0, "available": False, "code": "ledger_store_unavailable"}
    history = _broker_history_snapshot(days=days)
    if not history.usable:
        return {"ingested": 0, "available": False,
                "code": "broker_history_unavailable", "detail": history.detail}
    # A read can SUCCEED and still be incomplete. Persisting a truncated window
    # is the failure this guard exists for: the ledger would gain a plausible
    # partial history, and a watermark advanced past the discarded deals would
    # make the loss permanent and undetectable. Reporting nothing ingested is
    # recoverable; reporting a partial window as ingested is not.
    if not history.ingestable:
        return {"ingested": 0, "available": True, "complete": False,
                "code": "broker_history_incomplete",
                "detail": (history.incomplete_reason
                           or "history read could not be proven exhaustive"),
                "readEvidence": history.read_evidence}
    exec_store = _execution_store()
    scenario_store = _scenario_store()
    try:
        intents = exec_store.intents_by_state(limit=1000) if exec_store else []
        transitions = {row.get("intent_id"): exec_store.transitions_of(row.get("intent_id"))
                       for row in intents} if exec_store else {}
    except Exception:
        intents, transitions = [], {}
    try:
        scenarios = scenario_store.list_scenarios(limit=500) if scenario_store else []
    except Exception:
        scenarios = []
    try:
        latest = exec_store.latest_reconciliation() if exec_store else None
        recon_items = [i for i in ((latest or {}).get("items") or [])
                       if not i.get("resolved")]
    except Exception:
        recon_items = []
    results = reconstruction_layer.reconstruct(
        reconstruction_layer.ReconstructionInput(
            history=history, intents=tuple(intents), scenarios=tuple(scenarios),
            reconciliation_items=tuple(recon_items), transitions=transitions,
            node_id=_node_facts().instance_id, adapter=broker_layer.active_kind(),
            broker=history.provenance, deployment=None))
    entries = store.ingest_broker_history(results, now=_now_iso(),
                                          provenance=history.provenance)
    return {"ingested": len(entries), "available": True, "complete": True,
            "accountMode": history.account_mode,
            "readyToFinalize": sum(1 for e in entries
                                   if e.status == ledger_domain.TradeLedgerStatus.READY_TO_FINALIZE)}


def _ledger_filters(request: Request) -> dict:
    q = request.query_params
    return {"instrument": q.get("instrument"), "scenario_id": q.get("scenarioId"),
            "node_id": q.get("nodeId"),
            "account_fingerprint": q.get("accountFingerprint"),
            "opened_from": q.get("openedFrom"), "opened_to": q.get("openedTo"),
            "closed_from": q.get("closedFrom"), "closed_to": q.get("closedTo")}


MAX_LEDGER_PAGE = 200


def _ledger_unavailable(code: str = "ledger_store_unavailable") -> JSONResponse:
    view = projection_layer.build_trade_ledger(None, now=_now_iso(), code=code)
    return _projection_response(view.as_dict(), 503)


@api_router.get("/ledger/summary")
def ledger_summary():
    """Ledger TOTALS only — no analytics."""
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        view = projection_layer.build_trade_ledger(
            [], store.summary(), now=_now_iso(),
            provenance=projection_layer.PROV_DURABLE_STORE)
        return _projection_response({"summary": view.summary.as_dict(),
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("ledger summary failed")
        return _ledger_unavailable("ledger_summary_failed")


@api_router.get("/ledger/trades")
def ledger_trades(request: Request, status: str | None = None,
                  limit: int = 50, offset: int = 0):
    """Deterministically ordered, filtered, bounded page of ledger entries."""
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        page = max(1, min(int(limit), MAX_LEDGER_PAGE))
        filters = _ledger_filters(request)
        statuses = (status,) if status else None
        entries = store.list_trades(statuses=statuses, limit=page,
                                    offset=max(0, int(offset)), **filters)
        total = store.count_trades(statuses=statuses, **filters)
        view = projection_layer.build_trade_ledger(
            entries, store.summary(), now=_now_iso(), total_count=total)
        return _projection_response({**view.as_dict(), "limit": page,
                                     "offset": max(0, int(offset)),
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("ledger trade listing failed")
        return _ledger_unavailable("ledger_listing_failed")


@api_router.get("/ledger/incomplete")
def ledger_incomplete():
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        entries = store.list_trades(
            statuses=(ledger_domain.TradeLedgerStatus.INCOMPLETE,), limit=MAX_LEDGER_PAGE)
        view = projection_layer.build_trade_ledger(entries, now=_now_iso())
        return _projection_response({**view.as_dict(),
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("ledger incomplete listing failed")
        return _ledger_unavailable("ledger_listing_failed")


@api_router.get("/ledger/conflicts")
def ledger_conflicts():
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        view = projection_layer.build_trade_ledger(store.list_conflicts(), now=_now_iso())
        return _projection_response({**view.as_dict(),
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("ledger conflict listing failed")
        return _ledger_unavailable("ledger_listing_failed")


@api_router.get("/ledger/trades/{trade_id}")
def ledger_trade(trade_id: str):
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        entry = store.get_trade(trade_id)
        if entry is None:
            return _projection_response(
                {"error": "not_found", "code": "trade_not_found",
                 "tradeId": trade_id}, 404)
        view = projection_layer.build_trade_ledger([entry], now=_now_iso())
        return _projection_response(view.trades[0].as_dict())
    except Exception:
        logger.exception("ledger trade detail failed")
        return _ledger_unavailable("ledger_detail_failed")


@api_router.get("/ledger/trades/{trade_id}/history")
def ledger_trade_history(trade_id: str, limit: int = 200):
    """The append-only ledger event history for one trade."""
    try:
        store = _ledger_store()
        if store is None:
            return _ledger_unavailable()
        events = store.history(trade_id, limit=max(1, min(int(limit), 500)))
        if not events:
            return _projection_response(
                {"error": "not_found", "code": "trade_not_found",
                 "tradeId": trade_id}, 404)
        return _projection_response({
            "tradeId": trade_id,
            "events": [e.as_dict() for e in events],
            "latestVersion": store.latest_version(trade_id),
            "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("ledger history failed")
        return _ledger_unavailable("ledger_history_failed")


# ── LIVE-4D: the canonical Recommendation read surface (read-only, derived) ──
#
# PATH NAMESPACE: these live under `/api/trade-recommendations/*`, NOT
# `/api/recommendations`. The audit found a pre-existing `/api/recommendations`
# route serving the fixture world's POLICY-CHANGE proposals (a different
# concept, consumed by PolicyEngineView, EdgeMonitorView and PolicyCellRenderer).
# Reusing that path would silently shadow it and break those views, so the
# canonical TRADE recommendation domain gets its own namespace and the existing
# fixture surface is left exactly as it was.
#
# LIVE-4E adds the MINIMUM write surface: accept, reject and expire. There is no
# delete, no update and no generic patch, and the operator command vocabulary
# still contains no decision verb. **Accepting a Recommendation records a
# decision and nothing else** — no order is submitted, modified or executed, and
# there remains no browser-to-broker path anywhere in this file.

MAX_RECOMMENDATION_PAGE = 200


def _recommendation_unavailable(code: str = "recommendation_store_unavailable"):
    summary = projection_layer.build_recommendation_summary(None, now=_now_iso())
    return _projection_response(
        {"error": "unavailable", "code": code, "recommendations": [],
         "summary": summary.as_dict(), "projectionTimestamp": _now_iso()}, 503)


def _recommendation_views(items):
    """Project through the ONE projection owner, with decisions and warnings
    supplied by the service — no aggregation happens in a route handler."""
    store = _recommendation_store()
    now = _now_iso()
    return projection_layer.build_recommendations(
        items, now=now,
        decisions_for=(lambda rid: store.decisions(rid)) if store else None,
        warnings_for=lambda r: _RECOMMENDATION_SERVICE.warnings_for(r, now=now))


def _recommendation_filters(request: Request) -> dict:
    q = request.query_params
    return {"scenario_id": q.get("scenarioId"), "instrument": q.get("instrument"),
            "direction": q.get("direction"), "source": q.get("source"),
            "node_id": q.get("nodeId"),
            "account_fingerprint": q.get("accountFingerprint"),
            "created_from": q.get("createdFrom"), "created_to": q.get("createdTo"),
            "linked_intent": q.get("linkedIntent")}


@api_router.get("/trade-recommendations/summary")
def recommendations_summary():
    """Counts only — no performance metrics."""
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        summary = projection_layer.build_recommendation_summary(
            store.summary(), now=_now_iso())
        return _projection_response({"summary": summary.as_dict(),
                                     "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("recommendation summary failed")
        return _recommendation_unavailable("recommendation_summary_failed")


@api_router.get("/trade-recommendations")
def list_recommendations_route(request: Request, status: str | None = None,
                               decisionType: str | None = None,
                               limit: int = 50, offset: int = 0):
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        page = max(1, min(int(limit), MAX_RECOMMENDATION_PAGE))
        filters = _recommendation_filters(request)
        statuses = (status,) if status else None
        items = store.list_recommendations(statuses=statuses, limit=page,
                                           offset=max(0, int(offset)), **filters)
        views = _recommendation_views(items)
        if decisionType:
            views = tuple(v for v in views if v.latest_decision
                          and v.latest_decision.decision_type == decisionType)
        return _projection_response({
            "recommendations": [v.as_dict() for v in views],
            "summary": projection_layer.build_recommendation_summary(
                store.summary(), now=_now_iso()).as_dict(),
            "totalCount": store.count_recommendations(statuses=statuses, **filters),
            "limit": page, "offset": max(0, int(offset)),
            "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("recommendation listing failed")
        return _recommendation_unavailable("recommendation_listing_failed")


@api_router.get("/trade-recommendations/active")
def active_recommendations_route():
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        views = _recommendation_views(store.list_active_recommendations())
        return _projection_response({
            "recommendations": [v.as_dict() for v in views],
            "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("active recommendation listing failed")
        return _recommendation_unavailable("recommendation_listing_failed")


@api_router.get("/trade-recommendations/{recommendation_id}")
def get_recommendation_route(recommendation_id: str):
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        item = store.get_recommendation(recommendation_id)
        if item is None:
            return _projection_response(
                {"error": "not_found", "code": "recommendation_not_found",
                 "recommendationId": recommendation_id}, 404)
        view = _recommendation_views([item])[0]
        return _projection_response({
            **view.as_dict(),
            "decisions": [d.safe_view() for d in store.decisions(recommendation_id)],
            "history": [e.as_dict() for e in store.history(recommendation_id)]})
    except Exception:
        logger.exception("recommendation detail failed")
        return _recommendation_unavailable("recommendation_detail_failed")


@api_router.get("/trade-recommendations/{recommendation_id}/history")
def recommendation_history_route(recommendation_id: str, limit: int = 200):
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        events = store.history(recommendation_id,
                               limit=max(1, min(int(limit), 500)))
        if not events:
            return _projection_response(
                {"error": "not_found", "code": "recommendation_not_found",
                 "recommendationId": recommendation_id}, 404)
        return _projection_response({
            "recommendationId": recommendation_id,
            "events": [e.as_dict() for e in events],
            "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("recommendation history failed")
        return _recommendation_unavailable("recommendation_history_failed")


@api_router.get("/trade-recommendations/{recommendation_id}/decisions")
def recommendation_decisions_route(recommendation_id: str):
    """The append-only decision history, with actors pseudonymized."""
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        if store.get_recommendation(recommendation_id) is None:
            return _projection_response(
                {"error": "not_found", "code": "recommendation_not_found",
                 "recommendationId": recommendation_id}, 404)
        decisions = store.decisions(recommendation_id)
        return _projection_response({
            "recommendationId": recommendation_id,
            "decisions": [d.safe_view() for d in decisions],
            "conflicts": list(recommendation_layer.conflicting_decisions(decisions)),
            "projectionTimestamp": _now_iso()})
    except Exception:
        logger.exception("recommendation decisions failed")
        return _recommendation_unavailable("recommendation_decisions_failed")


# ── LIVE-4E: the operator decision write surface ─────────────────────────────
#
# THE ONLY three mutations in the Recommendation domain. Each one records an
# operator decision in the append-only history and changes nothing else. There
# is no DELETE, no PUT and no PATCH, and no route here can reach the broker, the
# execution pipeline, the authorization plane or the ledger.

_DECISION_SERVICE = recommendation_decision_layer.RecommendationDecisionService(
    store_fn=_recommendation_store, now_iso_fn=_now_iso,
    execution_mode_fn=lambda: _EXECUTION_MODE.current_mode(),
    audit_fn=lambda event: _append_event(
        {**event, "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}", "seq": 0,
         "at": _now_iso()}, None))

#: Which HTTP status each refusal maps to. Anything unlisted is a 422 — a
#: well-formed request that the domain refused.
_DECISION_STATUS_CODES = {
    recommendation_decision_layer.REJECT_NOT_FOUND: 404,
    "recommendation_store_unavailable": 503,
}


def _decision_principal(request: Request):
    """Resolve WHO is acting, reusing the existing authentication boundary.

    The audit established there is no per-operator identity in this system: the
    auth boundary is a single shared token and `_operator_id()` is a fixture
    display value. So the caller must ASSERT an operator id, and the decision
    records how much that assertion is worth — `authenticated` when the boundary
    is enforcing, `asserted` when it is not. An anonymous request is refused
    outright, regardless of whether the global gate is switched on.
    """
    return recommendation_auth_layer.resolve_principal(
        asserted_actor=(request.headers.get(recommendation_auth_layer.ACTOR_HEADER)
                        or (request.query_params.get("actorId") or "")),
        boundary_authenticated=recommendation_auth_layer.boundary_is_authenticating(),
        correlation_id=request.headers.get(
            recommendation_auth_layer.CORRELATION_HEADER))


async def _decision_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:                                       # noqa: BLE001
        body = {}
    return body if isinstance(body, dict) else {}


def _decision_response(recommendation, decision, *, replayed: bool):
    """The decision outcome, stated so it cannot be mistaken for an execution."""
    return _projection_response({
        "recorded": True, "replayed": replayed,
        "recommendationId": recommendation.recommendation_id,
        "status": recommendation.status, "version": recommendation.version,
        "outcome": recommendation.outcome,
        "decision": decision.safe_view(),
        "executed": False,
        "notice": ("This records an operator decision only. No order was "
                   "submitted, modified or executed."),
        "projectionTimestamp": _now_iso()})


async def _record_decision_route(request: Request, recommendation_id: str,
                                 decision_type: str):
    """Shared handler: resolve principal -> validate -> record atomically."""
    body = await _decision_body(request)
    try:
        principal = _decision_principal(request)
    except recommendation_auth_layer.DecisionAuthorizationError as exc:
        return _projection_response(
            {"error": "forbidden", "code": exc.reason, "detail": exc.detail}, 403)

    raw_version = body.get("expectedVersion")
    expected_version = None
    if raw_version is not None:
        try:
            expected_version = int(raw_version)
        except (TypeError, ValueError):
            return _projection_response(
                {"error": "invalid", "code": "invalid_expected_version",
                 "detail": "expectedVersion must be an integer"}, 422)

    idem = (request.headers.get("Idempotency-Key") or "").strip() or None
    try:
        recommendation, decision = _DECISION_SERVICE.decide(
            recommendation_id, decision_type=decision_type, principal=principal,
            reason=body.get("reason") or "", note=body.get("note"),
            expected_version=expected_version, idempotency_key=idem)
    except recommendation_auth_layer.DecisionAuthorizationError as exc:
        return _projection_response(
            {"error": "forbidden", "code": exc.reason, "detail": exc.detail}, 403)
    except recommendation_decision_layer.DecisionServiceError as exc:
        status = 409 if exc.conflict else _DECISION_STATUS_CODES.get(exc.reason, 422)
        current = None
        try:
            store = _recommendation_store()
            current = store.get_recommendation(recommendation_id) if store else None
        except Exception:                                   # noqa: BLE001
            current = None
        return _projection_response({
            "error": "conflict" if exc.conflict else "rejected",
            "code": exc.reason, "detail": exc.detail, "executed": False,
            # A conflicted caller needs the CURRENT truth to retry against.
            "currentStatus": current.status if current else None,
            "currentVersion": current.version if current else None}, status)
    except Exception:                                       # noqa: BLE001
        logger.exception("recommendation decision failed")
        return _recommendation_unavailable("recommendation_decision_failed")

    replayed = bool(idem and decision.metadata.get("idempotencyKey") == idem
                    and decision.occurred_at != _now_iso())
    return _decision_response(recommendation, decision, replayed=replayed)


@api_router.post("/trade-recommendations/{recommendation_id}/accept")
async def accept_recommendation_route(request: Request, recommendation_id: str):
    """Record an operator ACCEPT.

    ACCEPTANCE IS NOT EXECUTION. This moves the proposal to ACCEPTED and writes
    one immutable decision. It submits no order, creates no intent, grants no
    authorization and changes no execution mode. Moving to INTENT_CREATED
    remains the execution boundary's job, because only it knows an intent
    exists."""
    return await _record_decision_route(
        request, recommendation_id,
        recommendation_layer.RecommendationDecisionType.ACCEPT)


@api_router.post("/trade-recommendations/{recommendation_id}/reject")
async def reject_recommendation_route(request: Request, recommendation_id: str):
    """Record an operator REJECT. `reason` is required — a rejection without a
    recorded reason is not auditable."""
    return await _record_decision_route(
        request, recommendation_id,
        recommendation_layer.RecommendationDecisionType.REJECT)


@api_router.post("/trade-recommendations/{recommendation_id}/expire")
async def expire_recommendation_route(request: Request, recommendation_id: str):
    """Record an operator EXPIRE — the proposal is stale and will not be acted
    on. Cancels no broker order and invalidates no Scenario."""
    return await _record_decision_route(
        request, recommendation_id,
        recommendation_layer.RecommendationDecisionType.EXPIRE)


# ── LIVE-5A: the live runtime surface (read-only, cached, no broker reads) ────
#
# PATH NAMESPACE: `/api/live-runtime/*`, NOT `/api/runtime/*`. The audit found a
# pre-existing `/api/runtime/health` (overlay/event-store liveness), plus
# `/api/runtime/active-package` and `/api/runtime/reset`. Reusing that namespace
# would have shadowed the existing health route — the same collision class caught
# in LIVE-4D — so the live runtime gets its own prefix and the existing family is
# left exactly as it was.

def _live_runtime_view():
    """Project the supervisor's CACHED snapshot. Performs no broker read: that
    is the supervisor's job and its alone."""
    now = _now_iso()
    totals = None
    try:
        store = _recommendation_store()
        totals = store.summary().as_dict() if store else None
    except Exception:
        totals = None
    scenario_count = None
    try:
        sstore = _scenario_store()
        if sstore is not None:
            scenario_count = len(sstore.list_active_scenarios(limit=500))
    except Exception:
        scenario_count = None
    return projection_layer.build_live_runtime(
        _RUNTIME_SUPERVISOR.last, now=now,
        health=_RUNTIME_SUPERVISOR.health(now=now),
        execution_mode=_EXECUTION_MODE.current_mode(),
        recommendation_totals=totals, scenario_count=scenario_count,
        adapter_kind=broker_layer.active_kind())


@api_router.get("/live-runtime")
def runtime_live_route():
    """THE ONE live-dashboard endpoint. Every live card is backed by this single
    response, so the whole dashboard describes one instant."""
    try:
        return _projection_response(_live_runtime_view().as_dict())
    except Exception:
        logger.exception("live runtime projection failed")
        return _projection_response(
            {"available": False, "code": "runtime_projection_failed",
             "projectionTimestamp": _now_iso()}, 503)


@api_router.get("/live-runtime/health")
def runtime_health_route():
    """Runtime health only — cheap enough to poll frequently."""
    try:
        now = _now_iso()
        return _projection_response({
            **_RUNTIME_SUPERVISOR.health(now=now).as_dict(),
            "projectionTimestamp": now})
    except Exception:
        logger.exception("runtime health failed")
        return _projection_response(
            {"state": runtime_supervisor_layer.UNKNOWN,
             "code": "runtime_health_failed"}, 503)


@api_router.post("/live-runtime/tick")
def runtime_tick_route():
    """Force ONE refresh. A read-only operation: it polls the broker and market
    exactly as the loop does and can neither submit nor modify an order. Exists
    so an operator (or the smoke workflow) can refresh without waiting a tick."""
    try:
        snapshot = _RUNTIME_SUPERVISOR.tick_once()
        return _projection_response({
            "ticked": True, "sequence": snapshot.sequence,
            "state": snapshot.health.state,
            "projectionTimestamp": snapshot.at})
    except Exception:
        logger.exception("runtime tick failed")
        return _projection_response(
            {"ticked": False, "code": "runtime_tick_failed"}, 503)


# ── LIVE-5B: integration diagnostics (read-only, no secrets) ─────────────────

def _subsystem(name: str, *, status: str, detail=None, last_success=None,
               last_failure=None, latency_ms=None, freshness=None,
               warnings=()) -> dict:
    """One subsystem row, in a fixed shape so the UI and the runbooks can rely
    on it. `status` uses the diagnostics vocabulary (pass/warn/fail/unknown)."""
    return {"name": name, "status": status, "detail": detail,
            "lastSuccess": last_success, "lastFailure": last_failure,
            "latencyMs": latency_ms, "freshness": freshness,
            "warnings": list(warnings)}


@api_router.get("/integration/diagnostics")
def integration_diagnostics_route():
    """PART 7 — one place that says which link in the chain is broken.

    Read-only and value-free: it performs no write, places no order and returns
    no credential (logins and server names are masked). Every failing check
    carries what failed, why, and the likely fix.
    """
    now = _now_iso()
    try:
        # ── the MT5 chain, in dependency order ───────────────────────────────
        gateway_diag = mt5_diagnostics_layer.diagnose(
            now=now, symbol=(RUNTIME_SYMBOLS[0] if RUNTIME_SYMBOLS else "EURUSD"),
            execution_mode=_EXECUTION_MODE.current_mode(),
            connect_fn=_diagnostic_connect, tick_fn=_diagnostic_tick)

        health = _RUNTIME_SUPERVISOR.health(now=now)
        snapshot = _RUNTIME_SUPERVISOR.last
        subsystems = [
            _subsystem(
                "gateway",
                status=(mt5_diagnostics_layer.PASS if gateway_diag.ready
                        else mt5_diagnostics_layer.FAIL),
                detail=(gateway_diag.first_blocker().detail
                        if gateway_diag.first_blocker() else "chain ready"),
                warnings=gateway_diag.blocking),
            _subsystem(
                "runtime", status=_runtime_status_word(health.state),
                detail=health.state, last_success=health.last_success_at,
                last_failure=health.last_failure_at,
                latency_ms=health.latency_ms,
                freshness=health.projection_age_seconds,
                warnings=health.warnings),
            _subsystem(
                "broker",
                status=(mt5_diagnostics_layer.PASS
                        if health.connection == "Connected"
                        else mt5_diagnostics_layer.FAIL),
                detail=health.connection, last_success=health.last_success_at,
                latency_ms=health.latency_ms),
            _subsystem(
                "projection",
                status=(mt5_diagnostics_layer.PASS if snapshot is not None
                        else mt5_diagnostics_layer.FAIL),
                detail=("cached snapshot available" if snapshot is not None
                        else "no tick recorded"),
                freshness=health.projection_age_seconds),
        ]
        subsystems.extend(_domain_subsystems())
        return _projection_response({
            "at": now, "adapter": broker_layer.active_kind(),
            "ready": gateway_diag.ready,
            "gateway": gateway_diag.as_dict(),
            "subsystems": subsystems,
            "projectionTimestamp": now})
    except Exception:
        logger.exception("integration diagnostics failed")
        return _projection_response(
            {"at": now, "ready": False, "code": "diagnostics_failed"}, 503)


def _runtime_status_word(state: str) -> str:
    if state == runtime_supervisor_layer.CONNECTED:
        return mt5_diagnostics_layer.PASS
    if state in (runtime_supervisor_layer.DEGRADED,
                 runtime_supervisor_layer.STARTING):
        return mt5_diagnostics_layer.WARN
    return mt5_diagnostics_layer.FAIL


def _diagnostic_connect() -> tuple:
    """Probe the broker link for diagnostics. A read: it opens a session and
    places no order."""
    connection = broker_layer.get_broker().connect()
    return connection.state == "Connected", connection.detail or connection.state


def _diagnostic_tick(symbol: str):
    return _runtime_quote(symbol)


def _domain_subsystems() -> list:
    """Execution, Recommendation, Scenario and Ledger readiness. Each store is
    probed independently, and an unreadable store is reported as such rather
    than as an empty one."""
    rows = []
    try:
        store = _execution_store()
        count = len(store.intents_by_state(limit=500) or []) if store else 0
        rows.append(_subsystem(
            "execution",
            status=(mt5_diagnostics_layer.PASS if store
                    else mt5_diagnostics_layer.FAIL),
            detail=(f"{count} tracked intents" if store
                    else "execution store unavailable")))
    except Exception as exc:                                # noqa: BLE001
        rows.append(_subsystem("execution", status=mt5_diagnostics_layer.FAIL,
                               detail=type(exc).__name__))
    try:
        store = _recommendation_store()
        summary = store.summary().as_dict() if store else None
        rows.append(_subsystem(
            "recommendation",
            status=(mt5_diagnostics_layer.PASS if summary
                    else mt5_diagnostics_layer.FAIL),
            detail=(f"{summary.get('activeCount', 0)} active" if summary
                    else "recommendation store unavailable")))
    except Exception as exc:                                # noqa: BLE001
        rows.append(_subsystem("recommendation", status=mt5_diagnostics_layer.FAIL,
                               detail=type(exc).__name__))
    try:
        store = _scenario_store()
        active = len(store.list_active_scenarios(limit=500)) if store else None
        rows.append(_subsystem(
            "scenario",
            status=(mt5_diagnostics_layer.PASS if active is not None
                    else mt5_diagnostics_layer.FAIL),
            detail=(f"{active} active" if active is not None
                    else "scenario store unavailable")))
    except Exception as exc:                                # noqa: BLE001
        rows.append(_subsystem("scenario", status=mt5_diagnostics_layer.FAIL,
                               detail=type(exc).__name__))
    try:
        store = _ledger_store()
        ingestion = _LEDGER_INGESTION.status
        if store is None:
            status, detail = (mt5_diagnostics_layer.FAIL,
                              "ledger store unavailable")
        elif ingestion.sweeps == 0:
            # The store opens but nothing has ever ingested. Reporting PASS here
            # is what let a permanently-empty ledger look healthy.
            status, detail = (mt5_diagnostics_layer.FAIL,
                              "ingestion has never run")
        elif ingestion.last_success_at is None:
            status, detail = (mt5_diagnostics_layer.FAIL,
                              f"{ingestion.sweeps} sweeps, none succeeded: "
                              f"{ingestion.last_failure_detail}")
        elif ingestion.last_failure_at and (
                ingestion.last_failure_at > (ingestion.last_success_at or "")):
            status, detail = (mt5_diagnostics_layer.WARN,
                              f"last sweep failed: {ingestion.last_failure_detail}")
        else:
            status, detail = (mt5_diagnostics_layer.PASS,
                              f"{ingestion.sweeps} sweeps, "
                              f"{ingestion.entries_touched_total} entries touched")
        rows.append(_subsystem(
            "ledger", status=status, detail=detail,
            last_success=ingestion.last_success_at,
            last_failure=ingestion.last_failure_at,
            warnings=([ingestion.last_failure_detail]
                      if ingestion.last_failure_detail else ())))
    except Exception as exc:                                # noqa: BLE001
        rows.append(_subsystem("ledger", status=mt5_diagnostics_layer.FAIL,
                               detail=type(exc).__name__))
    return rows


@api_router.get("/trade-recommendations/{recommendation_id}/preflight")
def recommendation_preflight_route(recommendation_id: str):
    """PART 15: every pre-trade condition, evaluated together.

    ADVISORY. The execution pipeline's own safety, authorization, mode and
    idempotency gates are untouched and run again at submission; this reports
    early and in full so a doomed submission is refused with a clear reason.
    """
    try:
        store = _recommendation_store()
        if store is None:
            return _recommendation_unavailable()
        recommendation = store.get_recommendation(recommendation_id)
        scenario = None
        if recommendation is not None:
            scenario = _scenario_reader(recommendation.scenario_id)
        now = _now_iso()

        def _intent_for(rid: str):
            execution_store = _execution_store()
            if execution_store is None:
                return None
            for intent in (execution_store.intents_by_state(limit=500) or []):
                if intent.get("recommendation_id") == rid:
                    return intent.get("intent_id")
            return None

        def _mode_allows(mode):
            # The mode owner's rule, not a re-implementation of it.
            return mode == execution_mode_layer.MODE_MANUAL_LIVE

        result = live_preflight_layer.evaluate(
            recommendation_id=recommendation_id, now=now,
            runtime_snapshot=_RUNTIME_SUPERVISOR.last,
            runtime_health=_RUNTIME_SUPERVISOR.health(now=now),
            recommendation=recommendation, scenario=scenario,
            execution_mode=_EXECUTION_MODE.current_mode(),
            mode_allows_execution=_mode_allows,
            authorization=_preflight_authorization(),
            existing_intent_for=_intent_for)
        return _projection_response({**result.as_dict(),
                                     "projectionTimestamp": now})
    except Exception:
        logger.exception("recommendation preflight failed")
        return _recommendation_unavailable("recommendation_preflight_failed")


def _preflight_authorization() -> dict | None:
    """The active authorization grant as a safe view, or None."""
    try:
        grant = _execution_context().authorization
        if grant is None or not grant.is_active(datetime.now(timezone.utc)):
            return None
        return {**grant.safe_view(), "valid": True}
    except Exception:
        return None


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
    """M-RISK-1 — the HONEST risk-limits contract.

    Rule: real configuration, real telemetry, or honestly unavailable — never
    fixture-derived. This route previously returned engine advisory defaults
    overlaid with the WORLD fixture account's `fundedRules`, which rendered
    fabricated funded-account limits as if operational. It now reads NO account,
    NO WORLD data and returns NO invented numbers.

    Field-level honesty:
      * `configured=False` / `limits=None` — no funded-account rule source is
        configured in the Control Tower today.
      * `unavailable` names exactly what is missing (external program limits).
      * genuinely ENFORCED safeguards live on the execution node (SafetyRails:
        daily-loss R, position caps) and surface via node telemetry — pointed
        to, never duplicated or invented here.
    The engine's advisory assessment ceilings (risk_engine.DEFAULT_LIMITS) are
    deliberately NOT exposed as "limits": they are Control-Tower assessment
    constants, not account or broker enforcement. The `account` query parameter
    is accepted for backward compatibility and deliberately ignored."""
    return {
        # First explicitly-versioned form of this contract. The pre-M-RISK-1
        # response was UNVERSIONED (a bare dict of advisory/fixture numbers), so
        # this starts at 1 per repository convention (ops_journal, ops_notifier,
        # resume markers all begin their versioned life at 1).
        "schemaVersion": 1,
        "configured": False,
        "source": "unconfigured",
        "limits": None,
        "unavailable": ["fundedAccountRules", "accountProgramLimits"],
        "detail": ("No funded-account rule source is configured. Node-enforced "
                   "safeguards (daily-loss, position caps) are published via "
                   "node telemetry when an execution node is connected; "
                   "external account-program limits are unavailable."),
    }


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
    if not _fixture_available():
        return _fixture_unavailable("policy_matrix")
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
    if not _fixture_available():
        return _fixture_unavailable("trades")
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
    """M-EDGE-1 — honest contract. This route previously served the WORLD
    fixture's authored track record (expectancy 0.39R, win rate 33.5%, edge
    drift "-0.02R vs research", policy health "green", research/ghost-vs-live
    comparisons). No edge or performance model exists in the Control Tower, and
    the real operational plane deliberately refuses to compute these figures:
    `operational_projection.LedgerOperationalSummary` and
    `trade_ledger_domain.LedgerSummaryTotals` both document "NO win rate, no
    expectancy, no drawdown, no equity curve". Publishing fixture versions of
    exactly those metrics created a second — and fictional — authority.

    Genuine performance must eventually derive from the authoritative trade
    ledger and real broker history; until then this reports nothing rather than
    inventing a track record an operator could size or promote against."""
    return {
        "schemaVersion": 1,
        "computed": False,
        "reason": "no_edge_performance_model",
        "detail": ("Edge performance is not computed. No edge or performance "
                   "model is implemented; genuine metrics must derive from the "
                   "trade ledger and real broker history."),
    }


@api_router.get("/system-confidence")
async def system_confidence():
    """M-CONF-1 — honest contract. The previous response served the WORLD
    fixture's authored confidence fiction (score 82, invented signal values).
    No genuine confidence model exists in the Control Tower: there is no real
    computation to expose, so no score, band, or signal list is returned.
    Rule: real data, real derivation, or honestly not computed — never fiction."""
    return {
        "schemaVersion": 1,
        "computed": False,
        "reason": "no_confidence_model",
        "detail": ("System confidence is not computed. No confidence model is "
                   "implemented; a future model must derive from genuine "
                   "operational telemetry, never authored values."),
    }


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
async def events(
    pair: str | None = None,
    since_seq: int = Query(0, ge=0),
    limit: int | None = Query(None, ge=1, le=EVENTS_PAGE_LIMIT_MAX),
):
    """Merged audit stream: frozen fixture events + retained stored events,
    ordered by monotonic seq (newest seq last). Sequence pagination (P-2):
    `since_seq` returns events with seq strictly greater (default 0 = the full
    available merged stream); `limit` caps the page, applied to the final
    merged ordered stream AFTER sequence filtering. Defaults preserve the
    pre-P-2 response exactly (bounded only by retention). This is a snapshot
    endpoint — no stale-cursor semantics; since_seq beyond head returns [].
    Filtered pagination is NOT supported: combining `pair` with `limit` is
    rejected with 422, because a pair-filtered page could come back empty
    while matching events still exist beyond it, which a seq-walking client
    cannot distinguish from exhaustion (audit defect D1)."""
    if pair is not None and limit is not None:
        raise HTTPException(
            status_code=422,
            detail="pair cannot be combined with limit; filtered pagination is not supported")
    fixtures = [e for e in WORLD.get("events", []) if e.get("seq", 0) > since_seq]
    stored = _stored_events_since(since_seq, limit)
    evs = sorted(fixtures + stored, key=lambda e: e.get("seq", 0))
    if pair:
        evs = [e for e in evs if e.get("scenarioKey") is None or e.get("scenarioKey", "").startswith(f"{pair}:")]
    if limit is not None:
        evs = evs[:limit]
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
    result = _ORCHESTRATOR.execute(name, payload, now, dry_run,
                                   command_id=command_id,
                                   idempotency_key=request.headers.get("Idempotency-Key"))
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
        # UI-9: payload redacted. No command carries a credential today, but this
        # line would log one verbatim if any ever did — keys matching the secret
        # hints are masked while the structure stays diagnosable.
        logger.info("Command %s: %s %s → event %s seq=%s", result.status, name,
                    security_config.redact_mapping(payload), event["eventId"],
                    event["seq"])
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
    The append-only event log is NOT cleared (audit history is preserved
    within the EVENTS_RETENTION_MAX retention window)."""
    count = _RUNTIME_OVERLAY.clear()
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


# ── Live-slice ingest (M3 Phase 1; P-1 transition gate) ──────────────────────
# The VPS runner publishes consolidated status here. Storage: latest payload in
# memory. Narration (P-1): BotEvents are appended ONLY for genuine transitions
# in the instance's operational identity — never per cycle. Per-cycle facts
# (boundary, intents, runner.status, execution.frozen) never narrate here;
# execution.frozen is reserved for L2's sequence-derived narration.
# Read side: /live/status for the UI/operators.
_LIVE_STATUS: dict = {}

# Per-instance in-memory transition gate (best-effort; L2 owns durable
# narration). Maps instance_id -> (signature, anchor_at). `signature` is the
# operational identity tuple over _LIVE_SIGNATURE_FIELDS; `anchor_at` is the
# producer-supplied payload `at` recorded when that signature was seeded or
# last advanced. Idempotency keys derive from the PRIOR anchor, so a retry on
# any later equivalent ingest (fresh `at`) reconstructs the identical key and
# deduplicates at the store instead of double-narrating. The gate advances
# only after every required append has inserted or confirmed-deduplicated;
# a failed append leaves (signature, anchor_at) untouched for retry. First
# observation of an instance — including after a backend restart — seeds
# silently: a first sighting has no from-state and is not a transition.
_LIVE_GATE: dict[str, tuple[tuple, Any]] = {}
_LIVE_SIGNATURE_FIELDS = ("mode", "engine_version", "deployment_profile", "data_seam")
_LIVE_CONFIG_FIELDS = ("engine_version", "deployment_profile", "data_seam")

_LIVE_MAX_INGEST_BYTES = live_telemetry.MAX_SNAPSHOT_BYTES


def _held_snapshot(instance_id: str) -> dict | None:
    """The record currently held for an instance: hot cache first, then the store."""
    held = _LIVE_STATUS.get(instance_id)
    if isinstance(held, dict) and isinstance(held.get("snapshot"), dict):
        return held
    return _load_live_snapshots().get(instance_id)


def _snapshot_is_stale(instance_id: str, incoming: dict) -> bool:
    """Is `incoming` strictly older than what is already held for this instance?

    Unparseable timestamps on either side answer False — validation already
    guarantees the incoming one parses, and refusing to store on an unreadable
    HELD value would strand the instance forever."""
    held = _held_snapshot(instance_id)
    if held is None:
        return False
    new_at = live_telemetry.parse_iso(incoming.get("published_at"))
    old_at = live_telemetry.parse_iso((held.get("snapshot") or {}).get("published_at"))
    if new_at is None or old_at is None:
        return False
    return new_at < old_at


def _live_signature(snapshot: dict) -> tuple:
    """The operational-identity tuple, read from the v1 snapshot.

    v1 nests what the pre-UI-2 payload kept flat, so each field is read from its
    v1 home. The legacy adapter fills those same homes, which is why the existing
    transition-narration contract survives the schema change unchanged."""
    runtime = snapshot.get("runtime") or {}
    engine = snapshot.get("engine") or {}
    return (
        runtime.get("mode"),
        engine.get("engine_version_actual"),
        engine.get("deployment_profile"),
        engine.get("data_seam"),
    )


@api_router.post("/live/ingest")
async def live_ingest(request: Request):
    """UI-2 — accept ONE validated node telemetry snapshot.

    The node is authoritative (I-7): everything here is transport, validation and
    storage. Nothing is recomputed, defaulted from the fixture world, or invented.
    A malformed or unsupported payload is rejected with 4xx and CANNOT overwrite
    the last valid snapshot — a broken publisher must degrade to visible staleness,
    never to plausible-looking wrong state.
    """
    # Declared length first: refusing before `body()` means an oversized POST is
    # never fully buffered. The post-read check still stands for chunked bodies
    # that declare no length.
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > _LIVE_MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="telemetry snapshot too large")
    raw = await request.body()
    if len(raw) > _LIVE_MAX_INGEST_BYTES:
        raise HTTPException(status_code=413, detail="telemetry snapshot too large")
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="telemetry rejected: malformed_json")
    try:
        snapshot, was_legacy = live_telemetry.coerce_snapshot(payload)
    except live_telemetry.TelemetryError as exc:
        detail = f"telemetry rejected: {exc.reason}"
        if exc.detail:
            detail = f"{detail} ({exc.detail})"
        raise HTTPException(status_code=400, detail=detail)

    instance_id = snapshot["instance_id"]
    received_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    record = {"snapshot": snapshot, "received_at": received_at,
              "legacy_source": was_legacy}
    # Latest-state semantics are by the NODE's own `published_at`, not by arrival
    # order. A delayed or retried publish must never replace a newer snapshot: doing
    # so can discard state the node has already reported — including a reconciliation
    # FREEZE — and make the tower show a resolved system that is actually halted.
    # Equal timestamps DO replace, so an idempotent retry is still absorbed.
    # Both the hot cache and the durable store obey the same rule, so a read can
    # never see one going backwards relative to the other.
    superseded = _snapshot_is_stale(instance_id, snapshot)
    persisted = None
    if superseded:
        logger.warning("live snapshot for %s ignored: published_at %s is older than "
                       "the snapshot already held", instance_id,
                       snapshot.get("published_at"))
        # A snapshot we refuse to store must not drive narration either: letting it
        # through would narrate a transition BACKWARDS out of stale data and advance
        # the gate, so the next genuine snapshot would narrate the same change again.
        return {"ok": True, "seq": None, "deduplicated": None}
    else:
        # Runtime state updates on EVERY accepted ingest, before and independently
        # of narration — a narration failure never prevents or rolls back this.
        _LIVE_STATUS[instance_id] = record
        persisted = _put_live_snapshot(instance_id, record)

    incoming = _live_signature(snapshot)
    payload_at = snapshot.get("published_at")
    prior = _LIVE_GATE.get(instance_id)
    if prior is None:
        # Silent first observation / restart re-seed: no event, steady response.
        _LIVE_GATE[instance_id] = (incoming, payload_at)
        return {"ok": True, "seq": None, "deduplicated": None}
    prior_sig, anchor_at = prior
    if incoming == prior_sig:
        # Steady state: no transition, no append attempted.
        return {"ok": True, "seq": None, "deduplicated": None}

    if persisted is False:
        logger.warning("live snapshot for %s held in memory only", instance_id)
    prior_map = dict(zip(_LIVE_SIGNATURE_FIELDS, prior_sig))
    new_map = dict(zip(_LIVE_SIGNATURE_FIELDS, incoming))
    cycle = snapshot.get("cycle")
    boundary = cycle.get("last_boundary") if isinstance(cycle, dict) else None
    grounding = {"instance_id": instance_id, "at": payload_at, "boundary": boundary}
    event_at = payload_at if payload_at else (
        datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"))

    # Fixed order: LIVE_MODE_CHANGED first, then the single aggregated
    # LIVE_CONFIG_CHANGED — at most two events per ingest.
    pending: list[tuple[dict, str]] = []
    if new_map["mode"] != prior_map["mode"]:
        pending.append((
            {
                "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}",
                "seq": 0,  # assigned inside _append_event
                "category": "operational",
                "code": "LIVE_MODE_CHANGED",
                "humanExplanation": (
                    f"Live instance {instance_id} changed mode from "
                    f"{prior_map['mode']} to {new_map['mode']}."),
                "scenarioKey": None,
                # UI-2: NO packageHash. The live node runs the Lux strategy core;
                # the fixture world's active package is unrelated to it, and
                # stamping it here attributed a node transition to a package the
                # node never ran. Engine lineage now travels in the snapshot's
                # `engine` section, sourced from the node itself.
                "packageHash": None,
                "who": "system",
                "causedBy": f"live_ingest:{instance_id}",
                "before": {"mode": prior_map["mode"]},
                "after": {"mode": new_map["mode"], **grounding},
                "at": event_at,
            },
            f"live|{instance_id}|LIVE_MODE_CHANGED|"
            f"{prior_map['mode']}|{new_map['mode']}|{anchor_at}",
        ))
    changed_cfg = [f for f in _LIVE_CONFIG_FIELDS if new_map[f] != prior_map[f]]
    if changed_cfg:
        # Canonical transition delta: exactly the changed fields, stable order,
        # prior and new values — one aggregated event and one key regardless of
        # how many of the three fields changed together.
        delta = ";".join(f"{f}:{prior_map[f]}→{new_map[f]}" for f in changed_cfg)
        changes_txt = ", ".join(
            f"{f} {prior_map[f]} → {new_map[f]}" for f in changed_cfg)
        pending.append((
            {
                "eventId": f"ev_{uuid.uuid4().hex[:26].upper()}",
                "seq": 0,
                "category": "operational",
                "code": "LIVE_CONFIG_CHANGED",
                "humanExplanation": (
                    f"Live instance {instance_id} configuration changed: "
                    f"{changes_txt}."),
                "scenarioKey": None,
                "packageHash": None,   # see LIVE_MODE_CHANGED above
                "who": "system",
                "causedBy": f"live_ingest:{instance_id}",
                "before": {f: prior_map[f] for f in changed_cfg},
                "after": {**{f: new_map[f] for f in changed_cfg}, **grounding},
                "at": event_at,
            },
            f"live|{instance_id}|LIVE_CONFIG_CHANGED|{delta}|{anchor_at}",
        ))

    last_seq: Any = None
    any_inserted = False
    any_processed = False
    try:
        for event, idem_key in pending:
            stored, deduplicated = _append_event(event, idem_key)
            any_processed = True
            last_seq = stored.get("seq")
            if not deduplicated:
                any_inserted = True
    except Exception:
        # Do NOT advance the gate: the same transition retries on the next
        # equivalent ingest with the identical anchor-derived keys, so any
        # event that already landed deduplicates instead of duplicating.
        logger.exception("live_ingest transition narration failed for %s", instance_id)
        return {"ok": True, "seq": last_seq,
                "deduplicated": None if not any_processed else (not any_inserted)}
    # Every required event inserted or confirmed-deduplicated: advance the gate.
    _LIVE_GATE[instance_id] = (incoming, payload_at)
    return {"ok": True, "seq": last_seq, "deduplicated": (not any_inserted)}


@api_router.get("/ops/status")
def ops_status_endpoint():
    """L1B — expose the FROZEN L1A Operational Status Model (schema_version 1).

    Pure transport pipeline: collect_sources -> build_operational_status -> return.
    The returned document is emitted verbatim — nothing is added, removed, renamed,
    merged, normalized, or reinterpreted here; `null` stays null and "unknown"
    stays "unknown". Every request performs a FRESH collection: no singleton, no
    cached model, no memoization, no request coalescing (OP-1).

    Operational degradation (missing / malformed / partial sources) is DATA, not an
    error — it is carried by sources_available, source_errors and "unknown" fields,
    so those cases return 200 with the model. Only an unexpected server failure
    yields 500, and its public detail is a STABLE, GENERIC string: the exception
    repr, type, traceback, filesystem paths and OS/decoder text stay server-side in
    the log. Sync def so the blocking reads run in the threadpool.
    """
    try:
        sources = ops_status_layer.collect_sources(
            OPS_STATE_DIR, OPS_MARKET_DATA_DIR, OPS_KILL_FILE)
        model = ops_status_layer.build_operational_status(
            sources, datetime.now(timezone.utc))
    except Exception:  # genuine server fault only; never source degradation
        logger.exception("operational status build failed")
        raise HTTPException(status_code=500, detail="operational status unavailable")
    return JSONResponse(content=model, headers={"Cache-Control": "no-store"})


def _node_beacon():
    """The node's own L1A aliveness classification, or None when unobtainable.

    This is the ONLY evidence that can make the node `disconnected` rather than
    `unknown`. In the target topology the node runs on a separate machine and its
    files are not visible here, so None is the normal answer — which is why the
    absence of a beacon must never be read as absence of the node.
    """
    try:
        sources = ops_status_layer.collect_sources(
            OPS_STATE_DIR, OPS_MARKET_DATA_DIR, OPS_KILL_FILE)
        if not sources.get("liveness"):
            return None                     # source not readable from this host
        model = ops_status_layer.build_operational_status(
            sources, datetime.now(timezone.utc))
        beacon = (model.get("process") or {}).get("aliveness")
        return beacon if isinstance(beacon, str) else None
    except Exception:                       # observability must never 500
        logger.exception("node liveness beacon unreadable")
        return None


def _live_status_entry(instance_id: str, record: dict, now: datetime) -> dict:
    """Wrap one stored record in the read-side observation envelope.

    The arrival time this endpoint already records is now PASSED IN rather than
    only attached afterwards, so freshness is judged on the tower's own clock.
    Previously it was persisted and then discarded by the freshness calculation,
    which left liveness resting on a timestamp the node chose for itself.
    """
    entry = live_telemetry.observation(record["snapshot"], now=now,
                                       received_at=record.get("received_at"))
    entry["received_at"] = record.get("received_at")
    entry["source"] = "node"
    return entry


@api_router.get("/live/status")
def live_status(instance_id: str | None = None):
    """UI-2 — the validated snapshots this backend actually received.

    Every field originates in a node snapshot that passed `live_telemetry`
    validation. Nothing is augmented from the fixture world, no package hash is
    fabricated, and an absent node yields an explicit empty state rather than
    zeros or placeholder values that would read as a quiet, healthy system.
    Freshness is computed here from the node's own `published_at` and reported
    alongside the server's observation time — the tower can only ever claim to
    know what it last observed, not what is true right now.

    Sync def so the blocking SQLite read runs in the threadpool rather than on the
    event loop (same reason as `/ops/status`). Reads only: no snapshot is mutated.
    """
    now = datetime.now(timezone.utc)
    # Memory is the hot path; the durable store makes a backend restart
    # non-destructive. Memory wins on conflict: it is at least as recent.
    records = _load_live_snapshots()
    records.update({k: v for k, v in _LIVE_STATUS.items()
                    if isinstance(v, dict) and isinstance(v.get("snapshot"), dict)})
    if instance_id:
        record = records.get(instance_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f"no telemetry for {instance_id}")
        return _live_status_entry(instance_id, record, now)
    instances = sorted(records)
    return {
        "schemaVersion": live_telemetry.SCHEMA_VERSION,
        "observedAt": now.isoformat().replace("+00:00", "Z"),
        # UI-1: no `connected` boolean. "A snapshot exists" is not a connection —
        # it ignores freshness entirely, so a node that died days ago read as
        # connected. Connection is four independent dimensions; see
        # GET /api/live/connection.
        "instances": instances,
        "statuses": {iid: _live_status_entry(iid, records[iid], now) for iid in instances},
        "emptyState": None if instances else
        "No live node has published telemetry to this Control Tower.",
    }


@api_router.get("/live/connection")
def live_connection():
    """UI-1 — the truthful connection model (read-only).

    Four independent dimensions with separate evidence, never collapsed into one
    "connected" flag. The backend dimension is deliberately ABSENT from this
    response: a backend cannot report its own unreachability, so the client
    derives it from whether this request succeeded at all.

    Nothing here is inferred from the fixture world, and nothing implies trading
    health: a running Control Tower, a published snapshot and a reachable MT5 are
    three different facts. Absent evidence yields `unknown`; only positive
    evidence of a failed read yields `unavailable`.

    Sync def so the blocking SQLite/file reads run in the threadpool.
    """
    now = datetime.now(timezone.utc)
    records = _load_live_snapshots()
    records.update({k: v for k, v in _LIVE_STATUS.items()
                    if isinstance(v, dict) and isinstance(v.get("snapshot"), dict)})
    return JSONResponse(
        content=connection_layer.build_connection_state(
            records, now, beacon=_node_beacon()),
        headers={"Cache-Control": "no-store"})


@api_router.get("/live/remote")
def live_remote():
    """UI-14 — read-only Control-Tower -> node integration status.

    A PULL: the tower reads health + a telemetry snapshot FROM the node through the
    UI-13 transport, distinct from UI-2's node-pushed `/live/status`. DISABLED by
    default (the default transport is NullTransport), so with no explicit operator
    configuration this returns `state: disabled` and opens no connection.

    Remote data is always provenance `remote-node`; a failure is a failure state,
    never a silent fixture fallback. No secret is returned — the bearer token lives
    only inside the transport, and every detail is redaction-safe.

    Sync def so any (bounded) blocking transport I/O runs in the threadpool.
    """
    try:
        content = node_client.poll_node()
    except Exception:                    # a read-only diagnostic must never 500 the app
        logger.exception("remote node poll failed")
        raise HTTPException(status_code=500, detail="remote node status unavailable")
    return JSONResponse(content=content, headers={"Cache-Control": "no-store"})


# ── UI-17: authenticated read-only operator command surface ───────────────────
# The single service instance. DISABLED by default: `default_command_transport()`
# uses `transport.default_transport()`, which is NullTransport unless an operator has
# explicitly enabled and validly configured a real transport (UI-13). This surface
# transmits ONLY the read-only command vocabulary (noop / request_health /
# request_telemetry) and never reuses the fixture-world `/api/commands/{name}` path,
# the execution orchestrator, or any mutation/arming/order machinery.
_COMMAND_TRANSPORT = command_transport.default_command_transport()

#: Default and maximum time-to-live (seconds) the server stamps on an operator
#: command. Expiry is REQUIRED by the UI-15 contract; the route always supplies one
#: so a caller cannot submit a command with no expiry.
_OPERATOR_CMD_DEFAULT_TTL_S = 30.0
_OPERATOR_CMD_MAX_TTL_S = 300.0
_OPERATOR_CMD_MAX_RECENT = 50


def _operator_command_error(exc: "command_channel.CommandError") -> JSONResponse:
    """Map a UI-15 CommandError onto a stable, redaction-safe 422. The reason code
    is a fixed machine string; the detail is passed through the UI-9 redactor."""
    return JSONResponse(
        status_code=422,
        content={
            "error": "rejected",
            "code": exc.reason,
            "detail": security_config.redact_text(exc.detail) if exc.detail else None,
        },
        headers={"Cache-Control": "no-store"},
    )


@api_router.post("/operator/commands")
async def operator_submit_command(request: Request) -> Any:
    """UI-17 — submit ONE permitted read-only command to the node.

    Body: `{ "commandType": <noop|request_health|request_telemetry>,
             "idempotencyKey": <str, REQUIRED>, "ttlSeconds"?: <number>,
             "operatorRef"?: <str>, "payload"?: <object> }`.

    The server stamps `requested_at` and a bounded `expires_at`, then hands a full
    UI-15 envelope to the UI-16 service, which validates (strict read-only allowlist,
    required idempotency, no secrets), de-duplicates, and dispatches at most once.
    NO mutation, arming, order, pause/resume/cancel/kill is representable here. The
    response is the value-free, redaction-safe lifecycle view; a contract violation
    is a stable 422, never a 500.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    now = datetime.now(timezone.utc)
    try:
        ttl = float(body.get("ttlSeconds") or _OPERATOR_CMD_DEFAULT_TTL_S)
    except (TypeError, ValueError):
        ttl = _OPERATOR_CMD_DEFAULT_TTL_S
    ttl = max(1.0, min(ttl, _OPERATOR_CMD_MAX_TTL_S))
    expires = now.timestamp() + ttl

    def _iso(ts: float) -> str:
        return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")

    payload = body.get("payload")
    envelope = {
        "schema_version": command_channel.SCHEMA_VERSION,
        "command_type": body.get("commandType"),
        # Idempotency is REQUIRED by contract; a missing key is rejected (422), not
        # silently generated — the CLIENT owns idempotency (a fresh key per action).
        "idempotency_key": body.get("idempotencyKey"),
        "requested_at": now.isoformat().replace("+00:00", "Z"),
        "expires_at": _iso(expires),
        "operator_ref": body.get("operatorRef"),
        "payload": payload if isinstance(payload, dict) else {},
    }

    try:
        record = _COMMAND_TRANSPORT.submit(envelope, now=now)
    except command_channel.CommandError as exc:
        return _operator_command_error(exc)
    except Exception:                    # a read-only surface must never 500 the app
        logger.exception("operator command submission failed")
        raise HTTPException(status_code=500, detail="operator command unavailable")

    view = _COMMAND_TRANSPORT.view(record.envelope.command_id)
    return JSONResponse(content=view, headers={"Cache-Control": "no-store"})


@api_router.get("/operator/commands/{command_id}")
def operator_command_status(command_id: str) -> Any:
    """UI-17 — the lifecycle status of a previously submitted command, or 404."""
    view = _COMMAND_TRANSPORT.view(command_id)
    if view is None:
        raise HTTPException(status_code=404, detail="command not found")
    return JSONResponse(content=view, headers={"Cache-Control": "no-store"})


@api_router.get("/operator/commands")
def operator_recent_commands() -> Any:
    """UI-17 — a bounded, newest-first list of recent command records (read-only)."""
    records = _COMMAND_TRANSPORT.recent(_OPERATOR_CMD_MAX_RECENT)
    commands = [_COMMAND_TRANSPORT.view(r.envelope.command_id) for r in records]
    return JSONResponse(
        content={"enabled": _COMMAND_TRANSPORT.enabled, "commands": commands},
        headers={"Cache-Control": "no-store"},
    )


@api_router.get("/security/config")
def security_config_status():
    """UI-9 — VALUE-FREE security configuration status (read-only diagnostics).

    Reports, per variable, only `configured` / `missing` / `invalid`, plus
    validation findings. It NEVER returns a value — not a token, not a certificate
    path, not an endpoint — because an endpoint or path is still deployment
    intelligence even though it is not a credential.

    `active` is always false: UI-9 is preparation only. There is no transport, no
    authentication, no TLS and no connectivity to report on, and this route opens
    nothing. It is a mirror held up to the environment, not a control.
    """
    try:
        config = security_config.load_config()
        body = security_config.describe_config(config)
        # UI-10: the browser boundary, described the same value-free way —
        # classifications and counts, never origin strings.
        body["cors"] = cors_policy.describe(_CORS_POLICY)
        # UI-11: authentication STATE only — no token value, prefix, suffix, hash
        # or length. The policy object holds no field a future edit could render.
        body["auth"] = auth_policy.describe(_AUTH_POLICY, app.routes)
        # ARCH-3: the NODE-INGEST principal, reported distinctly (value-free).
        body["ingestAuth"] = {
            "enabled": _INGEST_POLICY.enabled,
            "enforcing": _INGEST_POLICY.enforcing,
            "misconfigured": _INGEST_POLICY.misconfigured,
            "tokenPresent": _INGEST_POLICY.token_present,
            "issueCodes": sorted(set(_INGEST_POLICY.issues)),
            "routes": sorted(auth_policy.INGEST_ROUTES),
            # DEPRECATED legacy compat: operator token accepted on ingest. When
            # true the configuration is DEGRADED and reported as such.
            "legacyOperatorTokenAllowed": auth_policy.ingest_allows_operator_token(),
            "degraded": auth_policy.ingest_allows_operator_token(),
        }
        # ARCH-3: connectivity posture as EXPLICIT dimensions — enabled, configured,
        # approved and connected are distinct facts. This replaces the removed
        # `active: false` constant and its stale "nothing exists yet" reason.
        endpoint = os.environ.get(security_config.VAR_NODE_ENDPOINT, "").strip()
        policy_decision = (connection_policy.evaluate(endpoint, env=dict(os.environ))
                           if endpoint else None)
        body["connectivity"] = {
            "profile": connection_policy.active_profile(),
            "approvedProfiles": sorted(connection_policy.APPROVED_PROFILES),
            "remoteApproved": False,
            "transport": transport_layer.selection_status(),
            "connectionPolicy": (policy_decision.safe_view() if policy_decision
                                 else {"allowed": False,
                                       "reason": "no_endpoint_configured",
                                       "profile": connection_policy.active_profile()}),
            "outboundNodeAuth": {
                "tokenPresent": bool(os.environ.get(
                    security_config.VAR_NODE_API_TOKEN, "").strip()),
            },
            "missingRemotePrerequisites": list(connection_policy.REMOTE_PREREQUISITES),
            "localOnly": True,
        }
        return JSONResponse(content=body, headers={"Cache-Control": "no-store"})
    except Exception:               # diagnostics must never 500 the app
        logger.exception("security configuration description failed")
        raise HTTPException(status_code=500, detail="security configuration unavailable")


@api_router.get("/notifier/status")
def notifier_status_endpoint(limit: int = Query(50, ge=1, le=200)):
    """L3-C — READ-ONLY Notifier Operational Surface.

    Reports the notifier's live config + worker liveness, its process-lifetime
    last-tick activity snapshot, fresh full-file outbox aggregates, and a
    bounded newest-first allowlisted dead-letter projection. It performs NO
    writes: it never quarantines/renames/prunes the outbox, never delivers,
    never touches notifier state, and never invokes policy/baseline — all
    inspection is notifier-owned and server.py never opens or parses the outbox
    itself. A missing outbox is not an error (zero aggregates); a corrupt or
    unreadable outbox is DATA (200 with outbox=null + a generic outbox_error);
    only an unexpected programming fault yields a STABLE generic 500 (details
    stay server-side). Sync def so the blocking file read runs in the
    threadpool. `limit` (1..200, default 50) bounds only the listing; aggregates
    always cover the whole file."""
    try:
        model = _ops_notifier.inspect(enabled=OPS_NOTIFIER_ENABLED, limit=limit)
    except Exception:  # unexpected fault only; degradation is handled as data
        logger.exception("notifier status inspection failed")
        raise HTTPException(status_code=500, detail="notifier status unavailable")
    return JSONResponse(content=model, headers={"Cache-Control": "no-store"})


app.include_router(api_router)

# UI-10 — the local API browser boundary. Previously this was
# `allow_origins=["*"]` + `allow_credentials=True` + all methods + all headers,
# which answered EVERY origin with `Access-Control-Allow-Origin: *` and approved a
# DELETE preflight from an arbitrary remote site: any page the operator visited
# could read this entire API. Origins, methods and headers are now explicit,
# validated and loopback-only by default; wildcard is unsupported, so a
# credentialed wildcard is impossible rather than merely guarded.
#
# CORS is a BROWSER boundary, not authentication and not a network boundary: it
# does nothing about curl or any non-browser client, and requests with no Origin
# header are unaffected by design.
_CORS_POLICY = cors_policy.load_policy()

# UI-11 — the authenticated API boundary. DISABLED by default: while
# CONTROL_TOWER_AUTH_ENABLED is unset every route behaves exactly as before, so the
# local dry-run workflow is untouched. When deliberately enabled, every route
# except the explicitly enumerated public set requires `Authorization: Bearer`.
#
# Authentication is NOT encryption and NOT CORS: a bearer token over plaintext http
# is readable on the path, and this gate applies to curl as much as to a browser.
# Remote exposure still requires the UI-9 transport prerequisites.
_AUTH_POLICY = auth_policy.load_policy()
# ARCH-3: the NODE-INGEST principal — a distinct credential, distinct policy object,
# scoped solely to auth_policy.INGEST_ROUTES.
_INGEST_POLICY = auth_policy.load_ingest_policy()


#: FIX-2: routes whose payload comes ONLY from the development fixture, DERIVED
#: from the source rather than hand-listed. HARDEN-1 gated five surfaces by
#: inspection and missed sixteen, which then answered 200-with-empty when the
#: fixture was absent — `/api/broker/positions -> []` reading as "no open
#: positions". Deriving the set means a new fixture-reading route is refused
#: automatically, and the completeness test fails if the analysis and the
#: runtime ever disagree.
try:
    FIXTURE_ONLY_PATHS = fixture_surfaces.gated_paths(
        fixture_surfaces.analyse(Path(__file__)))
except Exception:                                           # noqa: BLE001
    # Analysis must never stop the process. It failing is itself notable, so it
    # is logged loudly and reported by diagnostics; the surfaces stay ungated,
    # which is the pre-FIX-2 behaviour rather than a new failure mode.
    logger.exception("fixture-surface analysis failed; surfaces are UNGATED")
    FIXTURE_ONLY_PATHS = set()


@app.middleware("http")
async def _fixture_surface_boundary(request: Request, call_next):
    """Refuse fixture-only surfaces explicitly when the fixture is absent.

    501 rather than 404 (the resource exists as a concept) or an empty 200 (which
    would imply real system state).

    Matching is on the EXACT path. Middleware runs before routing, so
    `scope["route"]` is not yet populated and a parametrised route
    (`/api/deployments/{id}`) cannot be matched here. Those fall through to their
    own handler, which answers 404 for an id it cannot find — honest, since with
    no fixture no such deployment exists. Collection endpoints, which are the
    ones that returned a misleading empty list, are covered.
    """
    if not WORLD.available and request.url.path.startswith("/api"):
        if request.url.path in FIXTURE_ONLY_PATHS:
            return _fixture_unavailable(request.url.path)
    return await call_next(request)


@app.middleware("http")
async def _no_store_boundary(request: Request, call_next):
    """ARCH-3: uniform `Cache-Control: no-store` on every /api response that did
    not set its own caching policy. Telemetry, readiness and security surfaces
    must never be replayed from a cache (a cached snapshot renders a halted
    system as running); applying the header centrally removes the per-route
    lottery Audit A found."""
    response = await call_next(request)
    if request.url.path.startswith("/api") and "cache-control" not in response.headers:
        response.headers["Cache-Control"] = "no-store"
    return response


@app.middleware("http")
async def _authentication_boundary(request: Request, call_next):
    """Deny-by-default request authentication.

    Middleware ORDER (verified at runtime, not assumed): Starlette inserts each
    added middleware at the outside, so the LAST one added is outermost. CORS is
    added after this block, so the actual wrap order is
    request -> CORSMiddleware -> this auth middleware -> handler. That is the
    correct order and needs no change:

      * A browser preflight `OPTIONS` is answered by the outer CORS middleware and
        (for an allowed origin) short-circuits before auth is even reached, so
        preflight keeps working while auth is enabled.
      * A real request passes through CORS to this guard; an auth 401/503 travels
        back out through CORS, which still attaches the `Access-Control-Allow-Origin`
        header (confirmed: 401 and 503 responses carry it).

    Belt and braces: this guard ALSO lets `OPTIONS` through unauthenticated
    (`PREAUTH_METHODS`), so even if it were reached first a preflight could never be
    blocked — a browser cannot attach credentials to a preflight by specification,
    and blocking it would break the UI-10 browser boundary.
    """
    # ── ARCH-3: the NODE-INGEST principal owns its routes exclusively ─────────
    # Evaluated FIRST so the ingest scope can never fall through to (or be
    # satisfied by) the operator principal. When ingest auth is disabled the
    # route stays open exactly as before — enabling the operator principal alone
    # deliberately does not change ingest behaviour (see the activation runbook).
    if request.url.path in auth_policy.INGEST_ROUTES:
        if request.method in auth_policy.PREAUTH_METHODS:
            return await call_next(request)
        if not _INGEST_POLICY.enabled:
            return await call_next(request)
        if _INGEST_POLICY.misconfigured:
            logger.warning("ingest authentication enabled but misconfigured; "
                           "failing closed (issues: %s)", ",".join(_INGEST_POLICY.issues))
            return JSONResponse(status_code=auth_policy.STATUS_MISCONFIGURED,
                                content=auth_policy.MISCONFIGURED_BODY,
                                headers={"Cache-Control": "no-store"})
        header = request.headers.get(auth_policy.AUTH_HEADER)
        if _INGEST_POLICY.verify(header):
            return await call_next(request)
        # DEPRECATED legacy compatibility, explicitly enabled only: the operator
        # credential may authenticate ingest. Reported as degraded elsewhere.
        if auth_policy.ingest_allows_operator_token() and _AUTH_POLICY.verify(header):
            logger.warning("ingest authenticated with the OPERATOR token via the "
                           "deprecated legacy-compat flag — configuration is degraded")
            return await call_next(request)
        logger.warning("unauthorized ingest request")
        return JSONResponse(status_code=auth_policy.STATUS_UNAUTHORIZED,
                            content=auth_policy.UNAUTHORIZED_BODY,
                            headers=dict(auth_policy.UNAUTHORIZED_HEADERS))

    if not _AUTH_POLICY.enabled:
        return await call_next(request)                     # untouched behaviour
    if request.method in auth_policy.PREAUTH_METHODS:
        return await call_next(request)
    if not auth_policy.is_protected(request.url.path):
        return await call_next(request)                     # explicitly public

    if _AUTH_POLICY.misconfigured:
        # Enabled but unusable. Fail CLOSED — never silently revert to open — and
        # say it is a server fault so the caller does not hunt for a credential.
        logger.warning("authentication enabled but misconfigured; protected routes "
                       "are failing closed (issues: %s)",
                       ",".join(_AUTH_POLICY.issues))
        return JSONResponse(status_code=auth_policy.STATUS_MISCONFIGURED,
                            content=auth_policy.MISCONFIGURED_BODY,
                            headers={"Cache-Control": "no-store"})

    if _AUTH_POLICY.verify(request.headers.get(auth_policy.AUTH_HEADER)):
        return await call_next(request)

    # One indistinguishable response for missing / malformed / wrong / empty. The
    # log records the path and nothing about the credential — not its length, not
    # its prefix, not which check failed.
    logger.warning("unauthorized request to %s", request.url.path)
    return JSONResponse(status_code=auth_policy.STATUS_UNAUTHORIZED,
                        content=auth_policy.UNAUTHORIZED_BODY,
                        headers=dict(auth_policy.UNAUTHORIZED_HEADERS))


app.add_middleware(CORSMiddleware, **cors_policy.middleware_kwargs(_CORS_POLICY))

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

# UI-10: one startup line so the effective boundary is visible without guessing.
# Counts and classifications only — a log file must not disclose the origin list.
logger.info("%s", environment_layer.summarise_for_log(ENVIRONMENT))
logger.info("%s", cors_policy.summarise_for_log(_CORS_POLICY))
logger.info("%s", auth_policy.summarise_for_log(_AUTH_POLICY))
for _issue in _CORS_POLICY.issues:
    logger.warning("CORS configuration issue: %s", _issue.code)


# ── L2-A: Operational Transition Log projector wiring (additive) ─────────────
# One process-local projector (D5 single-process guarantee). Disabled by
# default; OPS_JOURNAL_ENABLED must be explicitly truthy to start it. The
# projector reads the canonical cycle stream via the same configured paths as
# L1A and appends narration through the shared _append_event path — the lambda
# wrapper is the fourth (and only new) production append call site, and it
# late-binds so the serialized store path stays the single writer route.
OPS_JOURNAL_ENABLED = ops_journal_layer.env_flag(os.environ.get("OPS_JOURNAL_ENABLED"))
OPS_JOURNAL_CHECKPOINT = ROOT_DIR / "ops_journal_checkpoint.json"
_ops_journal = ops_journal_layer.OpsJournalProjector(
    cycles_path=OPS_STATE_DIR / "ops" / "cycles.jsonl",
    checkpoint_path=OPS_JOURNAL_CHECKPOINT,
    append_event=lambda ev, key: _append_event(ev, key),
    # No package_hash: the projector narrates NODE-derived events only and stamps
    # packageHash null. Passing the fixture world's active package here was
    # fabricated provenance — the projector no longer accepts a provider at all.
    logger=logger,
    # L2-B: canonical level classifications, consumed verbatim through the
    # frozen L1A public API — no thresholds or file inspection in the projector.
    level_provider=lambda now: ops_status_layer.build_operational_status(
        ops_status_layer.collect_sources(OPS_STATE_DIR, OPS_MARKET_DATA_DIR, OPS_KILL_FILE),
        now),
)


# ── L3-A: Notifier policy engine wiring (additive, headless) ─────────────────
# One process-local notifier (D5). Disabled by default; OPS_NOTIFIER_ENABLED
# must be explicitly truthy to start it. It is a POLICY engine over the frozen
# L1A model (same public-API seam as the projector) — it owns no health, writes
# no BotEvent, and delivers nothing (delivery is L3-B). Its only outputs are its
# own durable state + outbox files under the backend-owned directory.
OPS_NOTIFIER_ENABLED = ops_notifier_layer.env_flag(os.environ.get("OPS_NOTIFIER_ENABLED"))
OPS_NOTIFIER_STATE = ROOT_DIR / "ops_notifier_state.json"
OPS_NOTIFIER_OUTBOX = ROOT_DIR / "ops_notifier_outbox.json"
_ops_notifier = ops_notifier_layer.OpsNotifier(
    l1a_provider=lambda now: ops_status_layer.build_operational_status(
        ops_status_layer.collect_sources(OPS_STATE_DIR, OPS_MARKET_DATA_DIR, OPS_KILL_FILE),
        now),
    state_path=OPS_NOTIFIER_STATE,
    outbox_path=OPS_NOTIFIER_OUTBOX,
    logger=logger,
    # L3-B: env-only webhook destination. Absent/blank -> notifications remain
    # pending (no delivery attempt); the notifier still records decisions.
    webhook_url=os.environ.get("OPS_NOTIFIER_WEBHOOK_URL"),
)


@app.on_event("startup")
async def _start_ops_journal():
    # LIVE-5A: the ONE runtime refresh owner. Started here so exactly one loop
    # polls the broker for the whole process lifetime.
    if RUNTIME_SUPERVISOR_ENABLED:
        _RUNTIME_SUPERVISOR.start()
    if OPS_JOURNAL_ENABLED:
        _ops_journal.start()
    if OPS_NOTIFIER_ENABLED:
        _ops_notifier.baseline(datetime.now(timezone.utc))  # silent seed, no back-page
        _ops_notifier.start()


@app.on_event("shutdown")
async def shutdown_db_client():
    _RUNTIME_SUPERVISOR.stop()  # safe when never started
    _ops_journal.stop()  # safe when never started
    _ops_notifier.stop()  # safe when never started
    if client is not None:
        client.close()
