"""
Market Data Engine (Phase 11).

The single owner of all market snapshots. It produces IMMUTABLE `MarketSnapshot`
objects and hands them to consumers (the Strategy Engine consumes snapshots only;
the Scheduler decides WHEN to request them). It EXECUTES NOTHING — no strategies,
no commands, no overlay writes, no events.

`market_data.py` imports nothing from `server.py`. Providers receive read-only
callables (the fixture regime snapshot, replay sessions) injected by the runtime,
exactly like `strategy.py`/`scheduler.py`. OHLC is DERIVED from the existing
fixture regime `components` (ema / pxVsEma / bbwValue / adxValue) — the engine
does not duplicate the frontend synthetic-candle pipeline, and there is no
`md.bar.v1` OHLC series in the fixture (// FLAG: CONTRACT REQUIRED).
"""
from __future__ import annotations

import hashlib
import math
import os
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Callable


DEFAULT_TIMEFRAME = "M15"

# Timeframe → bar width in milliseconds (the candle series time axis).
_TIMEFRAME_MS = {"M1": 60_000, "M5": 300_000, "M15": 900_000, "M30": 1_800_000,
                 "H1": 3_600_000, "H4": 14_400_000, "D1": 86_400_000}

# Timeframe name → MetaTrader5 constant name (resolved lazily against the module so this
# file imports nothing Windows-only; the real values only exist once MetaTrader5 loads).
_MT5_TF_NAMES = {"M1": "TIMEFRAME_M1", "M5": "TIMEFRAME_M5", "M15": "TIMEFRAME_M15",
                 "M30": "TIMEFRAME_M30", "H1": "TIMEFRAME_H1", "H4": "TIMEFRAME_H4",
                 "D1": "TIMEFRAME_D1"}


# ---------------------------------------------------------------------------
# Deterministic helpers (no RNG — snapshots must be reproducible & resumable).
# ---------------------------------------------------------------------------

def _seed_float(*parts) -> float:
    """A stable float in [0, 1) from the given parts (deterministic, no Math.random)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:8], 16) / 0xFFFFFFFF


def _hour_of(iso: str) -> int:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).hour
    except Exception:  # pragma: no cover - defensive
        return 12


def _session_for(hour: int) -> str:
    """UTC-hour → trading session (coherent, deterministic; no calendar data)."""
    if hour < 7:
        return "Tokyo"
    if hour < 12:
        return "London"
    if hour < 17:
        return "NewYork"
    if hour < 21:
        return "NewYork"
    return "Sydney"


_LIQUIDITY = {  # session → (tier, score, base spread)
    "London": ("deep", 0.92, 0.00008),
    "NewYork": ("deep", 0.88, 0.00009),
    "Tokyo": ("normal", 0.64, 0.00013),
    "Sydney": ("thin", 0.41, 0.00021),
}


def _hash01(s: str) -> float:
    """FNV-1a 32-bit hash → [0, 1). Ported verbatim from the frontend `hash()` so
    the engine's candle series is visually identical to the retired frontend
    `synthCandles` — this is now the SINGLE OHLC pipeline (no duplicate generation)."""
    h = 2166136261
    for ch in s:
        h ^= ord(ch)
        h = (h * 16777619) & 0xFFFFFFFF
    return h / 0xFFFFFFFF


def _iso_to_ms(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)
    except Exception:  # pragma: no cover - defensive
        return 0


def synth_candles(symbol: str, count: int, end_iso: str, timeframe: str = DEFAULT_TIMEFRAME) -> list[dict]:
    """Deterministic OHLC series ending at `end_iso`, `count` bars back. The single
    canonical candle generator for the whole Control Tower — every provider produces
    its series through here. Deterministic per (symbol, bar index): no RNG, resumable,
    reproducible. Values match the old frontend generator (same FNV-1a walk)."""
    step_ms = _TIMEFRAME_MS.get(timeframe, _TIMEFRAME_MS[DEFAULT_TIMEFRAME])
    end_ms = _iso_to_ms(end_iso)
    start_ms = end_ms - count * step_ms
    seed0 = _hash01(f"{symbol}:chart:seed")
    price = 1.084 if symbol == "EURUSD" else 1.25
    bars: list[dict] = []
    for i in range(count):
        r = _hash01(f"{symbol}:bar:{i}")
        drift = (seed0 - 0.5) * 0.00006
        vol = 0.00025 + r * 0.00035
        direction = math.sin(i * 0.12) + (r - 0.5) * 1.5
        open_ = price
        close = open_ + drift + direction * vol
        high = max(open_, close) + r * 0.0002
        low = min(open_, close) - (1.0 - r) * 0.0002
        bars.append({
            "time": (start_ms + i * step_ms) // 1000,
            "open": round(open_, 5), "high": round(high, 5),
            "low": round(low, 5), "close": round(close, 5),
        })
        price = close
    return bars


# ---------------------------------------------------------------------------
# (Phase 23's bundled-CSV loader was retired in Phase 24: real candles now come
# from the Market Data Service via the injected candle_source on FixtureProvider.)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Immutable MarketSnapshot (Objective 2).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MarketSnapshot:
    snapshotId: str
    timestamp: str
    symbol: str
    timeframe: str
    ohlc: dict          # {open, high, low, close}
    spread: float
    session: str
    marketState: dict   # {state, confidence, confirmed, source} — strategy consumes this
    volatility: dict    # {value, threshold, regime}
    trend: dict         # {direction, strength, adx}
    liquidity: dict     # {tier, score}
    integrity: dict     # {ok, checks, issues}
    provider: str
    source: str
    asOf: str | None


# ---------------------------------------------------------------------------
# Snapshot construction — one place derives every field from a regime record.
# ---------------------------------------------------------------------------

def _build_snapshot(symbol: str, timeframe: str, regime: dict | None, now: str,
                    provider_id: str, source: str, bucket: str) -> MarketSnapshot:
    comp = (regime or {}).get("components", {}) if regime else {}
    state = (regime or {}).get("state")
    confidence = float((regime or {}).get("confidence") or 0.0)
    confirmed = bool((regime or {}).get("confirmed"))

    # --- OHLC derived from the regime engine's ema / price-vs-ema / band width ---
    ema = float(comp.get("ema") or (1.084 if symbol == "EURUSD" else 1.25))
    px_off = float(comp.get("pxVsEma") or 0.0)
    bbw = float(comp.get("bbwValue") or 0.006)
    bbw_threshold = float(comp.get("bbwThreshold") or 0.009)
    close = round(ema + px_off, 5)
    r = _seed_float(symbol, timeframe, bucket)
    rng = max(0.00010, bbw * 0.15)
    open_ = round(close - (r - 0.5) * rng, 5)
    high = round(max(open_, close) + r * rng * 0.5, 5)
    low = round(min(open_, close) - (1.0 - r) * rng * 0.5, 5)
    ohlc = {"open": open_, "high": high, "low": low, "close": close}

    # --- spread from session liquidity (+ deterministic jitter) ---
    session = _session_for(_hour_of(now))
    tier, liq_score, base_spread = _LIQUIDITY.get(session, ("normal", 0.6, 0.00013))
    spread = round(base_spread + _seed_float(symbol, bucket, "spread") * 0.00004, 6)

    # --- volatility / trend from band width + ADX + ema relation ---
    regime_vol = "expanding" if bbw >= bbw_threshold else "contracting"
    adx = float(comp.get("adxValue") or 0.0)
    ema_rel = comp.get("emaRelation")
    if ema_rel == "above" or (state or "").lower().startswith("bull"):
        direction = "up"
    elif ema_rel == "below" or (state or "").lower().startswith("bear"):
        direction = "down"
    else:
        direction = "range"
    trend = {"direction": direction, "strength": "strong" if adx >= 25 else "weak", "adx": adx}
    volatility = {"value": bbw, "threshold": bbw_threshold, "regime": regime_vol}
    liquidity = {"tier": tier, "score": liq_score}

    # --- integrity checks (all read-only; the engine never repairs) ---
    checks = {
        "hasRegime": regime is not None,
        "priceParsable": close > 0,
        "ohlcOrdered": high >= max(open_, close) and low <= min(open_, close),
        "spreadSane": 0 < spread < 0.01,
        "confidencePresent": confidence > 0,
    }
    issues = [k for k, ok in checks.items() if not ok]
    integrity = {"ok": not issues, "checks": checks, "issues": issues}

    sid = f"snap_{hashlib.sha256(f'{symbol}|{timeframe}|{bucket}|{provider_id}'.encode()).hexdigest()[:12]}"
    return MarketSnapshot(
        snapshotId=sid, timestamp=now, symbol=symbol, timeframe=timeframe, ohlc=ohlc,
        spread=spread, session=session,
        marketState={"state": state, "confidence": confidence, "confirmed": confirmed,
                     "source": (regime or {}).get("source")},
        volatility=volatility, trend=trend, liquidity=liquidity, integrity=integrity,
        provider=provider_id, source=source, asOf=(regime or {}).get("asOf"),
    )


# ---------------------------------------------------------------------------
# Provider interface + implementations (Objective 3).
# ---------------------------------------------------------------------------

class MarketDataProvider:
    provider_id = "provider"
    name = "Provider"
    live = False

    def snapshot(self, symbol: str, timeframe: str, now: str) -> MarketSnapshot:  # pragma: no cover
        raise NotImplementedError

    def candles(self, symbol: str, timeframe: str, count: int, end_iso: str,
                start_iso: str | None = None) -> list[dict]:
        """OHLC series for this provider. Base implementation is the deterministic
        synthetic generator (`start_iso` unsupported there — count-anchored only);
        the ONLY difference between live and replay is which provider is asked.
        Range-capable providers (Fixture→DataService) honour `start_iso`."""
        return synth_candles(symbol, count, end_iso, timeframe)

    def status(self) -> dict:
        return {"providerId": self.provider_id, "name": self.name, "live": self.live,
                "available": True, **self.feed_status()}

    def symbols(self) -> list[dict]:
        """Available symbols this provider serves. Local providers have no fixed
        universe → empty; a live feed (MT5) advertises its symbol list."""
        return []

    def live_tick(self, symbol: str) -> dict | None:
        """LIVE-5A: the GENUINE newest tick, or None when this provider has none.

        Deliberately separate from `quote()`. `quote()` may be derived from a
        fixture regime (the MT5 provider's is explicitly "PREVIEW ONLY"), which
        is fine for a preview but must never back a live trade. A provider that
        cannot read a real tick returns None, so the runtime reports the price as
        unavailable rather than showing a synthesized one as though it were live.
        """
        return None
    def feed_status(self) -> dict:
        """Provider-level feed status (Objective 4). Local synthetic providers report
        a `local` connection with no per-feed latency; MT5 overrides with real values."""
        return {"connection": "local", "symbolCount": None, "feedLatencyMs": None,
                "lastUpdate": None}


class FixtureProvider(MarketDataProvider):
    """The default active provider. Snapshots (market-state path) are derived from the
    frozen fixture regime; CANDLES come from the injected Market Data Service source
    (Phase 24 — real bars, real timestamps, range-capable), falling back to the
    deterministic synthetic series for symbols the service has no data for. The
    provider never knows where candles came from (Architecture V1.2 §4.2/§13)."""
    provider_id = "fixture"
    name = "Fixture"

    def __init__(self, fixture_regime: Callable[[str], dict | None],
                 candle_source: Callable[..., list] | None = None):
        self._regime = fixture_regime
        # candle_source(symbol, timeframe, count, end_iso, start_iso) -> list[Bar]
        self._candle_source = candle_source

    def candles(self, symbol: str, timeframe: str, count: int, end_iso: str,
                start_iso: str | None = None) -> list[dict]:
        if self._candle_source is not None:
            bars = self._candle_source(symbol, timeframe, count, end_iso, start_iso)
            if bars:
                return bars
        return synth_candles(symbol, count, end_iso, timeframe)

    def snapshot(self, symbol: str, timeframe: str, now: str) -> MarketSnapshot:
        regime = self._regime(symbol)
        # Deterministic bucket = the regime's own asOf, so the fixture snapshot is stable.
        bucket = (regime or {}).get("asOf") or "fixture"
        return _build_snapshot(symbol, timeframe, regime, now, self.provider_id, "fixture", bucket)


class ReplayProvider(MarketDataProvider):
    """Replay-clocked snapshots anchored to a fixture replay session's window.
    Reads replay sessions read-only; it does NOT touch the (frozen) Replay layer —
    it merely produces snapshots stamped at the replay window's end."""
    provider_id = "replay"
    name = "Replay"

    def __init__(self, fixture_regime: Callable[[str], dict | None],
                 replay_sessions: Callable[[str], dict | None],
                 candle_source: Callable[..., list] | None = None):
        self._regime = fixture_regime
        self._session = replay_sessions
        # Same real-data seam as FixtureProvider: candles come from the Market Data
        # Service anchored at the replay window end, so replay shows REAL bars rather
        # than the synthetic template. Falls back to synth only if no source/no data.
        self._candle_source = candle_source

    def candles(self, symbol: str, timeframe: str, count: int, end_iso: str,
                start_iso: str | None = None) -> list[dict]:
        if self._candle_source is not None:
            bars = self._candle_source(symbol, timeframe, count, end_iso, start_iso)
            if bars:
                return bars
        return synth_candles(symbol, count, end_iso, timeframe)

    def snapshot(self, symbol: str, timeframe: str, now: str) -> MarketSnapshot:
        regime = self._regime(symbol)
        sess = self._session(symbol) or {}
        window = sess.get("window", {})
        clock = window.get("end") or now  # replay clock, not wall clock
        bucket = f"replay|{clock}"
        return _build_snapshot(symbol, timeframe, regime, clock, self.provider_id, "replay", bucket)


class MockLiveProvider(MarketDataProvider):
    """A deterministic pseudo-live feed: perturbs the fixture baseline by a
    minute-seeded delta so the snapshot advances over time. No broker, no MT5,
    no socket — purely local and reproducible."""
    provider_id = "mock_live"
    name = "Mock Live"
    live = True

    def __init__(self, fixture_regime: Callable[[str], dict | None]):
        self._regime = fixture_regime

    def snapshot(self, symbol: str, timeframe: str, now: str) -> MarketSnapshot:
        base = self._regime(symbol)
        regime = dict(base) if base else None
        if regime is not None:
            comp = dict(regime.get("components", {}))
            minute = now[:16]  # bucket to the minute → advances ~every minute, stays deterministic
            drift = (_seed_float(symbol, minute, "live") - 0.5) * 0.0008
            comp["pxVsEma"] = round(float(comp.get("pxVsEma") or 0.0) + drift, 5)
            regime["components"] = comp
            regime["asOf"] = now
            bucket = f"live|{minute}"
        else:
            bucket = f"live|{now}"
        return _build_snapshot(symbol, timeframe, regime, now, self.provider_id, "mock_live", bucket)


class MT5MarketDataProvider(MarketDataProvider):
    """GENUINE MetaTrader 5 market-data feed (Phase 21). Read-only: it NEVER places
    trades and NEVER talks to the Broker abstraction (it imports nothing from
    broker.py). Historical OHLC comes directly from MT5 (`copy_rates_from_pos`);
    the current forming bar is refreshed from the latest bar + live tick every request
    (polling-on-request — no websocket, no background worker, no scheduler). An internal
    per-(symbol,timeframe) cache keeps completed bars and only re-fetches history when a
    new bar has formed, so refreshes don't request full history.

    The `MetaTrader5` package is Windows-only. Where it is unavailable (e.g. native
    macOS, no terminal, no credentials) the provider reports `connection=Disconnected` /
    `available=false` and returns NO bars — the interface is identical, so the same
    backend on Windows (with `MT5_LOGIN`/`MT5_PASSWORD`/`MT5_SERVER`/`MT5_PATH` or a
    running logged-in terminal) serves real data with no further code change.

    Disabled by default: `enabled=False` (Fixture stays active). Selection is by
    CONFIGURATION only (`MARKET_DATA_PROVIDER=mt5`); nothing switches automatically.

    `snapshot()`/`quote()` remain lightweight fixture-derived PREVIEW helpers (Phase 13,
    market-state path) so the rest of the engine keeps working; the live OHLC FEED is
    `candles()`, which is real-MT5-or-empty."""
    provider_id = "mt5"
    name = "MT5"
    live = True

    def __init__(self, fixture_regime: Callable[[str], dict | None],
                 available_symbols: list[str] | None = None,
                 aliases: dict[str, str] | None = None, enabled: bool = False):
        self._regime = fixture_regime  # preview snapshot/quote only — NOT the live feed
        self._universe = list(available_symbols or ["EURUSD", "GBPUSD", "XAUUSD"])
        self._aliases = dict(aliases or {})
        self.enabled = enabled
        self._mt5 = None              # the MetaTrader5 module once imported
        self._import_tried = False
        self._connected = False
        self._last_error: str | None = None
        self._last_update: str | None = None
        self._last_feed_ms: float | None = None
        self._cache: dict[tuple, dict] = {}  # (symbol,tf) -> {barStart, count, completed}
        self._history_fetches = 0
        self._cache_hits = 0

    def to_broker_symbol(self, canonical: str) -> str:
        return self._aliases.get(canonical, canonical)

    # --- real MT5 session (lazy import + env-configurable initialize) ---
    def _module(self):
        if not self._import_tried:
            self._import_tried = True
            try:
                import MetaTrader5 as _m  # Windows-only wheel; absent on macOS/native Python
                self._mt5 = _m
            except Exception as exc:  # ImportError on non-Windows, etc.
                self._mt5 = None
                self._last_error = f"MetaTrader5 unavailable: {exc.__class__.__name__}"
        return self._mt5

    def _connect(self) -> bool:
        if not self.enabled:
            return False
        if self._connected:
            return True
        m = self._module()
        if m is None:
            return False
        try:
            kwargs: dict = {}
            if os.environ.get("MT5_PATH"):
                kwargs["path"] = os.environ["MT5_PATH"]
            if os.environ.get("MT5_LOGIN"):
                kwargs.update(login=int(os.environ["MT5_LOGIN"]),
                              password=os.environ.get("MT5_PASSWORD", ""),
                              server=os.environ.get("MT5_SERVER", ""))
            ok = bool(m.initialize(**kwargs))
            self._connected = ok
            if not ok:
                self._last_error = f"MT5 initialize failed: {m.last_error()}"
        except Exception as exc:  # pragma: no cover - only on a real MT5 host
            self._connected = False
            self._last_error = f"MT5 initialize error: {exc.__class__.__name__}"
        return self._connected

    def _timeframe(self, timeframe: str):
        name = _MT5_TF_NAMES.get(timeframe)
        return getattr(self._mt5, name, None) if (self._mt5 and name) else None

    @staticmethod
    def _bar(r) -> dict:
        # r is a MetaTrader5 numpy structured record (or dict-like) with named fields.
        return {"time": int(r["time"]), "open": round(float(r["open"]), 5),
                "high": round(float(r["high"]), 5), "low": round(float(r["low"]), 5),
                "close": round(float(r["close"]), 5)}

    def candles(self, symbol: str, timeframe: str, count: int, end_iso: str,
                start_iso: str | None = None) -> list[dict]:
        """Genuine MT5 OHLC series. Completed bars via `copy_rates_from_pos` (cached per
        (symbol,timeframe); only re-fetched when a new bar has formed), plus the current
        forming bar (position 0) refreshed from the latest bar + live tick every request.
        `start_iso` ranges are not implemented for MT5 yet (count-anchored only).
        Returns [] when MT5 is unavailable — the engine simply has no MT5 data here."""
        if not self._connect():
            return []
        m = self._mt5
        tf = self._timeframe(timeframe)
        if tf is None:
            return []
        t0 = time.perf_counter()
        bsym = self.to_broker_symbol(symbol)
        count = max(1, int(count))
        try:
            m.symbol_select(bsym, True)  # ensure the symbol is in Market Watch
            step = (_TIMEFRAME_MS.get(timeframe, _TIMEFRAME_MS[DEFAULT_TIMEFRAME])) // 1000
            bar_start = int(time.time()) // step * step
            key = (symbol, timeframe)
            ent = self._cache.get(key)
            if ent and ent["barStart"] == bar_start and ent["count"] >= count:
                completed = ent["completed"][-(count - 1):] if count > 1 else []
                self._cache_hits += 1
            else:
                # completed bars = positions 1..count-1 (skip the forming bar at pos 0)
                rates = m.copy_rates_from_pos(bsym, tf, 1, count - 1) if count > 1 else []
                completed = [self._bar(r) for r in (rates if rates is not None else [])]
                self._cache[key] = {"barStart": bar_start, "count": count, "completed": completed}
                self._history_fetches += 1
            bars = list(completed)
            cur = m.copy_rates_from_pos(bsym, tf, 0, 1)  # the current forming bar
            if cur is not None and len(cur):
                forming = self._bar(cur[0])
                tick = m.symbol_info_tick(bsym)  # live price → continuously-updating close
                bid, ask = getattr(tick, "bid", 0.0), getattr(tick, "ask", 0.0)
                if tick is not None and bid and ask:
                    mid = round((bid + ask) / 2.0, 5)
                    forming["close"] = mid
                    forming["high"] = round(max(forming["high"], mid), 5)
                    forming["low"] = round(min(forming["low"], mid), 5)
                bars.append(forming)
            self._last_feed_ms = round((time.perf_counter() - t0) * 1000, 3)
            self._last_update = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            return bars
        except Exception as exc:  # pragma: no cover - only on a real MT5 host
            self._last_error = f"MT5 copy_rates error: {exc.__class__.__name__}"
            return []

    def snapshot(self, symbol: str, timeframe: str, now: str) -> MarketSnapshot:
        # PREVIEW ONLY (fixture-derived) — keeps the market-state path alive; NOT the feed.
        t0 = time.perf_counter()
        regime = self._regime(symbol)
        snap = _build_snapshot(symbol, timeframe, regime, now, self.provider_id, "mt5", f"mt5|{now[:16]}")
        self._last_feed_ms = round((time.perf_counter() - t0) * 1000, 3)
        return snap

    def quote(self, symbol: str, timeframe: str, now: str) -> dict:
        # PREVIEW ONLY (fixture-derived bid/ask); connection reflects the real MT5 session.
        regime = self._regime(symbol)
        snap = _build_snapshot(symbol, timeframe, regime, now, self.provider_id, "mt5", f"mt5|{now[:16]}")
        mid, spread = snap.ohlc["close"], snap.spread
        return {
            "symbol": symbol, "brokerSymbol": self.to_broker_symbol(symbol),
            "bid": round(mid - spread / 2, 5), "ask": round(mid + spread / 2, 5),
            "spread": spread, "timeframe": timeframe, "timestamp": now,
            "connection": self._connection(),
        }

    def live_tick(self, symbol: str) -> dict | None:
        """A REAL MT5 tick via `symbol_info_tick` — the same call the live
        gateway uses. Returns None on any failure, so an unavailable price is
        reported as unavailable rather than substituted."""
        if not self._connect():
            return None
        m = self._module()
        if m is None:
            return None
        bsym = self.to_broker_symbol(symbol)
        try:
            m.symbol_select(bsym, True)          # ensure it is in Market Watch
            tick = m.symbol_info_tick(bsym)
        except Exception as exc:  # pragma: no cover - only on a real MT5 host
            self._last_error = f"symbol_info_tick failed: {exc.__class__.__name__}"
            return None
        if tick is None:
            return None
        bid = getattr(tick, "bid", None)
        ask = getattr(tick, "ask", None)
        if not bid or not ask:
            # MT5 reports 0.0 for "no quote". Zero is NOT a price.
            return None
        epoch = getattr(tick, "time", None)
        at = None
        if epoch:
            from datetime import datetime as _dt, timezone as _tz
            # MT5 tick time is a UTC epoch; normalized here so nothing
            # downstream ever performs local-time arithmetic.
            at = _dt.fromtimestamp(int(epoch), _tz.utc).isoformat().replace("+00:00", "Z")
        return {
            "symbol": symbol, "brokerSymbol": bsym,
            "bid": float(bid), "ask": float(ask),
            "last": float(getattr(tick, "last", None) or 0) or None,
            "spread": round(float(ask) - float(bid), 8),
            "at": at, "provider": self.provider_id, "source": "live_mt5",
        }

    def symbols(self) -> list[dict]:
        return [{"symbol": s, "brokerSymbol": self.to_broker_symbol(s)} for s in self._universe]

    def _connection(self) -> str:
        if not self.enabled:
            return "Disconnected"
        return "Connected" if self._connect() else "Disconnected"

    def feed_status(self) -> dict:
        return {"connection": self._connection(), "symbolCount": len(self._universe),
                "feedLatencyMs": self._last_feed_ms, "lastUpdate": self._last_update,
                "cacheHits": self._cache_hits, "historyFetches": self._history_fetches,
                "timeframes": list(_MT5_TF_NAMES.keys()), "note": self._last_error}

    def status(self) -> dict:
        return {"providerId": self.provider_id, "name": self.name, "live": True,
                "available": bool(self.enabled and self._connect()), "enabled": self.enabled,
                **self.feed_status()}


# ---------------------------------------------------------------------------
# Metrics (Objective 5).
# ---------------------------------------------------------------------------

@dataclass
class MarketDataMetrics:
    produced: int = 0
    total_ms: float = 0.0
    last_ms: float = 0.0
    last_snapshot_at: str | None = None

    def record(self, duration_ms: float, now: str) -> None:
        self.produced += 1
        self.total_ms += duration_ms
        self.last_ms = duration_ms
        self.last_snapshot_at = now


# ---------------------------------------------------------------------------
# Market Data Engine (Objectives 1, 4, 5). Produces immutable snapshots; owns
# every snapshot; executes nothing.
# ---------------------------------------------------------------------------

class MarketDataEngine:
    def __init__(self, now_fn: Callable[[], str]):
        self._now = now_fn
        self._providers: dict[str, MarketDataProvider] = {}
        self._active: str | None = None
        self._current: dict[tuple[str, str], MarketSnapshot] = {}
        self._history: list[MarketSnapshot] = []
        self._metrics = MarketDataMetrics()

    # --- provider registry ---
    def register(self, provider: MarketDataProvider, active: bool = False) -> None:
        self._providers[provider.provider_id] = provider
        if active or self._active is None:
            self._active = provider.provider_id

    def set_active(self, provider_id: str) -> bool:
        if provider_id in self._providers:
            self._active = provider_id
            return True
        return False

    def active_provider(self) -> MarketDataProvider | None:
        return self._providers.get(self._active or "")

    # --- snapshot production (the single ownership point) ---
    def snapshot(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> MarketSnapshot:
        provider = self.active_provider()
        if provider is None:  # pragma: no cover - defensive
            raise RuntimeError("no active market-data provider")
        now = self._now()
        t0 = time.perf_counter()
        snap = provider.snapshot(symbol, timeframe, now)
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        self._metrics.record(duration_ms, now)
        self._current[(symbol, timeframe)] = snap
        self._history.insert(0, snap)
        self._history = self._history[:50]
        return snap

    # --- reads (all read-only) ---
    def current(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> MarketSnapshot | None:
        return self._current.get((symbol, timeframe))

    def market_state_view(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> dict | None:
        """The strategy's market input — the CURRENT snapshot's marketState projection.
        Strategies consume this (never runtime state directly)."""
        snap = self.current(symbol, timeframe)
        return dict(snap.marketState) if snap else None

    def history(self, limit: int = 25) -> list[dict]:
        return [asdict(s) for s in self._history[:limit]]

    def live_tick(self, symbol: str) -> dict | None:
        """LIVE-5A: the active provider's genuine tick, or None."""
        provider = self.active_provider()
        if provider is None:
            return None
        try:
            return provider.live_tick(symbol)
        except Exception:
            return None

    def snapshot_dict(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> dict | None:
        snap = self.current(symbol, timeframe)
        return asdict(snap) if snap else None

    def provider_status(self) -> dict:
        return {
            "active": self._active,
            "providers": [p.status() for p in self._providers.values()],
        }

    def provider(self, provider_id: str | None = None) -> MarketDataProvider | None:
        """Look up a provider by id (defaults to the active one)."""
        return self._providers.get(provider_id) if provider_id else self.active_provider()

    def candles(self, symbol: str, timeframe: str = DEFAULT_TIMEFRAME, count: int = 220,
                end_iso: str | None = None, provider_id: str | None = None,
                start_iso: str | None = None) -> list[dict]:
        """The single candle-series entry point. Delegates to a named provider (replay)
        or the active provider (live) — switching the active provider automatically
        changes live candles with no chart change. `start_iso` makes it a RANGE query
        (Phase 24 — historical scrolling). Read-only; produces no side effects."""
        p = self.provider(provider_id)
        if p is None:
            return []
        return p.candles(symbol, timeframe, count, end_iso or self._now(), start_iso)

    def symbols(self, provider_id: str | None = None) -> list[dict]:
        p = self.provider(provider_id)
        return p.symbols() if p else []

    def preview(self, provider_id: str, symbol: str, timeframe: str = DEFAULT_TIMEFRAME) -> dict | None:
        """READ-ONLY: render a snapshot from a NAMED provider without changing the
        active provider or touching engine state (history/metrics/current). Lets an
        operator inspect e.g. the MT5 feed while Fixture stays active."""
        p = self._providers.get(provider_id)
        if p is None:
            return None
        return asdict(p.snapshot(symbol, timeframe, self._now()))

    def health(self) -> dict:
        last = self._history[0] if self._history else None
        now = self._now()
        age_ms = None
        if last is not None:
            try:
                t_last = datetime.fromisoformat(last.timestamp.replace("Z", "+00:00"))
                t_now = datetime.fromisoformat(now.replace("Z", "+00:00"))
                age_ms = round((t_now - t_last).total_seconds() * 1000.0, 1)
            except Exception:  # pragma: no cover - defensive
                age_ms = None
        avg = round(self._metrics.total_ms / self._metrics.produced, 3) if self._metrics.produced else 0.0
        provider = self.active_provider()
        fs = provider.feed_status() if provider else {}
        return {
            "marketDataHealthy": True,
            "provider": self._active,
            "snapshotsProduced": self._metrics.produced,
            "snapshotAgeMs": age_ms,
            "updateLatencyMs": self._metrics.last_ms,
            "averageLatencyMs": avg,
            "lastSnapshotAt": self._metrics.last_snapshot_at,
            "dataIntegrity": (last.integrity.get("ok") if last else None),
            "currentSymbol": (last.symbol if last else None),
            "currentTimeframe": (last.timeframe if last else None),
            # Provider feed status (Phase 13, Objective 4) — from the active provider.
            "connection": fs.get("connection"),
            "symbolCount": fs.get("symbolCount"),
            "feedLatencyMs": fs.get("feedLatencyMs"),
            "lastUpdate": fs.get("lastUpdate") or self._metrics.last_snapshot_at,
        }
