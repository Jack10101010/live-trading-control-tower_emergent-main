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

# DST calendars a broker clock can follow. The zone supplies only the DST
# *schedule* (its own base offset is irrelevant — `_base` provides that).
_DST_ZONES = {"us": ZoneInfo("America/New_York"), "eu": ZoneInfo("Europe/Brussels")}


def _finite_positive(v) -> bool:
    """A price we can compare against. NaN/inf/None/<=0 are not prices."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return False
    return f == f and f not in (float("inf"), float("-inf")) and f > 0


class MT5Gateway:
    """Thin, stateless-ish wrapper. `sdk` is injectable for tests/fakes."""

    def __init__(self, config, sdk: Any = None):
        self.config = config
        self.sdk = sdk if sdk is not None else _mt5
        self._connected = False
        self._last_probe_error = ""
        self._base = timedelta(hours=int(getattr(config, "mt5_server_base_utc_offset_hours", 2)))
        self._dst_rule = str(getattr(config, "mt5_server_dst_rule", "us")).lower()
        self._dst_zone = _DST_ZONES.get(self._dst_rule)
        if self._dst_rule != "none" and self._dst_zone is None:
            raise ValueError(f"MT5_SERVER_DST_RULE must be one of {sorted(_DST_ZONES)} or 'none', "
                             f"got {self._dst_rule!r}")

    # ── time base: server wall clock <-> canonical UTC ───────────────────────
    def server_utc_offset(self, at_utc: datetime | None = None) -> timedelta:
        """The broker clock's UTC offset at an instant: base + DST calendar.

        Modelled rather than taken from an IANA zone because this broker keeps
        an EET-style base but switches on the US calendar (measured), which no
        single IANA zone expresses.
        """
        if self._dst_zone is None:
            return self._base
        at = at_utc or datetime.now(timezone.utc)
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        return self._base + (at.astimezone(self._dst_zone).dst() or timedelta(0))

    def server_epoch_to_utc(self, epoch: float) -> datetime:
        """MT5-encoded timestamp -> true UTC.

        The stored value is the server's wall clock stamped as UTC, so it is
        read back as a naive wall clock and the offset subtracted. The offset
        itself depends on the instant, so it is estimated from the base and then
        refined once — which resolves correctly on both sides of a transition.
        """
        wall = datetime.fromtimestamp(float(epoch), tz=timezone.utc).replace(tzinfo=None)
        approx = (wall - self._base).replace(tzinfo=timezone.utc)
        offset = self.server_utc_offset(approx)
        result = (wall - offset).replace(tzinfo=timezone.utc)
        refined = self.server_utc_offset(result)
        if refined != offset:
            result = (wall - refined).replace(tzinfo=timezone.utc)
        return result

    def utc_to_server_arg(self, dt_utc: datetime) -> datetime:
        """True UTC -> the datetime the copy_* APIs expect.

        MT5 compares the argument's epoch against its stored (server-encoded)
        values, so the argument must carry the server wall clock stamped as UTC
        — the exact inverse of `server_epoch_to_utc`.
        """
        wall = dt_utc + self.server_utc_offset(dt_utc)
        return wall.replace(tzinfo=timezone.utc)

    def declared_offset_hours(self, at_utc: datetime | None = None) -> float:
        """Server offset the configured model implies at an instant."""
        return self.server_utc_offset(at_utc).total_seconds() / 3600.0

    def measured_offset_hours(self) -> float | None:
        """Offset implied by a live tick: raw server label vs true UTC."""
        if not self._connected:
            return None
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return None
        raw = datetime.fromtimestamp(float(tick.time), tz=timezone.utc)
        return (raw - datetime.now(timezone.utc)).total_seconds() / 3600.0

    def dst_disagreement(self, at_utc: datetime | None = None) -> str:
        """Non-empty while the US and EU DST calendars disagree.

        These shoulder windows (~4 weeks/yr) are exactly where picking the wrong
        calendar costs an hour, so the condition is surfaced rather than hidden.
        """
        at = at_utc or datetime.now(timezone.utc)
        us = at.astimezone(_DST_ZONES["us"]).dst() or timedelta(0)
        eu = at.astimezone(_DST_ZONES["eu"]).dst() or timedelta(0)
        if us == eu:
            return ""
        return (f"US/EU DST calendars disagree today (us={us}, eu={eu}); "
                f"rule in force = {self._dst_rule!r}")

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
        model = (f"base{self._base.total_seconds()/3600:+.0f}h dst={self._dst_rule} "
                 f"=> {declared:+.0f}h")
        shoulder = self.dst_disagreement(now)
        suffix = f" [{shoulder}]" if shoulder else ""
        if skew > tolerance_minutes * 60:
            # Distinguish the two causes, because the remedies are opposite: a
            # whole-hour error means the CLOCK MODEL is wrong; a small ragged
            # offset means this VPS's own clock has drifted (fix NTP, not config).
            hours = skew / 3600.0
            off_hour = abs(hours - round(hours))
            cause = (f"server clock model ({model}) is wrong"
                     if off_hour < 0.05 and abs(hours) >= 0.5 else
                     f"THIS HOST's clock looks wrong — check w32time/NTP sync "
                     f"(model {model} is a whole-hour offset, this error is not)")
            return False, (f"tick {hours:+.2f}h in the FUTURE after conversion — {cause}{suffix}")
        if abs(skew) <= tolerance_minutes * 60:
            return True, f"verified against live tick ({model}, skew {skew:+.0f}s){suffix}"
        # Stale tick (market closed): the model cannot be confirmed, but a live
        # tick would be reported here if one existed, and the future-check above
        # still guards the dangerous direction.
        measured = self.measured_offset_hours()
        return True, (f"unverified — last tick {abs(skew)/3600:.1f}h old (market closed); "
                      f"{model}, raw tick implies {measured:+.2f}h{suffix}")

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
            if not ok:
                return False, f"reconnect failed: {detail} ({self._last_probe_error})".strip(" ()")
            # A reconnect can land on a DIFFERENT terminal or account than the one
            # validated at startup, so the time base is re-verified rather than
            # inherited — a broker in another zone would otherwise silently
            # resume mislabelling bars.
            tb_ok, tb_detail = self.verify_time_base()
            if not tb_ok:
                self.disconnect()
                return False, f"reconnected but time base rejected: {tb_detail}"
            return True, "reconnected"
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

    def read_account_state(self) -> tuple[bool, dict | str]:
        """READ-ONLY terminal + account facts, as ONE coherent sample.

        Added for M-NODE-ACCT-1: `snapshot()` exposes only login/balance/equity/
        currency, but the canonical telemetry contract also needs `server`,
        `trade_mode`, `free_margin` and the broker permission flags. Rather than
        widen `snapshot()` (which the executor's reconciliation path depends on)
        this is a separate accessor with no caller in the trading path.

        Terminal and account are read in one call so identity and health cannot
        be stitched together from two different sessions or two different
        instants -- the contract requires one coherent observation.

        NEVER connects. If the governed session is not already up this returns
        `(False, "not connected")`; opening a second MT5 session would compete
        with the node's own and is exactly what the milestone forbids. Raw SDK
        objects never cross this boundary: only plain values are returned.

        `terminal.trade_allowed` is the AutoTrading toggle and is deliberately
        kept distinct from `account.trade_allowed`/`trade_expert`, which are
        broker-side permissions. Conflating them would let a locked-down terminal
        report as trade-enabled.
        """
        if not self._connected:
            return False, "not connected"
        term = self.sdk.terminal_info()
        acct = self.sdk.account_info()
        if acct is None:
            return False, "account_info unavailable"
        return True, {
            "terminal": None if term is None else {
                "connected": bool(getattr(term, "connected", False)),
                # AutoTrading button — NOT a broker permission.
                "trade_allowed": bool(getattr(term, "trade_allowed", False)),
            },
            "account": {
                "login": getattr(acct, "login", None),
                "server": getattr(acct, "server", None),
                "currency": getattr(acct, "currency", None),
                "trade_mode": getattr(acct, "trade_mode", None),
                "balance": getattr(acct, "balance", None),
                "equity": getattr(acct, "equity", None),
                "free_margin": getattr(acct, "margin_free", None),
                # Broker-side permissions.
                "trade_allowed": bool(getattr(acct, "trade_allowed", False)),
                "trade_expert": bool(getattr(acct, "trade_expert", False)),
            },
        }

    # ── order operations (market mirror model: engine is the state machine) ──
    def current_quote(self) -> tuple[bool, dict | str]:
        """Fresh executable bid/ask, sampled NOW. Read-only.

        M-LIVE-STALE-OPEN-GUARDS-1. The price-divergence rail must compare
        against the price the broker would actually execute against at the
        moment of submission -- not a quote captured when the ~20 minute
        recompute began. This is the same `symbol_info_tick` read
        `open_position` performs microseconds later, exposed so a rail can use
        it without reaching into the SDK or duplicating the side convention.

        Returns `(False, reason)` rather than a guess when the tick is absent,
        so the caller can fail closed.
        """
        if not self._connected:
            return False, "not connected"
        tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
        if tick is None:
            return False, "no tick"
        bid, ask = getattr(tick, "bid", None), getattr(tick, "ask", None)
        if not _finite_positive(bid) or not _finite_positive(ask):
            return False, f"non-finite quote bid={bid!r} ask={ask!r}"
        return True, {"bid": float(bid), "ask": float(ask),
                      "at": datetime.now(timezone.utc).isoformat()}

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
