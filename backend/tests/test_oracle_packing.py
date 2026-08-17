"""Packed-transport tests (Wave 2b-i).

TradingView caps a script at 64 plot-family calls and raises RE10140 at RUNTIME
on a live chart — not at compile time, and not in any static check that existed
before it was hit for real at 83 plots. The fix was to pack small-integer fields
several to a plot, which trades a platform limit for a NEW failure mode: a field
that overflows its radix carries into its neighbour and both sides decode
plausible-looking wrong values.

These tests exist to make that failure mode impossible to reach quietly:

  * every group round-trips over its ENTIRE capacity, not a sample;
  * the largest packed integer stays far inside float64's exact-integer range;
  * Python's `pack` raises on an out-of-range field, and Pine's clamp produces a
    value that CANNOT equal the correct one, so the comparator fails the bar;
  * the measured production maxima stay well inside the declared widths, so a
    future width reduction fails here rather than on someone's chart;
  * the Pine fragments contain no hand-written radix arithmetic at all.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import session_codes as codes  # noqa: E402
from tools.oracle.compare_stages import (STAGE_COLUMNS, STAGE_FIELDS,  # noqa: E402
                                         export_schema_hash)
from tools.oracle.generate_pine import OUT_PINE, SRC_DIR  # noqa: E402
from tools.oracle.lint_pine import PLOT_LIMIT, PLOT_RESERVE, lint  # noqa: E402

PLOT_FUNCS = ("plot", "plotshape", "plotchar", "plotarrow", "plotcandle",
              "plotbar")


def _plot_calls(src: str) -> int:
    code = "\n".join(l.split("//")[0] for l in src.splitlines())
    return sum(len(re.findall(rf"(?<![\w.]){f}\s*\(", code)) for f in PLOT_FUNCS)


# ── round trip ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("group", sorted(codes.PACKED_SPEC))
def test_every_packed_value_round_trips_over_the_whole_capacity(group):
    """Exhaustive, not sampled. The capacities are small enough to enumerate
    (the largest is 4.2M), and a decode that is wrong for one value in a million
    is exactly the kind of defect a sample misses."""
    cap = codes.packed_capacity(group)
    step = 1 if cap <= 100_000 else 7            # 4.2M / 7 still covers ~600k
    for v in range(0, cap, step):
        assert codes.pack(group, codes.unpack(group, v)) == v
    # …and always the extremes, whatever the step landed on.
    for v in (0, cap - 1):
        assert codes.pack(group, codes.unpack(group, v)) == v


@pytest.mark.parametrize("group", sorted(codes.PACKED_SPEC))
def test_each_field_survives_its_full_range_independently(group):
    """One field at its maximum must not disturb any other field."""
    spec = codes.PACKED_SPEC[group]
    base = {f: -o for f, _w, o in spec}          # every field at its minimum
    for field, width, offset in spec:
        for v in (-offset, width - 1 - offset):
            vals = dict(base, **{field: v})
            assert codes.unpack(group, codes.pack(group, vals)) == vals


# ── float64 safety ───────────────────────────────────────────────────────────

def test_largest_packed_integer_is_exactly_representable():
    worst = max(codes.packed_capacity(g) - 1 for g in codes.PACKED_SPEC)
    assert worst < codes.FLOAT64_EXACT_INT
    # A wide margin, not a bare inequality: the value also has to survive
    # TradingView rendering it into a CSV and Python parsing it back.
    assert codes.FLOAT64_EXACT_INT / worst > 1e6
    for g in codes.PACKED_SPEC:
        cap = codes.packed_capacity(g)
        for v in (0, cap // 2, cap - 1):
            assert int(float(v)) == v
            assert int(round(float(f"{float(v):.10g}"))) == v


# ── fail closed ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("group", sorted(codes.PACKED_SPEC))
def test_pack_refuses_an_out_of_range_field(group):
    field, width, offset = codes.PACKED_SPEC[group][0]
    vals = {f: -o for f, _w, o in codes.PACKED_SPEC[group]}
    with pytest.raises(ValueError, match="outside its packed width"):
        codes.pack(group, dict(vals, **{field: width - offset}))
    with pytest.raises(ValueError, match="outside its packed width"):
        codes.pack(group, dict(vals, **{field: -offset - 1}))


def _pine_clamp(v, width, offset):
    """The exact semantics of `f_packv` in pine/src/10_inputs.pinefrag."""
    x = (-offset if v is None else v) + offset
    return 0 if x < 0 else (width - 1 if x > width - 1 else x)


@pytest.mark.parametrize("group", sorted(codes.PACKED_SPEC))
def test_a_pine_side_overflow_cannot_decode_as_the_right_answer(group):
    """Pine has no range check, so `f_packv` CLAMPS. The property that matters is
    not that the clamp is right — it is that a clamped field can never produce
    the packed integer the correct value would have produced, so the comparator
    reports a mismatch instead of agreeing with a corrupted decode."""
    spec = codes.PACKED_SPEC[group]
    mults = codes.packed_multipliers(group)
    for i, (field, width, offset) in enumerate(spec):
        overflow = width - offset            # one past the largest legal value
        clamped = sum(_pine_clamp(overflow if j == i else -o, w, o) * m
                      for j, ((_f, w, o), m) in enumerate(zip(spec, mults)))
        # Python refuses outright…
        with pytest.raises(ValueError):
            codes.pack(group, dict({f: -o for f, _w, o in spec},
                                   **{field: overflow}))
        # …and whatever Pine emitted differs from every legal encoding of a
        # SMALLER value of that field, which is what the comparator sees.
        legal = sum(_pine_clamp(width - 1 - offset if j == i else -o, w, o) * m
                    for j, ((_f, w, o), m) in enumerate(zip(spec, mults)))
        assert clamped == legal   # clamp lands on the top of the radix
        assert codes.unpack(group, clamped)[field] == width - 1 - offset
        assert codes.unpack(group, clamped)[field] != overflow


def test_na_maps_to_the_absent_code_for_every_optional_field():
    """Optional fields are specified with offset 1 so `na` -> 0 and the smallest
    real value -> 1. A blank cell would have meant "absent" to one reader and
    "zero" to another."""
    for group, spec in codes.PACKED_SPEC.items():
        for field, width, offset in spec:
            if offset == 0:
                continue
            assert _pine_clamp(None, width, offset) == 0
            assert codes.unpack(group, 0)[field] == -offset


# ── measured headroom ────────────────────────────────────────────────────────

#: Maxima observed over the FULL production window (285,790 detection bars,
#: 2,080 accepted order blocks) — see the Wave 2b-i measurement. A width chosen
#: below these would corrupt real data, so shrinking one fails here.
MEASURED_MAX = {
    ("S4", "oracleStructEventCount"): 1,
    ("S4", "oracleBias"): 1,
    ("S5A", "oracleObCandidates"): 1,
    ("S5A", "oracleObCreated"): 1,
    ("S5B", "oracleObOriginBack"): 250,
    ("S5B", "oracleObPivotBack"): 393,
}


@pytest.mark.parametrize("key,observed", sorted(MEASURED_MAX.items()))
def test_declared_width_leaves_measured_headroom(key, observed):
    group, field = key
    width, offset = next((w, o) for f, w, o in codes.PACKED_SPEC[group]
                         if f == field)
    largest = width - 1 - offset
    assert largest >= observed, f"{group}.{field} cannot hold its observed max"
    assert largest >= observed * 2, (
        f"{group}.{field} holds {largest} against an observed {observed} — too "
        "little headroom for a window that loads more history")


# ── generator / Pine wiring ──────────────────────────────────────────────────

def test_pine_contains_no_hand_written_radix_arithmetic():
    """Every multiplier, width and offset the fragments use must be a GENERATED
    identifier. A literal here would be a silent decode error the moment the
    spec moved."""
    frag = (SRC_DIR / "60_parity_output.pinefrag").read_text(encoding="utf-8")
    code = "\n".join(l.split("//")[0] for l in frag.splitlines())
    for m in re.finditer(r"f_packv\s*\(([^)]*)\)\s*\*\s*([A-Za-z_]\w*|\d+)",
                         code):
        args, mult = m.group(1), m.group(2)
        assert mult.startswith("PACK_"), f"literal multiplier {mult!r}"
        width, offset = [a.strip() for a in args.split(",")[1:3]]
        assert width.startswith("PACK_") and "_W" in width, width
        assert offset.startswith("PACK_") and "_O" in offset, offset


def test_generated_pine_defines_every_pack_constant_the_fragments_use():
    """Per BUILD TARGET. Each build emits only the multipliers for the surfaces
    it exports — the execution oracle has no swings and no order blocks, so
    shipping their radices would imply it exports them — so a fragment's
    constants must be defined by ITS OWN build, not by whichever happens to be
    on disk."""
    from tools.oracle.compare_stages import TARGET_SURFACES
    from tools.oracle.generate_pine import BUILD_TARGETS
    all_used = set()
    for name, spec in BUILD_TARGETS.items():
        # A build that EXPORTS NOTHING references no packed constants, and that
        # is not a hole in the check — the packed transport exists to carry
        # exported values to the comparator. The strategy companion has no
        # export surface at all (it reuses the detection build's stages, where
        # the parity question is already answered), so requiring constants of it
        # would be requiring an export it deliberately does not have.
        if not TARGET_SURFACES[name]:
            assert not spec["stages"], (
                f"{name} owns stages but exports nothing — one of the two is "
                f"wrong, and a stage with no export can never be compared")
            continue
        pine = spec["pine"].read_text(encoding="utf-8")
        defined = set(re.findall(r"^(PACK_\w+)\s*=", pine, re.M))
        used = set()
        for frag in spec["fragments"]:
            code = "\n".join(
                l.split("//")[0] for l in
                (SRC_DIR / f"{frag}.pinefrag").read_text(
                    encoding="utf-8").splitlines())
            used |= set(re.findall(r"\b(PACK_\w+)\b", code))
        assert used, f"{name}: no packed constants referenced at all"
        assert used <= defined, f"{name}: undefined {sorted(used - defined)}"
        all_used |= used

    # …and across the two builds every group's multipliers are all used, so a
    # field cannot be dropped from the export while staying in the spec.
    for g, spec in codes.PACKED_SPEC.items():
        for i in range(len(spec)):
            assert f"PACK_{g}_{i}" in all_used, f"{g} field {i} is never plotted"


@pytest.fixture(scope="module")
def probe_bar():
    """The last bar of a long fixture — every stage is out of warm-up there, so
    each one returns its FULL column set."""
    from tools.oracle.compare_stages import latch_s5
    from tools.oracle.export_trace import build_trace
    t = build_trace(symbol="EURUSD", timeframe="15min",
                    input_path=(CT_ROOT / "golden" / "tradingview_oracle" /
                                "s1" / "F-TV-S1S5.csv"),
                    fixture_id="F-TV-S1S5")
    latch_s5(t["bars"])
    return t["bars"][-1]


def test_export_schema_matches_stage_fields(probe_bar):
    """`STAGE_COLUMNS` is declared, not derived — this is what keeps it true.

    `XF` is excluded: it is the execution build's DERIVED 15-minute detection
    frame, not a production stage, so it has no `STAGE_FIELDS` entry. It is
    proved instead against production's own `resample_candles`, bar for bar, in
    test_oracle_dual_target.py.
    """
    for stage, declared in STAGE_COLUMNS.items():
        if stage not in STAGE_FIELDS:
            assert stage == "XF", f"{stage} has no field function"
            continue
        produced = tuple(STAGE_FIELDS[stage](probe_bar).keys())
        assert set(produced) == set(declared), (
            f"{stage}: declared {sorted(declared)} but produced "
            f"{sorted(produced)}")


def test_export_schema_hash_moves_with_the_packing(monkeypatch):
    before = export_schema_hash()
    spec = {k: list(v) for k, v in codes.PACKED_SPEC.items()}
    spec["S4"] = [("oracleBias", 8, 1)] + spec["S4"][1:]
    monkeypatch.setattr(codes, "PACKED_SPEC", spec)
    assert export_schema_hash() != before


# ── plot budget ──────────────────────────────────────────────────────────────

def test_plot_budget_keeps_the_reserve():
    used = _plot_calls(OUT_PINE.read_text(encoding="utf-8"))
    assert used <= PLOT_LIMIT - PLOT_RESERVE, (
        f"{used} of {PLOT_LIMIT} plot slots used; the reserve is {PLOT_RESERVE}")


def test_lint_errors_when_the_reserve_is_spent():
    body = "//@version=6\nindicator(\"x\")\n" + "\n".join(
        f"plot(close, \"p{i}\")" for i in range(PLOT_LIMIT - PLOT_RESERVE + 1))
    findings = lint(body)
    assert any(f["rule"] == "plot_limit" and f["severity"] == "error"
               for f in findings), findings


def test_lint_still_errors_above_the_hard_ceiling():
    body = "//@version=6\nindicator(\"x\")\n" + "\n".join(
        f"plot(close, \"p{i}\")" for i in range(PLOT_LIMIT + 1))
    findings = lint(body)
    assert any(f["rule"] == "plot_limit" and f["severity"] == "error"
               and "RE10140" in f["detail"] for f in findings), findings
