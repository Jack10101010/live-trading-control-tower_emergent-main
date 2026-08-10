"""Build the S1 golden fixtures from REAL production candles.

    python -m tools.oracle.build_fixtures [--write]

Every fixture is a slice of the pinned frozen dataset
(``data/candles/EURUSD_1m_extended_2015_2026.csv``) — the same file the live
runner assembles from — except the three deliberately-malformed ones, which are
derived from a real slice by a documented mutation so the defect is the only
difference.

Read-only with respect to ``LUX_ROOT``: the source CSV is read, never written.
Fixtures land in ``golden/tradingview_oracle/s1/`` inside the Control Tower repo.

WHY THESE WINDOWS
-----------------
Session windows are UTC hour-of-day with boundaries at 00/07/10/12/15/17, so a
single full UTC day exercises every opening AND closing boundary, the fallback
(Outside), and the midnight rollover. The DST fixtures target the dates the
project MEASURED for this broker (US calendar, not EU — LiveConfig comments), to
prove sessions do NOT move on either date. The remaining fixtures target the
handling production actually has rather than the handling one might assume.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import CT_ROOT, load_engine

FIXTURE_DIR = CT_ROOT / "golden" / "tradingview_oracle" / "s1"

#: (id, description, start_utc, end_utc_exclusive, kind)
#: `kind` drives post-processing: "slice" is verbatim; the others mutate.
FIXTURES = [
    ("F-S1-DAY", "One full UTC weekday: every session opening and closing boundary, "
                 "the Outside fallback, and the midnight rollover.",
     "2026-01-05 00:00:00", "2026-01-06 00:00:00", "slice"),

    ("F-S1-BOUNDARIES", "The minute before, at, and after every session boundary "
                        "(07/10/12/15/17) plus 00:00 — the inclusivity proof.",
     "2026-01-05 00:00:00", "2026-01-06 00:00:00", "boundary_minutes"),

    ("F-S1-MIDNIGHT", "UTC midnight rollover across two days: day-key change and the "
                      "Outside -> Asia transition at 00:00.",
     "2026-01-05 23:00:00", "2026-01-06 01:00:00", "slice"),

    ("F-S1-WEEKEND", "Friday close through Monday open. Production has NO weekend "
                     "logic — the gap is simply absent bars, and the first Monday bar "
                     "is a gap_resync transition.",
     "2026-01-09 16:00:00", "2026-01-12 08:00:00", "slice"),

    ("F-S1-DST-US", "US DST start 2026-03-08 (the date this broker's server clock "
                    "MEASURABLY shifts). Sessions must NOT move: they are pure UTC.",
     "2026-03-07 22:00:00", "2026-03-09 02:00:00", "slice"),

    ("F-S1-DST-EU", "EU DST start 2026-03-29 — the date an IANA European zone WOULD "
                    "shift. Sessions must not move here either.",
     "2026-03-28 22:00:00", "2026-03-30 02:00:00", "slice"),

    # DST-END dates are in 2025, not 2026: the frozen dataset ends 2026-06-19, so
    # the autumn 2026 transitions have no candles. Using the most recent in-range
    # transitions keeps these fixtures real rather than synthetic.
    ("F-S1-DST-END-US", "US DST end 2025-11-02 — the repeated local hour. Pure-UTC "
                        "assignment means no UTC hour repeats in the trace and no "
                        "session boundary moves.",
     "2025-11-01 22:00:00", "2025-11-03 02:00:00", "slice"),

    ("F-S1-DST-END-EU", "EU DST end 2025-10-26 — the date an IANA European zone "
                        "would shift back. Sessions must not move.",
     "2025-10-25 22:00:00", "2025-10-27 02:00:00", "slice"),

    ("F-S1-GAP", "A real intra-session data gap: proves empty resample intervals are "
                 "DROPPED, so bar_index is not a time grid (limitation L-09).",
     "2026-01-05 00:00:00", "2026-01-06 00:00:00", "drop_middle"),

    ("F-S1-DUPLICATE", "A duplicated timestamp. Production SORTS but does NOT "
                       "de-duplicate — the trace must show both rows.",
     "2026-01-05 07:00:00", "2026-01-05 08:00:00", "duplicate_row"),

    ("F-S1-OUTOFORDER", "Rows shuffled out of chronological order. Production "
                        "silently re-sorts (stable); the trace must come out ordered.",
     "2026-01-05 07:00:00", "2026-01-05 08:00:00", "shuffle"),
]

#: Stage S2/S3/S4 fixtures. These need DETECTION-timeframe depth, not minutes:
#: the ATR is a 200-period RMA and a swing needs `swing_length` (50) bars of right
#: window, so a window of a few hours proves nothing. Each is sized in 15m bars
#: and cut from the same frozen dataset.
#:
#: (id, description, start_utc, end_utc_exclusive, kind, stage)
STRUCTURE_FIXTURES = [
    ("F-S2-WARMUP", "From the very first bar of the frozen dataset: the RMA seed, "
                    "the first 200 bars of ATR warm-up, and the max(index,1) "
                    "divisor quirk at index 0/1.",
     "2015-01-01 00:00:00", "2015-01-08 00:00:00", "slice", "S2"),

    ("F-S2-RMASEED", "The divergence fixture. Long enough for a first-value-seeded "
                     "RMA and an SMA-seeded ta.rma to be compared bar by bar and "
                     "shown never to converge exactly.",
     "2015-01-01 00:00:00", "2015-02-01 00:00:00", "slice", "S2"),

    ("F-S2-FLIP", "A month containing high-volatility bars that trip the "
                  "(H-L) >= 2*measure flip, so parsed_high/parsed_low swap.",
     "2020-03-01 00:00:00", "2020-04-01 00:00:00", "slice", "S2"),

    ("F-S3-SWINGS", "Three months at 15m: many confirmed swing highs and lows, "
                    "replacement of the retained swing, and the +50-bar "
                    "confirmation lag.",
     "2025-01-01 00:00:00", "2025-04-01 00:00:00", "slice", "S3"),

    ("F-S3-WARMUP", "Exactly the first bars of the dataset: production's loop "
                    "starts at index swing_length, so the first 50 detection bars "
                    "have NO structure state at all.",
     "2015-01-01 00:00:00", "2015-01-03 00:00:00", "slice", "S3"),

    ("F-S4-STRUCTURE", "Six months: BOS and CHoCH in both directions, repeated "
                       "same-direction breaks, and bias flips.",
     "2025-01-01 00:00:00", "2025-07-01 00:00:00", "slice", "S4"),

    ("F-S4-SEED", "Starts mid-history. The bias is seeded by the FIRST break in "
                  "THIS window, which is what makes a truncated chart tag BOS "
                  "where full history tags CHoCH (limitation L-04).",
     "2024-06-01 00:00:00", "2024-09-01 00:00:00", "slice", "S4"),

    ("F-S4-VOLATILE", "A high-volatility regime: rapid alternating breaks and "
                      "back-to-back structure events.",
     "2020-02-15 00:00:00", "2020-05-01 00:00:00", "slice", "S4"),
]

#: ── TradingView-reachable verification fixtures ─────────────────────────────
#:
#: The S2/S3/S4 fixtures above are cut wherever the phenomenon lives in the frozen
#: dataset, which is the right choice for testing the PYTHON side. They are not
#: all reachable on a TradingView chart: a retail plan caps EURUSD 15m history at
#: roughly nine months, so a window in early 2025 simply cannot be exported.
#:
#: These fixtures exist to be COMPARED, so they are bounded by what the chart can
#: actually load, and they start at the chart's EARLIEST BAR on purpose.
#:
#: WHY MATCHED-START MATTERS (measured, 2025-09-30 vs full history to 2026-01-01):
#:     bias disagrees for            72 bars, converging at bar 122
#:     retained swings converge at   bar 84
#:     ATR within 1e-9 from          bar ~2000 (about four weeks)
#:     parsed-price FLIP decisions differing:  3
#:
#: Those are limitations L-04 and L-03 made concrete. Starting the Python trace at
#: the same bar the chart loads removes them BY CONSTRUCTION — both sides seed the
#: bias, the swing state and the RMA identically, so equality is exact rather than
#: convergent. What it verifies is the ALGORITHM, not production's ten-year seed;
#: that distinction is stated in the operator document rather than papered over.
TV_CHART_EARLIEST = "2025-09-30 00:00:00"

#: ── S5 fixtures cut from REAL history ───────────────────────────────────────
#:
#: These edges DO occur in production, so they are sliced rather than invented.
#: The windows were located by replaying the full frozen dataset (285,790
#: detection bars) and asking where each phenomenon actually happens:
#:
#:   inverted (high-volatility origin) boxes : 20 in 11 years, ONE of them inside
#:                                             TradingView-reachable history
#:   origin ties (equal extremes)            : 78 in 11 years, 7 reachable
#:   size-gate rejections                    : ZERO in 11 years — see SYNTHETIC
#: What each real S5 slice must still contain. A truncated window has its own
#: ATR seed, and the parsed-price flip is a threshold test — so an inverted box
#: present in full history is NOT guaranteed to survive being sliced. These are
#: checked at build time rather than assumed.
S5_SLICE_EXPECTATIONS = {
    "F-S5-INVERTED": {"min_candidates": 1, "min_inverted": 1},
    "F-S5-ORIGIN-TIE": {"min_candidates": 1, "min_inverted": 0},
    "F-S5-OPPOSITE-BREAKS": {"min_candidates": 2, "min_inverted": 0,
                             "require_both_sides": True},
}

S5_FIXTURES = [
    ("F-S5-INVERTED",
     "A REAL inverted order block: the origin bar trips (H-L) >= 2*ATR, so its "
     "parsed high and low are SWAPPED and the box has top < bottom. Production "
     "does not normalise it and neither may the oracle. 2026-04-17 20:15 is the "
     "only one of the 20 in the dataset that a TradingView chart can still load.",
     "2026-04-10 00:00:00", "2026-04-19 00:00:00", "slice", "S5"),

    ("F-S5-ORIGIN-TIE",
     "The origin search finds EQUAL extremes and must take the EARLIEST bar. "
     "`search[search == search.min()].index[0]` is first-wins; a `<=` scan would "
     "silently take the last and move the whole box. Contains the real tie "
     "detected at 2025-11-25 13:15.",
     "2025-11-20 00:00:00", "2025-11-27 00:00:00", "slice", "S5"),

    ("F-S5-OPPOSITE-BREAKS",
     "The closest thing production contains to a same-bar dual candidate: a "
     "bullish and a bearish break 12 bars apart, with the bias carried between "
     "them so the second is tagged CHoCH. A TRUE same-bar pair is unreachable — "
     "see the audit; the break conditions are mutually exclusive.",
     "2026-02-08 00:00:00", "2026-02-12 00:00:00", "slice", "S5"),
]

#: ── S5 fixtures that MUST be synthetic ──────────────────────────────────────
#:
#: The 100-pip size gate has NEVER rejected an order block in the frozen dataset:
#: 2,080 blocks, widths spanning 0.80 to 38.90 pips, none within 61 pips of the
#: limit. So no slice of real history can exercise it, and the whole
#: reject / no-id-consumed / next-id-is-dense chain is unreachable from real data.
#:
#: Each entry is (id, description, scenario-callable, expectations, stage). The
#: expectations are re-checked against the production replay at build time —
#: a scenario that stops producing its edge FAILS the build rather than quietly
#: becoming a fixture that tests nothing.
SYNTHETIC_S5_FIXTURES = [
    ("F-S5-SIZE-UNDER",
     "Order block comfortably inside the 100-pip limit: accepted, appended, and "
     "assigned ob_id 1.",
     lambda sc: sc.bull_ob_scenario(99.0, 120.0),
     {"candidates": 1, "appended": 1, "rejected": 0, "ids": [1],
      "inverted": 0, "reasons": [None]}),

    ("F-S5-SIZE-AT-LIMIT",
     "The widest order block the gate ACCEPTS. An exactly-100.0-pip width is not "
     "representable — |high-low|/pip_size steps from 99.99999999999787 straight "
     "to 100.00000000000009 — so this is the closest double at or below the "
     "limit. The gate is `width > max`, STRICT, so it passes.",
     lambda sc: sc.bull_ob_scenario(
         None, 120.0, origin_prices=sc.boundary_prices(1.09300, 100.0, False)),
     {"candidates": 1, "appended": 1, "rejected": 0, "ids": [1]}),

    ("F-S5-SIZE-OVER",
     "The narrowest order block the gate REJECTS — one ULP wider than the "
     "fixture above, 2e-12 pips more. Rejected with reason size_filter, and it "
     "consumes NO ob_id.",
     lambda sc: sc.bull_ob_scenario(
         None, 120.0, origin_prices=sc.boundary_prices(1.09300, 100.0, True)),
     {"candidates": 1, "appended": 0, "rejected": 1, "ids": [None],
      "reasons": ["size_filter"]}),

    ("F-S5-ID-DENSITY",
     "A rejected candidate followed by an accepted one. The rejected block never "
     "takes an id, so the accepted block that follows is ob_id 1, not 2 — the "
     "observable consequence of production incrementing the counter only inside "
     "the accept branch. The rejection still burns its swing and still flips the "
     "bias, which is why the second cycle can happen at all.",
     lambda sc: sc.reject_then_accept_scenario(),
     {"candidates": 2, "appended": 1, "rejected": 1, "ids": [None, 1],
      "reasons": ["size_filter", None]}),
]

#: ── S6 daily-regime fixtures ────────────────────────────────────────────────
#:
#: S6 needs DAILY depth: EMA(200) is seeded at the first daily close, BBW needs
#: 20 complete days and ADX 14, so a fixture measured in hours proves nothing.
#: Each window below was chosen by replaying the full dataset and asking where
#: the phenomenon actually occurs.
S6_FIXTURES = [
    ("F-S6-WARMUP",
     "From the very first day of the frozen dataset: the day-0 null row, the "
     "EMA seed (where close == ema and pxVsEma is EXACTLY 0, which classifies "
     "BEAR because the operator is `> 0`), and the 20-day wait before BBW — and "
     "therefore any market state — becomes valid at all.",
     "2015-01-01 00:00:00", "2015-03-01 00:00:00", "slice", "S6"),

    ("F-S6-STATES",
     "A year spanning every one of the six market states, both trend sides and "
     "both volatility labels, with ADX crossing the chop level in both "
     "directions.",
     "2020-01-01 00:00:00", "2021-01-01 00:00:00", "slice", "S6"),

    ("F-S6-TRANSITIONS",
     "A dense run of market-state transitions — the COVID volatility regime — "
     "so the transition flag, the previous-state field and the one-day shift "
     "are all exercised repeatedly rather than once.",
     "2020-02-01 00:00:00", "2020-06-01 00:00:00", "slice", "S6"),
]

TV_FIXTURES = [
    ("F-TV-S1S5",
     "THE one-export verification window. Starts at the chart's earliest bar so "
     "Pine and Python seed identically, and runs to the end of the frozen "
     "dataset so a SINGLE export can verify S1-S5. Contains the Christmas "
     "one-sided feed gap (36 bars absent from OANDA), 7 origin ties, the only "
     "chart-reachable inverted box (2026-04-17), and 59 opposite-side break "
     "pairs.",
     TV_CHART_EARLIEST, "2026-06-20 00:00:00", "slice", "S1-S5"),

    ("F-TV-S1S4",
     "PRIMARY comparison window. Starts at the chart's earliest available bar so "
     "the Pine and Python seeds match exactly. Three months of 15m bars carrying "
     "session/day transitions, ATR warm-up and decay, ~97 swings and 39 structure "
     "events in both directions.",
     TV_CHART_EARLIEST, "2026-01-01 00:00:00", "slice", "S1-S4"),
]

#: Contexts that must be REFUSED. No candle data — the guard fires before any bar
#: is read, which is the behaviour being pinned.
UNSUPPORTED_CONTEXTS = [
    ("F-S1-BADSYMBOL", {"symbol": "GBPUSD", "timeframe": "15min"},
     "symbol outside the production whitelist"),
    ("F-S1-BADTF", {"symbol": "EURUSD", "timeframe": "H4"},
     "H4 does not divide 60, so a bar can straddle a session boundary"),
    ("F-S1-BADTF-D1", {"symbol": "EURUSD", "timeframe": "1440min"},
     "daily is neither the detection nor the execution timeframe"),
]


def _read_slice(pd, src: Path, start: str, end: str):
    """Stream the frozen CSV and keep only the requested UTC window."""
    rows = []
    with src.open(newline="", encoding="utf-8") as fh:
        for rec in csv.DictReader(fh):
            t = rec["time"]
            if t >= end:
                break            # file is chronological; stop early
            if t >= start:
                rows.append(rec)
    return rows


def _boundary_minutes(rows, boundaries=(0, 7, 10, 12, 15, 17)):
    """Keep only HH:59, HH:00 and HH:01 around each session boundary hour."""
    want = set()
    for h in boundaries:
        want.add(f"{(h - 1) % 24:02d}:59")
        want.add(f"{h:02d}:00")
        want.add(f"{h:02d}:01")
    return [r for r in rows if r["time"][11:16] in want]


def _drop_middle(rows):
    """Remove a contiguous run mid-window to create a genuine gap."""
    if len(rows) < 200:
        return rows
    cut_from, cut_to = len(rows) // 2, len(rows) // 2 + 90
    return rows[:cut_from] + rows[cut_to:]


def _duplicate_row(rows):
    if len(rows) < 5:
        return rows
    i = len(rows) // 2
    dup = dict(rows[i])
    dup["close"] = str(float(dup["close"]) + 0.00005)   # distinguishable payload
    return rows[:i + 1] + [dup] + rows[i + 1:]


def _shuffle(rows):
    """Deterministic reordering — swap adjacent pairs. No RNG, so the fixture is
    reproducible byte-for-byte on any host."""
    out = list(rows)
    for i in range(0, len(out) - 1, 2):
        out[i], out[i + 1] = out[i + 1], out[i]
    return out


TRANSFORMS = {
    "slice": lambda rows: rows,
    "boundary_minutes": _boundary_minutes,
    "drop_middle": _drop_middle,
    "duplicate_row": _duplicate_row,
    "shuffle": _shuffle,
}

COLUMNS = ["time", "open", "high", "low", "close", "volume"]


def _write_csv(path: Path, rows) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" + explicit \n keeps the file byte-identical across platforms.
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS, lineterminator="\n")
        w.writeheader()
        for r in rows:
            w.writerow({c: r[c] for c in COLUMNS})
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _verify_s5_slices(engine, index) -> None:
    """Refuse an S5 slice that no longer contains its edge.

    A window cut from full history does not inherit full history's ATR, and the
    parsed-price flip is a `>= 2*atr` threshold — so the very property that makes
    a box inverted can disappear when the window is truncated. Checking at build
    time turns that into a loud failure instead of a fixture that quietly stops
    testing anything.
    """
    import pandas as pd

    from tools.oracle import replay_structure as rs
    from tools.oracle.engine_access import resolve_config
    cfg, _ = resolve_config(engine)
    for fid, want in S5_SLICE_EXPECTATIONS.items():
        det = engine.rb.resample_candles(
            engine.core.prepare_candles_for_simulation(
                pd.read_csv(FIXTURE_DIR / f"{fid}.csv")),
            cfg.detection_timeframe)
        rows, _obs = rs.replay(
            engine.core, det["high"].tolist(), det["low"].tolist(),
            det["close"].tolist(), swing_length=cfg.swing_length,
            ob_filter=cfg.ob_filter, pip_size=cfg.pip_size,
            min_ob_size_pips=cfg.min_ob_size_pips,
            max_ob_size_pips=cfg.max_ob_size_pips)
        cands = [c for r in rows
                 for c in (r.get("s5") or {}).get("candidates", [])]
        inverted = sum(1 for c in cands
                       if c["proposed_top"] is not None
                       and c["proposed_top"] < c["proposed_bottom"])
        if len(cands) < want["min_candidates"]:
            raise SystemExit(
                f"{fid}: expected >= {want['min_candidates']} order-block "
                f"candidates, got {len(cands)} — widen the window")
        if inverted < want["min_inverted"]:
            raise SystemExit(
                f"{fid}: expected >= {want['min_inverted']} INVERTED box(es), "
                f"got {inverted}. The slice no longer seeds the ATR high enough "
                "for the parsed-price flip to fire.")
        if want.get("require_both_sides") and                 len({c["side"] for c in cands}) < 2:
            raise SystemExit(
                f"{fid}: expected both a bullish and a bearish candidate, got "
                f"{sorted({c['side'] for c in cands})}")


def build(write: bool = False) -> dict:
    import pandas as pd
    engine = load_engine(require_pin=True)
    src = engine.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv"
    if not src.is_file():
        raise SystemExit(f"frozen dataset not found: {src}")

    index = {"fixtures": [], "unsupported_contexts": []}
    all_specs = ([(f + ("S1",)) for f in FIXTURES] + STRUCTURE_FIXTURES
                 + S5_FIXTURES + S6_FIXTURES + TV_FIXTURES)
    for fid, desc, start, end, kind, stage in all_specs:
        rows = _read_slice(pd, src, start, end)
        rows = TRANSFORMS[kind](rows)
        entry = {
            "fixture_id": fid, "description": desc, "kind": kind, "stage": stage,
            "window_start_utc": start, "window_end_utc_exclusive": end,
            "rows": len(rows),
            "detection_bars_est": len(rows) // 15,
            "path": f"golden/tradingview_oracle/s1/{fid}.csv",
        }
        if not rows:
            entry["status"] = "EMPTY — no candles in this window"
        elif write:
            entry["sha256"] = _write_csv(FIXTURE_DIR / f"{fid}.csv", rows)
            entry["status"] = "written"
        else:
            entry["status"] = "dry-run"
        index["fixtures"].append(entry)

    # ── synthetic S5 fixtures ────────────────────────────────────────────────
    # Built from constructed candles, then VERIFIED against the production replay.
    # A scenario that no longer produces its edge raises rather than silently
    # becoming a fixture that asserts nothing.
    import datetime as _dt

    from tools.oracle import s5_scenarios as sc
    for fid, desc, make, expect in SYNTHETIC_S5_FIXTURES:
        bars, _meta = make(sc)
        observed, _cands = sc.verify_scenario(engine, bars, expect, fid)
        rows = sc.expand_to_m1(bars, _dt.datetime(2026, 1, 5, 0, 0))
        entry = {
            "fixture_id": fid, "description": desc, "kind": "synthetic",
            "stage": "S5",
            "window_start_utc": rows[0]["time"],
            "window_end_utc_exclusive": rows[-1]["time"],
            "rows": len(rows), "detection_bars_est": len(bars),
            "path": f"golden/tradingview_oracle/s1/{fid}.csv",
            "synthetic_expectations": expect,
            "synthetic_observed": observed,
        }
        if write:
            entry["sha256"] = _write_csv(FIXTURE_DIR / f"{fid}.csv", rows)
            entry["status"] = "written"
        else:
            entry["status"] = "dry-run"
        index["fixtures"].append(entry)

    # Real S5 slices must still exhibit what they were cut for.
    if write:
        _verify_s5_slices(engine, index)

    for fid, ctx, why in UNSUPPORTED_CONTEXTS:
        index["unsupported_contexts"].append({
            "fixture_id": fid, "context": ctx, "expected": "UNSUPPORTED_DATA_CONTEXT",
            "reason": why,
            "note": "no candle file — the context guard must fire before any bar is read",
        })

    index["source"] = {
        "frozen_dataset": "data/candles/EURUSD_1m_extended_2015_2026.csv",
        "lux_root_basename": engine.lux_root.name,
        "engine_manifest_id": engine.engine_manifest_id,
    }
    return index


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    index = build(write=args.write)
    for f in index["fixtures"]:
        flag = "" if f["rows"] else "   <-- EMPTY"
        print(f"  {f['fixture_id']:<20} {f['rows']:>6} rows  {f['status']}{flag}")
    for u in index["unsupported_contexts"]:
        print(f"  {u['fixture_id']:<20} {'':>6}       {u['expected']}")

    if args.write:
        (FIXTURE_DIR / "INDEX.json").parent.mkdir(parents=True, exist_ok=True)
        (FIXTURE_DIR / "INDEX.json").write_text(
            json.dumps(index, indent=1, sort_keys=True) + "\n", encoding="utf-8")
        print(f"\nwritten: golden/tradingview_oracle/s1/ "
              f"({len([f for f in index['fixtures'] if f['rows']])} fixtures + INDEX.json)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
