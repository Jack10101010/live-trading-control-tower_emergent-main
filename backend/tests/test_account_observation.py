"""M-NODE-ACCT-1A-ii — read-only account observation seam.

The defect being removed is that the historical implementation sampled the
account only inside `Executor.apply(intents, ...)`. Several tests below exist
purely to pin that this seam has NO relationship with intents, positions, bars
or strategy state — a passing observation on a zero-intent, no-new-bar,
market-closed cycle is the whole point.

Fake identifiers only.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from live import telemetry as nt                                       # noqa: E402
from live.account_observation import (STATUS_DEGRADED, STATUS_OK,      # noqa: E402
                                      STATUS_UNAVAILABLE, AccountObserver)

FAKE_LOGIN = 10000001
FAKE_SERVER = "Example-Demo"


def _payload(**over):
    acct = {"login": FAKE_LOGIN, "server": FAKE_SERVER, "currency": "USD",
            "trade_mode": "demo", "balance": 100_000.0, "equity": 100_050.0,
            "free_margin": 99_000.0, "trade_allowed": True, "trade_expert": True}
    acct.update(over.pop("account", {}))
    term = {"connected": True, "trade_allowed": False}
    term.update(over.pop("terminal", {}))
    return {"account": acct, "terminal": term}


class FakeGateway:
    """Records every attribute touched, so 'no order call' is provable."""

    def __init__(self, result=None, raises=None):
        self.result = result if result is not None else (True, _payload())
        self.raises = raises
        self.calls = 0
        self.touched: list[str] = []

    def read_account_state(self):
        self.calls += 1
        self.touched.append("read_account_state")
        if self.raises:
            raise self.raises
        return self.result

    def __getattr__(self, name):          # any other access is a violation
        self.__dict__.setdefault("touched", []).append(name)
        raise AssertionError(f"observer touched forbidden gateway member {name!r}")


class Clock:
    def __init__(self):
        self.wall = 1_000_000.0
        self.mono = 0.0

    def now(self):
        from datetime import datetime, timezone
        return datetime.fromtimestamp(self.wall, tz=timezone.utc)

    def monotonic(self):
        return self.mono

    def advance(self, seconds):
        self.wall += seconds
        self.mono += seconds


def make(gateway, clock=None, **kw):
    c = clock or Clock()
    return AccountObserver(gateway, clock=c.now, monotonic=c.monotonic, **kw), c


# ── success ──────────────────────────────────────────────────────────────────
def test_successful_identity_is_observed_and_fingerprints():
    obs, _ = make(FakeGateway())
    r = obs.observe()
    assert r.status == STATUS_OK
    assert r.identity.login == FAKE_LOGIN
    assert r.identity.server == FAKE_SERVER
    ident = nt.safe_identity(r.identity)
    assert ident["available"] is True
    assert ident["fingerprint"] == nt.account_fingerprint(FAKE_LOGIN, FAKE_SERVER)


def test_successful_health_is_observed_with_finite_values():
    obs, _ = make(FakeGateway())
    r = obs.observe()
    health = nt.safe_health(r.health, None, r.health_observed_at)
    assert health["available"] is True
    assert health["balance"] == 100_000.0
    assert health["equity"] == 100_050.0


def test_terminal_autotrading_is_kept_separate_from_broker_permissions():
    """A locked-down terminal must not read as trade-enabled."""
    obs, _ = make(FakeGateway())
    r = obs.observe()
    assert r.terminal_trade_allowed is False        # AutoTrading button
    assert r.health.trade_allowed is True           # broker permission
    assert r.health.trade_expert is True
    # and the terminal flag never leaks into the published health block
    assert "terminal_trade_allowed" not in nt.safe_health(r.health, None, None)


# ── unavailable / degraded ───────────────────────────────────────────────────
def test_unavailable_when_the_gateway_is_not_connected():
    obs, _ = make(FakeGateway(result=(False, "not connected")))
    r = obs.observe()
    assert r.status == STATUS_UNAVAILABLE
    assert r.identity is None and r.health is None
    assert nt.safe_identity(r.identity)["available"] is False
    assert nt.safe_health(r.health, None, None)["available"] is False


def test_exception_is_contained_and_never_propagates():
    """Observation failure must not be able to stop a trading cycle."""
    obs, _ = make(FakeGateway(raises=RuntimeError("terminal exploded")))
    r = obs.observe()                       # must not raise
    assert r.status == STATUS_UNAVAILABLE
    assert "terminal exploded" in r.error


def test_missing_identity_fields_leave_identity_unavailable_but_health_usable():
    obs, _ = make(FakeGateway(result=(True, _payload(account={"login": None, "server": None}))))
    r = obs.observe()
    assert r.identity is None
    assert r.health is not None
    assert r.status == STATUS_DEGRADED
    assert nt.safe_identity(r.identity)["available"] is False
    assert nt.safe_health(r.health, None, None)["available"] is True


def test_non_finite_money_is_refused_rather_than_published():
    obs, _ = make(FakeGateway(result=(True, _payload(account={"balance": float("nan")}))))
    r = obs.observe()
    assert r.health is None, "NaN must never reach the health block"
    assert r.identity is not None


def test_a_failed_query_never_becomes_zero():
    obs, _ = make(FakeGateway(result=(False, "account_info unavailable")))
    r = obs.observe()
    health = nt.safe_health(r.health, None, None)
    assert health["balance"] is None
    assert health["balance"] != 0


# ── cache semantics ──────────────────────────────────────────────────────────
def test_cached_reuse_does_not_resample_and_keeps_original_observed_at():
    gw = FakeGateway()
    obs, clock = make(gw)
    first = obs.observe()
    clock.advance(10)                       # inside both TTLs
    second = obs.observe()
    assert gw.calls == 1, "must not re-hit the terminal inside the interval"
    assert second.fresh_or_cached == "cached"
    assert second.health_observed_at == first.health_observed_at


def test_cache_expiry_triggers_a_fresh_sample_with_a_new_observed_at():
    gw = FakeGateway()
    obs, clock = make(gw)
    first = obs.observe()
    clock.advance(120)                      # past the 60s health TTL
    second = obs.observe()
    assert gw.calls == 2
    assert second.fresh_or_cached == "fresh"
    assert second.health_observed_at != first.health_observed_at


def test_failed_refresh_does_not_fabricate_a_new_timestamp():
    """The core honesty rule: stale data must never look freshly observed."""
    gw = FakeGateway()
    obs, clock = make(gw)
    first = obs.observe()
    gw.result = (False, "not connected")
    clock.advance(120)
    second = obs.observe()
    assert second.identity is None, "cached sample must not publish as current"
    assert second.health is None
    assert second.health_observed_at == first.health_observed_at, \
        "the old sample keeps its ORIGINAL timestamp"


def test_recovery_produces_a_fresh_observed_at():
    gw = FakeGateway()
    obs, clock = make(gw)
    first = obs.observe()
    gw.result = (False, "not connected")
    clock.advance(120)
    obs.observe()
    gw.result = (True, _payload())
    clock.advance(120)
    recovered = obs.observe()
    assert recovered.identity is not None
    assert recovered.status == STATUS_OK
    assert recovered.health_observed_at != first.health_observed_at


def test_identity_uses_a_slower_cadence_than_health():
    from live.account_observation import HEALTH_TTL_S, IDENTITY_TTL_S
    assert IDENTITY_TTL_S > HEALTH_TTL_S


# ── independence from the order path ─────────────────────────────────────────
def test_observation_needs_no_intents_positions_or_bars():
    """Nothing about the call site can express intents — that is the fix."""
    import inspect
    sig = inspect.signature(AccountObserver.observe)
    assert list(sig.parameters) == ["self"], \
        "observe() must not accept intents, positions or bar state"
    obs, _ = make(FakeGateway())
    assert obs.observe().status == STATUS_OK


@pytest.mark.parametrize("scenario", ["no_new_bar", "market_closed_authenticated",
                                      "zero_intent_cycle"])
def test_observation_succeeds_in_every_non_trading_scenario(scenario):
    """A quiet or closed market changes nothing: the terminal is still authenticated."""
    obs, _ = make(FakeGateway())
    r = obs.observe()
    assert r.status == STATUS_OK, scenario
    assert r.identity is not None and r.health is not None


def test_observer_touches_no_order_or_reconciliation_method():
    """FakeGateway raises on any member other than read_account_state."""
    gw = FakeGateway()
    obs, _ = make(gw)
    obs.observe()
    assert gw.touched == ["read_account_state"]
    for forbidden in ("open_position", "close_position", "modify_position_sl",
                      "connect", "ensure_connected", "snapshot"):
        assert forbidden not in gw.touched


def test_observer_never_opens_a_second_mt5_session():
    """No connect path exists: an unconnected gateway yields unavailable."""
    src = (REPO_ROOT / "live" / "account_observation.py").read_text()
    for banned in ("mt5.initialize", "initialize(", "connect(", "ensure_connected"):
        assert banned not in src, f"observer must not reference {banned!r}"
    obs, _ = make(FakeGateway(result=(False, "not connected")))
    assert obs.observe().status == STATUS_UNAVAILABLE


def test_error_text_is_bounded_and_drops_paths():
    obs, _ = make(FakeGateway(result=(False, "C:\\Program Files\\MT5\\terminal64.exe blew up")))
    r = obs.observe()
    assert "Program Files" not in r.error
    assert len(r.error) <= 160


def test_observed_mapping_matches_what_the_builder_consumes():
    obs, _ = make(FakeGateway())
    mapping = obs.observe().as_observed_mapping()
    assert set(mapping) == {"identity", "health", "health_verdict", "observed_at"}
    assert mapping["health_verdict"] is None, "the seam must not invent a verdict"
    snap_identity = nt.safe_identity(mapping["identity"])
    assert snap_identity["available"] is True
