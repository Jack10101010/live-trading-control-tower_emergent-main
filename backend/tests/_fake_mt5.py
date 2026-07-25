"""Deterministic in-process fake of the MetaTrader5 SDK surface used by
`live/mt5_gateway.py`.

Test-only helper: it requires no real MetaTrader5 package, performs no network
or filesystem access, and is injected via ``MT5Gateway(config, sdk=FakeMT5(...))``
so the *real* request-construction code in the gateway runs unchanged. Every
``order_send`` request is deep-copied when recorded, so a later mutation of the
request dict by production code (or a test) cannot alter the captured evidence.

It implements only the surface the current gateway touches — it is not a general
MT5 simulator. Constants are arbitrary but distinct sentinels; tests assert the
gateway forwards *these* values, not any real broker numbers.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace

# ── constants referenced by live/mt5_gateway.py (distinct sentinels) ──────────
TRADE_ACTION_DEAL = "TRADE_ACTION_DEAL"
TRADE_ACTION_SLTP = "TRADE_ACTION_SLTP"
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TIME_GTC = "ORDER_TIME_GTC"
ORDER_FILLING_IOC = "ORDER_FILLING_IOC"
ORDER_FILLING_FOK = "ORDER_FILLING_FOK"
ORDER_FILLING_RETURN = "ORDER_FILLING_RETURN"
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_REJECT = 10006
# reject family (deterministic, no position created)
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_INVALID_FILL = 10030
# ambiguous family (execution not safely knowable)
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_CONNECTION = 10031
TIMEFRAME_M1 = 1

# Symbol filling-capability bitmask flags. FOK/IOC mirror the real MetaTrader5
# constants; SYMBOL_FILLING_RETURN does NOT exist in the real package (the
# gateway getattr-defaults it to 0) — the fake advertises it so RETURN-first
# selection is unit-testable.
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2
SYMBOL_FILLING_RETURN = 4

_UNSET = object()


def make_symbol_info(*, name: str = "EURUSD", digits: int = 5, point: float = 0.00001,
                     volume_min: float = 0.01, volume_max: float = 100.0,
                     volume_step: float = 0.01, trade_stops_level: int = 0,
                     trade_freeze_level: int = 0,
                     filling_mode: int = SYMBOL_FILLING_IOC) -> SimpleNamespace:
    """A fake symbol_info (the fields broker_constraints reads via getattr)."""
    return SimpleNamespace(name=name, digits=digits, point=point,
                           volume_min=volume_min, volume_max=volume_max,
                           volume_step=volume_step, trade_stops_level=trade_stops_level,
                           trade_freeze_level=trade_freeze_level, filling_mode=filling_mode)


def make_tick(bid: float, ask: float, time: int = 1_700_000_000) -> SimpleNamespace:
    """A fake symbol tick (only .bid/.ask/.time are read by the gateway)."""
    return SimpleNamespace(bid=bid, ask=ask, time=time)


def fresh_tick(bid: float, ask: float, *, age_s: float = 1.0) -> SimpleNamespace:
    """A tick stamped ``age_s`` seconds before now — passes the LX-1 Slice 5
    feed-freshness rail (unlike make_tick's fixed 2023 default)."""
    import time as _t
    return SimpleNamespace(bid=bid, ask=ask, time=int(_t.time() - age_s))


def make_position(ticket: int, type: int, volume: float, *, symbol: str = "EURUSD",
                  price_open: float = 1.10000, sl: float = 0.0, tp: float = 0.0,
                  comment: str = "", magic: int = 77001,
                  profit: float = 0.0) -> SimpleNamespace:
    """A fake broker position (fields the gateway/executor read)."""
    return SimpleNamespace(ticket=ticket, type=type, volume=volume, symbol=symbol,
                           price_open=price_open, sl=sl, tp=tp, comment=comment,
                           magic=magic, profit=profit)


def make_result(retcode: int = TRADE_RETCODE_DONE, *, order: int = 0, deal: int = 0,
                price: float = 0.0, volume: float = 0.0, bid: float = 0.0, ask: float = 0.0,
                comment: str = "", request_id: int = 0,
                retcode_external: int = 0) -> SimpleNamespace:
    """A fake order_send result exposing the MT5 result fields the classifier
    snapshots (retcode/order/deal/volume/price/bid/ask/comment/request_id/
    retcode_external)."""
    return SimpleNamespace(retcode=retcode, order=order, deal=deal, price=price,
                           volume=volume, bid=bid, ask=ask, comment=comment,
                           request_id=request_id, retcode_external=retcode_external)


def make_account(login: int = 1_000_001, server: str = "Broker-Demo",
                 balance: float = 10_000.0, equity: float = 10_000.0,
                 currency: str = "EUR", trade_allowed: bool = True,
                 trade_mode: int = 0, margin_free: float = 10_000.0,
                 trade_expert: bool = True) -> SimpleNamespace:
    # trade_mode mirrors MT5 ACCOUNT_TRADE_MODE_* (0=demo, 1=contest, 2=real);
    # margin_free/trade_expert mirror the real account_info fields (LX-1 Slice 7).
    return SimpleNamespace(login=login, server=server, balance=balance,
                           equity=equity, currency=currency, trade_allowed=trade_allowed,
                           trade_mode=trade_mode, margin_free=margin_free,
                           trade_expert=trade_expert)


class FakeMT5:
    """Scriptable stand-in for the MetaTrader5 module. Constants live as class
    attributes so ``gateway.sdk.ORDER_TYPE_BUY`` resolves exactly as in production."""

    # constants (as attributes — the gateway reads them off the injected sdk)
    # account trade-mode enum (matches the real MetaTrader5 package values)
    ACCOUNT_TRADE_MODE_DEMO = 0
    ACCOUNT_TRADE_MODE_CONTEST = 1
    ACCOUNT_TRADE_MODE_REAL = 2
    TRADE_ACTION_DEAL = TRADE_ACTION_DEAL
    TRADE_ACTION_SLTP = TRADE_ACTION_SLTP
    ORDER_TYPE_BUY = ORDER_TYPE_BUY
    ORDER_TYPE_SELL = ORDER_TYPE_SELL
    ORDER_TIME_GTC = ORDER_TIME_GTC
    ORDER_FILLING_IOC = ORDER_FILLING_IOC
    ORDER_FILLING_FOK = ORDER_FILLING_FOK
    ORDER_FILLING_RETURN = ORDER_FILLING_RETURN
    TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
    TRADE_RETCODE_DONE_PARTIAL = TRADE_RETCODE_DONE_PARTIAL
    TRADE_RETCODE_REJECT = TRADE_RETCODE_REJECT
    TRADE_RETCODE_REQUOTE = TRADE_RETCODE_REQUOTE
    TRADE_RETCODE_INVALID = TRADE_RETCODE_INVALID
    TRADE_RETCODE_INVALID_VOLUME = TRADE_RETCODE_INVALID_VOLUME
    TRADE_RETCODE_INVALID_PRICE = TRADE_RETCODE_INVALID_PRICE
    TRADE_RETCODE_INVALID_STOPS = TRADE_RETCODE_INVALID_STOPS
    TRADE_RETCODE_TRADE_DISABLED = TRADE_RETCODE_TRADE_DISABLED
    TRADE_RETCODE_MARKET_CLOSED = TRADE_RETCODE_MARKET_CLOSED
    TRADE_RETCODE_NO_MONEY = TRADE_RETCODE_NO_MONEY
    TRADE_RETCODE_INVALID_FILL = TRADE_RETCODE_INVALID_FILL
    TRADE_RETCODE_PLACED = TRADE_RETCODE_PLACED
    TRADE_RETCODE_TIMEOUT = TRADE_RETCODE_TIMEOUT
    TRADE_RETCODE_CONNECTION = TRADE_RETCODE_CONNECTION
    TIMEFRAME_M1 = TIMEFRAME_M1
    SYMBOL_FILLING_FOK = SYMBOL_FILLING_FOK
    SYMBOL_FILLING_IOC = SYMBOL_FILLING_IOC
    SYMBOL_FILLING_RETURN = SYMBOL_FILLING_RETURN

    def __init__(self, *, tick=None, positions=None, account=None, terminal=None,
                 symbol_info=None, order_result=_UNSET, order_exc=None, rates=None,
                 initialize_ok: bool = True, last_error=(0, "ok")):
        self._tick = tick
        self._positions = list(positions or [])
        self._account = account
        self._terminal = terminal
        self._symbol_info = symbol_info
        self._order_result = make_result() if order_result is _UNSET else order_result
        self._order_exc = order_exc
        self._rates = rates
        self._initialize_ok = initialize_ok
        self._last_error = last_error
        # ── call recorders (explicit, inspectable) ──
        self.initialize_calls: list[dict] = []
        self.shutdown_calls: int = 0
        self.symbol_select_calls: list = []
        self.symbol_info_tick_calls: list = []
        self.positions_get_calls: list[dict] = []
        self.orders_get_calls: list[dict] = []
        self.account_info_calls: int = 0
        self.copy_rates_range_calls: list = []
        self.order_send_calls: list[dict] = []

    # ── lifecycle ────────────────────────────────────────────────────────────
    def initialize(self, **kwargs) -> bool:
        self.initialize_calls.append(dict(kwargs))
        return self._initialize_ok

    def shutdown(self) -> None:
        self.shutdown_calls += 1

    def last_error(self):
        return self._last_error

    def terminal_info(self):
        return self._terminal

    # ── account / symbol ──────────────────────────────────────────────────────
    def account_info(self):
        self.account_info_calls += 1
        return self._account

    def symbol_info(self, symbol):
        return self._symbol_info

    def symbol_select(self, symbol, enable: bool = True) -> bool:
        self.symbol_select_calls.append((symbol, enable))
        return True

    def symbol_info_tick(self, symbol):
        self.symbol_info_tick_calls.append(symbol)
        return self._tick

    # ── positions / orders ─────────────────────────────────────────────────────
    def positions_get(self, ticket=None, symbol=None):
        self.positions_get_calls.append({"ticket": ticket, "symbol": symbol})
        pos = self._positions
        if ticket is not None:
            pos = [p for p in pos if p.ticket == ticket]
        elif symbol is not None:
            pos = [p for p in pos if p.symbol == symbol]
        return list(pos)

    def orders_get(self, symbol=None):
        self.orders_get_calls.append({"symbol": symbol})
        return []

    # ── market data ────────────────────────────────────────────────────────────
    def copy_rates_range(self, symbol, timeframe, date_from, date_to):
        self.copy_rates_range_calls.append((symbol, timeframe, date_from, date_to))
        return self._rates

    # ── order submission ───────────────────────────────────────────────────────
    def order_send(self, request):
        # Deep-copy so later mutation cannot rewrite the recorded evidence.
        self.order_send_calls.append(copy.deepcopy(request))
        if self._order_exc is not None:
            raise self._order_exc
        return self._order_result
