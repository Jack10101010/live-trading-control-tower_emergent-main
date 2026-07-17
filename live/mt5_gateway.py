"""MT5 gateway — the ONLY module (besides backend/broker.py's adapter, which
delegates here) allowed to touch the MetaTrader5 package. Import-guarded so the
whole slice imports and tests cleanly off-VPS; every call answers with a
structured (ok, data|error) pair and never raises broker SDK exceptions upward.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

try:  # Windows VPS only; absent everywhere else by design
    import MetaTrader5 as _mt5  # type: ignore
except ImportError:  # pragma: no cover - exercised via injection in tests
    _mt5 = None


class MT5Gateway:
    """Thin, stateless-ish wrapper. `sdk` is injectable for tests/fakes."""

    def __init__(self, config, sdk: Any = None):
        self.config = config
        self.sdk = sdk if sdk is not None else _mt5
        self._connected = False

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def available(self) -> bool:
        return self.sdk is not None

    def connect(self) -> tuple[bool, str]:
        if not self.available:
            return False, "MetaTrader5 package not available on this host"
        kwargs = {}
        if self.config.mt5_login:
            kwargs = {"login": int(self.config.mt5_login),
                      "password": self.config.mt5_password,
                      "server": self.config.mt5_server}
        if not self.sdk.initialize(**kwargs):
            return False, f"mt5.initialize failed: {self.sdk.last_error()}"
        self._connected = True
        return True, "connected"

    def disconnect(self) -> None:
        if self.available and self._connected:
            self.sdk.shutdown()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── market data (closed bars only) ───────────────────────────────────────
    def server_time_utc(self) -> datetime | None:
        if not self._connected:
            return None
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return None
        return datetime.fromtimestamp(tick.time, tz=timezone.utc)

    def closed_m1_bars(self, since_utc: datetime, limit: int = 5000) -> tuple[bool, list[dict] | str]:
        """Closed M1 bars with open time > since_utc. The currently forming
        minute is excluded by requiring bar_open + 60s <= server time."""
        if not self._connected:
            return False, "not connected"
        now = self.server_time_utc()
        if now is None:
            return False, "no server time (symbol tick unavailable)"
        rates = self.sdk.copy_rates_range(
            self.config.broker_symbol, self.sdk.TIMEFRAME_M1,
            since_utc, now)
        if rates is None:
            return False, f"copy_rates_range failed: {self.sdk.last_error()}"
        out = []
        cutoff = now.timestamp() - 60
        for r in list(rates)[:limit]:
            t = float(r["time"]) if isinstance(r, dict) else float(r[0])
            if t <= since_utc.timestamp() or t > cutoff:
                continue  # already stored / still forming
            rec = (r if isinstance(r, dict)
                   else {"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                         "close": r[4], "tick_volume": r[5]})
            out.append({
                "time": datetime.fromtimestamp(float(rec["time"]), tz=timezone.utc)
                                .strftime("%Y-%m-%d %H:%M:%S+00:00"),
                "open": float(rec["open"]), "high": float(rec["high"]),
                "low": float(rec["low"]), "close": float(rec["close"]),
                "volume": float(rec.get("tick_volume", 0.0)),
            })
        return True, out

    # ── account state ────────────────────────────────────────────────────────
    def snapshot(self) -> tuple[bool, dict | str]:
        if not self._connected:
            return False, "not connected"
        acct = self.sdk.account_info()
        positions = self.sdk.positions_get(symbol=self.config.broker_symbol) or []
        orders = self.sdk.orders_get(symbol=self.config.broker_symbol) or []
        def _pos(p):
            return {"ticket": p.ticket, "symbol": p.symbol, "volume": p.volume,
                    "type": p.type, "price_open": p.price_open, "sl": p.sl, "tp": p.tp,
                    "profit": p.profit, "comment": getattr(p, "comment", ""),
                    "magic": getattr(p, "magic", 0)}
        def _ord(o):
            return {"ticket": o.ticket, "symbol": o.symbol, "type": o.type,
                    "volume": getattr(o, "volume_current", 0.0),
                    "price_open": getattr(o, "price_open", 0.0),
                    "sl": o.sl, "tp": o.tp, "comment": getattr(o, "comment", ""),
                    "magic": getattr(o, "magic", 0)}
        return True, {
            "account": None if acct is None else {"login": acct.login, "balance": acct.balance,
                                                  "equity": acct.equity, "currency": acct.currency},
            "positions": [_pos(p) for p in positions],
            "orders": [_ord(o) for o in orders],
        }

    # ── order operations (market mirror model: engine is the state machine) ──
    def open_position(self, side: str, lots: float, sl: float, tp: float,
                      intent_id: str) -> tuple[bool, dict | str]:
        if not self._connected:
            return False, "not connected"
        order_type = self.sdk.ORDER_TYPE_BUY if side == "long" else self.sdk.ORDER_TYPE_SELL
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return False, "no tick"
        price = tick.ask if side == "long" else tick.bid
        request = {
            "action": self.sdk.TRADE_ACTION_DEAL, "symbol": self.config.broker_symbol,
            "volume": float(lots), "type": order_type, "price": float(price),
            "sl": float(sl), "tp": float(tp), "deviation": 20,
            "magic": self.config.magic_number, "comment": intent_id[:26],
            "type_time": self.sdk.ORDER_TIME_GTC,
            "type_filling": self.sdk.ORDER_FILLING_IOC,
        }
        result = self.sdk.order_send(request)
        if result is None or result.retcode != self.sdk.TRADE_RETCODE_DONE:
            return False, f"order_send failed: {getattr(result, 'retcode', 'none')} {self.sdk.last_error()}"
        return True, {"ticket": result.order, "price": result.price, "volume": result.volume}

    def modify_position_sl(self, ticket: int, sl: float) -> tuple[bool, dict | str]:
        if not self._connected:
            return False, "not connected"
        request = {"action": self.sdk.TRADE_ACTION_SLTP, "symbol": self.config.broker_symbol,
                   "position": int(ticket), "sl": float(sl)}
        result = self.sdk.order_send(request)
        if result is None or result.retcode != self.sdk.TRADE_RETCODE_DONE:
            return False, f"modify failed: {getattr(result, 'retcode', 'none')}"
        return True, {"ticket": ticket, "sl": sl}

    def close_position(self, ticket: int) -> tuple[bool, dict | str]:
        if not self._connected:
            return False, "not connected"
        positions = self.sdk.positions_get(ticket=int(ticket)) or []
        if not positions:
            return False, f"position {ticket} not found"
        p = positions[0]
        side_close = self.sdk.ORDER_TYPE_SELL if p.type == self.sdk.ORDER_TYPE_BUY else self.sdk.ORDER_TYPE_BUY
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        price = tick.bid if side_close == self.sdk.ORDER_TYPE_SELL else tick.ask
        request = {"action": self.sdk.TRADE_ACTION_DEAL, "symbol": self.config.broker_symbol,
                   "volume": p.volume, "type": side_close, "position": int(ticket),
                   "price": float(price), "deviation": 20, "magic": self.config.magic_number,
                   "comment": "close", "type_filling": self.sdk.ORDER_FILLING_IOC}
        result = self.sdk.order_send(request)
        if result is None or result.retcode != self.sdk.TRADE_RETCODE_DONE:
            return False, f"close failed: {getattr(result, 'retcode', 'none')}"
        return True, {"ticket": ticket, "closed": True}
