"""M-LEDGER-ORIGIN-1 — the single admission policy for ledger records.

WHY THIS EXISTS
    Before this contract, the only origin-shaped field a consumer could see on
    `/api/ledger/*` was `provenance: "durable-store"`. That describes where a
    record is KEPT. The same store holds trades reconstructed from the mock
    adapter, so admitting on it would have laundered simulated fills into
    broker history — the exact mistake M-FLEET-2 was built to prevent, one
    layer deeper.

THE THREE AXES, AND WHY ONLY ONE OF THEM ADMITS
    initiation origin  (TradeOrigin)      who caused the trade
    execution origin   (ExecutionOrigin)  which adapter produced/observed it
    storage class      ("durable-store")  where it is persisted

    Only EXECUTION origin grants admission. The combination that makes this
    necessary is CONTROL_TOWER + mock: a genuinely tower-initiated trade whose
    fill was simulated. Its initiation is real; its price is not. Conversely
    MANUAL_BROKER + mt5 is genuine broker history despite the tower never
    having initiated it. Initiation alone therefore proves nothing either way.

    Reconciliation is a fourth, separate fact. A record reconstructed by
    reconciliation from MT5 deals is still MT5-originated; reconciliation
    describes HOW the record was assembled, never WHO executed it.

FAIL CLOSED
    `unknown` is rejected everywhere. It is what a legacy row or an unrecorded
    adapter yields, and there is no evidence on which to promote it.
"""
from __future__ import annotations

import trade_ledger_domain as tld

#: Execution origins admissible as ordinary broker trade history.
HISTORY_ADMISSIBLE = frozenset({tld.ExecutionOrigin.MT5})

#: Execution origins admissible as input to live performance analytics.
#: Identical today, and deliberately a SEPARATE constant: analytics may narrow
#: further (it must not widen), and collapsing them would hide that.
ANALYTICS_ADMISSIBLE = frozenset({tld.ExecutionOrigin.MT5})

REASON_ADMITTED = "admitted"
REASON_SIMULATED = "execution_origin_simulated"
REASON_UNKNOWN = "execution_origin_unknown"
REASON_MALFORMED = "execution_origin_malformed"


def _classify(execution_origin: object) -> tuple[str, str]:
    """Normalise and explain. Never raises; anything odd fails closed."""
    if not isinstance(execution_origin, str):
        return tld.ExecutionOrigin.UNKNOWN, REASON_MALFORMED
    value = tld.ExecutionOrigin.normalize(execution_origin)
    if value == tld.ExecutionOrigin.MOCK:
        return value, REASON_SIMULATED
    if value == tld.ExecutionOrigin.UNKNOWN:
        return value, (REASON_MALFORMED
                       if execution_origin.strip().lower() not in tld.ExecutionOrigin.KNOWN
                       else REASON_UNKNOWN)
    return value, REASON_ADMITTED


def admits_history(execution_origin: object) -> bool:
    """May this record appear in ordinary broker trade history?"""
    value, _ = _classify(execution_origin)
    return value in HISTORY_ADMISSIBLE


def admits_analytics(execution_origin: object) -> bool:
    """May this record feed live performance analytics?

    Execution origin is a NECESSARY condition, not a sufficient one. Closed
    status, valid broker identifiers and cost completeness remain separate
    predicates owned by the analytics milestone; this answers only the origin
    question, which is the one that could previously not be answered at all.
    """
    value, _ = _classify(execution_origin)
    return value in ANALYTICS_ADMISSIBLE


def rejection_reason(execution_origin: object) -> str:
    """Machine-readable reason a record was refused, or `admitted`."""
    return _classify(execution_origin)[1]


def admits_entry(entry_row: dict, *, for_analytics: bool = False) -> bool:
    """Admission for a serialized ledger row.

    Reads ONLY `executionOrigin`. It deliberately ignores `provenance`,
    `origin` and the endpoint the row arrived from — storage class and
    initiation authority must not be able to grant admission by accident.
    """
    origin = (entry_row or {}).get("executionOrigin")
    return admits_analytics(origin) if for_analytics else admits_history(origin)
