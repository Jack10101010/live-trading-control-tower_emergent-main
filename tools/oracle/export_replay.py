"""Export production's OWN setup decisions so the chart can draw them.

    python -m tools.oracle.export_replay --write

WHY THIS EXISTS
---------------
The 15-minute indicator needs to show entry, stop, target, risk-reward, whether a
setup was allowed or blocked, and how it ended. Production already computes every
one of those. Re-deriving them in Pine would mean porting the fill gate, the
policy chain, break-even, protection and the exit precedence — and then arguing
about which side is right when they disagree.

So this runs the DEPLOYED simulation and records what it decided. The chart draws
that. It is exact by construction, because it IS production's output rather than
a second implementation of it.

WHAT IT IS NOT
--------------
It is a RECORDING, not a live computation. The chart replays decisions made on
production's data; it does not recompute them from the bars on screen. Two
consequences, both surfaced on the chart rather than buried here:

  * prices come from production's feed, which is bid where the chart is mid
    (limitation L-02), so a line can sit a fraction of a pip off the candle that
    caused it;
  * the set is bounded and generated at build time, so a setup after the last
    build does not appear until the next one.

Everything is keyed by UTC TIMESTAMP, never by bar index, so it survives a feed
that disagrees about which bars exist (L-09).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import (CT_ROOT, EngineAccessError, _in_lux,
                                        load_engine, resolve_config)

DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "replay_setups.json"

#: How many setups the RECORDING carries. The chart then draws the most recent N
#: of these, chosen by an input, so raising this costs build size but never
#: costs chart objects. Pine hard-limits boxes/lines/labels to 500 EACH and every
#: drawn setup spends up to 3 lines, 2 boxes and 2 labels.
DEFAULT_LIMIT = 200

#: Setup status, as the chart shows it. Derived from production's `outcome` and
#: cancel/missed reason — see `_status`.
STATUS = ("UNKNOWN", "PENDING", "ALLOWED", "BLOCKED", "FILLED", "CANCELLED",
          "COMPLETED")

#: Outcome, for setups that reached a conclusion.
OUTCOME = ("NONE", "WIN", "LOSS", "BREAKEVEN", "OPEN")

#: The main blocking / cancellation reason, in the vocabulary production emits.
#: Ordered so the code is stable; an unrecognised reason maps to 0 and renders as
#: the raw string in the label rather than being silently dropped.
REASON = (
    "",
    "never_triggered",
    "never_filled_after_trigger",
    "invalidated_before_edge_entry",
    "state_target_block",
    "cohort_disabled",
    "regime_blocked",
    "portfolio_disabled",
    "state_not_allowed",
    "direction_mismatch",
    "session_filter_cancel",
    "news_touch_cancel",
    "reverse_touch_cancel",
    "used_ob_retrace_cancel",
    "first_failed_tag_cancel",
    "exited_ob_before_arm",
)

_BLOCKED_OUTCOMES = {"STATE_BLOCKED", "COHORT_DISABLED", "REGIME_BLOCKED",
                     "SESSION_FILTERED"}
_DONE_OUTCOMES = {"WIN": "WIN", "LOSS": "LOSS", "BE": "BREAKEVEN",
                  "BREAKEVEN": "BREAKEVEN"}


class ReplayError(RuntimeError):
    pass


def _ms(value):
    """UTC epoch milliseconds, or 0 for absent. Pine matches on `time`, so this
    is the join key — never a bar index (L-09)."""
    import pandas as pd
    if value in (None, "", "nan") or (isinstance(value, float) and value != value):
        return 0
    try:
        ts = pd.Timestamp(value)
    except (ValueError, TypeError):
        return 0
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.value // 1_000_000)


def _text(value):
    """A string, with pandas' NaN rendered as absent rather than as the word
    "nan" — which is what a float NaN becomes under `str()` and what leaked into
    19 of 60 blocking reasons on the first pass."""
    if value is None or (isinstance(value, float) and value != value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ("nan", "none") else text


def _num(value, default=0.0):
    try:
        f = float(value)
    except (TypeError, ValueError):
        return default
    return default if f != f else f


def _status(row):
    """Map production's outcome onto what the chart shows.

    `outcome` alone is not enough: UNFILLED covers both "never armed" (the setup
    simply never happened) and "armed but never filled" (it happened and missed),
    and those read very differently on a chart.
    """
    outcome = str(row.get("outcome") or "")
    reason = str(row.get("cancel_reason") or row.get("missed_reason") or "")
    if outcome in _DONE_OUTCOMES:
        return "COMPLETED", _DONE_OUTCOMES[outcome]
    if outcome == "OPEN":
        return "FILLED", "OPEN"
    if outcome in _BLOCKED_OUTCOMES:
        return "BLOCKED", "NONE"
    if outcome == "INVALID":
        return "CANCELLED", "NONE"
    if outcome == "UNFILLED":
        # Armed and missed is a DIFFERENT story from never armed.
        if "never_filled" in reason or row.get("armed_at"):
            return "ALLOWED", "NONE"
        return "PENDING", "NONE"
    if outcome:
        return "CANCELLED", "NONE"
    return "UNKNOWN", "NONE"


#: Values that mean "no reason". `nan` is here because the trades frame is a
#: pandas DataFrame: a column that is blank for SOME rows becomes a float NaN for
#: those rows, and `str(nan)` is the string "nan" — which sailed through the
#: first version and rendered as a blocking reason on 19 of 60 setups.
_NO_REASON = {"", "nan", "none", "never_filled"}


def _reason(row):
    for key in ("cancel_reason", "regime_block_reason", "missed_reason"):
        value = row.get(key)
        if isinstance(value, float) and value != value:      # NaN
            continue
        text = str(value or "").strip()
        if text.lower() not in _NO_REASON:
            return text
    return ""


def _extension():
    """The snapshotted live segment, if an operator has taken one."""
    import pandas as pd

    from tools.oracle.snapshot_execution_feed import DEFAULT_OUT as EXT
    if not EXT.is_file():
        return None, None
    meta_path = EXT.with_suffix(".json")
    meta = (json.loads(meta_path.read_text(encoding="utf-8"))
            if meta_path.is_file() else {})
    return pd.read_csv(EXT), meta


def build(engine, cfg, start=None, end=None, limit=DEFAULT_LIMIT,
          use_seam=False):
    """Run the deployed simulation and reduce it to what the chart draws."""
    import pandas as pd

    from tools.oracle import replay_prefill as rp

    pre = rp.resolve_prefill_config(cfg)
    ext, ext_meta = _extension() if use_seam else (None, None)
    prepared, obs, news, seam = rp._load(engine, cfg, start, end,
                                         extension=ext)
    records = prepared.to_dict("records")

    with _in_lux(Path(engine.lux_root)):
        kwargs = engine.rb.simulation_kwargs(
            cfg, "allow_multi_position", news, pre["stop_buffer"])
        kwargs.update(entry_model="triggered_edge",
                      entry_threshold_pct=pre["trigger_threshold_pct"],
                      triggered_edge_delay_candles=pre["delay_candles"],
                      inputs_prepared=True)
        # The REAL policy emitters. Without them every setup reads as allowed and
        # the "blocked" half of the picture — the half hardest to see by eye —
        # would silently not exist.
        kwargs["regime_emit"] = engine.rb.regime_emit_for_run(cfg, prepared)
        kwargs["portfolio_emit"] = engine.rb.portfolio_emit_for_run(cfg, prepared)
        kwargs["state_policy_emit"] = engine.rb.state_policy_emit_for_run(
            cfg, prepared)
        trades = engine.core.simulate_trades(pd.DataFrame(records), obs, **kwargs)

    rows = trades.to_dict("records") if hasattr(trades, "to_dict") else list(trades)
    ob_bounds = {r.get("ob_id"): (float(r["top"]), float(r["bottom"]))
                 for r in (obs.to_dict("records") if hasattr(obs, "to_dict")
                           else obs)}

    setups = []
    for row in rows:
        detected = _ms(row.get("detection_time"))
        if not detected:
            continue
        status, outcome = _status(row)
        top, bottom = ob_bounds.get(row.get("ob_id"), (0.0, 0.0))
        setups.append({
            "detected_ms": detected,
            "ob_id": int(_num(row.get("ob_id"))),
            "direction": 1 if row.get("direction") == "bullish" else 2,
            "structure": 2 if "choch" in str(row.get("structure_tag", "")).lower()
                         else 1,
            "ob_top": round(top, 8),
            "ob_bottom": round(bottom, 8),
            "entry": round(_num(row.get("entry")), 8),
            "stop": round(_num(row.get("stop")), 8),
            "target": round(_num(row.get("tp")), 8),
            "status": status,
            "outcome": outcome,
            "reason": _reason(row),
            "armed_ms": _ms(row.get("armed_at") or row.get("trigger_time")),
            "fill_ms": _ms(row.get("fill_time")),
            "exit_ms": _ms(row.get("exit_time")),
            "net_r": round(_num(row.get("net_r")), 4),
            "gross_r": round(_num(row.get("gross_r")), 4),
            "rr": round(_num(row.get("rr_multiple")), 3),
            "session": _text(row.get("fill_session")),
            # The market state that applied WHEN THIS SETUP HAPPENED — production
            # stamps it on the row from the leakage-safe daily panel. The chart's
            # own S6 band shows the state NOW; a historical setup must carry the
            # state it was actually judged under, not today's.
            "market_state": _text(row.get("market_state")),
            "trend_state": _text(row.get("trend_state")),
            "volatility_state": _text(row.get("volatility_state")),
            "cohort": _text(row.get("portfolio_cohort_key")),
        })

    setups.sort(key=lambda s: (s["detected_ms"], s["ob_id"]))
    dropped = max(0, len(setups) - limit)
    if dropped:
        setups = setups[-limit:]          # the most RECENT, which is what a
                                          # reader scrolls to first
    return {
        "schema": "tradingview-oracle-replay-v1",
        # THE SEAM IS DECLARED, NEVER BLENDED AWAY. Rows after `frozen_end`
        # come from the live MT5 segment, not the frozen historical source, and
        # anything drawn from them inherits that provenance.
        "seam": (None if seam is None else
                 {**seam, "live_source": (ext_meta or {}).get("source_name"),
                  "live_sha256": (ext_meta or {}).get("source_sha256"),
                  "_note": "rows after frozen_end are the LIVE MT5 1-minute "
                           "segment, not the frozen historical file"}),
        "window": {"start": start, "end": end,
                   "first_bar": str(prepared["time"].iloc[0]),
                   "last_bar": str(prepared["time"].iloc[-1])},
        "engine_hash": None,              # stamped by the caller
        "limit": limit,
        "dropped_oldest": dropped,
        "execution_candles": len(records),
        "order_blocks": len(ob_bounds),
        "setups": setups,
        "_note": "Production's own decisions, recorded. Prices are production's "
                 "feed (bid) and may sit a fraction of a pip from a mid chart "
                 "(L-02). Keyed by UTC timestamp, never by bar index (L-09).",
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--start", default="2025-09-30")
    ap.add_argument("--end", default=None)
    ap.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    ap.add_argument("--seam", action="store_true",
                    help="extend past the frozen candle file with the "
                         "snapshotted live segment (snapshot_execution_feed)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        engine = load_engine(require_pin=True)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2
    cfg, _ = resolve_config(engine)

    from tools.oracle import fingerprint as fp
    from tools.oracle.engine_access import effective_policy_table
    table, _ = effective_policy_table(engine, cfg)
    out = build(engine, cfg, args.start, args.end, args.limit, args.seam)
    out["engine_hash"] = fp.from_engine(engine, cfg, table)["oracle_engine_hash"]

    from collections import Counter
    counts = Counter(s["status"] for s in out["setups"])
    print(f"window        : {out['window']['first_bar']} .. "
          f"{out['window']['last_bar']}")
    print(f"candles       : {out['execution_candles']:,}   "
          f"order blocks: {out['order_blocks']:,}")
    print(f"setups kept   : {len(out['setups'])} "
          f"(dropped {out['dropped_oldest']} older)")
    for status in STATUS:
        if counts.get(status):
            print(f"  {status:<10} {counts[status]}")
    reasons = Counter(s["reason"] for s in out["setups"] if s["reason"])
    for reason, n in reasons.most_common():
        mark = "" if reason in REASON else "   <- UNMAPPED, rendered as text"
        print(f"    {reason:<32} {n}{mark}")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(out, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"\nwritten: {args.out.relative_to(CT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
