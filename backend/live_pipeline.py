"""LIVE-5A — the minimum Scenario and Recommendation producers.

WHAT THIS IS
    The smallest thing that turns a completed candle into a Scenario and a
    Recommendation, so the pipeline
    Market -> Scenario -> Recommendation -> Decision -> Intent -> Order ->
    Position -> Ledger can be exercised end to end.

WHAT THIS IS NOT — AND THE DISTINCTION MATTERS
    **This is not a strategy engine and the rule below has no edge.** It exists
    to prove the plumbing. Two candles produce a Scenario; nothing here was
    backtested, tuned or selected, and no claim is made that it should be traded.
    LIVE-5A is a pipeline proof, not a trading system.

    It is also NOT autonomous trading. The producer creates a PROPOSAL. Nothing
    downstream happens without an operator decision through the LIVE-4E surface,
    and accepting still places no order — the execution pipeline, its
    authorization and its safety gates are untouched.

THE RULE (deliberately trivial)
    On each COMPLETED M15 candle, compare it with the previous completed candle:

        close > previous high  ->  long  Scenario  (structure: BREAK_OF_HIGH)
        close < previous low   ->  short Scenario  (structure: BREAK_OF_LOW)
        otherwise              ->  no Scenario

    An in-progress candle is never considered: a forming bar would produce a
    different answer on every poll, so `market_runtime` only ever hands over
    bars it has already marked complete.

DETERMINISM, DUPLICATES AND RESTART SAFETY
    Scenario identity is derived from the candle's own CLOSE TIME (as the
    discriminator), never from wall-clock time. So:

      * the same candle always derives the same ScenarioId;
      * re-processing a candle after a restart creates nothing new, because the
        store's create is idempotent on that id;
      * at most one Scenario per candle per symbol, by construction rather than
        by a de-duplication pass.

    The producer additionally remembers the last candle it acted on per symbol,
    so a repeated tick does not even attempt a write. That is an optimisation;
    identity is what makes it CORRECT.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

import recommendation_domain as rd
import scenario_domain as sd

#: Provenance stamped on everything this module creates, so a Scenario produced
#: by the LIVE-5A proof is never mistaken for one a real strategy produced.
PROVENANCE = "live-5a-candle-producer"

STRUCTURE_BREAK_HIGH = "BREAK_OF_HIGH"
STRUCTURE_BREAK_LOW = "BREAK_OF_LOW"
ENTRY_MODEL = "CandleBreak"

#: Risk terms for the proposal. Fixed and tiny: this is a pipeline proof, and a
#: proof should risk as little as possible.
DEFAULT_RISK_PERCENT = 0.25
DEFAULT_QUANTITY = 0.01                  # the smallest lot most brokers accept
DEFAULT_STOP_BUFFER = 0.0005             # 5 pips on a 5-decimal FX pair
DEFAULT_TARGET_R = 2.0
#: A proposal older than this is stale evidence — the market has moved on.
DEFAULT_EXPIRY_MINUTES = 45

SESSION_BOUNDS = (
    ("asia", 0, 7), ("london", 7, 12), ("newyork", 12, 21), ("late", 21, 24),
)


def _parse(ts: Any) -> datetime | None:
    if ts is None:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def session_for(closed_at: str) -> str:
    """Trading session from the candle's UTC close hour. One rule, one place."""
    dt = _parse(closed_at)
    if dt is None:
        return "unknown"
    hour = dt.astimezone(timezone.utc).hour
    for name, start, end in SESSION_BOUNDS:
        if start <= hour < end:
            return name
    return "unknown"


@dataclass(frozen=True)
class CandleSignal:
    """The decision the rule reached about one completed candle."""
    symbol: str
    direction: str | None                  # "long" | "short" | None
    structure: str | None
    closed_at: str
    close: float
    previous_high: float
    previous_low: float

    @property
    def triggered(self) -> bool:
        return self.direction is not None

    def as_dict(self) -> dict:
        return {
            "symbol": self.symbol, "direction": self.direction,
            "structure": self.structure, "closedAt": self.closed_at,
            "close": self.close, "previousHigh": self.previous_high,
            "previousLow": self.previous_low, "triggered": self.triggered,
        }


def evaluate_candle(*, symbol: str, candle, previous) -> CandleSignal | None:
    """Apply the rule to a COMPLETED candle and its predecessor.

    Returns None when the inputs cannot support a decision — an incomplete bar,
    a missing predecessor or an unreadable level. A missing input is never
    treated as a zero.
    """
    if candle is None or previous is None:
        return None
    if not getattr(candle, "complete", False):
        return None                        # a forming bar is not evidence
    close = candle.close
    prev_high, prev_low = previous.high, previous.low
    if close is None or prev_high is None or prev_low is None:
        return None
    if candle.closed_at is None:
        return None
    direction = structure = None
    if close > prev_high:
        direction, structure = "long", STRUCTURE_BREAK_HIGH
    elif close < prev_low:
        direction, structure = "short", STRUCTURE_BREAK_LOW
    return CandleSignal(
        symbol=symbol, direction=direction, structure=structure,
        closed_at=candle.closed_at, close=close,
        previous_high=prev_high, previous_low=prev_low)


def scenario_for(signal: CandleSignal, *, node_id: str | None = None,
                 account_fingerprint: str | None = None) -> sd.Scenario:
    """The Scenario a triggered signal implies.

    The candle's CLOSE TIME is the discriminator, which is what makes identity
    deterministic and restart-safe: the same candle can only ever derive one
    ScenarioId, so re-processing it writes nothing new.
    """
    if not signal.triggered:
        raise ValueError("scenario_for requires a triggered signal")
    return sd.new_scenario(
        instrument=signal.symbol, session=session_for(signal.closed_at),
        structure=signal.structure, direction=signal.direction,
        entry_model=ENTRY_MODEL, created_at=signal.closed_at,
        discriminator=signal.closed_at,
        node_id=node_id, account_fingerprint=account_fingerprint,
        provenance=PROVENANCE)


def recommendation_for(signal: CandleSignal, scenario: sd.Scenario, *,
                       quote=None, node_id: str | None = None,
                       account_fingerprint: str | None = None,
                       expiry_minutes: int = DEFAULT_EXPIRY_MINUTES,
                       ) -> rd.Recommendation:
    """Exactly ONE Recommendation per Scenario, with complete trade terms.

    Entry comes from the live quote when one is available, and otherwise from the
    candle close — recorded either way, never invented. Stop and target are
    derived from the candle's own levels, so the proposal is arithmetic on
    measured evidence rather than a guess.
    """
    long_side = signal.direction == "long"
    entry = None
    if quote is not None:
        # Buy at the ask, sell at the bid — the price actually available.
        entry = quote.ask if long_side else quote.bid
    if entry is None:
        entry = signal.close

    if long_side:
        stop = round(min(signal.previous_low, entry) - DEFAULT_STOP_BUFFER, 5)
        risk = entry - stop
        target = round(entry + DEFAULT_TARGET_R * risk, 5)
    else:
        stop = round(max(signal.previous_high, entry) + DEFAULT_STOP_BUFFER, 5)
        risk = stop - entry
        target = round(entry - DEFAULT_TARGET_R * risk, 5)

    # A non-positive stop distance would make R meaningless; refuse rather than
    # publish a proposal whose risk cannot be stated.
    if not (risk > 0):
        raise ValueError("stop distance is not positive; refusing to propose")

    closed = _parse(signal.closed_at)
    expiry = _iso(closed + timedelta(minutes=expiry_minutes)) if closed else None

    terms = rd.RecommendationTerms(
        execution=rd.RecommendationExecutionTerms(
            order_type="market", requested_entry_price=round(entry, 5),
            entry_price_policy=rd.EntryPricePolicy.MARKET,
            quantity=DEFAULT_QUANTITY, quantity_policy=rd.QuantityPolicy.FIXED),
        risk=rd.RecommendationRiskTerms(
            stop_loss=stop, take_profit=target,
            risk_percent=DEFAULT_RISK_PERCENT, planned_r=DEFAULT_TARGET_R),
        rationale=(f"LIVE-5A pipeline proof: M15 close {signal.close} "
                   f"{'above previous high' if long_side else 'below previous low'} "
                   f"({signal.previous_high if long_side else signal.previous_low}). "
                   f"Not a tuned strategy."),
        confidence=None,                   # no basis to claim one — stays absent
        tags=("live-5a", "pipeline-proof"),
        metadata={"candleClosedAt": signal.closed_at,
                  "producer": PROVENANCE,
                  "structure": signal.structure})
    return rd.new_recommendation(
        scenario_id=scenario.scenario_id, instrument=signal.symbol,
        # STRATEGY = "not an operator". It does NOT assert a tuned strategy —
        # see this module's docstring and the vocabulary comment in the domain.
        direction=signal.direction, source=rd.RecommendationSource.STRATEGY,
        created_at=signal.closed_at, terms=terms,
        discriminator=signal.closed_at, node_id=node_id,
        account_fingerprint=account_fingerprint, expiry_at=expiry,
        provenance=PROVENANCE)


@dataclass
class ProducerResult:
    """What one pass produced. Counts are facts, not estimates."""
    evaluated: int = 0
    signals: int = 0
    scenarios_created: list = field(default_factory=list)
    recommendations_created: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "evaluated": self.evaluated, "signals": self.signals,
            "scenariosCreated": list(self.scenarios_created),
            "recommendationsCreated": list(self.recommendations_created),
            "skipped": list(self.skipped), "warnings": list(self.warnings),
        }


class LivePipelineProducer:
    """Turns completed candles into Scenarios and Recommendations.

    Wired as an OBSERVER of the runtime supervisor's tick, so there is still
    exactly one polling owner: this never reads the broker or the market itself,
    it only inspects the snapshot the supervisor already produced.
    """

    def __init__(self, *, scenario_store_fn: Callable[[], Any],
                 recommendation_service_fn: Callable[[], Any],
                 now_iso_fn: Callable[[], str],
                 history_fn: Callable[[str], list] | None = None,
                 node_id_fn: Callable[[], str | None] | None = None,
                 enabled_fn: Callable[[], bool] | None = None,
                 logger: Any = None):
        self._scenario_store = scenario_store_fn
        self._recommendations = recommendation_service_fn
        self._now = now_iso_fn
        #: Supplies the PREVIOUS completed candle. The rule needs two bars and
        #: the runtime snapshot carries only the newest, so history comes from
        #: the same market-data engine the supervisor reads.
        self._history = history_fn
        self._node_id = node_id_fn or (lambda: None)
        #: OFF by default. A producer that starts creating proposals the moment
        #: the process boots is exactly the autonomy this slice must not add.
        self._enabled = enabled_fn or (lambda: False)
        self._logger = logger
        self._seen: dict[str, str] = {}     # symbol -> last candle closed_at

    def on_tick(self, snapshot) -> ProducerResult:
        """Observe one runtime tick. Never raises: a producer failure must not
        break the refresh loop that the whole dashboard depends on."""
        result = ProducerResult()
        if not self._enabled():
            result.skipped.append("producer_disabled")
            return result
        market = getattr(snapshot, "market", None)
        if market is None:
            result.warnings.append("no_market_evidence")
            return result
        for sub in market.subscriptions:
            try:
                self._process(sub, result)
            except Exception as exc:                            # noqa: BLE001
                result.warnings.append(
                    f"producer_failed:{sub.symbol}:{type(exc).__name__}")
                if self._logger is not None:
                    self._logger.exception("live pipeline producer failed for %s",
                                           sub.symbol)
        return result

    def _process(self, sub, result: ProducerResult) -> None:
        candle = sub.candle
        if candle is None or not candle.complete:
            result.skipped.append(f"{sub.symbol}:no_completed_candle")
            return
        if self._seen.get(sub.symbol) == candle.closed_at:
            result.skipped.append(f"{sub.symbol}:candle_already_processed")
            return

        previous = self._previous_candle(sub.symbol, candle)
        if previous is None:
            result.skipped.append(f"{sub.symbol}:no_previous_candle")
            return

        result.evaluated += 1
        signal = evaluate_candle(symbol=sub.symbol, candle=candle,
                                 previous=previous)
        # Mark the candle seen even when it does not trigger: it has been
        # evaluated, and re-evaluating it would waste a read every tick.
        self._seen[sub.symbol] = candle.closed_at
        if signal is None:
            result.skipped.append(f"{sub.symbol}:insufficient_candle_evidence")
            return
        if not signal.triggered:
            result.skipped.append(f"{sub.symbol}:no_break")
            return

        result.signals += 1
        store = self._scenario_store()
        if store is None:
            result.warnings.append("scenario_store_unavailable")
            return
        scenario = scenario_for(signal, node_id=self._node_id())
        # Idempotent on the derived id: re-processing the same candle after a
        # restart returns the stored Scenario and writes nothing.
        existing = store.get_scenario(scenario.scenario_id)
        stored = store.create_scenario(scenario) if existing is None else existing
        if existing is None:
            result.scenarios_created.append(stored.scenario_id)

        service = self._recommendations()
        if service is None:
            result.warnings.append("recommendation_service_unavailable")
            return
        recommendation = recommendation_for(signal, stored, quote=sub.quote,
                                           node_id=self._node_id())
        try:
            created = service.create(recommendation)
            # Immediately PROPOSED: a DRAFT is not decidable, and an operator
            # cannot act on a proposal that was never put forward.
            service.propose(created.recommendation_id,
                            reason="live-5a candle break")
            result.recommendations_created.append(created.recommendation_id)
        except Exception as exc:                                # noqa: BLE001
            # A duplicate is the normal case on replay, not an error.
            reason = getattr(exc, "reason", type(exc).__name__)
            result.warnings.append(f"recommendation_not_created:{reason}")

    def _previous_candle(self, symbol: str, newest):
        if self._history is None:
            return None
        try:
            bars = self._history(symbol) or []
        except Exception:                                       # noqa: BLE001
            return None
        # The bar immediately BEFORE the newest completed one, matched by close
        # time so ordering assumptions cannot silently pick the wrong bar.
        candidates = [b for b in bars
                      if getattr(b, "closed_at", None)
                      and getattr(b, "complete", False)
                      and str(b.closed_at) < str(newest.closed_at)]
        if not candidates:
            return None
        return sorted(candidates, key=lambda b: str(b.closed_at))[-1]
