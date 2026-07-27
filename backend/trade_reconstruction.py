"""LIVE-4C — deterministic CLOSED-TRADE RECONSTRUCTION. One owner.

Turns broker history (deals, orders, closed-position evidence) plus recorded
lineage into canonical `ClosedTrade` facts. It is a pure function of its
inputs: no I/O, no clock, no persistence, no broker call, no state.

GROUPING SEMANTICS — stated, not assumed
    Deals are grouped by BROKER POSITION ID. Within a group, DEAL_ENTRY
    determines direction:

        entry=in    opens or SCALES IN     -> contributes to entry quantity
        entry=out   closes or SCALES OUT   -> contributes to exit quantity
        entry=inout REVERSAL               -> cannot be split into one trade
        entry=out_by CLOSED BY OPPOSITE    -> cannot be attributed by price

    This supports, by construction:
      * one order producing several deals (many deals share an order id);
      * several entry orders contributing to one position (several `in` deals);
      * one position closing through several exit deals;
      * partial close followed by final close;
      * scale-in and scale-out (weighted averages over each side).

ACCOUNT-MODE HONESTY (the fail-closed rule)
    Grouping by position id is only sound when a position id identifies ONE
    economic trade. That holds on HEDGING accounts. On NETTING accounts a
    single position id accumulates and nets independent trades, so the
    boundaries reconstructed here would be wrong.

    Therefore: on `netting`, `exchange` or `unknown` account mode the trade is
    reconstructed and FULLY VISIBLE, but it is marked with a blocking warning
    so finalization fails closed. Nothing is silently reconstructed under
    hedging assumptions.

WHAT THIS MODULE MUST NEVER DO
    call a broker write, mutate execution or scenario state, authorize an
    operation, infer strategy intent, or hide incomplete evidence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import broker_history as bh
import trade_ledger_domain as tld

# ── blocking reasons (prevent finalization; always surfaced) ──────────────────
BLOCK_ACCOUNT_MODE = "account_mode_unsupported_for_finalization"
BLOCK_STILL_OPEN = "position_still_open"
BLOCK_NO_CLOSING_DEAL = "no_closing_deal_observed"
BLOCK_QUANTITY_MISMATCH = "exit_quantity_exceeds_entry_quantity"
BLOCK_UNSUPPORTED_ENTRY = "unsupported_deal_entry_direction"
BLOCK_SCENARIO_CONFLICT = "conflicting_scenario_lineage"
BLOCK_DUPLICATE_DEAL = "duplicate_deal_evidence"
BLOCK_RECONCILIATION = "unresolved_material_reconciliation_finding"

#: Non-blocking warnings — visible, but they do not prevent finalization.
WARN_COSTS_PENDING = "cost_evidence_pending"
WARN_RISK_UNAVAILABLE = "initial_risk_evidence_unavailable"
WARN_SCENARIO_UNLINKED = "scenario_lineage_unavailable"
WARN_PARTIAL_CLOSE = "position_partially_closed"
WARN_NO_ORDER_HISTORY = "broker_order_history_unavailable"

#: Only a HEDGING book lets a position id stand for exactly one trade.
FINALIZABLE_ACCOUNT_MODES = frozenset({bh.MODE_HEDGING})

#: Reconciliation classes that are MATERIAL to a trade's economics. An
#: unresolved finding in these classes blocks finalization; anything else is
#: recorded as a visible finding but does not gate the record.
MATERIAL_RECONCILIATION_CLASSES = frozenset({
    "account_identity_mismatch", "duplicate_broker_reference",
    "quantity_mismatch", "missing_broker_reference", "unknown",
})


@dataclass(frozen=True)
class ReconstructionInput:
    """Everything reconstruction is allowed to see. Explicit and inert."""
    history: bh.BrokerHistorySnapshot
    #: intent rows (execution store) — used ONLY for recorded lineage/quality.
    intents: tuple = field(default_factory=tuple)
    #: scenario objects with explicit link tuples.
    scenarios: tuple = field(default_factory=tuple)
    #: unresolved reconciliation items (`class`, `entity_id`, `critical`, ...).
    reconciliation_items: tuple = field(default_factory=tuple)
    #: lifecycle transitions per intent id, for management history.
    transitions: dict = field(default_factory=dict)
    node_id: str | None = None
    adapter: str | None = None
    broker: str | None = None
    deployment: str | None = None


@dataclass(frozen=True)
class ReconstructionResult:
    """One reconstructed trade plus the reasons it may not be finalized."""
    trade: tld.ClosedTrade
    blocking: tuple = field(default_factory=tuple)
    warnings: tuple = field(default_factory=tuple)
    conflicts: tuple = field(default_factory=tuple)
    reconciliation_findings: tuple = field(default_factory=tuple)
    management_history: tuple = field(default_factory=tuple)

    @property
    def finalizable(self) -> bool:
        return not self.blocking

    @property
    def status(self) -> str:
        if self.conflicts:
            return tld.TradeLedgerStatus.CONFLICTED
        if self.blocking:
            return tld.TradeLedgerStatus.INCOMPLETE
        return tld.TradeLedgerStatus.READY_TO_FINALIZE


def _weighted_average(pairs) -> float | None:
    """Volume-weighted average price. None when no usable evidence exists —
    never zero."""
    usable = [(p, v) for p, v in pairs
              if p is not None and v is not None and v > 0]
    if not usable:
        return None
    total_volume = sum(v for _, v in usable)
    if total_volume <= 0:
        return None
    return round(sum(p * v for p, v in usable) / total_volume, 8)


def _sum_or_none(values) -> float | None:
    present = [v for v in values if v is not None]
    return round(sum(present), 8) if present else None


def _cost_summary(deals) -> tld.TradeCostSummary:
    """Aggregate realized costs. A broker that reports nothing yields
    UNAVAILABLE; one that reports some components yields PARTIAL. Nothing is
    coerced to zero."""
    commissions = [d.costs.commission for d in deals if d.costs.commission is not None]
    fees = [d.costs.fee for d in deals if d.costs.fee is not None]
    swaps = [d.costs.swap for d in deals if d.costs.swap is not None]
    groups = (commissions, fees, swaps)
    present = sum(1 for g in groups if g)
    if present == 0:
        return tld.TradeCostSummary(completeness=tld.UNAVAILABLE)
    completeness = tld.COMPLETE if present == 3 else tld.PARTIAL
    return tld.TradeCostSummary(
        commission=round(sum(commissions), 8) if commissions else None,
        fees=round(sum(fees), 8) if fees else None,
        swap=round(sum(swaps), 8) if swaps else None,
        other_costs=None, completeness=completeness)


def _risk_summary(*, intent_row: dict | None, average_entry: float | None,
                  entry_quantity: float | None,
                  gross_pnl: float | None) -> tld.TradeRiskSummary:
    """Realized R from GROUNDED initial-risk evidence only.

    The initial stop must come from the ORIGINAL intent (`stop_loss` recorded
    at intent creation). A later protective modification is deliberately NOT
    consulted — inferring intended risk from a moved stop would fabricate it.

    POLICY: realized R uses GROSS realized PnL (see TradeRiskSummary)."""
    if not intent_row:
        return tld.TradeRiskSummary(completeness=tld.UNAVAILABLE)
    initial_stop = intent_row.get("stop_loss")
    initial_entry = intent_row.get("entry")
    if initial_stop is None:
        return tld.TradeRiskSummary(initial_entry_price=initial_entry,
                                    completeness=tld.UNAVAILABLE)
    basis_entry = initial_entry if initial_entry is not None else average_entry
    if basis_entry is None or entry_quantity is None:
        return tld.TradeRiskSummary(initial_stop_price=initial_stop,
                                    initial_entry_price=initial_entry,
                                    completeness=tld.PARTIAL)
    risk_amount = abs(basis_entry - initial_stop) * entry_quantity
    if risk_amount <= 0:
        return tld.TradeRiskSummary(initial_stop_price=initial_stop,
                                    initial_entry_price=basis_entry,
                                    initial_risk_amount=0.0,
                                    completeness=tld.PARTIAL)
    realized_r = (round(gross_pnl / risk_amount, 6)
                  if gross_pnl is not None else None)
    return tld.TradeRiskSummary(
        initial_stop_price=initial_stop, initial_entry_price=basis_entry,
        initial_risk_amount=round(risk_amount, 8), planned_r=1.0,
        realized_r=realized_r, realized_r_basis="gross",
        completeness=tld.COMPLETE if realized_r is not None else tld.PARTIAL)


def _quality(*, intent_row: dict | None, average_entry: float | None,
             average_exit: float | None, side: str | None) -> tld.TradeExecutionQuality:
    if not intent_row or average_entry is None:
        return tld.TradeExecutionQuality(average_fill_price=average_entry,
                                         completeness=tld.UNAVAILABLE)
    requested = intent_row.get("entry")
    if requested is None:
        # A market order requests no price — slippage is NOT APPLICABLE, which
        # is different from unavailable.
        return tld.TradeExecutionQuality(average_fill_price=average_entry,
                                         completeness=tld.NOT_APPLICABLE)
    signed = (average_entry - requested) if side == "long" else (requested - average_entry)
    return tld.TradeExecutionQuality(
        requested_entry_price=requested, average_fill_price=average_entry,
        entry_slippage=round(signed, 8), requested_exit_price=None,
        exit_slippage=None, completeness=tld.PARTIAL)


def _scenario_lineage(*, position_id: str, order_ids, intent_ids,
                      scenarios) -> tuple:
    """Resolve Scenario linkage from EXPLICIT identifiers only.

    Returns `(scenario_id, completeness, conflicts)`. Multiple distinct
    scenarios matching the same trade is a CONFLICT — never silently resolved
    by picking one."""
    matched: set = set()
    for scenario in scenarios:
        links = (set(getattr(scenario, "linked_position_ids", ()) or ())
                 | set(getattr(scenario, "linked_order_ids", ()) or ())
                 | set(getattr(scenario, "linked_intent_ids", ()) or ()))
        if not links:
            continue
        if position_id in links or links & set(order_ids) or links & set(intent_ids):
            matched.add(scenario.scenario_id)
    if not matched:
        return (None, tld.UNAVAILABLE, ())
    if len(matched) > 1:
        return (None, tld.CONFLICTED,
                (f"{BLOCK_SCENARIO_CONFLICT}: {sorted(matched)}",))
    return (next(iter(matched)), tld.COMPLETE, ())


def _management_history(intent_rows, transitions: dict) -> tuple:
    """Canonical projected management facts. This REFERENCES the execution
    lifecycle; it does not duplicate the execution store."""
    out: list = []
    for row in intent_rows:
        intent_id = row.get("intent_id")
        for step in (transitions.get(intent_id) or []):
            out.append({
                "intentId": intent_id,
                "command": row.get("command_name"),
                "state": step.get("to_state"),
                "at": step.get("at"),
                "reason": step.get("reason"),
                "brokerRef": step.get("broker_ref"),
            })
    out.sort(key=lambda m: (m.get("at") or "", m.get("intentId") or ""))
    return tuple(out)


def reconstruct(inputs: ReconstructionInput) -> tuple:
    """Reconstruct every trade visible in the supplied history.

    Deterministic and idempotent: the same inputs always yield the same
    results, in the same order, with the same identities."""
    history = inputs.history
    if not history.usable:
        return ()

    by_position: dict[str, list] = {}
    for deal in history.deals:
        if not deal.is_trade_deal or not deal.position_id:
            continue                    # non-trade deals never move exposure
        by_position.setdefault(deal.position_id, []).append(deal)

    open_ids = set(history.open_position_ids)
    results: list = []
    for position_id in sorted(by_position):
        deals = sorted(by_position[position_id], key=lambda d: (d.at or "", d.deal_id))
        results.append(_reconstruct_one(position_id, deals, inputs, open_ids))
    return tuple(results)


def _reconstruct_one(position_id: str, deals, inputs: ReconstructionInput,
                     open_ids: set) -> ReconstructionResult:
    history = inputs.history
    blocking: list = []
    warnings: list = []
    conflicts: list = []

    # -- duplicate evidence ---------------------------------------------------
    deal_ids = [d.deal_id for d in deals]
    if len(set(deal_ids)) != len(deal_ids):
        blocking.append(BLOCK_DUPLICATE_DEAL)
        deals = list({d.deal_id: d for d in deals}.values())

    # -- account-mode honesty (fail closed for finalization) -----------------
    if history.account_mode not in FINALIZABLE_ACCOUNT_MODES:
        blocking.append(f"{BLOCK_ACCOUNT_MODE}:{history.account_mode}")

    # -- split by DEAL_ENTRY --------------------------------------------------
    entries = [d for d in deals if d.entry == bh.ENTRY_IN]
    exits = [d for d in deals if d.entry == bh.ENTRY_OUT]
    unsupported = [d for d in deals
                   if d.entry in (bh.ENTRY_INOUT, bh.ENTRY_OUT_BY, bh.ENTRY_UNKNOWN)]
    if unsupported:
        blocking.append(
            f"{BLOCK_UNSUPPORTED_ENTRY}:{sorted({d.entry for d in unsupported})}")

    entry_quantity = _sum_or_none([d.volume for d in entries])
    exit_quantity = _sum_or_none([d.volume for d in exits])
    average_entry = _weighted_average([(d.price, d.volume) for d in entries])
    average_exit = _weighted_average([(d.price, d.volume) for d in exits])

    # -- closure state --------------------------------------------------------
    still_open = position_id in open_ids
    if still_open:
        blocking.append(BLOCK_STILL_OPEN)
    if not exits:
        blocking.append(BLOCK_NO_CLOSING_DEAL)

    residual = None
    fully_closed = False
    if entry_quantity is not None and exit_quantity is not None:
        residual = round(entry_quantity - exit_quantity, 8)
        if residual < -1e-9:
            blocking.append(BLOCK_QUANTITY_MISMATCH)
        fully_closed = abs(residual) <= 1e-9 and not still_open
        if residual > 1e-9:
            warnings.append(WARN_PARTIAL_CLOSE)

    # -- side, from the OPENING deal (not guessed from PnL) ------------------
    side = None
    if entries:
        side = "long" if entries[0].deal_type == "buy" else "short"

    # -- realized PnL ---------------------------------------------------------
    gross = _sum_or_none([d.profit for d in exits])
    costs = _cost_summary(deals)
    if costs.completeness == tld.UNAVAILABLE:
        warnings.append(WARN_COSTS_PENDING)
    net = (round(gross + costs.total, 8)
           if gross is not None and costs.total is not None else None)

    # -- lineage from RECORDED identifiers only ------------------------------
    order_ids = sorted({d.order_id for d in deals if d.order_id})
    matching_intents = [row for row in inputs.intents
                        if str(row.get("broker_ref") or "") in
                        ({position_id} | set(order_ids))]
    intent_ids = sorted({row.get("intent_id") for row in matching_intents
                         if row.get("intent_id")})
    # An intent may also carry the scenario id directly (LIVE-4B lineage).
    intent_scenarios = {row.get("scenario_id") for row in matching_intents
                        if row.get("scenario_id")}
    scenario_id, scenario_completeness, scenario_conflicts = _scenario_lineage(
        position_id=position_id, order_ids=order_ids, intent_ids=intent_ids,
        scenarios=inputs.scenarios)
    if intent_scenarios:
        candidates = intent_scenarios | ({scenario_id} if scenario_id else set())
        if len(candidates) > 1:
            scenario_id, scenario_completeness = None, tld.CONFLICTED
            scenario_conflicts = (f"{BLOCK_SCENARIO_CONFLICT}: {sorted(candidates)}",)
        else:
            scenario_id = next(iter(candidates))
            scenario_completeness = tld.COMPLETE
    if scenario_conflicts:
        conflicts.extend(scenario_conflicts)
        blocking.append(BLOCK_SCENARIO_CONFLICT)
    elif scenario_id is None:
        warnings.append(WARN_SCENARIO_UNLINKED)

    # -- origin (PART 19): a recorded intent means the tower opened it -------
    origin = (tld.TradeOrigin.CONTROL_TOWER if matching_intents
              else tld.TradeOrigin.MANUAL_BROKER if history.provenance == bh.PROV_LIVE_MT5
              else tld.TradeOrigin.UNKNOWN)

    # -- reconciliation findings ---------------------------------------------
    findings: list = []
    entity_refs = {position_id} | set(order_ids) | set(intent_ids)
    for item in inputs.reconciliation_items:
        entity = str(item.get("entity_id") or "")
        if entity and entity not in entity_refs:
            continue
        findings.append({"class": item.get("class"), "entityId": entity or None,
                         "critical": bool(item.get("critical")),
                         "detail": item.get("detail")})
        if str(item.get("class")) in MATERIAL_RECONCILIATION_CLASSES \
                and not item.get("resolved"):
            blocking.append(f"{BLOCK_RECONCILIATION}:{item.get('class')}")

    if not history.orders_available:
        warnings.append(WARN_NO_ORDER_HISTORY)

    # -- risk + quality from the ORIGINAL intent ------------------------------
    opening_intent = next((row for row in matching_intents
                           if row.get("kind") == "submit"), None) \
        or (matching_intents[0] if matching_intents else None)
    risk = _risk_summary(intent_row=opening_intent, average_entry=average_entry,
                         entry_quantity=entry_quantity, gross_pnl=gross)
    if risk.completeness in (tld.UNAVAILABLE, tld.PARTIAL):
        warnings.append(WARN_RISK_UNAVAILABLE)
    quality = _quality(intent_row=opening_intent, average_entry=average_entry,
                       average_exit=average_exit, side=side)

    # -- exit classification from EXPLICIT evidence ---------------------------
    closing = exits[-1] if exits else None
    exit_classification = tld.classify_exit(
        broker_reason=closing.reason if closing else None,
        exit_price=average_exit,
        stop_loss=(opening_intent or {}).get("stop_loss"),
        take_profit=(opening_intent or {}).get("take_profit"),
        entry_price=average_entry, fully_closed=fully_closed)

    trade_id = tld.TradeId.derive(
        account_fingerprint=history.account_fingerprint, position_id=position_id,
        instrument=deals[0].symbol,
        opening_deal_id=entries[0].deal_id if entries else None).value

    lineage = tld.TradeLineage(
        trade_id=trade_id, scenario_id=scenario_id,
        recommendation_id=None,                 # no recommendation link is recorded yet
        intent_ids=tuple(intent_ids),
        internal_operation_ids=tuple(sorted(
            {row.get("command_id") for row in matching_intents if row.get("command_id")})),
        broker_order_ids=tuple(order_ids),
        broker_position_ids=(position_id,),
        broker_deal_ids=tuple(sorted(d.deal_id for d in deals)),
        node_id=inputs.node_id, account_fingerprint=history.account_fingerprint,
        broker=inputs.broker, adapter=inputs.adapter, deployment=inputs.deployment,
        instrument=deals[0].symbol, origin=origin,
        scenario_completeness=scenario_completeness)

    trade = tld.ClosedTrade(
        trade_id=trade_id, instrument=deals[0].symbol, side=side,
        total_entry_quantity=entry_quantity, total_exit_quantity=exit_quantity,
        average_entry_price=average_entry, average_exit_price=average_exit,
        account_currency=history.account_currency,
        gross_realized_pnl=gross, net_realized_pnl=net,
        fully_closed=fully_closed, residual_quantity=residual,
        exit_classification=exit_classification, costs=costs, risk=risk,
        timing=tld.TradeTimingSummary.of(
            entries[0].at if entries else None,
            exits[-1].at if exits else None),
        quality=quality, lineage=lineage, account_mode=history.account_mode,
        provenance=history.provenance)

    return ReconstructionResult(
        trade=trade, blocking=tuple(sorted(set(blocking))),
        warnings=tuple(sorted(set(warnings))), conflicts=tuple(conflicts),
        reconciliation_findings=tuple(findings),
        management_history=_management_history(matching_intents, inputs.transitions))
