"""UI-1 — truthful connection state.

Four INDEPENDENT dimensions, each with its own evidence source. There is
deliberately no single `connected` boolean: a Control Tower that is running says
nothing about the node, a node that is publishing says nothing about MT5, and a
snapshot that exists says nothing about whether it is current.

    dimension    states                                     evidence
    ---------    ------                                     --------
    backend      starting / available / unavailable         the CLIENT's own
                                                            ability to reach the
                                                            API (derived in the
                                                            frontend — a backend
                                                            cannot report its own
                                                            unavailability)
    telemetry    never_received / fresh / stale /            stored snapshot +
                 unavailable                                its `published_at`
    node         unknown / connected / disconnected         telemetry freshness,
                                                            plus the node's own
                                                            L1A liveness beacon
                                                            when that source is
                                                            readable
    bridge       unknown / healthy / degraded /             MT5-derived facts the
                 unavailable                                node itself reported
                                                            in the snapshot

UNKNOWN vs UNAVAILABLE — the distinction this whole module exists to preserve:

    unknown      We have no evidence either way. Absence of a reading is NOT a
                 reading. A stale snapshot makes the node unknown, not dead: the
                 node keeps trading and protecting the account with the Control
                 Tower offline (invariant I-10), so silence is ambiguous.
    unavailable  We have POSITIVE evidence that something could not be reached or
                 read — the node reported `snapshot_status: unavailable`, or the
                 stored snapshot cannot be parsed/validated.

STALE is a statement about the OBSERVATION, never about the system. Past the
freshness threshold we stop claiming anything current; we do not start claiming
something is broken.

Nothing here decides trading state, and nothing is inferred from the fixture
world. Every value traces to a node-published fact, to the absence of one, or to
the reader's own failure to obtain one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import live_telemetry

# ── state vocabularies (stable strings; the frontend mirrors these) ───────────
BACKEND_STARTING = "starting"
BACKEND_AVAILABLE = "available"
BACKEND_UNAVAILABLE = "unavailable"

TELEMETRY_NEVER = "never_received"
TELEMETRY_FRESH = "fresh"
TELEMETRY_STALE = "stale"
TELEMETRY_UNAVAILABLE = "unavailable"

NODE_UNKNOWN = "unknown"
NODE_CONNECTED = "connected"
NODE_DISCONNECTED = "disconnected"

BRIDGE_UNKNOWN = "unknown"
BRIDGE_HEALTHY = "healthy"
BRIDGE_DEGRADED = "degraded"
BRIDGE_UNAVAILABLE = "unavailable"

# Why a stored snapshot could not be used. Each is visually distinguishable in
# the UI: they mean different things and demand different operator responses.
PROBLEM_MALFORMED = "malformed_snapshot"
PROBLEM_UNSUPPORTED_SCHEMA = "unsupported_schema"
PROBLEM_UNKNOWN_SCHEMA = "unknown_schema"

# Same threshold the UI-2 read side already applies, imported rather than
# redeclared so freshness has exactly one definition.
DEFAULT_STALE_AFTER_S = live_telemetry.DEFAULT_STALE_AFTER_S


def _classify_problem(error: live_telemetry.TelemetryError) -> str:
    """Map a validation failure onto the operator-facing problem vocabulary."""
    if error.reason == "schema_version_missing":
        return PROBLEM_UNKNOWN_SCHEMA
    if error.reason == "schema_version_unsupported":
        return PROBLEM_UNSUPPORTED_SCHEMA
    return PROBLEM_MALFORMED


def telemetry_state(signed_age_s: float | None, stale_after_s: float) -> str:
    """Classify freshness from the SIGNED age delta (now - published).

    RETAINED FOR CALLERS THAT HOLD ONLY AN AGE. `instance_view` no longer uses
    it — see `telemetry_state_from_observation`, which is the canonical path.

    A value beyond the window in EITHER direction is not current: a future-dated
    snapshot (negative delta, from clock skew) must not read as fresh, exactly as a
    too-old one does not. Symmetric tolerance around zero, one threshold."""
    if signed_age_s is None:
        return TELEMETRY_NEVER
    return TELEMETRY_FRESH if abs(signed_age_s) <= stale_after_s else TELEMETRY_STALE


def telemetry_state_from_observation(observation: dict) -> str:
    """Map the CANONICAL M-TEL-1 freshness envelope onto this vocabulary.

    M-NODE-READ-1 — THE SECOND FRESHNESS AUTHORITY, REMOVED.

    `instance_view` previously computed its own verdict: `now - published_at`
    against a flat 120s. `live_telemetry.observation()` — the module this file
    already imports for its threshold — decides it two ways differently:

      1. It judges liveness on `received_at`, THIS TOWER'S ARRIVAL CLOCK, not on
         the node's `published_at`. That is deliberate and load-bearing: a node
         cannot make itself look fresh by publishing a manipulated timestamp.
         Judging on `published_at` handed the node authority over its own
         liveness verdict.

      2. Its budget is PHASE-AWARE. 120s applies only to an idle node
         (`cycle.status == "no_new_bar"`); every other phase gets one bar
         interval (900s), because a warm recompute legitimately publishes
         nothing while it runs. The flat 120s here is exactly the defect
         M-TEL-1 documented: "a healthy node was marked stale ~2 minutes into a
         legitimate recompute".

    So the two surfaces genuinely disagreed. During any recompute,
    `/api/live/connection` reported telemetry STALE and the node UNKNOWN while
    `/api/live/status` reported the same snapshot fresh — one node, two answers,
    at the same instant.

    WHY IT IS SAFE TO FIX HERE: `build_connection_state` has exactly one
    consumer, `GET /api/live/connection`, which is display-only. The execution
    safety path builds `NodeFacts` from `_live_status_entry` — the canonical
    envelope — and never reads this module. Nothing about command
    authorization, arming, execution or reconciliation changes.

    The canonical `stale` is `liveness_stale OR data_stale`, so this can only
    ever be equal to or stricter than the old flat rule in the cases that
    mattered, and more permissive only where M-TEL-1 deliberately made it so.
    """
    if not isinstance(observation, dict):
        return TELEMETRY_UNAVAILABLE
    if observation.get("age_seconds") is None and observation.get("published_at") is None:
        return TELEMETRY_NEVER
    return TELEMETRY_STALE if observation.get("stale") else TELEMETRY_FRESH


def node_state(telemetry: str, beacon: Any) -> tuple[str, str]:
    """(state, evidence) for the execution node.

    `connected` requires FRESH telemetry — a snapshot alone is not presence.
    `disconnected` requires POSITIVE evidence: the node's own L1A liveness beacon
    reporting UNAVAILABLE (the beacon writer is gone). Everything else is
    `unknown`, including stale telemetry: the node may be alive and trading while
    merely unable to reach this Control Tower.
    """
    if beacon == "UNAVAILABLE":
        return NODE_DISCONNECTED, "node liveness beacon reports UNAVAILABLE"
    if telemetry == TELEMETRY_FRESH:
        return NODE_CONNECTED, "fresh telemetry received from the node"
    if telemetry == TELEMETRY_STALE:
        return NODE_UNKNOWN, ("telemetry is stale; the node may still be running "
                              "and unable to reach this Control Tower")
    if telemetry == TELEMETRY_UNAVAILABLE:
        return NODE_UNKNOWN, "the stored snapshot could not be read"
    return NODE_UNKNOWN, "no telemetry has ever been received"


def bridge_state(snapshot: dict | None, telemetry: str) -> tuple[str, str]:
    """(state, evidence) for the MT5 bridge, from node-reported facts only.

    The Control Tower never contacts MT5, so every input here is something the
    NODE observed and published. Stale telemetry yields `unknown` rather than a
    carried-forward reading: an old bridge observation is not a current one.
    """
    if telemetry != TELEMETRY_FRESH or not isinstance(snapshot, dict):
        return BRIDGE_UNKNOWN, "no current node observation of the bridge"

    reconciliation = snapshot.get("reconciliation")
    reconciliation = reconciliation if isinstance(reconciliation, dict) else {}
    market = snapshot.get("market")
    market = market if isinstance(market, dict) else {}

    # POSITIVE evidence of unreachability: the node tried to read broker state and
    # could not. This is the only path to `unavailable`.
    if reconciliation.get("snapshot_status") == "unavailable":
        return BRIDGE_UNAVAILABLE, "node could not read broker positions"

    if reconciliation.get("snapshot_status") == "unreadable":
        return BRIDGE_DEGRADED, "broker position snapshot was unreadable"
    if reconciliation.get("frozen") is True:
        return BRIDGE_DEGRADED, "node froze reconciliation against broker state"

    if market.get("available") is True:
        if market.get("feed_healthy") is False:
            return BRIDGE_DEGRADED, "node reported an unhealthy price feed"
        if market.get("feed_healthy") is True:
            return BRIDGE_HEALTHY, "node observed a healthy price feed"
        return BRIDGE_UNKNOWN, "node sampled prices but did not classify feed health"

    # The node samples market/account only on cycles that contain an OPEN, so
    # "not sampled" is the common case and must not read as a fault.
    return BRIDGE_UNKNOWN, "node did not sample the bridge on its last cycle"


def instance_view(instance_id: str, record: dict, now: datetime,
                  stale_after_s: float, beacon: Any = None) -> dict:
    """The full connection view for ONE instance.

    Multi-instance by construction: the caller maps over whatever instances the
    store holds. Nothing here assumes a single node, and no instance's state can
    influence another's.
    """
    raw = record.get("snapshot") if isinstance(record, dict) else None
    problem = None
    snapshot = None
    try:
        snapshot = live_telemetry.validate_snapshot(raw)
    except live_telemetry.TelemetryError as exc:
        problem = _classify_problem(exc)

    observation: dict | None = None
    if problem is not None:
        telemetry = TELEMETRY_UNAVAILABLE
        published_at = None
        age = None
        schema_version = raw.get("schema_version") if isinstance(raw, dict) else None
        budget = stale_after_s
    else:
        # M-NODE-READ-1: ONE freshness authority. This is the same call
        # `_live_status_entry` makes, so `/api/live/connection` and
        # `/api/live/status` can no longer disagree about the same snapshot —
        # they are now literally the same verdict.
        observation = live_telemetry.observation(
            snapshot, now=now,
            received_at=record.get("received_at") if isinstance(record, dict) else None)
        published_at = observation["published_at"]
        age = observation["age_seconds"]
        budget = observation["stale_after_seconds"]
        telemetry = telemetry_state_from_observation(observation)
        if telemetry == TELEMETRY_NEVER:
            # Validation guarantees a parseable timestamp, so this is unreachable
            # in practice; treated as unusable rather than silently "fresh".
            telemetry = TELEMETRY_UNAVAILABLE
            problem = PROBLEM_MALFORMED
        schema_version = snapshot.get("schema_version")

    node, node_evidence = node_state(telemetry, beacon)
    bridge, bridge_evidence = bridge_state(snapshot if problem is None else None, telemetry)
    return {
        "instanceId": instance_id,
        "telemetry": telemetry,
        "node": node,
        "nodeEvidence": node_evidence,
        "bridge": bridge,
        "bridgeEvidence": bridge_evidence,
        "problem": problem,
        "schemaVersion": schema_version,
        "legacySource": bool(record.get("legacy_source")) if isinstance(record, dict) else False,
        "publishedAt": published_at,
        "receivedAt": record.get("received_at") if isinstance(record, dict) else None,
        "ageSeconds": None if age is None else round(age, 3),
        # The budget ACTUALLY APPLIED to this snapshot — phase-aware, from the
        # canonical envelope. Reporting the flat default while judging against a
        # different number made the displayed threshold unfalsifiable.
        "staleAfterSeconds": budget,
        # M-TEL-1 decomposition, passed through so a reader can see WHY a node
        # reads stale: gone quiet, or still publishing old observations.
        "livenessAgeSeconds": (observation or {}).get("liveness_age_seconds"),
        "livenessStale": (observation or {}).get("liveness_stale"),
        "dataStale": (observation or {}).get("data_stale"),
        "freshnessBasis": (observation or {}).get("freshness_basis"),
        # Node-reported operating context. Present so the UI can show WHICH node
        # this is without a second request; never used to derive a state above.
        "mode": ((snapshot or {}).get("runtime") or {}).get("mode"),
        "engineVersion": ((snapshot or {}).get("engine") or {}).get("engine_version_actual"),
    }


def build_connection_state(records: dict, now: datetime, *,
                           stale_after_s: float = DEFAULT_STALE_AFTER_S,
                           beacon: Any = None) -> dict:
    """The whole read-side connection model. Pure: no I/O, no clock of its own.

    `beacon` is the node's L1A aliveness classification when that source is
    readable by this process, else None. In the target topology the node runs on a
    separate machine and its files are NOT visible here, so None (-> `unknown`)
    is the normal case rather than an error.
    """
    instances = sorted(records)
    views = [instance_view(iid, records[iid], now, stale_after_s, beacon)
             for iid in instances]
    if not views:
        return {
            "observedAt": now.isoformat().replace("+00:00", "Z"),
            "staleAfterSeconds": stale_after_s,
            "firstRun": True,          # explicit: nothing has EVER been received
            "telemetry": TELEMETRY_NEVER,
            "node": NODE_UNKNOWN,
            "bridge": BRIDGE_UNKNOWN,
            "instanceCount": 0,
            "instances": [],
            "emptyState": "No live execution node has ever published telemetry to "
                          "this Control Tower.",
        }
    return {
        "observedAt": now.isoformat().replace("+00:00", "Z"),
        "staleAfterSeconds": stale_after_s,
        "firstRun": False,
        # Fleet roll-ups are the WORST state across instances, never the best: one
        # silent node must not be hidden behind a healthy one.
        "telemetry": _worst((v["telemetry"] for v in views), _TELEMETRY_ORDER),
        "node": _worst((v["node"] for v in views), _NODE_ORDER),
        "bridge": _worst((v["bridge"] for v in views), _BRIDGE_ORDER),
        "instanceCount": len(views),
        "instances": views,
        "emptyState": None,
    }


# Worst-first orderings, one per dimension. Index 0 is the most alarming state.
# Kept per-dimension rather than in a shared lookup because the vocabularies
# genuinely overlap — "unknown" and "unavailable" appear in more than one
# dimension and rank differently in each.
_TELEMETRY_ORDER = (TELEMETRY_UNAVAILABLE, TELEMETRY_NEVER, TELEMETRY_STALE, TELEMETRY_FRESH)
_NODE_ORDER = (NODE_DISCONNECTED, NODE_UNKNOWN, NODE_CONNECTED)
_BRIDGE_ORDER = (BRIDGE_UNAVAILABLE, BRIDGE_DEGRADED, BRIDGE_UNKNOWN, BRIDGE_HEALTHY)


def _worst(states, order: tuple) -> str:
    """The most alarming of the given states within ONE dimension's ordering.

    An unrecognized state ranks worst: an unreadable classification must never be
    optimistically ignored."""
    values = list(states)
    if not values:
        return order[0]
    return min(values, key=lambda s: order.index(s) if s in order else -1)
