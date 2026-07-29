"""MS-A — durable runner-state foundations for confirmed-close accounting.

WHAT THIS MILESTONE FIXED
    Two durable-state defects made confirmed-close realised-R accounting
    impossible, and both were invisible until traced by execution:

    1. THE PLANNED STOP WAS DESTROYED AT CONFIRMATION. `ledger_set` replaced the
       detail dict, so the originating intent written at SENT was overwritten by
       broker execution facts. The original stop is the DENOMINATOR of realised
       R and is unrecoverable afterwards — the engine frame no longer holds a
       closed trade, and the broker's stop is gone once the position closes (and
       may have been moved to breakeven, which the canonical formula refuses to
       use).

    2. THE DAILY ACCUMULATOR COULD HOLD ONE DAY. `add_realized_r` reset itself
       whenever the date key differed, so a delayed prior-day close wiped the
       current day AND re-dated the bucket, after which the rail read 0.0 for
       today and silently re-disarmed.

WHAT THIS MILESTONE DELIBERATELY DOES NOT DO
    No broker-history reader, no production realised-R writer, no change to any
    SafetyRails verdict. The rail stays dormant until Milestone B supplies
    confirmed closes. Tests here seed state explicitly, exactly as the existing
    live-slice tests already do.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import state as live_state                              # noqa: E402
from live.intents import OPEN_POSITION, OrderIntent               # noqa: E402
from live.state import (LEDGER_CONFIRMED, LEDGER_FAILED,          # noqa: E402
                        LEDGER_PARTIAL, LEDGER_SENT, RunnerState)

TODAY = "2026-07-28"
YESTERDAY = "2026-07-27"


@pytest.fixture()
def state(tmp_path):
    return RunnerState(tmp_path)


def an_intent(**kw):
    base = dict(intent_id="i1", action=OPEN_POSITION, trade_id="t1", side="long",
                frontier_bar="2026-07-28 10:00", entry=1.1000, stop=1.0950,
                target=1.1100)
    base.update(kw)
    return OrderIntent(**base)


# ── per-date realised R: the reported failure ────────────────────────────────

def test_a_delayed_prior_day_post_no_longer_erases_today(state):
    """THE reported defect, pinned exactly as specified."""
    state.add_realized_r(-2.0, TODAY)
    state.add_realized_r(-1.0, YESTERDAY)
    assert state.daily_realized_r(TODAY) == -2.0
    assert state.daily_realized_r(YESTERDAY) == -1.0


def test_a_current_day_post_does_not_disturb_an_earlier_day(state):
    """The mirror of the above — the new bucket must not reset old ones."""
    state.add_realized_r(-1.0, YESTERDAY)
    state.add_realized_r(-2.0, TODAY)
    assert state.daily_realized_r(YESTERDAY) == -1.0
    assert state.daily_realized_r(TODAY) == -2.0


def test_multiple_posts_to_one_day_accumulate(state):
    for delta in (-1.0, -0.5, 2.0):
        state.add_realized_r(delta, TODAY)
    assert state.daily_realized_r(TODAY) == pytest.approx(0.5)


@pytest.mark.parametrize("amount", [-2.5, 0.0, 3.25])
def test_positive_negative_and_zero_r_all_post(state, amount):
    state.add_realized_r(amount, TODAY)
    assert state.daily_realized_r(TODAY) == pytest.approx(amount)


def test_an_unknown_day_reads_zero_exactly_as_before(state):
    """Unchanged observable semantics: an unseen day is 0.0, not an error."""
    assert state.daily_realized_r("2020-01-01") == 0.0


def test_an_empty_state_reads_zero(state):
    assert state.daily_realized_r(TODAY) == 0.0
    assert state.realized_r_by_date() == {}


def test_the_default_day_is_the_utc_calendar_date(state, monkeypatch):
    """The R day is UTC, matching the executor's own definition. MS-A introduces
    no account timezone and no configured reset — that is the funded-rules
    milestone, and inventing one here would change which day a trade counts in.
    """
    monkeypatch.setattr(live_state, "_utc_today", lambda: TODAY)
    state.add_realized_r(-1.5)
    assert state.daily_realized_r(TODAY) == -1.5
    assert state.daily_realized_r() == -1.5


@pytest.mark.parametrize("bad", ["28/07/2026", "July 28 2026", "2026-13-01",
                                 "", "20260728", "2026-07-28T00:00:00Z"])
def test_a_malformed_date_is_rejected_rather_than_silently_bucketed(state, bad):
    """A locale-formatted key would create a bucket no reader could address, so
    the loss would be silent. Rejecting is the only safe answer."""
    with pytest.raises(ValueError):
        state.add_realized_r(-1.0, bad)


def test_a_non_padded_iso_date_is_normalised_not_rejected(state):
    """CHARACTERIZATION: `2026-7-28` parses and round-trips to the canonical
    `2026-07-28`. It is accepted because the key it produces is canonical and
    addressable — nothing is lost — so rejecting it would be pedantry rather
    than safety. It must NOT create a second bucket for the same day."""
    state.add_realized_r(-1.0, "2026-7-28")
    state.add_realized_r(-1.0, "2026-07-28")
    assert state.realized_r_by_date() == {"2026-07-28": -2.0}


def test_a_non_string_date_is_rejected(state):
    with pytest.raises(ValueError):
        state.add_realized_r(-1.0, 20260728)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_a_non_finite_amount_is_rejected(state, bad):
    with pytest.raises(ValueError):
        state.add_realized_r(bad, TODAY)


# ── retention ────────────────────────────────────────────────────────────────

def test_the_date_map_is_bounded(state):
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-06-{day:02d}")
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-07-{day:02d}")
    assert len(state.realized_r_by_date()) <= live_state._RETAIN_DAYS


def test_pruning_keeps_the_most_recent_days_deterministically(state):
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-06-{day:02d}")
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-07-{day:02d}")
    retained = sorted(state.realized_r_by_date())
    assert retained[-1] == "2026-07-28"          # newest always survives
    assert "2026-06-01" not in retained          # oldest pruned first


def test_a_freshly_posted_historical_bucket_is_never_pruned_by_its_own_post(state):
    """Retention must not discard the very post that triggered it."""
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-07-{day:02d}")
    for day in range(1, 29):
        state.add_realized_r(-0.1, f"2026-08-{day:02d}")
    state.add_realized_r(-9.0, "2026-01-15")     # far older than the window
    assert state.daily_realized_r("2026-01-15") == -9.0


# ── ledger lineage retention ─────────────────────────────────────────────────

@pytest.mark.parametrize("terminal", [LEDGER_CONFIRMED, LEDGER_PARTIAL,
                                      LEDGER_FAILED])
def test_the_original_intent_survives_every_lifecycle_transition(state, terminal):
    intent = an_intent()
    state.ledger_set("i1", LEDGER_SENT, {"intent": intent.to_dict()})
    state.ledger_set("i1", terminal, {"order": 555, "deal": 999, "price": 1.1001})
    facts = state.planned_risk_facts("i1")
    assert facts is not None
    assert facts["stop"] == 1.0950 and facts["entry"] == 1.1000
    assert facts["side"] == "long" and facts["trade_id"] == "t1"


def test_broker_execution_detail_remains_available_alongside_the_intent(state):
    """Retention must not cost the lifecycle facts existing consumers read."""
    state.ledger_set("i1", LEDGER_SENT, {"intent": an_intent().to_dict()})
    state.ledger_set("i1", LEDGER_CONFIRMED,
                     {"order": 555, "deal": 999, "price": 1.1001,
                      "filled_volume": 0.01, "trade_id": "t1"})
    detail = state.data["ledger"]["i1"]["detail"]
    assert detail["order"] == 555 and detail["deal"] == 999
    assert detail["price"] == 1.1001 and detail["filled_volume"] == 0.01
    assert "intent" in detail


def test_a_moved_stop_can_never_replace_the_original_planned_stop(state):
    """The canonical formula requires the ORIGINAL stop. A breakeven move must
    not become the denominator of R — that would fabricate planned risk."""
    state.ledger_set("i1", LEDGER_SENT, {"intent": an_intent(stop=1.0950).to_dict()})
    state.ledger_set("i1", LEDGER_CONFIRMED, {"order": 555, "sl": 1.1000})
    assert state.planned_risk_facts("i1")["stop"] == 1.0950


def test_stale_transient_detail_does_not_survive_into_a_later_state(state):
    """Only lineage is carried. A failure diagnostic must not persist into a
    subsequent successful record."""
    state.ledger_set("i1", LEDGER_SENT,
                     {"intent": an_intent().to_dict(), "diagnostic": "timeout",
                      "error": "transient"})
    state.ledger_set("i1", LEDGER_CONFIRMED, {"order": 555})
    detail = state.data["ledger"]["i1"]["detail"]
    assert "diagnostic" not in detail and "error" not in detail
    assert "intent" in detail


def test_an_explicitly_supplied_intent_wins_over_the_carried_one(state):
    state.ledger_set("i1", LEDGER_SENT, {"intent": an_intent(stop=1.0950).to_dict()})
    state.ledger_set("i1", LEDGER_SENT, {"intent": an_intent(stop=1.0900).to_dict()})
    assert state.planned_risk_facts("i1")["stop"] == 1.0900


def test_a_legacy_record_without_an_intent_stays_explicitly_incomplete(state):
    """Migration must never fabricate planned risk. Milestone B has to treat
    None as 'cannot compute R', never as zero risk."""
    state.ledger_set("i1", LEDGER_CONFIRMED, {"order": 555, "price": 1.1})
    assert state.planned_risk_facts("i1") is None


def test_planned_risk_facts_for_an_unknown_intent_is_none(state):
    assert state.planned_risk_facts("never-existed") is None


# ── persistence, migration, round trip ───────────────────────────────────────

def legacy_file(tmp_path, payload: dict) -> RunnerState:
    (tmp_path / "runner_state.json").write_text(json.dumps(payload))
    return RunnerState(tmp_path)


def test_a_legacy_single_bucket_migrates_without_value_or_sign_change(tmp_path):
    s = legacy_file(tmp_path, {"ledger": {}, "mirror": {},
                               "daily": {"date": YESTERDAY, "realized_r": -1.5}})
    assert s.daily_realized_r(YESTERDAY) == -1.5
    assert s.realized_r_by_date() == {YESTERDAY: -1.5}


def test_a_legacy_positive_bucket_keeps_its_sign(tmp_path):
    s = legacy_file(tmp_path, {"daily": {"date": YESTERDAY, "realized_r": 2.25}})
    assert s.daily_realized_r(YESTERDAY) == 2.25


@pytest.mark.parametrize("legacy", [
    {"date": None, "realized_r": -1.0},        # never had a day
    {"date": YESTERDAY, "realized_r": None},   # never had an amount
    {"date": "27/07/2026", "realized_r": -1.0},  # unusable date format
    {"date": YESTERDAY, "realized_r": True},   # bool is not an amount
    {},
])
def test_malformed_legacy_data_is_dropped_rather_than_misdated(tmp_path, legacy):
    """Misdating a realised loss is worse than not having it — a wrong day both
    understates the real day and overstates another."""
    s = legacy_file(tmp_path, {"daily": legacy})
    assert s.realized_r_by_date() == {}


def test_migration_never_overwrites_an_existing_canonical_bucket(tmp_path):
    """A partially migrated file keeps the newer canonical value."""
    s = legacy_file(tmp_path, {
        "daily": {"date": YESTERDAY, "realized_r": -1.0},
        "realized_r_by_date": {YESTERDAY: -5.0}})
    assert s.daily_realized_r(YESTERDAY) == -5.0


def test_reading_an_old_file_does_not_rewrite_it(tmp_path):
    """State is rewritten only by a normal save, never merely by being read."""
    path = tmp_path / "runner_state.json"
    payload = {"ledger": {}, "mirror": {},
               "daily": {"date": YESTERDAY, "realized_r": -1.5}}
    path.write_text(json.dumps(payload))
    RunnerState(tmp_path)
    assert json.loads(path.read_text()) == payload


def test_the_canonical_shape_is_written_on_the_next_normal_save(tmp_path):
    s = legacy_file(tmp_path, {"daily": {"date": YESTERDAY, "realized_r": -1.5}})
    s.save()
    written = json.loads((tmp_path / "runner_state.json").read_text())
    assert written["realized_r_by_date"] == {YESTERDAY: -1.5}
    assert "daily" not in written                      # one canonical shape


def test_state_survives_a_full_serialization_round_trip(tmp_path):
    s = RunnerState(tmp_path)
    s.ledger_set("i1", LEDGER_SENT, {"intent": an_intent().to_dict()})
    s.ledger_set("i1", LEDGER_CONFIRMED, {"order": 555, "price": 1.1001})
    s.add_realized_r(-2.0, TODAY)
    s.add_realized_r(-1.0, YESTERDAY)
    s.save()

    reloaded = RunnerState(tmp_path)
    assert reloaded.daily_realized_r(TODAY) == -2.0
    assert reloaded.daily_realized_r(YESTERDAY) == -1.0
    assert reloaded.planned_risk_facts("i1")["stop"] == 1.0950


def test_a_save_is_atomic_and_never_leaves_a_truncated_state(tmp_path):
    """The whole-file replace model is preserved: a reader either sees the old
    complete state or the new complete state, never a partial one."""
    s = RunnerState(tmp_path)
    s.add_realized_r(-1.0, TODAY)
    s.save()
    first = json.loads((tmp_path / "runner_state.json").read_text())

    s.add_realized_r(-1.0, TODAY)
    s.save()
    second = json.loads((tmp_path / "runner_state.json").read_text())
    assert first["realized_r_by_date"][TODAY] == -1.0
    assert second["realized_r_by_date"][TODAY] == -2.0
    assert not list(tmp_path.glob("*.tmp"))            # no leftover temp file


def test_a_non_object_state_file_fails_visibly(tmp_path):
    (tmp_path / "runner_state.json").write_text(json.dumps(["not", "an", "object"]))
    with pytest.raises(ValueError):
        RunnerState(tmp_path)


# ── the rail is unchanged and still dormant ──────────────────────────────────

def test_the_real_safety_rail_reads_the_new_state_correctly(tmp_path, monkeypatch):
    """The rail is untouched; it simply reads the same value from the new
    representation. Seeded explicitly — MS-A adds no production writer."""
    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("LIVE_DAILY_LOSS_LIMIT_R", "5.0")
    from live.config import LiveConfig
    from live.safety import SafetyRails

    config = LiveConfig()
    s = RunnerState(config.state_dir)
    rails = SafetyRails(config, s)
    intent = an_intent()

    assert rails.evaluate(intent, "EURUSD", TODAY).allowed            # nothing lost
    s.add_realized_r(-4.9, TODAY)
    assert rails.evaluate(intent, "EURUSD", TODAY).allowed            # under limit
    s.add_realized_r(-0.1, TODAY)                                     # exactly -5.0
    verdict = rails.evaluate(intent, "EURUSD", TODAY)
    assert not verdict.allowed and verdict.rail == "daily_loss_limit"


def test_a_prior_day_loss_does_not_block_today(tmp_path, monkeypatch):
    """The whole point of per-date buckets: yesterday's breach is yesterday's."""
    monkeypatch.setenv("LIVE_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("LIVE_KILL_FILE", str(tmp_path / "KILL"))
    monkeypatch.setenv("LIVE_DAILY_LOSS_LIMIT_R", "5.0")
    from live.config import LiveConfig
    from live.safety import SafetyRails

    config = LiveConfig()
    s = RunnerState(config.state_dir)
    s.add_realized_r(-10.0, YESTERDAY)
    assert SafetyRails(config, s).evaluate(an_intent(), "EURUSD", TODAY).allowed


def test_milestone_a_adds_no_production_realised_r_writer():
    """The rail stays dormant. Only tests seed the accumulator until Milestone B
    adds confirmed-close accounting — this asserts that plainly rather than
    letting a reader assume protection is active."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rn", "add_realized_r", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout
    callers = [ln for ln in out.splitlines()
               if "def add_realized_r" not in ln and "/tests/" not in ln
               and "test_" not in ln]
    assert callers == [], f"unexpected production writer: {callers}"


# ── structural guards (Phase 13) ─────────────────────────────────────────────

def _live_source(name: str) -> str:
    """Source of a `live/` module with comments and the module docstring removed,
    so a guard matches real code rather than prose describing it."""
    import ast
    text = (REPO_ROOT / "live" / name).read_text()
    tree = ast.parse(text)
    doc = ast.get_docstring(tree, clean=False)
    if doc is not None:
        text = text.replace(doc, "", 1)
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.lstrip().startswith("#"))


def test_risk_math_is_a_pure_domain_module():
    """No MT5, FastAPI, database, fixture, gateway, config or filesystem. It must
    be importable from any process, including one with no broker and no state."""
    source = _live_source("risk_math.py")
    for forbidden in ("MetaTrader5", "fastapi", "sqlite3", "world.v1",
                      "mt5_gateway", "RunnerState", "live.config", "live.state",
                      "os.environ", "open(", "Path(", "requests", "urllib"):
        assert forbidden not in source, f"risk_math imports/uses {forbidden}"


def test_risk_math_imports_nothing_from_the_repository():
    """A pure calculation over supplied values — no repository coupling at all,
    in either direction."""
    import ast
    tree = ast.parse((REPO_ROOT / "live" / "risk_math.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            assert node.module in ("__future__", "dataclasses"), node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name == "dataclasses", alias.name


def test_the_backend_delegates_and_keeps_no_shadow_formula():
    """One canonical implementation. If the arithmetic reappears in the backend,
    the two definitions can drift and historical ledger records stop matching
    newly computed ones."""
    from conftest import code_only
    source = code_only("trade_reconstruction.py")
    assert "risk_math" in source, "backend no longer delegates"
    assert "gross_pnl / risk_amount" not in source, "shadow R formula in backend"
    assert "abs(basis_entry - initial_stop)" not in source, "shadow risk formula"


def test_only_one_canonical_risk_formula_exists_in_the_repository():
    import subprocess
    hits = subprocess.run(
        ["grep", "-rn", "gross_pnl / risk_amount", "--include=*.py",
         str(REPO_ROOT / "live"), str(REPO_ROOT / "backend")],
        capture_output=True, text=True).stdout.splitlines()
    real = [h for h in hits if "/tests/" not in h and "test_" not in h]
    # The property is LOCATION, not count: risk_math states the formula in its
    # docstring as well as in code, and documenting it there is desirable. What
    # must never happen is the arithmetic appearing in a second module.
    assert real, "the canonical formula disappeared entirely"
    outside = [h for h in real if "live/risk_math.py" not in h]
    assert outside == [], f"a second R formula exists outside risk_math: {outside}"


def test_live_does_not_import_the_backend():
    """The sanctioned direction is backend -> live. An inversion here would make
    the runner depend on FastAPI, the fixture and the databases."""
    import subprocess
    out = subprocess.run(
        ["grep", "-rnE", r"^\s*(from|import) (backend|server|trade_reconstruction)",
         "--include=*.py", str(REPO_ROOT / "live")],
        capture_output=True, text=True).stdout
    assert out.strip() == "", f"live/ imports backend: {out}"


def test_the_sdk_history_call_is_confined_to_the_gateway_layer():
    """Successor to MS-A's "no reader exists" guard.

    MS-A asserted the absence of a history reader, because its absence was what
    kept the rail dormant. B1 deliberately added that reader, so the invariant
    moves from EXISTENCE to LOCATION: the raw SDK history call may appear only
    behind the gateway boundary, never in the executor, state, safety or
    accounting layers. That is the constraint worth keeping — the original guard
    had done its job and would otherwise have to be deleted outright.
    """
    import subprocess
    out = subprocess.run(
        ["grep", "-rln", "history_deals_get", "--include=*.py",
         str(REPO_ROOT / "live")],
        capture_output=True, text=True).stdout
    files = {Path(line).name for line in out.splitlines() if line.strip()}
    assert files <= {"mt5_gateway.py", "deal_records.py"}, (
        f"the SDK history call escaped the gateway layer: {sorted(files)}")


def test_no_fixture_funded_rules_or_timezone_defaults_entered_the_live_path():
    for name in ("state.py", "risk_math.py", "safety.py"):
        source = _live_source(name)
        for forbidden in ("fundedRules", "world.v1", "Europe/Prague",
                          "accountTimezone", "dailyResetTime", "dailyLossLimit"):
            assert forbidden not in source, f"{name} references {forbidden}"


def test_runner_state_remains_a_single_writer_with_one_file():
    """No second writer, no second state file, no database, no thread."""
    source = _live_source("state.py")
    for forbidden in ("sqlite3", "threading", "multiprocessing", "Lock(",
                      "subprocess"):
        assert forbidden not in source, f"state.py introduced {forbidden}"
    assert source.count("os.replace") == 1, "more than one durable write path"


def test_the_date_map_has_a_bounded_deterministic_retention_policy():
    assert isinstance(live_state._RETAIN_DAYS, int)
    assert 0 < live_state._RETAIN_DAYS <= 400, "retention must stay bounded"
    source = _live_source("state.py")
    assert "_prune(" in source, "retention is declared but never applied"
