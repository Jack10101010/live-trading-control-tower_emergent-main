"""Take a provenanced copy of the LIVE 1-minute extension, so the replay can
cover the same history production does.

    python -m tools.oracle.snapshot_execution_feed --from <EURUSD_1m_live.csv> --write

WHY THIS IS A SEPARATE, OPERATOR-DRIVEN STEP
--------------------------------------------
Production's frozen candle file ends 2026-06-19. The running node continues on a
live MT5 segment, so its trades extend months past that; the replay exporter,
reading the frozen file alone, could not contain them. A 2026-07-29 trade was
invisible on the chart for exactly this reason — not because of any lifecycle
rule, but because the recording's input stopped 27 days before the order block
was detected.

The live segment is owned by the running node, which holds an OS lock on its
directory. `tools/oracle/` is forbidden from naming that directory at all
(`test_tooling_never_touches_live_state_or_the_broker` walks the AST for the
literal), and that guard is worth keeping: nothing in the oracle toolchain
should have a hard-coded route into the node's state.

So the path is supplied by the operator on the command line and the file is
copied HERE, into an oracle-owned artefact with a digest and a span. Everything
downstream reads the artefact. The read is also made safe against the node
appending mid-read: the final row is dropped, because it may be half-written.

WHAT THIS IS NOT
----------------
A merge. The snapshot is the live segment verbatim; the seam is applied at
export time using production's own rule (frozen rows win at or before the frozen
end, live rows win after), and the resulting recording DECLARES the seam so the
chart never implies one continuous feed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle.engine_access import CT_ROOT

DEFAULT_OUT = (CT_ROOT / "artifacts" / "tradingview_oracle"
               / "execution_feed_extension.csv")
META_OUT = DEFAULT_OUT.with_suffix(".json")

REQUIRED = ("time", "open", "high", "low", "close")


class SnapshotError(RuntimeError):
    pass


def build(source: Path):
    """Return (frame, meta). Never writes."""
    import pandas as pd

    if not source.is_file():
        raise SnapshotError(f"no such file: {source}")
    frame = pd.read_csv(source)
    missing = set(REQUIRED) - set(frame.columns)
    if missing:
        raise SnapshotError(f"{source.name} is missing {sorted(missing)}")

    # THE LAST ROW MAY BE HALF-WRITTEN. The node appends to this file while it
    # trades, so the tail is the one row that cannot be trusted to be complete.
    frame = frame.iloc[:-1].copy()
    frame["time"] = pd.to_datetime(frame["time"], utc=True)

    if not frame["time"].is_monotonic_increasing:
        raise SnapshotError(
            "timestamps are not ascending — refusing to snapshot a feed that "
            "may have been read mid-write")
    dupes = int(frame["time"].duplicated().sum())
    if dupes:
        raise SnapshotError(f"{dupes} duplicate timestamps in {source.name}")
    if frame.empty:
        raise SnapshotError("no usable rows after dropping the trailing row")

    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    meta = {
        "schema": "tradingview-oracle-execution-feed-v1",
        "source_name": source.name,
        "source_sha256": digest,
        "rows": int(len(frame)),
        "first": str(frame["time"].min()),
        "last": str(frame["time"].max()),
        "dropped_trailing_row": True,
        "_note": "The LIVE MT5 1-minute segment. It is NOT the frozen "
                 "historical source; anything recorded from it must say so.",
    }
    return frame, meta


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="source", required=True, type=Path,
                    help="the live 1-minute CSV the node maintains")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        frame, meta = build(args.source)
    except SnapshotError as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    print(f"source   : {meta['source_name']}  sha {meta['source_sha256'][:16]}")
    print(f"rows     : {meta['rows']}  (trailing row dropped)")
    print(f"span     : {meta['first']} .. {meta['last']}")

    if args.write:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        frame.to_csv(args.out, index=False)
        args.out.with_suffix(".json").write_text(
            json.dumps(meta, indent=1) + "\n", encoding="utf-8")
        print(f"\nwritten  : {args.out.relative_to(CT_ROOT)}")
        print("now run  : python -m tools.oracle.export_replay --seam --write")
    else:
        print("\nDRY RUN — nothing written. Re-run with --write.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
