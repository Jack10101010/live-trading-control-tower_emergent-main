"""M-MOCK-DECOUPLE-1 — the MockBroker's own dataset. A TEST DOUBLE, not a preview.

WHAT THIS REPLACES
    `server._broker_context()` bound the broker's `accounts`, `live_trades`,
    `trade_current` and `trade_by_order_id` callables to the UI fixture world.
    Only `MockBroker` consumed them — `MT5Adapter` reads nothing but `ctx.now`,
    verified by AST — yet a single request to `/api/operations/accounts` under
    the development-default mock adapter lazily loaded all 22 collections of
    `world.v1.json`.

    M-WORLD-ISOLATE-1 made that load lazy. This makes it unnecessary: the mock
    adapter now carries the handful of records it actually needs, and the
    runtime source tree stops depending on the fixture file altogether.

WHY THIS IS NOT THE FIXTURE WORLD IN PYTHON
    The fixture is AUTHORED DEMONSTRATION DATA for `/api/dev/*` previews: 22
    collections, deployments, packages, decision chains, regime snapshots. This
    module is a TEST DOUBLE: the minimum an in-process broker stub needs to
    answer `accounts()`, `positions()` and `orders()` and to accept a close.

    Those are different things with different lifetimes, and collapsing them is
    what created the coupling in the first place. Dev-preview routes must not
    read this module, and a guard asserts they do not.

WHY THE SENTINEL FIGURES ARE UNCHANGED
    `balance 100000.0` / `equity 100412.0` are deliberately preserved. Half a
    dozen honesty guards across the suite scan for exactly those numbers to
    prove invented money never reaches an operator surface. Making the mock
    "less conspicuous" would blind every one of them — the conspicuousness is
    the feature.

WHY EVERY FIELD IS HERE, AND NO OTHERS
    Derived by AST from `MockBroker`'s own reads, not from what the fixture
    happened to contain. `MockBroker` reads exactly:

        accounts    accountId, brokerId, type, baseCurrency, balance, equity
        trades      tradeId, brokerOrderId, deploymentId, scenarioKey, lane,
                    state, size, entry, sl, tp

    `fundedRules`, `packageHash`, `decisionId`, `originalPlan`, `currentR`,
    `floatingPl`, `protectionStatus`, `management` and the rest of the fixture's
    trade shape are absent because nothing in the broker path reads them. The
    risk-limits contract asserts that funded rules CANNOT influence a response,
    so omitting them here strengthens that guarantee rather than weakening it.

IMMUTABILITY
    The constants are tuples of frozen mappings and every accessor returns a
    deep copy. `MockBroker` operates against a mutable overlay store, and a
    shared mutable base would let one request alter another's balance — the
    exact cross-test coupling M-WORLD-ISOLATE-1 removed from the fixture path.
"""
from __future__ import annotations

import copy
from types import MappingProxyType

#: The one mock broker. `MockBroker` does not read `ctx.brokers()` at all (AST
#: confirmed), so this exists only for the diagnostics surfaces that name a
#: broker id; it is deliberately not wired into the broker context.
MOCK_BROKER = MappingProxyType({
    "brokerId": "brk_mock_0000000000000000000000",
    "name": "Mock Broker (in-process test double)",
    "connection": "Connected",
})

#: One account. `type: "funded"` is retained because the operational projection
#: and several tests branch on it; the funded RULES are not, because nothing in
#: the broker path reads them.
MOCK_ACCOUNTS = (
    MappingProxyType({
        "accountId": "acct_mock_000000000000000000000",
        "brokerId": MOCK_BROKER["brokerId"],
        "type": "funded",
        "baseCurrency": "USD",
        # PRESERVED SENTINELS — see the module docstring. Do not "clean these up".
        "balance": 100000.0,
        "equity": 100412.0,
    }),
)

#: Two trades: one OPEN (`managing`) and one CLOSED.
#:
#: The open one is required — `MockBroker.positions()` filters on `state` and
#: several tests assert at least one open position exists, one of them cloning
#: it to prove two entities stay independent. The closed one keeps
#: `positions()`'s filter honest: with only open records, a broken filter would
#: still pass.
#:
#: `scenarioKey` carries the pair AND the side: `MockBroker` derives both from
#: it (`scenarioKey.split(":")[0]`, and `"long" in scenarioKey`). That is the
#: stub's own parsing rule, mirrored here rather than changed.
MOCK_LIVE_TRADES = (
    MappingProxyType({
        "tradeId": "tr_mock_open_00000000000000000",
        "brokerOrderId": "mock-order-1",
        "deploymentId": "dpl_mock_0000000000000000000",
        "scenarioKey": "EURUSD:london:BOS:long:BullExpand",
        "lane": "live",
        "state": "managing",
        "size": 0.65,
        "entry": 1.0845,
        "sl": 1.0830,
        "tp": 1.08862,
    }),
    MappingProxyType({
        "tradeId": "tr_mock_closed_0000000000000000",
        "brokerOrderId": "mock-order-2",
        "deploymentId": "dpl_mock_0000000000000000000",
        "scenarioKey": "EURUSD:asia:CHoCH:long:BullChop",
        "lane": "live",
        "state": "closed",
        "size": 0.40,
        "entry": 1.0810,
        "sl": 1.0795,
        "tp": 1.0855,
    }),
)


def accounts() -> list[dict]:
    """A fresh mutable copy of the mock accounts."""
    return [dict(a) for a in MOCK_ACCOUNTS]


def brokers() -> list[dict]:
    """A fresh mutable copy of the mock brokers."""
    return [dict(MOCK_BROKER)]


def live_trades() -> list[dict]:
    """Fresh mutable copies of the mock trades.

    Deep-copied per call: `MockBroker` hands these to overlay application, and a
    shared base would let one request's close leak into the next.
    """
    return [copy.deepcopy(dict(t)) for t in MOCK_LIVE_TRADES]


def trade_ids() -> tuple:
    """Every mock trade id, for tests that need to name one without hard-coding."""
    return tuple(t["tradeId"] for t in MOCK_LIVE_TRADES)


def open_trade_id() -> str:
    """The one `managing` trade. Tests clone this to prove entity independence."""
    return next(t["tradeId"] for t in MOCK_LIVE_TRADES if t["state"] == "managing")
