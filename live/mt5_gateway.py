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
from live.account_health import AccountHealth
from live.account_identity import AccountIdentity, finite_nonneg
from live.broker_constraints import normalize_open
from live.config import SYMBOL
from live.safety import MarketCondition

_MAX_SERVER_LEN = 64
_MAX_CURRENCY_LEN = 8

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

    # ── account identity (LX-1 Slice 6 — preflight verification only) ─────────
    def account_identity(self):
        """Read-only, non-throwing snapshot of the connected account's identity as
        an immutable ``AccountIdentity``, or ``None`` if disconnected/unavailable/
        malformed. Never mutates the connection, submits nothing, retains no SDK
        object, and exposes NO password/credentials. Used only by preflight
        verification (never threaded into execution). The ENTIRE read is
        exception-guarded — a hostile/broken account object yields ``None``."""
        if not self._connected:
            return None
        try:
            return self._read_account_identity()
        except Exception:   # noqa: BLE001 — any malformed/hostile account -> unavailable
            return None

    def _normalize_trade_mode(self, raw):
        """MT5 account trade_mode int -> {demo, contest, real}, read via sdk
        constants (like build_retcode_map); unknown/bool/None -> None (fail closed).

        S6-F1 hardening: the three constants must be distinct integers. A broken or
        aliased SDK whose constants collide would otherwise let a value silently
        last-win onto the wrong mode label, so ANY duplicate or non-integer
        constant fails closed (None) rather than guessing."""
        if isinstance(raw, bool) or not isinstance(raw, int):
            return None
        s = self.sdk
        pairs = ((getattr(s, "ACCOUNT_TRADE_MODE_DEMO", 0), "demo"),
                 (getattr(s, "ACCOUNT_TRADE_MODE_CONTEST", 1), "contest"),
                 (getattr(s, "ACCOUNT_TRADE_MODE_REAL", 2), "real"))
        values = [v for v, _ in pairs]
        if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
            return None                       # malformed constant type
        if len(set(values)) != len(values):
            return None                       # aliased/duplicated constants -> ambiguous
        return dict(pairs).get(raw)

    def _read_account_identity(self):
        acct = self.sdk.account_info()
        if acct is None:
            return None
        login = getattr(acct, "login", None)
        if isinstance(login, bool) or not isinstance(login, int) or login <= 0:
            return None
        server = getattr(acct, "server", None)
        if not isinstance(server, str):
            return None
        server = server.strip()
        if not server or len(server) > _MAX_SERVER_LEN:
            return None
        currency = getattr(acct, "currency", None)
        if not isinstance(currency, str):
            return None
        currency = currency.strip().upper()
        if not currency or len(currency) > _MAX_CURRENCY_LEN:
            return None
        trade_mode = self._normalize_trade_mode(getattr(acct, "trade_mode", None))
        if trade_mode is None:
            return None
        balance = finite_nonneg(getattr(acct, "balance", None))
        equity = finite_nonneg(getattr(acct, "equity", None))
        if balance is None or equity is None:
            return None
        return AccountIdentity(login=int(login), server=server, currency=currency,
                               trade_mode=trade_mode, balance=balance, equity=equity)

    # ── account health (LX-1 Slice 7 — per-OPEN-cycle capital/permission gate) ─
    def account_health(self):
        """Read-only, non-throwing snapshot of the connected account's capital &
        trade-permission health as an immutable ``AccountHealth``, or ``None`` if
        disconnected/unavailable/malformed. Exactly ONE ``account_info()`` call;
        never mutates, submits nothing, retains no SDK object, exposes NO
        credentials. The ENTIRE read is exception-guarded — a hostile/broken
        account object yields ``None``. Does NOT call ``terminal_info()`` and does
        NOT widen ``snapshot()``."""
        if not self._connected:
            return None
        try:
            return self._read_account_health()
        except Exception:   # noqa: BLE001 — any malformed/hostile account -> unavailable
            return None

    def _read_account_health(self):
        acct = self.sdk.account_info()
        if acct is None:
            return None
        currency = getattr(acct, "currency", None)
        if not isinstance(currency, str):
            return None
        currency = currency.strip().upper()
        if not currency or len(currency) > _MAX_CURRENCY_LEN:
            return None
        balance = finite_nonneg(getattr(acct, "balance", None))
        equity = finite_nonneg(getattr(acct, "equity", None))
        free_margin = finite_nonneg(getattr(acct, "margin_free", None))
        if balance is None or equity is None or free_margin is None:
            return None
        trade_allowed = getattr(acct, "trade_allowed", None)
        trade_expert = getattr(acct, "trade_expert", None)
        if not isinstance(trade_allowed, bool) or not isinstance(trade_expert, bool):
            return None
        return AccountHealth(currency=currency, balance=balance, equity=equity,
                             free_margin=free_margin, trade_allowed=trade_allowed,
                             trade_expert=trade_expert)

    # ── account state ────────────────────────────────────────────────────────
    def closed_deals(self, since_utc, until_utc) -> "deal_records.DealReadResult":
        """Read this instance's TRADE deals over a bounded UTC window (B1).

        The runner has never had a history reader: `snapshot()` describes only
        OPEN entities, so a closed position simply vanishes and its realised
        outcome is invisible. This is the narrow read that makes confirmed-close
        evidence available — and nothing more. It resolves no intent, computes no
        R and writes no state; Milestone B2 owns all of that.

        NON-THROWING, matching this module's other accessors: any hostile or
        broken SDK response yields UNAVAILABLE rather than propagating.

        THE DISTINCTION THIS METHOD EXISTS TO PRESERVE: `history_deals_get`
        signals failure by returning `None`. The idiom `... or []` turns that
        into an empty successful read — a failed read reported as a quiet market.
        Here `None`, a disconnected terminal and an exception are all UNAVAILABLE,
        and no value of `deals` can ever mean "the read failed".

        Filtering is to THIS instance: configured magic number, configured broker
        symbol, and trade deal types only (balance/credit/correction records are
        excluded, not rejected). BOTH `in` and `out` deals are returned, because
        B2 needs entry deals to derive the total entry quantity that forms the
        denominator of R.
        """
        from live import deal_records

        if not isinstance(since_utc, datetime) or not isinstance(until_utc, datetime):
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="window bounds must be datetimes")
        if since_utc.tzinfo is None or until_utc.tzinfo is None:
            # A naive bound would be interpreted in local time by the SDK and
            # could silently shift which day a deal is attributed to.
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="window bounds must be timezone-aware UTC")
        if since_utc > until_utc:
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="window start is after window end",
                window_from=since_utc, window_to=until_utc)
        if not self._connected:
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="gateway not connected",
                window_from=since_utc, window_to=until_utc)

        history = getattr(self.sdk, "history_deals_get", None)
        if not callable(history):
            # A terminal build without deal history is UNAVAILABLE, not empty.
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="terminal exposes no deal history",
                window_from=since_utc, window_to=until_utc)
        try:
            raw_deals = history(since_utc, until_utc)
        except Exception as exc:                                # noqa: BLE001
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail=f"{type(exc).__name__}",
                window_from=since_utc, window_to=until_utc)
        if raw_deals is None:
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail="history read failed (None)",
                window_from=since_utc, window_to=until_utc)

        deals: list = []
        rejected: list = []
        try:
            for raw in raw_deals:
                # Per-record isolation: a single hostile object must not discard
                # the whole window. Only a failure to WALK the collection is
                # fatal (handled below); a failure to read one record is a
                # rejection, surfaced rather than dropped.
                try:
                    if not deal_records.is_trade_deal(raw):
                        continue                # balance/credit: not our subject
                    if getattr(raw, "magic", None) != self.config.magic_number:
                        continue                # another EA or a hand-placed trade
                    if str(getattr(raw, "symbol", "") or "") != self.config.broker_symbol:
                        continue
                except Exception as exc:                        # noqa: BLE001
                    rejected.append(deal_records.RejectedDeal(
                        reason=deal_records.REJECT_UNREADABLE,
                        detail=type(exc).__name__))
                    continue
                record, reject = deal_records.normalize_deal(raw)
                (deals if record is not None else rejected).append(
                    record if record is not None else reject)
        except Exception as exc:                                # noqa: BLE001
            # The collection itself was not iterable/stable: the read cannot be
            # trusted as a whole, so it is UNAVAILABLE rather than partial.
            return deal_records.DealReadResult(
                outcome=deal_records.DealReadOutcome.UNAVAILABLE,
                detail=f"{type(exc).__name__}",
                window_from=since_utc, window_to=until_utc)

        # Deterministic order: (execution time, deal id). `history_deals_get` is
        # unordered and nothing documents its order, so an ordering imposed here
        # is what makes repeated reads comparable.
        deals.sort(key=lambda d: (d.execution_time_utc, d.deal_id))
        rejected.sort(key=lambda r: (r.reason, r.raw_ticket or ""))
        outcome = (deal_records.DealReadOutcome.MALFORMED if rejected
                   else deal_records.DealReadOutcome.OK)
        return deal_records.DealReadResult(
            outcome=outcome, deals=tuple(deals), rejected=tuple(rejected),
            detail=(f"{len(rejected)} unusable trade deal(s)" if rejected else None),
            window_from=since_utc, window_to=until_utc)

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

    # ── LIVE-3 typed manual-management operations ────────────────────────────
    # Narrowly scoped: each performs AT MOST one order_send, returns a typed
    # MT5ActionResult, never raises SDK exceptions upward, retains no SDK object.

    def modify_position_protection(self, ticket: int, sl, tp) -> "mt5_results.MT5ActionResult":
        """Modify SL/TP of ONE open position (TRADE_ACTION_SLTP). A None sl/tp
        keeps the position's current value (verified from a fresh positions_get
        read); the position must exist. Exactly one order_send."""
        if not self._connected:
            return mt5_results.action_not_submitted("gateway_not_connected")
        try:
            positions = self.sdk.positions_get(ticket=int(ticket)) or []
            if not positions:
                return mt5_results.action_not_submitted(f"position {ticket} not found")
            p = positions[0]
            request = {"action": self.sdk.TRADE_ACTION_SLTP,
                       "symbol": self.config.broker_symbol,
                       "position": int(ticket),
                       "sl": float(sl) if sl is not None else float(getattr(p, "sl", 0.0) or 0.0),
                       "tp": float(tp) if tp is not None else float(getattr(p, "tp", 0.0) or 0.0)}
            result = self.sdk.order_send(request)          # called exactly once
        except Exception as exc:                            # noqa: BLE001 - typed
            return mt5_results.action_from_exception(exc)
        return mt5_results.classify_action(mt5_results.extract_evidence(result),
                                           mt5_results.build_retcode_map(self.sdk))

    def cancel_pending_order(self, ticket: int) -> "mt5_results.MT5ActionResult":
        """Remove ONE pending order (TRADE_ACTION_REMOVE). The order must exist
        in a fresh orders_get read. Exactly one order_send."""
        if not self._connected:
            return mt5_results.action_not_submitted("gateway_not_connected")
        try:
            orders = [o for o in (self.sdk.orders_get(symbol=self.config.broker_symbol) or [])
                      if getattr(o, "ticket", None) == int(ticket)]
            if not orders:
                return mt5_results.action_not_submitted(f"pending order {ticket} not found")
            request = {"action": self.sdk.TRADE_ACTION_REMOVE, "order": int(ticket)}
            result = self.sdk.order_send(request)          # called exactly once
        except Exception as exc:                            # noqa: BLE001 - typed
            return mt5_results.action_from_exception(exc)
        return mt5_results.classify_action(mt5_results.extract_evidence(result),
                                           mt5_results.build_retcode_map(self.sdk))

    def close_position_full(self, ticket: int) -> "mt5_results.MT5ActionResult":
        """Close ONE open position IN FULL at market (TRADE_ACTION_DEAL with the
        observed volume — never more, never a partial). Exactly one order_send."""
        if not self._connected:
            return mt5_results.action_not_submitted("gateway_not_connected")
        try:
            positions = self.sdk.positions_get(ticket=int(ticket)) or []
            if not positions:
                return mt5_results.action_not_submitted(f"position {ticket} not found")
            p = positions[0]
            side_close = (self.sdk.ORDER_TYPE_SELL if p.type == self.sdk.ORDER_TYPE_BUY
                          else self.sdk.ORDER_TYPE_BUY)
            tick = self.sdk.symbol_info_tick(self.config.broker_symbol)
            if tick is None:
                return mt5_results.action_not_submitted("no market tick for close price")
            price = tick.bid if side_close == self.sdk.ORDER_TYPE_SELL else tick.ask
            request = {"action": self.sdk.TRADE_ACTION_DEAL,
                       "symbol": self.config.broker_symbol,
                       "volume": p.volume, "type": side_close, "position": int(ticket),
                       "price": float(price), "deviation": 20,
                       "magic": self.config.magic_number, "comment": "ct_close",
                       "type_filling": self.sdk.ORDER_FILLING_IOC}
            result = self.sdk.order_send(request)          # called exactly once
        except Exception as exc:                            # noqa: BLE001 - typed
            return mt5_results.action_from_exception(exc)
        return mt5_results.classify_action(mt5_results.extract_evidence(result),
                                           mt5_results.build_retcode_map(self.sdk))
