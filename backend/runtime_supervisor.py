"""LIVE-5A — the ONE runtime refresh owner.

THE PROBLEM THIS FIXES (audited, not assumed)
    Before LIVE-5A there was no refresh owner at all. Every `/api/operations/*`
    handler called `server._projection_sources()`, whose `broker_snapshot`
    binding is `_fresh_broker_snapshot()` — a SYNCHRONOUS broker read on the
    request path. The browser polls fourteen queries (nine at 10s, plus 15s, 20s
    and 30s tiers), so the broker was read fourteen times per cycle, each read
    with its own independent notion of "now", and no component could say when the
    broker was last actually heard from.

    This module makes the cadence explicit and singular:

        supervisor tick  ->  broker read + market read  ->  cached snapshot
        every API request ->  serves the CACHED snapshot, reads no broker

RESPONSIBILITIES
    broker reads, market reads, projection rebuild, runtime health.

EXPLICITLY NOT RESPONSIBLE FOR
    execution, recommendation decisions, authorization, strategy optimisation.
    None of those are imported (structurally pinned). The loop can therefore
    never place, modify or cancel an order, and a stalled loop cannot cause a
    trade — only a stale projection, which is reported honestly.

CADENCE
    One tick every `interval_s` (default 5s). The loop is a single daemon thread,
    mirroring the existing `ops_journal` / `ops_notifier` pattern already used in
    this codebase rather than inventing a second concurrency style. Ticks NEVER
    overlap: the thread runs them sequentially, and `tick_once()` additionally
    holds a lock so a manual tick cannot interleave with the loop's.

STALE LOOP DETECTION
    The supervisor records `last_tick_at` and a monotonic tick counter. If the
    loop dies or wedges, `health()` reports the runtime as STALE with the true
    age — it does not keep serving the last snapshot as though it were current.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

import market_runtime as mr

# ── runtime health states (PART 11) ──────────────────────────────────────────
STARTING = "STARTING"          # loop created, no successful tick yet
CONNECTED = "CONNECTED"        # broker connected, evidence fresh
STALE = "STALE"                # evidence too old to trust
DEGRADED = "DEGRADED"          # answering, but unhealthy (latency / partial feed)
RECONNECTING = "RECONNECTING"  # recent failures, still trying
STOPPED = "STOPPED"            # loop deliberately stopped
UNKNOWN = "UNKNOWN"            # cannot be determined

ALL_RUNTIME_STATES = frozenset({
    STARTING, CONNECTED, STALE, DEGRADED, RECONNECTING, STOPPED, UNKNOWN})

#: A snapshot older than this is stale — roughly three default ticks, so one
#: missed tick is tolerated and a wedged loop is not.
DEFAULT_STALE_AFTER_S = 20.0
#: Consecutive failed ticks before the runtime is RECONNECTING rather than
#: merely degraded.
RECONNECT_AFTER_FAILURES = 2


def _now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse(ts: Any) -> datetime | None:
    return mr._parse(ts)


def _sorted(obj: Any) -> Any:
    return mr._sorted(obj)


@dataclass(frozen=True)
class RuntimeHealth:
    """Deterministic runtime health. Derived ONLY from recorded evidence —
    timestamps, counters and connection state — never from opinion."""
    state: str = UNKNOWN
    at: str | None = None
    last_tick_at: str | None = None
    last_success_at: str | None = None
    tick_count: int = 0
    consecutive_failures: int = 0
    projection_age_seconds: float | None = None
    broker_age_seconds: float | None = None
    latency_ms: float | None = None
    connection: str = mr.CONN_UNKNOWN
    running: bool = False
    interval_s: float | None = None
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return _sorted({
            "state": self.state, "at": self.at,
            "lastTickAt": self.last_tick_at,
            "lastSuccessAt": self.last_success_at,
            "tickCount": self.tick_count,
            "consecutiveFailures": self.consecutive_failures,
            "projectionAgeSeconds": self.projection_age_seconds,
            "brokerAgeSeconds": self.broker_age_seconds,
            "latencyMs": self.latency_ms, "connection": self.connection,
            "running": self.running, "intervalSeconds": self.interval_s,
            "warnings": list(self.warnings),
        })


@dataclass(frozen=True)
class RuntimeSnapshot:
    """Everything one tick produced: market evidence, broker evidence, health.

    This is what every API request serves. It is immutable, so a request can
    never observe a half-updated runtime.
    """
    at: str
    sequence: int
    market: mr.MarketRuntimeSnapshot | None = None
    broker_snapshot: dict | None = None
    account: dict | None = None
    server_time: str | None = None
    health: RuntimeHealth = field(default_factory=RuntimeHealth)
    tick_duration_ms: float | None = None
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self, now: str | None = None) -> dict:
        ref = now or self.at
        return _sorted({
            "at": self.at, "sequence": self.sequence,
            "market": self.market.as_dict(ref) if self.market else None,
            "account": self.account, "serverTime": self.server_time,
            "health": self.health.as_dict(),
            "tickDurationMs": self.tick_duration_ms,
            "warnings": list(self.warnings),
        })


class RuntimeSupervisor:
    """The single owner of broker reads, market reads and runtime health.

    Collaborators are injected, so the supervisor holds no global and can be
    driven tick-by-tick in a test with no threads and no real clock.
    """

    def __init__(self, *, market: mr.MarketRuntime,
                 broker_snapshot_fn: Callable[[], dict | None],
                 account_fn: Callable[[], dict | None] | None = None,
                 server_time_fn: Callable[[], str | None] | None = None,
                 now_iso_fn: Callable[[], str] = _now_utc_iso,
                 interval_s: float = 5.0,
                 stale_after_s: float = DEFAULT_STALE_AFTER_S,
                 on_tick: Callable[[RuntimeSnapshot], None] | None = None,
                 monotonic_fn: Callable[[], float] = time.monotonic,
                 logger: Any = None):
        self._market = market
        self._broker_snapshot = broker_snapshot_fn
        self._account = account_fn or (lambda: None)
        self._server_time = server_time_fn or (lambda: None)
        self._now = now_iso_fn
        self.interval_s = float(interval_s)
        self._stale_after_s = float(stale_after_s)
        #: Called after each tick with the new snapshot. Used to drive the
        #: scenario producer — the supervisor itself knows nothing about
        #: scenarios, and a failing observer can never break a tick.
        self._on_tick = on_tick
        self._monotonic = monotonic_fn
        self._logger = logger

        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._stopped = False
        self._sequence = 0
        self._failures = 0
        self._last: RuntimeSnapshot | None = None
        self._last_tick_at: str | None = None
        self._last_success_at: str | None = None

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self) -> None:
        """Idempotent: at most one daemon worker thread; never blocks requests."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._stopped = False
            self._thread = threading.Thread(
                target=self._run, name="runtime-supervisor", daemon=True)
            self._thread.start()

    def stop(self, timeout_s: float = 5.0) -> None:
        """Safe when never started."""
        self._stop_evt.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout_s)
        self._stopped = True
        self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop_evt.is_set():
            try:
                self.tick_once()
            except Exception:                                   # noqa: BLE001
                # A tick must never kill the loop: the next one may succeed, and
                # a dead loop would silently freeze the whole dashboard.
                if self._logger is not None:
                    self._logger.exception("runtime supervisor tick failed")
            self._stop_evt.wait(self.interval_s)

    # ── the tick ─────────────────────────────────────────────────────────────
    def tick_once(self) -> RuntimeSnapshot:
        """One refresh: market read, broker read, health. Never overlaps.

        The lock is what guarantees non-overlap: the loop and any manual tick
        serialize, so two concurrent refreshes cannot interleave their reads or
        publish out-of-order snapshots.
        """
        with self._lock:
            started = self._monotonic()
            now = self._now()
            warnings: list[str] = []

            market = None
            try:
                market = self._market.refresh()
                warnings.extend(market.warnings)
            except Exception as exc:                            # noqa: BLE001
                warnings.append(f"market_refresh_failed:{type(exc).__name__}")

            snapshot = self._guarded(self._broker_snapshot, "broker_snapshot",
                                     warnings)
            account = self._guarded(self._account, "account", warnings)
            server_time = self._guarded(self._server_time, "server_time",
                                        warnings)

            connected = bool(market and market.heartbeat.connection
                             == mr.CONN_CONNECTED)
            ok = bool(connected and snapshot is not None)
            self._failures = 0 if ok else self._failures + 1
            self._sequence += 1
            self._last_tick_at = now
            if ok:
                self._last_success_at = now

            duration = round((self._monotonic() - started) * 1000.0, 3)
            health = self._health(now=now, market=market, warnings=warnings,
                                 duration_ms=duration)
            result = RuntimeSnapshot(
                at=now, sequence=self._sequence, market=market,
                broker_snapshot=snapshot, account=account,
                server_time=server_time, health=health,
                tick_duration_ms=duration,
                warnings=tuple(dict.fromkeys(warnings)))
            self._last = result

        # Observers run OUTSIDE the lock: a slow scenario producer must not
        # stall the next broker read, and its failure must not lose the tick.
        if self._on_tick is not None:
            try:
                self._on_tick(result)
            except Exception:                                   # noqa: BLE001
                if self._logger is not None:
                    self._logger.exception("runtime tick observer failed")
        return result

    @staticmethod
    def _guarded(fn: Callable, name: str, warnings: list):
        try:
            return fn()
        except Exception as exc:                                # noqa: BLE001
            warnings.append(f"{name}_read_failed:{type(exc).__name__}")
            return None

    # ── health derivation ────────────────────────────────────────────────────
    def _health(self, *, now: str, market, warnings: list,
                duration_ms: float | None) -> RuntimeHealth:
        connection = market.heartbeat.connection if market else mr.CONN_UNKNOWN
        latency = market.heartbeat.latency_ms if market else None
        broker_age = market.heartbeat.age_seconds(now) if market else None
        projection_age = 0.0            # this snapshot IS the projection input
        state = self._derive_state(
            connection=connection, latency_ms=latency, broker_age=broker_age,
            market=market)
        return RuntimeHealth(
            state=state, at=now, last_tick_at=self._last_tick_at,
            last_success_at=self._last_success_at, tick_count=self._sequence,
            consecutive_failures=self._failures,
            projection_age_seconds=projection_age, broker_age_seconds=broker_age,
            latency_ms=latency, connection=connection, running=self.running,
            interval_s=self.interval_s,
            warnings=tuple(dict.fromkeys(warnings)))

    def _derive_state(self, *, connection: str, latency_ms: float | None,
                      broker_age: float | None, market) -> str:
        """The state machine. Deterministic and ordered — the FIRST matching
        rule wins, so a given set of evidence always yields the same state."""
        if self._stopped:
            return STOPPED
        if self._sequence == 0 or self._last_success_at is None:
            # Never succeeded. Failing repeatedly at startup is still STARTING
            # until it has had a real chance; after that it is RECONNECTING.
            return (RECONNECTING if self._failures >= RECONNECT_AFTER_FAILURES
                    else STARTING)
        if self._failures >= RECONNECT_AFTER_FAILURES:
            return RECONNECTING
        if broker_age is None:
            return UNKNOWN
        if broker_age > self._stale_after_s:
            return STALE
        if connection != mr.CONN_CONNECTED:
            return RECONNECTING
        if latency_ms is not None and latency_ms > mr.DEGRADED_LATENCY_MS:
            return DEGRADED
        if market is not None and not all(s.active for s in market.subscriptions):
            # Connected and fast, but not every symbol is answering. That is
            # degraded, not connected — and definitely not "fine".
            return DEGRADED
        return CONNECTED

    # ── reads (no broker access, no rebuild) ─────────────────────────────────
    @property
    def last(self) -> RuntimeSnapshot | None:
        """The newest snapshot, or None before the first tick.

        None means "the runtime has not read anything yet" and must be reported
        as unavailable — never as an empty-but-healthy runtime.
        """
        return self._last

    def health(self, *, now: str | None = None) -> RuntimeHealth:
        """Current health, RE-AGED against `now`.

        This is the stale-loop detector: if the loop died, the stored snapshot
        keeps its original timestamps but the age recomputed here grows, so the
        state degrades to STALE instead of freezing on CONNECTED.
        """
        ref = now or self._now()
        last = self._last
        if last is None:
            return RuntimeHealth(
                state=(STOPPED if self._stopped else STARTING), at=ref,
                running=self.running, interval_s=self.interval_s,
                warnings=("no_tick_recorded",))
        age = mr._age_s(ref, last.at)
        if age is None:
            return last.health
        if self._stopped:
            return RuntimeHealth(**{**last.health.__dict__, "state": STOPPED,
                                    "at": ref, "projection_age_seconds": age,
                                    "running": False})
        state = last.health.state
        if age > self._stale_after_s and state not in (STOPPED,):
            state = STALE
        return RuntimeHealth(**{**last.health.__dict__, "state": state, "at": ref,
                                "projection_age_seconds": age,
                                "running": self.running})
