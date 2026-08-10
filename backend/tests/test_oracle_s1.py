"""Stage S1 tests — data, time, session, generation, freshness, repo safety.

These pin the S1 half of the oracle the way test_oracle_contract.py pins the
Phase 0.5 half. Several assert behaviour that is COUNTER-INTUITIVE (duplicates
survive, a weekend gap emits no transition, H4 is refused) — those exist because
the behaviour was measured against the pinned engine and a "reasonable" Pine
implementation would get them wrong.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import fingerprint as fp  # noqa: E402
from tools.oracle import session_codes as codes_mod  # noqa: E402
from tools.oracle.check_freshness import evaluate  # noqa: E402
from tools.oracle.engine_access import load_engine  # noqa: E402
from tools.oracle.export_trace import (TIMEFRAME_MINUTES, TraceError,  # noqa: E402
                                       build_trace, canonical_json, content_hash)
from tools.oracle.generate_pine import (MANIFEST_PATH, OUT_PINE,  # noqa: E402
                                        GenerateError, generate)
from tools.oracle.impact import analyse, load_map  # noqa: E402
from tools.oracle.lint_pine import lint  # noqa: E402
from tools.oracle.verify_s1 import run as verify_run  # noqa: E402

FIXTURES = CT_ROOT / "golden" / "tradingview_oracle" / "s1"


def _trace(fid, tf="15min", **kw):
    return build_trace(symbol="EURUSD", timeframe=tf,
                       input_path=FIXTURES / f"{fid}.csv", fixture_id=fid, **kw)


# ══ trace: UTC and day semantics ═════════════════════════════════════════════

def test_trace_timestamps_are_utc_bar_open():
    t = _trace("F-S1-DAY")
    first = t["bars"][0]
    assert first["bar_timestamp"].endswith("+00:00")
    assert first["bar_timestamp"] == "2026-01-05 00:00:00+00:00"
    # epoch ms must agree with the string, so Pine's `time` can be matched
    import datetime as dt
    parsed = dt.datetime.strptime(first["bar_timestamp"][:19], "%Y-%m-%d %H:%M:%S")
    parsed = parsed.replace(tzinfo=dt.timezone.utc)
    assert first["bar_epoch_ms"] == int(parsed.timestamp() * 1000)


def test_detection_bars_are_bar_open_labelled_and_complete():
    t = _trace("F-S1-DAY")
    assert len(t["bars"]) == 96                    # 1440 / 15
    assert t["bars"][0]["bar_timestamp"][11:16] == "00:00"
    assert t["bars"][-1]["bar_timestamp"][11:16] == "23:45"
    assert all(b["bar_completeness"] == "closed" for b in t["bars"])


def test_utc_day_key_is_calendar_not_exchange_session():
    t = _trace("F-S1-MIDNIGHT", tf="1min")
    rolls = [b for b in t["bars"] if b["utc_day"]["day_transition"]]
    at_midnight = [b for b in rolls if b["bar_timestamp"][11:16] == "00:00"]
    assert at_midnight, "no rollover at 00:00 UTC"
    b = at_midnight[0]
    assert b["utc_day"]["day_key"] == "2026-01-06"
    assert b["utc_day"]["prev_day_key"] == "2026-01-05"
    # An exchange-session day would roll at 17:00 New York, never at 00:00 UTC.
    assert not any(x["utc_day"]["day_transition"] and x["bar_timestamp"][11:16] == "22:00"
                   for x in t["bars"])


# ══ trace: session assignment ════════════════════════════════════════════════

BOUNDARY_EXPECT = [
    ("00:00", "asia"), ("06:59", "asia"), ("07:00", "london"),
    ("09:59", "london"), ("10:00", "lull"), ("11:59", "lull"),
    ("12:00", "newYork"), ("14:59", "newYork"), ("15:00", "ny_pm"),
    ("16:59", "ny_pm"), ("17:00", "outside"), ("23:59", "outside"),
]


@pytest.mark.parametrize("hhmm,expected", BOUNDARY_EXPECT)
def test_session_boundary_inclusivity(hhmm, expected):
    """[start, end) — inclusive start, EXCLUSIVE end. 07:00 is London; 06:59 is
    Asia; 17:00 falls through to the Outside fallback."""
    t = _trace("F-S1-BOUNDARIES", tf="1min")
    got = {b["bar_timestamp"][11:16]: b["session"]["key"] for b in t["bars"]}
    assert got[hhmm] == expected


def test_session_windows_and_fallback_are_reported():
    t = _trace("F-S1-BOUNDARIES", tf="1min")
    by = {b["bar_timestamp"][11:16]: b["session"] for b in t["bars"]}
    assert by["07:00"]["window_start_hour"] == 7
    assert by["07:00"]["window_end_hour"] == 10
    assert by["07:00"]["is_fallback"] is False
    assert by["17:00"]["is_fallback"] is True
    assert by["17:00"]["window_start_hour"] is None


def test_session_transitions_and_reasons():
    t = _trace("F-S1-DAY")
    tr = [(b["bar_timestamp"][11:16], b["session"]["key"],
           b["session"]["transition_reason"]) for b in t["bars"]
          if b["session"]["transition"]]
    assert tr == [
        ("00:00", "asia", "first_bar"),
        ("07:00", "london", "session_open"),
        ("10:00", "lull", "session_open"),
        ("12:00", "newYork", "session_open"),
        ("15:00", "ny_pm", "session_open"),
        ("17:00", "outside", "session_close_to_fallback"),
    ]


def test_session_codes_are_contract_derived_and_stable():
    contract = json.loads(
        (CT_ROOT / "contracts" / "tradingview_oracle_contract.json").read_text(
            encoding="utf-8"))
    codes = codes_mod.session_codes(contract)
    assert codes == {"asia": 1, "london": 2, "lull": 3,
                     "newYork": 4, "ny_pm": 5, "outside": 6}
    assert codes_mod.code_for("nope", codes) == codes_mod.CODE_UNKNOWN
    assert codes_mod.code_for(None, codes) == codes_mod.CODE_NONE


# ══ trace: DST ═══════════════════════════════════════════════════════════════

@pytest.mark.parametrize("fid", ["F-S1-DST-US", "F-S1-DST-EU",
                                 "F-S1-DST-END-US", "F-S1-DST-END-EU"])
def test_sessions_move_with_the_london_clock_on_dst_dates(fid):
    """INVERTED BY M-SESSION-DST-1. This test used to assert that no transition
    could move a boundary, because the pinned engine classified on the UTC hour
    and contained no tz call at all. `strategy_core/sessions.py` now converts
    through `ZoneInfo("Europe/London")`, so the boundaries are fixed in LONDON
    local terms and their UTC projection MUST move — asserting otherwise is
    asserting the defect.

    The schedule is unchanged in local terms; only the mapping to UTC moves."""
    import datetime as dt
    from zoneinfo import ZoneInfo

    london = ZoneInfo("Europe/London")
    t = _trace(fid, tf="1min")

    def expect(h):
        for s, e, k in ((0, 7, "asia"), (7, 10, "london"), (10, 12, "lull"),
                        (12, 15, "newYork"), (15, 17, "ny_pm")):
            if s <= h < e:
                return k
        return "outside"

    bad, shifted = [], 0
    for b in t["bars"]:
        ts = dt.datetime.fromisoformat(b["bar_timestamp"])
        local_h = ts.astimezone(london).hour
        if b["session"]["key"] != expect(local_h):
            bad.append(b["bar_timestamp"])
        if expect(local_h) != expect(b["session"]["utc_hour"]):
            shifted += 1
    assert not bad, f"{len(bad)} bars disagree with London local, e.g. {bad[:3]}"
    if fid == "F-S1-DST-EU":
        # The SPRING fixture spans a boundary the shift actually crosses, so a
        # zero here would mean the classifier is still reading the UTC hour.
        # The autumn fixture's window does not, and demanding a shift there
        # would be asserting a property of the fixture, not of the classifier.
        assert shifted > 0, "no bar moved — the classifier is still on UTC"


@pytest.mark.parametrize("fid", ["F-S1-DST-END-US", "F-S1-DST-END-EU"])
def test_no_repeated_or_skipped_utc_hour(fid):
    """A local clock repeats an hour at DST end. UTC never does, so the trace
    must contain no duplicate timestamps."""
    t = _trace(fid, tf="1min")
    stamps = [b["bar_timestamp"] for b in t["bars"]]
    assert len(stamps) == len(set(stamps))


# ══ trace: malformed / awkward data ══════════════════════════════════════════

def test_missing_intervals_are_dropped_not_filled():
    """resample(...).dropna() removes empty intervals, so bar_index is NOT a
    uniform time grid — limitation L-09."""
    t = _trace("F-S1-GAP")
    assert len(t["bars"]) < 96
    idx = [b["bar_index"] for b in t["bars"]]
    assert idx == list(range(len(idx)))            # contiguous index…
    stamps = [b["bar_timestamp"] for b in t["bars"]]
    assert stamps == sorted(stamps)                # …but not a contiguous clock


def test_duplicate_timestamps_survive_because_production_does_not_dedupe():
    t = _trace("F-S1-DUPLICATE", tf="1min")
    integ = t["header"]["source"]["integrity"]
    assert integ["duplicate_timestamps"] > 0
    stamps = [b["bar_timestamp"] for b in t["bars"]]
    assert len(stamps) != len(set(stamps)), (
        "prepare_candles_for_simulation sorts but does NOT de-duplicate; a trace "
        "that removed duplicates would misrepresent production")


def test_out_of_order_input_is_silently_resorted():
    t = _trace("F-S1-OUTOFORDER", tf="1min")
    integ = t["header"]["source"]["integrity"]
    assert integ["out_of_order_rows"] > 0
    assert integ["reordered_by_production"] is True
    stamps = [b["bar_timestamp"] for b in t["bars"]]
    assert stamps == sorted(stamps)


def test_weekend_gap_produces_no_session_transition():
    """Measured: the FX week closes ~Fri 21:45 UTC and reopens ~Sun 22:00 UTC.
    Both sit inside the SAME `outside` window (17:00-24:00), so the ~48h gap is
    invisible to session logic. A Pine build that emitted a weekend "session
    close" would diverge here."""
    t = _trace("F-S1-WEEKEND")
    import datetime as dt
    stamps = [dt.datetime.strptime(b["bar_timestamp"][:19], "%Y-%m-%d %H:%M:%S")
              for b in t["bars"]]
    gaps = [(a, b) for a, b in zip(stamps, stamps[1:])
            if (b - a).total_seconds() > 3600]
    assert gaps, "fixture should contain a weekend gap"
    after = {stamps.index(b) for _, b in gaps}
    assert not [i for i in after if t["bars"][i]["session"]["transition"]]


# ══ trace: context guard ═════════════════════════════════════════════════════

@pytest.mark.parametrize("symbol,tf", [("GBPUSD", "15min"), ("XAUUSD", "1min")])
def test_unsupported_symbol_is_refused(symbol, tf):
    with pytest.raises(TraceError, match="unsupported data context"):
        build_trace(symbol=symbol, timeframe=tf, input_path=FIXTURES / "F-S1-DAY.csv")


@pytest.mark.parametrize("tf", ["H4", "1440min", "240min"])
def test_unsupported_timeframe_is_refused(tf):
    with pytest.raises(TraceError):
        build_trace(symbol="EURUSD", timeframe=tf,
                    input_path=FIXTURES / "F-S1-DAY.csv")


def test_every_supported_timeframe_divides_sixty():
    """A session window is [start,end) on the UTC HOUR, so a bar that spans an
    hour boundary would have two valid answers. H4 straddles 4 bars a day —
    measured — which is why it is refused."""
    for tf, minutes in TIMEFRAME_MINUTES.items():
        if minutes <= 60:
            assert 60 % minutes == 0, f"{tf} does not divide 60"


def test_unsupported_context_can_be_traced_when_explicitly_allowed():
    t = build_trace(symbol="EURUSD", timeframe="15min",
                    input_path=FIXTURES / "F-S1-DAY.csv", allow_unsupported=True)
    assert t["bars"][0]["data_context"]["status"] == "SUPPORTED"


# ══ trace: determinism and schema ════════════════════════════════════════════

def test_trace_export_is_deterministic():
    assert content_hash(_trace("F-S1-DAY")) == content_hash(_trace("F-S1-DAY"))


def test_trace_declares_stage_and_refuses_to_fake_later_sections():
    """A 1m trace carries S1 sections ONLY.

    Production computes structure on the resampled DETECTION frame, never on 1m,
    so emitting s2/s3/s4 here would invent values production never had. The
    detection-timeframe equivalent lives in test_oracle_s234.py.
    """
    t = _trace("F-S1-BOUNDARIES", tf="1min")
    h = t["header"]
    assert h["stage"] == "S1"
    assert h["trace_schema_version"] == fp.TRACE_SCHEMA_VERSION
    assert set(h["sections_implemented"]) == {"time", "session", "utc_day",
                                              "data_context"}
    for section in ("structure", "order_blocks", "market_state", "candidates",
                    "cohorts", "lifecycle", "volatility", "swings"):
        assert section in h["sections_unimplemented"]
    for key in ("s2", "s3", "s4"):
        # ABSENT, not null/zero — a placeholder would be indistinguishable from
        # implemented parity.
        assert all(key not in b for b in t["bars"])


def test_trace_matches_its_schema_required_fields():
    schema = json.loads((CT_ROOT / "contracts" /
                         "tradingview_oracle_trace.schema.json").read_text(
                             encoding="utf-8"))
    assert schema["properties"]["header"]["properties"][
        "trace_schema_version"]["const"] == fp.TRACE_SCHEMA_VERSION
    req_hdr = schema["properties"]["header"]["required"]
    t = _trace("F-S1-DAY")
    for k in req_hdr:
        assert k in t["header"], f"header missing {k}"
    req_bar = schema["definitions"]["detection_bar"]["required"]
    for k in req_bar:
        assert k in t["bars"][0], f"bar missing {k}"
    assert "structure" not in req_bar, (
        "structure must be OPTIONAL so an S1 trace need not fabricate it")


# ══ Pine generation ══════════════════════════════════════════════════════════

def test_pine_generation_is_deterministic():
    a = generate(write=False)["pine"]
    b = generate(write=False)["pine"]
    assert a == b


def test_generated_pine_on_disk_matches_a_fresh_generation():
    assert OUT_PINE.is_file(), "run `python -m tools.oracle.generate_pine --write`"
    assert OUT_PINE.read_text(encoding="utf-8") == generate(write=False)["pine"]


def test_generated_pine_lints_clean():
    findings = lint(OUT_PINE.read_text(encoding="utf-8"))
    errors = [f for f in findings if f["severity"] == "error"]
    assert not errors, errors


def test_generated_pine_has_no_absolute_paths_or_wallclock():
    src = OUT_PINE.read_text(encoding="utf-8")
    assert "C:\\" not in src and "/Users/" not in src and "/home/" not in src
    assert not re.search(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d", src)


def test_generated_pine_carries_the_do_not_edit_banner():
    src = OUT_PINE.read_text(encoding="utf-8")
    assert "DO NOT EDIT" in src
    assert "check_freshness" in src
    assert "THIS INDICATOR IS WRONG" in src


def test_generated_header_matches_the_contract():
    contract = json.loads((CT_ROOT / "contracts" /
                           "tradingview_oracle_contract.json").read_text(
                               encoding="utf-8"))
    src = OUT_PINE.read_text(encoding="utf-8")
    f = contract["fingerprint"]
    assert f'ORACLE_ENGINE_HASH           = "{f["oracle_engine_hash"]}"' in src
    assert f'ORACLE_TRACE_SCHEMA          = "{contract["trace_schema_version"]}"' in src
    assert contract["engine"]["engine_manifest_id"][:16] in src


def test_generated_pine_declares_a_version_6_indicator_not_a_strategy():
    src = OUT_PINE.read_text(encoding="utf-8")
    assert "//@version=6" in src
    assert re.search(r"^indicator\(", src, re.M)
    assert not re.search(r"^strategy\(", src, re.M)


def test_generated_pine_contains_no_stage_two_plus_logic():
    """S1 is data/time/session only. Nothing here may compute structure."""
    src = OUT_PINE.read_text(encoding="utf-8")
    code = "\n".join(l.split("//")[0] for l in src.splitlines())
    for banned in ("ta.atr", "ta.rma", "ta.ema", "ta.sma", "ta.pivothigh",
                   "ta.pivotlow", "swingHigh", "orderBlock", "bosLevel"):
        assert banned not in code, f"{banned} is beyond S1"


def test_generated_pine_has_no_undeclared_constants():
    """Regression for CE10272.

    `90_debug.pinefrag` referenced `ORACLE_SOURCE_HASH_SHORT`, which the generator
    never emitted. Every structural lint rule passed and the build shipped; the
    failure only surfaced when a human pasted it into TradingView and the compiler
    said "Undeclared identifier". Pine reports this at COMPILE time, which is the
    one thing this environment cannot do — so it has to be caught statically.
    """
    findings = lint(OUT_PINE.read_text(encoding="utf-8"))
    undeclared = [f for f in findings if f["rule"] == "undeclared_constant"]
    assert not undeclared, undeclared


def test_undeclared_constant_rule_fires_and_ignores_hex_colours():
    """The rule must catch a missing constant WITHOUT flagging `#EF476F`.

    A colour literal whose hex starts with a letter looks exactly like a
    SCREAMING_SNAKE identifier once you strip the `#`. Flagging those would train
    people to ignore this check, which is worse than not having it.
    """
    bad = '//@version=6\nindicator("x")\nplot(SOME_MISSING_CONST)\n'
    rules = {f["rule"] for f in lint(bad) if f["severity"] == "error"}
    assert "undeclared_constant" in rules

    good = ('//@version=6\nindicator("x")\n'
            'MY_COLOUR = color.new(#EF476F, 90)\n'
            'plot(1, color = MY_COLOUR)\n')
    undeclared = [f for f in lint(good) if f["rule"] == "undeclared_constant"]
    assert not undeclared, undeclared


def test_generated_pine_has_no_global_assignment_inside_a_function():
    """Regression for CE10088.

    v0.3.0 kept retention counters inside `f_pushLabel`, which Pine refuses with
    "Cannot modify global variable ... in function". Like CE10272 it is only
    reported at COMPILE time — the one thing this environment cannot do — so it
    must be caught statically.
    """
    findings = lint(OUT_PINE.read_text(encoding="utf-8"))
    bad = [f for f in findings if f["rule"] == "global_assign_in_function"]
    assert not bad, bad


def test_global_assign_rule_catches_the_bug_that_shipped():
    src = ('//@version=6\nindicator("x")\n'
           'var int markersDropped = 0\n'
           'var array<label> oracleLabels = array.new<label>()\n'
           'f_pushLabel(label lbl) =>\n'
           '    array.push(oracleLabels, lbl)\n'
           '    if array.size(oracleLabels) > 10\n'
           '        label.delete(array.shift(oracleLabels))\n'
           '        markersDropped := markersDropped + 1\n')
    hits = [f for f in lint(src) if f["rule"] == "global_assign_in_function"]
    assert hits and "markersDropped" in hits[0]["detail"]


def test_global_assign_rule_allows_assignment_inside_an_if_block():
    """An `if` at global scope is indented too, but assigning to a global there
    is perfectly legal — conflating the two would make the rule useless."""
    src = ('//@version=6\nindicator("x")\n'
           'var int counter = 0\n'
           'if barstate.islast\n'
           '    counter := counter + 1\n')
    assert not [f for f in lint(src) if f["rule"] == "global_assign_in_function"]


def test_global_assign_rule_allows_locals_inside_a_function():
    src = ('//@version=6\nindicator("x")\n'
           'f_calc(int a) =>\n'
           '    int t = a * 2\n'
           '    t := t + 1\n'
           '    t\n')
    assert not [f for f in lint(src) if f["rule"] == "global_assign_in_function"]


def test_undeclared_constant_rule_ignores_words_inside_strings():
    """`"SHADOW MODE · EXECUTION STATE UNKNOWN"` must not read as identifiers.

    The linter blanks string literals before this rule runs. A first attempt at a
    separate cross-check test skipped that step and flagged 29 phantom
    "constants" — all of them words inside banner text. Pinned so the
    string-blanking cannot be dropped.
    """
    src = ('//@version=6\nindicator("x")\n'
           'msg = "SHADOW MODE · EXECUTION STATE UNKNOWN · UNSUPPORTED_DATA_CONTEXT"\n'
           'plot(1)\n')
    undeclared = [f for f in lint(src) if f["rule"] == "undeclared_constant"]
    assert not undeclared, undeclared


def test_pine_arrays_use_array_from_not_global_push():
    """A top-level `array.push` re-executes every bar and grows the array without
    bound — the arrays must be built inside the `var` initialiser."""
    src = OUT_PINE.read_text(encoding="utf-8")
    for line in src.splitlines():
        assert not line.startswith("array.push("), line
    assert "array.from(" in src


def test_no_table_overflows_in_the_generated_pine():
    """Regression for RE10040.

    v0.5.0 added nine HUD rows to a table still declared with 24 and produced
    "Row 24 is out of table bounds" on a live chart. The debug table was
    simultaneously latent: 18 declared, 21 reachable writes, which would have
    fired the first time anyone enabled debug mode.

    This is a RUNTIME error — it survives compilation AND static syntax checking,
    and when it fires the whole indicator stops rendering.
    """
    findings = lint(OUT_PINE.read_text(encoding="utf-8"))
    overflow = [f for f in findings if f["rule"] == "table_row_overflow"]
    assert not overflow, overflow


def test_table_overflow_rule_fires_and_clears():
    over = ('//@version=6\nindicator("x")\n'
            'var table hud = table.new(position.top_right, 2, 3,\n'
            '     border_width = 1)\n'
            'f_row(int r, string k) =>\n'
            '    table.cell(hud, 0, r, k)\n'
            'if barstate.islast\n'
            '    int r = 0\n'
            '    f_row(r, "a")\n'
            '    r := r + 1\n'
            '    f_row(r, "b")\n'
            '    r := r + 1\n'
            '    f_row(r, "c")\n'
            '    r := r + 1\n'
            '    f_row(r, "d")\n')
    assert [f for f in lint(over) if f["rule"] == "table_row_overflow"]
    assert not [f for f in lint(over.replace("2, 3,", "2, 40,"))
                if f["rule"] == "table_row_overflow"]


def test_table_row_writes_are_guarded_at_runtime():
    """Even with correct sizing, the helpers bound-check. A table overflow kills
    the entire indicator, so degrading to an undrawn row is strictly better than
    a blank chart."""
    src = OUT_PINE.read_text(encoding="utf-8")
    assert "if r < HUD_ROWS" in src
    assert "if r < DBG_ROWS" in src


def test_pine_warning_renders_on_both_hud_paths():
    src = OUT_PINE.read_text(encoding="utf-8")
    hits = [l for l in src.splitlines()
            if "SHADOW MODE" in l and "EXECUTION STATE UNKNOWN" in l]
    assert len(hits) >= 2, "the warning must survive the HUD being switched off"


def test_pine_session_table_matches_production():
    engine = load_engine(require_pin=True)
    src = OUT_PINE.read_text(encoding="utf-8")
    starts = [s for s, *_ in engine.core._SESSION_SCHEDULE]
    ends = [e for _, e, *_ in engine.core._SESSION_SCHEDULE]
    keys = [k for *_, k in engine.core._SESSION_SCHEDULE]
    assert f"array.from({', '.join(str(x) for x in starts)})" in src
    assert f"array.from({', '.join(str(x) for x in ends)})" in src
    assert f"array.from({', '.join(chr(34) + k + chr(34) for k in keys)})" in src
    assert f'SESSION_FALLBACK_KEY         = "{engine.core._SESSION_OUTSIDE[1]}"' in src


def test_generation_refuses_when_contract_is_stale(monkeypatch):
    """A contract that does not describe the deployed engine must never produce a
    build — the artefact would be labelled with a fingerprint it does not mirror."""
    import tools.oracle.generate_pine as gp
    real = gp.fp.from_engine

    def fake(engine, config, table):
        out = dict(real(engine, config, table))
        out["oracle_engine_hash"] = "0" * 64
        return out

    monkeypatch.setattr(gp.fp, "from_engine", fake)
    with pytest.raises(GenerateError, match="does not match the deployed engine"):
        gp.generate(write=False)


# ══ parity manifest ══════════════════════════════════════════════════════════

def test_manifest_exists_and_is_schema_shaped():
    assert MANIFEST_PATH.is_file()
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    schema = json.loads((CT_ROOT / "contracts" /
                         "tradingview_oracle_parity_manifest.schema.json").read_text(
                             encoding="utf-8"))
    for k in schema["required"]:
        assert k in m, f"manifest missing {k}"


def test_manifest_never_claims_global_matched():
    """Global MATCHED now requires all THREE dimensions on every stage, so it is
    unreachable while the feed differs. Unimplemented stages may claim nothing."""
    from tools.oracle import parity_status as ps
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert m["global_status"] == "PARTIAL"
    implemented = {"S1", "S2", "S3", "S4", "S5", "S6"}
    for stage, rec in m["stages"].items():
        ps.validate_stage_record(stage, rec)
        if stage in implemented:
            assert rec["implementation_status"] == ps.IMPL_IMPLEMENTED
        else:
            assert rec["implementation_status"] == ps.IMPL_UNIMPLEMENTED
            # The property is "cannot CLAIM parity", not "must say nothing".
            # S7 is unimplemented AND carries
            # LOGIC_UNATTAINABLE_IN_CURRENT_CONTEXT, which is a finding, not a
            # claim: the pre-fill path runs on 1-minute candles and a 15-minute
            # chart cannot order events inside its own bars. Asserting the
            # narrower equality would have forced that finding to be discarded.
            assert rec["logic_parity"]["status"] not in ps.LOGIC_GATE_PASSING, (
                stage, "an unimplemented stage cannot claim logic parity")


def test_manifest_records_fixture_hashes_and_limitations():
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert len(m["golden_fixtures"]) >= 10
    for fx in m["golden_fixtures"]:
        assert (CT_ROOT / fx["path"]).is_file()
    ids = {l["id"] for l in m["known_limitations"]}
    assert {"L-02", "L-06", "L-09", "L-20"} <= ids


def test_manifest_is_deterministic_for_identical_inputs():
    a = generate(write=False)["manifest"]
    b = generate(write=False)["manifest"]
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
    assert a["generated_at"] is None, (
        "a wall-clock stamp would make regeneration non-deterministic")


def test_manifest_has_no_unsupported_active_features():
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    assert m["unsupported_active_features"] == []


# ══ freshness ════════════════════════════════════════════════════════════════

def test_freshness_reports_partial_or_unverified_never_current():
    r = evaluate()
    assert r["status"] in ("UNVERIFIED", "PARTIAL")
    assert r["status"] != "CURRENT"


@pytest.fixture
def mirrored_repo(tmp_path):
    """A COPY of every artefact freshness inspects, plus a root to resolve against.

    Tamper tests must never mutate the real repo: these run under pytest-xdist,
    so an edit here would race any worker reading the same file — an intermittent
    failure that only shows up under load. `evaluate(root=…)` exists for this.
    """
    import shutil
    m = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    wanted = [m["pine_source"]["path"]]
    wanted += [f["path"] for f in m["pine_source"]["module_sources"]
               if not f.get("generated")]
    wanted += [f["path"] for f in m["golden_fixtures"]]
    for rel in wanted:
        dst = tmp_path / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(CT_ROOT / rel, dst)
    manifest_copy = tmp_path / "parity_manifest.json"
    shutil.copy2(MANIFEST_PATH, manifest_copy)
    return tmp_path, manifest_copy


def test_freshness_is_clean_against_an_untouched_mirror(mirrored_repo):
    root, manifest = mirrored_repo
    r = evaluate(manifest_path=manifest, root=root)
    codes = {f["code"] for f in r["findings"]}
    assert "pine_artifact_edited" not in codes
    assert "pine_fragment_edited" not in codes
    assert "fixture_changed" not in codes


def test_freshness_detects_a_hand_edited_generated_file(mirrored_repo):
    root, manifest = mirrored_repo
    target = root / json.loads(manifest.read_text(encoding="utf-8"))["pine_source"]["path"]
    target.write_text(target.read_text(encoding="utf-8") + "\n// tampered\n",
                      encoding="utf-8", newline="\n")
    r = evaluate(manifest_path=manifest, root=root)
    assert r["status"] == "INCOMPATIBLE"
    assert any(f["code"] == "pine_artifact_edited" for f in r["findings"])


def test_freshness_detects_an_edited_source_fragment(mirrored_repo):
    root, manifest = mirrored_repo
    frag = root / "pine" / "src" / "30_time.pinefrag"
    frag.write_text(frag.read_text(encoding="utf-8") + "\n// tampered\n",
                    encoding="utf-8", newline="\n")
    r = evaluate(manifest_path=manifest, root=root)
    assert any(f["code"] == "pine_fragment_edited" for f in r["findings"])


def test_freshness_detects_a_changed_golden_fixture(mirrored_repo):
    root, manifest = mirrored_repo
    fx = root / "golden" / "tradingview_oracle" / "s1" / "F-S1-BOUNDARIES.csv"
    fx.write_bytes(fx.read_bytes() + b"2026-01-05 12:00:00+00:00,1,1,1,1,1\n")
    r = evaluate(manifest_path=manifest, root=root)
    assert any(f["code"] == "fixture_changed" for f in r["findings"])


def test_freshness_detects_a_missing_fixture(mirrored_repo):
    root, manifest = mirrored_repo
    (root / "golden" / "tradingview_oracle" / "s1" / "F-S1-DAY.csv").unlink()
    r = evaluate(manifest_path=manifest, root=root)
    assert any(f["code"] == "fixture_missing" for f in r["findings"])


# ══ change impact ════════════════════════════════════════════════════════════

def test_impact_map_covers_the_s1_production_surface():
    m = load_map()
    mapped = {p for e in m["entries"] for p in e["production"]}
    for path in ("strategy_core/sessions.py", "src/resample.py",
                 "strategy_core/regime.py", "live/runner.py"):
        assert path in mapped, f"{path} is unmapped"


def test_session_change_names_s1_fixtures_and_fragments():
    r = analyse(["strategy_core/sessions.py"], load_map())
    assert "S1" in r["stages"]
    assert "F-S1-BOUNDARIES" in r["fixtures"]
    assert any("40_sessions" in p for p in r["pine_modules"])
    assert "backend/tests/test_oracle_s1.py" in r["python_tests"]


def test_editing_a_pine_fragment_is_classified():
    r = analyse(["pine/src/40_sessions.pinefrag"], load_map())
    assert r["overall_category"] == "ALGORITHM_PARITY_UPDATE"
    assert "S1" in r["stages"]


# ══ verification honesty ═════════════════════════════════════════════════════

def test_verify_s1_passes_python_side_but_stays_unverified():
    r = verify_run()
    assert r["python_side"]["all_pass"], r["python_side"]
    assert r["unsupported_contexts"]["all_pass"]
    assert r["pine_static"]["all_pass"], r["pine_static"]
    assert r["tradingview_comparison"]["performed"] is False
    assert r["global_status"] == "PARTIAL"


def test_verify_s1_refuses_to_record_pass_over_failures():
    import tools.oracle.verify_s1 as v
    broken = v.run()
    broken["python_side"]["all_pass"] = False
    assert v._record("PASS", "OANDA:EURUSD", broken, compiled="PASS") == 2


def test_verify_s1_refuses_pass_without_feed_compiler_or_evidence():
    """A PASS must be backed by machine-checked evidence, not an assertion."""
    import tools.oracle.verify_s1 as v
    clean = v.run()
    before = MANIFEST_PATH.read_text(encoding="utf-8")
    assert v._record("PASS", "", clean, compiled="PASS") == 2          # no feed
    assert v._record("PASS", "F:X", clean, compiled="") == 2           # no compiler result
    assert v._record("PASS", "F:X", clean, compiled="PASS",
                     evidence=[]) == 2                                 # no evidence
    assert MANIFEST_PATH.read_text(encoding="utf-8") == before, (
        "a refused record must not touch the manifest")


def test_verify_s1_refuses_evidence_from_a_different_build(tmp_path):
    import tools.oracle.verify_s1 as v
    clean = v.run()
    bogus = tmp_path / "e.json"
    bogus.write_text(json.dumps({
        "stage": "S1", "fixture": "F-S1-DAY", "timeframe": "15min", "result": "PASS",
        "coverage": {"bars_in_both": 96}, "export": {"file": "x", "sha256": "0"},
        "trace": {"oracle_engine_hash": "0" * 64},
    }), encoding="utf-8")
    before = MANIFEST_PATH.read_text(encoding="utf-8")
    # It covers only one of the two required fixtures AND has a wrong fingerprint.
    assert v._record("PASS", "F:X", clean, compiled="PASS", evidence=[bogus]) == 2
    assert MANIFEST_PATH.read_text(encoding="utf-8") == before


# ══ TradingView export comparator ════════════════════════════════════════════

def _synth_tv_export(fixture, timeframe, path, perturb=None, drop=0, epoch=False):
    """A CSV shaped like TradingView's 'Export chart data', built from the trace."""
    import csv as _csv
    from tools.oracle.compare_tv_export import FIELD_MAP
    t = _trace(fixture, tf=timeframe)
    cols = ["time", "open", "high", "low", "close", "Volume"] + list(FIELD_MAP)
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=cols, lineterminator="\n")
        w.writeheader()
        for i, b in enumerate(t["bars"]):
            if drop and i < drop:
                continue
            ts = (str(b["bar_epoch_ms"] // 1000) if epoch
                  else b["bar_timestamp"][:19].replace(" ", "T") + "Z")
            row = {"time": ts, "open": b["ohlcv"]["open"], "high": b["ohlcv"]["high"],
                   "low": b["ohlcv"]["low"], "close": b["ohlcv"]["close"],
                   "Volume": b["ohlcv"]["volume"]}
            for f, get in FIELD_MAP.items():
                row[f] = get(b)
            if perturb:
                perturb(i, row)
            w.writerow(row)
    return t


def test_comparator_passes_a_faithful_export(tmp_path):
    from tools.oracle.compare_tv_export import compare
    p = tmp_path / "e.csv"
    _synth_tv_export("F-S1-DAY", "15min", p)
    r = compare("F-S1-DAY", "15min", p, feed="TEST:EURUSD")
    assert r["result"] == "PASS"
    assert r["coverage"]["bars_in_both"] == 96


def test_comparator_accepts_epoch_timestamps(tmp_path):
    from tools.oracle.compare_tv_export import compare
    p = tmp_path / "e.csv"
    _synth_tv_export("F-S1-DAY", "15min", p, epoch=True)
    r = compare("F-S1-DAY", "15min", p)
    assert r["result"] == "PASS" and r["coverage"]["bars_in_both"] == 96


def test_comparator_catches_a_single_wrong_session_code(tmp_path):
    from tools.oracle.compare_tv_export import compare
    p = tmp_path / "e.csv"
    _synth_tv_export("F-S1-DAY", "15min", p,
                     perturb=lambda i, row: row.update(oracleSessionCode=1)
                     if i == 28 else None)
    r = compare("F-S1-DAY", "15min", p)
    assert r["result"] == "FAIL"
    assert r["fields"]["oracleSessionCode"]["fail"] == 1
    assert r["mismatches"][0]["bar_utc"] == "2026-01-05 07:00"


def test_comparator_catches_a_one_bar_shift(tmp_path):
    """The classic boundary defect: everything correct, one bar late."""
    import csv as _csv
    from tools.oracle.compare_tv_export import FIELD_MAP, compare
    t = _trace("F-S1-DAY")
    p = tmp_path / "shift.csv"
    with p.open("w", newline="", encoding="utf-8") as fh:
        w = _csv.DictWriter(fh, fieldnames=["time"] + list(FIELD_MAP),
                            lineterminator="\n")
        w.writeheader()
        for i, b in enumerate(t["bars"]):
            src = t["bars"][max(0, i - 1)]
            row = {"time": b["bar_timestamp"][:19].replace(" ", "T") + "Z"}
            for f, get in FIELD_MAP.items():
                row[f] = get(src)
            w.writerow(row)
    assert compare("F-S1-DAY", "15min", p)["result"] == "FAIL"


def test_comparator_refuses_an_export_without_oracle_columns(tmp_path):
    from tools.oracle.compare_tv_export import CompareError, compare
    p = tmp_path / "bare.csv"
    p.write_text("time,open,high,low,close\n2026-01-05T00:00:00Z,1,1,1,1\n",
                 encoding="utf-8")
    with pytest.raises(CompareError, match="missing oracle plot column"):
        compare("F-S1-DAY", "15min", p)


def test_comparator_treats_feed_gaps_as_feed_difference_not_failure(tmp_path):
    """Bars the feeds disagree about are limitation L-02, not a Pine defect."""
    from tools.oracle.compare_tv_export import compare
    p = tmp_path / "e.csv"
    _synth_tv_export("F-S1-DAY", "15min", p, drop=10)
    r = compare("F-S1-DAY", "15min", p)
    assert r["result"] == "PASS"
    assert r["coverage"]["python_only"] == 10


def test_comparator_fails_when_nothing_overlaps(tmp_path):
    from tools.oracle.compare_tv_export import FIELD_MAP, compare
    p = tmp_path / "e.csv"
    cols = ",".join(["time"] + list(FIELD_MAP))
    vals = ",".join(["1999-01-01T00:00:00Z"] + ["0"] * len(FIELD_MAP))
    p.write_text(f"{cols}\n{vals}\n", encoding="utf-8")
    r = compare("F-S1-DAY", "15min", p)
    assert r["result"] == "FAIL"
    assert r["coverage"]["bars_in_both"] == 0


def test_pine_and_trace_agree_on_the_fingerprint():
    r = verify_run()
    cross = {c["invariant"]: c["result"] for c in r["pine_static"]["cross_checks"]}
    assert cross["pine_matches_trace_fingerprint"] == "PASS"
    assert cross["pine_session_codes_match_contract"] == "PASS"


# ══ repository safety ════════════════════════════════════════════════════════

# The analysis lives in `_lux_write_guard` so that this repository-wide scan and
# the suite-local one in `test_engine_identity.py` cannot drift apart. They were
# separate implementations, and the second — line-based — could not see the very
# defect the first was written for.
from _lux_write_guard import (LUX_LITERAL as _LUX_LITERAL,  # noqa: E402
                              LUX_REFS as _LUX_REFS,
                              LUX_WRITE_MARKER,
                              OPEN_WRITE_MODES as _OPEN_WRITE_MODES,
                              WRITE_FUNCS as _WRITE_FUNCS,
                              WRITE_FUNCS_DST as _WRITE_FUNCS_DST,
                              WRITE_METHODS as _WRITE_METHODS,
                              audited_functions as _audited_functions,
                              write_offenders)


def _lux_write_offenders(path, src, with_meta=False):
    """Repository-wide wrapper; `path` is kept for call-site readability."""
    return write_offenders(src, with_meta=with_meta)


def test_the_lux_write_guard_catches_the_defect_it_missed():
    """Negative test for the guard itself.

    The guard is only worth having if it catches the exact shape that got past
    its predecessor: the path bound to a variable on one line, the write six
    lines later, in a directory the old scan never visited, spelled `LUX_ROOT`
    rather than `lux_root`. Reproduced verbatim here rather than trusted.
    """
    original_defect = '''
import hashlib
from pathlib import Path
LUX_ROOT = Path(__file__).resolve().parents[2].parent / "Lux-OB-Backtester"

def test_blind_spot():
    target = LUX_ROOT / "strategy_core" / "execution.py"
    original = target.read_bytes()
    before = hashlib.sha256(original).hexdigest()
    try:
        target.write_bytes(original + b"\\n# probe\\n")
    finally:
        target.write_bytes(original)
'''
    hits = _lux_write_offenders(Path("fake.py"), original_defect)
    assert hits, "the guard would MISS the very defect it was written for"
    assert any("write_bytes" in h for h in hits)

    # ...and does not condemn a helper that writes to a copy.
    safe = '''
from pathlib import Path
LUX_ROOT = Path("/x/Lux-OB-Backtester")

def mirror(dest):
    for src in LUX_ROOT.rglob("*.py"):
        rel = src.relative_to(LUX_ROOT)
        (dest / rel).write_bytes(src.read_bytes())

def unrelated(root):
    (root / "note.txt").write_text("hello")
'''
    assert not [h for h in _lux_write_offenders(Path("fake.py"), safe)
                if "note.txt" in h or "root" in h.split(":")[1][:12]], \
        "a helper writing under its own parameter must not be flagged"


#: Every mutating shape the guard must catch, written as it would really appear.
#: One entry per verb family; the guard is only as good as this list.
_UNSAFE_SHAPES = {
    "write_text": 'LUX_ROOT.joinpath("a.py").write_text("x")',
    "write_bytes": 'target = LUX_ROOT / "a.py"\n    target.write_bytes(b"x")',
    "open_w": 'open(LUX_ROOT / "a.py", "w")',
    "open_a": 'open(LUX_ROOT / "a.py", "a")',
    "open_x": 'open(LUX_ROOT / "a.py", "x")',
    "open_plus": 'open(LUX_ROOT / "a.py", "r+")',
    "open_kw": 'open(LUX_ROOT / "a.py", mode="w")',
    "path_open": '(LUX_ROOT / "a.py").open("w")',
    "unlink": '(LUX_ROOT / "a.py").unlink()',
    "rename": '(LUX_ROOT / "a.py").rename("b.py")',
    "replace": '(LUX_ROOT / "a.py").replace("b.py")',
    "touch": '(LUX_ROOT / "a.py").touch()',
    "chmod": '(LUX_ROOT / "a.py").chmod(0o644)',
    "mkdir": '(LUX_ROOT / "sub").mkdir()',
    "os_remove": 'os.remove(LUX_ROOT / "a.py")',
    "os_unlink": 'os.unlink(LUX_ROOT / "a.py")',
    "os_rename": 'os.rename(LUX_ROOT / "a.py", "b")',
    "os_replace": 'os.replace("b", LUX_ROOT / "a.py") if False else os.replace(LUX_ROOT / "a.py", "b")',
    "os_makedirs": 'os.makedirs(LUX_ROOT / "sub")',
    "os_utime": 'os.utime(LUX_ROOT / "a.py")',
    "shutil_copy": 'shutil.copy("src.py", LUX_ROOT / "a.py")',
    "shutil_copy2": 'shutil.copy2("src.py", LUX_ROOT / "a.py")',
    "shutil_copyfile": 'shutil.copyfile("src.py", LUX_ROOT / "a.py")',
    "shutil_copytree": 'shutil.copytree("src", LUX_ROOT / "sub")',
    "shutil_copy_kw": 'shutil.copy("src.py", dst=LUX_ROOT / "a.py")',
    "shutil_move": 'shutil.move("src.py", LUX_ROOT / "a.py")',
    "shutil_rmtree": 'shutil.rmtree(LUX_ROOT / "sub")',
    "split_lines": ('root = LUX_ROOT\n    sub = root / "strategy_core"\n'
                    '    f = sub / "execution.py"\n    f.write_bytes(b"x")'),
    "via_resolve": '(LUX_ROOT.resolve() / "a.py").write_text("x")',
    "lowercase_alias": 'lux = LUX_ROOT\n    (lux / "a.py").write_text("x")',
}


@pytest.mark.parametrize("name,body", sorted(_UNSAFE_SHAPES.items()))
def test_the_guard_catches_every_mutating_shape(name, body):
    """The guard is a list of verbs, and a list is only as good as its coverage.

    Each of these is a way to modify the live engine tree. A `read` of the tree
    must NOT appear here — building a mirror requires reading it.
    """
    src = ('import os, shutil\nfrom pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n\n'
           f'def f():\n    {body}\n')
    hits = _lux_write_offenders(Path("fake.py"), src)
    assert hits, f"{name}: the guard does not catch this"


@pytest.mark.parametrize("body", [
    '(LUX_ROOT / "a.py").read_bytes()',
    '(LUX_ROOT / "a.py").read_text()',
    'open(LUX_ROOT / "a.py")',
    'open(LUX_ROOT / "a.py", "r")',
    'open(LUX_ROOT / "a.py", "rb")',
    '(LUX_ROOT / "a.py").open("rb")',
    'shutil.copy(LUX_ROOT / "a.py", dest / "a.py")',
    'shutil.copytree(LUX_ROOT, dest)',
    'list(LUX_ROOT.rglob("*.py"))',
])
def test_the_guard_permits_reading_the_live_tree(body):
    """Reading is how a mirror gets built. A guard that flagged reads would push
    someone toward mutating the original instead — the opposite of the goal."""
    src = ('import os, shutil\nfrom pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n\n'
           f'def f(dest):\n    {body}\n')
    assert not _lux_write_offenders(Path("fake.py"), src), body


def test_an_audited_marker_can_never_excuse_a_live_tree_write():
    """THE rule the marker exists under.

    A marker may excuse writing a TREE-DERIVED relative path into a tmp_path
    mirror. It may never excuse a write ANCHORED at the live tree, however
    conscientious the comment — and "it restores the bytes afterwards" is
    exactly the reasoning that would otherwise be written there.
    """
    src = ('from pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n\n'
           f'{LUX_WRITE_MARKER} restores the exact bytes in a finally block\n'
           'def probe():\n'
           '    t = LUX_ROOT / "strategy_core" / "execution.py"\n'
           '    original = t.read_bytes()\n'
           '    t.write_bytes(original + b"probe")\n'
           '    t.write_bytes(original)\n')
    hits = _lux_write_offenders(Path("fake.py"), src, with_meta=True)
    assert hits, "the guard missed a live-tree write"
    assert all(h["root"] for h in hits), (
        "a write anchored at LUX_ROOT must be graded ROOT-ANCHORED, which is "
        "the grade no marker can excuse")
    assert "probe" in _audited_functions(src)     # the marker IS seen…
    # …and the test's own filter still reports it, because `root` is True.


def test_the_audited_marker_is_scoped_to_one_function():
    """It used to be file-scoped, so one audited helper silenced the module."""
    src = ('from pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n\n'
           f'{LUX_WRITE_MARKER} writes only into `dest`, a tmp_path\n'
           'def mirror(dest):\n'
           '    for s in LUX_ROOT.rglob("*.py"):\n'
           '        rel = s.relative_to(LUX_ROOT)\n'
           '        (dest / rel).write_bytes(s.read_bytes())\n'
           '\n'
           'def sneaky():\n'
           '    (LUX_ROOT / "a.py").write_text("x")\n')
    audited = _audited_functions(src)
    assert audited == {"mirror"}, audited
    hits = _lux_write_offenders(Path("fake.py"), src, with_meta=True)
    unexcused = [h for h in hits if h["root"] or h["func"] not in audited]
    assert [h["func"] for h in unexcused] == ["sneaky"], hits


#: The original defect, verbatim. Used by two tests below.
_ORIGINAL_DEFECT = '''
def test_blind_spot():
    target = LUX_ROOT / "strategy_core" / "execution.py"
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"probe")
    finally:
        target.write_bytes(original)
'''


def test_a_line_local_scan_would_miss_the_defect():
    """WHY the guard parses instead of matching strings.

    The suite-local guard used to look for a write verb and the token `LUX_ROOT`
    ON THE SAME LINE. In the defect it existed to prevent, the taint and the
    writes are five lines apart and NEITHER write line contains `LUX_ROOT` — so
    that scan scores zero. Measured here rather than asserted, because it is the
    whole reason both guards now share one AST implementation.
    """
    write_markers = (".write_bytes(", ".write_text(", ".unlink(", ".rename(",
                     ".rmdir(", "shutil.rmtree(", "open(", ".touch(")
    line_local = [l for l in _ORIGINAL_DEFECT.splitlines()
                  if "LUX_ROOT" in l and any(m in l for m in write_markers)]
    assert line_local == [], (
        "premise moved: a line-local scan now sees the defect, so this test no "
        "longer explains why the AST guard exists")

    src = ('from pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n' + _ORIGINAL_DEFECT)
    hits = _lux_write_offenders(Path("fake.py"), src, with_meta=True)
    assert len(hits) == 2, hits
    assert all(h["root"] for h in hits), "both writes are ROOT-ANCHORED"


def test_a_mirror_write_is_graded_tree_derived_not_root_anchored():
    """`dest / rel` is anchored at `dest`. That distinction is what lets a
    legitimate mirror be excused while a live-tree write cannot be."""
    src = ('from pathlib import Path\n'
           'LUX_ROOT = Path("/x/Lux-OB-Backtester")\n\n'
           'def mirror(dest):\n'
           '    for s in LUX_ROOT.rglob("*.py"):\n'
           '        rel = s.relative_to(LUX_ROOT)\n'
           '        (dest / rel).write_bytes(s.read_bytes())\n')
    hits = _lux_write_offenders(Path("fake.py"), src, with_meta=True)
    assert hits, "a tree-derived write should still be REPORTED, just excusable"
    assert not any(h["root"] for h in hits)


def test_nothing_in_this_repository_writes_into_lux_root():
    """LUX_ROOT is the LIVE trading engine. Nothing here may write to it.

    This guard used to scan only `tools/oracle/*.py`, and only for a write verb
    and the lowercase token `lux_root` ON THE SAME LINE. It missed a real
    violation for months: `test_engine_identity.py` bound `LUX_ROOT / ... ` to a
    variable on one line and called `write_bytes` on it six lines later, in a
    directory the scan never looked at. That test corrupted the live engine's
    source on every suite run — restoring the bytes, but restamping the mtime and
    opening a window in which the trading node would have refused to start.

    So: scan the WHOLE repository, decouple the verb from the reference, and
    require any file that does both to carry an explicit audited-marker
    explaining why it is safe. A file that needs to write to a COPY of the tree
    is legitimate — but it has to say so out loud.
    """
    load_engine(require_pin=True)             # the pin must hold to begin with
    offenders = []
    scanned = 0
    for py in sorted(CT_ROOT.rglob("*.py")):
        if set(py.parts) & {".venv", "__pycache__", "node_modules",
                            "site-packages", "build", "dist"}:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        if not any(t in src for t in _LUX_REFS):
            continue
        scanned += 1
        audited = _audited_functions(src)
        for hit in _lux_write_offenders(py, src, with_meta=True):
            # A ROOT-anchored write can NEVER be excused. The marker exists for
            # a mirror whose destination is a tmp_path; it is not a licence to
            # write to the live engine, and treating it as one is precisely how
            # a "restores the bytes afterwards" test justified itself.
            if not hit["root"] and hit["func"] in audited:
                continue
            grade = "ROOT-ANCHORED" if hit["root"] else "tree-derived"
            offenders.append(f"{py.relative_to(CT_ROOT)}  line {hit['line']}: "
                             f"{hit['text']}  [{grade}, fn {hit['func']}]")

    assert scanned >= 5, (
        f"only {scanned} files reference the engine tree — the scan is probably "
        "not looking where it thinks it is")
    assert not offenders, (
        "write(s) target a path derived from the LIVE engine tree. If the target "
        "is a COPY, add a `" + LUX_WRITE_MARKER + " <reason>` comment to the "
        "file:\n  " + "\n  ".join(offenders))


def test_engine_pin_still_verifies_after_s1():
    engine = load_engine(require_pin=True)
    assert engine.pin_verified is True
    assert engine.manifest["file_count"] == 30


def test_pin_check_is_not_confused_by_modules_with_relative_paths():
    """Regression: `verify_loaded_modules` resolves each loaded module's
    `__file__` and asks whether it lives under LUX_ROOT. A module whose
    `__file__` is RELATIVE resolves against the CURRENT WORKING DIRECTORY, so
    running the check while chdir'd into LUX_ROOT made such modules resolve to
    `LUX_ROOT/<name>` and be reported as ungoverned Lux modules.

    That produced a spurious "engine module provenance" refusal whose likelihood
    depended on what else the process had imported — intermittent under
    pytest-xdist, and capable of falsely blocking a real build. The verification
    now runs outside the chdir.
    """
    import sys as _sys
    import types
    sentinel = types.ModuleType("oracle_relpath_probe")
    sentinel.__file__ = "relative_module.py"          # deliberately relative
    _sys.modules["oracle_relpath_probe"] = sentinel
    try:
        engine = load_engine(require_pin=True)
        assert engine.pin_verified is True
    finally:
        _sys.modules.pop("oracle_relpath_probe", None)


def test_pin_check_still_catches_a_genuinely_ungoverned_lux_module():
    """The fix above must not weaken the real property it protects."""
    import importlib.util as iu
    import sys as _sys
    from tools.oracle.engine_access import EngineAccessError

    engine = load_engine(require_pin=True)
    probe = engine.lux_root / "src" / "retest_tracker_test.py"  # excluded by design
    if not probe.is_file():
        pytest.skip("no ungoverned Lux file available to probe with")
    spec = iu.spec_from_file_location("lux_ungoverned_probe", probe)
    mod = iu.module_from_spec(spec)
    mod.__file__ = str(probe)
    _sys.modules["lux_ungoverned_probe"] = mod
    try:
        with pytest.raises(EngineAccessError, match="engine module provenance"):
            load_engine(require_pin=True)
    finally:
        _sys.modules.pop("lux_ungoverned_probe", None)
