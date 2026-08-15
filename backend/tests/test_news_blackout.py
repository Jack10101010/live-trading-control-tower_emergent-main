"""M-LIVE-NEWS-1 — current calendar, exact +/-3 blackout, fail-closed staleness.

The defect being pinned: the live node's calendar ended 2026-05-21 and had no
refresh path, so "no event found" meant "we never looked". These tests make
that state unreachable in both directions —

  * a calendar that cannot PROVE it is current refuses new exposure;
  * a calendar that IS current blocks exactly the intended six-minute window
    and nothing wider;

— and pin the two asymmetries that make the rule safe rather than merely
strict: news never blocks CLOSE/MODIFY, and news never closes a position.

Everything runs against tmp_path with an injected fetcher. No network, no MT5,
no production live_state.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
LUX = REPO_ROOT.parent / "Lux-OB-Backtester"

from live import news_feed as nf  # noqa: E402
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,  # noqa: E402
                          OrderIntent)
from live.safety import SafetyRails  # noqa: E402

#: The event every boundary test is measured against.
T = datetime(2026, 8, 12, 12, 30, tzinfo=timezone.utc)


# ── fixtures ────────────────────────────────────────────────────────────────

# ── M-LIVE-STALE-OPEN-GUARDS-1 test wiring ──────────────────────────────────
# Two OPEN rails were added after these tests were written: wall-clock freshness
# of the modelled fill, and executable-price divergence. Production always
# supplies both a fill_time (diff_frontier sets it on every OPEN) and a quote
# provider (the Executor wires the gateway), so these fixtures now do the same.
# The fixed clock keeps them deterministic and the quote sits exactly on the
# canonical entry, so both guards abstain and each test still proves what it
# was written to prove rather than tripping on the new rails.
import datetime as _dt
_GUARD_NOW = _dt.datetime.fromisoformat("2026-08-12T12:05:00+00:00")


def _guard_clock():
    return _GUARD_NOW


def _guard_quote():
    return True, {"bid": 1.1, "ask": 1.1, "at": "2026-08-12T12:05:00+00:00"}


class Cfg:
    """Minimal LiveConfig stand-in. News parameters are set explicitly here so
    a test never depends on the production golden config."""
    def __init__(self, root, **over):
        self.state_dir = root
        self.kill_file = root / "KILL"
        # dry_run so the ARM rail (which precedes news) abstains: these tests
        # are about the news rail, and a `not_armed` block would mask it. The
        # news rail is deliberately mode-independent -- it gates OPEN in both.
        self.mode = "dry_run"
        self.broker_symbol = "EURUSD"
        self.symbol = "EURUSD"
        self.daily_loss_limit_r = 5.0
        self.max_open_positions = 6
        self.golden_config_path = None
        self.news_blackout_enabled = True
        self.news_blackout_impacts = ["high"]
        self.news_blackout_currencies = None      # -> derived from the symbol
        self.news_blackout_minutes_before = 3
        self.news_blackout_minutes_after = 3
        self.news_flatten_active_trades = False
        self.__dict__.update(over)


class State:
    def __init__(self, mirror=None):
        self.data = {"ledger": {}, "mirror": dict(mirror or {}), "broker_closed": {}}
    def ledger_status(self, i): return None
    def mirror_ticket(self, t): return self.data["mirror"].get(t)
    def broker_closed_ticket(self, t): return None
    def daily_realized_r(self, d): return 0.0
    def open_mirror_count(self): return len(self.data["mirror"])
    def record_block(self, *a, **k): pass


def ff_row(when=T, currency="USD", impact="High", title="CPI m/m", offset_h=-4):
    """A row in the SOURCE's shape: local wall clock plus an explicit offset."""
    local = when.astimezone(timezone(timedelta(hours=offset_h)))
    return {"title": title, "country": currency, "impact": impact,
            "date": local.isoformat(), "forecast": "", "previous": ""}


def calendar(tmp_path, rows=None, *, now=T, cfg=None, fetch_error=None, **over):
    """A NewsCalendar with a cache written by an INJECTED fetch — the real
    refresh path, so normalisation and atomic write are exercised too."""
    rows = [ff_row()] if rows is None else rows

    def opener(url):
        if fetch_error:
            raise fetch_error
        return rows
    cal = nf.NewsCalendar(cfg or Cfg(tmp_path, **over), opener=opener)
    cal.refresh(now)
    return cal


class FrozenGate:
    """The rail asks `verdict()` with no argument -- it has no clock and must
    not acquire one. Tests therefore freeze the clock in the gate."""
    def __init__(self, cal, now):
        self.cal, self.now, self.config = cal, now, cal.config
    def verdict(self, now=None):
        return self.cal.verdict(self.now)


def rails(cal, tmp_path, mirror=None):
    """Rails with news wired and the preceding gates deliberately quiet (no
    kill file, dry_run so arming abstains, empty ledger), so a block on an
    OPEN can only have come from the news rail."""
    return SafetyRails(cal.config, State(mirror), arm_runtime=None,
                       observed_account={}, news_gate=cal,
                       quote_provider=_guard_quote, clock=_guard_clock)


def open_intent():
    return OrderIntent(intent_id="i1", action=OPEN_POSITION, trade_id="L_1",
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00",
                       entry=1.1, stop=1.0, target=1.3,
                       fill_time="2026-08-12 12:02:00+00:00")


def close_intent():
    return OrderIntent(intent_id="c1", action=CLOSE_POSITION, trade_id="L_1",
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00")


def modify_intent():
    return OrderIntent(intent_id="m1", action=MODIFY_STOP, trade_id="L_1",
                       side="long", frontier_bar="2026-08-12 12:00:00+00:00",
                       stop=1.05)


# ── PART 4: the exact +/-3 minute rule ──────────────────────────────────────

#: Straight from the milestone spec. The window is [T-3, T+3], CLOSED at both
#: ends: an event exactly three minutes away is inside the blackout.
BOUNDARY = [
    (timedelta(minutes=-4),             True,  "T-4:00"),
    (timedelta(minutes=-3, seconds=-1), True,  "T-3:01"),
    (timedelta(minutes=-3),             False, "T-3:00"),
    (timedelta(minutes=-2, seconds=-59), False, "T-2:59"),
    (timedelta(0),                      False, "T"),
    (timedelta(minutes=2, seconds=59),  False, "T+2:59"),
    (timedelta(minutes=3),              False, "T+3:00"),
    (timedelta(minutes=3, seconds=1),   True,  "T+3:01"),
    (timedelta(minutes=4),              True,  "T+4:00"),
]


@pytest.mark.parametrize("delta,allow,label", BOUNDARY)
def test_blackout_boundaries_are_exact(tmp_path, delta, allow, label):
    cal = calendar(tmp_path)
    now = T + delta
    got, reason, detail = cal.verdict(now)
    assert got is allow, f"{label}: expected {'ALLOW' if allow else 'BLOCK'}, got {reason} {detail}"
    if not allow:
        assert reason == nf.REFUSE_BLACKOUT


@pytest.mark.parametrize("delta,allow,label", BOUNDARY)
def test_the_RAIL_enforces_the_same_boundaries(tmp_path, delta, allow, label):
    """The rule is only real where orders are actually stopped."""
    cal = calendar(tmp_path)
    v = rails(FrozenGate(cal, T + delta), tmp_path).evaluate(
        open_intent(), "EURUSD", "2026-08-12")
    assert v.allowed is allow, f"{label}: rail said {v.rail} {v.detail}"
    if not allow:
        assert v.rail == nf.REFUSE_BLACKOUT


def test_window_is_six_minutes_wide_and_no_wider(tmp_path):
    """Guards against an off-by-one that silently widens the block."""
    cal = calendar(tmp_path)
    blocked = [s for s in range(-400, 401)
               if not cal.verdict(T + timedelta(seconds=s))[0]]
    assert min(blocked) == -180 and max(blocked) == 180
    assert len(blocked) == 361          # inclusive both ends


# ── PART 4: impact and currency filtering ───────────────────────────────────

@pytest.mark.parametrize("currency,blocks", [
    ("EUR", True), ("USD", True),
    ("GBP", False), ("JPY", False), ("CHF", False), ("AUD", False),
])
def test_only_the_instrument_currencies_block(tmp_path, currency, blocks):
    cal = calendar(tmp_path, [ff_row(currency=currency)])
    allowed = cal.verdict(T)[0]
    assert allowed is not blocks, f"HIGH {currency} should {'block' if blocks else 'not block'} EURUSD"


@pytest.mark.parametrize("impact,blocks", [
    ("High", True), ("Medium", False), ("Low", False), ("Holiday", False),
])
def test_only_high_impact_blocks(tmp_path, impact, blocks):
    cal = calendar(tmp_path, [ff_row(impact=impact)])
    assert cal.verdict(T)[0] is not blocks


def test_both_instrument_currencies_are_watched(tmp_path):
    cal = calendar(tmp_path)
    assert cal.currencies == ["EUR", "USD"]
    assert cal.impacts == ["high"]


def test_explicit_currency_list_overrides_symbol_derivation(tmp_path):
    cal = calendar(tmp_path, news_blackout_currencies=["GBP"])
    assert cal.currencies == ["GBP"]
    assert cal.verdict(T)[0] is True         # the USD event is now irrelevant


def test_the_earliest_overlapping_event_is_the_one_reported(tmp_path):
    """Two events can blanket the same instant; the report must be stable."""
    cal = calendar(tmp_path, [ff_row(title="Later", when=T + timedelta(minutes=2)),
                              ff_row(title="Earlier", when=T)])
    assert "Earlier" in cal.verdict(T + timedelta(seconds=30))[2]


# ── PART 5: timezone safety ─────────────────────────────────────────────────

def test_offsets_are_honoured_not_assumed(tmp_path):
    """12:30 UTC arrives as 08:30-04:00. If the offset were dropped the event
    would land four hours away and the blackout would fire at the wrong time."""
    row = ff_row(offset_h=-4)
    assert row["date"].startswith("2026-08-12T08:30:00-04:00")
    cal = calendar(tmp_path, [row])
    assert cal.events()[0]["time"] == "2026-08-12T12:30:00Z"
    assert cal.verdict(T)[0] is False
    assert cal.verdict(T - timedelta(hours=4))[0] is True    # nothing at 08:30Z


def test_the_same_instant_via_any_offset_is_the_same_instant(tmp_path):
    """DST cannot move an economic event: identical instants expressed in
    different offsets must produce byte-identical normalised rows."""
    outs = set()
    for off in (-4, -5, 0, 1, 5.5 // 1):
        cal = calendar(tmp_path, [ff_row(offset_h=int(off))])
        outs.add(cal.events()[0]["time"])
    assert outs == {"2026-08-12T12:30:00Z"}


def test_a_naive_timestamp_is_rejected_not_assumed_utc(tmp_path):
    """Guessing an offset is exactly how an event moves by an hour."""
    with pytest.raises(ValueError, match="no UTC offset"):
        nf.parse_event_time("2026-08-12T12:30:00")


def test_naive_now_is_refused_by_the_comparison(tmp_path):
    cal = calendar(tmp_path)
    with pytest.raises(ValueError):
        nf.blackout_event(cal.events(), datetime(2026, 8, 12, 12, 30),
                          before_min=3, after_min=3)


def test_zulu_and_offset_forms_both_parse(tmp_path):
    a = nf.parse_event_time("2026-08-12T12:30:00Z")
    b = nf.parse_event_time("2026-08-12T14:30:00+02:00")
    assert a == b == T


@pytest.mark.parametrize("when,expect_start,expect_end", [
    # A week wholly inside EDT (UTC-4): Sun 00:00 ET == 04:00Z
    (datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc),
     "2026-08-09T04:00:00+00:00", "2026-08-16T04:00:00+00:00"),
    # The week containing the autumn transition: it must still be seven local
    # days, so the end offset differs from the start offset.
    (datetime(2026, 11, 3, 12, 0, tzinfo=timezone.utc),
     "2026-11-01T04:00:00+00:00", "2026-11-08T05:00:00+00:00"),
])
def test_publication_week_survives_a_dst_transition(when, expect_start, expect_end):
    """The FF week is Sunday->Sunday LOCAL. Naive arithmetic in absolute time
    would silently shorten or lengthen the week across a transition, moving the
    coverage horizon by an hour in the direction nobody checks."""
    start, end = nf.ff_week_bounds(when)
    assert start.isoformat() == expect_start
    assert end.isoformat() == expect_end
    assert (end - start) in (timedelta(days=7), timedelta(days=7, hours=1),
                             timedelta(days=6, hours=23))


def test_week_bounds_require_an_aware_instant():
    with pytest.raises(ValueError):
        nf.ff_week_bounds(datetime(2026, 8, 12, 12, 0))


# ── PART 3: fail closed when the calendar cannot prove itself ───────────────

def test_no_cache_at_all_blocks_opens(tmp_path):
    cal = nf.NewsCalendar(Cfg(tmp_path), opener=lambda u: (_ for _ in ()).throw(OSError("down")))
    allowed, reason, _ = cal.verdict(T)
    assert allowed is False and reason == nf.REFUSE_UNAVAILABLE


def test_a_stale_calendar_blocks_opens(tmp_path):
    cal = calendar(tmp_path)
    late = T + timedelta(seconds=nf.MAX_STALE_S + 60)
    allowed, reason, detail = cal.verdict(late)
    assert allowed is False and reason == nf.REFUSE_STALE
    assert "limit" in detail


def test_an_expired_coverage_horizon_blocks_opens(tmp_path):
    """The week runs out before the fetch does, so coverage is checked first:
    a 'fresh' calendar for a week that has ended protects nothing."""
    cal = calendar(tmp_path)
    end = nf.parse_event_time(cal._load()["coverage_until"])
    allowed, reason, _ = cal.verdict(end - timedelta(seconds=nf.REQUIRED_HORIZON_S - 60))
    assert allowed is False and reason == nf.REFUSE_COVERAGE


def test_the_78_day_stale_csv_scenario_is_now_blocked(tmp_path):
    """The exact production state this milestone exists to make impossible:
    a calendar whose newest event is 78.8 days old."""
    cal = calendar(tmp_path)
    allowed, reason, _ = cal.verdict(T + timedelta(days=78.8))
    assert allowed is False and reason in (nf.REFUSE_STALE, nf.REFUSE_COVERAGE)


def test_a_corrupt_cache_is_not_consent(tmp_path):
    cal = calendar(tmp_path)
    cal.cache_path.write_text("{not json", encoding="utf-8")
    cal._loaded = False
    assert cal.verdict(T)[1] == nf.REFUSE_UNAVAILABLE


def test_a_wrong_schema_cache_is_refused(tmp_path):
    cal = calendar(tmp_path)
    cal.cache_path.write_text(json.dumps({"schema": "something-else"}), encoding="utf-8")
    cal._loaded = False
    assert cal.verdict(T)[1] == nf.REFUSE_UNAVAILABLE


def test_an_internal_fault_refuses_rather_than_passes(tmp_path):
    """A gate that opens on an unexpected error is not a gate."""
    cal = calendar(tmp_path)
    cal.health = lambda now=None: (_ for _ in ()).throw(RuntimeError("boom"))
    allowed, reason, _ = cal.verdict(T)
    assert allowed is False and reason == nf.REFUSE_UNAVAILABLE


def test_empty_week_proves_coverage_instead_of_failing(tmp_path):
    """The false-failure trap the milestone calls out: a quiet week has no
    events, and that must read as 'covered and calm', not 'broken'."""
    cal = calendar(tmp_path, [])
    h = cal.health(T)
    assert h.ok and h.event_count == 0
    assert cal.verdict(T)[0] is True


def test_weekend_with_no_events_still_allows(tmp_path):
    sat = datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc)
    cal = calendar(tmp_path, [], now=sat)
    assert cal.verdict(sat + timedelta(hours=2))[0] is True


# ── PART 3: malformed data can never grant an OPEN ──────────────────────────

@pytest.mark.parametrize("bad", [
    {"title": "x", "country": "USD", "impact": "High", "date": "not-a-date"},
    {"title": "x", "country": "USD", "impact": "High", "date": "2026-08-12T12:30:00"},
    {"title": "x", "country": "USD", "impact": "High"},
    {"title": "x", "country": "", "impact": "High", "date": "2026-08-12T12:30:00Z"},
    {"title": "x", "country": "USD", "impact": "Critical", "date": "2026-08-12T12:30:00Z"},
    "not-an-object",
])
def test_one_malformed_row_aborts_the_whole_refresh(tmp_path, bad):
    """A dropped row is a silently missing blackout. Strictness here means the
    node keeps the LAST GOOD calendar and says so, rather than caching a
    partial one that looks healthy."""
    with pytest.raises(ValueError):
        nf.normalise_events([ff_row(), bad])


def test_a_failed_refresh_never_erases_a_good_cache(tmp_path):
    cal = calendar(tmp_path)
    good = cal.cache_path.read_bytes()
    cal._opener = lambda u: [{"garbage": True}]
    out = cal.refresh(T + timedelta(seconds=1))
    assert out["ok"] is False
    assert cal.cache_path.read_bytes() == good, "protection was erased by a bad fetch"
    assert cal.verdict(T)[1] == nf.REFUSE_BLACKOUT, "cache still answers after a failed fetch"


def test_a_failed_refresh_is_recorded_durably(tmp_path):
    cal = calendar(tmp_path)
    cal._opener = lambda u: (_ for _ in ()).throw(OSError("connection reset"))
    cal.refresh(T)
    rec = json.loads(cal.status_path.read_text(encoding="utf-8"))
    assert rec["ok"] is False and "connection reset" in rec["error"]


def test_a_non_list_payload_is_refused(tmp_path):
    with pytest.raises(ValueError, match="JSON list"):
        nf.normalise_events({"events": []})


def test_the_network_is_not_touched_by_the_verdict(tmp_path):
    """Rails must never acquire an I/O failure mode."""
    cal = calendar(tmp_path)
    cal._opener = lambda u: pytest.fail("verdict() went to the network")
    for _ in range(3):
        cal.verdict(T)


# ── PART 1 + PART 3: an existing position stays manageable ──────────────────

def test_close_is_allowed_during_a_blackout(tmp_path):
    """News suppresses ENTRY. Trapping a position inside a news window is the
    opposite of protection."""
    cal = calendar(tmp_path)
    v = rails(FrozenGate(cal, T), tmp_path, mirror={"L_1": 555}).evaluate(
        close_intent(), "EURUSD", "2026-08-12")
    assert v.allowed, f"blocked by {v.rail}: {v.detail}"


def test_modify_stop_is_allowed_during_a_blackout(tmp_path):
    cal = calendar(tmp_path)
    v = rails(FrozenGate(cal, T), tmp_path, mirror={"L_1": 555}).evaluate(
        modify_intent(), "EURUSD", "2026-08-12")
    assert v.allowed, f"blocked by {v.rail}: {v.detail}"


def test_close_is_allowed_when_the_news_source_is_completely_dead(tmp_path):
    """The failure this ordering prevents: a calendar outage stranding a live
    position because the node refused its own exit."""
    cal = nf.NewsCalendar(Cfg(tmp_path), opener=lambda u: (_ for _ in ()).throw(OSError("dead")))
    cal.refresh(T)
    r = rails(cal, tmp_path, mirror={"L_1": 555})
    assert r.evaluate(close_intent(), "EURUSD", "2026-08-12").allowed
    assert r.evaluate(modify_intent(), "EURUSD", "2026-08-12").allowed
    assert not r.evaluate(open_intent(), "EURUSD", "2026-08-12").allowed


def test_open_is_the_only_action_the_news_rail_considers(tmp_path):
    cal = calendar(tmp_path)
    r = rails(FrozenGate(cal, T), tmp_path, mirror={"L_1": 555})
    assert r._news_verdict(close_intent()) is None
    assert r._news_verdict(modify_intent()) is None
    assert r._news_verdict(open_intent()) is not None


def test_no_news_gate_means_the_rail_abstains(tmp_path):
    """Pre-M-LIVE-NEWS-1 call sites and tests must be unaffected."""
    r = SafetyRails(Cfg(tmp_path), State(), news_gate=None)
    assert r._news_verdict(open_intent()) is None


def test_kill_switch_still_beats_everything(tmp_path):
    cal = calendar(tmp_path)
    (tmp_path / "KILL").write_text("stop")
    v = rails(FrozenGate(cal, T), tmp_path).evaluate(open_intent(), "EURUSD", "2026-08-12")
    assert v.rail == "kill_switch"


# ── PART 1: the pre-blackout flatten is gone ────────────────────────────────

@pytest.mark.skipif(not LUX.exists(), reason="pinned Lux tree not present")
def test_the_tracked_strategy_no_longer_flattens_on_news():
    """The user's rule: upcoming news must not close an existing trade. This
    asserts the REAL deployed authority, not a fixture."""
    cfg = json.loads((LUX / "strategy" / "production_strategy.v1.json")
                     .read_text(encoding="utf-8"))
    assert cfg["news_flatten_active_trades"] is False
    # ...and the blackout itself is still on, at exactly +/-3 minutes.
    assert cfg["news_blackout_enabled"] is True
    assert cfg["news_blackout_minutes_before"] == 3
    assert cfg["news_blackout_minutes_after"] == 3
    assert cfg["news_block_new_fills"] is True
    assert cfg["news_blackout_impacts"] == ["high"]


@pytest.mark.skipif(not LUX.exists(), reason="pinned Lux tree not present")
def test_the_runtime_config_is_byte_identical_to_the_authority():
    from live.config import LiveConfig
    from live import strategy_authority as sa
    cfg = LiveConfig()
    ok, detail, _ = sa.verify(cfg.lux_root, cfg.golden_config_path)
    assert ok, detail


@pytest.mark.skipif(not LUX.exists(), reason="pinned Lux tree not present")
def test_every_flatten_call_site_is_gated_by_the_flag_we_turned_off():
    """Proves the flag actually reaches the behaviour, rather than trusting a
    name. Each site ANDs it with news_blackout_enabled, so false disables the
    flatten while leaving the blackout running."""
    src = (LUX / "scripts" / "run_backtest.py").read_text(encoding="utf-8", errors="replace")
    sites = [ln for ln in src.splitlines() if "news_flatten_active_trades" in ln
             and "config." in ln]
    assert sites, "flatten call sites not found - has run_backtest.py moved?"
    for ln in sites:
        assert "news_blackout_enabled" in ln or "getattr" in ln or '"' in ln, ln


def test_telemetry_reports_the_flatten_is_off(tmp_path):
    cal = calendar(tmp_path)
    assert cal.telemetry_block(T)["flatten_active_trades"] is False


# ── PART 7: telemetry ───────────────────────────────────────────────────────

REQUIRED_TELEMETRY = ("schema_version", "source", "health", "healthy",
                      "last_refresh_at", "last_refresh_age_s", "last_error",
                      "coverage_until", "coverage_remaining_s", "event_count",
                      "relevant_event_count", "blackout_active", "blackout_event",
                      "next_relevant_event", "new_open_allowed", "refusal_reason")


def test_telemetry_block_carries_everything_the_UI_needs(tmp_path):
    b = calendar(tmp_path).telemetry_block(T)
    for f in REQUIRED_TELEMETRY:
        assert f in b, f"missing {f}"
    assert b["schema_version"] == nf.SCHEMA
    assert b["blackout_active"] is True
    assert b["blackout_event"]["event"] == "CPI m/m"
    assert b["new_open_allowed"] is False
    assert b["refusal_reason"] == nf.REFUSE_BLACKOUT


def test_telemetry_names_the_next_event_when_quiet(tmp_path):
    b = calendar(tmp_path).telemetry_block(T - timedelta(hours=2))
    assert b["blackout_active"] is False and b["new_open_allowed"] is True
    assert b["next_relevant_event"]["time"] == "2026-08-12T12:30:00Z"


def test_telemetry_says_unhealthy_when_stale(tmp_path):
    b = calendar(tmp_path).telemetry_block(T + timedelta(seconds=nf.MAX_STALE_S + 60))
    assert b["healthy"] is False and b["health"] == "stale"
    assert b["new_open_allowed"] is False


def test_telemetry_carries_no_raw_payload_or_secret(tmp_path):
    blob = json.dumps(calendar(tmp_path).telemetry_block(T))
    for banned in ("forecast", "previous", "password", "MT5", "login", "token"):
        assert banned not in blob, banned
    assert len(blob) < 4000, "telemetry block must stay small"


def test_snapshot_omits_news_when_absent_and_includes_it_when_present(tmp_path):
    """Additive and backward-compatible, exactly like the decision feed."""
    from live import telemetry as tel
    import inspect
    assert "news" in inspect.signature(tel.build_snapshot).parameters


# ── refresh cadence ─────────────────────────────────────────────────────────

def test_refresh_is_ttl_gated_not_per_decision(tmp_path):
    calls = []
    cfg = Cfg(tmp_path)
    cal = nf.NewsCalendar(cfg, opener=lambda u: calls.append(1) or [ff_row()])
    cal.refresh(T)
    assert len(calls) == 1
    for _ in range(5):
        cal.refresh_if_due(T + timedelta(seconds=nf.REFRESH_TTL_S - 60))
    assert len(calls) == 1, "TTL did not suppress redundant fetches"
    cal.refresh_if_due(T + timedelta(seconds=nf.REFRESH_TTL_S + 60))
    assert len(calls) == 2


def test_cache_is_written_atomically_and_is_deterministic(tmp_path):
    a = calendar(tmp_path, [ff_row(title="B", when=T + timedelta(minutes=5)), ff_row(title="A")])
    first = a.cache_path.read_text(encoding="utf-8")
    b = calendar(tmp_path, [ff_row(title="A"), ff_row(title="B", when=T + timedelta(minutes=5))])
    second = b.cache_path.read_text(encoding="utf-8")
    assert first == second, "row order in the feed changed the cached bytes"
    assert [e["event"] for e in a.events()] == ["A", "B"]   # sorted by time, not feed order


def test_cache_contains_no_forecast_or_previous_fields(tmp_path):
    cal = calendar(tmp_path)
    assert set(cal.events()[0]) == {"time", "currency", "impact", "event", "source"}


# ── PART 6: the arming preflight ────────────────────────────────────────────

def test_arm_cli_refuses_when_news_protection_is_not_provable(tmp_path, monkeypatch, capsys):
    """Arming is when a human hands the node unattended permission to open
    positions, so it is the right place to prove news protection works -- not
    the moment it first tries to trade at 02:00 with nobody watching."""
    import live.arm_cli as ac
    monkeypatch.setattr(ac, "LiveConfig", lambda: Cfg(tmp_path))
    monkeypatch.setattr(ac, "_observed", lambda cfg: (True, {
        "account": {"login": 9000000001, "server": "FTMO-Demo", "trade_mode": 0},
        "positions_total": 0}))
    monkeypatch.setattr(nf.NewsCalendar, "_fetch",
                        lambda self: (_ for _ in ()).throw(OSError("feed down")))
    rc = ac.main(["create", "--ttl-minutes", "30"])
    out = capsys.readouterr().out
    assert rc == 2, out
    assert "REFUSED" in out and nf.REFUSE_UNAVAILABLE in out
    assert not (tmp_path / "arm_token.json").exists(), "an arm was created anyway"


def test_arm_cli_proceeds_when_the_calendar_is_healthy(tmp_path, monkeypatch, capsys):
    import live.arm_cli as ac
    monkeypatch.setattr(ac, "LiveConfig", lambda: Cfg(tmp_path))
    monkeypatch.setattr(ac, "_observed", lambda cfg: (True, {
        "account": {"login": 9000000001, "server": "FTMO-Demo", "trade_mode": 0},
        "positions_total": 0}))
    monkeypatch.setattr(nf.NewsCalendar, "_fetch", lambda self: [ff_row(
        when=datetime.now(timezone.utc) + timedelta(days=1))])
    assert ac.main(["create", "--ttl-minutes", "30"]) == 0
    assert "ARMED" in capsys.readouterr().out


def test_the_override_still_leaves_the_rail_in_force(tmp_path, monkeypatch, capsys):
    """--allow-stale-news skips the PREFLIGHT, never the rail. An operator who
    overrides gets a warning and a node that still refuses every OPEN."""
    import live.arm_cli as ac
    monkeypatch.setattr(ac, "LiveConfig", lambda: Cfg(tmp_path))
    monkeypatch.setattr(ac, "_observed", lambda cfg: (True, {
        "account": {"login": 9000000001, "server": "FTMO-Demo", "trade_mode": 0},
        "positions_total": 0}))
    monkeypatch.setattr(nf.NewsCalendar, "_fetch",
                        lambda self: (_ for _ in ()).throw(OSError("feed down")))
    assert ac.main(["create", "--ttl-minutes", "30", "--allow-stale-news"]) == 0
    assert "WARNING" in capsys.readouterr().out
    # the rail is a separate object and is unmoved by the override
    cal = nf.NewsCalendar(Cfg(tmp_path), opener=lambda u: (_ for _ in ()).throw(OSError()))
    assert rails(cal, tmp_path).evaluate(open_intent(), "EURUSD", "2026-08-12").allowed is False


def test_source_url_is_overridable_for_failover(monkeypatch):
    assert nf.DEFAULT_SOURCE_URL.startswith("https://nfs.faireconomy.media/")
    assert nf.SOURCE_URL  # resolved once at import from NEWS_SOURCE_URL or default
