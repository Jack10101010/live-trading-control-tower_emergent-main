"""LIVE-5A — the canonical LIVE MARKET RUNTIME.

WHAT THIS OWNS
    The live relationship with the market feed, as evidence: whether the broker
    answered, when it last answered, how long it took, which symbols are
    subscribed, the newest tick and the newest COMPLETED candle per symbol, and
    whether any of that is now too old to trust.

WHAT THIS DOES NOT OWN (each is asserted by a structural test)
    * Trades. There is no order submission, modification or cancellation here,
      and no import of the execution pipeline.
    * Strategy. No signal, no indicator, no decision.
    * Projections. This produces market EVIDENCE; `operational_projection`
      remains the single owner of what the UI sees.
    * Broker WRITES. Every broker call in this module is a read.

AUDIT THAT SHAPED THIS MODULE
    The repository already had substantial live-read capability, so this composes
    it rather than replacing anything:

      * `market_data.MarketDataEngine` owns provider selection (Fixture, Replay,
        MockLive, MT5) and per-symbol snapshots, including MT5 `symbol_select`
        subscription and `copy_rates_from_pos` candles.
      * `broker.get_broker()` owns connection state and the canonical account /
        position snapshot.
      * `live/mt5_gateway.py` owns the raw SDK, and is annotated
        "Windows VPS only; absent everywhere else by design".

    What did NOT exist was any single component that polled those sources on a
    deterministic cadence and recorded WHEN it last succeeded. Before LIVE-5A
    every `/api/operations/*` request performed its own synchronous broker read
    (`server._fresh_broker_snapshot()` via `_projection_sources()`), so with
    fourteen polling queries in the browser the broker was read fourteen times a
    cycle with no shared notion of freshness. This module is the read half of
    fixing that; `runtime_supervisor` is the cadence half.

VALUE SEMANTICS
    A field that could not be read is `None` and its availability says so. Zero
    is never a substitute for absent: a spread of 0.0 is a real measured spread,
    and a spread that could not be read is `None`. A stale reading is neither
    fresh nor missing — it is explicitly `stale`, with its age.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Callable

# ── availability vocabulary (identical to the projection's, deliberately) ─────
AVAILABLE = "ok"
STALE = "stale"
UNAVAILABLE = "unavailable"

# ── connection vocabulary ────────────────────────────────────────────────────
CONN_CONNECTED = "Connected"
CONN_DISCONNECTED = "Disconnected"
CONN_CONNECTING = "Connecting"
CONN_UNKNOWN = "Unknown"

#: A tick older than this is stale. One trading minute is generous for a live
#: feed and tolerant of a slow poll; it is a CONSTANT, not a guess per call.
DEFAULT_TICK_STALE_S = 60.0
#: A completed M15 candle is expected roughly every 900s. Two intervals plus
#: slack: beyond this the candle stream is not keeping up.
DEFAULT_CANDLE_STALE_S = 2100.0
#: A heartbeat older than this means we no longer know the broker is there.
DEFAULT_HEARTBEAT_STALE_S = 45.0

#: Latency above this is reported as degraded — the read worked but the link is
#: unhealthy, which is a different fact from "disconnected".
DEGRADED_LATENCY_MS = 2000.0


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _age_s(now: str, at: Any) -> float | None:
    """Age in seconds, or None when either end is unreadable.

    TIMEZONE: every timestamp crossing this boundary is ISO-8601 UTC. MT5 server
    time is normalized to UTC by the gateway (`server_time_utc`) before it ever
    reaches here, so no local-time arithmetic happens in this module.
    """
    ref, then = _parse(now), _parse(at)
    if ref is None or then is None:
        return None
    return round((ref - then).total_seconds(), 3)


def _sorted(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _sorted(v) for k, v in sorted(obj.items())}
    if isinstance(obj, (list, tuple)):
        return [_sorted(v) for v in obj]
    return obj


# ── immutable read models ────────────────────────────────────────────────────

@dataclass(frozen=True)
class FeedHeartbeat:
    """Did the feed answer, when, and how fast. Evidence, not opinion."""
    at: str | None = None                     # when the read SUCCEEDED
    attempted_at: str | None = None            # when it was TRIED
    latency_ms: float | None = None
    ok: bool = False
    connection: str = CONN_UNKNOWN
    detail: str | None = None                  # why it failed, when it failed
    consecutive_failures: int = 0

    def age_seconds(self, now: str) -> float | None:
        return _age_s(now, self.at)

    def availability(self, now: str, *,
                     stale_after_s: float = DEFAULT_HEARTBEAT_STALE_S) -> str:
        if self.at is None:
            return UNAVAILABLE
        age = self.age_seconds(now)
        if age is None:
            return UNAVAILABLE
        return STALE if age > stale_after_s else AVAILABLE

    def as_dict(self, now: str) -> dict:
        return _sorted({
            "at": self.at, "attemptedAt": self.attempted_at,
            "latencyMs": self.latency_ms, "ok": self.ok,
            "connection": self.connection, "detail": self.detail,
            "consecutiveFailures": self.consecutive_failures,
            "ageSeconds": self.age_seconds(now),
            "availability": self.availability(now),
        })


@dataclass(frozen=True)
class SymbolQuote:
    """The newest tick for one symbol. `bid`/`ask` are None when unread."""
    symbol: str
    bid: float | None = None
    ask: float | None = None
    last: float | None = None
    spread: float | None = None
    at: str | None = None
    provider: str | None = None
    detail: str | None = None

    def age_seconds(self, now: str) -> float | None:
        return _age_s(now, self.at)

    def availability(self, now: str, *,
                     stale_after_s: float = DEFAULT_TICK_STALE_S) -> str:
        if self.at is None or self.bid is None or self.ask is None:
            return UNAVAILABLE
        age = self.age_seconds(now)
        if age is None:
            return UNAVAILABLE
        return STALE if age > stale_after_s else AVAILABLE

    def as_dict(self, now: str) -> dict:
        return _sorted({
            "symbol": self.symbol, "bid": self.bid, "ask": self.ask,
            "last": self.last, "spread": self.spread, "at": self.at,
            "provider": self.provider, "detail": self.detail,
            "ageSeconds": self.age_seconds(now),
            "availability": self.availability(now),
        })


@dataclass(frozen=True)
class SymbolCandle:
    """The newest COMPLETED candle for one symbol and timeframe.

    Completed only: an in-progress candle is not evidence of anything, and the
    scenario producer must never see one (a bar that is still forming would
    produce a different Scenario every poll).
    """
    symbol: str
    timeframe: str
    open: float | None = None
    high: float | None = None
    low: float | None = None
    close: float | None = None
    opened_at: str | None = None               # bar open time (UTC)
    closed_at: str | None = None               # bar close time (UTC)
    provider: str | None = None
    detail: str | None = None

    @property
    def complete(self) -> bool:
        return (self.closed_at is not None and self.close is not None
                and self.open is not None)

    def age_seconds(self, now: str) -> float | None:
        return _age_s(now, self.closed_at)

    def availability(self, now: str, *,
                     stale_after_s: float = DEFAULT_CANDLE_STALE_S) -> str:
        if not self.complete:
            return UNAVAILABLE
        age = self.age_seconds(now)
        if age is None:
            return UNAVAILABLE
        return STALE if age > stale_after_s else AVAILABLE

    def as_dict(self, now: str) -> dict:
        return _sorted({
            "symbol": self.symbol, "timeframe": self.timeframe,
            "open": self.open, "high": self.high, "low": self.low,
            "close": self.close, "openedAt": self.opened_at,
            "closedAt": self.closed_at, "provider": self.provider,
            "detail": self.detail, "complete": self.complete,
            "candleAgeSeconds": self.age_seconds(now),
            "availability": self.availability(now),
        })


@dataclass(frozen=True)
class SymbolSubscription:
    """One symbol this runtime is polling, and its newest evidence."""
    symbol: str
    broker_symbol: str | None = None           # the mapped feed symbol
    active: bool = False
    quote: SymbolQuote | None = None
    candle: SymbolCandle | None = None

    def as_dict(self, now: str) -> dict:
        return _sorted({
            "symbol": self.symbol, "brokerSymbol": self.broker_symbol,
            "active": self.active,
            "quote": self.quote.as_dict(now) if self.quote else None,
            "candle": self.candle.as_dict(now) if self.candle else None,
        })


@dataclass(frozen=True)
class MarketRuntimeSnapshot:
    """One deterministic reading of the whole market runtime.

    Immutable and self-describing: given the same sources at the same `now`, two
    reads produce byte-identical `as_dict()` output.
    """
    at: str
    heartbeat: FeedHeartbeat = field(default_factory=FeedHeartbeat)
    subscriptions: tuple = field(default_factory=tuple)
    provider: str | None = None
    adapter_kind: str | None = None
    refresh_sequence: int = 0
    refresh_duration_ms: float | None = None
    warnings: tuple = field(default_factory=tuple)

    def symbol(self, name: str) -> SymbolSubscription | None:
        for sub in self.subscriptions:
            if sub.symbol == name:
                return sub
        return None

    @property
    def degraded_latency(self) -> bool:
        return (self.heartbeat.latency_ms is not None
                and self.heartbeat.latency_ms > DEGRADED_LATENCY_MS)

    def as_dict(self, now: str | None = None) -> dict:
        ref = now or self.at
        return _sorted({
            "at": self.at, "heartbeat": self.heartbeat.as_dict(ref),
            "subscriptions": [s.as_dict(ref) for s in self.subscriptions],
            "provider": self.provider, "adapterKind": self.adapter_kind,
            "refreshSequence": self.refresh_sequence,
            "refreshDurationMs": self.refresh_duration_ms,
            "degradedLatency": self.degraded_latency,
            "warnings": list(self.warnings),
        })


# ── the reader ───────────────────────────────────────────────────────────────

class MarketRuntime:
    """Polls the feed on demand and returns immutable evidence.

    Deliberately has NO timer of its own: `runtime_supervisor` owns cadence, so
    there is exactly one component deciding when a read happens. `refresh()` is
    therefore safe to call from a test with an injected clock and produces the
    same result every time for the same inputs.
    """

    def __init__(self, *, symbols: tuple, now_iso_fn: Callable[[], str],
                 quote_fn: Callable[[str], dict | None],
                 candle_fn: Callable[[str], dict | None] | None = None,
                 connection_fn: Callable[[], str] | None = None,
                 provider_fn: Callable[[], str | None] | None = None,
                 adapter_kind_fn: Callable[[], str | None] | None = None,
                 broker_symbol_fn: Callable[[str], str | None] | None = None,
                 timeframe: str = "M15",
                 monotonic_fn: Callable[[], float] = time.monotonic):
        self._symbols = tuple(symbols)
        self._now = now_iso_fn
        self._quote = quote_fn
        self._candle = candle_fn
        self._connection = connection_fn or (lambda: CONN_UNKNOWN)
        self._provider = provider_fn or (lambda: None)
        self._adapter_kind = adapter_kind_fn or (lambda: None)
        self._broker_symbol = broker_symbol_fn or (lambda s: s)
        self._timeframe = timeframe
        # Injected so latency is measurable without a real clock in tests.
        self._monotonic = monotonic_fn
        self._sequence = 0
        self._failures = 0
        self._last: MarketRuntimeSnapshot | None = None

    @property
    def symbols(self) -> tuple:
        return self._symbols

    @property
    def last(self) -> MarketRuntimeSnapshot | None:
        """The newest snapshot, or None before the first refresh. None means
        "never read", which is NOT the same as "nothing to report"."""
        return self._last

    def refresh(self) -> MarketRuntimeSnapshot:
        """One complete polling pass. Never raises: a failed read becomes
        explicit unavailability plus a warning, because a market runtime that
        throws takes the whole dashboard down with it."""
        started = self._monotonic()
        now = self._now()
        warnings: list[str] = []

        connection = CONN_UNKNOWN
        try:
            connection = str(self._connection() or CONN_UNKNOWN)
        except Exception as exc:                                # noqa: BLE001
            warnings.append(f"connection_read_failed:{type(exc).__name__}")

        subscriptions: list[SymbolSubscription] = []
        any_quote = False
        for symbol in self._symbols:
            broker_symbol = None
            try:
                broker_symbol = self._broker_symbol(symbol)
            except Exception:                                   # noqa: BLE001
                broker_symbol = None
            quote = self._read_quote(symbol, warnings)
            candle = self._read_candle(symbol, warnings)
            any_quote = any_quote or (quote is not None and quote.bid is not None)
            subscriptions.append(SymbolSubscription(
                symbol=symbol, broker_symbol=broker_symbol,
                # "active" means we are polling it AND it answered — a symbol in
                # Market Watch that returns nothing is not an active feed.
                active=bool(quote is not None and quote.bid is not None),
                quote=quote, candle=candle))

        latency_ms = round((self._monotonic() - started) * 1000.0, 3)
        ok = bool(any_quote and connection == CONN_CONNECTED)
        self._failures = 0 if ok else self._failures + 1
        if not any_quote:
            warnings.append("no_symbol_answered")
        if connection != CONN_CONNECTED:
            warnings.append(f"broker_not_connected:{connection}")
        if latency_ms > DEGRADED_LATENCY_MS:
            warnings.append("feed_latency_degraded")

        provider = adapter = None
        try:
            provider = self._provider()
        except Exception:                                       # noqa: BLE001
            warnings.append("provider_read_failed")
        try:
            adapter = self._adapter_kind()
        except Exception:                                       # noqa: BLE001
            warnings.append("adapter_kind_read_failed")

        self._sequence += 1
        previous = self._last
        heartbeat = FeedHeartbeat(
            # A FAILED read must not advance the heartbeat: the last time we
            # genuinely heard from the feed is what "last heartbeat" means.
            at=now if ok else (previous.heartbeat.at if previous else None),
            attempted_at=now, latency_ms=latency_ms, ok=ok,
            connection=connection,
            detail=None if ok else "; ".join(warnings) or None,
            consecutive_failures=self._failures)
        snapshot = MarketRuntimeSnapshot(
            at=now, heartbeat=heartbeat, subscriptions=tuple(subscriptions),
            provider=provider, adapter_kind=adapter,
            refresh_sequence=self._sequence, refresh_duration_ms=latency_ms,
            warnings=tuple(dict.fromkeys(warnings)))
        self._last = snapshot
        return snapshot

    # ── individual reads, each independently guarded ─────────────────────────
    def _read_quote(self, symbol: str, warnings: list) -> SymbolQuote:
        try:
            raw = self._quote(symbol)
        except Exception as exc:                                # noqa: BLE001
            warnings.append(f"quote_read_failed:{symbol}")
            return SymbolQuote(symbol=symbol,
                               detail=f"read failed: {type(exc).__name__}")
        if not isinstance(raw, dict):
            return SymbolQuote(symbol=symbol, detail="no quote available")
        bid, ask = _as_float(raw.get("bid")), _as_float(raw.get("ask"))
        spread = _as_float(raw.get("spread"))
        if spread is None and bid is not None and ask is not None:
            # Derived ONCE, here, from measured evidence — never in the browser.
            spread = round(ask - bid, 8)
        return SymbolQuote(
            symbol=symbol, bid=bid, ask=ask,
            last=_as_float(raw.get("last") if "last" in raw else raw.get("close")),
            spread=spread,
            at=raw.get("at") or raw.get("timestamp") or raw.get("asOf"),
            provider=raw.get("provider"), detail=raw.get("detail"))

    def _read_candle(self, symbol: str, warnings: list) -> SymbolCandle:
        if self._candle is None:
            return SymbolCandle(symbol=symbol, timeframe=self._timeframe,
                                detail="no candle source configured")
        try:
            raw = self._candle(symbol)
        except Exception as exc:                                # noqa: BLE001
            warnings.append(f"candle_read_failed:{symbol}")
            return SymbolCandle(symbol=symbol, timeframe=self._timeframe,
                                detail=f"read failed: {type(exc).__name__}")
        if not isinstance(raw, dict):
            return SymbolCandle(symbol=symbol, timeframe=self._timeframe,
                                detail="no completed candle available")
        return SymbolCandle(
            symbol=symbol, timeframe=str(raw.get("timeframe") or self._timeframe),
            open=_as_float(raw.get("open")), high=_as_float(raw.get("high")),
            low=_as_float(raw.get("low")), close=_as_float(raw.get("close")),
            opened_at=raw.get("openedAt") or raw.get("time") or raw.get("at"),
            closed_at=raw.get("closedAt") or raw.get("closeTime"),
            provider=raw.get("provider"), detail=raw.get("detail"))


def _as_float(value: Any) -> float | None:
    """A number, or None. A non-numeric reading is ABSENT, never 0.0."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def candle_close_time(opened_at: str, timeframe: str) -> str | None:
    """The UTC close time of a bar that OPENED at `opened_at`.

    Used to decide whether a bar is complete. Kept here so exactly one rule
    converts an open time into a close time.
    """
    minutes = TIMEFRAME_MINUTES.get(str(timeframe).upper())
    start = _parse(opened_at)
    if minutes is None or start is None:
        return None
    from datetime import timedelta
    end = start + timedelta(minutes=minutes)
    return end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


TIMEFRAME_MINUTES = {
    "M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440,
}
