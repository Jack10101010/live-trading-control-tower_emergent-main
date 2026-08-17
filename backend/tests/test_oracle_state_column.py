"""The market state's TARGET COLUMN — every cell, both directions.

WHAT WENT WRONG. `s6_stateCode` is a 1-based index into `REGIME_STATE_NAME`,
which the contract derives from `strategy_core.regime.MARKET_STATES`.
`TGT_STATE_RR` and `TGT_STATE_ELIG` are ordered by `TGT_STATE_NAME`, which comes
from `target_table.py`. Nothing required the two to agree and they did not —
they were exact reversals — so `st = s6_stateCode - 1` read the mirrored column
for every confirmed state. 108 of 144 cells returned the wrong RR and 60 the
wrong eligibility, which is how L_2106 rendered 0.70R (the Bull/Expand cell)
where production resolved 1.25R for Bear/Chop.

WHY A TEST THAT BOTH LISTS "CONTAIN THE SAME SIX STRINGS" WOULD NOT HAVE CAUGHT
IT. They did contain the same six strings, in opposite orders, throughout. The
only checks that bite are the ones below: the mapping must round-trip by NAME,
and every addressable cell must agree with production's own name-keyed lookup.

`test_the_old_positional_lookup_would_have_failed_these` keeps the regression
non-vacuous — it recomputes what `s6_stateCode - 1` used to select and asserts
that it disagrees, so these tests cannot quietly start passing for the wrong
reason.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import target_table as tt  # noqa: E402
from tools.oracle.generate_pine import GenerateError, state_column_map  # noqa: E402

GEN = (CT_ROOT / "pine" / "generated"
       / "tradingview_visual_oracle_detection_15m.pine")
SRC = CT_ROOT / "pine" / "src"

pytestmark = pytest.mark.skipif(not GEN.is_file(), reason="oracle not generated")


def _arr(name, cast=str):
    s = GEN.read_text(encoding="utf-8")
    m = re.search(name + r"\s*=\s*array\.from\(([^)]*)\)", s)
    assert m, f"{name} not found in the generated build"
    return [cast(x.strip().strip('"')) for x in m.group(1).split(",")]


@pytest.fixture(scope="module")
def pine():
    return {
        "regime": _arr("REGIME_STATE_NAME"),
        "target": _arr("TGT_STATE_NAME"),
        "map": _arr("REGIME_TO_TGT_STATE_IDX", int),
        "rr": _arr("TGT_STATE_RR", float),
        "elig": _arr("TGT_STATE_ELIG", int),
        "sessions": _arr("TGT_SESSION_NAME"),
        "base_rr": _arr("TGT_BASE_RR", float),
        "base_on": _arr("TGT_BASE_ON", int),
    }


@pytest.fixture(scope="module")
def table():
    from tools.oracle.engine_access import load_engine, resolve_config
    engine = load_engine()
    cfg, _ = resolve_config(engine)
    return tt.build(engine, cfg)


def _cell(pine, sess_i, is_choch, is_long, state):
    """What PINE now resolves, transcribed from `f_cell` + `f_tgtStateCol`."""
    n = len(pine["target"])
    base = (sess_i * 2 + (1 if is_choch else 0)) * 2 + (0 if is_long else 1)
    rr, ok = pine["base_rr"][base], pine["base_on"][base] == 1
    code = pine["regime"].index(state) + 1
    col = pine["map"][code - 1]
    slot = base * n + col
    elig = pine["elig"][slot]
    if elig == 1:
        ok = True
    elif elig == 2:
        ok = False
    ov = pine["rr"][slot]
    if ov >= 0:
        rr = ov
    return rr, ok


# ── the mapping itself ───────────────────────────────────────────────────────

def test_every_state_round_trips_by_name(pine):
    assert len(pine["map"]) == len(pine["regime"]) == len(pine["target"])
    for i, name in enumerate(pine["regime"]):
        col = pine["map"][i]
        assert pine["target"][col] == name, (
            f"regime code {i + 1} ({name}) maps to column {col}, which is "
            f"{pine['target'][col]}")


def test_the_mapping_is_bijective(pine):
    assert sorted(pine["map"]) == list(range(len(pine["target"])))


def test_the_two_vocabularies_really_are_in_different_orders(pine):
    """If they ever become identical the mapping is still correct — but this
    records that positional equality is NOT what makes it work, so nobody
    re-derives `s6_stateCode - 1` from a lucky ordering."""
    if pine["regime"] == pine["target"]:
        pytest.skip("vocabularies now coincide; the mapping remains authority")
    assert set(pine["regime"]) == set(pine["target"])


def test_pine_actually_uses_the_mapping_not_the_raw_index():
    """The array existing proves nothing if the lookup ignores it."""
    for frag, n in (("68_live_setups", 1), ("s90_strategy", 1)):
        body = (SRC / f"{frag}.pinefrag").read_text(encoding="utf-8")
        code = "\n".join(l.split("//")[0] for l in body.splitlines())
        assert code.count("st = f_tgtStateCol(s6_stateCode)") == n, (
            f"{frag} does not resolve the state column through the mapping")
        assert "st = s6_stateCode - 1" not in code, (
            f"{frag} still indexes a target array with a regime index")


def test_the_helper_reads_the_generated_array():
    body = (SRC / "66_regime.pinefrag").read_text(encoding="utf-8")
    assert "f_tgtStateCol(int stateCode) =>" in body
    assert "array.get(REGIME_TO_TGT_STATE_IDX, stateCode - 1)" in body


# ── generation refuses a broken vocabulary ───────────────────────────────────

@pytest.mark.parametrize("regime,target,why", [
    (["A", "B"], ["A"], "target column missing"),
    (["A"], ["A", "B"], "regime state missing"),
    (["A", "A"], ["A", "B"], "duplicate regime name"),
    (["A", "B"], ["A", "A"], "duplicate target name"),
    (["A", "B"], ["A", "C"], "names do not correspond"),
])
def test_generation_refuses_a_vocabulary_it_cannot_relate(regime, target, why):
    """A build that cannot establish the correspondence has no correct column
    to emit. Falling back to the base target would render a plausible wrong RR
    with nothing to indicate it."""
    with pytest.raises(GenerateError):
        state_column_map(regime, target)


def test_generation_accepts_a_reordered_vocabulary():
    assert state_column_map(["a", "b", "c"], ["c", "b", "a"]) == [2, 1, 0]


# ── L_2106 ───────────────────────────────────────────────────────────────────

L2106 = dict(session="outside", is_choch=False, is_long=True,
             state="Bear/Chop", entry=1.15234, stop=1.15166)


def test_L_2106_resolves_1_25_and_not_0_70(pine, table):
    """The incident. Long BOS, Outside, Bear/Chop — production resolved 1.25 and
    the chart rendered 0.70, which is the Bull/Expand cell of the same cohort."""
    rr, ok = _cell(pine, pine["sessions"].index(L2106["session"]),
                   L2106["is_choch"], L2106["is_long"], L2106["state"])
    assert rr == 1.25, f"Pine resolves {rr}, production resolves 1.25"
    assert ok is True
    prod, _src, pok, _why = tt.resolve(table, "outside", "BOS", "Long",
                                       "Bear/Chop", True)
    assert (rr, ok) == (prod, pok)


def test_L_2106_target_price(pine):
    rr, _ok = _cell(pine, pine["sessions"].index("outside"), False, True,
                    "Bear/Chop")
    risk = L2106["entry"] - L2106["stop"]
    assert round(L2106["entry"] + risk * rr, 5) == 1.15319


def test_the_old_positional_lookup_would_have_failed_these(pine):
    """NON-VACUOUS. Recompute what `s6_stateCode - 1` selected and prove it
    disagrees — otherwise the tests above could pass for the wrong reason."""
    n = len(pine["target"])
    base = (pine["sessions"].index("outside") * 2 + 0) * 2 + 0
    old_col = pine["regime"].index("Bear/Chop")          # the raw regime index
    assert pine["rr"][base * n + old_col] == 0.7, (
        "the old lookup no longer reproduces 0.70 — re-derive this test")
    assert pine["target"][old_col] == "Bull/Expand"


# ── the full matrix ──────────────────────────────────────────────────────────

def _all_cells(pine):
    for si, sess in enumerate(pine["sessions"]):
        for is_choch in (False, True):
            for is_long in (True, False):
                for state in pine["regime"]:
                    yield si, sess, is_choch, is_long, state


def test_all_144_cells_match_production_on_rr_and_eligibility(pine, table):
    """Deterministic table lookup — exact, not 'substantially improved'."""
    rr_bad, elig_bad, total = [], [], 0
    for si, sess, is_choch, is_long, state in _all_cells(pine):
        total += 1
        prr, pok = _cell(pine, si, is_choch, is_long, state)
        arr_, _src, aok, _why = tt.resolve(
            table, sess, "CHoCH" if is_choch else "BOS",
            "Long" if is_long else "Short", state, True)
        key = f"{sess}/{'CHoCH' if is_choch else 'BOS'}/" \
              f"{'Long' if is_long else 'Short'}/{state}"
        if prr != arr_:
            rr_bad.append(f"{key}: pine {prr} vs production {arr_}")
        if pok != aok:
            elig_bad.append(f"{key}: pine {pok} vs production {aok}")
    assert total == 144, f"expected 144 addressable cells, walked {total}"
    assert not rr_bad, f"{len(rr_bad)} RR mismatches:\n  " + "\n  ".join(rr_bad[:12])
    assert not elig_bad, (f"{len(elig_bad)} eligibility mismatches:\n  "
                          + "\n  ".join(elig_bad[:12]))


def test_the_old_lookup_mismatched_a_measurable_number_of_cells(pine, table):
    """Records the blast radius, and fails if the fix ever silently reverts to
    something that was never broken in the first place."""
    n = len(pine["target"])
    rr_bad = elig_bad = 0
    for si, sess, is_choch, is_long, state in _all_cells(pine):
        base = (si * 2 + (1 if is_choch else 0)) * 2 + (0 if is_long else 1)
        old = base * n + pine["regime"].index(state)      # the faulty column
        new = base * n + pine["map"][pine["regime"].index(state)]
        rr_bad += pine["rr"][old] != pine["rr"][new]
        elig_bad += pine["elig"][old] != pine["elig"][new]
    assert rr_bad == 108, f"expected 108 wrong RR cells, measured {rr_bad}"
    assert elig_bad == 60, f"expected 60 wrong eligibility cells, got {elig_bad}"


def test_every_distinct_rr_in_the_matrix_is_exercised(pine, table):
    """Not just L_2106's 1.25 — the parity walk must actually touch the
    matrix's whole range of targets."""
    seen = set()
    for si, _s, is_choch, is_long, state in _all_cells(pine):
        seen.add(_cell(pine, si, is_choch, is_long, state)[0])
    assert len(seen) >= 6, f"only {len(seen)} distinct RRs exercised: {seen}"


# ── the unconfirmed path must not regress ────────────────────────────────────

def test_an_unconfirmed_state_keeps_the_base_cell(pine, table):
    """`f_cell` gates the override on `s6_stateCode > 0 and s6_confirmed`. An
    unknown or warming-up state keeps the cohort's base target and never
    blocks — that is production behaviour and this fix does not touch it."""
    for si, sess in enumerate(pine["sessions"]):
        base = (si * 2) * 2
        prod, _src, pok, _why = tt.resolve(table, sess, "BOS", "Long",
                                           None, False)
        assert prod == pine["base_rr"][base]
        assert pok == (pine["base_on"][base] == 1)


def test_a_zero_state_code_resolves_to_no_column():
    """`f_tgtStateCol(0)` must return -1, not index the array at -1."""
    body = (SRC / "66_regime.pinefrag").read_text(encoding="utf-8")
    assert "int col = -1" in body
    assert "if stateCode > 0 and stateCode - 1 < array.size(" in body
