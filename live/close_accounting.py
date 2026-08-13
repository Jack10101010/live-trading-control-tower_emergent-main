"""B2 — resolve confirmed close deals to their intent and compute canonical R.

DRY RUN. This module computes and explains; it persists NOTHING. No RunnerState
write, no realised-R bucket, no accounted-deal id, no pruning, no SafetyRails
change, no executor hook. Milestone B3 owns activation.

WHAT IT PROVES
    That a confirmed broker close can be turned into the SAME realised R the
    Control Tower's trade reconstruction would produce, using only durable facts
    the runner already holds. Proving that before anything is posted is the whole
    point of the dry run: an accounting error discovered after the daily-loss
    rail depends on it is an error discovered with money at risk.

THE TWO SOURCES OF TRUTH, AND WHY THEY ARE SPLIT
    * The LEDGER owns PLANNED RISK — intended entry, original stop, filled
      volume. It is the only place the ORIGINAL stop survives (Milestone A), and
      a stop later moved to breakeven must never become the denominator of R.
    * BROKER HISTORY owns the REALISED OUTCOME — profit per exit deal, and the
      UTC instant it occurred.

    Neither is asked to supply the other's facts. In particular the history
    window is NEVER widened to reconstruct entry quantity: a position opened
    before the window would otherwise silently produce a different denominator
    depending on when the read happened, and R would stop being comparable
    between trades.

RESOLUTION IS THROUGH THE LEDGER, NEVER THE MIRROR
    `reconcile()` clears the mirror the moment a position vanishes from the
    broker snapshot — which is the SAME event that produces the close deal. A
    mirror-based join could therefore be destroyed before the deal is ever read.
    The ledger is independent of that, which is what makes accounting and mirror
    demotion safely decoupled.
"""

from __future__ import annotations

from dataclasses import dataclass

from live import deal_records as dr
from live import risk_math
from live.intents import OPEN_POSITION

# ── classification: every deal ends in exactly one of these ──────────────────
#: R was computed from confirmed evidence.
ACCOUNTABLE = "accountable"
#: No ledger record owns this position — a foreign or pre-existing position.
UNRESOLVED = "unresolved"
#: Resolved, but the planned-risk facts needed for R are absent or unusable.
UNACCOUNTABLE = "unaccountable"
#: Terminal for the position, but not attributable to one trade by price.
QUARANTINED = "quarantined"
#: Not a close — an entry deal, which moves exposure the other way.
IGNORED = "ignored"


@dataclass(frozen=True)
class AccountingRecord:
    """One deal's dry-run outcome. In memory only; nothing here is persisted.

    `realised_r` is populated ONLY for ACCOUNTABLE. Every other classification
    leaves it None rather than zero — a deal whose risk cannot be computed is a
    known unknown, and a zero would understate the day's realised loss while
    looking like a real measurement.
    """
    deal_id: str
    position_id: str
    intent_id: str | None
    trade_id: str | None
    classification: str
    realised_r: float | None
    utc_date: str | None
    explanation: str

    @property
    def accountable(self) -> bool:
        return self.classification == ACCOUNTABLE


def _ticket_keys() -> tuple:
    """The canonical ledger ticket keys, reused rather than re-declared.

    `Executor._TICKET_KEYS` is the one place this repository states which detail
    keys actually carry a broker ticket ("order" from the Slice-3 result,
    "adopted_ticket" from reconcile adoption). Importing it keeps a single
    definition; inventing a second list here is exactly how the two would drift.
    """
    from live.executor import Executor
    return Executor._TICKET_KEYS


def _tickets_in(detail: dict) -> set:
    return {str(detail[key]) for key in _ticket_keys()
            if detail.get(key) not in (None, "", 0)}


def resolve_opening_record(position_id: str, ledger: dict) -> tuple:
    """Find the OPENING ledger record for a broker position.

    Returns `(intent_id, detail, owned)`, where `owned` states whether ANY ledger
    record claims this position even if the opening intent could not be found.

    That distinction is the point. Two very different facts would otherwise look
    identical to an operator:
      * no record matches at all — a foreign or pre-existing position, nothing
        of ours;
      * a record matches but carries no opening intent — OUR trade, whose planned
        risk was lost (a pre-Milestone-A record, whose intent was overwritten at
        confirmation).
    The first needs no action; the second is a permanently unaccountable trade
    that should be counted and reported.

    Selecting the OPENING intent specifically also matters: a CLOSE intent records
    `{"ticket": <position>, "closed": True}`, and "ticket" is itself a canonical
    ticket key, so a naive match resolves BOTH records for the same position.
    Only the opening one carries planned entry and stop.

    Deterministic: ledger ids are sorted, so a state file containing several
    matching records always resolves the same way.
    """
    target = str(position_id)
    owned = False
    for intent_id in sorted(ledger):
        record = ledger.get(intent_id) or {}
        detail = record.get("detail") or {}
        if target not in _tickets_in(detail):
            continue
        owned = True
        intent = detail.get("intent")
        if isinstance(intent, dict) and intent.get("action") == OPEN_POSITION:
            return intent_id, detail, True
    return None, None, owned


def account_deal(deal, ledger: dict) -> AccountingRecord:
    """Classify ONE deal and, where possible, compute its canonical R delta.

    Pure: reads the supplied ledger mapping, writes nothing, and never raises for
    ordinary bad data — an unusable deal is classified, not an exception.
    """
    def record(classification, *, explanation, intent_id=None, trade_id=None,
               realised_r=None, utc_date=None):
        return AccountingRecord(
            deal_id=deal.deal_id, position_id=deal.position_id,
            intent_id=intent_id, trade_id=trade_id,
            classification=classification, realised_r=realised_r,
            utc_date=utc_date, explanation=explanation)

    # 1) Entry deals move exposure the other way; they are not closes.
    if deal.entry == dr.ENTRY_IN:
        return record(IGNORED, explanation="entry deal; opens or increases exposure")

    # 2) Reversal and closed-by-opposite are terminal for the position but cannot
    #    be attributed to one trade by price. The Control Tower's reconstruction
    #    blocks finalization on exactly these, and approximating here would
    #    invent a realised outcome.
    if deal.entry in (dr.ENTRY_INOUT, dr.ENTRY_OUT_BY):
        return record(QUARANTINED,
                      explanation=f"entry={deal.entry} cannot be attributed to a "
                                  "single trade by price")
    if not deal.is_exit:
        return record(QUARANTINED,
                      explanation=f"unhandled deal entry direction {deal.entry!r}")

    # 3) Ledger resolution — never the mirror.
    intent_id, detail, owned = resolve_opening_record(deal.position_id, ledger)
    if intent_id is None:
        if owned:
            # Ours, but the opening intent is gone: a pre-Milestone-A record whose
            # planned risk was destroyed at confirmation. Permanently
            # unaccountable — and must be reported, never counted as zero risk.
            return record(UNACCOUNTABLE,
                          explanation="ledger owns this position but no opening "
                                      "intent survives; planned risk unrecoverable")
        return record(UNRESOLVED,
                      explanation="no ledger record owns this position "
                                  "(foreign or pre-existing)")

    intent = detail.get("intent") or {}
    trade_id = intent.get("trade_id") or detail.get("trade_id")

    # 4) Planned risk comes ONLY from the ledger. `filled_volume` is the entry
    #    quantity of record; the history window is never widened to rediscover
    #    it, which keeps the denominator independent of when the read happened.
    planned_stop = intent.get("stop")
    planned_entry = intent.get("entry")
    filled_volume = detail.get("filled_volume")
    executed_entry = detail.get("price")

    if planned_stop is None:
        return record(UNACCOUNTABLE, intent_id=intent_id, trade_id=trade_id,
                      explanation="no original planned stop; risk was never "
                                  "recorded and must not be inferred")

    # 5) Canonical R — delegated, never re-implemented. `average_entry` supplies
    #    the same executed-fill fallback the Control Tower reconstruction uses
    #    when no intended entry was recorded.
    result = risk_math.realised_risk(
        initial_stop=planned_stop, initial_entry=planned_entry,
        average_entry=executed_entry, entry_quantity=filled_volume,
        gross_pnl=deal.profit)

    if result.completeness != risk_math.COMPLETE:
        return record(UNACCOUNTABLE, intent_id=intent_id, trade_id=trade_id,
                      explanation=f"planned risk incomplete "
                                  f"({result.completeness}); no R computed")

    # 6) One R delta per exit deal, against the FULL planned risk. Because R is
    #    linear in PnL with a fixed denominator, the deltas for a position sum
    #    exactly to the canonical whole-trade R — so a partial close needs no
    #    special case, and each delta lands in the UTC day it actually occurred.
    return record(ACCOUNTABLE, intent_id=intent_id, trade_id=trade_id,
                  realised_r=result.realized_r, utc_date=deal.utc_date_key(),
                  explanation=f"R={result.realized_r} from profit {deal.profit} "
                              f"over planned risk {result.initial_risk_amount}")


def account_deals(deals, ledger: dict) -> tuple:
    """Dry-run accounting over a batch. Deterministic and side-effect free."""
    return tuple(account_deal(deal, ledger) for deal in deals)


def summarise(records) -> dict:
    """Counts by classification plus the accountable R total, for diagnostics.

    The total is informational ONLY. It is not posted, and it deliberately does
    not aggregate by day — day bucketing is a persistence concern that belongs to
    B3, where it can be made crash-safe and idempotent.
    """
    counts: dict = {}
    for rec in records:
        counts[rec.classification] = counts.get(rec.classification, 0) + 1
    total = sum(r.realised_r for r in records
                if r.accountable and r.realised_r is not None)
    return {"counts": counts, "accountable_r_total": round(total, 8),
            "records": len(tuple(records))}
