"""M-LIVE-STALE-OPEN-GUARDS-1 — a modelled fill must still be executable.

THE INCIDENT (S_2108, 2026-08-14, forensically established). The engine modelled
a fill at 07:52:00Z @ 1.15493. The cycle that processed that boundary ran
08:09:13 -> 08:30:25, so a MARKET order went in at 08:30:24 and filled at
1.15527 — 38m24s late and 3.4 pips away, 45.95% of the trade's own 7.4-pip risk.
Direction, gating, arm, delay and entry geometry were all CORRECT; the price was
not. `net_r -1.081` was reported against a real -$0.40.

Two interim execution guards, both OPEN-only, both fail-closed:

  * stale_open_wallclock  — elapsed time since the modelled fill  (limit 15m)
  * entry_price_divergence — executable price vs modelled entry   (limit 0.25R)

Deliberately SEPARATE from the pre-existing `stale_open`, which is frontier/bar
ORDERING and is unchanged. S_2108 satisfied ordering perfectly (fill 07:52 >=
frontier 07:45) and was still 38 minutes stale — that is exactly why a second,
different concept is needed rather than an overload of the first.

No network, no MT5, no production live_state, no order ever submitted.
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

from live.config import OPEN_MAX_AGE_S, OPEN_MAX_DIVERGENCE_R  # noqa: E402
from live.intents import (CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION,  # noqa: E402
                          OrderIntent)
from live.safety import (R_PRICE_DIVERGENCE, R_STALE_WALLCLOCK,  # noqa: E402
                         SafetyRails)

# ── the locked S_2108 fixture, from the forensic ────────────────────────────
S2108 = {
    "entry": 1.15493,          # canonical OB bottom edge, entry_level_pct 0
    "stop": 1.15567,           # top + 1 pip buffer
    "risk": 0.00074,           # 7.4 pips
    "fill_time": "2026-08-14 07:52:00+00:00",
    "frontier": "2026-08-14 07:45:00+00:00",
    "submitted_at": datetime(2026, 8, 14, 8, 30, 24, tzinfo=timezone.utc),
    "broker_fill": 1.15527,    # what actually executed
}


class Cfg:
    def __init__(self, root, mode="live"):
        self.state_dir = root
        self.kill_file = root / "KILL"
        self.mode = mode
        self.broker_symbol = "EURUSD"
        self.daily_loss_limit_r = 5.0
        self.max_open_positions = 6
        self.fixed_risk_lots = 0.01


class State:
    """Records every ledger write so side-effect freedom is provable."""
    def __init__(self):
        self.ledger_writes = []
    def ledger_status(self, i): return None
    def ledger_set(self, *a, **k): self.ledger_writes.append(a)
    def mirror_ticket(self, t): return 555
    def broker_closed_ticket(self, t): return None
    def daily_realized_r(self, d): return 0.0
    def open_mirror_count(self): return 0
    def record_block(self, *a, **k): self.ledger_writes.append(("record_block",))


def quote(bid=None, ask=None, ok=True, err="no tick"):
    """Injected executable quote. `ok=False` models an unavailable tick."""
    def provider():
        if not ok:
            return False, err
        return True, {"bid": bid, "ask": ask, "at": "2026-08-14T08:30:24+00:00"}
    return provider


def rails(tmp_path, *, now, quote_provider, mode="live"):
    """Rails with the preceding gates deliberately quiet (dry_run-equivalent
    arming abstention is NOT used here — mode stays live but arm_runtime is
    None only where irrelevant), so a refusal can only come from the guard
    under test. Both new guards run LAST in evaluate()."""
    return SafetyRails(Cfg(tmp_path, mode), State(), arm_runtime=None,
                       observed_account={}, news_gate=None,
                       quote_provider=quote_provider, clock=lambda: now)


def open_intent(*, entry=S2108["entry"], stop=S2108["stop"],
                fill_time=S2108["fill_time"], side="short", iid="i1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id="S_2108",
                       side=side, frontier_bar=S2108["frontier"],
                       entry=entry, stop=stop, target=1.152895,
                       fill_time=fill_time)


def guard(r, intent):
    """Run ONLY the two new guards, so other rails cannot mask the result."""
    return (r._wallclock_freshness_verdict(intent),
            r._price_divergence_verdict(intent))


# ── 2/6. THE S_2108 REGRESSION — both guards, independently ─────────────────

def test_S2108_is_refused_by_wallclock_freshness(tmp_path):
    r = rails(tmp_path, now=S2108["submitted_at"],
              quote_provider=quote(bid=S2108["broker_fill"], ask=S2108["broker_fill"] + 0.00001))
    v = r._wallclock_freshness_verdict(open_intent())
    assert v is not None and not v.allowed
    assert v.rail == R_STALE_WALLCLOCK
    age = v.evidence["age_seconds"]
    assert 2300 < age < 2320, f"expected ~38m24s, got {age}s"
    assert v.evidence["max_age_seconds"] == 900


def test_S2108_is_INDEPENDENTLY_refused_by_price_divergence(tmp_path):
    """Even with the clock guard satisfied, the price alone must refuse."""
    r = rails(tmp_path, now=S2108["submitted_at"],
              quote_provider=quote(bid=S2108["broker_fill"], ask=S2108["broker_fill"] + 0.00001))
    v = r._price_divergence_verdict(open_intent())
    assert v is not None and not v.allowed and v.rail == R_PRICE_DIVERGENCE
    e = v.evidence
    assert e["quote_side"] == "bid", "a SELL must be compared against the BID"
    assert abs(e["divergence_price"] - 0.00034) < 1e-9
    assert 0.458 < e["divergence_r"] < 0.460, e["divergence_r"]
    assert e["max_divergence_r"] == 0.25


def test_S2108_is_refused_by_the_FULL_rail_stack(tmp_path):
    # dry_run so the ARM rail abstains and the refusal can only be one of the
    # two new guards; both are deliberately mode-independent, exactly like the
    # news rail, so dry_run stays an honest simulation of live.
    r = rails(tmp_path, now=S2108["submitted_at"], mode="dry_run",
              quote_provider=quote(bid=S2108["broker_fill"], ask=S2108["broker_fill"] + 0.00001))
    v = r.evaluate(open_intent(), "EURUSD", "2026-08-14")
    assert not v.allowed and v.rail in (R_STALE_WALLCLOCK, R_PRICE_DIVERGENCE)


def test_the_pre_existing_frontier_rail_would_NOT_have_caught_S2108(tmp_path):
    """Why a second concept was needed: ordering was perfectly satisfied."""
    r = rails(tmp_path, now=S2108["submitted_at"], quote_provider=quote(1.15493, 1.15494))
    assert r._stale_open_verdict(open_intent()) is None, \
        "the ordering rail must remain unchanged and silent here"


# ── 7. POSITIVE CASES — mandatory ───────────────────────────────────────────

def _fresh(offset_s=60):
    ft = datetime(2026, 8, 14, 7, 52, tzinfo=timezone.utc)
    return ft + timedelta(seconds=offset_s)


def test_A_fresh_open_at_the_exact_entry_is_ALLOWED(tmp_path):
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=1.15493, ask=1.15494))
    assert guard(r, open_intent()) == (None, None)


def test_B_fresh_open_with_small_divergence_is_ALLOWED(tmp_path):
    # 1 pip on 7.4 pips of risk = 0.135R
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=1.15503, ask=1.15504))
    assert guard(r, open_intent()) == (None, None)


def test_C_divergence_exactly_at_the_limit_is_ALLOWED(tmp_path):
    """Boundary pinned as inclusive: 0.25R passes, > 0.25R refuses."""
    entry, risk = S2108["entry"], S2108["risk"]
    exact = entry - OPEN_MAX_DIVERGENCE_R * risk          # SELL -> compared to BID
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=exact, ask=exact + 0.00001))
    v = r._price_divergence_verdict(open_intent())
    assert v is None, f"exactly {OPEN_MAX_DIVERGENCE_R}R must pass (inclusive boundary)"


def test_D_divergence_just_over_the_limit_is_REFUSED(tmp_path):
    entry, risk = S2108["entry"], S2108["risk"]
    over = entry - (OPEN_MAX_DIVERGENCE_R * risk + 0.00001)
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=over, ask=over + 0.00001))
    v = r._price_divergence_verdict(open_intent())
    assert v is not None and v.rail == R_PRICE_DIVERGENCE


def test_E_age_exactly_at_the_limit_is_ALLOWED(tmp_path):
    r = rails(tmp_path, now=_fresh(OPEN_MAX_AGE_S), quote_provider=quote(1.15493, 1.15494))
    assert r._wallclock_freshness_verdict(open_intent()) is None


def test_F_age_one_second_over_the_limit_is_REFUSED(tmp_path):
    r = rails(tmp_path, now=_fresh(OPEN_MAX_AGE_S + 1), quote_provider=quote(1.15493, 1.15494))
    v = r._wallclock_freshness_verdict(open_intent())
    assert v is not None and v.rail == R_STALE_WALLCLOCK


# ── BUY / SELL directionality — proven, not assumed ─────────────────────────

def test_a_BUY_is_compared_against_the_ASK(tmp_path):
    """Using the bid for a BUY would understate divergence by the spread."""
    entry, stop = 1.15493, 1.15419          # long: stop below entry, risk 7.4 pips
    # ask is 0.30R away, bid only 0.16R -> must refuse on the ASK
    ask = entry + 0.30 * 0.00074
    bid = entry + 0.16 * 0.00074
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=bid, ask=ask))
    v = r._price_divergence_verdict(open_intent(entry=entry, stop=stop, side="long"))
    assert v is not None and v.evidence["quote_side"] == "ask"
    assert v.evidence["executable_price"] == ask


def test_a_SELL_is_compared_against_the_BID(tmp_path):
    entry, stop = 1.15493, 1.15567
    bid = entry - 0.30 * 0.00074
    ask = entry - 0.16 * 0.00074
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=bid, ask=ask))
    v = r._price_divergence_verdict(open_intent(entry=entry, stop=stop, side="short"))
    assert v is not None and v.evidence["quote_side"] == "bid"
    assert v.evidence["executable_price"] == bid


def test_a_fresh_BUY_at_the_ask_is_ALLOWED(tmp_path):
    entry, stop = 1.15493, 1.15419
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=entry - 0.00001, ask=entry))
    assert guard(r, open_intent(entry=entry, stop=stop, side="long")) == (None, None)


# ── fail-closed inputs ──────────────────────────────────────────────────────

def test_H_missing_quote_is_REFUSED(tmp_path):
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(ok=False, err="no tick"))
    v = r._price_divergence_verdict(open_intent())
    assert v is not None and v.rail == R_PRICE_DIVERGENCE
    assert "no tick" in v.evidence["quote_error"]


def test_no_quote_provider_at_all_is_REFUSED(tmp_path):
    r = rails(tmp_path, now=_fresh(), quote_provider=None)
    v = r._price_divergence_verdict(open_intent())
    assert v is not None and v.rail == R_PRICE_DIVERGENCE


def test_a_raising_quote_provider_is_REFUSED_not_propagated(tmp_path):
    def boom():
        raise RuntimeError("terminal wedged")
    r = rails(tmp_path, now=_fresh(), quote_provider=boom)
    v = r._price_divergence_verdict(open_intent())
    assert v is not None and v.rail == R_PRICE_DIVERGENCE


@pytest.mark.parametrize("side_val", [None, float("nan"), "abc"])
def test_a_non_finite_quote_side_is_REFUSED(tmp_path, side_val):
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=side_val, ask=side_val))
    assert r._price_divergence_verdict(open_intent()) is not None


@pytest.mark.parametrize("bad", [None, "", "nan", "not-a-time"])
def test_I_missing_or_invalid_fill_time_is_REFUSED(tmp_path, bad):
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(1.15493, 1.15494))
    v = r._wallclock_freshness_verdict(open_intent(fill_time=bad))
    assert v is not None and v.rail == R_STALE_WALLCLOCK


def test_a_FUTURE_fill_time_fails_closed(tmp_path):
    r = rails(tmp_path, now=_fresh(-600), quote_provider=quote(1.15493, 1.15494))
    v = r._wallclock_freshness_verdict(open_intent())
    assert v is not None and "FUTURE" in v.detail


@pytest.mark.parametrize("entry,stop", [(1.15493, 1.15493), (None, 1.15567), (1.15493, None)])
def test_J_invalid_or_zero_risk_is_REFUSED(tmp_path, entry, stop):
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(1.15493, 1.15494))
    v = r._price_divergence_verdict(open_intent(entry=entry, stop=stop))
    assert v is not None and v.rail == R_PRICE_DIVERGENCE


def test_risk_is_the_MODEL_risk_not_recomputed_from_current_price(tmp_path):
    """A drifted entry must not be allowed to redefine its own tolerance."""
    entry, stop = S2108["entry"], S2108["stop"]
    px = 1.15550                                   # close to the stop
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=px, ask=px + 0.00001))
    v = r._price_divergence_verdict(open_intent())
    assert v is not None
    assert abs(v.evidence["model_risk"] - abs(entry - stop)) < 1e-12
    # if risk had been recomputed from px it would be ~0.00017 and divergence
    # would read >3R; the model risk keeps it honest at ~0.77R
    assert 0.76 < v.evidence["divergence_r"] < 0.78


# ── K. OPEN-only: CLOSE and MODIFY are untouched ────────────────────────────

@pytest.mark.parametrize("action", [CLOSE_POSITION, MODIFY_STOP])
def test_K_close_and_modify_are_unaffected_by_both_guards(tmp_path, action):
    """A node that cannot exit is the worse failure. Both guards abstain."""
    i = OrderIntent(intent_id="c1", action=action, trade_id="S_2108", side="short",
                    frontier_bar=S2108["frontier"], stop=1.15567,
                    fill_time="2020-01-01 00:00:00+00:00")     # ancient on purpose
    r = rails(tmp_path, now=S2108["submitted_at"], quote_provider=quote(ok=False))
    assert guard(r, i) == (None, None)


def test_the_inclusive_boundary_is_not_decided_by_float_noise(tmp_path):
    """A value mathematically AT the limit must pass every time, not depend on
    which side of the limit its float representation lands."""
    entry, risk = S2108["entry"], S2108["risk"]
    for scale in (0.2499999, 0.25, 0.2500000001):
        px = entry - scale * risk
        r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=px, ask=px + 1e-5))
        v = r._price_divergence_verdict(open_intent())
        assert v is None, f"{scale}R should be allowed at/under the limit"
    px = entry - 0.2501 * risk
    r = rails(tmp_path, now=_fresh(), quote_provider=quote(bid=px, ask=px + 1e-5))
    assert r._price_divergence_verdict(open_intent()) is not None


def test_a_stale_CLOSE_still_passes_the_full_stack(tmp_path):
    i = OrderIntent(intent_id="c2", action=CLOSE_POSITION, trade_id="S_2108",
                    side="short", frontier_bar=S2108["frontier"],
                    fill_time="2020-01-01 00:00:00+00:00")
    r = rails(tmp_path, now=S2108["submitted_at"], quote_provider=quote(ok=False))
    assert r.evaluate(i, "EURUSD", "2026-08-14").allowed


# ── 8. side-effect safety, proven with spies ────────────────────────────────

def test_the_guards_write_no_ledger_record_and_place_no_order(tmp_path):
    st = State()
    calls = []
    def spy_quote():
        calls.append("quote")
        return True, {"bid": 1.15527, "ask": 1.15528, "at": "x"}
    r = SafetyRails(Cfg(tmp_path), st, arm_runtime=None, observed_account={},
                    news_gate=None, quote_provider=spy_quote,
                    clock=lambda: S2108["submitted_at"])
    for _ in range(20):
        r._wallclock_freshness_verdict(open_intent())
        r._price_divergence_verdict(open_intent())
    assert st.ledger_writes == [], f"guards mutated the ledger: {st.ledger_writes}"
    assert calls, "the divergence guard never sampled a quote"


def test_the_quote_provider_is_read_only_by_construction(tmp_path):
    """The gateway method the executor wires in must not be an order path."""
    import inspect
    from live.mt5_gateway import MT5Gateway
    src = inspect.getsource(MT5Gateway.current_quote)
    for banned in ("order_send", "TRADE_ACTION", "ORDER_TYPE"):
        assert banned not in src, f"current_quote references {banned}"


def test_evidence_is_recorded_for_a_refusal(tmp_path):
    st = State()
    r = SafetyRails(Cfg(tmp_path), st, arm_runtime=None, observed_account={},
                    news_gate=None, quote_provider=quote(1.15527, 1.15528),
                    clock=lambda: S2108["submitted_at"])
    v = r._price_divergence_verdict(open_intent())
    r.record_block(open_intent(), v)
    written = st.ledger_writes[-1]
    payload = written[2] if len(written) > 2 else {}
    assert payload["rail"] == R_PRICE_DIVERGENCE
    for f in ("canonical_entry", "canonical_stop", "model_risk", "quote_side",
              "executable_price", "divergence_price", "divergence_r",
              "max_divergence_r"):
        assert f in payload["evidence"], f"missing {f}"


def test_no_account_or_secret_in_the_evidence(tmp_path):
    import os
    r = rails(tmp_path, now=S2108["submitted_at"], quote_provider=quote(1.15527, 1.15528))
    blob = json.dumps([v.evidence for v in guard(r, open_intent()) if v])
    for banned in ("login", "password", "server", "token"):
        assert banned not in blob, banned
    real = os.environ.get("MT5_LOGIN", "").strip()
    if real.isdigit():
        assert real not in blob


# ── 10. the C0 delay expression is untouched by this milestone ──────────────

def test_the_C0_delay_expression_is_unchanged():
    """BACKTEST-vs-INTENDED C0 DELAY PARITY — OPEN. This milestone must not
    move N+3; the parity question is decided separately against the original
    research, not from memory."""
    lux = REPO_ROOT.parent / "Lux-OB-Backtester" / "strategy_core" / "execution.py"
    if not lux.exists():
        pytest.skip("pinned Lux tree not present")
    src = lux.read_text(encoding="utf-8", errors="replace")
    assert 'candle_index >= item.get("trigger_candle_index") + item.get("trigger_delay_candles", 0)' in src, \
        "the C0 delay expression changed — out of scope for this milestone"
