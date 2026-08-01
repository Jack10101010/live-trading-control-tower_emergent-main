"""M-MT5-READ-1 — the one place that decides what a broker-truth record may CLAIM.

THE DEFECT THIS EXISTS TO FIX
    `operational_projection._provenance_for(kind)` derived provenance from the
    ACTIVE ADAPTER KIND alone:

        return PROV_LIVE_MT5 if adapter_kind == "mt5" else PROV_MOCK_FIXTURE

    An adapter kind is CONFIGURATION. It is set by an environment variable and
    is true before any terminal is contacted, true when the terminal is absent,
    and true when every read fails. So a Control Tower with
    `CONTROL_TOWER_BROKER_ADAPTER=mt5` and no MT5 terminal at all produced:

        {"provenance": "live_mt5", "balance": null, "equity": null,
         "connectionState": "Disconnected",
         "freshness": {"available": false, "stale": true}}

    The frontend gate (`lib/operationalProvenance.ts`) admits on the provenance
    string alone — correctly, that is its whole design — so this record was
    ADMITTED, rendered under a green LIVE border, and classified `stale` rather
    than `unavailable`, which `analyticsInputAdmissible` accepts as analytics
    input. One environment variable would have re-opened every hole the honesty
    programme closed, without a single line of fixture data being involved.

    The Mac cannot reach MT5 at all: the MetaTrader5 package is a Windows
    local-terminal IPC binding, `MT5Adapter._gateway` resolves to None here, and
    every read returns unavailable. So on this host the defect is not
    theoretical — it is exactly what setting the documented variable would do.

THE RULE
    Provenance is EARNED BY OBSERVATION, NEVER ASSIGNED BY CONFIGURATION.

    A record may name an origin only when that origin actually answered and the
    data in the record came back from it. When nothing was observed the record's
    origin is `absent` — which the frontend gate rejects, so the surface reports
    `unavailable`: the honest answer, and the one the operator needs.

    Note the asymmetry with the freshness envelope. `freshness.available: false`
    already recorded that nothing was observed, and the projection was emitting
    both facts at once — `live_mt5` AND `available: false` — trusting every
    consumer to reconcile them. One did not. A contradiction that has to be
    reconciled downstream is a defect upstream, so the two facts are made to
    agree at the source.

TWO ORIGINS, NOT ONE
    `live_mt5` and `node_mt5` are both genuine MT5 terminal truth, and they are
    deliberately DISTINCT values rather than one merged "mt5":

      live_mt5  this process's own adapter read a terminal on THIS host.
      node_mt5  an execution NODE read the terminal and relayed the observation
                through `ct.node-telemetry.v1`.

    They differ in who observed, how far the data travelled, and which clock
    judges its freshness — and in this deployment only ONE of them can ever be
    real, because the MT5 terminal runs on the Windows VPS. Collapsing them
    would destroy the only evidence that distinguishes a first-hand reading from
    a relayed one, and would let a Mac claim direct broker connectivity it does
    not have.
"""
from __future__ import annotations

#: This process's own broker adapter read a local MT5 terminal.
PROV_LIVE_MT5 = "live_mt5"
#: An execution node observed an MT5 terminal and relayed it via node telemetry.
PROV_NODE_MT5 = "node_mt5"
#: The in-process development fixture. Never operational truth.
PROV_MOCK_FIXTURE = "mock-fixture"
#: Nothing was observed. NOT a fact about the account — a fact about the read.
PROV_ABSENT = "absent"

#: Adapter kinds that, WHEN THEY ACTUALLY OBSERVE SOMETHING, yield real broker
#: truth. Mirrors `broker_adapter.known_kinds()`; kept as an explicit policy
#: constant so widening it is a deliberate edit to this file, reviewed on its
#: own, rather than a side effect of adding an adapter elsewhere.
_LOCAL_ORIGINS = {
    "mt5": PROV_LIVE_MT5,
    "mock": PROV_MOCK_FIXTURE,
}


def for_local_adapter(adapter_kind, *, observed: bool) -> str:
    """Provenance for a record produced by THIS process's broker adapter.

    `observed` must be True only when the adapter actually returned the data in
    the record — not when it was merely selected, constructed, or configured.
    The caller holds that evidence (a snapshot dict, an account dict), so the
    decision is passed in rather than guessed at here.

    An unknown adapter kind fails closed to `absent`: a kind this policy has
    never heard of cannot be vouched for, and inventing an origin for it is the
    same mistake in a new costume.
    """
    if not observed:
        return PROV_ABSENT
    if not isinstance(adapter_kind, str):
        return PROV_ABSENT
    return _LOCAL_ORIGINS.get(adapter_kind.strip().lower(), PROV_ABSENT)


def for_node_observation(*, observed: bool) -> str:
    """Provenance for a record projected from an execution node's telemetry.

    `observed` is the node's OWN availability claim for the section in question
    (`account.health.available`, `account.identity.available`), never the mere
    arrival of a snapshot. A node that published successfully while sampling
    nothing has told us it observed nothing; treating its silence as an
    observation is precisely "treat successful node telemetry as complete
    account truth", which this milestone forbids.
    """
    return PROV_NODE_MT5 if observed else PROV_ABSENT


def is_broker_truth(provenance) -> bool:
    """Does this provenance name a real broker origin?

    Used by guards and tests. Deliberately excludes `mock-fixture` and `absent`,
    and deliberately does NOT accept `durable-store`: storage class is not
    origin (M-TRADES-1).
    """
    return provenance in (PROV_LIVE_MT5, PROV_NODE_MT5)
