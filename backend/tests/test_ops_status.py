"""L1A — Operational Status Model tests. Fast unit tests only.

Verifies the OP-1 projection contract: provenance fidelity (copied fields equal
their sources), derivation determinism, the single freshness policy's thresholds,
explicit-unknown propagation, corrupt-source tolerance, read-only/no-cache
behaviour, the source-precedence table, and the attention truth table — plus an
integration-lite build over runtime outputs produced by the REAL cycle path.
"""

from __future__ import annotations

import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import backend.ops_status as ops_status_module                       # noqa: E402
from backend.ops_status import (MODEL_NAME, POLICY, SCHEMA_VERSION,  # noqa: E402
                                SOURCE_PRECEDENCE, UNKNOWN,
                                OperationalFreshnessPolicy,
                                build_operational_status, collect_sources,
                                operational_status, resolve_source)

NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)


def _iso(seconds_ago: float) -> str:
    return (NOW - timedelta(seconds=seconds_ago)).isoformat()


def _sources(**over):
    src = {
        "liveness": {"at": _iso(2), "started_at": _iso(30), "cycle_seq": 7,
                     "phase": "runner", "tick": 12, "elapsed_s": 30.0,
                     "state": "running", "interval_s": 5.0, "pid": 111},
        "cycle_heartbeat": {"at": _iso(20), "boundary": "2026-07-21 11:45:00+00:00",
                            "status": "no_new_bar", "error": "", "duration_s": 1.2},
        "cycles": [
            {"cycle_end": _iso(400), "status": "ok", "boundary": "B1",
             "duration_s": 300.0, "error": "", "frozen": False,
             "published": {"delivered": True}},
            {"cycle_end": _iso(20), "status": "no_new_bar", "boundary": "B1",
             "duration_s": 1.2, "error": "", "frozen": False,
             "published": {"delivered": True}},
        ],
        "feed_heartbeat": {"at": _iso(15), "last_bar_time": "2026-07-21 11:58:00+00:00",
                           "appended": 1, "error": ""},
        "runner_state": {"last_boundary": "2026-07-21 11:45:00+00:00",
                         "last_recomputed_input_revision": "a" * 64,
                         "ledger": {"i1": {"status": "simulated"},
                                    "i2": {"status": "pending"},
                                    "i3": {"status": "simulated"}},
                         "mirror": {"L_1": 5001},
                         "daily": {"date": "2026-07-21", "realized_r": 1.5}},
        "publish_payload": {"instance_id": "inst-1", "symbol": "EURUSD",
                            "mode": "dry_run", "engine_version": "5bb6372c",
                            "deployment_profile": "GOLDEN_COMPATIBLE",
                            "data_seam": "seam-v1", "at": _iso(20),
                            "runner": {"status": "no_new_bar", "boundary": "B1"},
                            "intents": [{"intent_id": "x1", "action": "OPEN_POSITION",
                                         "reason": "fill"}],
                            "execution": {"applied": [], "blocked": [
                                {"intent_id": "x9", "rail": "daily_loss"}], "skipped": []},
                            "reconciliation": {"findings": []}},
        "kill_file": None,
        "_errors": {},
    }
    src.update(over)
    return src


# ── mandatory top-level metadata ─────────────────────────────────────────────
def test_top_level_metadata_fields():
    m = build_operational_status(_sources(), NOW)
    assert m["schema_version"] == SCHEMA_VERSION == 1
    assert m["model_name"] == MODEL_NAME == "operational_status"
    assert m["generated_at"] == NOW.isoformat()
    assert m["meta"]["freshness_policy"] == "OperationalFreshnessPolicyV1"


# ── provenance fidelity: copied fields equal their sources verbatim ──────────
def test_copied_fields_are_verbatim():
    src = _sources()
    m = build_operational_status(src, NOW)
    assert m["identity"]["instance_id"] == src["publish_payload"]["instance_id"]
    assert m["identity"]["engine_version"] == src["publish_payload"]["engine_version"]
    assert m["process"]["phase"] == src["liveness"]["phase"]
    assert m["process"]["cycle_seq"] == src["liveness"]["cycle_seq"]
    assert m["cycle"]["last_cycle_status"] == src["cycle_heartbeat"]["status"]
    assert m["data_feed"]["last_bar_time"] == src["feed_heartbeat"]["last_bar_time"]
    assert m["trading_state"]["input_revision"] == src["runner_state"]["last_recomputed_input_revision"]
    assert m["trading_state"]["daily_realized_r"] == 1.5
    # decisions are verbatim quotes — the "why" comes from the engine/rails
    assert m["decisions"]["intents"] == src["publish_payload"]["intents"]
    assert m["decisions"]["blocked"][0]["rail"] == "daily_loss"


def test_derived_counts_and_last_evaluation():
    m = build_operational_status(_sources(), NOW)
    assert m["trading_state"]["ledger_counts"] == {"simulated": 2, "pending": 1}
    assert m["trading_state"]["mirrored_positions"] == 1
    assert m["cycle"]["last_evaluation_status"] == "ok"      # newest ok/bootstrap
    assert m["cycle"]["last_evaluation_boundary"] == "B1"
    assert m["cycle"]["trailing_error_count"] == 0
    assert m["cycle"]["publish_failures_in_tail"] == 0


def test_ages_are_derived_from_injected_now():
    m = build_operational_status(_sources(), NOW)
    assert m["process"]["liveness_age_s"] == pytest.approx(2, abs=0.01)
    assert m["cycle"]["cycle_age_s"] == pytest.approx(20, abs=0.01)
    assert m["data_feed"]["feed_heartbeat_age_s"] == pytest.approx(15, abs=0.01)


# ── determinism ──────────────────────────────────────────────────────────────
def test_determinism_same_inputs_same_model():
    src = _sources()
    a = build_operational_status(src, NOW)
    b = build_operational_status(src, NOW)
    assert a == b


def test_no_clock_read_now_is_injected():
    later = build_operational_status(_sources(), NOW + timedelta(seconds=100))
    assert later["process"]["liveness_age_s"] == pytest.approx(102, abs=0.01)
    assert later["generated_at"] == (NOW + timedelta(seconds=100)).isoformat()


# ── freshness policy: one definition, exact thresholds ───────────────────────
@pytest.mark.parametrize("age,expected", [
    (0.0, "ALIVE"), (15.0, "ALIVE"), (15.1, "STALE"),
    (60.0, "STALE"), (60.1, "UNAVAILABLE")])
def test_aliveness_thresholds(age, expected):
    assert POLICY.aliveness(age, 5.0) == expected


@pytest.mark.parametrize("age,alive,state,expected", [
    (0.0, "ALIVE", "running", "FRESH"),
    (90.0, "ALIVE", "running", "FRESH"),
    (90.1, "ALIVE", "running", "COMPUTING"),      # long evaluation, corroborated
    (900.0, "ALIVE", "running", "COMPUTING"),
    (90.1, "STALE", "running", "DEGRADED"),       # not corroborated
    (90.1, "ALIVE", "idle", "DEGRADED"),          # beacon says not running
    (1020.0, "ALIVE", "running", "COMPUTING"),
    (1020.1, "ALIVE", "running", "UNAVAILABLE"),
])
def test_cycle_freshness_thresholds(age, alive, state, expected):
    assert POLICY.cycle_freshness(age, alive, state) == expected


def test_policy_is_immutable_and_single_source_of_thresholds():
    with pytest.raises(Exception):
        POLICY.beacon_unavailable_s = 999.0
    assert isinstance(POLICY, OperationalFreshnessPolicy)
    # module source contains no duplicated literals for the policy thresholds
    text = (REPO_ROOT / "backend" / "ops_status.py").read_text()
    body = text.split("class OperationalFreshnessPolicy", 1)[1]
    after_policy = body.split("POLICY = ", 1)[1]
    for literal in ("1020.0", "90.0", "60.0"):
        assert literal not in after_policy, literal


def test_computing_requires_beacon_corroboration_end_to_end():
    src = _sources(cycle_heartbeat={"at": _iso(600), "boundary": "B1",
                                    "status": "no_new_bar", "error": "", "duration_s": 1.0})
    m = build_operational_status(src, NOW)
    assert m["cycle"]["cycle_freshness"] == "COMPUTING"      # beacon fresh + running
    src2 = _sources(cycle_heartbeat=src["cycle_heartbeat"],
                    liveness={**_sources()["liveness"], "at": _iso(300)})
    assert build_operational_status(src2, NOW)["cycle"]["cycle_freshness"] == "DEGRADED"


# ── unknown-state behaviour: never guess ─────────────────────────────────────
# Precise field-level expectations per unavailable source (replaces the former
# "UNKNOWN appears somewhere in the model" assertion, which was near-tautological).
_UNAVAILABLE_FIELDS = {
    "liveness": [("process", "phase"), ("process", "state"), ("process", "cycle_seq"),
                 ("process", "tick"), ("process", "elapsed_s"),
                 ("process", "beacon_interval_s"), ("process", "liveness_age_s"),
                 ("process", "aliveness")],
    "cycle_heartbeat": [("cycle", "last_cycle_at"), ("cycle", "last_cycle_status"),
                        ("cycle", "last_cycle_boundary"), ("cycle", "last_cycle_duration_s"),
                        ("cycle", "last_cycle_error"), ("cycle", "cycle_age_s"),
                        ("cycle", "cycle_freshness")],
    "cycles": [("cycle", "last_evaluation_at"), ("cycle", "last_evaluation_status"),
               ("cycle", "trailing_error_count"), ("cycle", "publish_failures_in_tail"),
               ("cycle", "last_publish_delivered"), ("attention", "frozen")],
    "feed_heartbeat": [("data_feed", "last_bar_time"), ("data_feed", "bars_appended_last_poll"),
                       ("data_feed", "feed_error"), ("data_feed", "feed_heartbeat_age_s"),
                       ("data_feed", "last_bar_age_s"), ("data_feed", "polling_healthy")],
    "runner_state": [("trading_state", "last_boundary"), ("trading_state", "input_revision"),
                     ("trading_state", "daily_date"), ("trading_state", "daily_realized_r"),
                     ("trading_state", "ledger_counts"), ("trading_state", "mirrored_positions")],
    "publish_payload": [("identity", "instance_id"), ("identity", "engine_version"),
                        ("identity", "payload_at"), ("identity", "payload_age_s"),
                        ("decisions", "boundary"), ("decisions", "intents"),
                        ("decisions", "blocked"), ("decisions", "reconciliation")],
}


@pytest.mark.parametrize("missing", sorted(_UNAVAILABLE_FIELDS))
def test_missing_source_yields_unknown_in_exactly_its_fields(missing):
    m = build_operational_status(_sources(**{missing: None}), NOW)
    assert m["meta"]["sources_available"][missing] is False
    for section, field in _UNAVAILABLE_FIELDS[missing]:
        assert m[section][field] == UNKNOWN, f"{section}.{field}"
    assert isinstance(m["attention"]["intervention_required"], bool)   # never crashes


# ── D-1: legitimate null is preserved and is NOT "unknown" ───────────────────
def _null_state_sources():
    """Every source AVAILABLE and parsed, but the runtime legitimately wrote null:
    market closed (no bar), fresh RunnerState (bootstrap pending), frozen-recovery
    cycle (no boundary)."""
    return _sources(
        feed_heartbeat={"at": _iso(5), "last_bar_time": None, "appended": 0, "error": ""},
        runner_state={"last_boundary": None, "last_recomputed_input_revision": None,
                      "ledger": {}, "mirror": {},
                      "daily": {"date": None, "realized_r": 0.0}},
        cycle_heartbeat={"at": _iso(5), "boundary": None,
                         "status": "frozen_pending_recovery", "error": "",
                         "duration_s": 0.5},
    )


@pytest.mark.parametrize("section,field", [
    ("data_feed", "last_bar_time"),          # polled fine, no bars
    ("trading_state", "last_boundary"),      # bootstrap pending
    ("trading_state", "input_revision"),     # pre-first-evaluation
    ("trading_state", "daily_date"),         # no trades today
    ("cycle", "last_cycle_boundary"),        # frozen-recovery cycle
])
def test_legitimate_null_is_preserved_not_collapsed(section, field):
    m = build_operational_status(_null_state_sources(), NOW)
    assert m[section][field] is None                       # verbatim null
    assert m[section][field] != UNKNOWN                    # explicitly NOT unknown


def test_null_and_unknown_are_distinguishable_for_the_same_field():
    null_build = build_operational_status(_null_state_sources(), NOW)
    gone_build = build_operational_status(_sources(runner_state=None), NOW)
    assert null_build["trading_state"]["last_boundary"] is None          # known: no boundary
    assert gone_build["trading_state"]["last_boundary"] == UNKNOWN       # unreadable source
    assert null_build["meta"]["sources_available"]["runner_state"] is True
    assert gone_build["meta"]["sources_available"]["runner_state"] is False


def test_absent_key_in_available_source_is_unknown_not_null():
    # key missing entirely (older/partial document) -> unknown, never fabricated null
    m = build_operational_status(_sources(feed_heartbeat={"at": _iso(5)}), NOW)
    assert m["data_feed"]["last_bar_time"] == UNKNOWN
    assert m["data_feed"]["feed_error"] == UNKNOWN


def test_no_special_case_for_error_field():
    # `error` obeys the same rule as every other key: null stays null.
    m = build_operational_status(
        _sources(cycle_heartbeat={"at": _iso(5), "boundary": "B", "status": "ok",
                                  "error": None, "duration_s": 1.0}), NOW)
    assert m["cycle"]["last_cycle_error"] is None
    m2 = build_operational_status(
        _sources(cycle_heartbeat={"at": _iso(5), "boundary": "B", "status": "ok",
                                  "duration_s": 1.0}), NOW)
    assert m2["cycle"]["last_cycle_error"] == UNKNOWN       # absent key -> unknown


def test_null_last_bar_time_yields_unknown_age_but_healthy_polling():
    m = build_operational_status(_null_state_sources(), NOW)
    assert m["data_feed"]["last_bar_time"] is None
    assert m["data_feed"]["last_bar_age_s"] == UNKNOWN      # age of no bar is unknowable
    assert m["data_feed"]["polling_healthy"] is True        # the poll itself worked


# ── D-2: kill-file availability semantics ────────────────────────────────────
def test_kill_file_source_is_always_available_when_checked():
    absent = build_operational_status(_sources(kill_file=False), NOW)
    present = build_operational_status(_sources(kill_file=True), NOW)
    # availability = "the check was performed", never "the file exists"
    assert absent["meta"]["sources_available"]["kill_file"] is True
    assert present["meta"]["sources_available"]["kill_file"] is True
    # operational state lives ONLY here
    assert absent["attention"]["kill_file_present"] is False
    assert present["attention"]["kill_file_present"] is True
    assert "kill_file_present" not in absent["attention"]["attention_reasons"]
    assert "kill_file_present" in present["attention"]["attention_reasons"]


def test_kill_file_availability_true_in_healthy_operation_via_collector(tmp_path):
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    m = operational_status(state, md, tmp_path / "KILL", NOW)      # no kill file present
    assert m["meta"]["sources_available"]["kill_file"] is True     # healthy != unavailable
    assert m["attention"]["kill_file_present"] is False


def test_kill_file_unchecked_is_unknown_not_fabricated():
    src = _sources()
    src.pop("kill_file")
    m = build_operational_status(src, NOW)
    assert m["meta"]["sources_available"]["kill_file"] is False
    assert m["attention"]["kill_file_present"] == UNKNOWN


def test_all_sources_missing_still_builds():
    empty = {k: None for k in ("liveness", "cycle_heartbeat", "cycles",
                               "feed_heartbeat", "runner_state", "publish_payload",
                               "kill_file")}
    empty["_errors"] = {}
    m = build_operational_status(empty, NOW)
    assert m["schema_version"] == 1 and m["model_name"] == MODEL_NAME
    assert all(v is False for v in m["meta"]["sources_available"].values())
    assert m["process"]["aliveness"] == UNKNOWN
    assert m["cycle"]["cycle_freshness"] == UNKNOWN
    assert m["data_feed"]["polling_healthy"] == UNKNOWN


def test_clock_skew_is_reported_not_corrected():
    src = _sources(liveness={**_sources()["liveness"], "at": _iso(-30)})
    m = build_operational_status(src, NOW)
    assert m["meta"]["clock_skew_suspected"] is True
    assert m["process"]["liveness_age_s"] < 0               # raw, uncorrected


# ── source precedence table ──────────────────────────────────────────────────
def test_source_precedence_table_is_explicit_and_used():
    assert resolve_source("frozen") == "cycles"
    assert SOURCE_PRECEDENCE["frozen"][0] == "cycles"
    # payload claims frozen, cycles (the precedent source) says False -> cycles wins
    src = _sources()
    src["publish_payload"]["execution"]["frozen"] = True
    assert build_operational_status(src, NOW)["attention"]["frozen"] is False


# ── attention truth table ────────────────────────────────────────────────────
def test_attention_clean():
    a = build_operational_status(_sources(), NOW)["attention"]
    assert a["intervention_required"] is False and a["attention_reasons"] == []


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: s["cycles"].append({"cycle_end": _iso(5), "status": "no_new_bar",
                                   "frozen": True, "error": "",
                                   "published": {"delivered": True}}), "frozen"),
    (lambda s: s.update(kill_file=True), "kill_file_present"),
    (lambda s: s.update(cycles=[{"cycle_end": _iso(i), "status": "error", "error": "boom",
                                 "frozen": False, "published": {"delivered": False}}
                                for i in (30, 20, 10)]), "consecutive_errors"),
    (lambda s: s.update(liveness={**_sources()["liveness"], "at": _iso(300)}),
     "process_not_alive"),
    (lambda s: s.update(cycle_heartbeat={"at": _iso(5000), "boundary": "B", "status": "x",
                                         "error": "", "duration_s": 1.0}),
     "cycles_not_completing"),
])
def test_attention_reasons_fire_individually(mutate, reason):
    src = _sources()
    mutate(src)
    a = build_operational_status(src, NOW)["attention"]
    assert a["intervention_required"] is True
    assert reason in a["attention_reasons"]


# ── polling_healthy never infers market state ────────────────────────────────
def test_polling_healthy_is_about_polling_not_market():
    # stale bar (weekend) but the poll itself is healthy -> healthy True, age raw
    src = _sources(feed_heartbeat={"at": _iso(10), "appended": 0,
                                   "last_bar_time": "2026-07-17 21:00:00+00:00",
                                   "error": ""})
    m = build_operational_status(src, NOW)
    assert m["data_feed"]["polling_healthy"] is True
    assert m["data_feed"]["last_bar_age_s"] > 100000        # surfaced raw, unjudged
    assert "market" not in json.dumps(m).lower()            # no market-state inference


def test_polling_unhealthy_on_feed_error():
    src = _sources(feed_heartbeat={"at": _iso(5), "appended": 0,
                                   "last_bar_time": None, "error": "terminal offline"})
    m = build_operational_status(src, NOW)
    assert m["data_feed"]["polling_healthy"] is False
    assert m["data_feed"]["feed_error"] == "terminal offline"


# ── OP-1 structural: read-only, no cache, no canonical state ─────────────────
def test_collector_writes_nothing(tmp_path):
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    (state / "ops" / "heartbeat.json").write_text(json.dumps({"at": _iso(5)}))
    before = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    stats = {p.as_posix(): p.stat().st_mtime_ns for p in tmp_path.rglob("*") if p.is_file()}
    operational_status(state, md, tmp_path / "KILL", NOW)
    after = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*"))
    assert before == after                                  # no files created
    assert stats == {p.as_posix(): p.stat().st_mtime_ns
                     for p in tmp_path.rglob("*") if p.is_file()}   # none modified


def test_no_cache_second_build_reflects_changed_source(tmp_path):
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    hb = state / "ops" / "heartbeat.json"
    hb.write_text(json.dumps({"at": _iso(5), "status": "no_new_bar",
                              "boundary": "B1", "error": "", "duration_s": 1.0}))
    first = operational_status(state, md, tmp_path / "KILL", NOW)
    hb.write_text(json.dumps({"at": _iso(5), "status": "ok",
                              "boundary": "B2", "error": "", "duration_s": 9.0}))
    second = operational_status(state, md, tmp_path / "KILL", NOW)
    assert first["cycle"]["last_cycle_status"] == "no_new_bar"
    assert second["cycle"]["last_cycle_status"] == "ok"     # projection, never cached
    assert second["cycle"]["last_cycle_boundary"] == "B2"


def test_corrupt_sources_are_reported_not_repaired(tmp_path):
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    (state / "ops" / "heartbeat.json").write_text("{not json")
    (state / "ops" / "cycles.jsonl").write_text('{"status":"ok","cycle_end":"x"}\n{bad\n')
    m = operational_status(state, md, tmp_path / "KILL", NOW)
    assert m["meta"]["sources_available"]["cycle_heartbeat"] is False
    assert "cycle_heartbeat" in m["meta"]["source_errors"]
    assert "corrupt" in m["meta"]["source_errors"]["cycles"]
    assert m["cycle"]["last_cycle_status"] == UNKNOWN       # never guessed


def test_cycles_tail_is_bounded(tmp_path):
    state, md = tmp_path / "state", tmp_path / "md"
    (state / "ops").mkdir(parents=True)
    md.mkdir()
    with (state / "ops" / "cycles.jsonl").open("w") as fh:
        for i in range(1200):
            fh.write(json.dumps({"cycle_end": _iso(i), "status": "no_new_bar",
                                 "error": "", "frozen": False}) + "\n")
    src = collect_sources(state, md, tmp_path / "KILL")
    assert len(src["cycles"]) == 500                        # documented bound


# ── D-3: type unions are DERIVED FROM THE FROZEN DOCSTRING CONTRACT ──────────
# There is deliberately no table here. Expected unions are parsed out of the
# FIELD PROVENANCE TABLE in ops_status.py, so these tests validate
#     implementation -> frozen contract
# directly. A duplicated table in the tests could drift in lockstep with the
# docstring and hide exactly the mismatch this suite exists to catch.
_ALIVENESS = ("ALIVE", "STALE", "UNAVAILABLE", UNKNOWN)
_FRESHNESS = ("FRESH", "COMPUTING", "DEGRADED", "UNAVAILABLE", UNKNOWN)
_SECTIONS = ("identity", "process", "cycle", "data_feed", "trading_state",
             "decisions", "attention")
_FIELD_RE = re.compile(r"^(" + "|".join(("meta",) + _SECTIONS) + r")\.([a-z_0-9]+)\s", re.M)
_TYPE_TOKENS = ("str", "int", "float", "bool", "null", "array", "object")


def _contract_entries() -> dict[str, str]:
    """Split the frozen FIELD PROVENANCE TABLE into one text blob per field."""
    doc = ops_status_module.__doc__
    table = doc.split("FIELD PROVENANCE TABLE")[1].split("LIMITATION")[0]
    marks = [(m.start(), f"{m.group(1)}.{m.group(2)}")
             for m in _FIELD_RE.finditer(table)]
    entries: dict[str, str] = {}
    for i, (pos, name) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(table)
        entries[name] = entries.get(name, "") + table[pos:end]
    return entries


def _documented_union(blob: str) -> set[str]:
    """The declared JSON type union for one contract entry.

    Generic parameters are stripped first so `object<str,int>` contributes
    `object` (not `str`/`int`) and `array<str>` contributes `array`."""
    text = re.sub(r"<[^>]*>", "", blob)
    kinds = {tok for tok in _TYPE_TOKENS if re.search(rf"\b{tok}\b", text)}
    if '"unknown"' in text or re.search(r'"[A-Z]+"', text):
        kinds.add("str")                       # sentinel / enum members are strings
    return kinds


def _runtime_kind(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):                # bool before int (bool subclasses int)
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return type(value).__name__


def _all_builds() -> dict[str, dict]:
    """Every documented build condition the contract must cover."""
    empty = {k: None for k in ("liveness", "cycle_heartbeat", "cycles",
                               "feed_heartbeat", "runner_state", "publish_payload",
                               "kill_file")}
    empty["_errors"] = {}
    return {
        "fully-available": build_operational_status(_sources(), NOW),
        "all-unavailable": build_operational_status(empty, NOW),
        "legitimate-null": build_operational_status(_null_state_sources(), NOW),
        "kill-present": build_operational_status(_sources(kill_file=True), NOW),
        "kill-absent": build_operational_status(_sources(kill_file=False), NOW),
    }


def test_contract_parser_finds_a_union_for_every_documented_field():
    """Guards the parser itself: an entry yielding an empty union would make the
    conformance test below vacuous."""
    entries = _contract_entries()
    assert len(entries) >= 50, f"only {len(entries)} contract entries parsed"
    empty = [name for name, blob in entries.items() if not _documented_union(blob)]
    assert not empty, f"contract entries with no parseable type union: {empty}"


@pytest.mark.parametrize("section", _SECTIONS)
def test_runtime_types_conform_to_frozen_contract(section):
    entries = _contract_entries()
    for build_name, model in _all_builds().items():
        for field, value in model[section].items():
            path = f"{section}.{field}"
            assert path in entries, f"{path} emitted but absent from the frozen contract"
            allowed = _documented_union(entries[path])
            kind = _runtime_kind(value)
            assert kind in allowed, (
                f"[{build_name}] {path} emitted {kind}={value!r}; "
                f"frozen contract allows {sorted(allowed)}")


def test_every_documented_field_is_actually_emitted():
    entries = _contract_entries()
    model = build_operational_status(_sources(), NOW)
    emitted = {f"meta.{k}" for k in ("schema_version", "model_name", "generated_at")}
    emitted |= {f"meta.{k}" for k in model["meta"]}
    for section in _SECTIONS:
        emitted |= {f"{section}.{f}" for f in model[section]}
    assert not (set(entries) - emitted), f"documented but never emitted: {set(entries) - emitted}"


def test_enumerated_string_fields_stay_in_their_enumeration():
    for model in _all_builds().values():
        assert model["process"]["aliveness"] in _ALIVENESS
        assert model["cycle"]["cycle_freshness"] in _FRESHNESS


def test_error_fields_share_one_contract():
    """The two analogous copied error fields must declare the same union."""
    entries = _contract_entries()
    assert (_documented_union(entries["cycle.last_cycle_error"])
            == _documented_union(entries["data_feed.feed_error"])
            == {"str", "null"})


def test_null_and_unknown_rules_are_documented():
    doc = ops_status_module.__doc__
    assert "NULL vs UNKNOWN" in doc
    assert "There are no per-key exceptions to this rule." in doc
    assert "NOT an\natomic cross-file snapshot" in doc      # exact wording, no weak OR


# ── integration-lite: real runtime outputs -> model ──────────────────────────
HEADER = b"time,open,high,low,close,volume\n"


class _StubBridge:
    def poll_once(self):
        return {"ok": True, "appended": 0, "last_bar_time": None}


class _StubPublisher:
    def build_payload(self, *a, **k):
        return {"instance_id": "it-1", "at": datetime.now(timezone.utc).isoformat(),
                "runner": {"status": "n/a", "boundary": None}, "intents": [],
                "execution": {}, "reconciliation": {}}

    def publish(self, payload):
        return {"delivered": False, "error": "no backend"}


def test_integration_lite_model_from_real_cycle_outputs(tmp_path):
    from live import main as live_main
    from live.config import LiveConfig
    from live.ops_log import OpsLog
    from live.runner import LiveRunner

    cfg = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                     market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    cfg.ensure_dirs()
    (cfg.lux_root / "data" / "candles").mkdir(parents=True)
    times = pd.date_range("2026-07-17 09:00:00+00:00", periods=40,
                          freq="1min").strftime("%Y-%m-%d %H:%M:%S+00:00")
    (cfg.lux_root / "data" / "candles" / "EURUSD_1m_extended_2015_2026.csv").write_bytes(
        HEADER + b"".join(f"{t},1.0,1.0,1.0,1.0,0\n".encode() for t in times))
    cols = ["trade_id", "direction", "detection_time", "fill_time", "outcome", "entry", "stop", "tp"]
    frame = pd.DataFrame([{c: "" for c in cols} | {"trade_id": "L_1",
                          "outcome": "UNFILLED"}])[cols].astype(str)
    ops = OpsLog(cfg.state_dir)
    runner = LiveRunner(cfg, session=None, pipeline=lambda c, d: frame)
    live_main.cycle(cfg, None, _StubBridge(), runner, None, _StubPublisher(), ops)
    live_main.cycle(cfg, None, _StubBridge(), runner, None, _StubPublisher(), ops)

    m = operational_status(cfg.state_dir, cfg.market_data_dir, cfg.kill_file,
                           datetime.now(timezone.utc))
    # real field names line up with the projection's expectations
    assert m["meta"]["sources_available"]["cycle_heartbeat"] is True
    assert m["meta"]["sources_available"]["cycles"] is True
    assert m["meta"]["sources_available"]["runner_state"] is True
    assert m["cycle"]["last_cycle_status"] == "no_new_bar"
    assert m["cycle"]["last_evaluation_status"] == "bootstrap"   # first cycle evaluated
    assert m["cycle"]["cycle_freshness"] == "FRESH"
    assert m["trading_state"]["last_boundary"] is not None
    assert m["trading_state"]["input_revision"] != UNKNOWN
    assert m["cycle"]["last_publish_delivered"] is False         # stub publisher failed
    assert m["attention"]["intervention_required"] is False
