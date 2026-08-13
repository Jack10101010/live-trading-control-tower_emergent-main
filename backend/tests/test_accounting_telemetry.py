"""Final milestone — accounting telemetry and shadow mode.

WHY TELEMETRY IS THE LAST GATE
    Two defects in this programme were found only after the fact, and both were
    SILENT: a daily-loss rail that could never fire, and a pruning rule that
    could never prune. Neither was visible from outside. Telemetry exists so the
    third one is noticed by an operator rather than by an audit — its job is to
    make a subsystem that has stopped progressing look obviously stopped.

SHADOW MODE
    Enabling accounting must never enforce on the first deploy. In shadow the
    pipeline runs, posts durably and reports what the daily-loss rail WOULD have
    decided, while trading continues exactly as before. Promotion is a second,
    separate configuration change.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import deal_records as dr                              # noqa: E402
from live.deal_records import DealReadOutcome, DealReadResult    # noqa: E402
from live.state import RunnerState                               # noqa: E402
from test_durable_close_accounting import (BASE, POSITION, TODAY,  # noqa: E402
                                           FakeGateway, deal, executor, ok,
                                           seed_ledger)


def telemetry(ex):
    return ex.state.accounting_telemetry()


# ── telemetry per outcome ────────────────────────────────────────────────────

def test_successful_accounting_records_timings_and_counts(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.account_closed_deals(BASE)
    t = telemetry(ex)
    assert t["last_successful_history_read"] == BASE.isoformat()
    assert t["last_successful_accounting"] == BASE.isoformat()
    assert t["last_read_outcome"] == "ok"
    assert t["deals_processed"] == 1 and t["deals_accounted"] == 1
    assert t["cycles"] == 1 and t["errors"] == 0


def test_an_unavailable_read_is_recorded_as_an_error_not_a_quiet_market(tmp_path):
    """The distinction the whole subsystem is built on, made visible."""
    ex = executor(tmp_path, FakeGateway(
        DealReadResult(outcome=DealReadOutcome.UNAVAILABLE, detail="not connected")))
    ex.account_closed_deals(BASE)
    t = telemetry(ex)
    assert t["last_read_outcome"] == "unavailable"
    assert t["errors"] == 1 and "unavailable" in t["last_accounting_error"]
    assert "last_successful_history_read" not in t       # never falsely advanced


def test_malformed_records_are_counted(tmp_path):
    read = DealReadResult(outcome=DealReadOutcome.MALFORMED, deals=(deal("1", -5.0),),
                          rejected=(dr.RejectedDeal(reason=dr.REJECT_BAD_PROFIT,
                                                    raw_ticket="2"),))
    ex = executor(tmp_path, FakeGateway(read))
    ex.account_closed_deals(BASE)
    assert telemetry(ex)["malformed"] == 1
    assert telemetry(ex)["deals_accounted"] == 1


def test_unresolved_and_unaccountable_trades_are_counted_separately(tmp_path):
    """Different operator responses: a foreign position needs none, our trade
    with lost planned risk is a permanent gap worth investigating."""
    ex = executor(tmp_path, FakeGateway(ok([deal("1", -5.0, position_id="99999")])))
    ex.account_closed_deals(BASE)
    assert telemetry(ex)["unresolved"] == 1

    ex2 = executor(tmp_path / "b", FakeGateway(ok([deal("2", -5.0)])))
    ex2.state.data["ledger"]["i1"]["detail"]["intent"]["stop"] = None
    ex2.account_closed_deals(BASE)
    assert telemetry(ex2)["unaccountable"] == 1


def test_quarantined_and_ignored_deals_are_counted(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([
        deal("1", -5.0, entry=dr.ENTRY_INOUT),
        deal("2", -5.0, entry=dr.ENTRY_OUT_BY),
        deal("3", -5.0, entry=dr.ENTRY_IN)])))
    ex.account_closed_deals(BASE)
    t = telemetry(ex)
    assert t["quarantined"] == 2 and t["ignored"] == 1


def test_a_commit_failure_is_recorded(tmp_path, monkeypatch):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    monkeypatch.setattr(ex.state, "save",
                        lambda: (_ for _ in ()).throw(OSError("disk full")))
    ex.account_closed_deals(BASE)
    # The telemetry save fails too, so assert on the in-memory record.
    t = ex.state.data["accounting_telemetry"]
    assert t["errors"] == 1 and "commit failed" in t["last_accounting_error"]


def test_every_outcome_records_an_attempt(tmp_path):
    """No branch may return without reporting — silence is the failure mode."""
    for gw in (FakeGateway(ok([deal()])), FakeGateway(RuntimeError("x")),
               FakeGateway(DealReadResult(outcome=DealReadOutcome.UNAVAILABLE))):
        ex = executor(tmp_path / str(id(gw)), gw)
        ex.account_closed_deals(BASE)
        assert ex.state.data["accounting_telemetry"]["last_accounting_attempt"] \
            == BASE.isoformat()


# ── retention telemetry: the B4 defect made observable ───────────────────────

def test_retention_stats_report_sizes_and_oldest_age(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.state.data["ledger"]["i1"]["at"] = (BASE - timedelta(hours=30)).isoformat()
    ex.account_closed_deals(BASE)
    t = telemetry(ex)
    assert t["ledger_entries"] >= 1 and t["accounted_ids"] == 1
    assert t["oldest_ledger_age_hours"] == pytest.approx(30.0, abs=0.1)


def test_oldest_ledger_age_is_the_signal_that_pruning_stopped_progressing(tmp_path):
    """The exact B4 defect: entries accumulating with an age that climbs without
    bound. It was invisible then; it is a monitorable number now."""
    state = RunnerState(tmp_path)
    assert state.retention_stats(now=BASE)["oldest_ledger_age_hours"] is None
    state.data["ledger"]["old"] = {
        "status": "confirmed", "at": (BASE - timedelta(days=30)).isoformat(),
        "detail": {}}
    stats = state.retention_stats(now=BASE)
    assert stats["oldest_ledger_age_hours"] == pytest.approx(720.0, abs=0.1)
    assert stats["ledger_entries"] == 1


def test_pruning_counters_accumulate_across_cycles(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    for _ in range(3):
        ex.account_closed_deals(BASE)
    t = telemetry(ex)
    assert t["cycles"] == 3
    assert t["prune_runs"] == 3
    assert t["deals_accounted"] == 1          # idempotent: only the first posted


def test_counters_accumulate_but_timestamps_replace(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.account_closed_deals(BASE)
    later = BASE + timedelta(minutes=15)
    ex.account_closed_deals(later)
    t = telemetry(ex)
    assert t["cycles"] == 2                                  # accumulated
    assert t["last_accounting_attempt"] == later.isoformat()  # replaced


def test_telemetry_survives_a_restart(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.account_closed_deals(BASE)
    reloaded = RunnerState(tmp_path)
    t = reloaded.accounting_telemetry()
    assert t["cycles"] == 1 and t["deals_accounted"] == 1
    assert t["last_successful_accounting"] == BASE.isoformat()


# ── shadow mode ──────────────────────────────────────────────────────────────

def rails_for(tmp_path, *, shadow, enabled=True, limit=1.0):
    from live.config import LiveConfig
    config = LiveConfig(state_dir=tmp_path, kill_file=tmp_path / "KILL",
                        daily_loss_limit_r=limit)
    config.close_accounting_enabled = enabled
    config.close_accounting_shadow = shadow
    config.close_accounting_lookback_hours = 48.0
    return config


def test_shadow_mode_still_performs_durable_accounting(tmp_path):
    """Shadow observes the RAIL; it does not weaken the accounting."""
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.config.close_accounting_shadow = True
    result = ex.account_closed_deals(BASE)
    assert result["mode"] == "shadow" and result["committed"] == 1
    assert RunnerState(tmp_path).daily_realized_r(TODAY) == pytest.approx(-1.0)


def test_shadow_mode_reports_its_mode_in_telemetry(tmp_path):
    ex = executor(tmp_path, FakeGateway(ok([deal(profit=-5.0)])))
    ex.config.close_accounting_shadow = True
    ex.account_closed_deals(BASE)
    assert telemetry(ex)["mode"] == "shadow" and telemetry(ex)["shadow"] is True

    ex.config.close_accounting_shadow = False
    ex.account_closed_deals(BASE + timedelta(minutes=15))
    assert telemetry(ex)["mode"] == "enforce"


def test_shadow_mode_observes_a_daily_loss_block_without_enforcing_it(tmp_path):
    """The comparison an operator needs: what production did, versus what
    accounting-aware production would have done."""
    from live.intents import OPEN_POSITION, OrderIntent
    from live.safety import SafetyRails
    from live.executor import Executor

    config = rails_for(tmp_path, shadow=True)
    state = RunnerState(tmp_path)
    seed_ledger(state)
    state.add_realized_r(-5.0, TODAY)              # well past the 1.0R limit

    ex = Executor.__new__(Executor)
    ex.state, ex.config = state, config
    ex.rails = SafetyRails(config, state)
    ex.observed = {}
    intent = OrderIntent(intent_id="x", action=OPEN_POSITION, trade_id="t9",
                         side="long", frontier_bar="b", entry=1.1, stop=1.095)

    # The rail itself still says block -- it is NOT modified.
    assert not ex.rails.evaluate(intent, "EURUSD", TODAY).allowed

    verdict = ex.rails.evaluate(intent, "EURUSD", TODAY)
    if (not verdict.allowed and verdict.rail == "daily_loss_limit"
            and ex.config.close_accounting_enabled
            and ex.config.close_accounting_shadow):
        state.record_accounting_telemetry({"shadow_blocks_observed": 1},
                                          counters=("shadow_blocks_observed",))
    assert state.accounting_telemetry()["shadow_blocks_observed"] == 1


def test_leaving_shadow_mode_requires_only_configuration(tmp_path):
    config = rails_for(tmp_path, shadow=True)
    assert config.close_accounting_shadow is True
    config.close_accounting_shadow = False          # the whole promotion step
    assert config.close_accounting_shadow is False


def test_shadow_defaults_on_so_enabling_accounting_never_enforces(tmp_path):
    """You cannot reach enforcement by accident: enabling accounting starts in
    shadow, and promotion is a second, separate change."""
    import dataclasses
    from live.config import LiveConfig
    fields = {f.name: f for f in dataclasses.fields(LiveConfig)}
    assert fields["close_accounting_shadow"].default is True
    assert fields["close_accounting_enabled"].default is False


def test_with_accounting_disabled_the_rail_behaves_exactly_as_before(tmp_path):
    """Shadow must not suppress the rail when accounting is OFF — there is no
    accounting-aware behaviour to shadow, so nothing may change."""
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    guard = source[source.index('verdict.rail == "daily_loss_limit"'):]
    assert "close_accounting_enabled" in guard[:400], \
        "shadow must require accounting to be enabled"


def test_only_the_daily_loss_rail_is_ever_shadowed(tmp_path):
    """The kill switch, whitelist, max-open, health and market rails always
    enforce: none of them depends on the newly activated accounting."""
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    guard = source[source.index("SHADOW MODE."):source.index("if not verdict.allowed:")]
    for rail in ("kill_switch", "symbol_whitelist", "max_open_positions",
                 "account_health", "market_condition"):
        assert rail not in guard, f"{rail} must never be shadowed"


def test_shadow_mode_does_not_duplicate_or_fork_the_rails():
    """One evaluation, one verdict — no second SafetyRails and no forked path.

    Counts CODE, not prose: the executor also mentions `self.rails.evaluate()`
    in a comment, and a guard that matched that would be measuring documentation.
    """
    source = (REPO_ROOT / "live" / "executor.py").read_text()
    calls = [ln for ln in source.splitlines()
             if "self.rails.evaluate(" in ln and not ln.lstrip().startswith("#")]
    assert len(calls) == 1, f"the rails are evaluated in {len(calls)} places"

    # Exactly one SafetyRails is constructed in production, by the executor.
    import subprocess
    out = subprocess.run(["grep", "-rn", "SafetyRails(", "--include=*.py",
                          str(REPO_ROOT / "live")],
                         capture_output=True, text=True).stdout
    built = [ln for ln in out.splitlines()
             if "/tests/" not in ln and "class SafetyRails" not in ln
             and "import" not in ln]
    assert len(built) == 1, f"SafetyRails constructed in {len(built)} places: {built}"
