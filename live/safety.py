"""Hard safety rails. Every intent passes through evaluate() before ANY broker
action; a block is terminal for that intent (recorded, published, never retried
automatically). Rails are config-frozen at process start.
"""

from __future__ import annotations

from dataclasses import dataclass

from live.config import SYMBOL
from live.intents import CLOSE_POSITION, MODIFY_STOP, OPEN_POSITION
from live.state import LEDGER_BLOCKED, LEDGER_CONFIRMED, LEDGER_SENT, LEDGER_SIMULATED

ALLOWED = "allowed"


@dataclass(frozen=True)
class RailVerdict:
    allowed: bool
    rail: str
    detail: str = ""


class SafetyRails:
    def __init__(self, config, state):
        self.config = config
        self.state = state

    def _kill_switch_on(self) -> bool:
        return self.config.kill_file.exists()

    def evaluate(self, intent, symbol: str, today: str) -> RailVerdict:
        # 1) global kill switch — blocks everything except engine-driven closes
        if self._kill_switch_on() and intent.action != CLOSE_POSITION:
            return RailVerdict(False, "kill_switch", str(self.config.kill_file))
        # 2) symbol whitelist — this instance trades EURUSD only
        if symbol != SYMBOL:
            return RailVerdict(False, "symbol_whitelist", f"{symbol} != {SYMBOL}")
        # 3) duplicate-order protection — idempotent ledger
        status = self.state.ledger_status(intent.intent_id)
        if status in (LEDGER_SENT, LEDGER_CONFIRMED, LEDGER_SIMULATED):
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
        # 6) modify/close must reference a mirrored position
        if intent.action in (MODIFY_STOP, CLOSE_POSITION):
            if self.state.mirror_ticket(intent.trade_id) is None:
                return RailVerdict(False, "unknown_position",
                                   f"no mirrored ticket for {intent.trade_id}")
        return RailVerdict(True, ALLOWED)

    def record_block(self, intent, verdict: RailVerdict) -> None:
        self.state.ledger_set(intent.intent_id, LEDGER_BLOCKED,
                              {"rail": verdict.rail, "detail": verdict.detail})
