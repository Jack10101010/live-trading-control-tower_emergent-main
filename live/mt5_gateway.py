"""MT5 gateway — the ONLY module (besides backend/broker.py's adapter, which
delegates here) allowed to touch the MetaTrader5 package. Import-guarded so the
whole slice imports and tests cleanly off-VPS; every call answers with a
structured (ok, data|error) pair and never raises broker SDK exceptions upward.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

from live import mt5_results
from live.broker_constraints import normalize_open
from live.config import SYMBOL
from live.safety import MarketCondition

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

    def market_condition(self):
        """Read-only current market condition (LX-1 Slice 5) as an immutable
        ``MarketCondition``, or ``None`` if unavailable/malformed. Never mutates,
        never submits, retains no SDK object. Rejects missing data, non-finite /
        non-positive / bool prices, ask < bid, and malformed tick timestamps.
        ``server_time_utc`` is the process/local (time-synced VPS) reference
        clock — NOT the broker's server clock (contrast ``server_time_utc()``,
        which returns the broker's last-tick time) — the honest reference for how
        old the broker's last tick is right now.

        NON-THROWING (F1): the ENTIRE body is exception-guarded, so a hostile or
        broken SDK tick (symbol_info_tick raising, a raising attribute/property,
        a raising ``float()``/timestamp conversion) yields ``None`` (unavailable)
        rather than propagating — the accessor's fail-closed contract."""
        if not self._connected:
            return None
        try:
            return self._read_market_condition()
        except Exception:   # noqa: BLE001 — any malformed/hostile tick -> unavailable
            return None

    def _read_market_condition(self):
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return None
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        raw_t = getattr(tick, "time", None)
        if isinstance(bid, bool) or isinstance(ask, bool):
            return None
        if not isinstance(bid, (int, float)) or not isinstance(ask, (int, float)):
            return None
        if not (math.isfinite(bid) and math.isfinite(ask)) or bid <= 0 or ask <= 0:
            return None
        if ask < bid:
            return None
        if isinstance(raw_t, bool) or not isinstance(raw_t, (int, float)) \
                or not math.isfinite(raw_t) or raw_t <= 0:
            return None
        tick_time = datetime.fromtimestamp(float(raw_t), tz=timezone.utc)
        return MarketCondition(symbol=SYMBOL, bid=float(bid), ask=float(ask),
                               tick_time_utc=tick_time,
                               server_time_utc=datetime.now(timezone.utc))

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
    def _filling_preference(self):
        """Ordered (order_filling_const, symbol_filling_flag) preference —
        RETURN, then IOC, then FOK — read from the sdk. SYMBOL_FILLING_RETURN is
        absent on the real MT5 package (getattr -> 0), so RETURN is skipped in
        production and IOC/FOK win; a fake sdk may advertise it for testing."""
        s = self.sdk
        return (
            (getattr(s, "ORDER_FILLING_RETURN", None), getattr(s, "SYMBOL_FILLING_RETURN", 0)),
            (getattr(s, "ORDER_FILLING_IOC", None), getattr(s, "SYMBOL_FILLING_IOC", 0)),
            (getattr(s, "ORDER_FILLING_FOK", None), getattr(s, "SYMBOL_FILLING_FOK", 0)),
        )

    def open_position(self, side: str, lots: float, sl: float, tp: float,
                      intent_id: str) -> "mt5_results.MT5SubmitResult":
        """Submit ONE open-market order and return a typed classification
        (LX-1 Slice 3). Never returns a loose tuple; never resubmits; an
        order_send exception is CAUGHT and typed (EXCEPTION), reconciling the
        module docstring's 'never raises broker SDK exceptions upward'."""
        if not self._connected:
            return mt5_results.not_submitted("gateway_not_connected", requested_volume=lots)
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        symbol_info = self.sdk.symbol_info(self.config.broker_symbol)
        # Broker-constraint normalization (LX-1 Slice 2): NOT_SUBMITTED (zero
        # order_send) when the request cannot be made broker-valid. Never widens
        # a strategy stop; never increases volume.
        norm = normalize_open(symbol_info, tick, side, lots, sl, tp,
                              filling_preference=self._filling_preference())
        if not norm.ok:
            return mt5_results.not_submitted(
                f"normalization_rejected: {norm.reason.value} ({norm.diagnostic})",
                requested_volume=lots)
        o = norm.order
        order_type = self.sdk.ORDER_TYPE_BUY if side == "long" else self.sdk.ORDER_TYPE_SELL
        request = {
            "action": self.sdk.TRADE_ACTION_DEAL, "symbol": self.config.broker_symbol,
            "volume": o.volume, "type": order_type, "price": o.price,
            "sl": o.sl, "tp": o.tp, "deviation": o.deviation,
            "magic": self.config.magic_number, "comment": intent_id[:26],
            "type_time": self.sdk.ORDER_TIME_GTC,
            "type_filling": o.type_filling,
        }
        try:
            result = self.sdk.order_send(request)      # called exactly once
        except Exception as exc:                        # noqa: BLE001 - typed, never propagates
            return mt5_results.from_exception(exc, requested_volume=o.volume)
        return mt5_results.classify(mt5_results.extract_evidence(result), o.volume,
                                    mt5_results.build_retcode_map(self.sdk))

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
