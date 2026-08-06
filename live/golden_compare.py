"""M-GOLDEN-CONTRACT-1 — the two Golden comparisons, implemented once.

Two modes, deliberately asymmetric, defined by ``live/golden_contract.json``:

``same-host``
    The DEPLOYMENT GATE. Candidate vs production output produced on the SAME
    host: full byte equality first, then a complete field census anyway (so a
    byte match is corroborated and a byte mismatch is localised). No tolerance
    of any kind. This is what promotes a candidate.

``archive``
    The cross-machine regression WITNESS. Candidate output vs the Mac-produced
    archive reference: complete field census under the contract's three-tier
    policy — diagnostic-only fields (bbw_value) are enumerated in full but
    never reject; bounded-allowlist fields (the remaining write-only regime
    diagnostics) tolerate float noise up to max_ulp; everything else is
    zero-tolerance. A non-float change in ANY field, including a diagnostic
    one, is forbidden — the concession is to summation order, not to meaning.
    This never promotes; it can only add confidence or raise an alarm.

Exit codes: 0 pass, 1 comparison failed, 2 usage/contract error.

The full census is written as JSON next to stdout so a first-divergence report
can never again masquerade as a complete account (the S_22 lesson: rehearsal's
first-divergence output told us WHERE the archive comparison failed but not how
MUCH else differed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from pathlib import Path

import pandas as pd

CONTRACT_PATH = Path(__file__).resolve().parent / "golden_contract.json"


def load_contract() -> dict:
    try:
        return json.loads(CONTRACT_PATH.read_text())
    except (OSError, ValueError) as exc:
        print(f"CONTRACT ERROR: cannot load {CONTRACT_PATH}: {exc}")
        raise SystemExit(2)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ulp_distance(a: float, b: float) -> int:
    """Distance in representable float64 steps. inf for sign/NaN disagreements."""
    if math.isnan(a) and math.isnan(b):
        return 0
    if math.isnan(a) or math.isnan(b):
        return 2 ** 62
    ia = struct.unpack("<q", struct.pack("<d", a))[0]
    ib = struct.unpack("<q", struct.pack("<d", b))[0]
    # map negative floats onto a monotonic integer line
    if ia < 0:
        ia = -(2 ** 63) - ia - 1
    if ib < 0:
        ib = -(2 ** 63) - ib - 1
    return abs(ia - ib)


def _read(path: Path, key: str) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    if key not in df.columns:
        print(f"CONTRACT ERROR: key column '{key}' absent from {path}")
        raise SystemExit(2)
    return df


def census(ref: pd.DataFrame, cand: pd.DataFrame, key: str) -> dict:
    """Complete field-level census. Row ORDER is part of the contract: rows are
    compared positionally, and key equality per position is itself a check."""
    out: dict = {"rows_ref": len(ref), "rows_cand": len(cand),
                 "row_count_equal": len(ref) == len(cand),
                 "columns_ref": list(ref.columns), "columns_cand": list(cand.columns),
                 "columns_equal": list(ref.columns) == list(cand.columns),
                 "key_order_mismatches": [], "cell_mismatches": []}
    if not out["columns_equal"] or not out["row_count_equal"]:
        return out
    rk, ck = ref[key].tolist(), cand[key].tolist()
    out["key_order_mismatches"] = [
        {"row": i, "ref": a, "cand": b} for i, (a, b) in enumerate(zip(rk, ck)) if a != b]
    if out["key_order_mismatches"]:
        return out
    for col in ref.columns:
        ra, ca = ref[col].tolist(), cand[col].tolist()
        for i, (a, b) in enumerate(zip(ra, ca)):
            if a != b:
                rec = {"key": rk[i], "row": i, "field": col, "ref": a, "cand": b}
                try:
                    rec["ulp"] = ulp_distance(float(a), float(b))
                except (TypeError, ValueError):
                    rec["ulp"] = None
                out["cell_mismatches"].append(rec)
    return out


def run_same_host(ref: Path, cand: Path, key: str, out_json: Path) -> int:
    """Byte gate + corroborating census. ZERO tolerance."""
    report = {"mode": "same-host",
              "policy": "byte-identical on the same host; no tolerances",
              "ref": str(ref), "cand": str(cand),
              "ref_sha256": sha256_file(ref), "cand_sha256": sha256_file(cand)}
    report["byte_identical"] = report["ref_sha256"] == report["cand_sha256"]
    c = census(_read(ref, key), _read(cand, key), key)
    report["census"] = c
    structurally_equal = (c["row_count_equal"] and c["columns_equal"]
                          and not c["key_order_mismatches"] and not c["cell_mismatches"])
    report["verdict"] = "PASS" if (report["byte_identical"] and structurally_equal) else "FAIL"
    # A byte match with a census mismatch (or vice versa) is a tooling defect,
    # not a data verdict — surface it loudly rather than picking a winner.
    report["internal_consistency"] = report["byte_identical"] == structurally_equal
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=1))
    print(f"same-host: byte_identical={report['byte_identical']} "
          f"cells={len(c['cell_mismatches'])} verdict={report['verdict']}")
    if not report["internal_consistency"]:
        print("TOOLING DEFECT: byte verdict and census verdict disagree")
        return 1
    return 0 if report["verdict"] == "PASS" else 1


def run_archive(ref: Path, cand: Path, key: str, out_json: Path,
                drift_path: Path | None = None) -> int:
    """Field census under the narrow cross-machine policy."""
    contract = load_contract()
    policy = contract["comparison_policies"]["cross_machine_archive_witness"]
    allow = set(policy["allowlist_fields"])
    diagnostic_only = set(policy.get("diagnostic_only_fields", ()))
    max_ulp = int(policy["max_ulp"])
    #: ulp_distance returns 2**62 for NaN-vs-number; a genuine float pair is
    #: always far below it. Above this, "float noise" is not what happened.
    FLOAT_NOISE_CEILING = 2 ** 62
    # Known input drift: cells explained by a RECORDED dataset revision, matched
    # on ALL of key+field+ref+cand. This is provenance, not tolerance — a new
    # value in the same cell stays forbidden.
    drift_cells: set[tuple] = set()
    drift_meta = None
    if drift_path is not None:
        drift_meta = json.loads(Path(drift_path).read_text())
        drift_cells = {(e["key"], e["field"], e["ref"], e["cand"])
                       for e in drift_meta["explained_cells"]}
    c = census(_read(ref, key), _read(cand, key), key)
    permitted, forbidden, explained, diagnostic = [], [], [], []
    for rec in c["cell_mismatches"]:
        is_float_pair = rec["ulp"] is not None and rec["ulp"] < FLOAT_NOISE_CEILING
        if (rec["key"], rec["field"], rec["ref"], rec["cand"]) in drift_cells:
            explained.append(rec)
        elif rec["field"] in diagnostic_only and is_float_pair:
            # Reported in full, never rejecting: cross-machine summation-order
            # noise in a write-only diagnostic. A non-float change in the same
            # field (type/format/NaN-vs-number) falls through to forbidden.
            diagnostic.append(rec)
        elif rec["field"] in allow and is_float_pair and rec["ulp"] <= max_ulp:
            permitted.append(rec)
        else:
            forbidden.append(rec)
    ok = (c["row_count_equal"] and c["columns_equal"]
          and not c["key_order_mismatches"] and not forbidden)
    report = {"mode": "archive", "policy": policy["statement"],
              "allowlist_fields": sorted(allow), "max_ulp": max_ulp,
              "diagnostic_only_fields": sorted(diagnostic_only),
              "known_input_drift": (str(drift_path) if drift_path else None),
              "known_input_drift_cause": (drift_meta or {}).get("cause"),
              "ref": str(ref), "cand": str(cand), "census": c,
              "permitted_mismatches": permitted,
              "explained_input_drift": explained,
              "diagnostic_mismatches": diagnostic,
              "diagnostic_max_ulp_observed": max(
                  (r["ulp"] for r in diagnostic), default=0),
              "forbidden_mismatches": forbidden,
              "verdict": "PASS" if ok else "FAIL"}
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(report, indent=1))
    print(f"archive: cells={len(c['cell_mismatches'])} "
          f"permitted={len(permitted)} explained_drift={len(explained)} "
          f"diagnostic={len(diagnostic)} "
          f"forbidden={len(forbidden)} verdict={report['verdict']}")
    if diagnostic:
        print(f"  diagnostic (reported, non-rejecting): "
              f"{len(diagnostic)} cells, max ulp {report['diagnostic_max_ulp_observed']} "
              f"— full enumeration in {out_json}")
    for rec in forbidden[:10]:
        print(f"  FORBIDDEN {rec['key']} {rec['field']}: "
              f"ref={rec['ref']} cand={rec['cand']} ulp={rec['ulp']}")
    for rec in permitted[:5]:
        print(f"  permitted {rec['key']} {rec['field']}: ulp={rec['ulp']}")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--mode", choices=("same-host", "archive"), required=True)
    ap.add_argument("--ref", required=True, help="reference CSV (production output "
                    "for same-host; archived Golden reference for archive)")
    ap.add_argument("--cand", required=True, help="candidate CSV")
    ap.add_argument("--key", default="trade_id")
    ap.add_argument("--report", default=None, help="census JSON output path")
    ap.add_argument("--known-input-drift", default=None,
                    help="archive mode only: JSON ledger of exactly-matched "
                         "cells explained by a recorded input revision")
    args = ap.parse_args(argv)
    # Absolute immediately — nothing downstream may depend on cwd.
    ref, cand = Path(args.ref).resolve(), Path(args.cand).resolve()
    for p in (ref, cand):
        if not p.is_file():
            print(f"CONTRACT ERROR: not a file: {p}")
            return 2
    out_json = Path(args.report).resolve() if args.report else \
        cand.with_suffix(f".golden-compare.{args.mode}.json")
    if args.mode == "same-host":
        if args.known_input_drift:
            print("CONTRACT ERROR: --known-input-drift is archive-mode only; "
                  "the same-host gate admits no explanations")
            return 2
        return run_same_host(ref, cand, args.key, out_json)
    drift = Path(args.known_input_drift).resolve() if args.known_input_drift else None
    if drift is not None and not drift.is_file():
        print(f"CONTRACT ERROR: known-input-drift ledger missing: {drift}")
        return 2
    return run_archive(ref, cand, args.key, out_json, drift)


if __name__ == "__main__":
    raise SystemExit(main())
