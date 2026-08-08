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

SCHEMA_VERSION = "ct.node-decisions.v1"

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


def _action(row: dict) -> str:
    """What the strategy DID, as one word."""
    outcome = str(row.get("outcome", "") or "")
    if outcome in REFUSAL_OUTCOMES:
        return "REFUSED"
    if str(row.get("fill_time", "") or ""):
        return "FILLED"
    return "PENDING"


def _refusal_reason(row: dict) -> str | None:
    """Why the strategy refused, preferring the most specific recorded reason."""
    outcome = str(row.get("outcome", "") or "")
    if outcome in REFUSAL_OUTCOMES:
        for col in ("cancel_reason", "regime_block_reason", "portfolio_decision_reason"):
            v = _clip(row.get(col))
            if v:
                return v
        return REFUSAL_OUTCOMES[outcome]
    return None


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
        rec["eligible"] = _action(row) != "REFUSED"
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
