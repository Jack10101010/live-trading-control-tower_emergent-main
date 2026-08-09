"""M-LIVE-NEWS-1 — a CURRENT economic calendar, and proof that it is current.

The defect this replaces: the live node's only news source was a static CSV
(`data/news/master_economic_calendar_2020_present.csv`) whose last event was
2026-05-21. There was no fetch anywhere in the live path, so from late May
onward the node computed "no blackout" for every bar — not because no event was
due, but because it had never heard of any. It would have traded straight
through NFP, CPI and every ECB decision while reporting healthy. Silence read
as safety, which is the one thing a protection mechanism must never do.

Two separable jobs live here, and the second is the important one:

  1. KEEP THE CALENDAR CURRENT — fetch upcoming events from a live source and
     cache them locally in a deterministic representation the node consumes.
  2. PROVE THE CALENDAR IS CURRENT — publish a health verdict that a rail can
     refuse on. Any answer other than "provably covered and provably fresh" is
     a refusal, so a dead feed BLOCKS new exposure instead of silently removing
     protection.

SOURCE. Forex Factory's published weekly JSON
(`https://nfs.faireconomy.media/ff_calendar_thisweek.json`) — the same
publisher that produced the historical archive (`source=forexfactory` in every
CSV row), so live and historical rows are the same events under the same
impact/currency vocabulary. It needs no API key, no browser and no scraping:
one ~10KB GET. The alternatives were rejected on evidence, not preference — the
FMP endpoint returns 401 without a paid key, and forexfactory.com/calendar
returns 403 to a plain client and needs Playwright to defeat, which is not a
dependency worth putting in a trading node's critical path.

COVERAGE, defined the way the source actually works. FF publishes one file per
calendar WEEK, Sunday→Saturday in New York time. So coverage is that week's
bounds — NOT `max(event_time)`. Deriving coverage from the last event would
declare the calendar broken every quiet weekend and every public holiday, which
trains an operator to ignore it. The window exists whether or not events fall
inside it, so a genuinely empty period proves coverage instead of failing it.

TIME. Every event instant is an absolute UTC instant. The feed states an
explicit offset per event (`2026-08-09T19:50:00-04:00`) and we convert; we
never add or assume an offset, and we never parse a naive timestamp as if it
were UTC. The America/New_York zone appears in exactly one place — deriving the
publication week's bounds — and cannot move an event by so much as a second.
This is unrelated to, and must not be confused with, the Europe/London SESSION
classifier in `strategy_core.sessions`.

FAIL-CLOSED, ASYMMETRICALLY. Every refusal here blocks OPEN only. CLOSE,
MODIFY, reconciliation and the kill switch are never gated on news: a node that
cannot exit because a calendar server is down is a worse failure than one that
cannot enter. Stranding an open position on a news outage is not safety.

NOT A FLATTENER. This module never asks anyone to close anything. The
pre-blackout flatten (`news_flatten_active_trades`) is disabled in the tracked
strategy authority; upcoming news suppresses new fills and nothing else.
"""

from __future__ import annotations

import json
import os
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from live.state import atomic_write_text

SCHEMA = "ct.news-calendar.v1"
SOURCE_ID = "forexfactory-weekly-json"
#: Overridable so a failover mirror can be pointed at without a code change,
#: and so an operator can PROVE the fail-closed path by aiming it at a dead
#: host. The default is the production source.
DEFAULT_SOURCE_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
SOURCE_URL = os.environ.get("NEWS_SOURCE_URL") or DEFAULT_SOURCE_URL

CACHE_RELPATH = "news/calendar_live.json"
#: Written on EVERY attempt, success or failure, so a refusal can name the
#: actual error rather than reporting an absence.
STATUS_RELPATH = "news/refresh_last.json"

#: FF's publication week: Sunday 00:00 → Saturday 24:00, New York wall clock.
_FF_WEEK_TZ = ZoneInfo("America/New_York")

# ── refusal codes (the rail publishes these verbatim) ────────────────────────
REFUSE_UNAVAILABLE = "news_calendar_unavailable"
REFUSE_STALE = "news_calendar_stale"
REFUSE_COVERAGE = "news_calendar_coverage_unknown"
REFUSE_BLACKOUT = "news_blackout"

HEALTH_OK = "ok"


def _env_int(name: str, default: int) -> int:
    try:
        v = int(str(os.environ.get(name, "")).strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


#: How often to go to the network. The cycle is ~20 minutes, so this is at most
#: one small GET per cycle — never per strategy decision.
REFRESH_TTL_S = _env_int("NEWS_REFRESH_TTL_S", 1800)
#: How long a cached calendar stays usable after the last SUCCESSFUL fetch.
#: Cached events remain valid while their week is covered, so this bounds a
#: different risk: an event being added or RESCHEDULED without us hearing.
MAX_STALE_S = _env_int("NEWS_MAX_STALE_S", 6 * 3600)
#: Coverage must extend at least this far past now before a new position is
#: allowed — enough that a fill cannot outlive the horizon we can see.
REQUIRED_HORIZON_S = _env_int("NEWS_REQUIRED_HORIZON_S", 3600)
FETCH_TIMEOUT_S = _env_int("NEWS_FETCH_TIMEOUT_S", 20)
#: A weekly calendar is ~10KB. Anything of a different order is not our feed.
MAX_PAYLOAD_BYTES = 8 * 1024 * 1024

_IMPACTS = ("high", "medium", "low", "holiday")


# ── week bounds ──────────────────────────────────────────────────────────────

def ff_week_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    """(start, end) UTC instants of the FF publication week containing `now`.

    Computed on NAIVE New York wall clock and localised afterwards, so a DST
    transition inside the week cannot shorten or lengthen it: the week is
    always Sunday 00:00 to the next Sunday 00:00 local, whatever that is in
    absolute time.
    """
    if now_utc.tzinfo is None:
        raise ValueError("ff_week_bounds requires an aware UTC datetime")
    et = now_utc.astimezone(_FF_WEEK_TZ)
    days_since_sunday = (et.weekday() + 1) % 7          # Mon=0 … Sun=6
    start_naive = (et.replace(tzinfo=None, hour=0, minute=0, second=0, microsecond=0)
                   - timedelta(days=days_since_sunday))
    end_naive = start_naive + timedelta(days=7)
    return (start_naive.replace(tzinfo=_FF_WEEK_TZ).astimezone(timezone.utc),
            end_naive.replace(tzinfo=_FF_WEEK_TZ).astimezone(timezone.utc))


# ── normalisation ────────────────────────────────────────────────────────────

def parse_event_time(raw) -> datetime:
    """Feed timestamp -> absolute UTC instant. Raises on anything ambiguous.

    A naive timestamp is REJECTED rather than assumed to be UTC: guessing an
    offset is exactly how an event moves by an hour.
    """
    s = str(raw or "").strip()
    if not s:
        raise ValueError("empty event time")
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)      # raises ValueError on malformed input
    if dt.tzinfo is None:
        raise ValueError(f"event time carries no UTC offset: {raw!r}")
    return dt.astimezone(timezone.utc)


def normalise_events(payload) -> list[dict]:
    """Raw feed rows -> deterministic normalised rows, in the archive's shape.

    STRICT BY DESIGN. One unparseable row aborts the whole refresh (see
    `NewsCalendar.refresh`) rather than being dropped. A dropped event is a
    silently missing blackout, and this module's entire job is to never let a
    missing event look like a quiet market.
    """
    if not isinstance(payload, list):
        raise ValueError(f"expected a JSON list, got {type(payload).__name__}")
    out = []
    for i, row in enumerate(payload):
        if not isinstance(row, dict):
            raise ValueError(f"row {i} is {type(row).__name__}, not an object")
        when = parse_event_time(row.get("date"))
        currency = str(row.get("country") or "").strip().upper()
        impact = str(row.get("impact") or "").strip().lower()
        title = str(row.get("title") or "").strip()
        if not currency:
            raise ValueError(f"row {i} has no currency")
        if impact not in _IMPACTS:
            raise ValueError(f"row {i} has unknown impact {row.get('impact')!r}")
        out.append({"time": when.isoformat().replace("+00:00", "Z"),
                    "currency": currency, "impact": impact,
                    "event": title[:120], "source": "forexfactory"})
    out.sort(key=lambda e: (e["time"], e["currency"], e["event"]))
    return out


def relevant(events, currencies, impacts) -> list[dict]:
    """Events that could suppress a fill for this instrument."""
    cur = {str(c).upper() for c in (currencies or [])}
    imp = {str(i).lower() for i in (impacts or [])}
    return [e for e in events
            if (not cur or e.get("currency") in cur)
            and (not imp or e.get("impact") in imp)]


# ── blackout ─────────────────────────────────────────────────────────────────

def blackout_event(events, now_utc: datetime, *, before_min: float,
                   after_min: float) -> dict | None:
    """The event blacking out `now`, or None.

    Window is [T - before, T + after], CLOSED at both ends: an event exactly
    `before` minutes away is inside the blackout, not outside it. Boundaries
    are the only part of a rule anyone gets wrong, so they are pinned by test.
    """
    if now_utc.tzinfo is None:
        raise ValueError("blackout_event requires an aware UTC datetime")
    before = timedelta(minutes=float(before_min))
    after = timedelta(minutes=float(after_min))
    hit = None
    for e in events:
        try:
            t = parse_event_time(e.get("time"))
        except (ValueError, TypeError):
            # Unparseable rows never reach the cache; if one somehow did, it
            # must not be treated as "no event here".
            raise
        if (t - before) <= now_utc <= (t + after):
            if hit is None or t < parse_event_time(hit["time"]):
                hit = e
    return hit


def next_event(events, now_utc: datetime) -> dict | None:
    """The soonest event strictly after `now`, or None."""
    future = [(parse_event_time(e["time"]), e) for e in events
              if parse_event_time(e["time"]) > now_utc]
    return min(future, key=lambda p: p[0])[1] if future else None


# ── health ───────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class NewsHealth:
    """Whether news protection is actually working, and if not, why not."""
    ok: bool
    status: str                       # ok | stale | coverage | unavailable
    reason: str | None                # refusal code, None when ok
    detail: str
    source: str | None = None
    fetched_at: str | None = None
    age_s: float | None = None
    coverage_until: str | None = None
    coverage_s: float | None = None
    event_count: int | None = None
    relevant_count: int | None = None
    last_error: str | None = None

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items()}


class NewsCalendar:
    """Owns the live calendar: refresh, cache, health, blackout.

    One instance per process. `refresh_if_due` is the only method that touches
    the network and is called once per cycle; every other method reads memory,
    so a safety rail can consult it without acquiring an I/O failure mode.
    """

    def __init__(self, config, *, url: str = SOURCE_URL, opener=None):
        self.config = config
        self.url = url
        #: Injection seam for tests and for the live-proof CLI. Production
        #: passes nothing and gets urllib.
        self._opener = opener
        state_dir = Path(getattr(config, "state_dir", "."))
        self.cache_path = state_dir / CACHE_RELPATH
        self.status_path = state_dir / STATUS_RELPATH
        self._cache: dict | None = None
        self._loaded = False
        self._strategy_cache: dict | None = None
        self.last_error: str | None = None

    # ── config projection ────────────────────────────────────────────────────
    def _strategy(self) -> dict:
        """The REVIEWED strategy parameters, read from the golden config.

        The rail must use the same ±3 minutes, the same impact set and the same
        currencies as the engine. Hardcoding them here would create a second
        source of truth that drifts silently the first time the authority is
        edited, so they are read from the file the strategy authority proves.
        `getattr` on the LiveConfig comes first purely as a test seam.
        """
        if self._strategy_cache is None:
            data = {}
            try:
                p = getattr(self.config, "golden_config_path", None)
                if p:
                    data = json.loads(Path(p).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = {}
            self._strategy_cache = data if isinstance(data, dict) else {}
        return self._strategy_cache

    def _param(self, name, default):
        v = getattr(self.config, name, None)
        if v is None:
            v = self._strategy().get(name)
        return default if v is None else v

    @property
    def currencies(self) -> list[str]:
        """Currencies whose news blocks THIS instrument.

        Mirrors the engine: an empty `news_blackout_currencies` means "derive
        from the symbol", so EURUSD is blocked by EUR and by USD.
        """
        explicit = list(self._param("news_blackout_currencies", None) or [])
        if explicit:
            return sorted({str(c).upper() for c in explicit})
        sym = str(getattr(self.config, "symbol", None)
                  or self._strategy().get("symbol") or "EURUSD").upper()[:6]
        return sorted({sym[:3], sym[3:6]}) if len(sym) == 6 else []

    @property
    def impacts(self) -> list[str]:
        return sorted({str(i).lower()
                       for i in (self._param("news_blackout_impacts", None) or ["high"])})

    @property
    def before_min(self) -> float:
        return float(self._param("news_blackout_minutes_before", 3))

    @property
    def after_min(self) -> float:
        return float(self._param("news_blackout_minutes_after", 3))

    @property
    def blackout_enabled(self) -> bool:
        return bool(self._param("news_blackout_enabled", True))

    # ── cache ────────────────────────────────────────────────────────────────
    def _load(self) -> dict | None:
        if not self._loaded:
            self._loaded = True
            try:
                d = json.loads(self.cache_path.read_text(encoding="utf-8"))
                self._cache = d if d.get("schema") == SCHEMA else None
                if self._cache is None:
                    self.last_error = f"cache schema {d.get('schema')!r} != {SCHEMA}"
            except (OSError, ValueError) as exc:
                self._cache = None
                self.last_error = f"{type(exc).__name__}: {exc}"
        return self._cache

    def events(self) -> list[dict]:
        c = self._load()
        return list(c.get("events") or []) if c else []

    def relevant_events(self) -> list[dict]:
        return relevant(self.events(), self.currencies, self.impacts)

    # ── refresh ──────────────────────────────────────────────────────────────
    def _fetch(self) -> list:
        if self._opener is not None:
            return self._opener(self.url)
        ctx = ssl.create_default_context()
        req = urllib.request.Request(
            self.url, headers={"User-Agent": "lux-live-node/1.0 (news-calendar)",
                               "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S, context=ctx) as resp:
            if getattr(resp, "status", 200) != 200:
                raise ValueError(f"HTTP {resp.status}")
            body = resp.read(MAX_PAYLOAD_BYTES + 1)
        if len(body) > MAX_PAYLOAD_BYTES:
            raise ValueError(f"payload exceeds {MAX_PAYLOAD_BYTES} bytes")
        return json.loads(body)

    def refresh(self, now: datetime | None = None) -> dict:
        """Fetch, validate, cache atomically. Never raises; returns an outcome.

        On ANY failure the previous cache is left untouched — a partial or
        malformed fetch must not be able to erase working protection. The
        failure is recorded durably and surfaces through `health()`, and if it
        persists past MAX_STALE_S the node fails closed on its own.
        """
        now = now or datetime.now(timezone.utc)
        attempt = {"at": now.isoformat(), "source": SOURCE_ID, "url": self.url}
        try:
            raw = self._fetch()
            events = normalise_events(raw)
            start, end = ff_week_bounds(now)
            payload = {"schema": SCHEMA, "source": SOURCE_ID, "source_url": self.url,
                       "fetched_at": now.isoformat(),
                       "coverage_from": start.isoformat(),
                       "coverage_until": end.isoformat(),
                       "event_count": len(events), "events": events}
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.cache_path, json.dumps(payload, indent=1))
            self._cache, self._loaded, self.last_error = payload, True, None
            attempt.update({"ok": True, "event_count": len(events),
                            "coverage_until": end.isoformat()})
        except Exception as exc:      # network, TLS, JSON, schema, disk — all equal
            self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            attempt.update({"ok": False, "error": self.last_error})
        try:
            self.status_path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self.status_path, json.dumps(attempt, indent=1))
        except OSError:
            pass
        return attempt

    def refresh_if_due(self, now: datetime | None = None) -> dict:
        """Refresh only when the cache is older than the TTL."""
        now = now or datetime.now(timezone.utc)
        c = self._load()
        if c:
            try:
                age = (now - parse_event_time(c["fetched_at"])).total_seconds()
                if age < REFRESH_TTL_S:
                    return {"ok": True, "skipped": "within_ttl", "age_s": round(age, 1)}
            except (KeyError, ValueError):
                pass
        return self.refresh(now)

    # ── the verdict ──────────────────────────────────────────────────────────
    def health(self, now: datetime | None = None) -> NewsHealth:
        now = now or datetime.now(timezone.utc)
        c = self._load()
        if not c:
            return NewsHealth(False, "unavailable", REFUSE_UNAVAILABLE,
                              f"no usable calendar cache at {self.cache_path.name}"
                              + (f" ({self.last_error})" if self.last_error else ""),
                              source=SOURCE_ID, last_error=self.last_error)
        try:
            fetched = parse_event_time(c["fetched_at"])
            until = parse_event_time(c["coverage_until"])
        except (KeyError, ValueError) as exc:
            return NewsHealth(False, "unavailable", REFUSE_UNAVAILABLE,
                              f"cache lacks usable timestamps: {exc}",
                              source=c.get("source"), last_error=self.last_error)
        age = (now - fetched).total_seconds()
        cov = (until - now).total_seconds()
        n_all, n_rel = len(self.events()), len(self.relevant_events())
        common = dict(source=c.get("source"), fetched_at=c.get("fetched_at"),
                      age_s=round(age, 1), coverage_until=c.get("coverage_until"),
                      coverage_s=round(cov, 1), event_count=n_all,
                      relevant_count=n_rel, last_error=self.last_error)
        if cov < REQUIRED_HORIZON_S:
            return NewsHealth(False, "coverage", REFUSE_COVERAGE,
                              f"calendar covers only {cov/60:.1f} more minutes; "
                              f"{REQUIRED_HORIZON_S/60:.0f} required", **common)
        if age > MAX_STALE_S:
            return NewsHealth(False, "stale", REFUSE_STALE,
                              f"last successful refresh {age/3600:.1f}h ago; "
                              f"limit {MAX_STALE_S/3600:.1f}h", **common)
        return NewsHealth(True, HEALTH_OK, None,
                          f"{n_rel} relevant events; fresh {age/60:.1f}m; "
                          f"covered {cov/3600:.1f}h", **common)

    def verdict(self, now: datetime | None = None) -> tuple[bool, str | None, str]:
        """(allowed, refusal_code, detail) for a NEW OPEN. Never raises.

        Order matters: an unprovable calendar is refused BEFORE asking it
        whether a blackout is running, because a calendar that cannot be
        trusted cannot be trusted to say "no event".
        """
        now = now or datetime.now(timezone.utc)
        try:
            if not self.blackout_enabled:
                # The reviewed strategy does not use news protection at all.
                # Gating on calendar health would then refuse OPENs to protect a
                # rule that does not exist. Changing this bool is a strategy
                # authority change: new digest, witness re-bootstrap, visible.
                return True, None, "news blackout disabled in strategy authority"
            h = self.health(now)
            if not h.ok:
                return False, h.reason, h.detail
            hit = blackout_event(self.relevant_events(), now,
                                 before_min=self.before_min, after_min=self.after_min)
            if hit is not None:
                return False, REFUSE_BLACKOUT, (
                    f"{hit['impact'].upper()} {hit['currency']} {hit['event']} "
                    f"at {hit['time']} (+/-{self.before_min:g}/{self.after_min:g}m)")
            return True, None, h.detail
        except Exception as exc:
            # Any unexpected fault is a refusal, never a pass. A news gate that
            # opens on error is not a gate.
            return False, REFUSE_UNAVAILABLE, f"{type(exc).__name__}: {str(exc)[:120]}"

    # ── reporting ────────────────────────────────────────────────────────────
    def telemetry_block(self, now: datetime | None = None) -> dict:
        """Additive `news` block for `ct.node-telemetry.v1`. No raw payloads."""
        now = now or datetime.now(timezone.utc)
        h = self.health(now)
        allowed, reason, detail = self.verdict(now)
        rel = self.relevant_events()
        hit = None
        try:
            hit = blackout_event(rel, now, before_min=self.before_min,
                                 after_min=self.after_min)
        except Exception:
            pass
        nxt = None
        try:
            nxt = next_event(rel, now)
        except Exception:
            pass

        def _ev(e):
            return None if not e else {"time": e["time"], "currency": e["currency"],
                                       "impact": e["impact"], "event": e["event"]}
        return {
            "schema_version": SCHEMA,
            "source": h.source or SOURCE_ID,
            "source_url": self.url,
            "health": h.status,
            "healthy": h.ok,
            "last_refresh_at": h.fetched_at,
            "last_refresh_age_s": h.age_s,
            "last_error": h.last_error,
            "coverage_until": h.coverage_until,
            "coverage_remaining_s": h.coverage_s,
            "event_count": h.event_count,
            "relevant_event_count": h.relevant_count,
            "watched_currencies": self.currencies,
            "watched_impacts": self.impacts,
            "blackout_enabled": self.blackout_enabled,
            "blackout_minutes_before": self.before_min,
            "blackout_minutes_after": self.after_min,
            "blackout_active": hit is not None,
            "blackout_event": _ev(hit),
            "next_relevant_event": _ev(nxt),
            "new_open_allowed": allowed,
            "refusal_reason": reason,
            "detail": detail[:200],
            # The pre-blackout flatten is OFF in the tracked authority; publish
            # it so the Control Tower can SEE that, not assume it.
            "flatten_active_trades": bool(
                self._param("news_flatten_active_trades", False)),
        }
