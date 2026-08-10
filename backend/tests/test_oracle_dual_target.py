"""Dual-oracle architecture: two build targets, two time domains.

Production has two time domains. `run_backtest` resamples to 15 minutes to
DETECT order blocks, then hands `simulate_trades` the raw 1-minute candle file —
so arming, the 3-candle delay and the containment touch are minute-by-minute, and
a 15-minute bar cannot order events inside itself.

Two builds follow: `detection_15m` owns S1-S6, `execution_1m` owns S7+. The tests
here are about the SEPARATION — that each build has its own artefact, manifest,
evidence and freshness, and that a change to one cannot silently restate the
other — plus the cross-timeframe handoff, which is the one piece of shared
machinery and therefore the one that has to be proved rather than asserted.
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

from tools.oracle import parity_status as ps  # noqa: E402
from tools.oracle import replay_prefill as rp  # noqa: E402
from tools.oracle import session_codes as codes  # noqa: E402
from tools.oracle.compare_stages import (STAGE_COLUMNS,  # noqa: E402
                                         TARGET_SURFACES, export_schema_hash)
from tools.oracle.engine_access import load_engine, resolve_config  # noqa: E402
from tools.oracle.generate_pine import (BUILD_TARGETS, HAND_FRAGMENTS,  # noqa: E402
                                        STAGE_STATUS, UnknownTarget, generate,
                                        resolve_target, target_for_stage)
from tools.oracle.lint_pine import PLOT_LIMIT, lint  # noqa: E402

PLOT_FUNCS = ("plot", "plotshape", "plotchar", "plotarrow", "plotcandle",
              "plotbar")


def _plots(src: str) -> int:
    code = "\n".join(l.split("//")[0] for l in src.splitlines())
    return sum(len(re.findall(rf"(?<![\w.]){f}\s*\(", code)) for f in PLOT_FUNCS)


@pytest.fixture(scope="module")
def builds():
    """Generate BOTH targets once, without writing."""
    return {name: generate(write=False, target=name) for name in BUILD_TARGETS}


# ══ targets ══════════════════════════════════════════════════════════════════

def test_every_stage_has_exactly_one_owner():
    """A stage in two builds would have two source hashes and two evidence
    trails for one claim, and the next question would be which is authoritative."""
    seen = {}
    for name, spec in BUILD_TARGETS.items():
        for st in spec["stages"]:
            assert st not in seen, f"{st} owned by {seen[st]} AND {name}"
            seen[st] = name
    assert set(seen) <= set(STAGE_STATUS)
    for st, owner in seen.items():
        assert target_for_stage(st) == owner
    # …and an unowned stage resolves to nothing rather than to a default.
    assert target_for_stage("S9") is None


def test_detection_target_matches_the_legacy_fragment_list():
    assert BUILD_TARGETS["detection_15m"]["fragments"] == HAND_FRAGMENTS


def test_unknown_target_is_refused_not_defaulted():
    with pytest.raises(UnknownTarget, match="unknown build target"):
        resolve_target("executon_1m")          # the plausible typo


def test_targets_write_to_different_files(builds):
    paths = {n: (s["pine"], s["manifest"]) for n, s in BUILD_TARGETS.items()}
    assert len({p for pair in paths.values() for p in pair}) == 2 * len(paths)


def test_the_two_builds_have_different_source_hashes(builds):
    a = builds["detection_15m"]["manifest"]["pine_source"]["sha256"]
    b = builds["execution_1m"]["manifest"]["pine_source"]["sha256"]
    assert a != b


def test_dual_generation_is_deterministic(builds):
    for name, out in builds.items():
        again = generate(write=False, target=name)
        assert again["pine"] == out["pine"], name
        assert (again["manifest"]["pine_source"]["sha256"]
                == out["manifest"]["pine_source"]["sha256"])


def test_generating_one_target_does_not_change_the_other_on_disk(builds):
    """`--write` used to have a single destination. With two builds a shared one
    would mean the last command run decides which oracle is on disk."""
    other = BUILD_TARGETS["execution_1m"]["pine"]
    before = other.read_text(encoding="utf-8") if other.is_file() else None
    generate(write=False, target="detection_15m")
    after = other.read_text(encoding="utf-8") if other.is_file() else None
    assert before == after


# ══ shared contract, separate manifests ══════════════════════════════════════

def test_both_builds_share_one_engine_identity(builds):
    a = builds["detection_15m"]["manifest"]["fingerprint"]
    b = builds["execution_1m"]["manifest"]["fingerprint"]
    assert a["oracle_engine_hash"] == b["oracle_engine_hash"]
    assert a["oracle_engine_id"] == b["oracle_engine_id"]
    assert a["inputs"] == b["inputs"]


def test_each_manifest_names_its_target_and_only_its_stages(builds):
    for name, out in builds.items():
        m = out["manifest"]
        assert m["build_target"] == name
        assert tuple(m["build"]["owned_stages"]) == BUILD_TARGETS[name]["stages"]
        assert set(m["stages"]) == set(BUILD_TARGETS[name]["stages"])
        assert m["build"]["chart_timeframe"] == BUILD_TARGETS[name]["timeframe"]


def test_manifests_record_the_whole_project_ownership_map(builds):
    """Each build knows which target owns every stage, so a reader holding ONE
    manifest can still tell that S7 lives somewhere else rather than concluding
    it does not exist."""
    for out in builds.values():
        own = out["manifest"]["build"]["stage_ownership"]
        assert own["S1"] == "detection_15m"
        assert own["S7"] == "execution_1m"
        assert own["S9"] is None


# ══ independent evidence and staleness ═══════════════════════════════════════

def test_export_schema_is_scoped_to_the_target():
    a = export_schema_hash("detection_15m")
    b = export_schema_hash("execution_1m")
    assert a != b
    assert set(TARGET_SURFACES["detection_15m"]) & set(
        TARGET_SURFACES["execution_1m"]) == set()


def test_an_execution_only_change_does_not_stale_the_detection_build(monkeypatch):
    """The whole point of two targets. A change confined to the 1-minute export
    surface must leave the 15-minute build's recorded evidence standing."""
    before_det = export_schema_hash("detection_15m")
    before_exe = export_schema_hash("execution_1m")
    spec = {k: list(v) for k, v in codes.PACKED_SPEC.items()}
    spec["X1"] = [("oracleXfBars", 32, 0)] + spec["X1"][1:]
    monkeypatch.setattr(codes, "PACKED_SPEC", spec)
    assert export_schema_hash("detection_15m") == before_det
    assert export_schema_hash("execution_1m") != before_exe


def test_a_detection_only_change_does_not_stale_the_execution_build(monkeypatch):
    before_det = export_schema_hash("detection_15m")
    before_exe = export_schema_hash("execution_1m")
    cols = {k: tuple(v) for k, v in STAGE_COLUMNS.items()}
    cols["S4"] = cols["S4"] + ("oracleSomethingNew",)
    monkeypatch.setattr("tools.oracle.compare_stages.STAGE_COLUMNS", cols)
    assert export_schema_hash("detection_15m") != before_det
    assert export_schema_hash("execution_1m") == before_exe


def test_evidence_from_another_target_is_not_carried_forward(tmp_path):
    """Different timeframe, different stages, different export schema. Reading
    it would be worse than having none."""
    from tools.oracle.generate_pine import carry_forward_evidence
    spec = dict(BUILD_TARGETS["execution_1m"])
    spec["manifest"] = tmp_path / "foreign.json"
    foreign = {
        "schema": ps.MANIFEST_SCHEMA_V3,
        "build_target": "detection_15m",
        "stages": {"S7": ps.new_stage_record(ps.IMPL_IMPLEMENTED)},
    }
    foreign["stages"]["S7"]["logic_parity"].update(
        status=ps.LOGIC_MATCHED, input_basis=ps.BASIS_CHART)
    spec["manifest"].write_text(json.dumps(foreign), encoding="utf-8")
    fresh = {"stages": {"S7": ps.new_stage_record(ps.IMPL_UNIMPLEMENTED)}}
    out = carry_forward_evidence(fresh, spec)
    assert out["stages"]["S7"]["logic_parity"]["status"] != ps.LOGIC_MATCHED


def test_the_execution_build_claims_no_parity(builds):
    m = builds["execution_1m"]["manifest"]
    rec = m["stages"]["S7"]
    assert rec["implementation_status"] == ps.IMPL_UNIMPLEMENTED
    assert rec["logic_parity"]["status"] not in ps.LOGIC_GATE_PASSING


# ══ chart context ════════════════════════════════════════════════════════════

def test_each_build_accepts_only_its_own_timeframe(builds):
    """Running the detection oracle on a 1-minute chart would apply a 200-bar
    ATR and a 50-bar swing window to minutes and report statuses measured at
    fifteen."""
    for name, out in builds.items():
        want = BUILD_TARGETS[name]["chart_timeframe_seconds"]
        m = re.search(r"ORACLE_TF_SECONDS\s*=\s*array\.from\(([^)]*)\)",
                      out["pine"])
        assert m, f"{name}: no ORACLE_TF_SECONDS"
        got = [int(x) for x in m.group(1).split(",")]
        assert got == want, f"{name}: accepts {got}, should accept {want}"


def test_the_builds_do_not_accept_each_others_timeframe():
    a = BUILD_TARGETS["detection_15m"]["chart_timeframe_seconds"]
    b = BUILD_TARGETS["execution_1m"]["chart_timeframe_seconds"]
    assert set(a).isdisjoint(b)


# ══ plot budgets ═════════════════════════════════════════════════════════════

@pytest.mark.parametrize("name", sorted(BUILD_TARGETS))
def test_each_build_keeps_its_own_reserve(builds, name):
    used = _plots(builds[name]["pine"])
    reserve = BUILD_TARGETS[name]["plot_reserve"]
    assert used <= PLOT_LIMIT - reserve, (
        f"{name}: {used} of {PLOT_LIMIT} used, reserve {reserve}")


def test_the_execution_build_does_not_inherit_detection_furniture(builds):
    """It has no swings, no structure labels and no session shading, so carrying
    those plots across would imply it draws things it does not."""
    pine = builds["execution_1m"]["pine"]
    for absent in ("oracleSwingHigh", "oracleObTop", "oracleRgEma",
                   "oracleS1Codes", "oracleBrokenLevel"):
        assert absent not in pine, absent


@pytest.mark.parametrize("name", sorted(BUILD_TARGETS))
def test_each_build_lints_clean(builds, name):
    findings = [f for f in lint(builds[name]["pine"]) if f["severity"] == "error"]
    assert not findings, findings


# ══ the cross-timeframe handoff ══════════════════════════════════════════════

def _bars(times, o, h, l, c):        # noqa: E741
    import pandas as pd
    return [{"time": pd.Timestamp(t, tz="UTC"), "open": a, "high": b,
             "low": d, "close": e} for t, a, b, d, e in zip(times, o, h, l, c)]


def test_derived_frame_matches_production_resampling_bar_for_bar():
    """THE load-bearing test. The execution build rebuilds the 15-minute frame
    from 1-minute bars; production builds its detection frame the same way. If
    these ever disagree, every stage the handoff feeds is meaningless."""
    import pandas as pd
    engine = load_engine(require_pin=True)
    cfg, _ = resolve_config(engine)
    prepared, _obs, _news, seam = rp._load(engine, cfg, "2026-01-05",
                                       "2026-01-20")
    assert seam is None, "a frozen-only load must not report a feed seam"
    ours = rp.derive_detection_frame(prepared.to_dict("records"))
    theirs = engine.rb.resample_candles(prepared, cfg.detection_timeframe)

    # The last bucket is deliberately not emitted — on a live chart it is still
    # open, and emitting it is exactly the lookahead this design avoids.
    assert len(ours) == len(theirs) - 1, (len(ours), len(theirs))
    for i, (label, o, h, l, c, n) in enumerate(ours):      # noqa: E741
        row = theirs.iloc[i]
        assert label == int(pd.Timestamp(row["time"]).value // 1_000_000)
        assert o == pytest.approx(row["open"], abs=0.0)
        assert h == pytest.approx(row["high"], abs=0.0)
        assert l == pytest.approx(row["low"], abs=0.0)
        assert c == pytest.approx(row["close"], abs=0.0)
        assert 1 <= n <= 15


def test_empty_buckets_are_dropped_not_filled():
    """pandas' `dropna` deletes buckets with no 1-minute bars, and over the chart
    window that is 28.8% of slots — almost all weekend. A frame that emitted
    placeholder bars there would disagree with production about which bars EXIST,
    which is a precondition for agreeing about their values."""
    bars = _bars(["2026-01-05 09:00", "2026-01-05 09:14",
                  # …a two-hour hole…
                  "2026-01-05 11:00", "2026-01-05 11:20"],
                 [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4], [1, 2, 3, 4])
    out = rp.derive_detection_frame(bars)
    labels = [o[0] for o in out]
    assert len(labels) == 2                      # 09:00 and 11:00; NOT 8 slots
    gap_ms = labels[1] - labels[0]
    assert gap_ms == 2 * 60 * 60 * 1000


def test_a_bucket_is_emitted_only_after_it_closes():
    """No lookahead, no repainting. A bucket is finalised on the FIRST bar of the
    NEXT bucket, so a value can never change after it has been read."""
    bars = _bars(["2026-01-05 09:00", "2026-01-05 09:05", "2026-01-05 09:10"],
                 [1, 2, 3], [1, 5, 3], [1, 2, 3], [1, 2, 3])
    assert rp.derive_detection_frame(bars) == []          # bucket still open
    bars += _bars(["2026-01-05 09:15"], [9], [9], [9], [9])
    out = rp.derive_detection_frame(bars)
    assert len(out) == 1
    assert out[0][2] == 5.0                                # the high it saw
    # …and adding more bars to the NEXT bucket never revises the emitted one.
    bars += _bars(["2026-01-05 09:16"], [99], [99], [99], [99])
    assert rp.derive_detection_frame(bars)[0] == out[0]


def test_a_single_minute_still_produces_a_bucket():
    """Production's aggregation is first/max/min/last over whatever bars exist —
    one is enough, and the count is what reveals a thin bucket."""
    bars = _bars(["2026-01-05 09:07", "2026-01-05 09:15"],
                 [1, 2], [1, 2], [1, 2], [1, 2])
    out = rp.derive_detection_frame(bars)
    assert len(out) == 1 and out[0][5] == 1


def test_bucket_labels_are_left_edges():
    bars = _bars(["2026-01-05 09:07", "2026-01-05 09:22"],
                 [1, 2], [1, 2], [1, 2], [1, 2])
    import pandas as pd
    label = rp.derive_detection_frame(bars)[0][0]
    assert label == int(pd.Timestamp("2026-01-05 09:00", tz="UTC").value
                        // 1_000_000)


def test_bucket_bar_count_fits_the_packed_radix():
    """Width 16 is EXACT, not generous: a 15-minute bucket holds at most 15
    one-minute bars, so 16 would mean the bucket arithmetic is wrong."""
    width = next(w for f, w, _o in codes.PACKED_SPEC["X1"]
                 if f == "oracleXfBars")
    assert width == 16
    assert codes.packed_capacity("X1") < codes.FLOAT64_EXACT_INT


# ══ scope ════════════════════════════════════════════════════════════════════

def test_s8_remains_unowned_and_unimplemented():
    assert STAGE_STATUS["S8"] == ps.IMPL_UNIMPLEMENTED
    assert target_for_stage("S8") is None
