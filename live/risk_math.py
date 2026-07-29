"""MS-A — the canonical realised-R calculation. One implementation, shared.

WHY THIS MODULE EXISTS
    Realised R was computed in exactly one place, `backend/trade_reconstruction`,
    and was therefore unavailable to the autonomous runner — whose trades are the
    ones its own daily-loss rail is meant to measure. Milestone B needs the same
    number from confirmed broker closes.

    Copying the formula would create two definitions of R that could drift apart
    silently, and every historical trade-ledger record already carries values
    produced by the original. So the calculation moved HERE and the backend
    delegates. There is no second formula.

WHY IT LIVES IN `live/` RATHER THAN `backend/`
    `backend/` → `live/` is the established, sanctioned dependency direction
    (`backend/broker.py` already imports `live.mt5_gateway`); `live/` → `backend/`
    is not, and would invert the boundary. Placing a pure domain calculation in
    the package that is already a dependency keeps one implementation without a
    new top-level package and without any import inversion.

PURITY CONTRACT (structurally enforced by a guard test)
    No MetaTrader5, no FastAPI, no database, no fixture, no RunnerState, no
    gateway, no environment configuration, no filesystem. Only arithmetic over
    values the caller supplies. This module can be imported from anywhere,
    including a process with no broker and no state directory.

THE POLICY IT PRESERVES, VERBATIM
    * Planned risk uses the ORIGINAL intended stop. A protective stop that was
      later moved (to breakeven, say) is deliberately NOT consulted — inferring
      intended risk from a moved stop fabricates the number the safety rail
      depends on.
    * The entry basis prefers the INTENDED entry, falling back to the executed
      average only when no intended entry was recorded. Slippage therefore does
      not change planned risk.
    * R is computed on GROSS PnL. Costs are excluded because commission and swap
      evidence frequently arrives late or not at all, and an R that silently
      changed when a swap settled would not be comparable between trades. Every
      result states its basis so a consumer never has to guess.
    * Direction is NOT an input. Long versus short is already carried by the sign
      of gross PnL; the stop distance is absolute. Anything needing direction
      must obtain it elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass

# Completeness ladder. These strings are the canonical values; the backend's
# `trade_ledger_domain` mirrors them and a parity test asserts they are equal, so
# a rename on either side fails loudly rather than producing records whose
# completeness silently stops matching.
COMPLETE = "complete"
PARTIAL = "partial"
UNAVAILABLE = "unavailable"

#: The only PnL basis this module computes on. Stated on every result.
BASIS_GROSS = "gross"

#: Rounding, preserved exactly from the original implementation. Historical
#: ledger records were produced with these, so changing either would re-value
#: trades that are already stored.
_R_PLACES = 6
_RISK_PLACES = 8


@dataclass(frozen=True)
class RiskResult:
    """Initial-risk evidence and realised R, or an explicit statement of what
    was missing. Field names mirror the backend's `TradeRiskSummary` so the
    delegation is a direct mapping rather than a translation."""
    initial_stop_price: float | None = None
    initial_entry_price: float | None = None
    initial_risk_amount: float | None = None
    planned_r: float | None = None
    realized_r: float | None = None
    realized_r_basis: str = BASIS_GROSS
    completeness: str = UNAVAILABLE


def realised_risk(*, initial_stop: float | None, initial_entry: float | None,
                  average_entry: float | None, entry_quantity: float | None,
                  gross_pnl: float | None) -> RiskResult:
    """The canonical calculation.

        risk_amount = |basis_entry - initial_stop| x entry_quantity
        realized_r  = gross_pnl / risk_amount

    Total: never raises and never divides by zero. Each early return states how
    far the evidence got, so a caller can distinguish "no planned risk was ever
    recorded" (UNAVAILABLE) from "planned risk is known but the outcome is not"
    (PARTIAL). Those are different facts and a safety rail must not conflate
    them.
    """
    # No planned stop means no planned risk. It is never inferred.
    if initial_stop is None:
        return RiskResult(initial_entry_price=initial_entry,
                          completeness=UNAVAILABLE)

    basis_entry = initial_entry if initial_entry is not None else average_entry
    if basis_entry is None or entry_quantity is None:
        return RiskResult(initial_stop_price=initial_stop,
                          initial_entry_price=initial_entry,
                          completeness=PARTIAL)

    risk_amount = abs(basis_entry - initial_stop) * entry_quantity
    # `<= 0` also catches a negative quantity, so no division can ever occur on
    # a degenerate risk. Zero risk is reported explicitly rather than skipped.
    if risk_amount <= 0:
        return RiskResult(initial_stop_price=initial_stop,
                          initial_entry_price=basis_entry,
                          initial_risk_amount=0.0,
                          completeness=PARTIAL)

    realized_r = (round(gross_pnl / risk_amount, _R_PLACES)
                  if gross_pnl is not None else None)
    return RiskResult(
        initial_stop_price=initial_stop, initial_entry_price=basis_entry,
        initial_risk_amount=round(risk_amount, _RISK_PLACES), planned_r=1.0,
        realized_r=realized_r, realized_r_basis=BASIS_GROSS,
        completeness=COMPLETE if realized_r is not None else PARTIAL)
