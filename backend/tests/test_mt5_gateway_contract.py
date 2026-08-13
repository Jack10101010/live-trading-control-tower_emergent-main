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
from live.mt5_results import MT5SubmitDisposition as Disp # noqa: E402

from _fake_mt5 import (                                   # noqa: E402
    FakeMT5, make_tick, make_position, make_result, make_symbol_info,
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, TRADE_ACTION_DEAL, TRADE_ACTION_SLTP,
    ORDER_TIME_GTC, ORDER_FILLING_IOC, ORDER_FILLING_FOK, ORDER_FILLING_RETURN,
    SYMBOL_FILLING_IOC, SYMBOL_FILLING_FOK, SYMBOL_FILLING_RETURN,
    TRADE_RETCODE_DONE, TRADE_RETCODE_REJECT,
)

# A symbol whose constraints leave the deliberately-revealing Slice-1 values
# already broker-valid, so normalization is a verified pass-through (the Slice-1
# request dicts are asserted unchanged): fine 0.001 step keeps 0.017, 5 digits
# keep the prices, zero stops-level accepts the stops, IOC-only filling -> IOC.
def _si_passthrough():
    return make_symbol_info(volume_step=0.001, filling_mode=SYMBOL_FILLING_IOC)

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
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_result=make_result(order=1, price=ASK, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert res.disposition is Disp.FILLED
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
    # For a SHORT the engine stops are inverted vs a long: SL ABOVE price, TP
    # BELOW price. (Slice-1 used long-shaped stops here; normalization now
    # enforces stop side, so short-valid stops are supplied.)
    sl_short, tp_short = 1.10567, 1.09234
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_result=make_result(order=2, price=BID, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("short", VOLUME, sl_short, tp_short, "shortIntent")
    assert res.disposition is Disp.FILLED
    req = fake.order_send_calls[0]
    assert req["type"] == ORDER_TYPE_SELL
    assert req["price"] == BID            # SELL -> bid side
    assert req["action"] == TRADE_ACTION_DEAL and req["symbol"] == "EURUSD"
    assert req["volume"] == VOLUME and req["sl"] == sl_short and req["tp"] == tp_short
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


# ── F. Success result mapping (typed FILLED) ─────────────────────────────────

def test_open_success_maps_to_typed_filled(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_result=make_result(TRADE_RETCODE_DONE, order=555, deal=777,
                                            price=ASK, volume=VOLUME))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert res.disposition is Disp.FILLED and res.freeze is False
    assert res.broker_order_ticket == 555 and res.broker_deal_ticket == 777
    assert res.price == ASK and res.filled_volume == VOLUME and res.remaining_volume == 0.0


# ── G. Non-success reject -> typed REJECTED (Slice 3, was a collapsed string) ─

def test_non_success_retcode_maps_to_typed_rejected(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_result=make_result(TRADE_RETCODE_REJECT), last_error=(10006, "reject"))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert res.disposition is Disp.REJECTED and res.freeze is False
    assert res.retcode == TRADE_RETCODE_REJECT


# ── H. None result -> typed AMBIGUOUS + freeze (Slice 3) ─────────────────────

def test_none_result_maps_to_typed_ambiguous(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_result=None, last_error=(1, "no result"))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert res.disposition is Disp.AMBIGUOUS and res.freeze is True
    assert len(fake.order_send_calls) == 1


# ── I. order_send exception -> typed EXCEPTION + freeze (Slice 3) ────────────

def test_order_send_exception_maps_to_typed_exception(tmp_path):
    fake = FakeMT5(tick=make_tick(BID, ASK), symbol_info=_si_passthrough(),
                   order_exc=RuntimeError("connection dropped mid-order_send"))
    gw = _connected(tmp_path, fake)
    # Slice 3: order_send exceptions are CAUGHT and typed (never propagate),
    # reconciling the gateway docstring; the outcome is unknown -> freeze.
    res = gw.open_position("long", VOLUME, SL, TP, LONG_INTENT_ID)
    assert res.disposition is Disp.EXCEPTION and res.freeze is True
    assert "RuntimeError" in res.diagnostic and "connection dropped" in res.diagnostic
    assert len(fake.order_send_calls) == 1


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
    open_res = gw.open_position("long", VOLUME, SL, TP, "x")
    assert open_res.disposition is Disp.NOT_SUBMITTED and "not_connected" in open_res.diagnostic
    assert gw.close_position(TICKET) == (False, "not connected")
    assert gw.modify_position_sl(TICKET, SL) == (False, "not connected")


# ── LX-1 Slice 2 — broker-constraint normalization (through the real gateway) ──
# These drive the real gateway open path with an injected fake SDK; each proves
# either a normalized request field or a rejection with NO order_send call.

from live import broker_constraints as bc                 # noqa: E402
from _fake_mt5 import make_symbol_info as _si             # noqa: E402

_OK_TICK = (1.10101, 1.10123)   # (bid, ask); ask side for a long open


def _open(tmp_path, symbol_info, *, volume=0.02, sl=1.09000, tp=1.11000, side="long"):
    fake = FakeMT5(tick=make_tick(*_OK_TICK), symbol_info=symbol_info,
                   order_result=make_result(TRADE_RETCODE_DONE, order=1, price=1.10123, volume=volume))
    gw = _connected(tmp_path, fake)
    return fake, gw.open_position(side, volume, sl, tp, "normIntent")


# -- Volume --

def test_volume_exact_step_unchanged(tmp_path):
    fake, res = _open(tmp_path, _si(volume_step=0.01), volume=0.02)
    assert res.disposition is Disp.FILLED and fake.order_send_calls[0]["volume"] == 0.02


def test_volume_fractional_step_quantized_down(tmp_path):
    fake, res = _open(tmp_path, _si(volume_step=0.01), volume=0.017)
    assert res.disposition is Disp.FILLED and fake.order_send_calls[0]["volume"] == 0.01     # floored, never up


def test_volume_below_minimum_rejected_no_send(tmp_path):
    fake, res = _open(tmp_path, _si(volume_min=0.01, volume_step=0.01), volume=0.009)
    assert res.disposition is Disp.NOT_SUBMITTED and "volume_below_min" in res.diagnostic
    assert fake.order_send_calls == []


def test_volume_above_maximum_rejected_no_send(tmp_path):
    fake, res = _open(tmp_path, _si(volume_max=1.0, volume_step=0.01), volume=2.0)
    assert res.disposition is Disp.NOT_SUBMITTED and "volume_above_max" in res.diagnostic
    assert fake.order_send_calls == []


def test_volume_zero_rejected_no_send(tmp_path):
    fake, res = _open(tmp_path, _si(), volume=0.0)
    assert res.disposition is Disp.NOT_SUBMITTED and "invalid_volume" in res.diagnostic
    assert fake.order_send_calls == []


def test_volume_nan_rejected_no_send(tmp_path):
    fake, res = _open(tmp_path, _si(), volume=float("nan"))
    assert res.disposition is Disp.NOT_SUBMITTED and "non_finite" in res.diagnostic
    assert fake.order_send_calls == []


# -- Stops --

def test_stop_wrong_side_rejected_no_send(tmp_path):
    # long with SL ABOVE price -> wrong side.
    fake, res = _open(tmp_path, _si(), sl=1.10500, tp=1.11000, side="long")
    assert res.disposition is Disp.NOT_SUBMITTED and "stop_wrong_side" in res.diagnostic
    assert fake.order_send_calls == []


def test_stop_too_close_rejected_no_send(tmp_path):
    # stops_level 100 * point 0.00001 = 0.001 min distance; SL 0.0001 away.
    si = _si(trade_stops_level=100)
    fake, res = _open(tmp_path, si, sl=1.10113, tp=1.11000, side="long")  # ask 1.10123
    assert res.disposition is Disp.NOT_SUBMITTED and "stop_too_close" in res.diagnostic
    assert fake.order_send_calls == []


def test_stops_valid_pass_through(tmp_path):
    fake, res = _open(tmp_path, _si(), sl=1.09000, tp=1.11000, side="long")
    assert res.disposition is Disp.FILLED
    req = fake.order_send_calls[0]
    assert req["sl"] == 1.09000 and req["tp"] == 1.11000


# -- Digit rounding --

def test_price_and_stops_rounded_to_digits(tmp_path):
    # digits=3: ask 1.234567 -> 1.235, sl 1.230444 -> 1.230, tp 1.240555 -> 1.241
    fake = FakeMT5(tick=make_tick(1.230123, 1.234567), symbol_info=_si(digits=3, point=0.001),
                   order_result=make_result(TRADE_RETCODE_DONE, order=1, price=1.235, volume=0.02))
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", 0.02, 1.230444, 1.240555, "roundIntent")
    assert res.disposition is Disp.FILLED
    req = fake.order_send_calls[0]
    assert req["price"] == 1.235 and req["sl"] == 1.230 and req["tp"] == 1.241


# -- Tick --

def test_missing_tick_rejected_no_send(tmp_path):
    fake = FakeMT5(tick=None, symbol_info=_si())
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", 0.02, 1.09, 1.11, "x")
    assert res.disposition is Disp.NOT_SUBMITTED and "missing_tick" in res.diagnostic
    assert fake.order_send_calls == []


def test_stale_tick_rejected_by_module():
    # Stale-tick detection lives in the module (gateway clock wiring is a later
    # slice): an old tick.time vs now_epoch beyond max_tick_age_s -> STALE_TICK.
    si = _si()
    tick = make_tick(1.10101, 1.10123, time=1_000_000)
    r = bc.normalize_open(si, tick, "long", 0.02, 1.09, 1.11,
                          filling_preference=((None, 0), (None, 0), (None, 0)),
                          max_tick_age_s=10, now_epoch=1_000_100)
    assert r.ok is False and r.reason is bc.NormReason.STALE_TICK and r.freeze is True


# -- Filling mode selection --

def _filling_req(tmp_path, filling_mode):
    fake, res = _open(tmp_path, _si(filling_mode=filling_mode))
    assert res.disposition is Disp.FILLED
    return fake.order_send_calls[0]["type_filling"]


def test_filling_ioc_selected(tmp_path):
    assert _filling_req(tmp_path, SYMBOL_FILLING_IOC) == ORDER_FILLING_IOC


def test_filling_fok_selected(tmp_path):
    assert _filling_req(tmp_path, SYMBOL_FILLING_FOK) == ORDER_FILLING_FOK


def test_filling_return_preferred_first(tmp_path):
    # symbol supports RETURN+IOC+FOK -> RETURN wins (preference order).
    mode = SYMBOL_FILLING_RETURN | SYMBOL_FILLING_IOC | SYMBOL_FILLING_FOK
    assert _filling_req(tmp_path, mode) == ORDER_FILLING_RETURN


def test_filling_unsupported_rejected_no_send(tmp_path):
    fake, res = _open(tmp_path, _si(filling_mode=0))
    assert res.disposition is Disp.NOT_SUBMITTED and "unsupported_filling" in res.diagnostic
    assert fake.order_send_calls == []


# -- Missing symbol metadata --

def test_missing_symbol_info_rejected_no_send(tmp_path):
    fake = FakeMT5(tick=make_tick(*_OK_TICK), symbol_info=None)
    gw = _connected(tmp_path, fake)
    res = gw.open_position("long", 0.02, 1.09, 1.11, "x")
    assert res.disposition is Disp.NOT_SUBMITTED and "missing_symbol_meta" in res.diagnostic
    assert fake.order_send_calls == []


# ── LX-1 Slice 2 — audit fail-closed corrections (D-S2-1..4) ──────────────────
# Each proves a malformed input is rejected with NO order_send, plus the D-S2-4
# exact-minimum-distance boundary now accepts (was float-rejected pre-fix).

def _open_tick(tmp_path, bid, ask, *, sl=0.0, tp=0.0, side="long"):
    fake = FakeMT5(tick=make_tick(bid, ask), symbol_info=_si(),
                   order_result=make_result(TRADE_RETCODE_DONE, order=1, price=ask, volume=0.02))
    gw = _connected(tmp_path, fake)
    return fake, gw.open_position(side, 0.02, sl, tp, "x")


# -- D-S2-1: invalid volume_step fails closed --

@pytest.mark.parametrize("bad_step", [0.0, -0.01, float("nan")])
def test_invalid_volume_step_rejected_no_send(tmp_path, bad_step):
    fake, res = _open(tmp_path, _si(volume_step=bad_step), volume=0.02)
    assert res.disposition is Disp.NOT_SUBMITTED and "missing_symbol_meta" in res.diagnostic
    assert fake.order_send_calls == []


# -- D-S2-2: unknown side fails closed (no aliasing, no price selection first) --

@pytest.mark.parametrize("bad_side", ["buy", "sell", "LONG", "", None])
def test_unknown_side_rejected_no_send(tmp_path, bad_side):
    fake, res = _open(tmp_path, _si(), side=bad_side)
    assert res.disposition is Disp.NOT_SUBMITTED and "invalid_side" in res.diagnostic
    assert fake.order_send_calls == []


def test_canonical_sides_still_accepted(tmp_path):
    for side, sl, tp in (("long", 1.09000, 1.11000), ("short", 1.10500, 1.09500)):
        fake, res = _open(tmp_path, _si(), side=side, sl=sl, tp=tp)
        assert res.disposition is Disp.FILLED and len(fake.order_send_calls) == 1


# -- D-S2-3: non-positive / non-finite tick prices fail closed --

@pytest.mark.parametrize("bid,ask", [
    (0.0, 1.10123), (1.10101, 0.0), (-1.0, 1.10123), (1.10101, -1.0),
    (float("nan"), 1.10123), (1.10101, float("nan")),
    (float("inf"), 1.10123), (1.10101, float("inf")),
])
def test_non_positive_or_nonfinite_tick_rejected_no_send(tmp_path, bid, ask):
    fake, res = _open_tick(tmp_path, bid, ask)
    assert res.disposition is Disp.NOT_SUBMITTED and "missing_tick" in res.diagnostic
    assert fake.order_send_calls == []


# -- D-S2-4: Decimal stop-distance — exact minimum accepted, one quantum inside rejected --
# stops_level 10 * point 0.00001 = 0.0001 min distance. Long price = ask 1.10123.
# Short price = bid 1.10101.

def test_long_sl_exactly_at_min_distance_accepted(tmp_path):
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.10113, tp=1.11000, side="long")
    assert res.disposition is Disp.FILLED and len(fake.order_send_calls) == 1     # 1.10123-1.10113 == 0.0001 exactly


def test_long_tp_exactly_at_min_distance_accepted(tmp_path):
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.09000, tp=1.10133, side="long")
    assert res.disposition is Disp.FILLED and len(fake.order_send_calls) == 1     # 1.10133-1.10123 == 0.0001 exactly


def test_long_sl_one_quantum_inside_rejected(tmp_path):
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.10114, tp=1.11000, side="long")
    assert res.disposition is Disp.NOT_SUBMITTED and "stop_too_close" in res.diagnostic
    assert fake.order_send_calls == []


def test_long_tp_one_quantum_inside_rejected(tmp_path):
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.09000, tp=1.10132, side="long")
    assert res.disposition is Disp.NOT_SUBMITTED and "stop_too_close" in res.diagnostic
    assert fake.order_send_calls == []


def test_short_sl_exactly_at_min_distance_accepted(tmp_path):
    # short: SL above bid, TP below bid. SL exactly 0.0001 above 1.10101 = 1.10111.
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.10111, tp=1.09000, side="short")
    assert res.disposition is Disp.FILLED and len(fake.order_send_calls) == 1


def test_short_sl_one_quantum_inside_rejected(tmp_path):
    fake, res = _open(tmp_path, _si(trade_stops_level=10), sl=1.10110, tp=1.09000, side="short")
    assert res.disposition is Disp.NOT_SUBMITTED and "stop_too_close" in res.diagnostic
    assert fake.order_send_calls == []
