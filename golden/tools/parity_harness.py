#!/usr/bin/env python3
"""
Golden parity harness (Milestone 2, Foundation Task 4).

Driver-side comparison tooling ONLY — never imported by strategy code, never part
of strategy_core. Compares two Lux run directories (A = reference, B = candidate):

  1. primary Triggered-Edge trades artifact   (keyed, byte-first)
  2. order-block artifact                     (keyed, byte-first)
  3. selected stable summary fields           (JSON, with explicit ignore list)

Byte comparison is attempted first; on difference it falls back to keyed
field-level comparison and classifies every discrepancy as one of:
  missing_rows / extra_rows / ordering / value_diffs / schema (column) diffs /
  ignored (explicitly volatile fields — reported, never silently dropped).
Duplicate keys are surfaced, never hidden. Exit code: 0 = parity, 1 = unexplained
differences, 2 = usage/inputs error. Originals are opened read-only; mutation
self-tests must operate on copies.

Usage:
  python3 parity_harness.py <reference_run_dir> <candidate_run_dir> [--report out.json]
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

PRIMARY_TRADES = "trades_allow_multi_position__entry_triggered_edge_25p0_d3.csv"
ORDER_BLOCKS = "order_blocks.csv"
SUMMARY = "summary.json"

# Summary fields that are legitimately volatile between two otherwise identical
# runs (paths/timestamps). Their differences are RECORDED as 'ignored', not errors.
SUMMARY_IGNORED_KEYS = {"completed_at", "run_id", "output_folder", "started_at"}

TRADE_KEY = "trade_id"
OB_KEY = "id"


def read_csv_rows(path: Path):
    with path.open(newline="") as fh:
        r = csv.DictReader(fh)
        return list(r), list(r.fieldnames or [])


def compare_csv(a_path: Path, b_path: Path, key: str) -> dict:
    out = {
        "artifact": a_path.name,
        "mode": None,
        "status": "PASS",
        "duplicate_keys": {"a": [], "b": []},
        "schema": {"missing_in_b": [], "extra_in_b": []},
        "missing_rows": [],
        "extra_rows": [],
        "ordering": None,
        "value_diffs": [],
        "rows": {"a": 0, "b": 0},
    }
    a_bytes, b_bytes = a_path.read_bytes(), b_path.read_bytes()
    if a_bytes == b_bytes:
        out["mode"] = "byte"
        rows_a, _ = read_csv_rows(a_path)
        out["rows"] = {"a": len(rows_a), "b": len(rows_a)}
        return out

    out["mode"] = "keyed"
    rows_a, cols_a = read_csv_rows(a_path)
    rows_b, cols_b = read_csv_rows(b_path)
    out["rows"] = {"a": len(rows_a), "b": len(rows_b)}

    out["schema"]["missing_in_b"] = [c for c in cols_a if c not in cols_b]
    out["schema"]["extra_in_b"] = [c for c in cols_b if c not in cols_a]
    shared_cols = [c for c in cols_a if c in cols_b]

    def keyed(rows, side):
        seen, dups, index = {}, [], {}
        for i, row in enumerate(rows):
            k = row.get(key, "")
            if k in seen:
                dups.append(k)
            seen[k] = True
            index.setdefault(k, []).append((i, row))
        if dups:
            out["duplicate_keys"][side] = sorted(set(dups))
        return index

    ia, ib = keyed(rows_a, "a"), keyed(rows_b, "b")
    keys_a, keys_b = set(ia), set(ib)
    out["missing_rows"] = sorted(keys_a - keys_b)[:200]
    out["extra_rows"] = sorted(keys_b - keys_a)[:200]

    common = keys_a & keys_b
    order_a = [r.get(key, "") for r in rows_a if r.get(key, "") in common]
    order_b = [r.get(key, "") for r in rows_b if r.get(key, "") in common]
    if order_a != order_b:
        first = next((i for i, (x, y) in enumerate(zip(order_a, order_b)) if x != y), None)
        out["ordering"] = {"differs": True, "first_divergence_index": first,
                           "a_key": order_a[first] if first is not None else None,
                           "b_key": order_b[first] if first is not None else None}

    for k in sorted(common):
        for (_, ra), (_, rb) in zip(ia[k], ib[k]):
            for c in shared_cols:
                va, vb = ra.get(c, ""), rb.get(c, "")
                if va != vb:
                    out["value_diffs"].append({"key": k, "column": c, "a": va, "b": vb})
                    if len(out["value_diffs"]) >= 500:
                        out["value_diffs_truncated"] = True
                        break
            if out.get("value_diffs_truncated"):
                break
        if out.get("value_diffs_truncated"):
            break

    unexplained = (out["missing_rows"] or out["extra_rows"] or out["value_diffs"]
                   or out["schema"]["missing_in_b"] or out["schema"]["extra_in_b"]
                   or out["duplicate_keys"]["a"] or out["duplicate_keys"]["b"])
    if unexplained:
        out["status"] = "FAIL"
    elif out["ordering"]:
        out["status"] = "ORDERING_ONLY"
    return out


def flatten(d, prefix=""):
    items = {}
    if isinstance(d, dict):
        for k, v in d.items():
            items.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(d, list):
        items[prefix[:-1]] = json.dumps(d, sort_keys=True)
    else:
        items[prefix[:-1]] = d
    return items


def compare_summary(a_path: Path, b_path: Path) -> dict:
    out = {"artifact": SUMMARY, "mode": "json", "status": "PASS",
           "value_diffs": [], "ignored": [], "missing_in_b": [], "extra_in_b": []}
    fa, fb = flatten(json.load(a_path.open())), flatten(json.load(b_path.open()))
    for k in fa:
        top = k.split(".")[0]
        if top in SUMMARY_IGNORED_KEYS:
            if fa[k] != fb.get(k):
                out["ignored"].append({"key": k, "a": fa[k], "b": fb.get(k)})
            continue
        if k not in fb:
            out["missing_in_b"].append(k)
        elif fa[k] != fb[k]:
            out["value_diffs"].append({"key": k, "a": fa[k], "b": fb[k]})
    out["extra_in_b"] = [k for k in fb if k not in fa and k.split(".")[0] not in SUMMARY_IGNORED_KEYS]
    if out["value_diffs"] or out["missing_in_b"] or out["extra_in_b"]:
        out["status"] = "FAIL"
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("reference")
    ap.add_argument("candidate")
    ap.add_argument("--report", default=None)
    args = ap.parse_args()
    ref, cand = Path(args.reference), Path(args.candidate)
    if not ref.is_dir() or not cand.is_dir():
        print("ERROR: both arguments must be run directories", file=sys.stderr)
        return 2

    results = []
    for name, key in ((PRIMARY_TRADES, TRADE_KEY), (ORDER_BLOCKS, OB_KEY)):
        pa, pb = ref / name, cand / name
        if not pa.exists() or not pb.exists():
            results.append({"artifact": name, "status": "FAIL",
                            "error": f"missing file ({'A' if not pa.exists() else 'B'})"})
            continue
        results.append(compare_csv(pa, pb, key))
    sa, sb = ref / SUMMARY, cand / SUMMARY
    if sa.exists() and sb.exists():
        results.append(compare_summary(sa, sb))
    else:
        results.append({"artifact": SUMMARY, "status": "FAIL", "error": "missing summary"})

    verdict = "PASS"
    for r in results:
        if r["status"] == "FAIL":
            verdict = "FAIL"
        elif r["status"] == "ORDERING_ONLY" and verdict == "PASS":
            verdict = "ORDERING_ONLY"
    report = {"reference": str(ref), "candidate": str(cand),
              "verdict": verdict, "artifacts": results}
    text = json.dumps(report, indent=1)
    if args.report:
        Path(args.report).write_text(text)
    print(text[:4000])
    print(f"\nVERDICT: {verdict}")
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
