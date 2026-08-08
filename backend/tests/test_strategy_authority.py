"""M-STRATEGY-AUTHORITY-1 — the cohort x market-state matrix IS the strategy.

These tests pin the strategy specification itself, so it can never again be
inferred from research directories. They assert that:

  * the production resolver (`strategy_core.scenario._build_cohort_index` plus
    the target/eligibility resolution in `strategy_core.execution`) agrees
    cell-for-cell with an INDEPENDENT read of the config, and
  * the committed canonical matrix artefact matches both.

Deliberately NOT asserted: that any particular cell has any particular value.
The matrix is the operator's strategy; these tests protect its FIDELITY, not
its content, so re-deriving cells does not require editing tests.
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent.parent
LUX = REPO.parent / "Lux-OB-Backtester"
if str(LUX) not in sys.path:
    sys.path.insert(0, str(LUX))

MATRIX = REPO / "artifacts" / "strategy-authority" / "current-effective-matrix.csv"
GOLDEN_CONFIG = LUX / "generated_configs" / "d6cdae589b1e4c37a67763253c466067.json"

pytestmark = pytest.mark.skipif(
    not (LUX.exists() and GOLDEN_CONFIG.exists()),
    reason="pinned Lux tree / golden config not present")

STATES = ["Bear/Chop", "Bear/Compress", "Bear/Expand",
          "Bull/Chop", "Bull/Compress", "Bull/Expand"]


def _scenario() -> dict:
    return json.loads(GOLDEN_CONFIG.read_text(encoding="utf-8"))["session_strategy_scenario"]


def _independent_read() -> dict:
    """Re-derive every cell straight from raw JSON, touching no production code."""
    out = {}
    for c in _scenario()["cohorts"]:
        e = c.get("eligibility") or {}
        base = e.get("base", "allow" if c.get("enabled", True) else "disable") == "allow"
        for st in STATES:
            sv = (e.get("states") or {}).get(st)
            elig = True if sv == "allow" else (False if sv == "block" else base)
            ov = (c.get("state_overrides") or {}).get(st)
            rr = (ov["rr"] if isinstance(ov, dict) and ov.get("mode") == "custom"
                  else (c.get("target") or {}).get("rr"))
            out[(c["session"], c["structure"], c["direction"], st)] = (elig, float(rr))
    return out


def _resolver_read() -> dict:
    """Replicate execution.py's resolution order using the PRODUCTION index."""
    from strategy_core.scenario import _build_cohort_index
    idx = _build_cohort_index(_scenario())
    out = {}
    for (sess, struct, direc), rule in idx.items():
        for st in STATES:
            allowed = rule["enabled"]
            if rule.get("elig_states"):
                sv = rule["elig_states"].get(st)
                if sv == "allow":
                    allowed = True
                elif sv == "block":
                    allowed = False
            rr = None
            if rule.get("state_overrides"):
                ov = rule["state_overrides"].get(st)
                if ov is not None and ov["mode"] == "custom":
                    rr = ov["rr"]
            out[(sess, struct, direc, st)] = (
                allowed, float(rr if rr is not None else rule["target_rr"]))
    return out


# ── fidelity: three independent views must agree ─────────────────────────────

def test_production_resolver_matches_an_independent_config_read():
    """If these ever diverge, the deployed strategy is not the configured one."""
    a, b = _independent_read(), _resolver_read()
    assert set(a) == set(b), "cell coverage differs"
    diffs = {k: (a[k], b[k]) for k in a if a[k] != b[k]}
    assert not diffs, f"resolver disagrees with config on {len(diffs)} cells: {list(diffs)[:5]}"


def test_committed_matrix_artefact_matches_the_resolver():
    """The canonical artefact is the published strategy; it must not drift."""
    assert MATRIX.exists(), f"canonical matrix missing at {MATRIX}"
    resolver = _resolver_read()
    rows = list(csv.DictReader(MATRIX.open(encoding="utf-8")))
    assert len(rows) == len(resolver), f"{len(rows)} rows vs {len(resolver)} cells"
    for r in rows:
        key = (r["session"], r["structure"], r["direction"], r["market_state"])
        assert key in resolver, f"artefact has an unknown cell {key}"
        want = resolver[key]
        got = (r["eligible"] == "YES", float(r["target_rr"]))
        assert want == got, f"{key}: artefact {got} != resolver {want}"


def test_every_cohort_state_combination_is_covered():
    """6 sessions x 2 structures x 2 directions x 6 states = 144 decisions."""
    assert len(_resolver_read()) == 144


# ── the base-RR question, pinned as a measured fact ──────────────────────────

def test_base_rr_is_reachable_and_the_reach_is_bounded():
    """The cohort base target.rr is NOT dead: cells with no state override
    resolve to it. This pins HOW MANY eligible cells do so, so that number
    cannot change silently — it is the blast radius of the base value."""
    resolver = _resolver_read()
    scen = _scenario()
    overrides = {(c["session"], c["structure"], c["direction"], st)
                 for c in scen["cohorts"]
                 for st, ov in (c.get("state_overrides") or {}).items()
                 if isinstance(ov, dict) and ov.get("mode") == "custom"}
    base_eligible = [k for k, (elig, _rr) in resolver.items()
                     if elig and k not in overrides]
    assert len(base_eligible) == 5, (
        f"eligible cells falling back to the cohort base RR changed: "
        f"{len(base_eligible)} (was 5) -> {sorted(base_eligible)}")


def test_engine_level_rr_multiple_is_unreachable_for_labelled_candidates():
    """Every (session, structure, direction) has a cohort rule with a non-None
    target_rr, so `config.rr_multiple` never decides a labelled candidate's
    target. It remains reachable only where no cohort rule applies."""
    from strategy_core.scenario import _build_cohort_index
    idx = _build_cohort_index(_scenario())
    combos = {(s, st, d)
              for s in {"asia", "london", "lull", "newYork", "ny_pm", "outside"}
              for st in ("BOS", "CHoCH") for d in ("Long", "Short")}
    assert combos - set(idx) == set(), "a cohort combination has no rule"
    assert [k for k, v in idx.items() if v["target_rr"] is None] == []


def test_state_rules_require_a_CONFIRMED_state():
    """Unconfirmed/warmup candidates fall back to cohort base eligibility AND
    base target — labels are never invented. Pinned because it is the other
    route by which the base RR reaches a real trade."""
    src = (LUX / "strategy_core" / "execution.py").read_text(encoding="utf-8")
    body = src[src.index("_needs_state = "):src.index("# Per-cohort BREAK-EVEN")]
    assert "_sp_confirmed" in body
    assert body.count("_sp_confirmed") >= 2, "confirmation gates both eligibility and target"


# ── the matrix is keyed on session, which is why DST matters ─────────────────

def test_matrix_is_session_keyed_so_dst_reroutes_population_not_values():
    """The DST correction changes which session a fill lands in; it cannot
    change any cell's value. This is why the matrix was preserved unchanged."""
    resolver = _resolver_read()
    sessions = {k[0] for k in resolver}
    assert sessions == {"asia", "london", "lull", "newYork", "ny_pm", "outside"}
