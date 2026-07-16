"""
Market Data Service (Phase 24) — Architecture V1.2 §4.2.

Provider adapters behind ONE contract, an OWNED historical store, gap detection,
timeframe aggregation, and caching. Everything else consumes `DataService` only —
nothing outside this module knows where candles came from (V1.2 §13.1 rule 2:
concretes depend on contracts; only this module's registry names the adapters).

Layering (V1.2 §13.1): this is a *driver/infrastructure* module — I/O is allowed
here and only here. It imports nothing from the engine/UI layers.

  DataService
    ├─ PolygonAdapter        (genuine Polygon.io REST; requires POLYGON_API_KEY;
    │                         degrades to unavailable without it — never fakes)
    └─ HistoricalStore       (owned store: real Dukascopy M1 CSVs, M1 canonical,
                              higher timeframes aggregated M1→M5/M15/H1/H4/D1)

Timeframes: M1 M5 M15 H1 H4 D1. M1 is the canonical source (locked decision);
the Polygon adapter MAY use native higher-timeframe aggregates (the abstraction
lets the implementation choose without affecting consumers — Phase 24 req #4).

Bars are REAL: real timestamps (unix seconds, UTC), real OHLC, real volume.
Range queries (`start`/`end`) are first-class — consumers ask for ranges, not
"latest N" (though count-anchored queries are supported for compatibility).
"""
from __future__ import annotations

import bisect
import json
import logging
import os
import ssl
import time
import urllib.request
import urllib.error
from datetime import datetime, timezone


def _https_context() -> ssl.SSLContext:
    """TLS context with an explicit CA bundle where available. Some Python installs
    (notably python.org macOS builds) ship without OS trust-store wiring, which makes
    every HTTPS call fail CERTIFICATE_VERIFY_FAILED; certifi fixes that portably."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:  # pragma: no cover - certifi absent → system default
        return ssl.create_default_context()


_SSL_CTX = _https_context()

_log = logging.getLogger("data_service")

TIMEFRAME_S = {"M1": 60, "M5": 300, "M15": 900, "H1": 3600, "H4": 14400, "D1": 86400}
DEFAULT_TF = "M15"

Bar = dict  # {"time": int, "open": f, "high": f, "low": f, "close": f, "volume": f}


def _iso_to_unix(iso: str) -> int:
    try:
        return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Provider contract (V1.2 §11.1: subscribe, history, backfill). `history` is a
# RANGE query; `latest` covers the live/polling path. Adapters implement this
# and nothing else in the platform may import them by name.
# ---------------------------------------------------------------------------

class MarketDataProviderPort:
    provider_id = "port"

    def available(self) -> bool:  # pragma: no cover - interface
        return False

    def history(self, symbol: str, timeframe: str, start_s: int, end_s: int) -> list[Bar]:
        raise NotImplementedError  # pragma: no cover

    def latest(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        raise NotImplementedError  # pragma: no cover

    def history_before(self, symbol: str, timeframe: str, end_s: int, count: int) -> list[Bar]:
        """The last `count` bars at-or-before `end_s`. Default: progressively widening
        range queries (remote adapters); stores override with an exact index lookup.
        Widening has absolute floors (3d, 30d) so small counts still bridge weekends
        and end-of-day feed plans that lag the live edge."""
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        levels = (count + 20,
                  max(count * 8, (3 * 86400) // step),
                  max(count * 64, (30 * 86400) // step))
        bars: list[Bar] = []
        for widen in levels:
            bars = self.history(symbol, timeframe, end_s - step * widen, end_s)
            if len(bars) >= count:
                return bars[-count:]
        return bars[-count:] if bars else []

    def status(self) -> dict:  # pragma: no cover - interface
        return {"providerId": self.provider_id, "available": self.available()}


# ---------------------------------------------------------------------------
# Owned historical store — M1 canonical, aggregation, gap detection, caching.
# Data dir: MARKET_DATA_DIR env (e.g. the full Lux master) or backend/data/history
# (bundled real Dukascopy slice). File convention: {SYMBOL}_M1.csv
# (time,open,high,low,close,volume — chronological).
# ---------------------------------------------------------------------------

class HistoricalStore(MarketDataProviderPort):
    provider_id = "store"

    def __init__(self, data_dir: str | None = None):
        self._dir = data_dir or os.environ.get("MARKET_DATA_DIR") or os.path.join(
            os.path.dirname(__file__), "data", "history")
        self._m1: dict[str, list[Bar]] = {}      # symbol -> chronological M1 bars
        self._times: dict[str, list[int]] = {}   # symbol -> bar times (for bisect)
        self._agg: dict[tuple, list[Bar]] = {}   # (symbol, tf) -> aggregated series
        self._gaps: dict[str, dict] = {}

    # --- loading & caching ---
    def _load(self, symbol: str) -> list[Bar]:
        if symbol in self._m1:
            return self._m1[symbol]
        path = os.path.join(self._dir, f"{symbol}_M1.csv")
        bars: list[Bar] = []
        if os.path.exists(path):
            with open(path) as fh:
                next(fh, None)
                for line in fh:
                    p = line.rstrip("\n").split(",")
                    if len(p) >= 5:
                        try:
                            bars.append({
                                "time": _iso_to_unix(p[0]),
                                "open": float(p[1]), "high": float(p[2]),
                                "low": float(p[3]), "close": float(p[4]),
                                "volume": float(p[5]) if len(p) > 5 and p[5] else 0.0,
                            })
                        except ValueError:
                            continue
        self._m1[symbol] = bars
        self._times[symbol] = [b["time"] for b in bars]
        self._gaps[symbol] = self._detect_gaps(bars)
        return bars

    @staticmethod
    def _detect_gaps(bars: list[Bar]) -> dict:
        """Gap detection over the M1 series. FX closes over the weekend, so runs
        that span a Saturday are classified separately from true data gaps."""
        gaps = 0
        weekend = 0
        largest = 0
        for i in range(1, len(bars)):
            delta = bars[i]["time"] - bars[i - 1]["time"]
            if delta <= 60:
                continue
            # weekend if the missing span contains any Saturday hour
            t0 = bars[i - 1]["time"]
            is_weekend = any(
                datetime.fromtimestamp(t0 + k * 3600, tz=timezone.utc).weekday() == 5
                for k in range(0, min(int(delta // 3600) + 1, 72), 6)
            )
            if is_weekend:
                weekend += 1
            else:
                gaps += 1
                largest = max(largest, delta)
        return {"dataGaps": gaps, "weekendClosures": weekend, "largestGapS": largest}

    # --- aggregation (M1 canonical → higher timeframes; V1.2 locked decision) ---
    def _series(self, symbol: str, timeframe: str) -> list[Bar]:
        if timeframe == "M1":
            return self._load(symbol)
        key = (symbol, timeframe)
        if key in self._agg:
            return self._agg[key]
        m1 = self._load(symbol)
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        out: list[Bar] = []
        cur: Bar | None = None
        for b in m1:
            bucket = b["time"] - (b["time"] % step)
            if cur is None or cur["time"] != bucket:
                if cur is not None:
                    out.append(cur)
                cur = {"time": bucket, "open": b["open"], "high": b["high"],
                       "low": b["low"], "close": b["close"], "volume": b["volume"]}
            else:
                cur["high"] = max(cur["high"], b["high"])
                cur["low"] = min(cur["low"], b["low"])
                cur["close"] = b["close"]
                cur["volume"] += b["volume"]
        if cur is not None:
            out.append(cur)
        self._agg[key] = out
        return out

    # --- port implementation ---
    def available(self) -> bool:
        return True

    def history(self, symbol: str, timeframe: str, start_s: int, end_s: int) -> list[Bar]:
        series = self._series(symbol, timeframe)
        times = [b["time"] for b in series]
        lo = bisect.bisect_left(times, start_s)
        hi = bisect.bisect_right(times, end_s)
        return series[lo:hi]

    def latest(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        series = self._series(symbol, timeframe)
        return series[-count:]

    def history_before(self, symbol: str, timeframe: str, end_s: int, count: int) -> list[Bar]:
        # Exact: index of the last bar at-or-before end_s, then count bars back.
        series = self._series(symbol, timeframe)
        times = [b["time"] for b in series]
        hi = bisect.bisect_right(times, end_s)
        return series[max(0, hi - count):hi]

    def coverage(self, symbol: str) -> dict | None:
        m1 = self._load(symbol)
        if not m1:
            return None
        return {"firstBar": m1[0]["time"], "lastBar": m1[-1]["time"], "m1Bars": len(m1),
                **self._gaps.get(symbol, {})}

    def symbols(self) -> list[str]:
        try:
            return sorted(f[:-7] for f in os.listdir(self._dir) if f.endswith("_M1.csv"))
        except OSError:
            return []

    def status(self) -> dict:
        return {"providerId": self.provider_id, "available": True, "dataDir": self._dir,
                "symbols": {s: self.coverage(s) for s in self.symbols()}}


# ---------------------------------------------------------------------------
# Polygon.io adapter — GENUINE REST integration (no simulation). Requires
# POLYGON_API_KEY; without it, `available()` is False and the service falls
# back to the historical store. Uses Polygon's NATIVE aggregates per timeframe
# (permitted by the abstraction; consumers never see the difference).
# ---------------------------------------------------------------------------

_POLY_SPAN = {"M1": (1, "minute"), "M5": (5, "minute"), "M15": (15, "minute"),
              "H1": (1, "hour"), "H4": (4, "hour"), "D1": (1, "day")}


class PolygonAdapter(MarketDataProviderPort):
    provider_id = "polygon"

    def __init__(self, api_key: str | None = None, timeout_s: float = 6.0):
        self._key = api_key if api_key is not None else os.environ.get("POLYGON_API_KEY", "")
        self._timeout = timeout_s
        self._last_error: str | None = None
        self._cache: dict[tuple, tuple[float, list[Bar]]] = {}  # key -> (fetched_at, bars)
        self.last_from_cache = False  # diagnostics: did the last history() hit the cache?

    def available(self) -> bool:
        return bool(self._key)

    def _fetch(self, symbol: str, timeframe: str, start_s: int, end_s: int) -> list[Bar]:
        mult, span = _POLY_SPAN.get(timeframe, _POLY_SPAN[DEFAULT_TF])
        # Floor the window to timeframe boundaries: Polygon anchors multi-minute
        # aggregates to the requested `from`, so an unaligned start yields :28/:43-style
        # buckets that would never line up with the store's :00/:15/:30/:45 series.
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        start_s -= start_s % step
        end_s -= end_s % step
        url = (f"https://api.polygon.io/v2/aggs/ticker/C:{symbol}/range/{mult}/{span}/"
               f"{start_s * 1000}/{(end_s + step - 1) * 1000}?adjusted=true&sort=asc&limit=50000"
               f"&apiKey={self._key}")
        req = urllib.request.Request(url, headers={"User-Agent": "control-tower/1.0"})
        with urllib.request.urlopen(req, timeout=self._timeout, context=_SSL_CTX) as resp:
            payload = json.loads(resp.read().decode())
        return [{"time": int(r["t"] // 1000), "open": float(r["o"]), "high": float(r["h"]),
                 "low": float(r["l"]), "close": float(r["c"]), "volume": float(r.get("v", 0))}
                for r in payload.get("results", []) or []]

    def history(self, symbol: str, timeframe: str, start_s: int, end_s: int) -> list[Bar]:
        if not self.available():
            return []
        # Floor the window to timeframe boundaries BEFORE the cache lookup — the raw
        # bounds carry a per-call `now`, which made every key unique and the cache
        # useless (the root cause of the free-tier 429 storms). Floored keys are
        # stable within a bar, so the live-edge TTL actually deduplicates polls.
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        start_s -= start_s % step
        end_s -= end_s % step
        live_edge = end_s >= int(time.time()) - step
        # Timeframe-aware live-edge TTL: the forming bar only needs refreshing on the
        # order of the bar interval, so cache it ~half a bar (min 20s, max 5min). This
        # keeps the free tier's 5-req/min budget from being spent on 15s polls — the
        # frontend polls 4x/min but most polls are served from cache, leaving budget
        # for timeframe switches. Under-budget requests => far fewer 429 fallbacks.
        live_ttl = min(max(step * 0.5, 20.0), 300.0)
        key = (symbol, timeframe, start_s, end_s)
        hit = self._cache.get(key)
        if hit and (not live_edge or time.time() - hit[0] < live_ttl):
            self.last_from_cache = True
            return hit[1]
        self.last_from_cache = False
        try:
            bars = self._fetch(symbol, timeframe, start_s, end_s)
            self._last_error = None
            self._cache[key] = (time.time(), bars)
            return bars
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
            self._last_error = f"polygon: {exc.__class__.__name__}"
            return []

    def history_before(self, symbol: str, timeframe: str, end_s: int, count: int) -> list[Bar]:
        # Widen ONLY to gather more bars, NEVER at the cost of recency. Polygon's free
        # tier truncates large windows to an OLDER slice: an 8-40 day H1 request reaches
        # the live edge (07-15), but a 60-day request returns data ending ~2 weeks stale
        # (07-03). Weekend gaps mean the narrow window often has < count bars, which the
        # old code "fixed" by widening — silently regressing the chart to stale bars.
        # Try narrow→wide and keep the result whose LAST bar is most recent (ties broken
        # by bar count). Fewer-but-recent beats more-but-stale on a live chart.
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        best: list[Bar] = []
        for widen in (max(count + 20, (3 * 86400) // step),
                      max(count * 8, (30 * 86400) // step)):
            bars = self.history(symbol, timeframe, end_s - step * widen, end_s)
            if bars and (not best
                         or bars[-1]["time"] > best[-1]["time"]
                         or (bars[-1]["time"] == best[-1]["time"] and len(bars) > len(best))):
                best = bars
            if len(best) >= count:
                break
        return best[-count:] if best else []

    def latest(self, symbol: str, timeframe: str, count: int) -> list[Bar]:
        step = TIMEFRAME_S.get(timeframe, 900)
        now = int(time.time())
        return self.history(symbol, timeframe, now - step * (count + 10), now)[-count:]

    def status(self) -> dict:
        return {"providerId": self.provider_id, "available": self.available(),
                "configured": bool(self._key), "note": self._last_error or
                (None if self._key else "POLYGON_API_KEY not set")}


# ---------------------------------------------------------------------------
# The service facade — the ONLY thing the rest of the platform consumes.
# Provider preference: Polygon when configured (live-capable), historical store
# otherwise. Selection is config (env), never automatic mid-flight switching.
# ---------------------------------------------------------------------------

class DataService:
    _req_seq = 0

    def __init__(self, store: HistoricalStore | None = None,
                 polygon: PolygonAdapter | None = None):
        self.last_query: dict = {}  # diagnostics for the most recent candles() call
        # Last successful LIVE-edge Polygon window per (symbol, timeframe). When a
        # later live fetch is rate-limited (429 → empty), we serve this instead of
        # the month-old store — a live chart must never regress to ancient bars.
        self._last_good_live: dict[tuple, list[Bar]] = {}
        self.store = store or HistoricalStore()
        self.polygon = polygon or PolygonAdapter()
        preferred = (os.environ.get("MARKET_DATA_SOURCE") or "auto").lower()
        if preferred == "polygon":
            self._order = [self.polygon, self.store]
        elif preferred == "store":
            self._order = [self.store]
        else:  # auto: polygon when genuinely configured, else store
            self._order = ([self.polygon, self.store] if self.polygon.available()
                           else [self.store])

    def _first(self) -> MarketDataProviderPort:
        for p in self._order:
            if p.available():
                return p
        return self.store

    def candles(self, symbol: str, timeframe: str = DEFAULT_TF, count: int = 220,
                start_s: int | None = None, end_s: int | None = None) -> list[Bar]:
        """Range-first query. start+end → that range; end only → `count` bars
        ending at/before end; neither → the latest `count` bars (live edge).
        If the primary provider returns nothing (e.g. Polygon rate-limited), the
        historical store answers instead — degradation stays on REAL data."""
        timeframe = timeframe if timeframe in TIMEFRAME_S else DEFAULT_TF
        count = max(1, min(5000, count))

        def query(p: MarketDataProviderPort) -> list[Bar]:
            if start_s is not None:
                return p.history(symbol, timeframe, start_s, end_s or int(time.time()))
            if end_s is not None:
                return p.history_before(symbol, timeframe, end_s, count)
            return p.latest(symbol, timeframe, count)

        DataService._req_seq += 1
        req_id = f"md-{DataService._req_seq}"
        # Live-edge = no explicit start and an end at (or after) ~now. The engine
        # substitutes now for a live request's end (market_data.py: `end_iso or now`),
        # so end_s is never None here — detect "ends at now" instead. Historical
        # back-scroll (start given, or end far in the past) is NOT live and keeps the
        # normal store fallback.
        step = TIMEFRAME_S.get(timeframe, TIMEFRAME_S[DEFAULT_TF])
        is_live = start_s is None and (end_s is None or end_s >= int(time.time()) - step)
        lg_key = (symbol, timeframe)
        primary = self._first()
        bars = query(primary)
        source = primary.provider_id
        fell_back = False
        stale_live = False
        if bars and primary is self.polygon and is_live:
            # Record the freshest good live window so a later 429 can reuse it.
            self._last_good_live[lg_key] = bars
        if not bars and primary is not self.store:
            if is_live and self._last_good_live.get(lg_key):
                # Live edge, Polygon empty (rate-limited): serve the last good
                # Polygon window rather than regressing to the month-old store.
                # Trim to the requested count (the cached window may be larger).
                bars = self._last_good_live[lg_key][-count:]
                source = self.polygon.provider_id
                stale_live = True
            else:
                # Polygon unavailable and no last-good yet: the bundled store is the
                # final fallback so the endpoint ALWAYS serves a series (invariant the
                # UI + tests rely on). This is the only path that can still show older
                # bars on the live edge; the timeframe-aware cache above keeps Polygon
                # under its budget so this cold-429 case is rare and self-heals on the
                # next poll (which serves fresh Polygon or the last-good window).
                bars = query(self.store)
                source = self.store.provider_id
                fell_back = True
        cache_hit = bool(self.polygon.last_from_cache) if source == "polygon" and not stale_live else False
        self.last_query = {
            "requestId": req_id, "timeframe": timeframe, "source": source,
            "fellBack": fell_back, "staleLive": stale_live, "cacheHit": cache_hit, "count": len(bars),
            "first": bars[0]["time"] if bars else None,
            "last": bars[-1]["time"] if bars else None,
        }
        _log.info(
            "candles %s tf=%s provider=%s%s cache=%s count=%d first=%s last=%s%s",
            req_id, timeframe, source,
            " (FELL BACK)" if fell_back else (" (STALE-LIVE cache)" if stale_live else ""),
            cache_hit, len(bars),
            datetime.fromtimestamp(bars[0]["time"], tz=timezone.utc).isoformat() if bars else "-",
            datetime.fromtimestamp(bars[-1]["time"], tz=timezone.utc).isoformat() if bars else "-",
            f" polyErr={self.polygon._last_error}" if fell_back else "",
        )
        return bars

    def m1_window(self, symbol: str, start_s: int, end_s: int) -> list[Bar]:
        """M1 magnifier support: the canonical M1 bars inside a window."""
        primary = self._first()
        bars = primary.history(symbol, "M1", start_s, end_s)
        if not bars and primary is not self.store:
            bars = self.store.history(symbol, "M1", start_s, end_s)
        return bars

    def status(self) -> dict:
        active = self._first()
        return {
            "active": active.provider_id,
            "timeframes": list(TIMEFRAME_S.keys()),
            "providers": [self.polygon.status(), self.store.status()],
        }
