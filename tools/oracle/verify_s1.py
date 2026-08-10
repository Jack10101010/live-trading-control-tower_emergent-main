"""S1 parity verification — Python side, plus the TradingView operator checklist.

    python -m tools.oracle.verify_s1                  # run all S1 fixtures
    python -m tools.oracle.verify_s1 --checklist      # emit the TV checklist
    python -m tools.oracle.verify_s1 --record PASS    # stamp the manifest

WHAT THIS CAN AND CANNOT DO
---------------------------
TradingView has no headless/API execution path, and this VPS cannot drive the
web app. So this tool CANNOT compare Pine output to Python by itself, and it
never claims to.

What it DOES do is everything that can be done without TradingView:

  1. runs the S1 trace exporter over every golden fixture and asserts the
     production-side invariants those fixtures exist to pin (boundary
     inclusivity, UTC day rollover, London-clock DST movement, gap/duplicate/out-of-order
     handling, unsupported-context refusal);
  2. statically validates the generated Pine;
  3. cross-checks that the constants embedded in the Pine artefact are the ones
     the trace was produced with — the single most likely silent mismatch;
  4. emits a per-bar CHECKLIST with the exact Data Window values an operator must
     read on TradingView, so the human step is mechanical rather than a judgement
     call.

Until a completed checklist is fed back with `--record PASS`, S1 stays
UNVERIFIED. This tool will not promote it, and neither will anything else.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from tools.oracle import fingerprint as fp
from tools.oracle import generate_pine as gp
from tools.oracle import parity_status as ps
from tools.oracle import session_codes as codes_mod
from tools.oracle.engine_access import CT_ROOT
from tools.oracle.export_trace import TraceError, build_trace
from tools.oracle.generate_pine import MANIFEST_PATH, OUT_PINE
from tools.oracle.lint_pine import lint

FIXTURE_DIR = CT_ROOT / "golden" / "tradingview_oracle" / "s1"
REPORT_PATH = CT_ROOT / "artifacts" / "tradingview_oracle" / "s1_verification.json"
CHECKLIST_PATH = CT_ROOT / "artifacts" / "tradingview_oracle" / "s1_tv_checklist.md"

#: fixture -> (timeframe, [invariant names])
FIXTURE_PLAN = {
    "F-S1-DAY": ("15min", ["full_day_bars", "six_transitions", "one_day_key"]),
    "F-S1-BOUNDARIES": ("1min", ["boundary_inclusivity"]),
    "F-S1-MIDNIGHT": ("1min", ["day_rollover"]),
    "F-S1-WEEKEND": ("15min", ["weekend_gap_is_session_invisible"]),
    "F-S1-DST-US": ("1min", ["sessions_follow_the_london_clock"]),
    "F-S1-DST-EU": ("1min", ["sessions_follow_the_london_clock"]),
    "F-S1-DST-END-US": ("1min", ["sessions_follow_the_london_clock", "no_repeated_utc_hour"]),
    "F-S1-DST-END-EU": ("1min", ["sessions_follow_the_london_clock", "no_repeated_utc_hour"]),
    "F-S1-GAP": ("15min", ["gap_drops_bars"]),
    "F-S1-DUPLICATE": ("1min", ["duplicates_preserved"]),
    "F-S1-OUTOFORDER": ("1min", ["reordered_ascending"]),
}

EXPECTED_BOUNDARIES = [
    ("00:00", "asia"), ("06:59", "asia"), ("07:00", "london"), ("07:01", "london"),
    ("09:59", "london"), ("10:00", "lull"), ("10:01", "lull"), ("11:59", "lull"),
    ("12:00", "newYork"), ("12:01", "newYork"), ("14:59", "newYork"),
    ("15:00", "ny_pm"), ("15:01", "ny_pm"), ("16:59", "ny_pm"),
    ("17:00", "outside"), ("17:01", "outside"), ("23:59", "outside"),
]


def _check(name: str, ok: bool, detail: str = "") -> dict:
    return {"invariant": name, "result": "PASS" if ok else "FAIL", "detail": detail}


def _run_invariants(fid: str, trace: dict) -> list[dict]:
    bars = trace["bars"]
    out = []
    plan = FIXTURE_PLAN[fid][1]

    if "full_day_bars" in plan:
        out.append(_check("full_day_bars", len(bars) == 96,
                          f"{len(bars)} bars (expect 96 = 1440/15)"))
    if "six_transitions" in plan:
        n = sum(1 for b in bars if b["session"]["transition"])
        out.append(_check("six_transitions", n == 6,
                          f"{n} transitions (first_bar + 5 session changes)"))
    if "one_day_key" in plan:
        keys = {b["utc_day"]["day_key"] for b in bars}
        out.append(_check("one_day_key", len(keys) == 1, f"day keys: {sorted(keys)}"))

    if "boundary_inclusivity" in plan:
        # Keyed on the LONDON wall clock, which is what the schedule's hours
        # are. A fixed UTC HH:MM was only ever right in GMT.
        got = {_london_hhmm_of(b["bar_timestamp"]): b["session"]["key"]
               for b in bars}
        bad = [(hhmm, exp, got.get(hhmm)) for hhmm, exp in EXPECTED_BOUNDARIES
               if hhmm in got and got.get(hhmm) != exp]
        out.append(_check("boundary_inclusivity", not bad,
                          "all [start, end) boundaries correct" if not bad
                          else f"mismatches: {bad}"))

    if "day_rollover" in plan:
        rolls = [b for b in bars if b["utc_day"]["day_transition"]]
        midnight = [b for b in rolls if b["bar_timestamp"][11:16] == "00:00"]
        out.append(_check("day_rollover", len(midnight) >= 1,
                          f"{len(rolls)} rollovers, {len(midnight)} at 00:00 UTC"))

    if "weekend_gap_is_session_invisible" in plan:
        # MEASURED, not assumed: the FX week closes ~Fri 21:45 UTC and reopens
        # ~Sun 22:00 UTC. Both instants fall inside the SAME `outside` window
        # (17:00-24:00), so the session key does not change across the ~48h gap
        # and NO transition is emitted at all. `gap_resync` therefore cannot fire
        # on an ordinary weekend — it needs a gap that also crosses a session
        # boundary. A Pine implementation that emitted a "session close" at the
        # weekend would diverge here, which is exactly what this pins.
        import datetime as _dt
        stamps = [_dt.datetime.strptime(b["bar_timestamp"][:19], "%Y-%m-%d %H:%M:%S")
                  for b in bars]
        gaps = [(a, b) for a, b in zip(stamps, stamps[1:])
                if (b - a).total_seconds() > 3600]
        idx_after_gap = {stamps.index(b) for _, b in gaps}
        crossed = [i for i in idx_after_gap if bars[i]["session"]["transition"]]
        out.append(_check(
            "weekend_gap_is_session_invisible",
            len(gaps) >= 1 and not crossed,
            f"{len(gaps)} multi-hour gap(s); none produced a session transition "
            "(both sides sit inside `outside`)"
            if not crossed else f"unexpected transition after gap at bars {crossed}"))

    if "sessions_follow_the_london_clock" in plan:
        # INVERTED BY M-SESSION-DST-1. This checked that DST moved nothing,
        # because the pinned engine classified on the UTC hour. Production now
        # converts through Europe/London, so the boundaries are fixed in LOCAL
        # terms and their UTC projection MUST move — asserting otherwise is
        # asserting the defect.
        bad = [b["bar_timestamp"] for b in bars
               if b["session"]["key"] != _expected_key_for_hour(
                   _london_hour_of(b["bar_timestamp"]))]
        out.append(_check("sessions_follow_the_london_clock", not bad,
                          "every bar maps by its LONDON wall-clock hour"
                          if not bad else f"{len(bad)} bars shifted, e.g. {bad[:3]}"))

    if "no_repeated_utc_hour" in plan:
        stamps = [b["bar_timestamp"] for b in bars]
        out.append(_check("no_repeated_utc_hour", len(stamps) == len(set(stamps)),
                          f"{len(stamps)} bars, {len(set(stamps))} distinct UTC stamps"))

    if "gap_drops_bars" in plan:
        out.append(_check("gap_drops_bars", len(bars) < 96,
                          f"{len(bars)} bars < 96 — empty intervals dropped, so "
                          "bar_index is not a time grid (L-09)"))

    if "duplicates_preserved" in plan:
        integ = trace["header"]["source"]["integrity"]
        stamps = [b["bar_timestamp"] for b in bars]
        out.append(_check("duplicates_preserved",
                          integ["duplicate_timestamps"] > 0 and
                          len(stamps) != len(set(stamps)),
                          f"{integ['duplicate_timestamps']} duplicate input rows "
                          "survive into the trace — production does NOT de-duplicate"))

    if "reordered_ascending" in plan:
        stamps = [b["bar_timestamp"] for b in bars]
        integ = trace["header"]["source"]["integrity"]
        out.append(_check("reordered_ascending",
                          stamps == sorted(stamps) and integ["out_of_order_rows"] > 0,
                          f"{integ['out_of_order_rows']} out-of-order input rows "
                          "silently re-sorted by production"))
    return out


def _london_hhmm_of(stamp: str) -> str:
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.fromisoformat(stamp).astimezone(
        ZoneInfo("Europe/London")).strftime("%H:%M")


def _london_hour_of(stamp: str) -> int:
    """The Europe/London wall-clock hour of a UTC bar stamp — the quantity
    `strategy_core.sessions._london_hour` classifies on."""
    import datetime as dt
    from zoneinfo import ZoneInfo
    return dt.datetime.fromisoformat(stamp).astimezone(
        ZoneInfo("Europe/London")).hour


def _expected_key_for_hour(h: int) -> str:
    for start, end, key in ((0, 7, "asia"), (7, 10, "london"), (10, 12, "lull"),
                            (12, 15, "newYork"), (15, 17, "ny_pm")):
        if start <= h < end:
            return key
    return "outside"


def _pine_constants() -> dict:
    """Extract the generated constants back OUT of the Pine artefact.

    This is the cross-check that matters: the trace and the chart must have been
    produced from the SAME contract. Reading them back from the emitted text
    (rather than trusting the generator) is what makes it a check.
    """
    if not OUT_PINE.is_file():
        return {}
    src = OUT_PINE.read_text(encoding="utf-8")
    out = {}
    for key in ("ORACLE_ENGINE_HASH", "ORACLE_SYMBOL", "ORACLE_TRACE_SCHEMA",
                "ORACLE_CONTRACT_SCHEMA", "ORACLE_S1_STATUS", "ORACLE_S1_LOGIC",
                    "ORACLE_GLOBAL_STATUS"):
        m = re.search(rf'^{key}\s*=\s*"([^"]*)"', src, re.M)
        if m:
            out[key] = m.group(1)
    m = re.search(r"^var array<int> SESSION_CODE = array\.from\(([^)]*)\)", src, re.M)
    if m:
        out["SESSION_CODE"] = [int(x) for x in m.group(1).split(",")]
    m = re.search(r"^var array<string> SESSION_KEY = array\.from\(([^)]*)\)", src, re.M)
    if m:
        out["SESSION_KEY"] = [x.strip().strip('"') for x in m.group(1).split(",")]
    return out


def run(argv_fixtures=None) -> dict:
    results, traces = [], {}
    for fid, (tf, _) in FIXTURE_PLAN.items():
        if argv_fixtures and fid not in argv_fixtures:
            continue
        path = FIXTURE_DIR / f"{fid}.csv"
        if not path.is_file():
            results.append({"fixture_id": fid, "status": "MISSING",
                            "checks": [_check("fixture_present", False, str(path))]})
            continue
        try:
            trace = build_trace(symbol="EURUSD", timeframe=tf, input_path=path,
                                fixture_id=fid)
        except TraceError as exc:
            results.append({"fixture_id": fid, "status": "ERROR",
                            "checks": [_check("trace_export", False, str(exc))]})
            continue
        traces[fid] = trace
        checks = _run_invariants(fid, trace)
        results.append({
            "fixture_id": fid, "timeframe": tf, "bars": len(trace["bars"]),
            "status": "PASS" if all(c["result"] == "PASS" for c in checks) else "FAIL",
            "checks": checks,
        })

    # Unsupported contexts must be REFUSED.
    ctx_checks = []
    for sym, tf, label in (("GBPUSD", "15min", "unsupported symbol"),
                           ("EURUSD", "H4", "H4 straddles a session boundary"),
                           ("EURUSD", "1440min", "daily is not a production timeframe")):
        try:
            build_trace(symbol=sym, timeframe=tf,
                        input_path=FIXTURE_DIR / "F-S1-DAY.csv")
            ctx_checks.append(_check(f"refuse[{sym}/{tf}]", False,
                                     "accepted an unsupported context"))
        except TraceError:
            ctx_checks.append(_check(f"refuse[{sym}/{tf}]", True, label))

    pine = _pine_constants()
    lint_findings = lint(OUT_PINE.read_text(encoding="utf-8")) if OUT_PINE.is_file() else []
    lint_errors = [f for f in lint_findings if f["severity"] == "error"]

    cross = []
    if pine and traces:
        any_trace = next(iter(traces.values()))
        cross.append(_check(
            "pine_matches_trace_fingerprint",
            pine.get("ORACLE_ENGINE_HASH") == any_trace["header"]["oracle_engine_hash"],
            f"pine={pine.get('ORACLE_ENGINE_HASH', '')[:16]}… "
            f"trace={any_trace['header']['oracle_engine_hash'][:16]}…"))
        cross.append(_check("pine_trace_schema_matches",
                            pine.get("ORACLE_TRACE_SCHEMA") == fp.TRACE_SCHEMA_VERSION,
                            f"pine={pine.get('ORACLE_TRACE_SCHEMA')} "
                            f"tool={fp.TRACE_SCHEMA_VERSION}"))
        expected_codes = codes_mod.session_codes(
            json.loads((CT_ROOT / "contracts" /
                        "tradingview_oracle_contract.json").read_text(encoding="utf-8")))
        pine_map = dict(zip(pine.get("SESSION_KEY", []), pine.get("SESSION_CODE", [])))
        mismatched = {k: (v, expected_codes.get(k)) for k, v in pine_map.items()
                      if expected_codes.get(k) != v}
        cross.append(_check("pine_session_codes_match_contract", not mismatched,
                            "codes agree" if not mismatched else str(mismatched)))
        # The chart must never claim more than the manifest records. Under the
        # split model that is a per-DIMENSION question: the implementation status
        # says only that the code exists, so the claim to police is the LOGIC
        # one, and it may not exceed what is recorded.
        recorded = ((gp.recorded_stages().get("S1") or {})
                    .get("logic_parity", {}).get("status", ps.LOGIC_UNVERIFIED))
        cross.append(_check(
            "pine_status_not_overclaimed",
            pine.get("ORACLE_GLOBAL_STATUS") in ("PARTIAL", "UNVERIFIED")
            and pine.get("ORACLE_S1_LOGIC") == ps.SHORT_LOGIC[recorded],
            f"S1 logic pine={pine.get('ORACLE_S1_LOGIC')} "
            f"manifest={ps.SHORT_LOGIC[recorded]} "
            f"global={pine.get('ORACLE_GLOBAL_STATUS')}"))

    python_ok = all(r["status"] == "PASS" for r in results)
    ctx_ok = all(c["result"] == "PASS" for c in ctx_checks)
    cross_ok = all(c["result"] == "PASS" for c in cross)

    return {
        "stage": "S1",
        "python_side": {"fixtures": results, "all_pass": python_ok},
        "unsupported_contexts": {"checks": ctx_checks, "all_pass": ctx_ok},
        "pine_static": {
            "lint_errors": lint_errors,
            "constants": pine,
            "cross_checks": cross,
            "all_pass": not lint_errors and cross_ok,
        },
        "tradingview_comparison": {
            "performed": False,
            "reason": "TradingView cannot be executed from this environment; it has "
                      "no headless or API run path. The Pine half of S1 parity is "
                      "unproven until an operator completes the checklist.",
            "checklist": str(CHECKLIST_PATH.relative_to(CT_ROOT)).replace("\\", "/"),
        },
        "s1_status": "UNVERIFIED",
        "s1_status_reason": (
            "Python-side invariants and Pine static validation pass, but no "
            "TradingView comparison has been recorded. S1 is promoted to MATCHED "
            "only by `--record PASS` after the checklist is completed."
        ) if (python_ok and ctx_ok and cross_ok) else (
            "Python-side or static validation FAILED — fix before attempting a "
            "TradingView comparison."),
        "global_status": "PARTIAL",
    }


def write_checklist(report: dict) -> str:
    """Emit the exact per-bar values an operator must confirm on TradingView."""
    trace = build_trace(symbol="EURUSD", timeframe="1min",
                        input_path=FIXTURE_DIR / "F-S1-BOUNDARIES.csv",
                        fixture_id="F-S1-BOUNDARIES")
    day = build_trace(symbol="EURUSD", timeframe="15min",
                      input_path=FIXTURE_DIR / "F-S1-DAY.csv", fixture_id="F-S1-DAY")

    lines = [
        "# Stage S1 — TradingView verification checklist",
        "",
        f"Build: `{trace['header']['oracle_engine_id']}`  ",
        f"Pine:  `pine/generated/tradingview_visual_oracle.pine`  ",
        f"Trace schema: `{trace['header']['trace_schema_version']}`",
        "",
        "This is the ONLY step that can move S1 from UNVERIFIED to MATCHED. Nothing",
        "in the repository can perform it: TradingView has no headless run path.",
        "",
        "## Setup",
        "",
        "1. Open a **EURUSD** chart on a single fixed feed (record which one).",
        "2. Set the chart timezone to **UTC**, and use **standard candles** (not",
        "   Heikin Ashi / Renko / Range / Kagi / Line Break / P&F).",
        "3. Paste `pine/generated/tradingview_visual_oracle.pine` into the Pine Editor",
        "   and add it to the chart. **Do not edit it** — an edit is detected by",
        "   `python -m tools.oracle.check_freshness` and reports INCOMPATIBLE.",
        "4. Record the Pine v6 compiler result verbatim (pass, or the exact errors).",
        "5. Confirm the HUD shows context `SUPPORTED` and global `PARTIAL`.",
        "",
        "---",
        "",
        "## ROUTE A (preferred) — bulk export, machine-compared",
        "",
        "Do NOT hand-read 24 bars if you can avoid it. Manual transcription is the",
        "single most likely way to record a WRONG parity result, and a false `MATCHED`",
        "would be inherited by every later stage.",
        "",
        "1. Scroll the chart so the fixture window is fully loaded.",
        "2. Right-click the chart → **Export chart data…** → CSV.",
        "3. Run the comparator on the VPS:",
        "",
        "```bash",
        "python -m tools.oracle.compare_tv_export --fixture F-S1-DAY --timeframe 15min --export <downloaded.csv> --feed \"OANDA:EURUSD\" --write",
        "```",
        "",
        "4. Repeat for `F-S1-BOUNDARIES` on a **1-minute** chart.",
        "",
        "The comparator joins on the UTC bar timestamp and diffs every oracle plot",
        "exactly. Bars present on only one side are reported as `FEED_DIFFERENCE`",
        "(limitation L-02) and do not fail the run; a bar present on both whose values",
        "disagree does.",
        "",
        "Both reports are **required** by `--record PASS` — it refuses without them.",
        "",
        "---",
        "",
        "## ROUTE B (fallback) — manual Data Window reading",
        "",
        "Only if the export omits the oracle columns. Open the **Data Window** (Alt+D),",
        "find the bar with that UTC timestamp, and read the integers.",
        "",
        "| # | UTC bar (open) | oracleUtcHour | oracleSessionCode | oracleTransCode | oracleDayKey |",
        "|---|---|---|---|---|---|",
    ]
    for i, b in enumerate(trace["bars"], 1):
        s = b["session"]
        lines.append(
            f"| {i} | `{b['bar_timestamp'][:16]}` | {s['utc_hour']} | "
            f"{s['code']} | {s['transition_code']} | "
            f"{codes_mod.day_key_to_int(b['utc_day']['day_key'])} |")

    lines += [
        "",
        "### Code legend (generated — must match the HUD/debug panel)",
        "",
        "| session | code |", "|---|---|",
    ]
    for k, v in sorted(trace["header"]["codes"]["session"].items(), key=lambda kv: kv[1]):
        lines.append(f"| `{k}` | {v} |")
    lines += ["", "| transition reason | code |", "|---|---|"]
    for k, v in sorted(trace["header"]["codes"]["transition_reason"].items(),
                       key=lambda kv: kv[1]):
        lines.append(f"| `{k}` | {v} |")

    tr = [b for b in day["bars"] if b["session"]["transition"]]
    lines += [
        "",
        "## Second pass — 15m chart, transitions only",
        "",
        "Switch the chart to **15m** and confirm each transition lands on the SAME bar.",
        "",
        "| UTC bar | session | code | reason | reason code |",
        "|---|---|---|---|---|",
    ]
    for b in tr:
        s = b["session"]
        lines.append(f"| `{b['bar_timestamp'][:16]}` | {s['key']} | {s['code']} | "
                     f"{s['transition_reason']} | {s['transition_code']} |")

    lines += [
        "",
        "## Unsupported-context checks",
        "",
        "| Chart | Expected HUD |",
        "|---|---|",
        "| GBPUSD, 15m | `UNSUPPORTED_DATA_CONTEXT` + symbol reason |",
        "| EURUSD, 4h | `UNSUPPORTED_DATA_CONTEXT` + timeframe reason |",
        "| EURUSD, 1D | `UNSUPPORTED_DATA_CONTEXT` + timeframe reason |",
        "",
        "In each case the parity claim must be suppressed and the red warning label",
        "must still be visible.",
        "",
        "## Recording the result",
        "",
        "Only when Route A reports PASS for **both** required fixtures, the visual",
        "checks above pass, and the script compiled:",
        "",
        "```bash",
        "python -m tools.oracle.verify_s1 --record PASS --feed \"OANDA:EURUSD\" --compiled PASS --operator \"<your name>\" --evidence artifacts/tradingview_oracle/s1_tv_compare.json",
        "```",
        "",
        "`--record PASS` **refuses** without a feed, without the compiler result, and",
        "without passing comparator evidence whose build fingerprint matches this",
        "manifest. That is deliberate: it cannot be used as a rubber stamp.",
        "",
        "If anything differs, record the divergence and leave S1 UNVERIFIED. A single",
        "mismatched session code is an S1 failure — there are no tolerances at this",
        "stage; every S1 field is class `exact`.",
        "",
        "## Known limitations affecting this comparison",
        "",
        "- **L-02 FEED-DEPENDENT** — TradingView builds its own bars. A missing or",
        "  extra bar shifts `bar_index` but must NOT change any session code for a",
        "  bar that exists in both.",
        "- **L-09 INDEX-LOCAL** — never compare `bar_index` directly; the checklist",
        "  matches on the UTC timestamp instead.",
        "- **L-20 MANUAL-COMPARISON** — this document exists because the comparison",
        "  cannot be automated from this environment.",
    ]
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--checklist", action="store_true", help="write the TV checklist")
    ap.add_argument("--write-report", action="store_true")
    ap.add_argument("--record", choices=["PASS", "FAIL"],
                    help="record a COMPLETED TradingView comparison in the manifest")
    ap.add_argument("--feed", default="",
                    help='exact TradingView feed, e.g. "OANDA:EURUSD" (required for PASS)')
    ap.add_argument("--compiled", default="",
                    help="Pine v6 compiler outcome: PASS or FAIL (required for PASS)")
    ap.add_argument("--operator", default="", help="verifier identity / label")
    ap.add_argument("--evidence", type=Path, nargs="*", default=[],
                    help="compare_tv_export report(s); required for PASS")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = run()

    if args.record:
        return _record(args.record, args.feed, report, evidence=list(args.evidence),
                       operator=args.operator, compiled=args.compiled)

    if args.json:
        print(json.dumps(report, indent=1, sort_keys=True))
    else:
        print(f"S1 VERIFICATION  ({report['s1_status']})")
        print("\npython-side fixtures:")
        for r in report["python_side"]["fixtures"]:
            print(f"  [{r['status']:<5}] {r['fixture_id']:<20} "
                  f"{r.get('bars', 0):>5} bars @ {r.get('timeframe', '-')}")
            for c in r["checks"]:
                if c["result"] != "PASS":
                    print(f"           FAIL {c['invariant']}: {c['detail']}")
        print("\nunsupported-context refusals:")
        for c in report["unsupported_contexts"]["checks"]:
            print(f"  [{c['result']:<5}] {c['invariant']}: {c['detail']}")
        print("\npine static validation:")
        print(f"  lint errors: {len(report['pine_static']['lint_errors'])}")
        for c in report["pine_static"]["cross_checks"]:
            print(f"  [{c['result']:<5}] {c['invariant']}: {c['detail']}")
        print(f"\nTradingView comparison performed: "
              f"{report['tradingview_comparison']['performed']}")
        print(f"  {report['tradingview_comparison']['reason']}")
        print(f"\nS1: {report['s1_status']}   GLOBAL: {report['global_status']}")

    if args.checklist:
        CHECKLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
        CHECKLIST_PATH.write_text(write_checklist(report), encoding="utf-8")
        print(f"\nchecklist: {CHECKLIST_PATH.relative_to(CT_ROOT)}")
    if args.write_report:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(report, indent=1, sort_keys=True) + "\n",
                               encoding="utf-8")
        print(f"report:    {REPORT_PATH.relative_to(CT_ROOT)}")

    ok = (report["python_side"]["all_pass"]
          and report["unsupported_contexts"]["all_pass"]
          and report["pine_static"]["all_pass"])
    return 0 if ok else 1


#: A PASS must be backed by machine-checked evidence, not an assertion. These are
#: the fixtures whose per-bar fields a TradingView export can mechanically prove.
REQUIRED_EVIDENCE_FIXTURES = {"F-S1-DAY", "F-S1-BOUNDARIES"}


def _load_evidence(paths: list[Path]) -> tuple[list[dict], list[str]]:
    reports, problems = [], []
    for p in paths:
        if not p.is_file():
            problems.append(f"evidence file not found: {p}")
            continue
        try:
            rep = json.loads(p.read_text(encoding="utf-8"))
        except ValueError as exc:
            problems.append(f"{p.name}: unreadable ({exc})")
            continue
        if rep.get("stage") != "S1":
            problems.append(f"{p.name}: not an S1 comparison report")
            continue
        if rep.get("result") != "PASS":
            problems.append(f"{p.name}: fixture {rep.get('fixture')} result is "
                            f"{rep.get('result')}, not PASS")
            continue
        if not rep.get("coverage", {}).get("bars_in_both"):
            problems.append(f"{p.name}: zero bars were actually compared")
            continue
        reports.append(rep)
    return reports, problems


def _record(result: str, feed: str, report: dict,
            evidence: list[Path] | None = None,
            operator: str = "", compiled: str = "") -> int:
    """Stamp a COMPLETED TradingView comparison into the parity manifest.

    Refuses unless BOTH halves are proven:

      * the Python side and static validation are clean, and
      * machine-checked TradingView evidence exists for the required fixtures,
        produced by `tools.oracle.compare_tv_export`, whose fingerprint matches
        this build.

    A PASS asserted without evidence would be exactly the false `MATCHED` this
    apparatus exists to prevent — so `--record PASS` cannot be used as a rubber
    stamp, by anyone, including an operator in a hurry.
    """
    if not MANIFEST_PATH.is_file():
        print(f"REFUSED: no manifest at {MANIFEST_PATH}", file=sys.stderr)
        return 2
    try:
        m = ps.migrate_manifest(json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8")))
    except (ValueError, ps.ParityStatusError) as exc:
        print(f"REFUSED: unreadable manifest: {exc}", file=sys.stderr)
        return 2

    if result == "PASS":
        if not (report["python_side"]["all_pass"]
                and report["unsupported_contexts"]["all_pass"]
                and report["pine_static"]["all_pass"]):
            print("REFUSED: cannot record PASS while Python-side or static "
                  "validation is failing.", file=sys.stderr)
            return 2
        if not feed:
            print("REFUSED: --feed is required for a PASS. The exact TradingView "
                  "feed must be recorded — candle differences explain later-stage "
                  "divergence.", file=sys.stderr)
            return 2
        if compiled.upper() != "PASS":
            print("REFUSED: --compiled PASS is required — record the actual Pine v6 "
                  "compiler outcome.", file=sys.stderr)
            return 2

        reports, problems = _load_evidence(evidence or [])
        if problems:
            print("REFUSED: TradingView evidence is not acceptable:", file=sys.stderr)
            for p in problems:
                print(f"  - {p}", file=sys.stderr)
            return 2
        covered = {r["fixture"] for r in reports}
        missing = sorted(REQUIRED_EVIDENCE_FIXTURES - covered)
        if missing:
            print(f"REFUSED: no passing TradingView comparison for {missing}. Run:\n"
                  "  python -m tools.oracle.compare_tv_export --fixture <id> "
                  "--export <tv.csv> --write", file=sys.stderr)
            return 2
        wrong_build = [r["fixture"] for r in reports
                       if r["trace"]["oracle_engine_hash"]
                       != m["fingerprint"]["oracle_engine_hash"]]
        if wrong_build:
            print(f"REFUSED: evidence for {wrong_build} was produced against a "
                  "different build fingerprint than this manifest.", file=sys.stderr)
            return 2

        m["tested_windows"] = [{
            "fixture_id": r["fixture"],
            "start": None, "end": None,
            "detection_bars": r["coverage"]["bars_in_both"],
            "execution_bars": 0,
            "warmup_excluded_bars": 0,
        } for r in reports]
        m["validation"] = {
            **m.get("validation", {}),
            "result": "PASS",
            "validated_at": None,
            "command": "python -m tools.oracle.verify_s1 --record PASS",
            "stages_run": ["S1"],
            "tradingview_feed": feed,
            "tradingview_compiler_result": compiled.upper(),
            "operator": operator,
            "evidence": [{
                "fixture": r["fixture"], "timeframe": r["timeframe"],
                "export_file": r["export"]["file"],
                "export_sha256": r["export"]["sha256"],
                "bars_compared": r["coverage"]["bars_in_both"],
                "feed": r.get("feed", ""),
            } for r in reports],
            "checklist_sha256": _sha256_file(CHECKLIST_PATH),
            "report_path": str(REPORT_PATH.relative_to(CT_ROOT)).replace("\\", "/"),
        }
        status = ps.REPLAY_MATCHED
    else:
        status = ps.REPLAY_DIVERGENT
        m["validation"] = {**m.get("validation", {}), "result": "FAIL",
                           "validated_at": None, "tradingview_feed": feed,
                           "operator": operator}

    # WHICH DIMENSION DOES THIS EVIDENCE ANSWER? `compare_tv_export` runs Python
    # over the PRODUCTION dataset and compares it to Pine on the broker's chart,
    # which is the `fixture_bars` basis — so it is production-replay evidence, not
    # logic evidence. Filing it as logic parity would credit the Pine for
    # something this comparison cannot see, and blame it for feed differences it
    # did not cause. Logic parity is recorded by `record_parity` from a
    # `chart_bars` run.
    m["stages"]["S1"]["production_replay_parity"] = {
        **m["stages"]["S1"]["production_replay_parity"],
        "status": status,
        "reason": None,
    }
    try:
        ps.validate_stage_record("S1", m["stages"]["S1"])
    except ps.ParityStatusError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    m["global_status"] = ps.global_status(m["stages"])
    MANIFEST_PATH.write_text(ps.serialize_manifest(m), encoding="utf-8")
    print(f"recorded S1 production-replay parity = {status}  "
          f"(global {m['global_status']})")
    print("NOTE: logic parity is NOT set by this command — use "
          "`python -m tools.oracle.record_parity` with a chart_bars report.")
    print("NOTE: regenerate Pine so the embedded status matches:")
    print("      python -m tools.oracle.generate_pine --write")
    return 0


def _sha256_file(p: Path) -> str | None:
    import hashlib
    if not p.is_file():
        return None
    return hashlib.sha256(
        p.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


if __name__ == "__main__":
    sys.exit(main())
