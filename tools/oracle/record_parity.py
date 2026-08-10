"""Record parity evidence into the manifest — one DIMENSION at a time.

    python -m tools.oracle.record_parity --evidence <compare_stages.json> ... --write
    python -m tools.oracle.record_parity --gate S5

WHY A SEPARATE RECORDER
-----------------------
`verify_s1 --record` was written when a stage had ONE status, and it hard-codes
S1. Parity now has three independent dimensions and evidence for each comes from
a differently-run comparison, so the thing that decides "what may this report be
filed against" is no longer a boolean.

THE CENTRAL RULE: **a report may only set the dimension its own comparison basis
can answer.** A `chart_bars` run compares Pine against Python on byte-identical
bars, so it answers LOGIC parity and nothing else. A `fixture_bars` run compares
the chart against the production dataset, so a pass there is production-replay
evidence and a failure is ambiguous between the feed and the Pine. Filing a
`fixture_bars` FAIL as a logic failure is precisely the mistake that made Wave 1
look like four broken stages when nothing was broken.

Feed parity is not derived from either: it is a measurement over the raw OHLC,
supplied by `--feed-measurement`, because "do these two feeds agree" is a
question about data, not about code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from tools.oracle import parity_status as ps
from tools.oracle.engine_access import CT_ROOT
from tools.oracle.generate_pine import MANIFEST_PATH, STAGE_DEPENDS_ON


class RecordError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(
        path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _export_schema_hash(manifest: dict = None) -> str:
    """Imported lazily: `compare_stages` imports this module's sibling
    `generate_pine`, so a module-level import here would close the cycle.

    Scoped to the manifest's BUILD TARGET, so evidence recorded against the
    1-minute execution oracle is fingerprinted over that build's columns rather
    than over both builds' — otherwise every change to either would stale both.
    """
    from tools.oracle.compare_stages import export_schema_hash
    target = (manifest or {}).get("build_target") or "detection_15m"
    return export_schema_hash(target)


def _load_manifest() -> dict:
    if not MANIFEST_PATH.is_file():
        raise RecordError(f"no manifest at {MANIFEST_PATH}")
    return ps.migrate_manifest(json.loads(
        MANIFEST_PATH.read_text(encoding="utf-8")))


def _check_build(report: dict, manifest: dict, path: Path) -> None:
    """Evidence produced against a different build proves nothing about this one."""
    got = (report.get("trace") or {}).get("oracle_engine_hash")
    want = manifest["fingerprint"]["oracle_engine_hash"]
    if got != want:
        raise RecordError(
            f"{path.name}: evidence was produced against build {got!r}, but this "
            f"manifest is {want!r}. Re-export and re-compare.")


def apply_report(manifest: dict, report: dict, path: Path,
                 only_stages=None) -> list[str]:
    """File one `compare_stages` report against the dimension it can answer.

    `only_stages` restricts which stages the report is filed for. It exists so
    that a strict run and a declared-bootstrap run can BOTH be recorded, each for
    the stages it legitimately covers, instead of the later one silently
    overwriting the earlier. Making that explicit at the command line beats a
    precedence rule nobody can see.
    """
    _check_build(report, manifest, path)
    basis = (report.get("input_basis") or {}).get("mode")
    if basis not in ps.INPUT_BASES:
        raise RecordError(
            f"{path.name}: no usable input_basis. Reports written before the "
            "basis was recorded cannot be filed — re-run compare_stages.")
    dimension = ps.dimension_for_basis(basis)
    sha = _sha256(path)
    rel = str(path.resolve()).replace("\\", "/")
    try:
        rel = str(path.resolve().relative_to(CT_ROOT)).replace("\\", "/")
    except ValueError:
        pass

    cov = report.get("coverage") or {}
    boot = int(cov.get("warmup_bars_excluded") or 0)
    window = cov.get("warmup_window")
    touched = []

    for stage, res in (report.get("results") or {}).items():
        if only_stages and stage not in only_stages:
            continue
        rec = manifest["stages"].get(stage)
        if rec is None:
            continue
        verdict = res.get("result")

        if dimension == "logic_parity":
            if verdict == "PASS":
                status = (ps.LOGIC_MATCHED_BOOTSTRAP if boot
                          else ps.LOGIC_MATCHED)
            elif verdict == "FAIL":
                status = ps.LOGIC_FAILED
            else:
                # INVALIDATED / NO_EVIDENCE carry no information about logic.
                continue
            rec["logic_parity"] = {
                "status": status,
                "input_basis": basis,
                "bootstrap_bars": boot,
                "bootstrap_window": list(window) if window else None,
                "compared_bars": int(res.get("compared") or 0),
                "post_bootstrap_divergences": 0 if verdict == "PASS" else None,
                "report_sha256": sha,
                "report_path": rel,
                "build_fingerprint": manifest["fingerprint"]["oracle_engine_hash"],
                # WHICH columns this claim was measured over, and how they were
                # encoded. The engine hash does not move when only the Pine
                # transport changes, so without this a packing refactor would
                # inherit a claim nobody had re-measured.
                "export_schema": _export_schema_hash(manifest),
            }
        else:
            if verdict == "PASS":
                status = ps.REPLAY_MATCHED
            elif verdict == "FAIL":
                status = ps.REPLAY_DIVERGENT
            else:
                continue
            rec["production_replay_parity"] = {
                **rec["production_replay_parity"],
                "status": status,
                "report_sha256": sha,
                "report_path": rel,
                "build_fingerprint": manifest["fingerprint"]["oracle_engine_hash"],
            }
        touched.append(f"{stage}.{dimension} = {status}")
    return touched


def apply_feed_measurement(manifest: dict, measurement: dict,
                           report_path: Path | None = None) -> list[str]:
    """Record the measured feed difference and set every stage's feed parity.

    Feed parity is a property of the DATA, so it is the same for every stage —
    but it is stored per stage anyway, because a reader looking at one stage must
    not have to know to go and look somewhere else for the caveat.
    """
    status = measurement.pop("status", None) or ps.FEED_DIFFERENT
    if status not in ps.FEED_STATUSES:
        raise RecordError(f"unknown feed status {status!r}")
    manifest["feed_measurement"] = measurement
    sha = _sha256(report_path) if report_path and report_path.is_file() else None
    for rec in manifest["stages"].values():
        rec["feed_parity"] = {
            "status": status,
            "production_feed": measurement.get("production_feed"),
            "tradingview_feed": measurement.get("tradingview_feed"),
            "report_sha256": sha,
            "report_path": (str(report_path).replace("\\", "/")
                            if report_path else None),
        }
    return [f"feed_parity = {status} (all stages)"]


def set_replay_unattainable(manifest: dict, reason: str) -> list[str]:
    """Replay parity cannot be measured when the feed differs — the comparison
    would be measuring the feed. Recording WHY is mandatory."""
    if reason not in ps.REPLAY_REASONS or reason is None:
        raise RecordError(f"unknown replay reason {reason!r}")
    for rec in manifest["stages"].values():
        rec["production_replay_parity"] = {
            **rec["production_replay_parity"],
            "status": ps.REPLAY_UNATTAINABLE,
            "reason": reason,
        }
    return [f"production_replay_parity = {ps.REPLAY_UNATTAINABLE} ({reason})"]


def set_logic_unattainable(manifest: dict, stage: str, reason: str,
                           evidence: dict | None = None) -> list[str]:
    """Record that the CHART cannot answer a stage's logic question.

    Distinct from FAILED and from UNVERIFIED, and the distinction is the whole
    point. FAILED says the Pine is wrong. UNVERIFIED says nobody has looked.
    UNATTAINABLE says someone looked, established what the answer would require,
    and found the chart cannot supply it — so no amount of Pine work moves it and
    no future export will either, until the context changes.
    """
    if reason not in ps.LOGIC_UNATTAINABLE_REASONS or reason is None:
        raise RecordError(f"unknown logic-unattainable reason {reason!r}")
    rec = manifest["stages"].get(stage)
    if rec is None:
        raise RecordError(f"unknown stage {stage}")
    rec["logic_parity"] = {
        **rec["logic_parity"],
        "status": ps.LOGIC_UNATTAINABLE,
        "unattainable_reason": reason,
        "evidence": evidence,
        "build_fingerprint": manifest["fingerprint"]["oracle_engine_hash"],
        "export_schema": _export_schema_hash(manifest),
    }
    return [f"{stage}.logic_parity = {ps.LOGIC_UNATTAINABLE} ({reason})"]


def set_input_availability(manifest: dict, stage: str, status: str,
                           reason: str, measurement: dict | None = None,
                           report_path: Path | None = None) -> list[str]:
    """Record dimension 5 for one stage.

    A PARTIAL claim without numbers is refused by `validate_stage_record`, which
    is deliberate: "matched on the bars we could score" is a materially weaker
    statement than "matched", and the only thing that makes it readable is the
    denominator.
    """
    if status not in ps.INPUT_STATUSES:
        raise RecordError(f"unknown input status {status!r}")
    if reason not in ps.INPUT_REASONS:
        raise RecordError(f"unknown input reason {reason!r}")
    rec = manifest["stages"].get(stage)
    if rec is None:
        raise RecordError(f"unknown stage {stage}")
    m = dict(measurement or {})
    rec["input_availability"] = {
        **rec["input_availability"],
        "status": status,
        "reason": reason,
        "total_bars": m.get("total_bars"),
        "unavailable_bars": m.get("unavailable_bars"),
        "scored_bars": m.get("scored_bars"),
        "coverage_pct": m.get("coverage_pct"),
        "affected_setups": m.get("affected_setups"),
        "affected_transitions": m.get("affected_transitions"),
        "report_sha256": (_sha256(report_path)
                          if report_path and report_path.is_file() else None),
        "report_path": (str(report_path).replace("\\", "/")
                        if report_path else None),
    }
    ps.validate_stage_record(stage, rec)
    return [f"{stage}.input_availability = {status} ({reason})"]


def invalidate_stale_evidence(manifest: dict) -> list[str]:
    """Demote every dimension whose evidence predates the current build.

    "The algorithm did not intentionally change" is not evidence that it did not
    change. A build fingerprint moves when the engine, the config, the contract
    or the trace schema moves, and any of those can alter what the Pine computes;
    deciding case by case which changes were harmless is exactly the judgement
    call this apparatus exists to remove.

    So the demotion is mechanical: the dimension returns to its ignorant value
    and the superseded claim is preserved under `superseded`, so the history is
    auditable and a refresh restores it from one new export.
    """
    live = manifest["fingerprint"]["oracle_engine_hash"]
    schema = _export_schema_hash(manifest)
    changed = []
    for stage, rec in manifest["stages"].items():
        for dim, ignorant in (("logic_parity", ps.LOGIC_UNVERIFIED),
                              ("production_replay_parity", ps.REPLAY_UNVERIFIED)):
            d = rec[dim]
            stamped = d.get("build_fingerprint")
            if not stamped or d["status"] == ignorant:
                continue
            # Two independent ways for evidence to go stale: PRODUCTION moved
            # (engine hash), or the CHART's export schema moved. The second was
            # invisible until the packing refactor changed every small-integer
            # field's transport while the engine hash stood still.
            why = ("build" if stamped != live else
                   "export schema" if d.get("export_schema") != schema else None)
            if why is None:
                continue
            rec[dim] = {
                **{k: v for k, v in d.items()},
                "status": ignorant,
                "superseded": {
                    "status": d["status"],
                    "build_fingerprint": stamped,
                    "export_schema": d.get("export_schema"),
                    "report_sha256": d.get("report_sha256"),
                    "reason": why,
                    "note": (f"evidence was gathered against a different {why}; "
                             "re-export and re-compare to restore the claim"),
                },
            }
            changed.append(
                f"{stage}.{dim}: {d['status']} -> {ignorant} (stale {why})")
    return changed


def evaluate_gate(manifest: dict, stage: str):
    """Part E's entry gate: LOGIC parity of every dependency, nothing more.

    The old gate — "every dependency MATCHED" — is unsatisfiable by construction
    now that MATCHED requires feed parity, and feed parity is unattainable on the
    available chart. Gating on the dimension a Pine defect can actually move is
    what the gate was always for.
    """
    deps = STAGE_DEPENDS_ON.get(stage, [])
    ok, findings = ps.evaluate_stage_gate(manifest["stages"], stage, deps)

    rec = manifest["stages"].get(stage)
    if rec and rec["production_replay_parity"]["status"] == ps.REPLAY_MATCHED \
            and rec["feed_parity"]["status"] != ps.FEED_MATCHED:
        ok = False
        findings.append(("replay_overclaimed",
                         f"{stage} claims replay parity without feed parity"))
    return ok, findings, deps


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--evidence", type=Path, nargs="*", default=[],
                    help="compare_stages report(s); each is filed against the "
                         "dimension its own input_basis can answer")
    ap.add_argument("--stages", nargs="*", default=None,
                    help="restrict the evidence to these stages, so a strict run "
                         "and a declared-bootstrap run can each be recorded for "
                         "the stages they legitimately cover")
    ap.add_argument("--feed-measurement", type=Path,
                    help="JSON produced by measure_feed; sets feed parity")
    ap.add_argument("--invalidate-stale", action="store_true",
                    help="demote every dimension whose evidence predates the "
                         "current build fingerprint")
    ap.add_argument("--replay-unattainable", metavar="REASON",
                    help=f"one of {[r for r in ps.REPLAY_REASONS if r]}")
    ap.add_argument("--logic-unattainable", nargs=2,
                    metavar=("STAGE", "REASON"),
                    help="record that the chart cannot answer STAGE's logic "
                         f"question; REASON is one of "
                         f"{[r for r in ps.LOGIC_UNATTAINABLE_REASONS if r]}")
    ap.add_argument("--input-availability", nargs=3,
                    metavar=("STAGE", "STATUS", "REASON"),
                    help=f"dimension 5. STATUS in {list(ps.INPUT_STATUSES)}, "
                         f"REASON in {[r for r in ps.INPUT_REASONS if r]}")
    ap.add_argument("--input-measurement", type=Path,
                    help="JSON with total_bars / unavailable_bars / scored_bars "
                         "/ coverage_pct (required for INPUT_COVERAGE_PARTIAL)")
    ap.add_argument("--gate", metavar="STAGE",
                    help="evaluate the entry gate for STAGE and exit")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args(argv)

    try:
        manifest = _load_manifest()
    except (RecordError, ps.ParityStatusError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    if args.gate:
        ok, findings, deps = evaluate_gate(manifest, args.gate)
        print(f"GATE {args.gate}  depends on {deps or '(nothing)'}")
        for dep in deps:
            r = manifest["stages"].get(dep, {})
            lp = (r.get("logic_parity") or {})
            boot = lp.get("bootstrap_bars") or 0
            print(f"  {dep}: logic={lp.get('status')}"
                  + (f" bootstrap={boot}" if boot else "")
                  + f"  basis={lp.get('input_basis')}")
        for code, detail in findings:
            print(f"  [{code}] {detail}")
        print(f"\nRESULT: {'OPEN' if ok else 'REFUSED'}")
        return 0 if ok else 1

    changes = []
    try:
        for path in args.evidence:
            changes += apply_report(
                manifest, json.loads(path.read_text(encoding="utf-8")), path,
                only_stages=set(args.stages) if args.stages else None)
        if args.feed_measurement:
            changes += apply_feed_measurement(
                manifest,
                json.loads(args.feed_measurement.read_text(encoding="utf-8")),
                args.feed_measurement)
        if args.replay_unattainable:
            changes += set_replay_unattainable(manifest,
                                               args.replay_unattainable)
        if args.input_availability:
            stage, status, reason = args.input_availability
            m = (json.loads(args.input_measurement.read_text(encoding="utf-8"))
                 if args.input_measurement else None)
            changes += set_input_availability(
                manifest, stage, status, reason, m, args.input_measurement)
        if args.logic_unattainable:
            stage, reason = args.logic_unattainable
            m = (json.loads(args.input_measurement.read_text(encoding="utf-8"))
                 if args.input_measurement else None)
            changes += set_logic_unattainable(manifest, stage, reason, m)
        if args.invalidate_stale:
            changes += invalidate_stale_evidence(manifest)
    except (RecordError, ps.ParityStatusError, ValueError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 2

    for stage, rec in manifest["stages"].items():
        try:
            ps.validate_stage_record(stage, rec)
        except ps.ParityStatusError as exc:
            print(f"REFUSED: {exc}", file=sys.stderr)
            return 2
    manifest["global_status"] = ps.global_status(manifest["stages"])

    for c in changes:
        print(f"  {c}")
    print(f"global_status = {manifest['global_status']}")
    if args.write:
        MANIFEST_PATH.write_text(ps.serialize_manifest(manifest),
                                 encoding="utf-8")
        print(f"written: {MANIFEST_PATH.relative_to(CT_ROOT)}")
        print("NOTE: regenerate so the embedded Pine status matches "
              "(evidence is now carried across a regeneration):")
        print("      python -m tools.oracle.generate_pine --write")
    else:
        print("(dry run — pass --write to record)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
