"""Is the released Pine artefact still the currently deployed Python engine?

    python -m tools.oracle.check_freshness [--json] [--manifest PATH]

Answers deterministically, by CONTENT. Modification timestamps are never
consulted — they are wrong the moment anyone touches a checkout, and they cannot
survive a clone.

STATUSES (exit code in brackets)
    CURRENT                [0]  released build matches the deployed engine exactly
    UNVERIFIED             [1]  no manifest, or it was never validated
    PARTIAL                [0]  matches, but not every stage is implemented yet
    STALE_ENGINE           [2]  governed engine content moved
    STALE_CONFIG           [3]  resolved configuration or policy moved
    STALE_CONTRACT         [4]  committed contract != freshly extracted contract
    STALE_TRACE_SCHEMA     [5]  trace schema version moved — fixtures invalid
    INCOMPATIBLE           [6]  an unsupported production feature is now ACTIVE,
                                or a generated file was hand-edited

Precedence is deliberate and NOT alphabetical: the most fundamental divergence
wins, because fixing it may resolve the others. INCOMPATIBLE outranks everything
(the oracle would be wrong, not merely old); then trace schema (fixtures are
invalid, so nothing else can be trusted); then engine; then config; then contract.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle import fingerprint as fp
from tools.oracle import parity_status as ps
from tools.oracle.engine_access import CT_ROOT, EngineAccessError
from tools.oracle.extract_contract import (DEFAULT_OUT as CONTRACT_PATH,
                                           build_contract, canonical_json)

#: Default manifest — the DETECTION build, which is what "the oracle" meant
#: before there were two. Pass `--target` to check the other one.
from tools.oracle.generate_pine import (BUILD_TARGETS,  # noqa: E402
                                        DEFAULT_TARGET, resolve_target)

MANIFEST_PATH = BUILD_TARGETS[DEFAULT_TARGET]["manifest"]

CURRENT = "CURRENT"
PARTIAL = "PARTIAL"
UNVERIFIED = "UNVERIFIED"
STALE_ENGINE = "STALE_ENGINE"
STALE_CONFIG = "STALE_CONFIG"
STALE_CONTRACT = "STALE_CONTRACT"
STALE_TRACE_SCHEMA = "STALE_TRACE_SCHEMA"
INCOMPATIBLE = "INCOMPATIBLE"

EXIT = {CURRENT: 0, PARTIAL: 0, UNVERIFIED: 1, STALE_ENGINE: 2, STALE_CONFIG: 3,
        STALE_CONTRACT: 4, STALE_TRACE_SCHEMA: 5, INCOMPATIBLE: 6}

#: Most fundamental first. The first matching condition decides the status.
PRECEDENCE = [INCOMPATIBLE, STALE_TRACE_SCHEMA, STALE_ENGINE, STALE_CONFIG,
              STALE_CONTRACT, UNVERIFIED, PARTIAL, CURRENT]


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    # Normalise line endings so a Windows/macOS checkout of the same content
    # agrees — the same defect live/engine_identity.py already fixed for the
    # engine manifest.
    raw = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(raw).hexdigest()


def evaluate(manifest_path: Path = MANIFEST_PATH,
             contract_path: Path = CONTRACT_PATH,
             root: Path = CT_ROOT) -> dict:
    """`root` resolves the repo-relative paths recorded in the manifest.

    It exists so tamper detection can be tested HERMETICALLY, against a copied
    tree in a tmp dir, instead of mutating the real artefacts. Mutating them
    would race any test running in parallel that reads the same files — a
    flakiness source, and one that would only ever appear under load.
    """
    findings: list[dict] = []
    conditions: set[str] = set()

    def note(status, code, detail, **extra):
        conditions.add(status)
        findings.append({"status": status, "code": code, "detail": detail, **extra})

    # ── the deployed truth, extracted fresh ──────────────────────────────────
    try:
        live_contract = build_contract(require_pin=True)
    except EngineAccessError as exc:
        # A failed pin is not "stale" — it means the deployed engine is not the
        # approved one at all. Nothing downstream can be trusted.
        return {
            "status": INCOMPATIBLE, "exit_code": EXIT[INCOMPATIBLE],
            "findings": [{"status": INCOMPATIBLE, "code": "engine_pin_invalid",
                          "detail": str(exc)}],
            "live_fingerprint": None, "manifest": None,
        }

    live_fp = live_contract["fingerprint"]

    if live_contract["unsupported_active_features"]:
        note(INCOMPATIBLE, "unsupported_feature_active",
             "a production feature the oracle does not implement is ACTIVE",
             features=live_contract["unsupported_active_features"])

    # ── committed contract vs freshly extracted ──────────────────────────────
    fresh_text = canonical_json(live_contract)
    if not contract_path.is_file():
        note(UNVERIFIED, "contract_missing",
             f"no committed contract at {contract_path.name} — run "
             "`python -m tools.oracle.extract_contract --write`")
    else:
        committed_text = contract_path.read_text(encoding="utf-8")
        if _sha256_text(committed_text) != _sha256_text(fresh_text):
            try:
                committed = json.loads(committed_text)
            except ValueError:
                committed = {}
            cfp = (committed.get("fingerprint") or {}).get("oracle_engine_hash")
            note(STALE_CONTRACT, "contract_drift",
                 "committed contract differs from a fresh extraction",
                 committed_fingerprint=cfp,
                 live_fingerprint=live_fp["oracle_engine_hash"])

    # ── released build manifest ──────────────────────────────────────────────
    if not manifest_path.is_file():
        note(UNVERIFIED, "manifest_missing",
             f"no released Pine build recorded at {manifest_path.name}")
        manifest = None
    else:
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except ValueError as exc:
            note(UNVERIFIED, "manifest_unreadable", f"{manifest_path.name}: {exc}")
            manifest = None

    if manifest is not None:
        m_inputs = (manifest.get("fingerprint") or {}).get("inputs") or {}

        # `validation` is the LEGACY single-verdict block. Under the split model
        # a build is "validated" when at least one stage carries recorded parity
        # evidence in some dimension; the legacy field is kept readable but is no
        # longer what this decides. Firing UNVERIFIED here regardless would mask
        # a build whose logic parity is fully established.
        _recorded = any(
            r["logic_parity"]["status"] != ps.LOGIC_UNVERIFIED
            or r["production_replay_parity"]["status"] != ps.REPLAY_UNVERIFIED
            for r in (ps.migrate_manifest(manifest).get("stages") or {}).values()
            if r["implementation_status"] != ps.IMPL_UNIMPLEMENTED)
        if not _recorded:
            note(UNVERIFIED, "not_validated",
                 "no stage carries recorded parity evidence in any dimension")

        if m_inputs.get("trace_schema_version") != fp.TRACE_SCHEMA_VERSION:
            note(STALE_TRACE_SCHEMA, "trace_schema_moved",
                 "trace schema changed — every golden fixture must be regenerated",
                 was=m_inputs.get("trace_schema_version"),
                 now=fp.TRACE_SCHEMA_VERSION)

        if m_inputs.get("engine_manifest_id") != live_fp["inputs"]["engine_manifest_id"]:
            note(STALE_ENGINE, "engine_manifest_moved",
                 "governed engine content changed since this Pine build",
                 was=m_inputs.get("engine_manifest_id"),
                 now=live_fp["inputs"]["engine_manifest_id"])
        elif m_inputs.get("engine_version") != live_fp["inputs"]["engine_version"]:
            note(STALE_ENGINE, "engine_version_moved",
                 "Lux 3-file digest changed", was=m_inputs.get("engine_version"),
                 now=live_fp["inputs"]["engine_version"])

        for key, code in (("resolved_config_hash", "config_moved"),
                          ("policy_content_sha256", "policy_content_moved"),
                          ("policy_version", "policy_version_moved")):
            if m_inputs.get(key) != live_fp["inputs"].get(key):
                note(STALE_CONFIG, code, f"{key} changed since this Pine build",
                     was=m_inputs.get(key), now=live_fp["inputs"].get(key))

        if m_inputs.get("contract_schema_version") != fp.CONTRACT_SCHEMA_VERSION:
            note(STALE_CONTRACT, "contract_schema_moved",
                 "contract schema version changed",
                 was=m_inputs.get("contract_schema_version"),
                 now=fp.CONTRACT_SCHEMA_VERSION)

        # Hand-edit detection on the generated Pine artefact.
        src = manifest.get("pine_source") or {}
        rel = src.get("path")
        if rel:
            actual = _sha256_file(root / rel)
            if actual is None:
                note(UNVERIFIED, "pine_artifact_missing",
                     f"manifest names {rel} but it is not present")
            elif actual != src.get("sha256"):
                note(INCOMPATIBLE, "pine_artifact_edited",
                     f"{rel} does not match its manifest hash — a GENERATED file "
                     "was modified by hand. Regenerate; never hand-edit.",
                     expected=src.get("sha256"), actual=actual)

        # Fragment-level tamper detection. The assembled file's hash would catch
        # any edit, but only AFTER regeneration; checking fragments catches an
        # edit to a SOURCE fragment that has not been regenerated yet, which is
        # the more likely mistake.
        for frag in src.get("module_sources") or []:
            if frag.get("generated"):
                continue          # produced in-memory; covered by the file hash
            actual = _sha256_file(root / frag["path"])
            if actual is None:
                note(UNVERIFIED, "pine_fragment_missing",
                     f"manifest names fragment {frag['path']} but it is not present")
            elif actual != frag["sha256"]:
                note(STALE_CONTRACT, "pine_fragment_edited",
                     f"{frag['path']} changed since this build — regenerate with "
                     "`python -m tools.oracle.generate_pine --write`",
                     expected=frag["sha256"], actual=actual)

        # Golden fixtures backing the recorded stage results.
        for fx in manifest.get("golden_fixtures") or []:
            actual = _sha256_file(root / fx["path"])
            if actual is None:
                note(UNVERIFIED, "fixture_missing",
                     f"golden fixture {fx['fixture_id']} is missing ({fx['path']})")
            elif actual != fx["sha256"]:
                note(STALE_TRACE_SCHEMA, "fixture_changed",
                     f"golden fixture {fx['fixture_id']} changed — every stage "
                     "validated against it must be re-run",
                     path=fx["path"])
            if fx.get("trace_schema_version") not in (None, fp.TRACE_SCHEMA_VERSION):
                note(STALE_TRACE_SCHEMA, "fixture_schema_moved",
                     f"fixture {fx['fixture_id']} was built for trace schema "
                     f"{fx['trace_schema_version']}, current is {fp.TRACE_SCHEMA_VERSION}")

        # A v1 manifest is migrated in memory. It is NOT rewritten here — a
        # freshness check must not mutate the artefact it is judging.
        try:
            stages = ps.migrate_manifest(manifest).get("stages") or {}
        except ps.ParityStatusError as exc:
            note(INCOMPATIBLE, "manifest_unreadable", str(exc))
            stages = {}

        def _sorted(pred):
            # Numeric stage order, so S10 does not sort before S2.
            return sorted((k for k, v in stages.items() if pred(v)),
                          key=lambda s: int(s[1:]) if s[1:].isdigit() else 999)

        def _logic(v):
            return v["logic_parity"]["status"]

        implemented = _sorted(
            lambda v: v["implementation_status"] != ps.IMPL_UNIMPLEMENTED)
        failing = _sorted(lambda v: _logic(v) == ps.LOGIC_FAILED)
        unvalidated = _sorted(
            lambda v: v["implementation_status"] != ps.IMPL_UNIMPLEMENTED
            and _logic(v) == ps.LOGIC_UNVERIFIED)
        unimplemented = _sorted(
            lambda v: v["implementation_status"] == ps.IMPL_UNIMPLEMENTED)
        # Only stages that EXIST can be affected; listing all fourteen turned a
        # real finding into noise.
        feed_diff = _sorted(
            lambda v: v["implementation_status"] != ps.IMPL_UNIMPLEMENTED
            and v["feed_parity"]["status"] not in
            (ps.FEED_MATCHED, ps.FEED_UNVERIFIED))
        replay_unattainable = _sorted(
            lambda v: v["implementation_status"] != ps.IMPL_UNIMPLEMENTED
            and v["production_replay_parity"]["status"]
            == ps.REPLAY_UNATTAINABLE)
        bootstrapped = _sorted(
            lambda v: _logic(v) == ps.LOGIC_MATCHED_BOOTSTRAP)

        if failing:
            note(INCOMPATIBLE, "stage_logic_failing",
                 "a stage's LOGIC parity is recorded as failed — Pine does not "
                 "reproduce Python on identical bars", stages=failing)
        if unvalidated:
            # PARTIAL, not UNVERIFIED. A newly-implemented stage awaiting its
            # first chart export must not drag the headline down to "nothing is
            # verified" while other stages carry real evidence — that reading is
            # as misleading as the overclaim this model exists to prevent.
            # UNVERIFIED is reserved for a build with NO evidence at all, which
            # `not_validated` above still reports.
            note(PARTIAL, "stage_logic_unvalidated",
                 "implemented but its logic parity was never established from a "
                 "chart_bars comparison — compiling and rendering are not "
                 "evidence", stages=unvalidated)
        if bootstrapped:
            note(PARTIAL, "stage_logic_bootstrapped",
                 "logic parity holds only AFTER a declared initialisation "
                 "interval; earlier bars are not verified", stages=bootstrapped)
        if feed_diff:
            # NOT a defect and NOT suppressible: the chart cannot show
            # production's numbers, and a reader must be told so every time.
            note(PARTIAL, "feed_parity_differs",
                 "the charted feed is not production's data, so no price-derived "
                 "value can equal production's", stages=feed_diff)
        if replay_unattainable:
            note(PARTIAL, "production_replay_unattainable",
                 "full-history production replay cannot be established in the "
                 "current charting context", stages=replay_unattainable)
        if unimplemented:
            note(PARTIAL, "stages_incomplete",
                 "some parity stages are not implemented", stages=unimplemented)

        # Evidence is bound to the build it measured. When the fingerprint moves
        # — a trace-schema bump, a config change — a recorded claim describes a
        # build that no longer exists. It is NOT silently discarded (the impact
        # map decides whether the change could actually affect that stage), but it
        # is never allowed to pass unremarked either.
        live_hash = live_fp["oracle_engine_hash"]
        stale_evidence = _sorted(
            lambda v: any(
                (v[d].get("build_fingerprint") or live_hash) != live_hash
                for d in ("logic_parity", "production_replay_parity")))
        if stale_evidence:
            note(PARTIAL, "stage_evidence_predates_build",
                 "recorded parity evidence was gathered against a different "
                 "build fingerprint; re-export and re-compare to refresh it",
                 stages=stale_evidence)

        # Dependency sanity: a stage may not claim logic parity while something
        # it is computed from does not. This catches a manifest edited out of
        # order. Deliberately NOT wrapped in a bare `except` — a broken
        # dependency map used to be swallowed silently, which meant the check
        # could stop working without anyone noticing.
        from tools.oracle.generate_pine import STAGE_DEPENDS_ON
        for st, deps in STAGE_DEPENDS_ON.items():
            rec = stages.get(st)
            if not rec or _logic(rec) not in ps.LOGIC_GATE_PASSING:
                continue
            broken = [d for d in deps
                      if _logic(stages.get(d, {"logic_parity": {
                          "status": ps.LOGIC_UNVERIFIED}}))
                      not in ps.LOGIC_GATE_PASSING]
            if broken:
                note(INCOMPATIBLE, "stage_dependency_violated",
                     f"{st} claims logic parity but {broken} do not — "
                     f"{st} is computed on what they produce",
                     stage=st, depends_on=broken)

        manifest_stage_view = {
            st: {
                "implementation": r["implementation_status"],
                "logic": _logic(r),
                "bootstrap_bars": r["logic_parity"].get("bootstrap_bars") or 0,
                "feed": r["feed_parity"]["status"],
                "production_replay": r["production_replay_parity"]["status"],
            } for st in implemented for r in [stages[st]]
        }

    if not conditions:
        conditions.add(CURRENT)
    status = next(s for s in PRECEDENCE if s in conditions)

    return {
        "status": status,
        "exit_code": EXIT[status],
        "all_conditions": sorted(conditions),
        "findings": findings,
        "stages": manifest_stage_view,
        "live_fingerprint": live_fp,
        "manifest_fingerprint": (manifest or {}).get("fingerprint", {}).get(
            "oracle_engine_hash") if manifest else None,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--target", choices=sorted(BUILD_TARGETS), default=None,
                    help=f"which build to check (default {DEFAULT_TARGET}). Each "
                         "target has its own manifest, evidence and freshness — "
                         "one being stale says nothing about the other.")
    ap.add_argument("--manifest", type=Path, default=None)
    ap.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    args = ap.parse_args(argv)

    spec = resolve_target(args.target)
    manifest_path = args.manifest or spec["manifest"]
    result = evaluate(manifest_path, args.contract)
    result["build_target"] = spec["target_id"]

    if args.json:
        print(json.dumps(result, indent=1, sort_keys=True))
        return result["exit_code"]

    print(f"ORACLE FRESHNESS [{spec['target_id']}]: {result['status']}")
    if result["live_fingerprint"]:
        print(f"  deployed : {result['live_fingerprint']['oracle_engine_id']}")
        print(f"  hash     : {result['live_fingerprint']['oracle_engine_hash']}")
    if result.get("manifest_fingerprint"):
        print(f"  released : {result['manifest_fingerprint']}")

    # Per-stage, per-DIMENSION. One line per question, because one line for all
    # three is what made "MATCHED" unable to describe this build.
    if result.get("stages"):
        print(f"\nGLOBAL: {result['status']}")
        for st in sorted(result["stages"], key=lambda s: int(s[1:])):
            r = result["stages"][st]
            print(f"{st}")
            print(f"  IMPLEMENTATION   : {r['implementation']}")
            print(f"  LOGIC            : {r['logic']}")
            if r["bootstrap_bars"]:
                print(f"  BOOTSTRAP        : {r['bootstrap_bars']} bars")
            print(f"  FEED             : {r['feed']}")
            print(f"  PRODUCTION REPLAY: {r['production_replay']}")
        missing = [f["stages"] for f in result["findings"]
                   if f["code"] == "stages_incomplete"]
        if missing:
            print(f"{', '.join(missing[0])}: UNIMPLEMENTED")
    if result["findings"]:
        print("\nfindings:")
        for f in result["findings"]:
            print(f"  [{f['status']}] {f['code']}: {f['detail']}")
            for k, v in f.items():
                if k in ("status", "code", "detail"):
                    continue
                print(f"      {k}: {v}")
    else:
        print("\n  no findings — the released Pine build represents the deployed engine.")
    return result["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
