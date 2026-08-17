"""No two scripts that share a chart may claim the same table cell.

THE DEFECT THIS EXISTS FOR. The Visual Oracle's HUD sits at
`position.top_right`. The Strategy Companion's diagnostics panel was put there
too. TradingView does not tile tables — two in one cell simply overlap, the
later drawn one wins, and neither script reports anything. The symptom was a
diagnostics panel that had "disappeared" while the strategy carried on trading
normally, with its own show/hide input having no effect because the table was
being drawn the whole time, underneath the oracle's.

Nothing in the language, the generator or the linter had an opinion about it.

WHAT THIS CHECKS, AND WHAT IT DOES NOT.

It reads the GENERATED builds — the artefacts that actually run — resolves the
default position of every table each one creates, and fails if two builds that
can be loaded on the same chart share one. "Same chart" means the same declared
timeframe: `detection_15m` and `strategy_companion` are both 15-minute and are
designed to be used together; `execution_1m` cannot be on that chart at all.

It deliberately does NOT check for two tables colliding WITHIN one build. The
detection oracle already has such a pair (`70_legend` and `90_debug`, both
`bottom_left`), both optional and both under the reader's own control, and
folding a pre-existing condition into this test would mean shipping it failing
or shipping it with an allowlist that rots. That pair is reported as a finding,
not silently adopted here.

IT IS NOT TAUTOLOGICAL. It does not restate a constant from the source; it
resolves `table.new(...)`'s first argument, following a helper function and its
input's default option where the position is user-selectable. A shape it cannot
resolve is an ERROR, not a pass — otherwise the check would quietly evaporate
the first time someone writes the call differently.
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle.generate_pine import BUILD_TARGETS  # noqa: E402

POS = re.compile(r"position\.(\w+)")


class Unresolvable(RuntimeError):
    """A `table.new` whose position this parser cannot follow.

    Raised rather than skipped. A resolver that shrugs at an unfamiliar shape
    turns a real check into a check of whatever it happens to understand.
    """


def _strip(src: str) -> list[str]:
    return [l.split("//")[0] for l in src.splitlines()]


def _first_arg(line: str, after: int) -> str:
    """The first argument of the call opening at `after`, bracket-aware."""
    depth, out, i = 0, [], line.index("(", after)
    for ch in line[i:]:
        if ch == "(":
            depth += 1
            if depth == 1:
                continue
        elif ch == ")":
            depth -= 1
            if depth == 0:
                break
        elif ch == "," and depth == 1:
            break
        out.append(ch)
    return "".join(out).strip()


def _resolve_helper(lines: list[str], fn: str) -> str:
    """Default `position.*` of a helper like `f_panelPos()`.

    The default is whichever branch tests the input's OWN default option, so
    this follows: helper body -> the input variable it compares -> that
    variable's `input.string` defval -> the branch guarded by it.
    """
    body, seen = [], False
    for l in lines:
        if re.match(rf"\s*{re.escape(fn)}\s*\(\s*\)\s*=>", l):
            seen = True
            continue
        if seen:
            if l.strip() and not l.startswith((" ", "\t")):
                break
            body.append(l)
    if not body:
        raise Unresolvable(f"no body found for helper {fn}()")

    cmp = re.search(r"(\w+)\s*==\s*\"", "\n".join(body))
    if not cmp:
        raise Unresolvable(f"{fn}() compares no input variable")
    var = cmp.group(1)

    dv = re.search(rf"{re.escape(var)}\s*=\s*input\.string\s*\(\s*\"([^\"]*)\"",
                   "\n".join(lines))
    if not dv:
        raise Unresolvable(f"no input.string default found for {var}")
    default = dv.group(1)

    for i, l in enumerate(body):
        if f'{var} == "{default}"' in l:
            for nxt in body[i + 1:i + 3]:
                m = POS.search(nxt)
                if m:
                    return m.group(1)
    raise Unresolvable(
        f"{fn}() has no branch for its own default option {default!r} — the "
        f"default position cannot be determined by reading the code")


def table_positions(src: str) -> list[str]:
    lines = _strip(src)
    found = []
    for n, l in enumerate(lines):
        for m in re.finditer(r"table\.new\s*\(", l):
            arg = _first_arg(l, m.start())
            if not arg:
                raise Unresolvable(f"line {n + 1}: empty table.new position")
            direct = POS.fullmatch(arg)
            if direct:
                found.append(direct.group(1))
                continue
            call = re.fullmatch(r"(\w+)\s*\(\s*\)", arg)
            if call:
                found.append(_resolve_helper(lines, call.group(1)))
                continue
            raise Unresolvable(
                f"line {n + 1}: cannot resolve table position from {arg!r}")
    return found


def _built():
    return {name: spec for name, spec in BUILD_TARGETS.items()
            if spec["pine"].is_file()}


def test_the_resolver_actually_finds_tables():
    """Guard against the whole check passing on an empty set.

    Every position below is compared for collisions; if the resolver silently
    returned nothing, every comparison would trivially succeed and this module
    would be decoration.
    """
    total = 0
    for name, spec in _built().items():
        pos = table_positions(spec["pine"].read_text(encoding="utf-8"))
        assert pos, f"{name}: resolver found no table at all"
        total += len(pos)
    assert total >= 5, f"only {total} tables resolved across all builds"


def test_the_resolver_refuses_a_shape_it_cannot_follow():
    """The anti-tautology guard. An unrecognised call must raise, not skip."""
    with pytest.raises(Unresolvable):
        table_positions("var table t = table.new(someWildExpr ? a : b, 2, 2)")


def test_the_resolver_follows_a_user_selectable_position():
    """It must read the DEFAULT out of the input, not just literals."""
    src = "\n".join([
        'i_pp = input.string("Middle right", "pos", options = ["Middle right"])',
        "f_pp() =>",
        "    string p = position.top_right",
        '    if i_pp == "Middle right"',
        "        p := position.middle_right",
        "    p",
        "var table t = table.new(f_pp(), 2, 2)",
    ])
    assert table_positions(src) == ["middle_right"]


def test_it_would_have_caught_the_defect_it_was_written_for():
    """The check must fail on the code as it was, or it proves nothing.

    A regression test written after the fix can pass for the wrong reason — it
    can be describing the new arrangement rather than detecting the old fault.
    This replays the exact prior source shape (`table.new(position.top_right`)
    against the real oracle build and asserts the collision is seen.
    """
    built = _built()
    if "detection_15m" not in built:
        pytest.skip("the oracle build must be generated")
    oracle = set(table_positions(
        built["detection_15m"]["pine"].read_text(encoding="utf-8")))
    before = set(table_positions(
        "var table sc_panel = table.new(position.top_right, 2, 32)"))
    assert before == {"top_right"}
    assert oracle & before == {"top_right"}, (
        "the resolver no longer sees the original collision, so the check "
        "below is not evidence of anything")


def test_no_two_coexisting_builds_share_a_table_position():
    """THE REGRESSION. Two scripts on one chart, one visible panel."""
    by_tf = defaultdict(list)
    for name, spec in _built().items():
        pos = set(table_positions(spec["pine"].read_text(encoding="utf-8")))
        by_tf[spec["timeframe"]].append((name, pos))

    checked = 0
    for tf, builds in by_tf.items():
        for i, (a, pa) in enumerate(builds):
            for b, pb in builds[i + 1:]:
                checked += 1
                clash = pa & pb
                assert not clash, (
                    f"{a} and {b} are both {tf} builds — they are meant to sit "
                    f"on the same chart — and both put a table at "
                    f"{sorted(clash)}. TradingView does not tile tables: one "
                    f"covers the other, with no runtime error in either script.")
    assert checked, "no coexisting pair was compared; the check is vacuous"


def test_the_companion_and_the_oracle_are_the_pair_this_protects():
    """Named explicitly, so a future refactor that stops building one of them
    cannot make the check above pass by having nothing left to compare."""
    built = _built()
    if not {"detection_15m", "strategy_companion"} <= set(built):
        pytest.skip("both builds must be generated")
    oracle = set(table_positions(
        built["detection_15m"]["pine"].read_text(encoding="utf-8")))
    comp = set(table_positions(
        built["strategy_companion"]["pine"].read_text(encoding="utf-8")))
    assert "top_right" in oracle, "the oracle HUD moved; re-derive this test"
    assert comp == {"middle_right"}, f"companion panel is at {comp}"
    assert not (oracle & comp)
