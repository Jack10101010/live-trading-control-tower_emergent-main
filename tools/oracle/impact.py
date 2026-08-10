"""Which Pine modules, fixtures and stages does a Python change invalidate?

    python -m tools.oracle.impact --since <git-ref>      # diff the engine repo
    python -m tools.oracle.impact --files a.py b.py      # explicit file list
    python -m tools.oracle.impact --vs-contract          # vs the committed contract

Answers by CONTENT where it can and by path where it must:

  * ``--vs-contract`` (the strongest form) recomputes the governed manifest and
    compares every file digest against the one recorded in the committed
    contract. It needs no git and works even against an uncommitted change.
  * ``--since`` uses ``git diff --name-only`` in LUX_ROOT for the common
    "what did this branch change" question.

Every changed governed file is mapped through
``contracts/tradingview_oracle_impact_map.json``. A governed file with NO entry
falls to FULL_PARITY_REVALIDATION_REQUIRED — fail closed, because an unclassified
file is one nobody has thought about.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from tools.oracle.engine_access import CT_ROOT, EngineAccessError, load_engine
from tools.oracle.extract_contract import DEFAULT_OUT as CONTRACT_PATH

IMPACT_MAP_PATH = CT_ROOT / "contracts" / "tradingview_oracle_impact_map.json"

#: Ordered least -> most severe. The run's overall category is the max.
SEVERITY = [
    "NO_ORACLE_IMPACT",
    "CONFIG_ONLY_ORACLE_UPDATE",
    "GENERATED_CONSTANTS_UPDATE",
    "PINE_LIMITATION_CHANGE",
    "TRACE_SCHEMA_UPDATE",
    "ALGORITHM_PARITY_UPDATE",
    "FULL_PARITY_REVALIDATION_REQUIRED",
]


def load_map(path: Path = IMPACT_MAP_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _entries_for(path: str, impact_map: dict) -> list[dict]:
    hits = [e for e in impact_map["entries"] if path in e["production"]]
    return hits


def changed_vs_contract(contract_path: Path = CONTRACT_PATH) -> tuple[list[str], dict]:
    """Governed files whose digest differs from the committed contract."""
    if not contract_path.is_file():
        raise EngineAccessError(
            f"no committed contract at {contract_path} — run "
            "`python -m tools.oracle.extract_contract --write` first")
    contract = json.loads(contract_path.read_text(encoding="utf-8"))
    recorded = {f["path"]: f["sha256"] for f in contract["engine"]["governed_files"]}

    engine = load_engine(require_pin=False)   # must describe a MOVED tree
    actual = {f["path"]: f["sha256"] for f in engine.manifest["files"]}

    changed = sorted(set(
        [p for p in recorded if p not in actual]                       # removed
        + [p for p in actual if p not in recorded]                     # added
        + [p for p in recorded if p in actual and recorded[p] != actual[p]]
    ))
    detail = {
        "removed": sorted(p for p in recorded if p not in actual),
        "added": sorted(p for p in actual if p not in recorded),
        "modified": sorted(p for p in recorded if p in actual and recorded[p] != actual[p]),
        "contract_engine_manifest_id": contract["engine"]["engine_manifest_id"],
        "actual_engine_manifest_id": engine.engine_manifest_id,
    }
    return changed, detail


def changed_since(ref: str) -> list[str]:
    engine = load_engine(require_pin=False)
    out = subprocess.run(
        ["git", "-C", str(engine.lux_root), "diff", "--name-only", ref],
        capture_output=True, text=True, check=False)
    if out.returncode != 0:
        raise EngineAccessError(f"git diff failed in {engine.lux_root}: {out.stderr.strip()}")
    return sorted(p.strip() for p in out.stdout.splitlines() if p.strip())


def analyse(changed: list[str], impact_map: dict) -> dict:
    governed_default = impact_map["default_for_unmapped_governed_file"]["category"]
    per_file, unmapped = [], []
    for path in changed:
        hits = _entries_for(path, impact_map)
        if not hits:
            # Only governed/live files fail closed; anything else is out of scope.
            is_relevant = (path.startswith(("strategy_core/", "src/", "live/"))
                           or path in ("scripts/run_backtest.py",)
                           or path.startswith("configs/policy/"))
            if is_relevant:
                unmapped.append(path)
                per_file.append({"path": path, "category": governed_default,
                                 "entry_id": None, "reason": "no impact-map entry"})
            else:
                per_file.append({"path": path, "category": "NO_ORACLE_IMPACT",
                                 "entry_id": "out-of-scope",
                                 "reason": "outside the governed/live surface"})
            continue
        for e in hits:
            per_file.append({"path": path, "category": e["category"],
                             "entry_id": e["id"], "reason": e["surface"]})

    def agg(field):
        out = []
        for row in per_file:
            e = next((x for x in impact_map["entries"] if x["id"] == row["entry_id"]), None)
            if e:
                out.extend(e.get(field, []))
        return sorted(set(out))

    categories = {r["category"] for r in per_file} or {"NO_ORACLE_IMPACT"}
    overall = max(categories, key=lambda c: SEVERITY.index(c) if c in SEVERITY
                  else len(SEVERITY))

    return {
        "changed_file_count": len(changed),
        "overall_category": overall,
        "categories_present": sorted(categories, key=lambda c: SEVERITY.index(c)
                                     if c in SEVERITY else len(SEVERITY)),
        "per_file": per_file,
        "unmapped_governed_files": unmapped,
        "pine_modules": agg("pine_modules"),
        "trace_fields": agg("trace_fields"),
        "fixtures": agg("fixtures"),
        "stages": agg("stages"),
        "python_tests": agg("python_tests"),
        "contract_sections": agg("contract_sections"),
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--since", metavar="GITREF", help="git ref to diff LUX_ROOT against")
    g.add_argument("--files", nargs="+", help="explicit LUX-relative paths")
    g.add_argument("--vs-contract", action="store_true",
                   help="compare governed digests against the committed contract")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    try:
        detail = {}
        if args.vs_contract:
            changed, detail = changed_vs_contract()
        elif args.since:
            changed = changed_since(args.since)
        else:
            changed = sorted(args.files)
    except EngineAccessError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 2

    result = analyse(changed, load_map())
    result["source_detail"] = detail

    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
    else:
        print(f"ORACLE IMPACT: {result['overall_category']}")
        print(f"  changed files : {result['changed_file_count']}")
        if not changed:
            print("\n  no governed change detected — the oracle is unaffected.")
            return 0
        for row in result["per_file"]:
            tag = row["entry_id"] or "UNMAPPED"
            print(f"    {row['category']:<36} {row['path']}  [{tag}]")
        if result["unmapped_governed_files"]:
            print("\n  !! UNMAPPED governed files (failed closed to full revalidation):")
            for p in result["unmapped_governed_files"]:
                print(f"       {p}  -> add an entry to contracts/tradingview_oracle_impact_map.json")
        print(f"\n  pine modules  : {', '.join(result['pine_modules']) or '-'}")
        print(f"  trace fields  : {', '.join(result['trace_fields']) or '-'}")
        print(f"  fixtures      : {', '.join(result['fixtures']) or '-'}")
        print(f"  stages        : {', '.join(result['stages']) or '-'}")
        print(f"  python tests  : {', '.join(result['python_tests']) or '-'}")

    return 0 if result["overall_category"] == "NO_ORACLE_IMPACT" else 1


if __name__ == "__main__":
    sys.exit(main())
