"""LX-1 Slice 1 — MT5 gateway CONTRACT characterization.

Locks down the CURRENT request-construction and result-handling behaviour of
`live/mt5_gateway.py` by driving the real, unmodified gateway with an injected
fake SDK (`_fake_mt5.FakeMT5`). No real MetaTrader5 package, no network, no live
state. These tests CHARACTERIZE present behaviour exactly (including its
un-normalized, SL-only, binary-success quirks) so later LX-1 slices that add
normalization / result classification / rails are provable diffs against a
recorded baseline — they intentionally do NOT assert any improved behaviour.

Deliberately revealing values are used (odd volume, un-rounded prices, distinct
bid/ask) so the assertions expose that the gateway forwards raw values without
digit / volume-step / stop-distance normalization.
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
from live.mt5_gateway import MT5Gateway                   # noqa: E402

from _fake_mt5 import (                                   # noqa: E402
    FakeMT5, make_tick, make_position, make_result,
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, TRADE_ACTION_DEAL, TRADE_ACTION_SLTP,
    ORDER_TIME_GTC, ORDER_FILLING_IOC, TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT,
)

# Deliberately revealing constants (see module docstring).
BID = 1.10101
ASK = 1.10123
VOLUME = 0.017                 # not a clean 0.01 step -> exposes no volume rounding
SL = 1.09567
TP = 1.11234
TICKET = 987654321
MAGIC = 77001                  # LiveConfig default
LONG_INTENT_ID = "intentID_ABCDEFGHIJKLMNOPQRSTUVWXYZ"   # > 26 chars -> exposes truncation


def _cfg(tmp_path) -> LiveConfig:
    c = LiveConfig(lux_root=tmp_path / "lux", state_dir=tmp_path / "state",
                   market_data_dir=tmp_path / "md", kill_file=tmp_path / "state" / "KILL")
    c.ensure_dirs()
    return c


def _connected(tmp_path, fake) -> MT5Gateway:
    """The real gateway, connected against the fake SDK (initialize -> True)."""
    gw = MT5Gateway(_cfg(tmp_path), sdk=fake)
    ok, detail = gw.connect()
    assert ok, detail
    assert fake.initialize_calls == [{}]          # default cfg has no login -> no kwargs
    return gw


# ── A. Open BUY request shape ────────────────────────────────────────────────

def test_open_buy_request_shape(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), order_result=make_result(order=1, price=ASK, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    ok, _ = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert ok
    assert len(fake.order_send_calls) == 1
    assert fake.order_send_calls[0] == {
        "action": TRADE_ACTION_DEAL, "symbol": "EURUSD",
        "volume": VOLUME,                 # raw, un-rounded
        "type": ORDER_TYPE_BUY,
        "price": ASK,                     # BUY -> ask side
        "sl": SL, "tp": TP,               # raw engine stops, un-normalized
        "deviation": 20,                  # hardcoded
        "magic": MAGIC,
        "comment": LONG_INTENT_ID[:26],   # truncated to 26
        "type_time": ORDER_TIME_GTC,
        "type_filling": ORDER_FILLING_IOC,
    }


# ── B. Open SELL request shape ───────────────────────────────────────────────

def test_open_sell_request_shape(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), order_result=make_result(order=2, price=BID, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    ok, _ = gw.open_position("short", VOLUME, SL, TP, "shortIntent")
    assert ok
    req = fake.order_send_calls[0]
    assert req["type"] == ORDER_TYPE_SELL
    assert req["price"] == BID            # SELL -> bid side
    assert req["action"] == TRADE_ACTION_DEAL and req["symbol"] == "EURUSD"
    assert req["volume"] == VOLUME and req["sl"] == SL and req["tp"] == TP
    assert req["deviation"] == 20 and req["magic"] == MAGIC
    assert req["comment"] == "shortIntent"
    assert req["type_time"] == ORDER_TIME_GTC and req["type_filling"] == ORDER_FILLING_IOC


# ── C. Close BUY position request shape ──────────────────────────────────────

def test_close_buy_position_request_shape(tmp_path):
    pos = make_position(TICKET, ORDER_TYPE_BUY, VOLUME)
    fake = FakeMT5(tick=make_tick(BID, ASK), positions=[pos])
    gw = _connected(tmp_path, fake)
    ok, _ = gw.close_position(TICKET)
    assert ok
    assert fake.positions_get_calls[-1] == {"ticket": TICKET, "symbol": None}
    assert fake.order_send_calls[0] == {
        "action": TRADE_ACTION_DEAL, "symbol": "EURUSD",
        "volume": VOLUME,                 # actual broker position volume
        "type": ORDER_TYPE_SELL,          # opposite of BUY
        "position": TICKET,
        "price": BID,                     # closing a BUY sells at bid
        "deviation": 20, "magic": MAGIC,
        "comment": "close",
        "type_filling": ORDER_FILLING_IOC,
        # NOTE: current close request carries NO "type_time" key (characterized).
    }
    assert "type_time" not in fake.order_send_calls[0]


# ── D. Close SELL position request shape ─────────────────────────────────────

def test_close_sell_position_request_shape(tmp_path):
    pos = make_position(TICKET, ORDER_TYPE_SELL, VOLUME)
    fake = FakeMT5(tick=make_tick(BID, ASK), positions=[pos])
    gw = _connected(tmp_path, fake)
    ok, _ = gw.close_position(TICKET)
    assert ok
    req = fake.order_send_calls[0]
    assert req["type"] == ORDER_TYPE_BUY          # opposite of SELL
    assert req["price"] == ASK                    # closing a SELL buys at ask
    assert req["position"] == TICKET and req["volume"] == VOLUME
    assert req["action"] == TRADE_ACTION_DEAL and req["comment"] == "close"


# ── E. Modify stop request shape (SL only) ───────────────────────────────────

def test_modify_stop_request_shape_sl_only(tmp_path):
    fake = FakeMT5()
    gw = _connected(tmp_path, fake)
    ok, res = gw.modify_position_sl(TICKET, SL)
    assert ok and res == {"ticket": TICKET, "sl": SL}
    req = fake.order_send_calls[0]
    # Current behaviour: SL-only SLTP request; no "tp" key is sent.
    assert req == {"action": TRADE_ACTION_SLTP, "symbol": "EURUSD",
                   "position": TICKET, "sl": SL}
    assert "tp" not in req


# ── F. Success result mapping ────────────────────────────────────────────────

def test_open_success_result_mapping(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK),
                   order_result=make_result(TRADE_RETCODE_DONE, order=555, price=ASK, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    ok, data = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert ok is True
    assert data == {"ticket": 555, "price": ASK, "volume": VOLUME}


# ── G. Non-success retcode -> collapsed error string ─────────────────────────

def test_non_success_retcode_collapsed_to_error_string(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK),
                   order_result=make_result(TRADE_RETCODE_REJECT), last_error=(10006, "reject"))
    gw = _connected(tmp_path, fake)
    ok, err = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert ok is False
    # Characterize: the retcode is COLLAPSED into a free-text string, not a
    # structured category (this is exactly what LX-1 slice 3 will replace).
    assert isinstance(err, str) and str(TRADE_RETCODE_REJECT) in err


# ── H. None result mapping ───────────────────────────────────────────────────

def test_none_result_mapping(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), order_result=None, last_error=(1, "no result"))
    gw = _connected(tmp_path, fake)
    ok, err = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert ok is False
    assert isinstance(err, str) and "none" in err       # getattr(result,'retcode','none')


# ── I. order_send raising an exception -> propagates (characterized) ─────────

def test_order_send_exception_propagates(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), order_exc=RuntimeError("connection dropped mid-order_send"))
    gw = _connected(tmp_path, fake)
    # Current behaviour: the gateway does NOT wrap order_send; the exception
    # propagates to the caller (the executor relies on this for LR-1 adoption).
    with pytest.raises(RuntimeError, match="connection dropped"):
        gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)


# ── J. Close with missing position -> no order_send ──────────────────────────

def test_close_missing_position_no_order_send(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), positions=[])   # ticket not present
    gw = _connected(tmp_path, fake)
    ok, err = gw.close_position(TICKET)
    assert ok is False
    assert isinstance(err, str) and str(TICKET) in err
    assert fake.order_send_calls == []                  # no submission attempted


# ── K. Gateway constructed without an SDK is safe ────────────────────────────

def test_gateway_without_sdk_is_safe(tmp_path):
    gw = MT5Gateway(_cfg(tmp_path), sdk=None)
    assert gw.available is False
    ok, detail = gw.connect()
    assert ok is False and "not available" in detail
    # order ops refuse cleanly (not connected), never raising.
    assert gw.open_position("long", VOLUME, SL, TP, "x") == (False, "not connected")
    assert gw.close_position(TICKET) == (False, "not connected")
    assert gw.modify_position_sl(TICKET, SL) == (False, "not connected")
