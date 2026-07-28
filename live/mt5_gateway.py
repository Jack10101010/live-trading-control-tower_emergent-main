"""MT5 gateway — the ONLY module (besides backend/broker.py's adapter, which
delegates here) allowed to touch the MetaTrader5 package. Import-guarded so the
whole slice imports and tests cleanly off-VPS; every call answers with a
structured (ok, data|error) pair and never raises broker SDK exceptions upward.

TIME BASE (the gateway's second responsibility). MetaTrader 5 encodes tick and
bar times as the BROKER SERVER's wall clock stamped as if it were UTC — the
vendor docs call this "UTC time zone (without the shift)", which reads as true
UTC but is not: on an EEST server a tick is timestamped +3h, i.e. in the future
relative to real UTC. The API exposes no timezone, so the zone is declared
(`MT5_SERVER_TZ`, default Europe/Athens = EET/EEST for FTMO) and converted here
with zoneinfo, which handles the DST transitions. This module is the ONLY place
the two bases meet: everything above it — bridge, runner, intents, executor,
state, ops — operates exclusively on canonical UTC.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

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
        self._zone = ZoneInfo(getattr(config, "mt5_server_tz", "Europe/Athens"))

    # ── time base: server wall clock <-> canonical UTC ───────────────────────
    def server_epoch_to_utc(self, epoch: float) -> datetime:
        """MT5-encoded timestamp -> true UTC.

        The stored value is the server's wall clock stamped as UTC, so it is
        first read back as a naive wall clock, then localised to the broker zone
        and converted. DST is resolved by zoneinfo; the ambiguous hour of the
        autumn fold falls inside the weekend market closure (EU transitions at
        03:00/04:00 local Sunday; the week opens 00:00 Monday server), so no
        live bar is ever produced inside it.
        """
        wall = datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(tzinfo=None)
        return wall.replace(tzinfo=self._zone).astimezone(timezone.utc)

    def utc_to_server_arg(self, dt_utc: datetime) -> datetime:
        """True UTC -> the datetime the copy_* APIs expect.

        MT5 compares the argument's epoch against its stored (server-encoded)
        values, so the argument must carry the server wall clock stamped as UTC
        — the exact inverse of `server_epoch_to_utc`.
        """
        wall = dt_utc.astimezone(self._zone).replace(tzinfo=None)
        return wall.replace(tzinfo=timezone.utc)

    def declared_offset_hours(self, at_utc: datetime | None = None) -> float:
        """Server offset the declared zone implies right now (+2 EET / +3 EEST)."""
        at = at_utc or datetime.now(timezone.utc)
        return at.astimezone(self._zone).utcoffset().total_seconds() / 3600.0

    def verify_time_base(self, tolerance_minutes: int = 5) -> tuple[bool, str]:
        """Cross-check the declared zone against a live tick.

        A converted tick may legitimately be old (market closed) but must NEVER
        be in the future — that is the unmistakable signature of a wrong or
        missing zone conversion, so it is the one condition that fails closed.
        """
        if not self._connected:
            return False, "not connected"
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return False, "no tick for %s" % self.config.broker_symbol
        now = datetime.now(timezone.utc)
        tick_utc = self.server_epoch_to_utc(tick.time)
        skew = (tick_utc - now).total_seconds()
        declared = self.declared_offset_hours(now)
        if skew > tolerance_minutes * 60:
            return False, (f"tick {skew/3600:+.2f}h in the FUTURE after conversion — "
                           f"MT5_SERVER_TZ={self.config.mt5_server_tz} (offset {declared:+.0f}h) is wrong")
        if abs(skew) <= tolerance_minutes * 60:
            return True, f"verified against live tick (zone offset {declared:+.0f}h, skew {skew:+.0f}s)"
        return True, (f"unverified — last tick {abs(skew)/3600:.1f}h old (market closed); "
                      f"zone offset {declared:+.0f}h, not in the future")

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

    def ensure_connected(self) -> tuple[bool, str]:
        """Idempotent connect that survives a terminal restart or IPC drop.

        `initialize()` succeeding once says nothing about the link staying up:
        the terminal can exit or lose the broker session while this process runs
        and every later call would fail with a stale handle. Probing
        terminal_info() each cycle and re-initialising is what turns a terminal
        restart from a 10-error process exit into a transparent recovery.
        """
        if self._connected:
            try:
                info = self.sdk.terminal_info()
            except Exception as exc:  # SDK may raise on a dead IPC handle
                info = None
                self._last_probe_error = str(exc)
            if info is not None and getattr(info, "connected", False):
                return True, "connected"
            self._connected = False
            try:
                self.sdk.shutdown()
            except Exception:  # pragma: no cover - best-effort teardown
                pass
            ok, detail = self.connect()
            return ok, ("reconnected" if ok else f"reconnect failed: {detail}")
        return self.connect()

    def disconnect(self) -> None:
        if self.available and self._connected:
            self.sdk.shutdown()
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    # ── market data (closed bars only) ───────────────────────────────────────
    def server_time_utc(self) -> datetime | None:
        """Current broker time as canonical UTC (converted from the server zone)."""
        if not self._connected:
            return None
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return None
        return self.server_epoch_to_utc(tick.time)

    def closed_m1_bars(self, since_utc: datetime, limit: int = 5000) -> tuple[bool, list[dict] | str]:
        """Closed M1 bars with open time > since_utc, in canonical UTC.

        Both directions of the time base are crossed here: the range bounds are
        converted UTC -> server encoding (passing raw UTC silently under-fetches
        by the whole server offset — 3h of missing bars on an EEST server) and
        every returned bar time is converted server -> UTC. The currently
        forming minute is excluded by requiring bar_open + 60s <= now.
        """
        if not self._connected:
            return False, "not connected"
        if since_utc.tzinfo is None:          # defensive: callers pass aware UTC
            since_utc = since_utc.replace(tzinfo=timezone.utc)
        now = self.server_time_utc()          # already canonical UTC
        if now is None:
            return False, "no server time (symbol tick unavailable)"
        rates = self.sdk.copy_rates_range(
            self.config.broker_symbol, self.sdk.TIMEFRAME_M1,
            self.utc_to_server_arg(since_utc), self.utc_to_server_arg(now))
        if rates is None:
            return False, f"copy_rates_range failed: {self.sdk.last_error()}"
        out = []
        cutoff = now - timedelta(seconds=60)
        for r in list(rates)[:limit]:
            rec = (r if isinstance(r, dict)
                   else {"time": r[0], "open": r[1], "high": r[2], "low": r[3],
                         "close": r[4], "tick_volume": r[5]})
            t_utc = self.server_epoch_to_utc(rec["time"])
            if t_utc <= since_utc or t_utc > cutoff:
                continue  # already stored / still forming
            out.append({
                "time": t_utc.strftime("%Y-%m-%d %H:%M:%S+00:00"),
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
