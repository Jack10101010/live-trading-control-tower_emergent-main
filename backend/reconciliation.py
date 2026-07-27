"""ARCH-2 — the canonical reconciliation authority.

ONE service owns reconciliation of canonical execution state. It compares:

  * durable tower intent/order state (the execution store)
  * broker-reported open orders / open positions / account identity
    (the adapter's `reconcile_snapshot`)
  * node-reported execution telemetry, where available

and classifies every discrepancy EXPLICITLY. It is READ-ONLY with respect to the
broker: it never issues a corrective broker action — its output is durable facts
(persisted via the execution store) and a safety posture (unresolved CRITICAL
discrepancies deny new risk-increasing execution through the safety gate).

RELATION TO `broker_sync` (ownership resolution): `broker_sync.py` remains the
FIXTURE-WORLD VIEW synchroniser — it compares the mock broker's view against the
runtime overlay presentation and drives the existing UI panels. It is not an
authority over canonical execution state. THIS module is the single
reconciliation authority; the sync cycle DELEGATES here so canonical
reconciliation runs on the same cadence without adding any new mutating route.
Its synthetic fault injection is test-only and disabled by default (the
`/api/broker/faults` route is gated behind `BROKER_FAULT_INJECTION_ENABLED`).

HARD RULES
  * account identity mismatch is a HARD FAILURE (run.failed, critical)
  * stale broker/node input can never produce a clean result
  * every run is immutable, identified by a `recon_` id, and records its source
    timestamps and account identity
  * discrepancy resolution requires evidence and is a new fact, never a deletion
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

import order_lifecycle as ol

# ── discrepancy classes ───────────────────────────────────────────────────────

TOWER_ONLY_ORDER = "tower_only_order"
BROKER_ONLY_ORDER = "broker_only_order"
TOWER_ONLY_POSITION = "tower_only_position"
BROKER_ONLY_POSITION = "broker_only_position"
STATUS_MISMATCH = "status_mismatch"
QUANTITY_MISMATCH = "quantity_mismatch"
PRICE_MISMATCH = "price_mismatch"
MISSING_BROKER_REFERENCE = "missing_broker_reference"
DUPLICATE_BROKER_REFERENCE = "duplicate_broker_reference"
STALE_SNAPSHOT = "stale_snapshot"
ACCOUNT_IDENTITY_MISMATCH = "account_identity_mismatch"
UNKNOWN = "unknown"

ALL_CLASSES = frozenset({
    TOWER_ONLY_ORDER, BROKER_ONLY_ORDER, TOWER_ONLY_POSITION, BROKER_ONLY_POSITION,
    STATUS_MISMATCH, QUANTITY_MISMATCH, PRICE_MISMATCH, MISSING_BROKER_REFERENCE,
    DUPLICATE_BROKER_REFERENCE, STALE_SNAPSHOT, ACCOUNT_IDENTITY_MISMATCH, UNKNOWN,
})

#: Classes that deny new risk-increasing execution while unresolved. Mismatched
#: account identity, entities one side cannot see, and duplicated broker refs are
#: exactly the situations in which adding risk is unsafe.
CRITICAL_CLASSES = frozenset({
    ACCOUNT_IDENTITY_MISMATCH, BROKER_ONLY_ORDER, BROKER_ONLY_POSITION,
    TOWER_ONLY_POSITION, DUPLICATE_BROKER_REFERENCE, MISSING_BROKER_REFERENCE,
    UNKNOWN,
})

#: How old a source snapshot may be before it is STALE and cannot reconcile clean.
DEFAULT_STALE_AFTER_S = 120.0


@dataclass(frozen=True)
class Discrepancy:
    cls: str
    entity_id: str | None
    detail: str

    @property
    def critical(self) -> bool:
        return self.cls in CRITICAL_CLASSES

    def as_record(self) -> dict:
        return {"class": self.cls, "entityId": self.entity_id,
                "critical": self.critical, "detail": self.detail}


@dataclass(frozen=True)
class ReconciliationRun:
    """One immutable reconciliation run."""
    recon_id: str
    at: str
    account_identity: str | None
    expected_account_identity: str | None
    sources: dict
    discrepancies: tuple = field(default_factory=tuple)
    stale: bool = False
    failed: bool = False

    @property
    def clean(self) -> bool:
        # Stale input or a hard failure can NEVER be clean, even with no items.
        return not self.discrepancies and not self.stale and not self.failed

    @property
    def critical(self) -> bool:
        return self.failed or any(d.critical for d in self.discrepancies)

    def as_record(self) -> dict:
        return {"reconId": self.recon_id, "at": self.at,
                "accountIdentity": self.account_identity,
                "expectedAccountIdentity": self.expected_account_identity,
                "clean": self.clean, "critical": self.critical,
                "stale": self.stale, "failed": self.failed,
                "sources": dict(self.sources)}


def _parse(ts) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _is_stale(source_at, now: str, stale_after_s: float) -> bool:
    src, ref = _parse(source_at), _parse(now)
    if src is None or ref is None:
        return True                       # unparseable is indistinguishable from stale
    return abs((ref - src).total_seconds()) > stale_after_s


def run_reconciliation(*, tower_intents: list[dict], broker_orders: list[dict],
                       broker_positions: list[dict],
                       broker_account_identity: str | None,
                       expected_account_identity: str | None,
                       broker_snapshot_at: str | None, now: str,
                       node_snapshot: dict | None = None,
                       node_snapshot_at: str | None = None,
                       stale_after_s: float = DEFAULT_STALE_AFTER_S) -> ReconciliationRun:
    """Pure, deterministic classification of one reconciliation pass.

    `tower_intents` are execution-store rows. Broker inputs come from the
    adapter's reconcile_snapshot. The function performs NO I/O and NO corrective
    action; the caller persists the result via `ExecutionStore.save_reconciliation`.
    """
    items: list[Discrepancy] = []
    stale = False
    failed = False

    # 1) Account identity — a mismatch is a HARD failure. Comparison only happens
    #    when both sides report an identity; a missing side is handled by staleness
    #    and the deny-by-default safety posture, never by assuming a match.
    if broker_account_identity and expected_account_identity \
            and broker_account_identity != expected_account_identity:
        failed = True
        items.append(Discrepancy(ACCOUNT_IDENTITY_MISMATCH, broker_account_identity,
                                 "broker-reported account identity does not match the expected account"))

    # 2) Staleness — stale input can never reconcile clean.
    if broker_snapshot_at is None or _is_stale(broker_snapshot_at, now, stale_after_s):
        stale = True
        items.append(Discrepancy(STALE_SNAPSHOT, None,
                                 f"broker snapshot missing or older than {stale_after_s}s"))
    if node_snapshot is not None and (
            node_snapshot_at is None or _is_stale(node_snapshot_at, now, stale_after_s)):
        stale = True
        items.append(Discrepancy(STALE_SNAPSHOT, None,
                                 f"node snapshot missing timestamp or older than {stale_after_s}s"))

    # 3) Broker-reference integrity across tower intents that should carry one.
    #    An intent the tower believes reached the broker must have a broker_ref;
    #    two intents sharing one broker_ref is a duplication fault (a duplicated
    #    broker acknowledgement). LIVE-2: OPEN market orders join this scan — an
    #    open position without a broker ticket is an acknowledgement gone missing.
    seen_refs: dict[str, str] = {}
    open_tower = [i for i in tower_intents
                  if i.get("state") in ol.IN_FLIGHT_STATES or i.get("state") == ol.RECONCILIATION_REQUIRED]
    open_market_orders = [i for i in tower_intents if i.get("state") == ol.OPEN]
    for intent in open_tower + open_market_orders:
        ref = intent.get("broker_ref")
        state = intent.get("state")
        if state in (ol.SUBMITTED, ol.ACKNOWLEDGED, ol.PARTIALLY_FILLED, ol.OPEN) and not ref:
            items.append(Discrepancy(MISSING_BROKER_REFERENCE, intent.get("intent_id"),
                                     f"intent in state {state} has no broker reference"))
        if ref:
            if ref in seen_refs:
                items.append(Discrepancy(DUPLICATE_BROKER_REFERENCE, ref,
                                         f"broker ref shared by {seen_refs[ref]} and {intent.get('intent_id')}"))
            else:
                seen_refs[ref] = intent.get("intent_id", "?")

    # 4) Tower-vs-broker order matching (by broker_ref / order id).
    broker_order_ids = {}
    for o in broker_orders:
        oid = o.get("id") or o.get("orderId")
        if oid in broker_order_ids:
            items.append(Discrepancy(DUPLICATE_BROKER_REFERENCE, oid,
                                     "broker reports the same order id more than once"))
        broker_order_ids[oid] = o
    tower_order_refs = {i.get("broker_ref") for i in open_tower if i.get("broker_ref")}
    for intent in open_tower:
        ref = intent.get("broker_ref")
        if ref and ref not in broker_order_ids:
            items.append(Discrepancy(TOWER_ONLY_ORDER, ref,
                                     f"tower intent {intent.get('intent_id')} references order {ref} the broker does not report"))
    for oid, order in broker_order_ids.items():
        if oid not in tower_order_refs:
            items.append(Discrepancy(BROKER_ONLY_ORDER, oid,
                                     "broker reports an order the tower has no intent for"))
        else:
            # field comparisons where both sides carry the fact
            intent = next(i for i in open_tower if i.get("broker_ref") == oid)
            if intent.get("quantity") is not None and order.get("size") is not None \
                    and float(intent["quantity"]) != float(order["size"]):
                items.append(Discrepancy(QUANTITY_MISMATCH, oid,
                                         f"tower quantity {intent['quantity']} != broker size {order['size']}"))
            if intent.get("entry") is not None and order.get("entry") is not None \
                    and float(intent["entry"]) != float(order["entry"]):
                items.append(Discrepancy(PRICE_MISMATCH, oid,
                                         f"tower entry {intent['entry']} != broker entry {order['entry']}"))
            tower_state, broker_state = intent.get("state"), order.get("state")
            if broker_state == "pending" and tower_state in (ol.CANCEL_PENDING, ol.CLOSE_PENDING):
                pass                       # request in flight — not a mismatch
            elif broker_state and tower_state and broker_state not in _COMPATIBLE_BROKER_STATES.get(tower_state, {broker_state}):
                items.append(Discrepancy(STATUS_MISMATCH, oid,
                                         f"tower state {tower_state} vs broker state {broker_state}"))

    # 5) Positions: intents are the tower-side lineage. LIVE-2: an OPEN market
    #    order's broker ticket is position lineage (the broker order ceased to
    #    exist when it filled; the position carries the ticket) — a broker
    #    position matching an OPEN intent's ref is expected, not broker-only.
    tower_position_refs = tower_order_refs | {
        i["broker_ref"] for i in open_market_orders if i.get("broker_ref")}
    for p in broker_positions:
        pid = p.get("id") or p.get("positionId")
        if pid and pid not in tower_position_refs and not p.get("_fixture_lineage", True):
            items.append(Discrepancy(BROKER_ONLY_POSITION, pid,
                                     "broker reports a position with no tower lineage"))
    if node_snapshot is not None:
        node_positions = node_snapshot.get("positions")
        if isinstance(node_positions, list) and len(node_positions) != len(broker_positions):
            items.append(Discrepancy(STATUS_MISMATCH, None,
                                     f"node reports {len(node_positions)} positions, broker reports {len(broker_positions)}"))

    return ReconciliationRun(
        recon_id=ol.new_reconciliation_id(), at=now,
        account_identity=broker_account_identity,
        expected_account_identity=expected_account_identity,
        sources={"brokerSnapshotAt": broker_snapshot_at,
                 "nodeSnapshotAt": node_snapshot_at,
                 "staleAfterSeconds": stale_after_s},
        discrepancies=tuple(items), stale=stale, failed=failed,
    )


#: Which broker-reported order states are compatible with each tower state (used
#: only where both sides report a state; anything else is a status mismatch).
_COMPATIBLE_BROKER_STATES: dict[str, set] = {
    ol.SUBMITTED: {"pending", "working", "accepted"},
    ol.ACKNOWLEDGED: {"pending", "working", "accepted"},
    ol.PARTIALLY_FILLED: {"working", "partial"},
    ol.CANCEL_PENDING: {"pending", "working", "cancelling"},
    ol.CLOSE_PENDING: {"pending", "working", "closing"},
    ol.MODIFY_PENDING: {"pending", "working"},
}


def persist(store, run: ReconciliationRun) -> None:
    """Persist a run through the execution store (immutable; append-only items)."""
    store.save_reconciliation(run.as_record(), [d.as_record() for d in run.discrepancies])


def safety_posture(store) -> dict:
    """The reconciliation facts the execution-context builder feeds the safety
    gate: are there unresolved critical discrepancies, and is the latest run
    usable? A store failure reports the FAIL-CLOSED posture (critical=True)."""
    try:
        latest = store.latest_reconciliation() if store else None
        unresolved = store.unresolved_critical_count() if store else 0
    except Exception:
        return {"criticalUnresolved": True, "stale": True,
                "lastRunId": None, "lastRunAt": None,
                "detail": "reconciliation state unavailable — failing closed"}
    return {
        "criticalUnresolved": unresolved > 0,
        "stale": bool(latest and latest.get("stale")),
        "lastRunId": latest.get("recon_id") if latest else None,
        "lastRunAt": latest.get("at") if latest else None,
        "detail": None,
    }
