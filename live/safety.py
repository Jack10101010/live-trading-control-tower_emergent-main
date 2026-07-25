"""Hard safety rails. Every intent passes through evaluate() before ANY broker
action; a block is terminal for that intent (recorded, published, never retried
automatically). Rails are config-frozen at process start.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation

from live.account_health import HealthConfigError, evaluate_health
from live.account_identity import IdentityConfigError
from live.config import SYMBOL
from live.intents import CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION
from live.state import (LEDGER_BLOCKED, LEDGER_CONFIRMED, LEDGER_PARTIAL, LEDGER_SENT,
                        LEDGER_SIMULATED)

ALLOWED = "allowed"

# Sentinel: the executor did not sample market conditions this cycle (e.g. the
# gateway is not connected, or there is no OPEN intent). The market-condition
# rails are then SKIPPED — distinct from ``None``, which means "sampled but
# unavailable/malformed" and BLOCKS an OPEN fail-closed.
MARKET_NOT_EVALUATED = object()

# Same contract for the LX-1 Slice 7 account-health sample: NOT_EVALUATED skips
# the health rails (not sampled — no OPEN / not connected); ``None`` means sampled
# but unavailable/malformed and BLOCKS an OPEN fail-closed.
HEALTH_NOT_EVALUATED = object()

# Small allowance for broker/VPS clock jitter when judging a tick "ahead of
# server time". A tick more than this many seconds in the future is malformed.
_CLOCK_SKEW_TOLERANCE_S = 2.0


@dataclass(frozen=True)
class RailVerdict:
    allowed: bool
    rail: str
    detail: str = ""


@dataclass(frozen=True)
class MarketCondition:
    """Immutable pre-trade market sample — plain scalars only, no SDK object.
    Produced by the gateway's read-only accessor, consumed by the OPEN rails.

    ``server_time_utc`` is the LOCAL/reference process clock (time-synced VPS) at
    sample time — NOT the broker's server clock — used only as the reference for
    how old ``tick_time_utc`` (the broker's last-tick time) is. Both must be
    timezone-aware UTC; the freshness rail rejects naive/mixed evidence."""
    symbol: str
    bid: float
    ask: float
    tick_time_utc: datetime
    server_time_utc: datetime


def _finite_pos(x) -> bool:
    """A usable price: numeric, finite, strictly > 0 — never bool/None/str."""
    if isinstance(x, bool) or x is None:
        return False
    if isinstance(x, (int, float)):
        return math.isfinite(x) and x > 0
    return False


def _age_seconds(market: MarketCondition):
    """server_time - tick_time in seconds, or None if either timestamp is missing,
    not a datetime, or naive (tz-aware UTC evidence is required — a naive or mixed
    pair is malformed, never subtracted/clamped). Never raises."""
    st, tt = market.server_time_utc, market.tick_time_utc
    if not isinstance(st, datetime) or not isinstance(tt, datetime):
        return None
    if st.tzinfo is None or st.utcoffset() is None or tt.tzinfo is None or tt.utcoffset() is None:
        return None   # require timezone-aware evidence on BOTH sides
    try:
        return (st - tt).total_seconds()
    except (TypeError, ValueError, OverflowError):
        return None


class SafetyRails:
    def __init__(self, config, state):
        self.config = config
        self.state = state

    def _kill_switch_on(self) -> bool:
        return self.config.kill_file.exists()

    def evaluate(self, intent, symbol: str, today: str,
                 market=MARKET_NOT_EVALUATED, health=HEALTH_NOT_EVALUATED) -> RailVerdict:
        # 1) global kill switch — blocks everything except engine-driven closes
        if self._kill_switch_on() and intent.action != CLOSE_POSITION:
            return RailVerdict(False, "kill_switch", str(self.config.kill_file))
        # 2) symbol whitelist — this instance trades EURUSD only
        if symbol != SYMBOL:
            return RailVerdict(False, "symbol_whitelist", f"{symbol} != {SYMBOL}")
        # 3) duplicate-order protection — idempotent ledger
        status = self.state.ledger_status(intent.intent_id)
        if status in (LEDGER_SENT, LEDGER_CONFIRMED, LEDGER_PARTIAL, LEDGER_SIMULATED):
            return RailVerdict(False, "duplicate_intent", f"already {status}")
        # 4) daily loss kill switch (opens only)
        if intent.action == OPEN_POSITION:
            realized = self.state.daily_realized_r(today)
            if realized <= -abs(self.config.daily_loss_limit_r):
                return RailVerdict(False, "daily_loss_limit",
                                   f"realized {realized:.2f}R <= -{self.config.daily_loss_limit_r}R")
            # 5) max open positions (mirror count)
            if self.state.open_mirror_count() >= self.config.max_open_positions:
                return RailVerdict(False, "max_open_positions",
                                   f"mirror at {self.state.open_mirror_count()}")
            # 6) account-health capital & permission rails (LX-1 Slice 7) —
            # OPEN-only, evaluated only when the executor sampled health this cycle
            # (gateway connected). Evaluated BEFORE market conditions: a capital /
            # permission problem is more fundamental than a transient spread.
            if health is not HEALTH_NOT_EVALUATED:
                hv = self._health_verdict(health)
                if not hv.allowed:
                    return hv
            # 7) pre-trade market-condition rails (LX-1 Slice 5) — OPEN-only,
            # evaluated only when the executor sampled market conditions this
            # cycle (gateway connected). CLOSE/MODIFY are never gated by these.
            if market is not MARKET_NOT_EVALUATED:
                mv = self._market_verdict(market)
                if not mv.allowed:
                    return mv
        # 6) modify/close must reference a mirrored position
        if intent.action in (MODIFY_STOP, CLOSE_POSITION):
            if self.state.mirror_ticket(intent.trade_id) is None:
                return RailVerdict(False, "unknown_position",
                                   f"no mirrored ticket for {intent.trade_id}")
        return RailVerdict(True, ALLOWED)

    def _health_verdict(self, health) -> RailVerdict:
        """OPEN account-health gate (LX-1 Slice 7): equity floor + trade/expert
        permission + expected-currency continuity. Fail-closed — an unavailable
        (None) sample, or a missing/malformed LIVE_MIN_EQUITY, blocks the OPEN. The
        first (fixed-order) reason becomes the rail; evidence is a bounded, JSON-
        safe string with no credentials/SDK objects."""
        try:
            policy = self.config.health_policy()
        except (HealthConfigError, IdentityConfigError):
            return RailVerdict(False, "account_health_unavailable",
                               "health policy misconfigured (LIVE_MIN_EQUITY/currency)")
        verdict = evaluate_health(health, policy)
        if verdict.allowed:
            return RailVerdict(True, ALLOWED)
        rail = verdict.reasons[0]
        ev = verdict.evidence
        if rail == "account_health_unavailable":
            detail = "account health unavailable"
        elif rail == "equity_floor":
            detail = f"equity {ev['equity']} < min {ev['min_equity']} {ev['currency']}"
        elif rail == "currency_mismatch":
            detail = f"currency {ev['currency']} != expected {ev['expected_currency']}"
        elif rail == "trading_not_allowed":
            detail = "account trading not allowed"
        elif rail == "expert_trading_not_allowed":
            detail = "expert/algo trading not allowed"
        else:
            detail = rail
        return RailVerdict(False, rail, detail)

    def _market_verdict(self, market) -> RailVerdict:
        """OPEN market-condition gate: spread ceiling + feed freshness. Fail-
        closed — an unavailable (None) or malformed sample blocks as stale_feed;
        the rail never trusts unvalidated numbers even though the gateway already
        rejects them. All evidence is a plain JSON-safe string."""
        if market is None:
            return RailVerdict(False, "stale_feed", "market conditions unavailable")
        # defensive re-validation of the sampled prices
        if not (_finite_pos(market.bid) and _finite_pos(market.ask)):
            return RailVerdict(False, "stale_feed",
                               f"malformed prices bid={market.bid!r} ask={market.ask!r}")
        if market.ask < market.bid:
            return RailVerdict(False, "stale_feed",
                               f"ask {market.ask} below bid {market.bid}")
        # spread ceiling — Decimal-exact so equality at the ceiling is ALLOWED
        # (block strictly-greater only) and clean quotes carry no float dust.
        try:
            spread = Decimal(str(market.ask)) - Decimal(str(market.bid))
            ceiling = Decimal(str(self.config.max_spread))
        except (InvalidOperation, ValueError):
            return RailVerdict(False, "stale_feed", "non-numeric spread comparison")
        if spread > ceiling:
            return RailVerdict(False, "spread_ceiling",
                               f"spread {float(spread)} > ceiling {float(ceiling)} "
                               f"(bid={market.bid}, ask={market.ask})")
        # feed freshness — server_time - tick_time
        age = _age_seconds(market)
        if age is None:
            return RailVerdict(False, "stale_feed", "malformed tick/server timestamps")
        if age > self.config.max_feed_age_s:
            return RailVerdict(False, "stale_feed",
                               f"tick age {age:.1f}s > max {self.config.max_feed_age_s}s")
        if age < -_CLOCK_SKEW_TOLERANCE_S:
            return RailVerdict(False, "stale_feed",
                               f"tick {(-age):.1f}s ahead of server time")
        return RailVerdict(True, ALLOWED)

    def record_block(self, intent, verdict: RailVerdict) -> None:
        self.state.ledger_set(intent.intent_id, LEDGER_BLOCKED,
                              {"rail": verdict.rail, "detail": verdict.detail})
