"""Measure how far the charted feed sits from the production dataset.

    python -m tools.oracle.measure_feed --fixture F-TV-S1S4 --export <tv.csv> \
        --feed "OANDA:EURUSD" --write

FEED PARITY IS NOT A CODE PROPERTY. It is a question about DATA, so it is
answered by comparing raw OHLC — never by comparing oracle outputs, which would
confound the feed with the algorithm.

The output is the evidence behind a `FEED_DIFFERENT` record. Without it the
status is an assertion; with it a reader can see the size and the SHAPE of the
difference, and the shape is what identifies the cause: a near-constant positive
offset on all four fields with unchanged bar RANGES is a quoting-basis
difference (bid vs mid), not a data error, and no correction reconciles it
because the spread varies.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

from tools.oracle.compare_stages import (FIXTURE_DIR, CompareError, _load_export,
                                         _norm, _parse_tv_time)
from tools.oracle.engine_access import CT_ROOT
from tools.oracle.export_trace import build_trace

DEFAULT_OUT = CT_ROOT / "artifacts" / "tradingview_oracle" / "feed_measurement.json"

#: Half a pip on EURUSD. Below this the two feeds are the same quote to the
#: precision either side publishes; above it they are different series.
SAME_QUOTE_TOL = 5e-6


def _infer_basis(mean_signed: float) -> tuple[str, str]:
    """Name the quoting bases from the SIGN of the difference.

    A feed quoting mid sits half a spread above one quoting bid. This is an
    inference from the measurement, not a fact read from either provider, so it
    is labelled as such in the output and both sides get 'unknown' when the
    offset is too small to distinguish.
    """
    if abs(mean_signed) < SAME_QUOTE_TOL:
        return "unknown", "unknown"
    return ("bid", "mid") if mean_signed > 0 else ("mid", "bid")


def measure(fixture: str, export_path: Path, timeframe: str = "15min",
            feed: str = "", production_feed: str = "") -> dict:
    csv_path = FIXTURE_DIR / f"{fixture}.csv"
    if not csv_path.is_file():
        raise CompareError(f"unknown fixture {fixture}")
    rows, lookup, time_col, export_sha = _load_export(export_path)
    missing = [c for c in ("open", "high", "low", "close") if c not in lookup]
    if missing:
        raise CompareError(f"export lacks raw OHLC columns {missing}")

    trace = build_trace(symbol="EURUSD", timeframe=timeframe,
                        input_path=csv_path, fixture_id=fixture)
    py = {b["bar_timestamp"][:16]: b for b in trace["bars"]}
    tv = {}
    for r in rows:
        dt = _parse_tv_time(r[time_col])
        if dt is not None:
            tv[dt.strftime("%Y-%m-%d %H:%M")] = r
    shared = sorted(set(py) & set(tv))
    if not shared:
        raise CompareError("no shared bars — nothing to measure")

    fields = {}
    means = []
    for f in ("open", "high", "low", "close"):
        diffs = []
        for ts in shared:
            got = tv[ts].get(lookup[f])
            if got in (None, "", "NaN"):
                continue
            diffs.append(float(got) - py[ts]["ohlcv"][f])
        if not diffs:
            continue
        means.append(statistics.mean(diffs))
        fields[f] = {
            "bit_exact_fraction": sum(1 for d in diffs if d == 0.0) / len(diffs),
            "median_signed_diff": statistics.median(diffs),
            "mean_signed_diff": statistics.mean(diffs),
            "max_abs_diff": max(abs(d) for d in diffs),
        }

    overall = statistics.mean(means) if means else 0.0
    prod_basis, tv_basis = _infer_basis(overall)
    # Bar RANGE is the discriminator: a quoting-basis difference shifts the whole
    # bar and leaves its height alone; a different data source would not.
    py_range = statistics.mean(py[ts]["ohlcv"]["high"] - py[ts]["ohlcv"]["low"]
                               for ts in shared)
    tv_range = statistics.mean(float(tv[ts][lookup["high"]])
                               - float(tv[ts][lookup["low"]]) for ts in shared)

    same = all(v["max_abs_diff"] <= SAME_QUOTE_TOL for v in fields.values())
    return {
        "status": "FEED_MATCHED" if same else "FEED_DIFFERENT",
        "production_feed": production_feed or "LUX_ROOT/data/candles/"
                                              "EURUSD_1m_extended_2015_2026.csv",
        "tradingview_feed": feed,
        "production_quote_basis": prod_basis,
        "tradingview_quote_basis": tv_basis,
        "window": [shared[0], shared[-1]],
        "shared_bars": len(shared),
        "python_only_bars": len(set(py) - set(tv)),
        "tradingview_only_bars": len(set(tv) - set(py)),
        "fields": fields,
        "export_sha256": export_sha,
        "note": (
            f"mean signed difference {overall:+.3e} "
            f"({overall * 1e4:+.3f} pip); mean bar range "
            f"{py_range * 1e4:.3f} pip production vs {tv_range * 1e4:.3f} pip "
            f"chart (ratio {tv_range / py_range:.4f}). A shift with an unchanged "
            "range is a quoting-basis difference, not a different market; the "
            "quote bases above are INFERRED from the sign, not read from either "
            "provider."),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--fixture", required=True)
    ap.add_argument("--export", type=Path, required=True)
    ap.add_argument("--timeframe", default="15min")
    ap.add_argument("--feed", default="")
    ap.add_argument("--production-feed", default="")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        m = measure(args.fixture, args.export, args.timeframe, args.feed,
                    args.production_feed)
    except CompareError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    print(f"feed     : {m['tradingview_feed'] or '(not recorded)'}")
    print(f"window   : {m['window'][0]} .. {m['window'][1]}  "
          f"({m['shared_bars']} shared bars)")
    for f, v in m["fields"].items():
        print(f"  {f:6s} bit-exact {v['bit_exact_fraction']:6.1%}  "
              f"median {v['median_signed_diff']:+.2e}  "
              f"mean {v['mean_signed_diff']:+.2e}  "
              f"max {v['max_abs_diff']:.2e}")
    print(f"\nquote basis: production {m['production_quote_basis'].upper()} / "
          f"chart {m['tradingview_quote_basis'].upper()}  (inferred)")
    print(f"RESULT: {m['status']}")
    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n",
                            encoding="utf-8")
        print(f"written: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
