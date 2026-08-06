"""M-GOLDEN-CONTRACT-1 — the artefact contract and both comparators.

The contract exists because three different digests were all being called "the
Golden hash". These tests pin that the contract distinguishes them, and that the
two comparison modes enforce exactly their stated policies — including negative
controls for every rule.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import golden_compare as gc                        # noqa: E402

CONTRACT = json.loads((REPO_ROOT / "live" / "golden_contract.json").read_text())


# ── the contract itself ──────────────────────────────────────────────────────

def test_contract_names_three_distinct_artefacts():
    arts = CONTRACT["artefacts"]
    assert {"golden.entry_trades.file.v1", "harness.entry_trades.mem.v1",
            "golden.baseline_trades.file.v1"} <= set(arts)
    digests = [a["known_digests"].get("mac_operator_producer")
               for a in arts.values() if a["known_digests"].get("mac_operator_producer")]
    assert len(digests) == len(set(digests)), "two artefacts share a digest"


def test_file_and_mem_artefacts_are_not_interchangeable():
    f = CONTRACT["artefacts"]["golden.entry_trades.file.v1"]
    m = CONTRACT["artefacts"]["harness.entry_trades.mem.v1"]
    assert f["kind"] == "file" and m["kind"] == "in-memory"
    assert f["generator"] != m["generator"]
    assert f["known_digests"]["mac_operator_producer"].startswith("ca925d54")
    assert m["known_digests"]["mac_operator_producer"].startswith("b43e3248")


def test_no_file_artefact_claims_machine_independence():
    for name, art in CONTRACT["artefacts"].items():
        assert art["machine_independent"] is False, (
            f"{name} claims machine independence; the S_22 bbw_value 2-ULP "
            f"discrepancy proves otherwise for this serialization family")


def test_rehearsal_expected_hashes_match_the_contract():
    from live.rehearsal import EXPECTED
    arts = CONTRACT["artefacts"]
    assert EXPECTED["trades_sha"] == \
        arts["golden.entry_trades.file.v1"]["known_digests"]["mac_operator_producer"]
    assert EXPECTED["baseline_sha"] == \
        arts["golden.baseline_trades.file.v1"]["known_digests"]["mac_operator_producer"]
    assert EXPECTED["order_blocks_sha"] == \
        arts["golden.order_blocks.file.v1"]["known_digests"]["mac_operator_producer"]


def test_archive_policy_field_tiers_are_exactly_the_regime_diagnostics():
    """Three tiers. bbw_value moved from the bounded allowlist to
    diagnostic-only after the 2026-08-06 VPS measurement: 1,179 cells at
    1-220 ULP vs the Mac archive while the same candidate was byte-identical
    to production on the same host — no bound can be both meaningful and
    satisfiable for that field across machines."""
    pol = CONTRACT["comparison_policies"]["cross_machine_archive_witness"]
    assert sorted(pol["allowlist_fields"]) == \
        ["adx_value", "ema_value", "px_vs_ema"]
    assert pol["diagnostic_only_fields"] == ["bbw_value"]
    assert pol["max_ulp"] == 2
    for econ in ("entry", "stop", "tp", "net_r", "outcome", "trade_id",
                 "fill_time", "market_state"):
        assert econ not in pol["allowlist_fields"]
        assert econ not in pol["diagnostic_only_fields"]


# ── ULP distance ─────────────────────────────────────────────────────────────

def test_ulp_distance_basics():
    assert gc.ulp_distance(1.0, 1.0) == 0
    assert gc.ulp_distance(1.0, math.nextafter(1.0, 2.0)) == 1
    assert gc.ulp_distance(1.0, math.nextafter(math.nextafter(1.0, 2.0), 2.0)) == 2
    assert gc.ulp_distance(-1.0, math.nextafter(-1.0, -2.0)) == 1


def test_ulp_distance_on_the_actual_s22_values():
    """The observed discrepancy measures as exactly 2 ULP (parsed doubles;
    shortest round-trip repr is bijective, so these ARE the computed values).
    This measurement is WHY the policy bound is 2, not the preferred 1."""
    assert gc.ulp_distance(2.540400593491076, 2.540400593491075) == 2


def test_ulp_distance_nan_and_sign():
    assert gc.ulp_distance(float("nan"), float("nan")) == 0
    assert gc.ulp_distance(float("nan"), 1.0) > 10 ** 9
    assert gc.ulp_distance(0.0, -0.0) <= 1        # adjacent on the integer line


# ── fixtures for comparator tests ────────────────────────────────────────────

HEADER = "trade_id,entry,stop,outcome,bbw_value,adx_value\n"


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(HEADER + "".join(r + "\n" for r in rows))
    return path


BASE_ROWS = ["L_1,1.1,1.0,WIN,2.540400593491076,33.1",
             "S_2,1.2,1.3,LOSS,1.5,20.0"]


def run_mode(tmp_path, mode, ref_rows, cand_rows):
    ref = write_csv(tmp_path / "ref.csv", ref_rows)
    cand = write_csv(tmp_path / "cand.csv", cand_rows)
    rep = tmp_path / "report.json"
    rc = gc.main(["--mode", mode, "--ref", str(ref), "--cand", str(cand),
                  "--report", str(rep)])
    return rc, json.loads(rep.read_text())


# ── same-host mode: zero tolerance ───────────────────────────────────────────

def test_same_host_identical_passes(tmp_path):
    rc, rep = run_mode(tmp_path, "same-host", BASE_ROWS, list(BASE_ROWS))
    assert rc == 0 and rep["verdict"] == "PASS" and rep["byte_identical"]


def test_same_host_one_ulp_in_diagnostic_FAILS(tmp_path):
    """The rule that keeps the deployment gate honest: on the same host, even
    the allowlisted diagnostic field must be byte-exact."""
    cand = ["L_1,1.1,1.0,WIN,2.540400593491075,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "same-host", BASE_ROWS, cand)
    assert rc == 1 and rep["verdict"] == "FAIL"
    assert rep["census"]["cell_mismatches"][0]["field"] == "bbw_value"


def test_same_host_economic_change_fails(tmp_path):
    cand = ["L_1,1.10000001,1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "same-host", BASE_ROWS, cand)
    assert rc == 1


def test_same_host_row_order_change_fails(tmp_path):
    rc, rep = run_mode(tmp_path, "same-host", BASE_ROWS, list(reversed(BASE_ROWS)))
    assert rc == 1
    assert rep["census"]["key_order_mismatches"]


def test_same_host_row_count_change_fails(tmp_path):
    rc, rep = run_mode(tmp_path, "same-host", BASE_ROWS, BASE_ROWS[:1])
    assert rc == 1 and not rep["census"]["row_count_equal"]


# ── archive mode: three-tier policy, full census ─────────────────────────────

def test_archive_bbw_float_difference_is_DIAGNOSTIC_and_enumerated(tmp_path):
    """bbw_value float noise is reported, never rejecting — at any ULP a real
    float pair can reach."""
    cand = ["L_1,1.1,1.0,WIN,2.540400593491075,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 0 and rep["verdict"] == "PASS"
    assert len(rep["diagnostic_mismatches"]) == 1
    assert rep["diagnostic_mismatches"][0]["field"] == "bbw_value"
    assert rep["diagnostic_mismatches"][0]["ulp"] == 2
    assert rep["forbidden_mismatches"] == []
    assert rep["permitted_mismatches"] == []


def test_archive_bbw_at_measured_maximum_220_ulp_still_passes(tmp_path):
    """The 2026-08-06 VPS census measured up to 220 ULP in bbw_value. The
    policy must accept the reality it was calibrated against — while still
    reporting the full magnitude."""
    v = 2.540400593491076
    for _ in range(220):
        v = math.nextafter(v, 0)
    cand = [f"L_1,1.1,1.0,WIN,{v!r},33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 0 and rep["verdict"] == "PASS"
    assert rep["diagnostic_mismatches"][0]["ulp"] == 220
    assert rep["diagnostic_max_ulp_observed"] == 220


def test_archive_non_float_change_in_bbw_stays_FORBIDDEN(tmp_path):
    """Diagnostic-only concedes summation order, not meaning: a type/format
    change in bbw_value is not float noise and must still reject."""
    cand = ["L_1,1.1,1.0,WIN,not-a-number,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["field"] == "bbw_value"
    assert rep["diagnostic_mismatches"] == []


def test_archive_nan_vs_number_in_bbw_stays_FORBIDDEN(tmp_path):
    """NaN appearing where a number was is a semantic change, not rounding."""
    cand = ["L_1,1.1,1.0,WIN,nan,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["field"] == "bbw_value"


def test_archive_exactly_max_ulp_in_allowlisted_adx_is_permitted(tmp_path):
    """The bounded allowlist still governs the other regime diagnostics."""
    v = 33.1
    for _ in range(2):
        v = math.nextafter(v, 34.0)
    cand = [f"L_1,1.1,1.0,WIN,2.540400593491076,{v!r}", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 0
    assert rep["permitted_mismatches"][0]["field"] == "adx_value"
    assert rep["permitted_mismatches"][0]["ulp"] == 2


def test_archive_beyond_max_ulp_in_allowlisted_adx_FAILS(tmp_path):
    """bbw_value's diagnostic status must not leak to the bounded fields."""
    v = 33.1
    for _ in range(3):
        v = math.nextafter(v, 34.0)
    cand = [f"L_1,1.1,1.0,WIN,2.540400593491076,{v!r}", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["field"] == "adx_value"
    assert rep["forbidden_mismatches"][0]["ulp"] == 3


def test_archive_one_ulp_in_ECONOMIC_field_FAILS(tmp_path):
    """The core of the policy: tolerance never leaks outside the allowlist."""
    entry_1ulp = repr(math.nextafter(1.1, 2.0))
    cand = [f"L_1,{entry_1ulp},1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["field"] == "entry"
    assert rep["forbidden_mismatches"][0]["ulp"] == 1


def test_archive_outcome_change_fails_regardless(tmp_path):
    cand = ["L_1,1.1,1.0,LOSS,2.540400593491076,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["field"] == "outcome"


def test_archive_identifier_change_fails(tmp_path):
    cand = ["L_9,1.1,1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 1 and rep["census"]["key_order_mismatches"]


def test_archive_census_is_complete_not_first_divergence(tmp_path):
    """Every mismatch must be enumerated in its tier — the S_22 lesson."""
    cand = ["L_1,1.1,1.0,WIN,2.540400593491075,33.10000000000001",
            "S_2,1.2,1.3,LOSS,1.5000000000000002,20.0"]
    rc, rep = run_mode(tmp_path, "archive", BASE_ROWS, cand)
    assert rc == 0
    # two bbw_value cells -> diagnostic; one adx_value cell -> bounded allowlist
    assert len(rep["diagnostic_mismatches"]) == 2
    assert len(rep["permitted_mismatches"]) == 1
    assert rep["permitted_mismatches"][0]["field"] == "adx_value"
    assert rep["forbidden_mismatches"] == []


def test_missing_file_is_a_usage_error_not_a_pass(tmp_path):
    rc = gc.main(["--mode", "same-host", "--ref", str(tmp_path / "absent.csv"),
                  "--cand", str(tmp_path / "absent2.csv")])
    assert rc == 2


def test_relative_paths_are_resolved_before_any_cwd_dependence(tmp_path, monkeypatch):
    ref = write_csv(tmp_path / "ref.csv", BASE_ROWS)
    cand = write_csv(tmp_path / "cand.csv", BASE_ROWS)
    monkeypatch.chdir(tmp_path)
    rc = gc.main(["--mode", "same-host", "--ref", "ref.csv", "--cand", "cand.csv",
                  "--report", "rep.json"])
    assert rc == 0
    assert (tmp_path / "rep.json").exists()


# ── known-input-drift ledger: provenance, not tolerance ──────────────────────

# The fixture uses an ECONOMIC field (entry): since bbw_value became
# diagnostic-only, a bbw cell can no longer demonstrate "unexplained drift is
# forbidden" — nothing in bbw is forbidden unless it stops being a float.
DRIFT = {"schema": "golden-known-input-drift-v1", "cause": "test",
         "explained_cells": [
             {"key": "L_1", "field": "entry",
              "ref": "1.1", "cand": "9.9"}]}


def write_drift(tmp_path, entries=None):
    import copy
    d = copy.deepcopy(DRIFT)
    if entries is not None:
        d["explained_cells"] = entries
    p = tmp_path / "drift.json"
    p.write_text(json.dumps(d))
    return p


def run_archive_with_drift(tmp_path, cand_rows, drift_path):
    ref = write_csv(tmp_path / "ref.csv", BASE_ROWS)
    cand = write_csv(tmp_path / "cand.csv", cand_rows)
    rep = tmp_path / "rep.json"
    rc = gc.main(["--mode", "archive", "--ref", str(ref), "--cand", str(cand),
                  "--report", str(rep), "--known-input-drift", str(drift_path)])
    return rc, json.loads(rep.read_text())


def test_drift_cell_is_explained_only_on_exact_four_way_match(tmp_path):
    cand = ["L_1,9.9,1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]
    rc, rep = run_archive_with_drift(tmp_path, cand, write_drift(tmp_path))
    assert rc == 0
    assert len(rep["explained_input_drift"]) == 1
    assert rep["forbidden_mismatches"] == []


def test_drift_with_different_candidate_value_stays_forbidden(tmp_path):
    """A NEW value in the same cell is not covered — provenance, not tolerance."""
    cand = ["L_1,8.8,1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]  # ledger says 9.9
    rc, rep = run_archive_with_drift(tmp_path, cand, write_drift(tmp_path))
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["cand"] == "8.8"
    assert rep["explained_input_drift"] == []


def test_drift_entry_never_leaks_to_other_keys(tmp_path):
    """Same field+values on a DIFFERENT trade stays forbidden."""
    cand = [BASE_ROWS[0], "S_2,9.9,1.3,LOSS,1.5,20.0"]
    drift = write_drift(tmp_path)                          # entry is for L_1
    rc, rep = run_archive_with_drift(tmp_path, cand, drift)
    assert rc == 1
    assert rep["forbidden_mismatches"][0]["key"] == "S_2"


def test_drift_match_on_a_diagnostic_field_classifies_as_explained(tmp_path):
    """Precedence pin: an exactly-recorded drift cell in bbw_value is
    provenance (explained_input_drift), not lumped into the diagnostic tier —
    a reviewer must see WHY it moved, not just that it is non-rejecting."""
    drift = write_drift(tmp_path, [{"key": "L_1", "field": "bbw_value",
                                    "ref": "2.540400593491076", "cand": "9.9"}])
    cand = ["L_1,1.1,1.0,WIN,9.9,33.1", BASE_ROWS[1]]
    rc, rep = run_archive_with_drift(tmp_path, cand, drift)
    assert rc == 0
    assert len(rep["explained_input_drift"]) == 1
    assert rep["diagnostic_mismatches"] == []


def test_drift_ledger_on_economic_field_still_requires_exact_match_and_is_visible(tmp_path):
    """The ledger CAN name an economic cell (a dataset revision may legitimately
    move one) — but only that exact cell, and the report shows it under
    explained_input_drift where a reviewer sees it, never silently."""
    cand = ["L_1,1.2,1.0,WIN,2.540400593491076,33.1", BASE_ROWS[1]]
    drift = write_drift(tmp_path, [{"key": "L_1", "field": "entry",
                                    "ref": "1.1", "cand": "1.2"}])
    rc, rep = run_archive_with_drift(tmp_path, cand, drift)
    assert rc == 0
    assert rep["explained_input_drift"][0]["field"] == "entry"


def test_same_host_mode_refuses_the_drift_ledger(tmp_path):
    ref = write_csv(tmp_path / "r.csv", BASE_ROWS)
    cand = write_csv(tmp_path / "c.csv", BASE_ROWS)
    rc = gc.main(["--mode", "same-host", "--ref", str(ref), "--cand", str(cand),
                  "--known-input-drift", str(write_drift(tmp_path))])
    assert rc == 2


def test_missing_drift_ledger_is_a_usage_error(tmp_path):
    ref = write_csv(tmp_path / "r.csv", BASE_ROWS)
    cand = write_csv(tmp_path / "c.csv", BASE_ROWS)
    rc = gc.main(["--mode", "archive", "--ref", str(ref), "--cand", str(cand),
                  "--known-input-drift", str(tmp_path / "absent.json")])
    assert rc == 2


def test_shipped_drift_ledger_matches_contract_reference():
    ledger = json.loads((REPO_ROOT / "live" / "golden_known_input_drift.json").read_text())
    assert ledger["schema"] == "golden-known-input-drift-v1"
    assert len(ledger["explained_cells"]) == 3
    assert all(e["field"] == "r_if_no_target" for e in ledger["explained_cells"])
    assert CONTRACT["input_pinning"]["known_input_drift_ledger"] == \
        "live/golden_known_input_drift.json"
