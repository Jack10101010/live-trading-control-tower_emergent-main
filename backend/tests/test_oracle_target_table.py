"""The production target table, and the resolver the chart will mirror.

The load-bearing test is `test_resolver_matches_production_for_every_cell`: the
chart is about to draw a target for every live setup, and if the resolution is
wrong the geometry is wrong everywhere. So all 144 addressable cells are checked
against production's own branch, plus the cases production handles by NOT looking
anything up — unknown state, unconfirmed state, a key the scenario never covers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

CT_ROOT = Path(__file__).resolve().parents[2]
if str(CT_ROOT) not in sys.path:
    sys.path.insert(0, str(CT_ROOT))

from tools.oracle import target_table as tt  # noqa: E402
from tools.oracle.engine_access import (_in_lux, load_engine,  # noqa: E402
                                        resolve_config)


@pytest.fixture(scope="module")
def engine():
    return load_engine(require_pin=True)


@pytest.fixture(scope="module")
def cfg(engine):
    return resolve_config(engine)[0]


@pytest.fixture(scope="module")
def table(engine, cfg):
    return tt.build(engine, cfg)


@pytest.fixture(scope="module")
def cohort_index(engine, cfg):
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.scenario import _build_cohort_index
        return _build_cohort_index(
            getattr(cfg, "session_strategy_scenario", None))


# ══ dimensions ═══════════════════════════════════════════════════════════════

def test_the_lookup_key_is_session_structure_direction(table, cohort_index):
    """Three dimensions in the key; the market state is a sparse OVERRIDE on top
    of the cell it selects, not a fourth axis of the key."""
    assert cohort_index, "no cohort index — the chart would have no table"
    for key in cohort_index:
        assert len(key) == 3, key
        session, structure, direction = key
        assert session in table["sessions"]
        assert structure in table["structures"]
        assert direction in table["directions"]


def test_the_session_vocabulary_is_the_cohort_one_not_s1s(table, engine):
    """`_cohort_session_key` is `_session_for_hour(hour)[1]`, which is defined
    separately from the S1 session keys. They overlap, and conflating them would
    silently mis-key every lookup."""
    with _in_lux(Path(engine.lux_root)):
        from strategy_core.sessions import _session_for_hour
        expected = sorted({_session_for_hour(h)[1] for h in range(24)})
    assert table["sessions"] == expected


def test_counts_are_derived_not_asserted(table):
    c = table["counts"]
    assert c["base_cells"] == len(table["sessions"]) * 2 * 2
    assert c["addressable_cells"] == c["base_cells"] * len(table["market_states"])
    assert c["state_target_overrides"] == sum(
        1 for row in table["state_rr"] for v in row if v is not None)
    assert c["cells_with_a_defined_target"] == (
        c["base_cells_present"] + c["state_target_overrides"])


def test_every_base_cell_has_a_target(table):
    for cell in table["base"]:
        assert cell["target_rr"] is not None and cell["target_rr"] > 0, cell


# ══ the resolver, against production ═════════════════════════════════════════

def _production_rr(rule, state, confirmed, global_rr):
    """Production's own branch, transcribed from execution.py's fill gate."""
    if rule is None:
        return global_rr, True
    allowed = bool(rule.get("enabled"))
    state_rr = None
    if state and confirmed:
        elig = (rule.get("elig_states") or {}).get(state)
        if elig == "allow":
            allowed = True
        elif elig == "block":
            allowed = False
        ov = (rule.get("state_overrides") or {}).get(state)
        if ov is not None and ov.get("mode") == "custom":
            state_rr = ov["rr"]
    rr = state_rr if state_rr is not None else rule.get("target_rr")
    return (float(rr) if rr is not None else float(global_rr)), allowed


def test_resolver_matches_production_for_every_cell(table, cohort_index):
    """All 144 addressable cells, both confirmed and unconfirmed."""
    checked = 0
    for session in table["sessions"]:
        for structure in table["structures"]:
            for direction in table["directions"]:
                rule = cohort_index.get((session, structure, direction))
                for state in table["market_states"]:
                    for confirmed in (True, False):
                        rr, _src, allowed, _why = tt.resolve(
                            table, session, structure, direction, state,
                            confirmed)
                        want_rr, want_ok = _production_rr(
                            rule, state, confirmed, table["global_rr"])
                        assert rr == pytest.approx(want_rr), (
                            session, structure, direction, state, confirmed)
                        assert allowed == want_ok, (
                            session, structure, direction, state, confirmed)
                        checked += 1
    assert checked == table["counts"]["addressable_cells"] * 2


def test_an_unconfirmed_state_uses_the_base_target(table):
    """Production only lets a CONFIRMED state move the target. Reading an
    unconfirmed one would apply a rule production did not."""
    for cell_i, cell in enumerate(table["base"]):
        row = table["state_rr"][cell_i]
        state = next((table["market_states"][j] for j, v in enumerate(row)
                      if v is not None and v != cell["target_rr"]), None)
        if state is None:
            continue
        rr, source, _ok, _why = tt.resolve(
            table, cell["session"], cell["structure"], cell["direction"],
            state, confirmed=False)
        assert rr == cell["target_rr"] and source == "BASE"
        return
    pytest.skip("no state override differs from its base")


def test_an_unknown_state_uses_the_base_target_and_never_blocks(table):
    cell = table["base"][0]
    rr, source, allowed, _ = tt.resolve(
        table, cell["session"], cell["structure"], cell["direction"],
        "No/SuchState", confirmed=True)
    assert rr == cell["target_rr"] and source == "BASE" and allowed


def test_a_missing_state_falls_back_rather_than_blocking(table):
    """Warm-up produces no state at all. Production allows and uses the base."""
    cell = table["base"][0]
    rr, source, allowed, _ = tt.resolve(
        table, cell["session"], cell["structure"], cell["direction"],
        None, confirmed=False)
    assert rr == cell["target_rr"] and source == "BASE" and allowed


def test_an_unknown_key_is_reported_not_defaulted(table):
    rr, source, _ok, _why = tt.resolve(table, "atlantis", "BOS", "Long")
    assert source == "NO_CELL" and rr is None, \
        "a key the table does not know must fail visibly, not pick a default"


def test_state_eligibility_blocks_are_carried(table):
    blocks = [(i, j) for i, row in enumerate(table["state_elig"])
              for j, v in enumerate(row) if v == "block"]
    assert blocks, "the scenario has state blocks; none survived extraction"
    i, j = blocks[0]
    cell = table["base"][i]
    _rr, _src, allowed, why = tt.resolve(
        table, cell["session"], cell["structure"], cell["direction"],
        table["market_states"][j], confirmed=True)
    assert not allowed and why == "state_target_block"


# ══ determinism and diffing ══════════════════════════════════════════════════

def test_the_table_is_deterministic(engine, cfg):
    a, b = tt.build(engine, cfg), tt.build(engine, cfg)
    assert a == b
    assert a["config_hash"] == b["config_hash"]


def test_the_hash_covers_content_not_metadata(table):
    changed = dict(table, counts={}, missing_keys=[], _note="different")
    assert tt.content_hash(changed) == table["config_hash"]


def test_the_hash_moves_when_a_target_moves(table):
    changed = json.loads(json.dumps(table))
    changed["base"][0]["target_rr"] = changed["base"][0]["target_rr"] + 0.25
    assert tt.content_hash(changed) != table["config_hash"]


def test_the_diff_names_the_cell_and_both_values(table):
    changed = json.loads(json.dumps(table))
    changed["base"][0]["target_rr"] = 9.5
    changed["state_rr"][1][0] = 7.25
    lines = tt.diff(table, changed)
    assert any("9.5R" in l and table["base"][0]["session"] in l for l in lines)
    assert any("7.25R" in l for l in lines)


# ══ what reaches Pine ════════════════════════════════════════════════════════

SRC = CT_ROOT / "pine" / "src"


def _frag(name):
    return (SRC / f"{name}.pinefrag").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def generated():
    from tools.oracle.generate_pine import build_generated_targets
    return build_generated_targets()


def test_the_generated_table_carries_every_cell(table, generated):
    import re
    c = table["counts"]
    for name, n in (("TGT_BASE_RR", c["base_cells"]),
                    ("TGT_BASE_ON", c["base_cells"]),
                    ("TGT_BASE_PRESENT", c["base_cells"]),
                    ("TGT_STATE_RR", c["addressable_cells"]),
                    ("TGT_STATE_ELIG", c["addressable_cells"])):
        m = re.search(rf"{name} = array\.from\(([^)]*)\)", generated)
        assert m, f"{name} missing"
        assert len(m.group(1).split(",")) == n, f"{name} has the wrong length"
    assert f'TGT_CONFIG_HASH    = "{table["config_hash"][:16]}"' in generated


def test_the_flattening_matches_the_python_index(table, generated):
    """Pine indexes `(session * 2 + structure) * 2 + direction`. If the two
    orderings disagree every target is drawn from the wrong cell."""
    import re
    rr = [float(x) for x in
          re.search(r"TGT_BASE_RR = array\.from\(([^)]*)\)",
                    generated).group(1).split(",")]
    for cell in table["base"]:
        i = tt.cell_index(table, cell["session"], cell["structure"],
                          cell["direction"])
        assert rr[i] == pytest.approx(cell["target_rr"]), cell


def test_a_missing_table_refuses_rather_than_defaulting(monkeypatch, tmp_path):
    """The one behaviour that must never regress: no generic RR fallback."""
    from tools.oracle import generate_pine as gp
    monkeypatch.setattr("tools.oracle.target_table.DEFAULT_OUT",
                        tmp_path / "absent.json")
    block = gp.build_generated_targets()
    assert "TGT_PRESENT        = false" in block
    assert "NO TABLE" in block


def test_pine_never_falls_back_to_a_generic_rr():
    body = _frag("68_live_setups")
    assert "NO TABLE" in body and "NO CELL" in body
    # The ONLY place a global RR may be used is the explicitly-reported
    # no-cohort-rule case, and it carries its own source code.
    assert body.count("TGT_GLOBAL_RR") == 1
    assert "LS_SRC_GLOBAL" in body


def test_pine_only_honours_a_confirmed_state():
    body = _frag("68_live_setups")
    assert "s6_stateCode > 0 and s6_confirmed" in body, \
        "an unconfirmed state must not move eligibility or the target"
    assert "s6_confirmed = s6_validity == CODE_REGIME_VALID" in _frag("66_regime")


def test_override_precedence_is_most_specific_last():
    """Later assignments win in the Pine helper, so the ORDER of the tiers is
    the precedence. Cell must be last."""
    body = _frag("68_live_setups")
    block = body[body.index("f_applyOverrides"):body.index("f_ovName")]
    order = [block.index(t) for t in
             ("LS_OV_GLOBAL", "LS_OV_SESSION", "LS_OV_STATE", "LS_OV_CELL")]
    assert order == sorted(order), \
        "global < session < state < cell, so the most specific wins"


def test_overrides_are_off_by_default_and_visual_only():
    inputs = _frag("10_inputs")
    import re
    mode = re.search(r'i_ovMode\s+=\s+input\.string\("([^"]+)"', inputs).group(1)
    assert mode == "Production defaults"
    for name in ("i_ovGlobal", "i_ovSession", "i_ovState", "i_ovCell"):
        default = re.search(rf"{name}\s+=\s+input\.float\((\d+)", inputs)
        assert default and default.group(1) == "0", f"{name} must default to 0"
    # …and nothing in the override path writes anywhere but a drawing.
    body = _frag("68_live_setups")
    assert "request." not in body and "strategy." not in body


def test_the_linter_catches_a_global_assignment_inside_a_function():
    """CE10088. It is a COMPILE error, so it never reaches a chart — but it also
    never reached this linter, and it cost a paste-and-fail cycle. Reproduced
    with the exact shape that failed: a function recording its verdict into
    module-level `var`s."""
    from tools.oracle.lint_pine import lint
    bad = ('//@version=6\nindicator("x")\nvar float g = na\n'
           'f_thing(int a) =>\n    g := a * 2\n    g\nplot(f_thing(1))\n')
    hits = [f for f in lint(bad) if f["rule"] == "global_assign_in_function"]
    assert hits and hits[0]["severity"] == "error"
    assert "CE10088" in hits[0]["detail"]


def test_the_linter_permits_mutating_a_global_collection():
    """`array.push` on a global is legal and is how every retention buffer in
    this build works. A rule that flagged it would be unusable."""
    from tools.oracle.lint_pine import lint
    good = ('//@version=6\nindicator("x")\n'
            'var array<float> arr = array.new<float>()\n'
            'f_keep(float v) =>\n    array.push(arr, v)\n'
            '    if array.size(arr) > 10\n        array.shift(arr)\n'
            'f_keep(close)\n')
    assert not [f for f in lint(good)
                if f["rule"] == "global_assign_in_function"]


@pytest.mark.parametrize("name,body", [
    ("missing return",
     'f_two(int a) =>\n    b = a * 2\n    label.new(bar_index, close, "x")\n'
     '[p, q] = f_two(1)\nplot(p)\n'),
    ("wrong arity",
     'f_two(int a) =>\n    b = a * 2\n    [b, b]\n[p, q, r] = f_two(1)\nplot(p)\n'),
    ("return nested in an if",
     'f_two(int a) =>\n    b = a * 2\n    if b > 0\n        [b, b]\n'
     '[p, q] = f_two(1)\nplot(p)\n'),
])
def test_the_linter_catches_tuple_arity_mistakes(name, body):
    """CE10172. The nested case is the one a reader is most likely to write: the
    tuple IS the last line of the body, but one level deeper, so it is not the
    function's return."""
    from tools.oracle.lint_pine import lint
    src = '//@version=6\nindicator("x")\n' + body
    hits = [f for f in lint(src) if f["rule"] == "tuple_arity"]
    assert hits, name
    assert "CE10172" in hits[0]["detail"]


@pytest.mark.parametrize("body", [
    'f_two(int a) =>\n    b = a * 2\n    [b, b + 1]\n[p, q] = f_two(1)\nplot(p)\n',
    'f_two(int a) =>\n    [math.max(a, 1), math.min(a, 2)]\n'
    '[p, q] = f_two(1)\nplot(p)\n',
    'f_two(int a) =>\n    label.new(bar_index, close, "x")\n    [a, a + 1]\n'
    '[p, q] = f_two(1)\nplot(p)\n',
])
def test_the_tuple_rule_has_no_false_positives(body):
    """Nested commas inside a call must not inflate the arity, and drawing
    before the return is exactly what the live-setup function does."""
    from tools.oracle.lint_pine import lint
    src = '//@version=6\nindicator("x")\n' + body
    assert not [f for f in lint(src) if f["rule"] == "tuple_arity"]


def test_creation_records_structure_only():
    """THE semantic regression guard. `f_liveCreate` must not resolve a cell, a
    session, a market state or a target: none of those are decided when a block
    is detected, and writing them at creation is what made the label claim a
    future execution decision it could not know."""
    body = _frag("68_live_setups")
    fn = body[body.index("f_liveCreate(bool isLong"):
              body.index("f_lsOriginBar() =>")]
    for banned in ("f_resolveCell", "f_applyOverrides", "sessKey",
                   "s6_stateCode", "f_newsState"):
        assert banned not in fn, (
            f"{banned} at creation time — session, state, news and target are "
            "FILL-TIME quantities (execution.py:2882/2950/2965)")
    assert "target = na" in fn, "a freshly detected block has no target yet"
    assert "phase = LS_PH_RESTING" in fn


def test_the_preview_is_recomputed_from_the_current_bar():
    """Option A: the resting preview reads the CURRENT session and state every
    time it is painted, not the ones captured at detection."""
    body = _frag("68_live_setups")
    loop = body[body.index("if CTX_OK and barClosed and i_showLive and array.size(lo_all) > 0"):]
    assert "f_resolveCell(ls_nowSessIdx, o.isChoch," in loop
    assert "ls_nowSessIdx = f_cohortSessionIdx()" in body
    assert "useSess := nowSess" in loop and "useState := nowState" in loop


def test_the_bar_scoped_context_is_hoisted_out_of_the_per_block_loop():
    """News walks NEWS_COUNT windows. Doing that per block would be that loop
    times the number of resting blocks, for an answer that cannot differ."""
    body = _frag("68_live_setups")
    hoist = body.index("ls_nowNews    = CTX_OK ? f_newsState(time, time_close)")
    loop = body.index("if CTX_OK and barClosed and i_showLive and array.size(lo_all) > 0")
    assert hoist < loop
    assert "f_newsState(" not in body[loop:],         "the news scan must not run inside the per-block loop"


def test_the_context_is_frozen_at_the_trigger_and_never_recomputed():
    body = _frag("68_live_setups")
    assert "o.finalSess := nowSess" in body
    assert "o.finalState := nowState" in body
    assert "o.finalRr := eRr" in body
    assert "o.finalNews := ls_nowNews" in body
    # …and the frozen branch reads the stored values, never the live ones.
    assert "useRr := o.finalRr" in body and "useSess := o.finalSess" in body


def test_the_risk_reward_tool_is_anchored_to_the_block_end():
    body = _frag("68_live_setups")
    assert "x0 = box.get_right(o.obBox)" in body,         "the RR tool hangs off the block's right edge, not its origin"
    assert "x1 = x0 + i_rrWidthBars" in body, "…with a fixed width"


def test_the_live_label_separates_context_from_decision():
    """Two lines: WHEN the statement applies and under what context, then the
    decision. A single line cannot distinguish 'would trade' from 'did trade'."""
    body = _frag("68_live_setups")
    assert 'head := (o.phase == LS_PH_ARMED ? "ARMED · " : "NOW · ")' in body
    assert 'head := "FINAL · " + sess + " · " + state' in body
    assert r'head + "\n" + body' in body


def test_labels_sit_clear_of_the_candles():
    """Bearish blocks are approached from BELOW, so their label goes ABOVE the
    box; bullish blocks the other way. Otherwise the label sits in the path of
    the price action that is about to arrive."""
    body = _frag("68_live_setups")
    assert "tag = label.new(obIdx, isLong ? obBottom : obTop" in body
    assert "style = isLong ? label.style_label_up : label.style_label_down" in body


def test_replay_risk_reward_lines_are_solid():
    """A completed setup's levels are facts. Broken edges read as uncertainty,
    which is the opposite of what they are."""
    assert "line.style_dashed" not in _frag("67_replay_visuals")


def test_no_risk_reward_line_is_broken_in_either_layer():
    """A broken edge reads as uncertainty about the LEVEL, and the level is
    never the uncertain part — it is arithmetic. Provisional-vs-final is carried
    by transparency instead. The only dashed style left in the live layer is the
    DOTTED arm level, which is a different thing from a different parameter."""
    for frag in ("67_replay_visuals", "68_live_setups"):
        assert "line.style_dashed" not in _frag(frag), frag
    assert "style = line.style_dotted" in _frag("68_live_setups"),         "the arm level keeps its dotted style - it is not an RR line"


def test_break_chips_can_be_turned_off_leaving_the_lines():
    body = _frag("59_structure_visuals")
    assert "if i_showStructLabels" in body
    line_at = body.index("line.new(x0, s4_broken_level")
    chip_at = body.index("if i_showStructLabels")
    assert line_at < chip_at, "the line must draw regardless of the chip toggle"


def test_static_boxes_step_aside_for_the_live_layer():
    """Two boxes at the same levels — one static, one lifecycle — is what made
    the first version unreadable. But "Chart-derived" must bring them BACK: it
    suppresses the replay layer's boxes, so without this the setting was one
    that only ever removed boxes."""
    body = _frag("10_inputs")
    assert 'f_drawChartObs() =>\n    i_obSource == "Chart-derived" or not i_showLive' \
        in body


@pytest.mark.parametrize("name,body", [
    ("never declared", "ghost := 5\nplot(close)\n"),
    ("declared later", "later := 5\nvar int later = 0\nplot(close)\n"),
])
def test_the_linter_catches_assignment_before_declaration(name, body):
    """CE10272. Pine resolves top to bottom, and assembling a script from
    fragments makes this easy to hit: a rewrite replaced the block holding six
    `var` declarations while the code assigning them survived."""
    from tools.oracle.lint_pine import lint
    hits = [f for f in lint('//@version=6\nindicator("x")\n' + body)
            if f["rule"] == "assign_before_declare"]
    assert hits, name
    assert "CE10272" in hits[0]["detail"]


@pytest.mark.parametrize("body", [
    "var int a = 0\na := 5\nplot(close)\n",
    "b = 0\nb := 5\nplot(close)\n",
    "var float c = na\nc := 1.5\nplot(c)\n",
    "f_t() =>\n    [1, 2]\n[d, e] = f_t()\nd := 9\nplot(d)\n",
    "var int t = 0\nfor i = 0 to 3\n    t := i\nplot(t)\n",
])
def test_the_declaration_rule_has_no_false_positives(body):
    """Tuple destructuring, loop variables and function parameters all declare
    names without a `var` keyword."""
    from tools.oracle.lint_pine import lint
    assert not [f for f in lint('//@version=6\nindicator("x")\n' + body)
                if f["rule"] == "assign_before_declare"]


def test_the_hud_state_is_declared_before_it_is_assigned():
    """The six values the HUD reads. Their declarations were removed by the
    lifecycle rewrite while the assignments survived — CE10272."""
    import re
    body = _frag("68_live_setups")
    for name in ("ls_lastRr", "ls_lastOvSrc", "ls_lastSrc", "ls_lastKey",
                 "ls_lastOk", "ls_lastWhy"):
        decl = re.search(rf"^var\s+\w+\s+{name}\s+=", body, re.M)
        assert decl, f"{name} is assigned but never declared"
        assign = re.search(rf"^\s+{name}\s+:=", body, re.M)
        assert assign, f"{name} is declared but never assigned"
        assert decl.start() < assign.start(), \
            f"{name} is assigned before it is declared"


def test_no_function_assigns_a_global_scalar():
    """CE10088. Every `ls_last*` write must happen at TOP LEVEL — inside the
    per-bar loop — never inside a function body."""
    body = _frag("68_live_setups")
    fn_start = body.index("f_liveCreate(bool isLong")
    fn_end = body.index("f_lsOriginBar()")
    assert "ls_last" not in body[fn_start:fn_end], \
        "creation must not write the HUD globals from inside a function"
    assert "                ls_lastRr := useRr" in body, \
        "the loop, at global scope, is where the assignment belongs"


def test_the_live_and_replay_layers_are_independently_toggled():
    inputs = _frag("10_inputs")
    assert "i_showLive" in inputs and "i_showReplay" in inputs
    live = _frag("68_live_setups")
    assert "RP_COUNT" not in live, \
        "live setups must not depend on the replay recording in any way"


def test_the_committed_table_matches_the_deployed_engine(table):
    """The build embeds what is on disk; if that has drifted from production the
    chart would draw targets the bot no longer uses."""
    path = tt.DEFAULT_OUT
    if not path.is_file():
        pytest.skip("no table written yet — run target_table --write")
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["config_hash"] == table["config_hash"], (
        "artifacts/tradingview_oracle/target_table.json is stale — "
        "run `python -m tools.oracle.update_detection_visual --write`")
