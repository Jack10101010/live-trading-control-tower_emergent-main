"""HARDEN-2 — the Trade Ledger's missing ownership point.

WHAT WAS MISSING
    Every link of the ledger chain existed and was correct:

        broker_history -> trade_reconstruction -> trade_ledger_domain
                       -> trade_ledger_store   -> projection -> 6 API routes

    but `server.refresh_trade_ledger()` had **zero production call sites** —
    verified at AST level across all backend modules. Six `/api/ledger/*`
    endpoints served a store that nothing ever wrote. The ledger was a complete,
    correct library wired to nothing.

    The missing piece was never logic. It was OWNERSHIP: no component was
    responsible for deciding *when* a closed trade should be reconstructed.

WHAT THIS OWNS
    Exactly that decision, and nothing else. It does not reconstruct trades
    (`trade_reconstruction` does), it does not persist them (`trade_ledger_store`
    does), and it does not read the broker itself (the injected refresh does).

EXACTLY-ONCE — AND WHERE IT ACTUALLY COMES FROM
    Not from this scheduler. `TradeId.derive()` hashes immutable broker lineage
    (account fingerprint, position id, instrument, opening deal id), and
    `ingest_broker_history` is idempotent on that id. So ingesting the same
    history ten times produces the same one ledger entry.

    That is what makes replay correct, and it is why this module can afford to
    be approximate about *when* it runs: a missed trigger delays a trade
    reaching the ledger, and a duplicated trigger costs a wasted read. Neither
    can produce a duplicate or a lost trade.

CADENCE, AND WHY IT IS NOT EVERY TICK
    The runtime ticks every ~5s. Reconstructing history that often would issue a
    broker history read twelve times a minute to observe an event that happens a
    few times a day. So:

      * a periodic sweep on a much longer interval (default 60s), and
      * an IMMEDIATE sweep when the observed open-position count FALLS, which is
        the actual signal that a trade may have completed.

    The second is the responsive path; the first is the safety net that makes
    the system correct even if the signal is missed entirely (a restart, a
    missed tick, a position closed while the process was down).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

#: Periodic sweep interval. Long relative to the runtime tick because closed
#: trades are rare and a history read is not free.
DEFAULT_INTERVAL_S = 60.0

# ── reasons a sweep ran, recorded so the behaviour is explainable ────────────
REASON_PERIODIC = "periodic"
REASON_POSITION_CLOSED = "position_count_fell"
REASON_FIRST_RUN = "first_run"
REASON_FORCED = "forced"

SKIP_NOT_DUE = "not_due"
SKIP_DISABLED = "disabled"


@dataclass
class IngestionResult:
    """What one observation did. Counts are facts, not estimates."""
    ran: bool = False
    reason: str | None = None
    ingested: int = 0
    available: bool = False
    code: str | None = None
    detail: str | None = None
    at: str | None = None

    def as_dict(self) -> dict:
        return {"ran": self.ran, "reason": self.reason, "ingested": self.ingested,
                "available": self.available, "code": self.code,
                "detail": self.detail, "at": self.at}


@dataclass
class IngestionStatus:
    """Cumulative evidence, for diagnostics. Never inferred."""
    sweeps: int = 0
    #: Entries TOUCHED across all sweeps. NOT a count of distinct trades:
    #: ingestion is idempotent, so a redundant sweep touches the same entry
    #: again. The distinct count lives in the ledger store, which is the
    #: authority. Verified: 6 sweeps over one history -> 1 ledger entry.
    entries_touched_total: int = 0
    last_run_at: str | None = None
    last_reason: str | None = None
    last_success_at: str | None = None
    last_failure_at: str | None = None
    last_failure_detail: str | None = None
    observed_positions: int | None = None
    warnings: tuple = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {"sweeps": self.sweeps,
                "entriesTouchedTotal": self.entries_touched_total,
                "lastRunAt": self.last_run_at, "lastReason": self.last_reason,
                "lastSuccessAt": self.last_success_at,
                "lastFailureAt": self.last_failure_at,
                "lastFailureDetail": self.last_failure_detail,
                "observedPositions": self.observed_positions,
                "warnings": list(self.warnings)}


class LedgerIngestionService:
    """Decides WHEN the ledger is refreshed. Observes the runtime tick.

    Wired as a tick observer so the runtime keeps exactly one polling owner:
    this never opens its own loop, never reads the broker directly, and never
    blocks a tick (the supervisor runs observers outside its lock and swallows
    their failures).
    """

    def __init__(self, *, refresh_fn: Callable[[], dict],
                 now_iso_fn: Callable[[], str],
                 interval_s: float = DEFAULT_INTERVAL_S,
                 enabled_fn: Callable[[], bool] | None = None,
                 monotonic_fn: Callable[[], float] | None = None,
                 logger: Any = None):
        self._refresh = refresh_fn
        self._now = now_iso_fn
        self.interval_s = float(interval_s)
        #: ON by default. Ingestion is a READ plus a write to the ledger's own
        #: store: it places no order and touches no execution state, so running
        #: it automatically cannot cause a trade. Leaving it off by default is
        #: what produced the empty-ledger defect in the first place.
        self._enabled = enabled_fn or (lambda: True)
        import time as _time
        self._monotonic = monotonic_fn or _time.monotonic
        self._logger = logger
        self._last_sweep_at: float | None = None
        self._last_positions: int | None = None
        self.status = IngestionStatus()

    # ── the observer ─────────────────────────────────────────────────────────
    def on_tick(self, snapshot) -> IngestionResult:
        """Observe one runtime tick and sweep if warranted. Never raises."""
        if not self._enabled():
            return IngestionResult(ran=False, reason=SKIP_DISABLED)
        positions = _open_position_count(snapshot)
        reason = self._due(positions)
        self.status.observed_positions = positions
        # Track the count AFTER deciding, so a fall is detected exactly once.
        if positions is not None:
            self._last_positions = positions
        if reason is None:
            return IngestionResult(ran=False, reason=SKIP_NOT_DUE)
        return self.sweep(reason=reason)

    def _due(self, positions: int | None) -> str | None:
        """Whether a sweep is warranted, and why. First matching rule wins."""
        if self._last_sweep_at is None:
            return REASON_FIRST_RUN
        # A FALL in open positions is the signal that a trade may have closed.
        if (positions is not None and self._last_positions is not None
                and positions < self._last_positions):
            return REASON_POSITION_CLOSED
        if (self._monotonic() - self._last_sweep_at) >= self.interval_s:
            return REASON_PERIODIC
        return None

    def sweep(self, *, reason: str = REASON_FORCED) -> IngestionResult:
        """Run one ingestion pass. Total: a failure is recorded, never raised.

        Safe to call repeatedly — ingestion is idempotent on the derived
        TradeId, so a redundant sweep converges rather than duplicating.
        """
        now = self._now()
        self._last_sweep_at = self._monotonic()
        self.status.sweeps += 1
        self.status.last_run_at = now
        self.status.last_reason = reason
        try:
            outcome = self._refresh()
        except Exception as exc:                                # noqa: BLE001
            detail = type(exc).__name__
            self.status.last_failure_at = now
            self.status.last_failure_detail = detail
            if self._logger is not None:
                self._logger.warning(
                    "ledger.ingest failed reason=%s detail=%s", reason, detail)
            return IngestionResult(ran=True, reason=reason, available=False,
                                   code="ingestion_failed", detail=detail, at=now)

        outcome = outcome if isinstance(outcome, dict) else {}
        ingested = int(outcome.get("ingested") or 0)
        available = bool(outcome.get("available"))
        code = outcome.get("code")
        if available:
            self.status.last_success_at = now
            self.status.entries_touched_total += ingested
        else:
            self.status.last_failure_at = now
            self.status.last_failure_detail = code or "unavailable"
        # Log only when something HAPPENED. A 60s heartbeat that ingests nothing
        # is noise; a trade reaching the ledger is not.
        if ingested and self._logger is not None:
            self._logger.info(
                "ledger.ingested at=%s reason=%s trades=%d ready_to_finalize=%s",
                now, reason, ingested, outcome.get("readyToFinalize"))
        return IngestionResult(ran=True, reason=reason, ingested=ingested,
                               available=available, code=code,
                               detail=outcome.get("detail"), at=now)


def _open_position_count(snapshot) -> int | None:
    """Open positions in this runtime snapshot, or None when unreadable.

    None is NOT zero: an unreadable snapshot must never look like "every
    position just closed" and trigger a spurious sweep.
    """
    raw = getattr(snapshot, "broker_snapshot", None)
    if not isinstance(raw, dict):
        return None
    positions = raw.get("positions")
    return len(positions) if isinstance(positions, (list, tuple)) else None
