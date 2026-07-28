"""LIVE-5B — the deterministic integration smoke checklist.

Twelve stages, each reporting an explicit PASS or FAIL with a reason. Run it on
the VPS before the first trade; run it again whenever something looks wrong.

    python3 -m integration_smoke            # from backend/

WHAT IT DOES AND DOES NOT DO
    It READS. It refreshes the runtime, inspects the projection and the stores,
    and reports. It never places, modifies or cancels an order, and it never
    accepts a Recommendation on an operator's behalf — the acceptance and order
    stages are therefore reported as OBSERVED state, not performed actions.

    Stages that cannot be evaluated report FAIL with the reason, never PASS by
    omission. A checklist that quietly skips a stage is worse than no checklist.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable

PASS = "PASS"
FAIL = "FAIL"

#: The twelve stages, in the order the pipeline traverses them.
STAGES = (
    "broker_connected", "quotes_updating", "candles_updating",
    "scenario_created", "recommendation_created", "operator_acceptance",
    "intent_created", "order_submitted", "position_appears",
    "position_updates", "position_closes", "ledger_updated",
)


@dataclass(frozen=True)
class StageResult:
    stage: str
    status: str
    detail: str | None = None
    fix: str | None = None

    def as_dict(self) -> dict:
        return {"stage": self.stage, "status": self.status,
                "detail": self.detail, "fix": self.fix}

    def line(self) -> str:
        base = f"[{self.status}] {self.stage:24} {self.detail or ''}"
        return base if self.status == PASS else f"{base}\n        fix: {self.fix or '-'}"


@dataclass
class SmokeReport:
    at: str
    results: list = field(default_factory=list)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.status == PASS)

    @property
    def complete(self) -> bool:
        return bool(self.results) and all(r.status == PASS for r in self.results)

    @property
    def first_failure(self) -> StageResult | None:
        return next((r for r in self.results if r.status == FAIL), None)

    def as_dict(self) -> dict:
        return {"at": self.at, "complete": self.complete,
                "passed": self.passed, "total": len(self.results),
                "firstFailure": (self.first_failure.stage
                                 if self.first_failure else None),
                "stages": [r.as_dict() for r in self.results]}

    def render(self) -> str:
        head = (f"LIVE-5B integration smoke — {self.passed}/{len(self.results)} "
                f"stages PASS at {self.at}")
        body = "\n".join(r.line() for r in self.results)
        tail = ("\nALL STAGES PASS — the pipeline completed end to end."
                if self.complete else
                f"\nFIRST FAILURE: {self.first_failure.stage}. "
                "Fix that stage and re-run; later stages depend on it.")
        return f"{head}\n{'-' * len(head)}\n{body}\n{tail}"


def run(*, live_runtime: dict, scenarios: Any = None, recommendations: Any = None,
        ledger_trades: Any = None, now: str = "") -> SmokeReport:
    """Evaluate all twelve stages from already-read evidence.

    Pure: every input is passed in, so the checklist is deterministic and
    testable without a broker.
    """
    report = SmokeReport(at=now or str(live_runtime.get("projectionTimestamp") or ""))
    add = report.results.append

    broker = live_runtime.get("broker") or {}
    runtime = live_runtime.get("runtime") or {}
    symbols = live_runtime.get("symbols") or []
    execution = live_runtime.get("execution") or {}

    # 1 ── broker connected
    if broker.get("connected"):
        add(StageResult("broker_connected", PASS,
                        f"{broker.get('adapterKind')} · {broker.get('connectionState')}"
                        f" · ping {broker.get('pingMs')}ms"))
    else:
        add(StageResult("broker_connected", FAIL,
                        str(broker.get("connectionState")),
                        "GET /api/integration/diagnostics and fix the reported "
                        "root cause"))

    # 2 ── quotes updating
    live_symbols = [s for s in symbols
                    if s.get("live") and s.get("availability") == "ok"]
    if live_symbols:
        first = live_symbols[0]
        add(StageResult("quotes_updating", PASS,
                        f"{first['symbol']} bid {first['bid']} ask {first['ask']} "
                        f"spread {first['spread']} age {first['quoteAgeSeconds']}s"))
    else:
        add(StageResult("quotes_updating", FAIL,
                        f"{len(symbols)} symbols, none quoting fresh",
                        "add the symbol to Market Watch; confirm the broker "
                        "symbol suffix; check the market is open"))

    # 3 ── candles updating
    fresh_candles = [s for s in symbols if s.get("candleAvailability") == "ok"]
    if fresh_candles:
        first = fresh_candles[0]
        add(StageResult("candles_updating", PASS,
                        f"{first['symbol']} {first['candleTimeframe']} closed "
                        f"{first['candleClosedAt']} age {first['candleAgeSeconds']}s"))
    else:
        add(StageResult("candles_updating", FAIL,
                        "no symbol has a fresh completed candle",
                        "set MARKET_DATA_PROVIDER=mt5; confirm history is "
                        "downloaded in the terminal for the timeframe"))

    # 4 ── scenario created
    scenario_count = _count(scenarios)
    if scenario_count:
        add(StageResult("scenario_created", PASS, f"{scenario_count} scenario(s)"))
    else:
        add(StageResult("scenario_created", FAIL, "no scenarios recorded",
                        "set LIVE_PRODUCER_ENABLED=1 and wait for an M15 candle "
                        "that closes beyond the previous candle's range"))

    # 5 ── recommendation created
    recommendation_count = _count(recommendations)
    if recommendation_count:
        add(StageResult("recommendation_created", PASS,
                        f"{recommendation_count} recommendation(s), "
                        f"{execution.get('openRecommendations')} active"))
    else:
        add(StageResult("recommendation_created", FAIL,
                        "no recommendations recorded",
                        "a Scenario must exist first; each one produces exactly "
                        "one Recommendation"))

    # 6 ── operator acceptance (OBSERVED, never performed here)
    accepted = _with_status(recommendations, {"ACCEPTED", "INTENT_CREATED",
                                              "PARTIALLY_EXECUTED", "EXECUTED"})
    if accepted:
        add(StageResult("operator_acceptance", PASS,
                        f"{len(accepted)} accepted by an operator"))
    else:
        add(StageResult("operator_acceptance", FAIL, "none accepted",
                        "review a PROPOSED recommendation and accept it in the "
                        "UI. This checklist never accepts on your behalf"))

    # 7 ── intent created
    linked = [r for r in accepted if getattr(r, "linked_intent_ids", ())]
    if linked:
        add(StageResult("intent_created", PASS,
                        f"{len(linked)} recommendation(s) linked to an intent"))
    else:
        add(StageResult("intent_created", FAIL, "no intent linked",
                        "submit the order with recommendationId in the payload "
                        "so lineage is captured"))

    # 8 ── order submitted
    executing = _with_status(recommendations, {"INTENT_CREATED",
                                               "PARTIALLY_EXECUTED", "EXECUTED"})
    if executing:
        add(StageResult("order_submitted", PASS,
                        f"{len(executing)} recommendation(s) reached the broker"))
    else:
        add(StageResult("order_submitted", FAIL, "no order submitted",
                        "POST /api/execution/market-order with an "
                        "Idempotency-Key; check the preflight endpoint first"))

    # 9 ── position appears
    positions = execution.get("activePositions")
    if execution.get("positionsAvailable") and positions:
        add(StageResult("position_appears", PASS, f"{positions} open position(s)"))
    else:
        add(StageResult("position_appears", FAIL,
                        f"{positions if execution.get('positionsAvailable') else 'unavailable'}",
                        "if the order filled but no position shows, run "
                        "reconciliation: the broker is the authority"))

    # 10 ── position updates (the runtime is refreshing it)
    if runtime.get("state") == "CONNECTED" and (runtime.get("tickCount") or 0) > 1:
        add(StageResult("position_updates", PASS,
                        f"runtime CONNECTED, {runtime.get('tickCount')} ticks, "
                        f"projection age {runtime.get('projectionAgeSeconds')}s"))
    else:
        add(StageResult("position_updates", FAIL,
                        f"runtime {runtime.get('state')} after "
                        f"{runtime.get('tickCount')} tick(s)",
                        "the refresh loop must be CONNECTED for positions to "
                        "update; see the runtime card and diagnostics"))

    # 11 ── position closes
    closed = _count(ledger_trades)
    if closed:
        add(StageResult("position_closes", PASS,
                        f"{closed} closed trade(s) reconstructed"))
    else:
        add(StageResult("position_closes", FAIL, "no closed trade observed",
                        "close the position (or let stop/target resolve it), "
                        "then refresh the ledger"))

    # 12 ── ledger updated with full lineage
    with_lineage = _with_lineage(ledger_trades)
    if with_lineage:
        add(StageResult("ledger_updated", PASS,
                        f"{with_lineage} trade(s) carry Scenario + "
                        "Recommendation lineage"))
    else:
        add(StageResult("ledger_updated", FAIL,
                        "no closed trade carries full lineage",
                        "lineage requires the intent to have carried "
                        "recommendationId and scenarioId at submission"))
    return report


def _count(items: Any) -> int:
    try:
        return len(list(items or ()))
    except TypeError:
        return 0


def _with_status(items: Any, statuses: set) -> list:
    out = []
    for item in (items or ()):
        if getattr(item, "status", None) in statuses:
            out.append(item)
    return out


def _with_lineage(trades: Any) -> int:
    count = 0
    for trade in (trades or ()):
        lineage = getattr(trade, "lineage", None)
        if (lineage is not None and getattr(lineage, "scenario_id", None)
                and getattr(lineage, "recommendation_id", None)):
            count += 1
    return count


def main(argv: list | None = None) -> int:
    """CLI entry point. Exit 0 only when all twelve stages PASS."""
    argv = argv if argv is not None else sys.argv[1:]
    as_json = "--json" in argv
    sys.path.insert(0, ".")
    import server                                          # noqa: PLC0415

    server._RUNTIME_SUPERVISOR.tick_once()                 # one fresh read
    live = server._live_runtime_view().as_dict()
    scenario_store = server._scenario_store()
    recommendation_store = server._recommendation_store()
    ledger_store = server._ledger_store()
    report = run(
        live_runtime=live,
        scenarios=(scenario_store.list_scenarios(limit=200)
                   if scenario_store else None),
        recommendations=(recommendation_store.list_recommendations(limit=200)
                         if recommendation_store else None),
        ledger_trades=None if ledger_store is None else _ledger_trades(ledger_store),
        now=server._now_iso())
    print(json.dumps(report.as_dict(), indent=2) if as_json else report.render())
    return 0 if report.complete else 1


def _ledger_trades(store) -> list:
    for method in ("list_trades", "list_entries"):
        reader = getattr(store, method, None)
        if callable(reader):
            try:
                return list(reader(limit=200) or [])
            except Exception:                               # noqa: BLE001
                return []
    return []


if __name__ == "__main__":
    raise SystemExit(main())
