"""LX-1 Slice 3 — executor OPEN result integration (live/executor.py).

Drives the REAL Executor + REAL MT5Gateway with an injected fake sdk in live
mode, one OPEN intent, isolated state per test. Proves each typed disposition
maps to the correct durable ledger/mirror outcome, that order_send is called at
most once, and that dry-run and the duplicate rail are unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
for p in (str(REPO_ROOT), str(BACKEND_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

from live.config import LiveConfig                        # noqa: E402
from live.executor import Executor                        # noqa: E402
from live.mt5_gateway import MT5Gateway                   # noqa: E402
from live.intents import OrderIntent, OPEN_POSITION       # noqa: E402
from live.state import (RunnerState, LEDGER_CONFIRMED, LEDGER_PARTIAL, LEDGER_FAILED,  # noqa: E402
                        LEDGER_SENT, LEDGER_SIMULATED)
import _fake_mt5 as F                                     # noqa: E402

_SI = F.make_symbol_info(volume_step=0.01, filling_mode=F.SYMBOL_FILLING_IOC)
_TICK = (1.10101, 1.10123)


def _cfg(tmp_path, mode="live"):
    # LIVE_MIN_EQUITY set so the Slice-7 health rail can build its policy; the fake
    # account (equity 10000 EUR, trade_allowed/expert True) is healthy above it.
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "st",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "st" / "KILL",
                   min_equity_raw="5000")
    c.mode = mode
    c.ensure_dirs()
    return c


def _intent(iid="ex1", trade_id="T1"):
    return OrderIntent(intent_id=iid, action=OPEN_POSITION, trade_id=trade_id, side="long",
                       frontier_bar="B1", stop=1.09000, target=1.11000)


def _run(tmp_path, *, order_result=..., order_exc=None, positions=None, mode="live"):
    cfg = _cfg(tmp_path, mode)
    # Fresh tick (age ~1s) so the LX-1 Slice 5 feed-freshness rail passes; spread
    # 2.2 pips is well below the 5-pip ceiling — OPENs follow the normal path.
    fake = F.FakeMT5(tick=F.fresh_tick(*_TICK), symbol_info=_SI, account=F.make_account(),
                     order_result=order_result, order_exc=order_exc, positions=positions or [])
    gw = MT5Gateway(cfg, sdk=fake)
    if mode == "live":
        ok, _ = gw.connect()
        assert ok
    state = RunnerState(cfg.state_dir)
    # LX-1 Slice 8: live OPENs are blocked unless ARMED. These tests exercise the
    # armed live path, so they supply the same typed ArmRuntime that startup
    # arming builds (fingerprint matches F.make_account()).
    arm = F.make_arm_runtime() if mode == "live" else None
    ex = Executor(cfg, state, gw, arm_runtime=arm)
    return fake, state, ex


def _detail(state, iid="ex1"):
    return state.data["ledger"][iid]["detail"]


# ── FILLED ────────────────────────────────────────────────────────────────────

def test_filled_confirms_with_actual_filled_volume(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE, order=555, volume=0.02))
    res = ex.apply([_intent()])
    assert res["frozen"] is False
    assert st.ledger_status("ex1") == LEDGER_CONFIRMED and st.mirror_ticket("T1") == 555
    assert _detail(st)["filled_volume"] == 0.02
    assert len(fake.order_send_calls) == 1


def test_filled_uses_broker_volume_not_requested(tmp_path):
    # broker reports a DIFFERENT filled volume than the fixed request (0.01 lot).
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE, order=9, volume=0.005))
    ex.apply([_intent()])
    assert _detail(st)["filled_volume"] == 0.005          # not the requested 0.01


# ── PARTIALLY_FILLED ──────────────────────────────────────────────────────────

def test_partial_records_partial_state_and_freezes(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=556, volume=0.007))
    res = ex.apply([_intent()])
    assert res["frozen"] is True                           # cycle frozen
    assert st.ledger_status("ex1") == LEDGER_PARTIAL       # NOT confirmed/full
    assert st.mirror_ticket("T1") == 556                   # a (partial) position exists
    d = _detail(st)
    assert d["filled_volume"] == 0.007 and d["remaining_volume"] is not None
    assert len(fake.order_send_calls) == 1                 # remainder NOT auto-submitted


def test_partial_then_second_intent_not_processed(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=1, volume=0.007))
    res = ex.apply([_intent("ex1", "T1"), _intent("ex2", "T2")])
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_PARTIAL
    assert st.ledger_status("ex2") is None                 # cycle stopped before it
    assert len(fake.order_send_calls) == 1


# ── REJECTED ──────────────────────────────────────────────────────────────────

def test_rejected_is_failed_no_position(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_NO_MONEY))
    res = ex.apply([_intent()])
    assert res["frozen"] is False
    assert st.ledger_status("ex1") == LEDGER_FAILED and st.mirror_ticket("T1") is None
    assert len(fake.order_send_calls) == 1


# ── AMBIGUOUS / EXCEPTION ─────────────────────────────────────────────────────

def test_ambiguous_leaves_sent_and_freezes(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_TIMEOUT))
    res = ex.apply([_intent()])
    assert res["frozen"] is True
    # left SENT (with its {intent} payload) for _reconcile_sent — never FAILED/CONFIRMED.
    assert st.ledger_status("ex1") == LEDGER_SENT
    assert st.data["ledger"]["ex1"]["detail"].get("intent") is not None
    assert st.mirror_ticket("T1") is None
    assert len(fake.order_send_calls) == 1                 # never resubmitted


def test_exception_leaves_sent_and_freezes_no_raise(tmp_path):
    fake, st, ex = _run(tmp_path, order_exc=RuntimeError("connection dropped"))
    res = ex.apply([_intent()])                            # must not raise
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_SENT          # not safe-to-retry / not FAILED
    assert len(fake.order_send_calls) == 1


def test_unknown_retcode_ambiguous_frozen(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(99999))
    res = ex.apply([_intent()])
    assert res["frozen"] is True and st.ledger_status("ex1") == LEDGER_SENT


# ── dry-run + duplicate rail unchanged ────────────────────────────────────────

def test_dry_run_simulates_no_order_send(tmp_path):
    fake, st, ex = _run(tmp_path, mode="dry_run")
    ex.apply([_intent()])
    assert st.ledger_status("ex1") == LEDGER_SIMULATED and len(fake.order_send_calls) == 0
    assert st.mirror_ticket("T1") == -1                    # simulated ticket


def test_duplicate_rail_blocks_resubmit_after_filled(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE, order=555, volume=0.02))
    ex.apply([_intent()])
    assert len(fake.order_send_calls) == 1
    res2 = ex.apply([_intent()])                           # same intent_id again
    assert res2["blocked"] and res2["blocked"][0]["rail"] == "duplicate_intent"
    assert len(fake.order_send_calls) == 1                 # NOT resubmitted


def test_duplicate_rail_blocks_resubmit_after_partial(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE_PARTIAL, order=1, volume=0.007))
    ex.apply([_intent()])
    res2 = ex.apply([_intent()])                           # LEDGER_PARTIAL must dedup
    assert res2["blocked"] and res2["blocked"][0]["rail"] == "duplicate_intent"
    assert len(fake.order_send_calls) == 1


# ── D-S3-1: insufficient success evidence never records CONFIRMED/PARTIAL ──────

from types import SimpleNamespace                          # noqa: E402
from live.mt5_results import MT5SubmitResult, MT5SubmitDisposition as _D  # noqa: E402


def test_done_missing_ticket_via_gateway_stays_sent(tmp_path):
    # Real gateway + fake sdk: a DONE with NO order ticket is downgraded by the
    # classifier to AMBIGUOUS -> executor leaves SENT + freeze (never CONFIRMED).
    fake, st, ex = _run(tmp_path, order_result=SimpleNamespace(retcode=10009, volume=0.02))  # no order
    res = ex.apply([_intent()])
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_SENT          # not CONFIRMED
    assert st.mirror_ticket("T1") is None                  # no mirror entry
    assert len(fake.order_send_calls) == 1


def test_done_zero_volume_via_gateway_stays_sent(tmp_path):
    fake, st, ex = _run(tmp_path, order_result=SimpleNamespace(retcode=10009, order=5, volume=0.0))
    res = ex.apply([_intent()])
    assert res["frozen"] is True and st.ledger_status("ex1") == LEDGER_SENT
    assert st.mirror_ticket("T1") is None


class _StubGateway:
    """Minimal gateway: clean reconcile snapshot + a scripted open_position result
    (used to force an IMPOSSIBLE classifier result at the executor guard).
    Connected with a fresh, in-spread market sample so the OPEN passes the
    Slice-5 rails and reaches _record_open_result."""
    connected = True

    def __init__(self, result):
        self._result = result

    def snapshot(self):
        return True, {"account": None, "positions": [], "orders": []}

    def market_condition(self):
        from datetime import datetime, timezone
        from live.safety import MarketCondition
        now = datetime.now(timezone.utc)
        return MarketCondition("EURUSD", 1.10101, 1.10123, now, now)

    def account_health(self):
        from live.account_health import AccountHealth
        return AccountHealth("EUR", 10_000.0, 10_000.0, 10_000.0, True, True)

    def account_identity(self):
        # matches F.make_arm_runtime()'s fingerprint (LX-1 Slice 8 continuity check)
        from live.account_identity import AccountIdentity
        return AccountIdentity(login=1_000_001, server="Broker-Demo", currency="EUR",
                               trade_mode="demo", balance=10_000.0, equity=10_000.0)

    def open_position(self, *a, **k):
        return self._result


def test_defensive_guard_filled_without_ticket_downgrades(tmp_path):
    # An IMPOSSIBLE FILLED (no usable ticket) reaching the executor must NOT
    # record CONFIRMED or call mirror_set(None) — the guard downgrades it.
    cfg = _cfg(tmp_path, "live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, arm_runtime=F.make_arm_runtime(), gateway=_StubGateway(
        MT5SubmitResult(disposition=_D.FILLED, broker_order_ticket=None, filled_volume=0.02)))
    res = ex.apply([_intent()])
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_SENT          # NOT confirmed
    assert st.mirror_ticket("T1") is None                  # mirror_set(None) never wrote an entry


def test_defensive_guard_partial_with_zero_ticket_downgrades(tmp_path):
    cfg = _cfg(tmp_path, "live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, arm_runtime=F.make_arm_runtime(), gateway=_StubGateway(
        MT5SubmitResult(disposition=_D.PARTIALLY_FILLED, broker_order_ticket=0, filled_volume=0.007)))
    res = ex.apply([_intent()])
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_SENT          # NOT partial
    assert st.mirror_ticket("T1") is None


def test_valid_filled_with_ticket_still_confirms(tmp_path):
    # Regression: the guard must not affect a real fill.
    fake, st, ex = _run(tmp_path, order_result=F.make_result(F.TRADE_RETCODE_DONE, order=555, volume=0.02))
    ex.apply([_intent()])
    from live.state import LEDGER_CONFIRMED
    assert st.ledger_status("ex1") == LEDGER_CONFIRMED and st.mirror_ticket("T1") == 555


# ── D-S3-3: defensive backstop also requires finite positive filled volume ────

@pytest.mark.parametrize("disp", [_D.FILLED, _D.PARTIALLY_FILLED])
@pytest.mark.parametrize("bad_vol", [None, 0.0, float("nan"), float("inf")])
def test_defensive_guard_success_with_bad_volume_downgrades(tmp_path, disp, bad_vol):
    # valid ticket but insufficient volume -> must NOT record CONFIRMED/PARTIAL
    cfg = _cfg(tmp_path, "live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, arm_runtime=F.make_arm_runtime(), gateway=_StubGateway(
        MT5SubmitResult(disposition=disp, broker_order_ticket=555, filled_volume=bad_vol)))
    res = ex.apply([_intent()])
    assert res["frozen"] is True
    assert st.ledger_status("ex1") == LEDGER_SENT          # not CONFIRMED / not PARTIAL
    assert st.mirror_ticket("T1") is None                  # no mirror mutation


@pytest.mark.parametrize("disp,vol,expect", [
    (_D.FILLED, 0.02, LEDGER_CONFIRMED),
    (_D.PARTIALLY_FILLED, 0.007, LEDGER_PARTIAL),
])
def test_defensive_guard_valid_success_unchanged(tmp_path, disp, vol, expect):
    cfg = _cfg(tmp_path, "live")
    st = RunnerState(cfg.state_dir)
    ex = Executor(cfg, st, arm_runtime=F.make_arm_runtime(), gateway=_StubGateway(
        MT5SubmitResult(disposition=disp, broker_order_ticket=555, filled_volume=vol)))
    ex.apply([_intent()])
    assert st.ledger_status("ex1") == expect and st.mirror_ticket("T1") == 555
