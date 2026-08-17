"""M-STRATEGY-AUTHORITY-2 — bounded per-decision records for the Control Tower.

The Mac needs to VERIFY a strategy decision, not take it on trust: for a given
order block, which cohort and market state did the engine resolve, did the
matrix say TRADE or DON'T TRADE, at what target RR, and — if refused — why.

The engine frame carries 211 columns. Publishing it would be both a payload
problem and a contract problem, so this module projects the small, stable
subset a human actually needs to audit a decision, in a deterministic order,
under its own schema version.

DELIBERATE OMISSIONS, matching the canonical telemetry contract:
  * no broker/authority fields (no admitted, no execution_authority);
  * no raw account login, no credentials;
  * no free-form frame text beyond bounded, known reason codes.

This is read-only reporting derived from a frame the engine already produced.
It grants nothing and decides nothing.
"""

from __future__ import annotations

from datetime import timezone

#: v2 — `action` and `eligible` CHANGED MEANING, so the version moved with
#: them. Under v1 every row that was not COHORT_DISABLED / STATE_BLOCKED and had
#: no fill_time was published as `action="PENDING", eligible=true`. That
#: included invalidated, regime-blocked and news-cancelled setups: on the live
#: node, 19 of 50 published decisions were terminal order blocks presented as
#: live and eligible. A receiver could not tell a resting candidate from a dead
#: one, so the value was not merely imprecise — it was wrong in the direction
#: that matters, implying opportunity where there was none.
SCHEMA_VERSION = "ct.node-decisions.v2"

#: Hard cap. A cycle can produce thousands of candidate rows across 11 years of
#: history; the Control Tower only ever renders recent decisions, and an
#: unbounded list would turn a telemetry POST into a data transfer.
MAX_RECORDS = 50

#: (frame column -> published field). Everything published is on this list, so
#: a new frame column can never leak into telemetry by accident.
FIELD_MAP = (
    ("ob_id", "ob_id"),
    ("trade_id", "trade_id"),
    ("base_trade_id", "base_trade_id"),
    ("detection_time", "detection_time"),
    ("direction", "direction"),
    ("structure_tag", "structure_tag"),
    ("fill_session", "session"),
    ("portfolio_cohort_key", "cohort_key"),
    ("market_state", "market_state"),
    ("trend_state", "trend_state"),
    ("volatility_state", "volatility_state"),
    ("chop_state", "chop_state"),
    ("state_confirmed", "state_confirmed"),
    ("state_eligibility", "state_eligibility"),
    ("regime_block_reason", "regime_block_reason"),
    ("portfolio_decision_reason", "portfolio_decision_reason"),
    ("portfolio_confidence", "portfolio_confidence"),
    ("rr_multiple", "target_rr"),
    ("entry", "entry"),
    ("stop", "stop"),
    ("tp", "tp"),
    ("entry_model_key", "entry_model"),
    ("entry_depth_pct", "entry_depth_pct"),
    ("news_blackout", "news_blackout"),
    ("news_blackout_event_time", "news_blackout_event_time"),
    ("outcome", "outcome"),
    ("cancel_reason", "cancel_reason"),
    ("fill_time", "fill_time"),
    ("net_r", "net_r"),
)

#: Outcomes the matrix produces when it REFUSES a candidate.
REFUSAL_OUTCOMES = {"COHORT_DISABLED": "cohort_disabled",
                    "STATE_BLOCKED": "state_blocked"}

# ── LIFECYCLE, FROM THE ENGINE'S OWN TERMINAL LABELS ─────────────────────────
#
# `simulate_trades` walks candles with a `pending` list. Every exit from that
# list stamps a specific reason MID-WALK — invalidated_before_edge_entry,
# state_target_block, regime_blocked, news_touch_cancel. What remains in
# `pending` when the walk ENDS is flushed by the loop at
# `strategy_core/execution.py:3293-3302`:
#
#     for item in pending:
#         reason = ("never_filled_after_trigger" if item["triggered_edge_armed"]
#                   else "never_triggered")
#         row["outcome"] = "UNFILLED"
#
# and immediately above it, open positions are flushed as `outcome = "OPEN"`.
#
# THAT IS THE STRUCTURAL DISTINCTION, and it is why this is not a heuristic. A
# row labelled `never_triggered` is not "a setup that historically failed to
# trigger" — it is a setup the engine was STILL CARRYING when it reached the
# last candle. On the live node the last candle IS the frontier, because every
# cycle replays to the current bar. So:
#
#     never_triggered            -> still resting at the frontier
#     never_filled_after_trigger -> armed at the frontier, delay/fill pending
#
# Nothing here infers liveness from detection recency, age or distance from
# price. Those were considered and rejected: they would answer a different
# question (is this setup interesting) than the one asked (is the engine still
# carrying it).
#
# Verified against the live frame: `cancel_reason` and `outcome` agree 1:1 over
# all 2089 rows, with no row carrying a reason its outcome contradicts.
RESTING = "RESTING"
ARMED = "ARMED"
POSITION_OPEN = "POSITION_OPEN"
CLOSED = "CLOSED"
BLOCKED = "BLOCKED"
INVALIDATED = "INVALIDATED"
CANCELLED = "CANCELLED"
UNKNOWN = "UNKNOWN"

#: Lifecycles whose story has ended. A terminal setup can never be eligible and
#: must never be published as pending.
TERMINAL = frozenset({CLOSED, BLOCKED, INVALIDATED, CANCELLED})

#: outcome -> lifecycle, for the outcomes that decide it on their own.
_OUTCOME_LIFECYCLE = {
    "OPEN": POSITION_OPEN,
    "WIN": CLOSED,
    "LOSS": CLOSED,
    "BE": CLOSED,
    "INVALID": INVALIDATED,
    "STATE_BLOCKED": BLOCKED,
    "COHORT_DISABLED": BLOCKED,
    "REGIME_BLOCKED": BLOCKED,
    "NEWS_TOUCH_CANCEL": CANCELLED,
}

#: The frontier flush. UNFILLED alone does not say which; the cancel_reason does.
_FRONTIER_REASON_LIFECYCLE = {
    "never_triggered": RESTING,
    "never_filled_after_trigger": ARMED,
}


def lifecycle(row: dict) -> str:
    """Where this setup actually stands, from combined source evidence.

    Deliberately NOT derived from any single column. `outcome` decides most
    cases; `UNFILLED` needs `cancel_reason` to separate resting from armed; and
    an unrecognised combination returns UNKNOWN rather than defaulting to
    something reassuring. A new engine outcome should surface as UNKNOWN and be
    noticed, not be silently absorbed into RESTING.
    """
    outcome = str(row.get("outcome", "") or "").strip().upper()
    reason = str(row.get("cancel_reason", "") or "").strip().lower()

    if outcome == "UNFILLED":
        return _FRONTIER_REASON_LIFECYCLE.get(reason, UNKNOWN)
    mapped = _OUTCOME_LIFECYCLE.get(outcome)
    if mapped is not None:
        # A WIN/LOSS is only CLOSED once the exit exists; without one the engine
        # is still carrying the position.
        if mapped is CLOSED and not str(row.get("exit_time", "") or ""):
            return POSITION_OPEN
        return mapped
    if not outcome and str(row.get("fill_time", "") or ""):
        return POSITION_OPEN
    return UNKNOWN


def is_terminal(row: dict) -> bool:
    return lifecycle(row) in TERMINAL

_MAX_STR = 64


def _clip(v):
    if v is None:
        return None
    s = str(v)
    return s[:_MAX_STR] if s != "" else None


def _structure(tag) -> str | None:
    t = str(tag or "").lower()
    if "choch" in t:
        return "CHoCH"
    return "BOS" if "bos" in t else None


#: lifecycle -> the one-word `action` a reader sees. The vocabulary keeps
#: v1's three words where they were right and adds the distinctions v1 could
#: not express: a setup that DIED is not a setup that is WAITING, and a policy
#: BLOCK is not the same event as price invalidating the block.
_ACTION = {
    RESTING: "PENDING",
    ARMED: "ARMED",
    POSITION_OPEN: "FILLED",
    CLOSED: "CLOSED",
    BLOCKED: "REFUSED",
    INVALIDATED: "INVALIDATED",
    CANCELLED: "CANCELLED",
    UNKNOWN: "UNKNOWN",
}


def _action(row: dict) -> str:
    """What the strategy DID, as one word.

    v1 returned PENDING for everything that was neither a matrix refusal nor
    filled, which swept invalidated, regime-blocked and news-cancelled setups
    into the same bucket as genuinely resting ones.
    """
    return _ACTION.get(lifecycle(row), "UNKNOWN")


def _refusal_reason(row: dict) -> str | None:
    """Why this setup ended, preferring the most specific recorded reason.

    v1 answered only for the two matrix refusals, so an invalidated or
    news-cancelled setup published `refusal_reason: null` — no reason at all
    for the thing that ended it. Every terminal lifecycle now carries one.
    """
    lc = lifecycle(row)
    if lc not in TERMINAL or lc is CLOSED:
        return None
    for col in ("cancel_reason", "regime_block_reason", "portfolio_decision_reason"):
        v = _clip(row.get(col))
        if v:
            return v
    return REFUSAL_OUTCOMES.get(str(row.get("outcome", "") or ""))


#: `eligible` answers exactly one question: CAN THIS SETUP STILL OPEN A
#: POSITION? A setup already holding one cannot open a second, so
#: POSITION_OPEN sits here beside the terminal states. The invariant is then
#: crisp and testable: `eligible is None` ⟺ the setup is still a live
#: candidate — which is precisely the set a live-setups view wants.
_CANNOT_OPEN = TERMINAL | {POSITION_OPEN}


def _eligible(row: dict):
    """TRI-STATE, and never optimistic.

    v1 computed `action != "REFUSED"`, so any non-refused row read `true` —
    including dead ones. Worse, `true` there meant only "the matrix did not
    refuse it", while a reader naturally hears "this can open".

    false  — it cannot open: policy refused it, price killed it, it already
             closed, or it is the position currently open.
    None   — the matrix admitted it and it is still resting or armed, but the
             candidate-specific rails (arm, divergence, staleness, duplicate)
             are evaluated at OPEN time by the executor, not here. The node
             does not know yet, and `true` would be a claim it cannot support.

    `true` is therefore never published. Nothing this projection can see
    entitles it to promise an entry.
    """
    return False if lifecycle(row) in _CANNOT_OPEN else None


def _session_debug(detection_time):
    """UTC / session-local / tz triple, so the UI can show
    'UTC 09:20 · London 10:20 BST · London Lull' and the operator can CHECK it."""
    try:
        import pandas as pd
        from strategy_core.sessions import session_debug
    except Exception:
        return {}
    try:
        ts = pd.Timestamp(detection_time)
        if pd.isna(ts):
            return {}
        return session_debug(ts)
    except Exception:
        return {}


def build_decision_records(frame, *, limit: int = MAX_RECORDS,
                           intents: list | None = None) -> dict:
    """Bounded, deterministically-ordered decision projection of a trade frame.

    `intents` (OrderIntent list) is used only to attach `intent_id` to the
    trade it belongs to — the node's own linkage, not a broker fact.
    """
    if frame is None or not hasattr(frame, "to_dict"):
        return {"schema_version": SCHEMA_VERSION, "count": 0, "truncated": False,
                "decisions": []}
    intent_by_trade = {}
    for i in (intents or []):
        tid = str(getattr(i, "trade_id", "") or "")
        if tid:
            intent_by_trade.setdefault(tid, getattr(i, "intent_id", None))

    rows = frame.to_dict("records")
    # Most recent decisions first, by detection_time; the frame's own row order
    # breaks ties so the output is deterministic for a given frame.
    ordered = sorted(
        enumerate(rows),
        key=lambda pair: (str(pair[1].get("detection_time", "") or ""), pair[0]),
        reverse=True)
    total = len(ordered)
    out = []
    for _idx, row in ordered[:max(0, int(limit))]:
        rec = {}
        for col, field in FIELD_MAP:
            rec[field] = _clip(row.get(col))
        rec["structure"] = _structure(row.get("structure_tag"))
        rec["lifecycle"] = lifecycle(row)
        rec["terminal"] = is_terminal(row)
        rec["eligible"] = _eligible(row)
        rec["action"] = _action(row)
        rec["refusal_reason"] = _refusal_reason(row)
        tid = str(row.get("trade_id", "") or "")
        rec["intent_id"] = intent_by_trade.get(tid)
        rec.update({k: v for k, v in _session_debug(row.get("detection_time")).items()
                    if k in ("session_local", "session_tz", "session_tz_abbrev",
                             "session_key", "utc")})
        out.append(rec)
    return {"schema_version": SCHEMA_VERSION, "count": len(out),
            "total_candidates": total, "truncated": total > len(out),
            "decisions": out}
