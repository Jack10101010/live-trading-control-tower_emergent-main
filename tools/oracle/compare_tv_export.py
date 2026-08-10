"""Mechanically diff a TradingView chart-data export against the Python S1 trace.

    python -m tools.oracle.compare_tv_export \
        --fixture F-S1-DAY --timeframe 15min \
        --export <tradingview-export.csv> \
        --feed "OANDA:EURUSD" --out artifacts/tradingview_oracle/s1_tv_compare.json

WHY THIS EXISTS
---------------
The original S1 checklist asked an operator to hover ~24 individual bars and read
integers out of the Data Window by eye. That is slow, and — worse — it is exactly
the kind of manual transcription that produces a WRONG parity record. A single
misread session code recorded as PASS would create the false `MATCHED` this whole
apparatus exists to prevent, and every later stage would inherit it.

TradingView can export every plotted series to CSV ("Export chart data…"). The
oracle plots ten integers for precisely this reason, so the comparison can be a
mechanical join instead of a reading exercise: the operator clicks Export once,
runs this, and the machine does the diffing.

WHAT IT IS NOT
--------------
It does not prove the script compiles, and it cannot check the HUD, the
unsupported-context banners, or realtime bar confirmation — those remain visual
and stay on the checklist. This covers the per-bar numeric fields, which are the
bulk of the risk and all of the tedium.

CLASSIFICATION
--------------
A row present in one side and not the other is NOT a Pine defect: TradingView
builds its own bars from its own feed (limitation L-02). Those are reported
separately as `feed_only` / `python_only` and do not fail the run. A row present
in BOTH whose values disagree IS a defect and fails.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from tools.oracle import session_codes as codes_mod
from tools.oracle.engine_access import CT_ROOT
from tools.oracle.export_trace import TraceError, build_trace

FIXTURE_DIR = CT_ROOT / "golden" / "tradingview_oracle" / "s1"
DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "s1_tv_compare.json"

#: TradingView plot title -> how to derive the expected value from a trace bar.
#: Every one is an EXACT match; S1 defines no tolerance fields.
FIELD_MAP = {
    "oracleSessionCode":    lambda b: b["session"]["code"],
    "oracleDayKey":         lambda b: codes_mod.day_key_to_int(b["utc_day"]["day_key"]),
    "oracleTransCode":      lambda b: b["session"]["transition_code"],
    "oracleUtcHour":        lambda b: b["session"]["utc_hour"],
    "oracleCtxCode":        lambda b: 0 if b["data_context"]["status"] == "SUPPORTED" else 1,
    "oracleBarsSinceTrans": lambda b: b["session"]["bars_since_transition"],
    "oracleSessIsFallback": lambda b: 1 if b["session"]["is_fallback"] else 0,
}

#: Present in the export but not compared: identity/diagnostic only.
INFORMATIONAL = ("oracleBarEpochMs", "oracleBarClosed", "oracleBuildCode")


class CompareError(RuntimeError):
    pass


def _parse_tv_time(raw: str) -> datetime | None:
    """TradingView exports either an ISO-8601 string or a unix timestamp."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        v = int(raw)
        if v > 10_000_000_000:      # milliseconds
            v //= 1000
        return datetime.fromtimestamp(v, tz=timezone.utc)
    txt = raw.replace("Z", "+00:00")
    for fmt in (None, "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.fromisoformat(txt) if fmt is None else datetime.strptime(txt, fmt)
        except ValueError:
            continue
        # A naive stamp in a TradingView export is UTC only if the chart was set
        # to UTC. That is a checklist precondition; record the assumption.
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    return None


def _norm(name: str) -> str:
    return "".join(ch for ch in name.lower() if ch.isalnum())


def load_export(path: Path) -> tuple[list[dict], dict]:
    if not path.is_file():
        raise CompareError(f"TradingView export not found: {path}")
    with path.open(newline="", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        raise CompareError(f"{path.name} contains no data rows")

    headers = list(rows[0].keys())
    lookup = {_norm(h): h for h in headers}
    time_col = next((lookup[k] for k in ("time", "date", "datetime", "timestamp")
                     if k in lookup), None)
    if time_col is None:
        raise CompareError(
            f"no time column in {path.name} (headers: {headers[:12]})")

    resolved, missing = {}, []
    for want in FIELD_MAP:
        col = lookup.get(_norm(want))
        if col is None:
            missing.append(want)
        else:
            resolved[want] = col

    meta = {
        "file": path.name,
        "sha256": hashlib.sha256(
            path.read_bytes().replace(b"\r\n", b"\n")).hexdigest(),
        "headers": headers,
        "time_column": time_col,
        "resolved_columns": resolved,
        "missing_columns": missing,
        "rows": len(rows),
        "informational_present": [c for c in INFORMATIONAL if _norm(c) in lookup],
    }
    return rows, meta


def compare(fixture: str, timeframe: str, export_path: Path,
            feed: str = "", max_examples: int = 10) -> dict:
    csv_path = FIXTURE_DIR / f"{fixture}.csv"
    if not csv_path.is_file():
        raise CompareError(f"unknown fixture {fixture} ({csv_path} not found)")
    trace = build_trace(symbol="EURUSD", timeframe=timeframe,
                        input_path=csv_path, fixture_id=fixture)

    rows, meta = load_export(export_path)
    if meta["missing_columns"]:
        raise CompareError(
            "the export is missing oracle plot column(s): "
            f"{meta['missing_columns']}. Re-export with the indicator ON the chart; "
            "if a column is genuinely absent from TradingView's export, that is a "
            "finding to record, not something to work around.")

    py_by_ts = {b["bar_timestamp"][:16]: b for b in trace["bars"]}
    tv_by_ts: dict[str, dict] = {}
    unparsed = 0
    for r in rows:
        dt = _parse_tv_time(r[meta["time_column"]])
        if dt is None:
            unparsed += 1
            continue
        tv_by_ts[dt.strftime("%Y-%m-%d %H:%M")] = r

    both = sorted(set(py_by_ts) & set(tv_by_ts))
    py_only = sorted(set(py_by_ts) - set(tv_by_ts))
    tv_only = sorted(set(tv_by_ts) - set(py_by_ts))

    per_field = {}
    mismatches = []
    for field, getter in FIELD_MAP.items():
        col = meta["resolved_columns"][field]
        ok = bad = 0
        for ts in both:
            expected = getter(py_by_ts[ts])
            raw = (tv_by_ts[ts].get(col) or "").strip()
            try:
                actual = int(round(float(raw)))
            except (TypeError, ValueError):
                actual = None
            if actual == expected:
                ok += 1
            else:
                bad += 1
                if len(mismatches) < max_examples:
                    mismatches.append({
                        "bar_utc": ts, "field": field,
                        "expected_python": expected, "observed_pine": raw,
                        "classification": "PINE_LOGIC_OR_TIMESTAMP_INTERPRETATION",
                    })
        per_field[field] = {"compared": ok + bad, "pass": ok, "fail": bad,
                            "result": "PASS" if bad == 0 else "FAIL"}

    all_pass = both and all(v["fail"] == 0 for v in per_field.values())

    return {
        "stage": "S1",
        "fixture": fixture,
        "timeframe": timeframe,
        "feed": feed,
        "trace": {
            "oracle_engine_hash": trace["header"]["oracle_engine_hash"],
            "oracle_engine_id": trace["header"]["oracle_engine_id"],
            "trace_schema_version": trace["header"]["trace_schema_version"],
            "bars": len(trace["bars"]),
        },
        "export": meta,
        "coverage": {
            "bars_in_both": len(both),
            "python_only": len(py_only),
            "tradingview_only": len(tv_only),
            "python_only_examples": py_only[:5],
            "tradingview_only_examples": tv_only[:5],
            "unparsed_export_rows": unparsed,
            "_note": "python_only / tradingview_only are FEED_DIFFERENCE (limitation "
                     "L-02), not Pine defects. They do not fail the comparison, but a "
                     "large count means the chart window does not cover the fixture.",
        },
        "fields": per_field,
        "mismatches": mismatches,
        "result": "PASS" if all_pass else "FAIL",
        "result_reason": (
            "every compared bar agrees on every exact field"
            if all_pass else
            ("no overlapping bars — the chart window does not cover this fixture"
             if not both else
             f"{sum(v['fail'] for v in per_field.values())} field mismatches")),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--timeframe", default="15min")
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--feed", default="")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        rep = compare(args.fixture, args.timeframe, args.export, args.feed)
    except (CompareError, TraceError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"fixture   : {rep['fixture']} @ {rep['timeframe']}")
    print(f"feed      : {rep['feed'] or '(not recorded)'}")
    print(f"export    : {rep['export']['file']}  ({rep['export']['rows']} rows)")
    c = rep["coverage"]
    print(f"coverage  : {c['bars_in_both']} bars compared "
          f"(python-only {c['python_only']}, tv-only {c['tradingview_only']})")
    for f, v in rep["fields"].items():
        print(f"  [{v['result']:<4}] {f:<22} {v['pass']}/{v['compared']}")
    if rep["mismatches"]:
        print("\nfirst mismatches:")
        for m in rep["mismatches"]:
            print(f"  {m['bar_utc']}  {m['field']}: "
                  f"python={m['expected_python']} pine={m['observed_pine']}")
    print(f"\nRESULT: {rep['result']} — {rep['result_reason']}")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(rep, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"written: {args.out.relative_to(CT_ROOT)}")
    return 0 if rep["result"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
