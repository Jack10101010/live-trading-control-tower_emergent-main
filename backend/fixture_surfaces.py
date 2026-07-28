"""FIX-2 — which API surfaces are backed only by the development fixture.

THE REGRESSION THIS REPAIRS
    HARDEN-1 removed the fixture boot dependency but gated only the five
    surfaces that were noticed by hand. Sixteen others kept answering `200` with
    an empty body when the fixture was absent:

        /api/deployments      /api/broker/positions   /api/broker/accounts
        /api/recommendations  /api/broker/orders      /api/events  ...

    `/api/broker/positions -> []` reads as "no open positions". For a trading
    system that is a dangerous falsehood, and it violated the milestone's own
    rule: never silently fabricate data. The failure mode had become WORSE than
    the bug it replaced — previously the process refused to start, which was
    loud and safe; afterwards it started and lied.

WHY DERIVED, NOT HAND-LISTED
    A hand-written allowlist is exactly what failed: it encoded what one person
    noticed on one day. This module DERIVES the answer from the source instead —
    a route is fixture-backed if its handler, or any helper it transitively
    calls, reads `WORLD`. The analysis is the same one an auditor would perform
    by hand, executed mechanically so it cannot drift.

    The derived set is used two ways:
      * at runtime, to decide whether a request must be refused;
      * in a test, to assert every fixture-reading route is actually refused —
        so adding a new fixture-reading route without gating it FAILS the suite
        rather than shipping another silent-empty endpoint.

WHAT IS DELIBERATELY NOT GATED
    Surfaces that merely MENTION the fixture while having a real production
    source. `/api/events` merges durable events with fixture events; with no
    fixture it still reports genuine recorded events, so refusing it would hide
    real data. Those are listed in `PRODUCTION_BACKED` with the reason, and the
    completeness test honours that list — the only hand-maintained part, and it
    exists to prevent OVER-refusal rather than to permit under-refusal.
"""

from __future__ import annotations

import ast
import functools
from pathlib import Path

#: Helpers whose fixture reads must NOT make their callers fixture-backed.
#:
#: `_broker_context` injects fixture callables (`accounts`, `brokers`,
#: `live_trades`, `trade_current`) into the context handed to EVERY adapter — but
#: an AST scan of `broker.py` shows every consumer of those callables lives in
#: `MockBroker`. `MT5Adapter` reads none of them. So a route reaching the broker
#: is ADAPTER-backed, not fixture-backed: with a live adapter it reports genuine
#: broker truth, and refusing it because the mock happens to read a fixture would
#: hide real data.
#:
#: Treating these as boundaries is what keeps the derivation honest in BOTH
#: directions — the earlier regression was under-refusal, and blanket-gating
#: everything downstream of the broker context would simply be over-refusal.
#: This list is short and about BOUNDARIES, not routes; the route set stays
#: derived.
BOUNDARY_HELPERS = frozenset({
    "_broker_context",        # fixture callables consumed only by MockBroker
    "_execution_env",         # same context, execution side
    "_operator_id",           # now resolves from operator_identity, not WORLD
})

#: Routes that read the fixture but ALSO have a genuine production source, so an
#: absent fixture degrades them rather than invalidating them. Each entry states
#: why, because "why is this one allowed through" is the question a future
#: auditor will ask.
PRODUCTION_BACKED: dict[str, str] = {
    "/api/events": (
        "merges durable events.db with fixture seed events; without the fixture "
        "it still reports genuine recorded events"),
    "/api/events/live": (
        "same durable event stream, filtered; genuinely empty means no events"),
    "/api/health": (
        "liveness must answer even when every data source is unavailable"),
    # CORRECTED. The earlier reason claimed "with a live adapter it is genuine
    # broker truth", which was FALSE: with the MT5 adapter DISCONNECTED these
    # answered `200 []`, i.e. Class B (UNAVAILABLE) reported as Class A
    # (TRUTHFUL EMPTY). They now return 503 `broker_unavailable` unless the
    # adapter is genuinely connected, so an empty list means CONNECTED-and-flat
    # and nothing else. They stay out of the fixture gate because their owner is
    # the adapter, not the fixture.
    "/api/broker/positions": (
        "adapter-owned; returns 503 broker_unavailable unless connected, so an "
        "empty list means connected-and-flat rather than unreachable"),
    "/api/broker/orders": "adapter-owned; same connected-or-503 rule as positions",
    "/api/broker/accounts": "adapter-owned; same connected-or-503 rule as positions",
    "/api/broker-health": "reports adapter connection state, not fixture content",
    "/api/execution/dry-run": (
        "simulates against the active adapter; a dry run with no fixture is a "
        "legitimately empty simulation, not a fabricated one"),
    "/api/operator/preferences": (
        "persisted in the runtime overlay store; the fixture only supplies "
        "defaults"),
    "/api/operator/commands": (
        "durable command lifecycle; the fixture only supplies an operator id"),
    # Incidental fixture use — the PAYLOAD comes from a production source and the
    # fixture supplies only metadata (an `asOf`, a seed count). Refusing these
    # would hide genuine data, which is the mirror-image mistake.
    "/api/runtime/health": (
        "runtime/event-store liveness; reads fixture event counts only as a "
        "seed offset. Liveness must answer even with no fixture"),
    "/api/market-data/candles": (
        "served by the market-data engine (fixture/replay/MT5 provider); the "
        "fixture is consulted only for `meta.asOf`"),
    "/api/broker/sync": (
        "drives real reconciliation through the active adapter"),
    "/api/broker/reconciliation": (
        "reports the durable reconciliation posture from execution_state.db"),
    "/api/": "service banner; reads only `meta` for a version string",
}


def analyse(source_path: Path) -> dict[str, set]:
    """Derive route -> the fixture collections it transitively reads.

    Pure static analysis of one module's source. Returns `{path: {keys}}` for
    every route whose handler reaches `WORLD`, so a caller can both gate and
    explain.
    """
    tree = ast.parse(Path(source_path).read_text())
    functions = {node.name: node for node in tree.body
                 if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}

    def route_paths(node) -> list:
        paths = []
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            target = decorator.func
            owner = getattr(getattr(target, "value", None), "id", None)
            verb = getattr(target, "attr", None)
            if owner not in ("api_router", "app"):
                continue
            # Only HTTP-verb decorators declare a route. `@app.middleware("http")`
            # also matches owner/arg shape and would otherwise contribute a
            # phantom "http" surface — which it did, and the gate then answered
            # 501 for a path that does not exist.
            if verb not in ("get", "post", "put", "patch", "delete", "head",
                            "options"):
                continue
            for arg in decorator.args:
                if (isinstance(arg, ast.Constant) and isinstance(arg.value, str)
                        and arg.value.startswith("/")):
                    prefix = "/api" if owner == "api_router" else ""
                    paths.append(prefix + arg.value)
        return paths

    def world_keys(node) -> set:
        """Fixture collections read directly by this function."""
        keys = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if (getattr(getattr(func, "value", None), "id", None) == "WORLD"
                        and getattr(func, "attr", None) == "get"
                        and child.args
                        and isinstance(child.args[0], ast.Constant)):
                    keys.add(child.args[0].value)
            elif (isinstance(child, ast.Subscript)
                    and getattr(child.value, "id", None) == "WORLD"):
                keys.add("<subscript>")
            elif isinstance(child, ast.Name) and child.id == "WORLD":
                keys.add("<reference>")
        return keys

    def called_names(node) -> set:
        return {getattr(call.func, "id", None) for call in ast.walk(node)
                if isinstance(call, ast.Call)} & set(functions)

    #: Transitive closure: a handler is fixture-backed if anything it calls is.
    resolved: dict[str, set] = {}

    def keys_for(name: str, seen: frozenset = frozenset()) -> set:
        if name in resolved:
            return resolved[name]
        if name in seen:
            return set()                      # recursion guard
        node = functions[name]
        keys = set(world_keys(node))
        for callee in called_names(node):
            if callee in BOUNDARY_HELPERS:
                continue          # an adapter boundary, not a fixture read
            keys |= keys_for(callee, seen | {name})
        resolved[name] = keys
        return keys

    for boundary in BOUNDARY_HELPERS:
        resolved[boundary] = set()          # pinned before any walk reaches it

    surfaces: dict[str, set] = {}
    for name, node in functions.items():
        paths = route_paths(node)
        if not paths:
            continue
        keys = keys_for(name)
        for path in paths:
            if keys:
                surfaces[path] = keys
    return surfaces


def gated_paths(surfaces: dict[str, set]) -> set:
    """The derived surfaces MINUS those with a genuine production source."""
    return {path for path in surfaces if path not in PRODUCTION_BACKED}


def requires_fixture(surface: str):
    """Mark a handler as backed only by the fixture world.

    The decorated handler runs only when the fixture is available; otherwise it
    returns the canonical explicit-unavailable response. Applied declaratively so
    a reader of the route sees the dependency, and verified for COMPLETENESS by
    the derived analysis above.
    """
    def decorate(handler):
        @functools.wraps(handler)
        def guard(*args, **kwargs):
            import server                                     # late: cycle-free
            if not server.WORLD.available:
                return server._fixture_unavailable(surface)
            return handler(*args, **kwargs)

        @functools.wraps(handler)
        async def async_guard(*args, **kwargs):
            import server
            if not server.WORLD.available:
                return server._fixture_unavailable(surface)
            return await handler(*args, **kwargs)

        import inspect
        chosen = async_guard if inspect.iscoroutinefunction(handler) else guard
        chosen.__fixture_surface__ = surface                  # discoverable
        return chosen
    return decorate
